import ast
import copy


class LambdaFoldTransformer(ast.NodeTransformer):
    """Replace complex while loops with fold() plus nested helper functions.

    Handles multiple accumulators by creating helper functions with tuple state.
    More aggressive than WhileFoldTransformer.
    """
    def __init__(self, task_id):
        super().__init__()
        self.task_id = task_id
        self.changes = 0
        self.helper_count = 0
        self.all_defs = {}

    def visit_FunctionDef(self, node):
        if node.name.startswith('verify_'):
            self.task_id = node.name[7:]
            self.helper_count = 0

        old_defs = self.all_defs
        self.all_defs = {}
        for s in node.body:
            if isinstance(s, ast.Assign):
                for t in s.targets:
                    if isinstance(t, ast.Name):
                        self.all_defs[t.id] = s

        node.body = self._process_body(node.body)
        self.all_defs = old_defs
        return node

    def _process_body(self, body):
        new_body = []
        i = 0
        while i < len(body):
            stmt = body[i]
            if isinstance(stmt, ast.While):
                res = self._match_complex_fold(body, i)
                if res:
                    fold_stmts, helper_def, consumed_nodes = res
                    helper_def.body = self._process_body(helper_def.body)
                    new_body = [s for s in new_body if id(s) not in consumed_nodes]
                    new_body.append(helper_def)
                    new_body.extend(fold_stmts)
                    self.changes += 1
                    i += 1
                    continue
                else:
                    # Recurse into unmatched while loop bodies to convert inner loops.
                    # Add this scope's assignments to all_defs so inner loops can find accumulators.
                    old_defs = self.all_defs.copy()
                    for s in stmt.body:
                        if isinstance(s, ast.Assign):
                            for t in s.targets:
                                if isinstance(t, ast.Name):
                                    self.all_defs[t.id] = s
                    stmt.body = self._process_body(stmt.body)
                    self.all_defs = old_defs

            if isinstance(stmt, ast.If):
                stmt.body = self._process_body(stmt.body)
                if stmt.orelse:
                    stmt.orelse = self._process_body(stmt.orelse)

            new_node = self.visit(stmt)
            new_body.append(new_node)
            i += 1
        return new_body

    def _match_complex_fold(self, body, while_idx):
        node = body[while_idx]
        w_body = node.body
        if len(w_body) < 2:
            return None

        last_stmt = w_body[-1]
        inc_result = self._get_inc_var(last_stmt)
        if not inc_result:
            return None
        inc_var, stride = inc_result

        # Extract loop limit
        limit_node = self._extract_limit(node.test, inc_var, stride)
        if not limit_node:
            return None

        # Check for item access at start
        first_stmt = w_body[0]
        has_item_access = False
        item_var = None
        collection_node = None
        if (isinstance(first_stmt, ast.Assign) and len(first_stmt.targets) == 1
                and isinstance(first_stmt.targets[0], ast.Name)
                and isinstance(first_stmt.value, ast.Subscript)
                and isinstance(first_stmt.value.slice, ast.Name)
                and first_stmt.value.slice.id == inc_var):
            has_item_access = True
            item_var = first_stmt.targets[0].id
            collection_node = first_stmt.value.value
            body_middle = w_body[1:-1]
        else:
            body_middle = w_body[:-1]

        # Identify accumulator variables
        assigned_in_loop = self._get_assigned_vars(body_middle)
        # Build local defs: assignments in the current body before this while loop
        local_defs = {}
        for j in range(while_idx):
            s = body[j]
            if isinstance(s, ast.Assign):
                for t in s.targets:
                    if isinstance(t, ast.Name):
                        local_defs[t.id] = s
        # Exclude the increment var and item var from accumulators
        exclude = {inc_var}
        if item_var:
            exclude.add(item_var)
        acc_vars = []
        for var in sorted(list(assigned_in_loop)):
            if var not in exclude and (var in local_defs or var in self.all_defs):
                acc_vars.append(var)

        if not acc_vars:
            return None

        # Check for break/continue and break-simulation (assigning to inc_var in body_middle)
        class LoopAnalyzer(ast.NodeVisitor):
            def __init__(self, target):
                self.target = target
                self.found_idx = False
                self.found_break = False
                self.found_idx_store = False
            def visit_Name(self, n):
                if n.id == self.target and isinstance(n.ctx, ast.Load):
                    self.found_idx = True
                if n.id == self.target and isinstance(n.ctx, ast.Store):
                    self.found_idx_store = True
            def visit_Break(self, n):
                self.found_break = True
            def visit_Continue(self, n):
                self.found_break = True

        la = LoopAnalyzer(inc_var)
        for s in w_body:
            la.visit(s)
        if la.found_break:
            return None
        # Check for inc_var being assigned in body_middle (break simulation like x5 = x1)
        for s in body_middle:
            for n in ast.walk(s):
                if isinstance(n, ast.Name) and n.id == inc_var and isinstance(n.ctx, ast.Store):
                    return None
        idx_used = la.found_idx

        # Construct helper function
        lineno = getattr(node, 'lineno', self.helper_count)
        helper_name = f"_{self.task_id}_fold_{lineno}_{self.helper_count}"
        self.helper_count += 1

        helper_body = []
        if len(acc_vars) > 1:
            helper_body.append(ast.Assign(
                targets=[ast.Tuple(elts=[ast.Name(id=v, ctx=ast.Store()) for v in acc_vars], ctx=ast.Store())],
                value=ast.Name(id='_state', ctx=ast.Load())
            ))
            state_arg_name = '_state'
        else:
            state_arg_name = acc_vars[0]

        if has_item_access:
            if idx_used:
                helper_body.append(ast.Assign(
                    targets=[ast.Name(id=item_var, ctx=ast.Store())],
                    value=ast.Subscript(
                        value=copy.deepcopy(collection_node),
                        slice=ast.Name(id=inc_var, ctx=ast.Load()),
                        ctx=ast.Load()
                    )
                ))
                fold_item_arg = inc_var
            else:
                fold_item_arg = item_var
        else:
            fold_item_arg = inc_var

        helper_body.extend(copy.deepcopy(body_middle))

        if len(acc_vars) > 1:
            ret_val = ast.Tuple(elts=[ast.Name(id=v, ctx=ast.Load()) for v in acc_vars], ctx=ast.Load())
        else:
            ret_val = ast.Name(id=acc_vars[0], ctx=ast.Load())
        helper_body.append(ast.Return(value=ret_val))

        helper_def = ast.FunctionDef(
            name=helper_name,
            args=ast.arguments(
                posonlyargs=[], args=[ast.arg(arg=state_arg_name), ast.arg(arg=fold_item_arg)],
                kwonlyargs=[], kw_defaults=[], defaults=[]
            ),
            body=helper_body, decorator_list=[]
        )
        ast.fix_missing_locations(helper_def)

        # Build collection for fold
        if has_item_access:
            final_col = copy.deepcopy(collection_node)
            if idx_used:
                size_call = ast.Call(func=ast.Name(id='size', ctx=ast.Load()), args=[final_col], keywords=[])
                if stride == 1:
                    final_col = ast.Call(
                        func=ast.Name(id='range', ctx=ast.Load()),
                        args=[size_call], keywords=[]
                    )
                else:
                    range_args = [ast.Constant(value=0), size_call, ast.Constant(value=stride)]
                    final_col = ast.Call(
                        func=ast.Name(id='range', ctx=ast.Load()),
                        args=range_args, keywords=[]
                    )
            if not (isinstance(final_col, ast.Call) and isinstance(final_col.func, ast.Name)
                    and final_col.func.id in ('totuple', 'range')):
                final_col = ast.Call(func=ast.Name(id='totuple', ctx=ast.Load()), args=[final_col], keywords=[])
        else:
            if stride == 1:
                final_col = ast.Call(func=ast.Name(id='range', ctx=ast.Load()),
                                    args=[copy.deepcopy(limit_node)], keywords=[])
            elif stride == -1:
                # Reverse: range(start, stop, -1) — start from init, stop at limit
                idx_init_info = self._find_init_in_body(body, while_idx, inc_var)
                start_node = ast.Constant(value=idx_init_info[1]) if idx_init_info and idx_init_info[1] is not None else ast.Name(id=inc_var, ctx=ast.Load())
                final_col = ast.Call(func=ast.Name(id='range', ctx=ast.Load()),
                                    args=[start_node, copy.deepcopy(limit_node), ast.Constant(value=-1)],
                                    keywords=[])
            else:
                final_col = ast.Call(func=ast.Name(id='range', ctx=ast.Load()),
                                    args=[ast.Constant(value=0), copy.deepcopy(limit_node), ast.Constant(value=stride)],
                                    keywords=[])

        initial_state = (ast.Tuple(elts=[ast.Name(id=v, ctx=ast.Load()) for v in acc_vars], ctx=ast.Load())
                         if len(acc_vars) > 1 else ast.Name(id=acc_vars[0], ctx=ast.Load()))
        fold_target = (ast.Tuple(elts=[ast.Name(id=v, ctx=ast.Store()) for v in acc_vars], ctx=ast.Store())
                       if len(acc_vars) > 1 else ast.Name(id=acc_vars[0], ctx=ast.Store()))

        fold_call = ast.Assign(
            targets=[fold_target],
            value=ast.Call(
                func=ast.Name(id='fold', ctx=ast.Load()),
                args=[final_col, initial_state, ast.Name(id=helper_name, ctx=ast.Load())],
                keywords=[]
            )
        )
        ast.fix_missing_locations(fold_call)

        # Determine which init statements to consume (by node identity)
        consumed_nodes = set()
        idx_init_info = self._find_init_in_body(body, while_idx, inc_var)
        if idx_init_info and idx_init_info[1] is not None:
            used_after = False
            for j in range(while_idx + 1, len(body)):
                for n in ast.walk(body[j]):
                    if isinstance(n, ast.Name) and n.id == inc_var and isinstance(n.ctx, (ast.Load, ast.Store)):
                        used_after = True
                        break
                if used_after:
                    break
            if not used_after:
                consumed_nodes.add(id(body[idx_init_info[0]]))

        return [fold_call], helper_def, consumed_nodes

    def _get_inc_var(self, stmt):
        """Detect increment/decrement patterns. Returns (var_name, stride) or None.

        Recognized patterns:
          increment(v)        -> (v, 1)
          v + N               -> (v, N)
          add(v, N)           -> (v, N)
          decrement(v)        -> (v, -1)
          v - N               -> (v, -N)
          subtract(v, N)      -> (v, -N)
        """
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            return None
        t, v = stmt.targets[0].id, stmt.value
        # increment(v)
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == 'increment' and len(v.args) == 1
                and isinstance(v.args[0], ast.Name) and v.args[0].id == t):
            return (t, 1)
        # decrement(v)
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == 'decrement' and len(v.args) == 1
                and isinstance(v.args[0], ast.Name) and v.args[0].id == t):
            return (t, -1)
        # add(v, N)
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == 'add' and len(v.args) == 2
                and isinstance(v.args[0], ast.Name) and v.args[0].id == t
                and isinstance(v.args[1], ast.Constant) and isinstance(v.args[1].value, int)):
            return (t, v.args[1].value)
        # subtract(v, N)
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == 'subtract' and len(v.args) == 2
                and isinstance(v.args[0], ast.Name) and v.args[0].id == t
                and isinstance(v.args[1], ast.Constant) and isinstance(v.args[1].value, int)):
            return (t, -v.args[1].value)
        # v + N
        if (isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add)
                and isinstance(v.left, ast.Name) and v.left.id == t
                and isinstance(v.right, ast.Constant) and isinstance(v.right.value, int)):
            return (t, v.right.value)
        # v - N
        if (isinstance(v, ast.BinOp) and isinstance(v.op, ast.Sub)
                and isinstance(v.left, ast.Name) and v.left.id == t
                and isinstance(v.right, ast.Constant) and isinstance(v.right.value, int)):
            return (t, -v.right.value)
        return None

    def _get_assigned_vars(self, nodes):
        result = set()
        for n in nodes:
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        result.add(t.id)
                    elif isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                        result.add(t.value.id)
            elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                if isinstance(n.value.func, ast.Attribute) and isinstance(n.value.func.value, ast.Name):
                    if n.value.func.attr in ('append', 'extend', 'add', 'update'):
                        result.add(n.value.func.value.id)
            elif hasattr(n, 'body'):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                result.update(self._get_assigned_vars(n.body))
                if hasattr(n, 'orelse') and n.orelse:
                    result.update(self._get_assigned_vars(n.orelse))
        return result

    def _is_call(self, node, func_name, nargs=None):
        """Check if node is a call to func_name with optional arg count."""
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == func_name
                and (nargs is None or len(node.args) == nargs))

    def _is_name(self, node, name):
        return isinstance(node, ast.Name) and node.id == name

    def _extract_limit(self, test, inc_var, stride):
        """Extract loop limit from various condition patterns. Returns limit AST node or None."""
        # Standard Python comparisons
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            op = test.ops[0]
            # Forward: i < limit
            if (isinstance(op, ast.Lt) and self._is_name(test.left, inc_var) and stride > 0):
                return test.comparators[0]
            # Forward: i != limit (with positive stride, equivalent to i < limit)
            if (isinstance(op, ast.NotEq) and self._is_name(test.left, inc_var) and stride > 0):
                return test.comparators[0]
            # Reverse: i >= limit or i > limit
            if (isinstance(op, (ast.GtE, ast.Gt)) and self._is_name(test.left, inc_var) and stride < 0):
                return test.comparators[0]
            # Reverse: limit <= i or limit < i
            if (isinstance(op, (ast.LtE, ast.Lt)) and self._is_name(test.comparators[0], inc_var) and stride < 0):
                return test.left

        # greater(limit, i) → i < limit (forward)
        if self._is_call(test, 'greater', 2) and self._is_name(test.args[1], inc_var) and stride > 0:
            return test.args[0]

        # greater(i, limit) → i > limit (reverse)
        if self._is_call(test, 'greater', 2) and self._is_name(test.args[0], inc_var) and stride < 0:
            return test.args[1]

        # not greater(i, limit) → i <= limit (forward)
        if (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                and self._is_call(test.operand, 'greater', 2)):
            inner = test.operand
            if self._is_name(inner.args[0], inc_var) and stride > 0:
                # i <= limit → range(start, limit + 1)
                return ast.BinOp(left=inner.args[1], op=ast.Add(), right=ast.Constant(value=1))
            if self._is_name(inner.args[1], inc_var) and stride < 0:
                # not greater(limit, i) → i >= limit (reverse)
                return inner.args[0]

        # flip(greater(i, limit)) → i <= limit
        if (self._is_call(test, 'flip', 1) and self._is_call(test.args[0], 'greater', 2)):
            inner = test.args[0]
            if self._is_name(inner.args[0], inc_var) and stride > 0:
                return ast.BinOp(left=inner.args[1], op=ast.Add(), right=ast.Constant(value=1))

        # both(flip(greater(ZERO, i)), greater(limit, i)) → 0 <= i < limit
        if self._is_call(test, 'both', 2):
            # Try both argument orders
            for a, b in [(test.args[0], test.args[1]), (test.args[1], test.args[0])]:
                limit = self._extract_limit(b, inc_var, stride)
                if limit is not None:
                    # Check that the other arg is a lower bound on i (just accept it)
                    return limit

        # positive(i) → i > 0 (for decrement loops)
        if self._is_call(test, 'positive', 1) and self._is_name(test.args[0], inc_var) and stride < 0:
            return ast.Constant(value=0)

        return None

    def _find_init_in_body(self, body, while_idx, var_name):
        for j in range(while_idx - 1, -1, -1):
            s = body[j]
            if (isinstance(s, ast.Assign) and len(s.targets) == 1
                    and isinstance(s.targets[0], ast.Name) and s.targets[0].id == var_name):
                v = s.value
                if isinstance(v, ast.Constant) and isinstance(v.value, int):
                    return (j, v.value)
                if isinstance(v, ast.Name):
                    if v.id == 'ZERO':
                        return (j, 0)
                    if v.id == 'ONE':
                        return (j, 1)
                return (j, None)
        return None
