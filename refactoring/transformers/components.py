import ast


class ComponentTransformer(ast.NodeTransformer):
    """Replace outer component-extraction loops (wrapping inner reachable patterns)
    with connected_components() DSL call.

    Handles three variants:

    Pattern 1 (e9fc42f2): Collect components into frozenset via insert
        x3 = frozenset()
        x4 = remaining
        while size(x4) > ZERO:
            x5 = first(totuple(x4))
            x6 = initset(x5)          # or frozenset({x5})
            ... inner reachable ...    # x6 = reachable(x6, x4) or while loop
            x4 = difference(x4, x6)
            x3 = insert(x6, x3)
      → x3 = connected_components(remaining)

    Pattern 2 (825aa9e9): List-based pop with set collection
        x1 = set()
        x2 = list(x0)
        while x2:
            x3 = x2.pop()
            ... inner reachable on x0 ...
            x1.add(component)
            x2 = list(difference(frozenset(x2), component))
      → return frozenset(connected_components(x0))

    Pattern 3 (37d3e8b2): Count components (returns count, not components)
        x1 = frozenset(x0)
        x2 = 0
        while size(x1) > 0:
            x2 = x2 + 1
            x3 = first(totuple(x1))
            ... inner reachable ...
            x1 = difference(x1, component)
      → x2 = size(connected_components(x0))
    """

    def __init__(self, task_id=None):
        super().__init__()
        self.task_id = task_id
        self.changes = 0

    def visit_FunctionDef(self, node):
        node.body = self._process_body(node.body)
        return self.generic_visit(node)

    def _process_body(self, body):
        new_body = []
        i = 0
        while i < len(body):
            stmt = body[i]
            # Recurse into nested blocks
            if isinstance(stmt, ast.If):
                stmt.body = self._process_body(stmt.body)
                if stmt.orelse:
                    stmt.orelse = self._process_body(stmt.orelse)
                new_body.append(stmt)
                i += 1
                continue
            if isinstance(stmt, (ast.For, ast.While)):
                stmt.body = self._process_body(stmt.body)

            # Try Pattern 1 (frozenset insert collection)
            res = self._match_pattern1(body, i)
            if res:
                replacement, skip = res
                new_body.extend(replacement)
                i += skip
                self.changes += 1
                continue

            # Try Pattern 2 (list-pop with set.add)
            res = self._match_pattern2(body, i)
            if res:
                replacement, skip = res
                new_body.extend(replacement)
                i += skip
                self.changes += 1
                continue

            # Try Pattern 3 (counting)
            res = self._match_pattern3(body, i)
            if res:
                replacement, skip = res
                new_body.extend(replacement)
                i += skip
                self.changes += 1
                continue

            new_body.append(stmt)
            i += 1
        return new_body

    def _match_pattern1(self, body, idx):
        """Pattern 1: frozenset collection with insert.

        [idx]   collection = frozenset()
        [idx+1] remaining = domain_expr
        [idx+2] while size(remaining) > ZERO:
                  seed = first(totuple(remaining))
                  component = initset(seed) or frozenset({seed})
                  ... reachable call or inner while ...
                  remaining = difference(remaining, component)
                  collection = insert(component, collection)
        """
        if idx + 2 >= len(body):
            return None

        s0 = body[idx]
        s1 = body[idx + 1]
        s2 = body[idx + 2]

        if not isinstance(s0, ast.Assign) or not isinstance(s1, ast.Assign):
            return None
        if not isinstance(s2, ast.While):
            return None

        collection_var = self._get_target(s0)
        remaining_var = self._get_target(s1)
        if not collection_var or not remaining_var:
            return None

        # collection = frozenset()
        if not self._is_empty_frozenset(s0.value):
            return None

        # while size(remaining) > ZERO
        if not self._is_size_positive_test(s2.test, remaining_var):
            return None

        # Parse the while body
        wbody = s2.body
        domain_expr = s1.value  # the original domain expression

        # Validate while body structure
        if not self._validate_component_loop_body(wbody, remaining_var, collection_var):
            return None

        # Build: collection = connected_components(domain_expr)
        cc_call = self._make_call('connected_components', [domain_expr])
        new_assign = ast.Assign(
            targets=[ast.Name(id=collection_var, ctx=ast.Store())],
            value=cc_call, lineno=0
        )
        return [new_assign], 3  # consumed s0, s1, while

    def _match_pattern2(self, body, idx):
        """Pattern 2: list-pop with set.add (825aa9e9).

        [idx]   result = set()
        [idx+1] worklist = list(domain)
        [idx+2] while worklist:
                  elem = worklist.pop()
                  ... inner reachable on domain ...
                  result.add(component)
                  worklist = list(difference(frozenset(worklist), component))
        """
        if idx + 2 >= len(body):
            return None

        s0 = body[idx]
        s1 = body[idx + 1]
        s2 = body[idx + 2]

        if not isinstance(s0, ast.Assign) or not isinstance(s1, ast.Assign):
            return None
        if not isinstance(s2, ast.While):
            return None

        result_var = self._get_target(s0)
        worklist_var = self._get_target(s1)
        if not result_var or not worklist_var:
            return None

        # result = set()
        if not self._is_set_call(s0.value):
            return None

        # worklist = list(domain)
        if not self._is_list_call(s1.value):
            return None
        if len(s1.value.args) != 1 or not isinstance(s1.value.args[0], ast.Name):
            return None
        domain_var = s1.value.args[0].id

        # while worklist:
        if not (isinstance(s2.test, ast.Name) and s2.test.id == worklist_var):
            return None

        # Check body has .pop() and .add() and inner reachable pattern
        wbody = s2.body
        has_pop = False
        has_add = False
        has_reachable = False

        for stmt in wbody:
            src = ast.unparse(stmt)
            if '.pop()' in src:
                has_pop = True
            if f'{result_var}.add(' in src:
                has_add = True
            if 'mapply(dneighbors' in src or 'mapply(neighbors' in src or 'reachable(' in src:
                has_reachable = True

        if not (has_pop and has_add and has_reachable):
            return None

        # Build: result_var = connected_components(domain_var)
        cc_call = self._make_call('connected_components', [
            ast.Name(id=domain_var, ctx=ast.Load())
        ])
        # The function returns frozenset(result), so we assign frozenset directly
        new_assign = ast.Assign(
            targets=[ast.Name(id=result_var, ctx=ast.Store())],
            value=cc_call, lineno=0
        )
        return [new_assign], 3

    def _match_pattern3(self, body, idx):
        """Pattern 3: counting components (37d3e8b2).

        [idx]   remaining = frozenset(domain)
        [idx+1] count = 0
        [idx+2] while size(remaining) > 0:
                  count = count + 1
                  seed = first(totuple(remaining))
                  ... inner reachable ...
                  remaining = difference(remaining, component)
        """
        if idx + 2 >= len(body):
            return None

        s0 = body[idx]
        s1 = body[idx + 1]
        s2 = body[idx + 2]

        if not isinstance(s0, ast.Assign) or not isinstance(s1, ast.Assign):
            return None
        if not isinstance(s2, ast.While):
            return None

        remaining_var = self._get_target(s0)
        count_var = self._get_target(s1)
        if not remaining_var or not count_var:
            return None

        # remaining = frozenset(domain)
        if not (isinstance(s0.value, ast.Call) and
                isinstance(s0.value.func, ast.Name) and
                s0.value.func.id == 'frozenset' and
                len(s0.value.args) == 1):
            return None
        domain_expr = s0.value.args[0]

        # count = 0
        if not (isinstance(s1.value, ast.Constant) and s1.value.value == 0):
            return None

        # while size(remaining) > 0
        if not self._is_size_positive_test(s2.test, remaining_var):
            return None

        # Check body has increment of count and inner reachable
        wbody = s2.body
        has_increment = False
        has_reachable = False
        has_difference = False

        for stmt in wbody:
            src = ast.unparse(stmt)
            if f'{count_var} = {count_var} + 1' in src or f'{count_var} + 1' in src:
                has_increment = True
            if 'mapply(dneighbors' in src or 'mapply(neighbors' in src or 'reachable(' in src:
                has_reachable = True
            if 'difference(' in src and remaining_var in src:
                has_difference = True

        if not (has_increment and has_reachable and has_difference):
            return None

        # Build: count = size(connected_components(domain))
        cc_call = self._make_call('connected_components', [domain_expr])
        size_call = self._make_call('size', [cc_call])
        new_assign = ast.Assign(
            targets=[ast.Name(id=count_var, ctx=ast.Store())],
            value=size_call, lineno=0
        )
        return [new_assign], 3

    def _validate_component_loop_body(self, wbody, remaining_var, collection_var):
        """Validate the while body of Pattern 1 has the expected structure."""
        if len(wbody) < 4:
            return False

        # First stmt: seed = first(totuple(remaining))
        s0 = wbody[0]
        if not isinstance(s0, ast.Assign):
            return False
        src0 = ast.unparse(s0.value)
        if f'first(totuple({remaining_var}))' not in src0:
            return False

        # Last two stmts: remaining = difference(remaining, comp); collection = insert(comp, collection)
        last_two = wbody[-2:]
        has_difference = False
        has_insert = False
        for stmt in last_two:
            src = ast.unparse(stmt)
            if f'difference({remaining_var}' in src and self._get_target(stmt) == remaining_var:
                has_difference = True
            if f'insert(' in src and self._get_target(stmt) == collection_var:
                has_insert = True

        return has_difference and has_insert

    # ── Helpers ──

    def _get_target(self, stmt):
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
                isinstance(stmt.targets[0], ast.Name)):
            return stmt.targets[0].id
        return None

    def _is_empty_frozenset(self, node):
        return (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Name) and
                node.func.id == 'frozenset' and
                len(node.args) == 0)

    def _is_set_call(self, node):
        return (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Name) and
                node.func.id == 'set' and
                len(node.args) == 0)

    def _is_list_call(self, node):
        return (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Name) and
                node.func.id == 'list')

    def _is_size_positive_test(self, test, var_name):
        """Check test is size(var) > 0/ZERO."""
        # size(var) > 0
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            if isinstance(test.ops[0], ast.Gt):
                if (isinstance(test.left, ast.Call) and
                        isinstance(test.left.func, ast.Name) and
                        test.left.func.id == 'size' and
                        len(test.left.args) == 1 and
                        isinstance(test.left.args[0], ast.Name) and
                        test.left.args[0].id == var_name):
                    c = test.comparators[0]
                    if isinstance(c, ast.Constant) and c.value == 0:
                        return True
                    if isinstance(c, ast.Name) and c.id == 'ZERO':
                        return True
        # greater(size(var), ZERO)
        if (isinstance(test, ast.Call) and isinstance(test.func, ast.Name) and
                test.func.id == 'greater' and len(test.args) == 2):
            arg0 = test.args[0]
            if (isinstance(arg0, ast.Call) and isinstance(arg0.func, ast.Name) and
                    arg0.func.id == 'size' and len(arg0.args) == 1 and
                    isinstance(arg0.args[0], ast.Name) and arg0.args[0].id == var_name):
                c = test.args[1]
                if isinstance(c, ast.Name) and c.id == 'ZERO':
                    return True
                if isinstance(c, ast.Constant) and c.value == 0:
                    return True
        return False

    def _make_call(self, name, args):
        return ast.Call(
            func=ast.Name(id=name, ctx=ast.Load()),
            args=args,
            keywords=[]
        )
