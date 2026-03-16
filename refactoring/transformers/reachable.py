import ast


class ReachableTransformer(ast.NodeTransformer):
    """Replace flood-fill / BFS fixpoint while-loops with reachable() DSL call.

    Pattern A (frozenset-equality termination):
        x4 = frozenset({x3})   # or seed expr
        x5 = frozenset()
        while x4 != x5:
            x5 = frozenset(x4)  # or x5 = x4
            x6 = mapply(dneighbors, x4)
            x7 = intersection(x6, domain)
            x4 = combine(x4, x7)
      → x4 = reachable(seed, domain)

    Pattern B (boolean-flag termination):
        x3 = True  # or T
        while x3:
            x4 = mapply(dneighbors, x2)
            x5 = intersection(x4, domain)
            x6 = combine(x2, x5)
            x3 = greater(size(x6), size(x2))
            x2 = x6
      → x2 = reachable(seed, domain)

    Pattern B2 (difference-based frontier):
        x4 = True
        while x4:
            x5 = mapply(dneighbors, x3)
            x6 = intersection(x5, domain)
            x7 = difference(x6, x2)
            x8 = combine(x2, x7)
            x4 = size(x7) > 0
            x3 = x7
            x2 = x8
      → x2 = reachable(seed, domain)

    Pattern B3 (greater(size(diff), ZERO) flag):
        x51 = T
        while x51:
            x52 = mapply(dneighbors, x50)
            x53 = intersection(x52, domain)
            x54 = difference(x53, x50)
            x51 = greater(size(x54), ZERO)
            x50 = combine(x50, x54)
      → x50 = reachable(seed, domain)
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
            # Recurse into if/elif/else blocks
            if isinstance(stmt, ast.If):
                stmt.body = self._process_body(stmt.body)
                if stmt.orelse:
                    stmt.orelse = self._process_body(stmt.orelse)
                new_body.append(stmt)
                i += 1
                continue
            if isinstance(stmt, (ast.For, ast.While)):
                stmt.body = self._process_body(stmt.body)
                new_body.append(stmt)
                i += 1
                continue

            # Try Pattern A: needs init stmts before while
            res = self._match_pattern_a(body, i)
            if res:
                replacement_stmts, skip_count = res
                new_body.extend(replacement_stmts)
                i += skip_count
                self.changes += 1
                continue

            # Try Pattern B: needs init stmt before while
            res = self._match_pattern_b(body, i)
            if res:
                if len(res) == 3:
                    replacement_stmts, skip_count, start_idx = res
                    # Remove consumed init stmts from new_body (they precede idx)
                    consumed_before = i - start_idx
                    if consumed_before > 0:
                        new_body = new_body[:-consumed_before]
                    new_body.extend(replacement_stmts)
                    i += skip_count - consumed_before
                else:
                    replacement_stmts, skip_count = res
                    new_body.extend(replacement_stmts)
                    i += skip_count
                self.changes += 1
                continue

            new_body.append(stmt)
            i += 1
        return new_body

    def _match_pattern_a(self, body, idx):
        """Match Pattern A: seed init, sentinel init, while seed != sentinel loop.

        Looks for sequence starting at idx:
          [idx]   seed_var = seed_expr      (e.g. frozenset({x3}))
          [idx+1] sentinel_var = frozenset()
          [idx+2] while seed_var != sentinel_var:
                    sentinel_var = frozenset(seed_var) | sentinel_var = seed_var
                    expanded = mapply(dneighbors/neighbors, seed_var)
                    filtered = intersection(expanded, domain)
                    seed_var = combine(seed_var, filtered)

        Also matches variant where sentinel init comes first (f1bcbc2c):
          [idx]   seed_var = intersection(x0, x1)  -- seed
          [idx+1] sentinel_copy = seed_var
          [idx+2] sentinel_var = frozenset()
          [idx+3] while sentinel_copy != sentinel_var: ...
        """
        # Need at least 3 statements: seed init, sentinel init, while
        if idx + 2 >= len(body):
            return None

        # Try standard 3-statement pattern
        res = self._try_pattern_a_3stmt(body, idx)
        if res:
            return res

        # Try f1bcbc2c variant: seed = expr; copy = seed; sentinel = frozenset(); while copy != sentinel
        if idx + 3 < len(body):
            res = self._try_pattern_a_4stmt(body, idx)
            if res:
                return res

        return None

    def _try_pattern_a_3stmt(self, body, idx):
        """Standard 3-statement Pattern A."""
        seed_stmt = body[idx]
        sentinel_stmt = body[idx + 1]
        while_stmt = body[idx + 2]

        if not isinstance(seed_stmt, ast.Assign) or not isinstance(sentinel_stmt, ast.Assign):
            return None
        if not isinstance(while_stmt, ast.While):
            return None

        seed_var = self._get_assign_target(seed_stmt)
        sentinel_var = self._get_assign_target(sentinel_stmt)
        if not seed_var or not sentinel_var:
            return None

        # sentinel must be initialized to frozenset()
        if not self._is_empty_frozenset(sentinel_stmt.value):
            return None

        # while test: seed_var != sentinel_var
        if not self._is_neq_compare(while_stmt.test, seed_var, sentinel_var):
            return None

        # Parse the while body
        result = self._parse_pattern_a_body(while_stmt.body, seed_var, sentinel_var)
        if not result:
            return None
        domain_expr, nbfn = result

        seed_expr = seed_stmt.value
        reachable_call = self._make_reachable_call(seed_expr, domain_expr, nbfn)
        new_assign = ast.Assign(
            targets=[ast.Name(id=seed_var, ctx=ast.Store())],
            value=reachable_call,
            lineno=0
        )
        return [new_assign], 3  # consumed seed_stmt, sentinel_stmt, while_stmt

    def _try_pattern_a_4stmt(self, body, idx):
        """f1bcbc2c variant: seed=expr, copy=seed, sentinel=frozenset(), while copy != sentinel."""
        seed_stmt = body[idx]
        copy_stmt = body[idx + 1]
        sentinel_stmt = body[idx + 2]
        while_stmt = body[idx + 3]

        if not all(isinstance(s, ast.Assign) for s in [seed_stmt, copy_stmt, sentinel_stmt]):
            return None
        if not isinstance(while_stmt, ast.While):
            return None

        seed_var = self._get_assign_target(seed_stmt)
        copy_var = self._get_assign_target(copy_stmt)
        sentinel_var = self._get_assign_target(sentinel_stmt)
        if not seed_var or not copy_var or not sentinel_var:
            return None

        # copy = seed_var
        if not (isinstance(copy_stmt.value, ast.Name) and copy_stmt.value.id == seed_var):
            return None

        # sentinel = frozenset()
        if not self._is_empty_frozenset(sentinel_stmt.value):
            return None

        # while copy != sentinel
        if not self._is_neq_compare(while_stmt.test, copy_var, sentinel_var):
            return None

        # Parse while body — operates on copy_var with sentinel_var snapshot
        result = self._parse_pattern_a_body(while_stmt.body, copy_var, sentinel_var)
        if not result:
            return None
        domain_expr, nbfn = result

        seed_expr = seed_stmt.value
        reachable_call = self._make_reachable_call(seed_expr, domain_expr, nbfn)
        new_assign = ast.Assign(
            targets=[ast.Name(id=copy_var, ctx=ast.Store())],
            value=reachable_call,
            lineno=0
        )
        return [new_assign], 4  # consumed 4 statements

    def _parse_pattern_a_body(self, while_body, accum_var, sentinel_var):
        """Parse body of Pattern A while loop.

        Expected body (3-4 statements):
          sentinel = frozenset(accum) | sentinel = accum
          expanded = mapply(dneighbors/neighbors, accum)
          filtered = intersection(expanded, domain)
          accum = combine(accum, filtered)

        Returns (domain_expr, 'dneighbors'|'neighbors') or None.
        """
        if len(while_body) < 3 or len(while_body) > 5:
            return None

        stmts = while_body
        si = 0

        # First: sentinel copy (sentinel = frozenset(accum) or sentinel = accum)
        s0 = stmts[si]
        if not isinstance(s0, ast.Assign):
            return None
        tgt = self._get_assign_target(s0)
        if tgt != sentinel_var:
            return None
        # Verify it's copying accum_var
        if isinstance(s0.value, ast.Call):
            if not (self._is_name_call(s0.value, 'frozenset') and
                    len(s0.value.args) == 1 and
                    isinstance(s0.value.args[0], ast.Name) and
                    s0.value.args[0].id == accum_var):
                return None
        elif isinstance(s0.value, ast.Name):
            if s0.value.id != accum_var:
                return None
        else:
            return None
        si += 1

        # mapply(dneighbors/neighbors, accum)
        s1 = stmts[si]
        if not isinstance(s1, ast.Assign):
            return None
        expanded_var = self._get_assign_target(s1)
        if not self._is_mapply_neighbors(s1.value, accum_var):
            return None
        nbfn = self._get_neighbor_fn(s1.value)
        si += 1

        # intersection(expanded, domain)
        s2 = stmts[si]
        if not isinstance(s2, ast.Assign):
            return None
        filtered_var = self._get_assign_target(s2)
        if not self._is_call(s2.value, 'intersection', 2):
            return None
        if not (isinstance(s2.value.args[0], ast.Name) and s2.value.args[0].id == expanded_var):
            return None
        domain_expr = s2.value.args[1]
        si += 1

        # accum = combine(accum, filtered)
        s3 = stmts[si]
        if not isinstance(s3, ast.Assign):
            return None
        if self._get_assign_target(s3) != accum_var:
            return None
        if not self._is_call(s3.value, 'combine', 2):
            return None
        if not (isinstance(s3.value.args[0], ast.Name) and s3.value.args[0].id == accum_var):
            return None
        if not (isinstance(s3.value.args[1], ast.Name) and s3.value.args[1].id == filtered_var):
            return None

        return domain_expr, nbfn

    def _match_pattern_b(self, body, idx):
        """Match Pattern B: flag init, while flag loop with growth check.

        Looks for:
          [idx]   flag_var = True/T
          [idx+1] while flag_var:
                    ... mapply, intersection, combine, greater(size...) ...
        """
        if idx + 1 >= len(body):
            return None

        flag_stmt = body[idx]
        while_stmt = body[idx + 1]

        if not isinstance(flag_stmt, ast.Assign) or not isinstance(while_stmt, ast.While):
            return None

        flag_var = self._get_assign_target(flag_stmt)
        if not flag_var:
            return None

        # flag = True or flag = T
        if not self._is_true_value(flag_stmt.value):
            return None

        # while flag_var:
        if not (isinstance(while_stmt.test, ast.Name) and while_stmt.test.id == flag_var):
            return None

        # Try different body shapes
        result = self._parse_pattern_b_body(while_stmt.body, flag_var)
        if not result:
            result = self._parse_pattern_b2_body(while_stmt.body, flag_var)
        if not result:
            result = self._parse_pattern_b3_body(while_stmt.body, flag_var)
        if not result:
            return None

        accum_var, seed_var, domain_expr, nbfn, extra_vars = result

        # Find the seed initialization: scan backwards from idx
        seed_expr = self._find_seed_init(body, idx, seed_var)

        reachable_call = self._make_reachable_call(
            ast.Name(id=seed_var, ctx=ast.Load()) if seed_expr is None else seed_expr,
            domain_expr, nbfn
        )
        new_assign = ast.Assign(
            targets=[ast.Name(id=accum_var, ctx=ast.Store())],
            value=reachable_call,
            lineno=0
        )

        # Count how many init stmts before flag to consume
        # (accum init, frontier init, etc.)
        consumed_before = 0
        vars_to_consume = {accum_var} | (extra_vars or set())
        for j in range(idx - 1, -1, -1):
            s = body[j]
            tgt = self._get_assign_target(s)
            if tgt in vars_to_consume:
                consumed_before += 1
                vars_to_consume.discard(tgt)
            else:
                break
            if not vars_to_consume:
                break

        start = idx - consumed_before
        skip = consumed_before + 2  # consumed inits + flag_stmt + while_stmt
        return [new_assign], skip, start

    def _parse_pattern_b_body(self, while_body, flag_var):
        """Pattern B: simple growth check.

        Body (5 stmts):
          expanded = mapply(dneighbors, accum)
          filtered = intersection(expanded, domain)
          combined = combine(accum, filtered)
          flag = greater(size(combined), size(accum))
          accum = combined
        """
        if len(while_body) != 5:
            return None

        s0, s1, s2, s3, s4 = while_body
        if not all(isinstance(s, ast.Assign) for s in while_body):
            return None

        # s0: expanded = mapply(dneighbors/neighbors, accum)
        expanded_var = self._get_assign_target(s0)
        if not expanded_var:
            return None
        accum_var = self._get_mapply_target(s0.value)
        if not accum_var:
            return None
        nbfn = self._get_neighbor_fn(s0.value)

        # s1: filtered = intersection(expanded, domain)
        filtered_var = self._get_assign_target(s1)
        if not self._is_call(s1.value, 'intersection', 2):
            return None
        if not (isinstance(s1.value.args[0], ast.Name) and s1.value.args[0].id == expanded_var):
            return None
        domain_expr = s1.value.args[1]

        # s2: combined = combine(accum, filtered)
        combined_var = self._get_assign_target(s2)
        if not self._is_call(s2.value, 'combine', 2):
            return None
        if not (isinstance(s2.value.args[0], ast.Name) and s2.value.args[0].id == accum_var):
            return None
        if not (isinstance(s2.value.args[1], ast.Name) and s2.value.args[1].id == filtered_var):
            return None

        # s3: flag = greater(size(combined), size(accum))
        if self._get_assign_target(s3) != flag_var:
            return None
        if not self._is_growth_check(s3.value, combined_var, accum_var):
            return None

        # s4: accum = combined
        if self._get_assign_target(s4) != accum_var:
            return None
        if not (isinstance(s4.value, ast.Name) and s4.value.id == combined_var):
            return None

        return accum_var, accum_var, domain_expr, nbfn, set()

    def _parse_pattern_b2_body(self, while_body, flag_var):
        """Pattern B2: difference-based frontier (e69241bd).

        Body (6 stmts):
          expanded = mapply(dneighbors, frontier)
          filtered = intersection(expanded, domain)
          new_cells = difference(filtered, accum)
          new_accum = combine(accum, new_cells)
          flag = size(new_cells) > 0
          frontier = new_cells
          accum = new_accum
        """
        if len(while_body) not in (6, 7):
            return None
        if not all(isinstance(s, ast.Assign) for s in while_body):
            return None

        s0 = while_body[0]
        frontier_var = self._get_mapply_target(s0.value)
        if not frontier_var:
            return None
        expanded_var = self._get_assign_target(s0)
        nbfn = self._get_neighbor_fn(s0.value)

        s1 = while_body[1]
        filtered_var = self._get_assign_target(s1)
        if not self._is_call(s1.value, 'intersection', 2):
            return None
        domain_expr = s1.value.args[1]

        s2 = while_body[2]
        diff_var = self._get_assign_target(s2)
        if not self._is_call(s2.value, 'difference', 2):
            return None

        s3 = while_body[3]
        new_accum_var = self._get_assign_target(s3)
        if not self._is_call(s3.value, 'combine', 2):
            return None

        # Find accum_var from combine's first arg
        if not isinstance(s3.value.args[0], ast.Name):
            return None
        accum_var = s3.value.args[0].id

        # flag update: size(diff) > 0 or greater(size(diff), ZERO)
        s4 = while_body[4]
        if self._get_assign_target(s4) != flag_var:
            return None
        # Accept: size(x) > 0, greater(size(x), ZERO)
        if not self._is_size_positive_check(s4.value, diff_var):
            return None

        # frontier = new_cells, accum = new_accum (order may vary)
        remaining = while_body[5:]
        found_frontier = False
        found_accum = False
        for s in remaining:
            tgt = self._get_assign_target(s)
            if tgt == frontier_var:
                found_frontier = True
            elif tgt == accum_var:
                found_accum = True

        if not (found_frontier and found_accum):
            return None

        return accum_var, accum_var, domain_expr, nbfn, {frontier_var}

    def _parse_pattern_b3_body(self, while_body, flag_var):
        """Pattern B3: difference then greater(size(diff), ZERO) (465b7d93).

        Body (5 stmts):
          expanded = mapply(dneighbors, accum)
          filtered = intersection(expanded, domain)
          diff = difference(filtered, accum)
          flag = greater(size(diff), ZERO)
          accum = combine(accum, diff)
        """
        if len(while_body) != 5:
            return None
        if not all(isinstance(s, ast.Assign) for s in while_body):
            return None

        s0 = while_body[0]
        accum_var = self._get_mapply_target(s0.value)
        if not accum_var:
            return None
        expanded_var = self._get_assign_target(s0)
        nbfn = self._get_neighbor_fn(s0.value)

        s1 = while_body[1]
        filtered_var = self._get_assign_target(s1)
        if not self._is_call(s1.value, 'intersection', 2):
            return None
        domain_expr = s1.value.args[1]

        s2 = while_body[2]
        diff_var = self._get_assign_target(s2)
        if not self._is_call(s2.value, 'difference', 2):
            return None
        # difference(filtered, accum)
        if not (isinstance(s2.value.args[0], ast.Name) and s2.value.args[0].id == filtered_var):
            return None
        if not (isinstance(s2.value.args[1], ast.Name) and s2.value.args[1].id == accum_var):
            return None

        s3 = while_body[3]
        if self._get_assign_target(s3) != flag_var:
            return None
        if not self._is_size_positive_check(s3.value, diff_var):
            return None

        s4 = while_body[4]
        if self._get_assign_target(s4) != accum_var:
            return None
        if not self._is_call(s4.value, 'combine', 2):
            return None

        return accum_var, accum_var, domain_expr, nbfn, set()

    # ── Helpers ──

    def _find_seed_init(self, body, before_idx, var_name):
        """Scan backwards to find var_name initialization before idx."""
        for i in range(before_idx - 1, -1, -1):
            s = body[i]
            if isinstance(s, ast.Assign) and self._get_assign_target(s) == var_name:
                return s.value
        return None

    def _get_assign_target(self, stmt):
        """Get single Name target from assignment, or None."""
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
                isinstance(stmt.targets[0], ast.Name)):
            return stmt.targets[0].id
        return None

    def _is_empty_frozenset(self, node):
        """Check if node is frozenset() call with no args."""
        return (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Name) and
                node.func.id == 'frozenset' and
                len(node.args) == 0)

    def _is_neq_compare(self, test, left_name, right_name):
        """Check if test is 'left_name != right_name'."""
        if not isinstance(test, ast.Compare):
            return False
        if len(test.ops) != 1 or not isinstance(test.ops[0], ast.NotEq):
            return False
        if len(test.comparators) != 1:
            return False
        left = test.left
        right = test.comparators[0]
        return (isinstance(left, ast.Name) and isinstance(right, ast.Name) and
                left.id == left_name and right.id == right_name)

    def _is_true_value(self, node):
        """Check if node is True or T."""
        if isinstance(node, ast.Constant) and node.value is True:
            return True
        if isinstance(node, ast.Name) and node.id == 'T':
            return True
        return False

    def _is_mapply_neighbors(self, node, expected_arg):
        """Check if node is mapply(dneighbors/neighbors, expected_arg)."""
        if not self._is_call(node, 'mapply', 2):
            return False
        if not isinstance(node.args[0], ast.Name):
            return False
        if node.args[0].id not in ('dneighbors', 'neighbors'):
            return False
        if not (isinstance(node.args[1], ast.Name) and node.args[1].id == expected_arg):
            return False
        return True

    def _get_mapply_target(self, node):
        """Get the variable passed to mapply(dneighbors/neighbors, var), or None."""
        if not isinstance(node, ast.Call):
            return None
        if not isinstance(node.func, ast.Name) or node.func.id != 'mapply':
            return None
        if len(node.args) != 2:
            return None
        if not isinstance(node.args[0], ast.Name):
            return None
        if node.args[0].id not in ('dneighbors', 'neighbors'):
            return None
        if not isinstance(node.args[1], ast.Name):
            return None
        return node.args[1].id

    def _get_neighbor_fn(self, node):
        """Get neighbor function name from mapply call."""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'mapply':
            if isinstance(node.args[0], ast.Name):
                return node.args[0].id
        return 'dneighbors'

    def _is_call(self, node, func_name, nargs):
        """Check if node is func_name(...) with exactly nargs positional args."""
        return (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Name) and
                node.func.id == func_name and
                len(node.args) == nargs)

    def _is_name_call(self, node, func_name):
        """Check if node is a call to func_name."""
        return (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Name) and
                node.func.id == func_name)

    def _is_growth_check(self, node, bigger_var, smaller_var):
        """Check if node is greater(size(bigger), size(smaller))."""
        if not self._is_call(node, 'greater', 2):
            return False
        arg0, arg1 = node.args
        if not (self._is_call(arg0, 'size', 1) and self._is_call(arg1, 'size', 1)):
            return False
        if not (isinstance(arg0.args[0], ast.Name) and arg0.args[0].id == bigger_var):
            return False
        if not (isinstance(arg1.args[0], ast.Name) and arg1.args[0].id == smaller_var):
            return False
        return True

    def _is_size_positive_check(self, node, var_name):
        """Check if node is size(var) > 0 or greater(size(var), ZERO)."""
        # greater(size(var), ZERO)
        if self._is_call(node, 'greater', 2):
            if self._is_call(node.args[0], 'size', 1):
                if isinstance(node.args[0].args[0], ast.Name) and node.args[0].args[0].id == var_name:
                    if isinstance(node.args[1], ast.Name) and node.args[1].id == 'ZERO':
                        return True
                    if isinstance(node.args[1], ast.Constant) and node.args[1].value == 0:
                        return True
        # size(var) > 0
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Gt):
            if self._is_call(node.left, 'size', 1):
                if isinstance(node.left.args[0], ast.Name) and node.left.args[0].id == var_name:
                    if len(node.comparators) == 1:
                        c = node.comparators[0]
                        if isinstance(c, ast.Constant) and c.value == 0:
                            return True
        return False

    def _make_reachable_call(self, seed_expr, domain_expr, nbfn):
        """Build AST for reachable(seed, domain[, T])."""
        args = [seed_expr, domain_expr]
        if nbfn == 'neighbors':
            args.append(ast.Name(id='T', ctx=ast.Load()))
        return ast.Call(
            func=ast.Name(id='reachable', ctx=ast.Load()),
            args=args,
            keywords=[]
        )
