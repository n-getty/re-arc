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
                    fold_stmts, helper_def, consumed_indices = res
                    helper_def.body = self._process_body(helper_def.body)
                    actual_new_body = [s for idx, s in enumerate(new_body) if idx not in consumed_indices]
                    new_body = actual_new_body
                    new_body.append(helper_def)
                    new_body.extend(fold_stmts)
                    self.changes += 1
                    i += 1
                    continue

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
        inc_var = self._get_inc_var(last_stmt)
        if not inc_var:
            return None

        # Extract loop limit
        limit_node = None
        if (isinstance(node.test, ast.Compare) and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.Lt)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == inc_var):
            limit_node = node.test.comparators[0]
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
        acc_vars = []
        for var in sorted(list(assigned_in_loop)):
            if var in self.all_defs:
                def_node = self.all_defs[var]
                if hasattr(def_node, 'lineno') and hasattr(node, 'lineno'):
                    if def_node.lineno < node.lineno:
                        acc_vars.append(var)

        if not acc_vars:
            return None

        # Check for break/continue
        class LoopAnalyzer(ast.NodeVisitor):
            def __init__(self, target):
                self.target = target
                self.found_idx = False
                self.found_break = False
            def visit_Name(self, n):
                if n.id == self.target and isinstance(n.ctx, ast.Load):
                    self.found_idx = True
            def visit_Break(self, n):
                self.found_break = True
            def visit_Continue(self, n):
                self.found_break = True

        la = LoopAnalyzer(inc_var)
        for s in w_body:
            la.visit(s)
        if la.found_break:
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
                final_col = ast.Call(
                    func=ast.Name(id='range', ctx=ast.Load()),
                    args=[ast.Call(func=ast.Name(id='size', ctx=ast.Load()), args=[final_col], keywords=[])],
                    keywords=[]
                )
            if not (isinstance(final_col, ast.Call) and isinstance(final_col.func, ast.Name)
                    and final_col.func.id in ('totuple', 'range')):
                final_col = ast.Call(func=ast.Name(id='totuple', ctx=ast.Load()), args=[final_col], keywords=[])
        else:
            final_col = ast.Call(func=ast.Name(id='range', ctx=ast.Load()), args=[copy.deepcopy(limit_node)], keywords=[])

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

        # Determine which init statements to consume
        consumed = set()
        idx_init_info = self._find_init_in_body(body, while_idx, inc_var)
        if idx_init_info and idx_init_info[1] == 0:
            used_after = False
            for j in range(while_idx + 1, len(body)):
                for n in ast.walk(body[j]):
                    if isinstance(n, ast.Name) and n.id == inc_var and isinstance(n.ctx, (ast.Load, ast.Store)):
                        used_after = True
                        break
                if used_after:
                    break
            if not used_after:
                consumed.add(idx_init_info[0])

        return [fold_call], helper_def, consumed

    def _get_inc_var(self, stmt):
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            return None
        t, v = stmt.targets[0].id, stmt.value
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == 'increment' and len(v.args) == 1 and v.args[0].id == t):
            return t
        if (isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add)
                and isinstance(v.left, ast.Name) and v.left.id == t
                and isinstance(v.right, ast.Constant) and v.right.value == 1):
            return t
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

    def _find_init_in_body(self, body, while_idx, var_name):
        for j in range(while_idx - 1, -1, -1):
            s = body[j]
            if (isinstance(s, ast.Assign) and len(s.targets) == 1
                    and isinstance(s.targets[0], ast.Name) and s.targets[0].id == var_name):
                v = s.value
                val = 0 if ((isinstance(v, ast.Name) and v.id == 'ZERO') or
                            (isinstance(v, ast.Constant) and v.value == 0)) else None
                return (j, val)
        return None
