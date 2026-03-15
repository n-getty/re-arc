import ast
import copy


class ForFoldTransformer(ast.NodeTransformer):
    """Replace for-loops with accumulator variables with fold() + nested helper.

    Rejects loops with break/continue.
    """
    def __init__(self, task_id):
        super().__init__()
        self.task_id = task_id
        self.changes = 0
        self.helper_count = 0
        self.all_defs = {}

    def visit_FunctionDef(self, node):
        if not node.name.startswith('verify_'):
            return node
        self.all_defs = {}
        for n in ast.walk(node):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        if t.id not in self.all_defs:
                            self.all_defs[t.id] = n

        node.body = self._process_body(node.body)
        return node

    def _process_body(self, body):
        new_body = []
        i = 0
        while i < len(body):
            stmt = body[i]
            if isinstance(stmt, ast.For) and isinstance(stmt.target, ast.Name):
                res = self._match_for_fold(stmt, body, i)
                if res:
                    fold_stmt, helper_def = res
                    helper_def.body = self._process_body(helper_def.body)
                    new_body.append(helper_def)
                    new_body.append(fold_stmt)
                    self.changes += 1
                    i += 1
                    continue

            if hasattr(stmt, 'body'):
                if isinstance(stmt, ast.If):
                    stmt.body = self._process_body(stmt.body)
                    if stmt.orelse:
                        stmt.orelse = self._process_body(stmt.orelse)
                else:
                    stmt.body = self._process_body(stmt.body)
            new_body.append(stmt)
            i += 1
        return new_body

    def _match_for_fold(self, node, parent_body, for_idx):
        item_var = node.target.id
        collection_node = node.iter

        assigned_in_loop = self._get_assigned_vars(node.body)
        acc_vars = []
        for var in sorted(list(assigned_in_loop)):
            if var in self.all_defs:
                def_node = self.all_defs[var]
                if hasattr(def_node, 'lineno') and hasattr(node, 'lineno'):
                    if def_node.lineno < node.lineno:
                        acc_vars.append(var)

        if not acc_vars:
            return None

        class LoopAnalyzer(ast.NodeVisitor):
            def __init__(self):
                self.found_break = False
            def visit_Break(self, n):
                self.found_break = True
            def visit_Continue(self, n):
                self.found_break = True

        la = LoopAnalyzer()
        for s in node.body:
            la.visit(s)
        if la.found_break:
            return None

        lineno = getattr(node, 'lineno', self.helper_count)
        helper_name = f"_{self.task_id}_for_fold_{lineno}_{self.helper_count}"
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

        helper_body.extend(copy.deepcopy(node.body))

        if len(acc_vars) > 1:
            ret_val = ast.Tuple(elts=[ast.Name(id=v, ctx=ast.Load()) for v in acc_vars], ctx=ast.Load())
        else:
            ret_val = ast.Name(id=acc_vars[0], ctx=ast.Load())
        helper_body.append(ast.Return(value=ret_val))

        helper_def = ast.FunctionDef(
            name=helper_name,
            args=ast.arguments(
                posonlyargs=[], args=[ast.arg(arg=state_arg_name), ast.arg(arg=item_var)],
                kwonlyargs=[], kw_defaults=[], defaults=[]
            ),
            body=helper_body, decorator_list=[]
        )
        ast.fix_missing_locations(helper_def)

        final_col = copy.deepcopy(collection_node)
        if not (isinstance(final_col, ast.Call) and isinstance(final_col.func, ast.Name)
                and final_col.func.id in ('totuple', 'range')):
            final_col = ast.Call(func=ast.Name(id='totuple', ctx=ast.Load()), args=[final_col], keywords=[])

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
        return fold_call, helper_def

    def _get_assigned_vars(self, nodes):
        result = set()
        for n in nodes:
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        result.add(t.id)
            elif hasattr(n, 'body'):
                result.update(self._get_assigned_vars(n.body))
                if hasattr(n, 'orelse'):
                    result.update(self._get_assigned_vars(n.orelse))
        return result
