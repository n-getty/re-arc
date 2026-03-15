import ast
import copy


class WhileFoldTransformer(ast.NodeTransformer):
    """Replace simple while-loop + increment patterns with fold().

    Targets: while i < len(X): acc = f(acc, X[i]); i = increment(i)
    """
    def __init__(self, task_id=None):
        super().__init__()
        self.changes = 0

    def visit_FunctionDef(self, node):
        node.body = self._process_body(node.body)
        self.generic_visit(node)
        return node

    def _process_body(self, body):
        new_body = []
        i = 0
        skip_indices = set()
        while i < len(body):
            if i in skip_indices:
                i += 1
                continue

            stmt = body[i]
            if isinstance(stmt, ast.While):
                res = self._match_fold_at_while(body, i)
                if res:
                    fold_assign, consumed_indices = res
                    actual_new_body = [s for idx, s in enumerate(new_body) if idx not in consumed_indices]
                    new_body = actual_new_body
                    new_body.append(fold_assign)
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

    def _match_fold_at_while(self, full_body, while_idx):
        while_node = full_body[while_idx]
        w_body = while_node.body
        if len(w_body) < 2:
            return None
        last_stmt = w_body[-1]
        inc_var = self._get_increment_var(last_stmt)
        if not inc_var:
            return None
        fold_info = self._parse_fold_body(w_body, inc_var)
        if not fold_info:
            return None
        acc_var, func_node, collection_node = fold_info

        idx_init = self._find_init(full_body, while_idx, inc_var)
        if not idx_init:
            return None
        idx_init_idx, idx_start_val = idx_init

        acc_init = self._find_init(full_body, while_idx, acc_var)
        acc_init_idx, init_value = acc_init if acc_init else (None, ast.Name(id=acc_var, ctx=ast.Load()))

        final_col = copy.deepcopy(collection_node)
        if not (isinstance(final_col, ast.Call) and isinstance(final_col.func, ast.Name)
                and final_col.func.id in ('totuple', 'range')):
            final_col = ast.Call(func=ast.Name(id='totuple', ctx=ast.Load()), args=[final_col], keywords=[])
        if idx_start_val == 1:
            final_col = ast.Subscript(value=final_col, slice=ast.Slice(lower=ast.Constant(value=1)), ctx=ast.Load())

        fold_call = ast.Assign(
            targets=[ast.Name(id=acc_var, ctx=ast.Store())],
            value=ast.Call(
                func=ast.Name(id='fold', ctx=ast.Load()),
                args=[final_col, copy.deepcopy(init_value), func_node],
                keywords=[]
            )
        )
        ast.fix_missing_locations(fold_call)
        consumed = {idx_init_idx}
        if acc_init_idx is not None:
            consumed.add(acc_init_idx)
        return fold_call, consumed

    def _get_increment_var(self, stmt):
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

    def _find_init(self, body, while_idx, var_name):
        for j in range(while_idx - 1, max(-1, while_idx - 6), -1):
            s = body[j]
            if (isinstance(s, ast.Assign) and len(s.targets) == 1
                    and isinstance(s.targets[0], ast.Name) and s.targets[0].id == var_name):
                v = s.value
                val = None
                if isinstance(v, ast.Constant):
                    val = v.value
                elif isinstance(v, ast.Name):
                    if v.id == 'ZERO':
                        val = 0
                    elif v.id == 'ONE':
                        val = 1
                return (j, val) if val is not None else (j, v)
        return None

    def _parse_fold_body(self, w_body, idx_var):
        if len(w_body) == 2:
            s = w_body[0]
            if (isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)):
                acc, v = s.targets[0].id, s.value
                if (isinstance(v, ast.Call) and len(v.args) == 2
                        and isinstance(v.args[0], ast.Name) and v.args[0].id == acc):
                    if (isinstance(v.args[1], ast.Subscript)
                            and isinstance(v.args[1].slice, ast.Name) and v.args[1].slice.id == idx_var):
                        return (acc, v.func, v.args[1].value)
        elif len(w_body) == 3:
            s1, s2 = w_body[0], w_body[1]
            if (isinstance(s1, ast.Assign) and isinstance(s1.value, ast.Subscript)
                    and isinstance(s1.value.slice, ast.Name) and s1.value.slice.id == idx_var):
                item = s1.targets[0].id
                if (isinstance(s2, ast.Assign) and isinstance(s2.value, ast.Call) and len(s2.value.args) == 2):
                    acc = s2.targets[0].id
                    if (isinstance(s2.value.args[0], ast.Name) and s2.value.args[0].id == acc
                            and isinstance(s2.value.args[1], ast.Name) and s2.value.args[1].id == item):
                        return (acc, s2.value.func, s1.value.value)
        return None
