import ast
import copy


class IterationTransformer(ast.NodeTransformer):
    """Replace for/while loops with early break patterns with any/all/find_first.

    Detects patterns like:
      found = False; for x in items: if cond: found = True; break
    And converts to:
      found = any_match(items, lambda x: cond)
    """
    def __init__(self, task_id):
        super().__init__()
        self.task_id = task_id
        self.changes = 0

    def visit_FunctionDef(self, node):
        node.body = self._process_body(node.body)
        return self.generic_visit(node)

    def _process_body(self, body):
        i = 0
        while i < len(body):
            res = self._match_iteration_pattern(body, i)
            if res:
                new_stmt, consumed_indices = res
                body = [
                    new_stmt if idx == i else s
                    for idx, s in enumerate(body)
                    if idx not in consumed_indices
                ]
                self.changes += 1
                continue

            stmt = body[i]
            if hasattr(stmt, 'body'):
                if isinstance(stmt, ast.If):
                    stmt.body = self._process_body(stmt.body)
                    if stmt.orelse:
                        stmt.orelse = self._process_body(stmt.orelse)
                else:
                    stmt.body = self._process_body(stmt.body)
            i += 1
        return body

    def _match_iteration_pattern(self, body, idx):
        node = body[idx]
        if not isinstance(node, (ast.For, ast.While)):
            return None

        # Find break inside an if
        break_if = None
        for s in node.body:
            if isinstance(s, ast.If) and len(s.body) == 2:
                if isinstance(s.body[0], ast.Assign) and isinstance(s.body[1], ast.Break):
                    break_if = s
                    break
        if not break_if:
            return None

        target_var = (break_if.body[0].targets[0].id
                      if isinstance(break_if.body[0].targets[0], ast.Name) else None)
        target_val = break_if.body[0].value
        if not target_var:
            return None

        # Find initializer
        init_idx = -1
        for j in range(idx - 1, -1, -1):
            if (isinstance(body[j], ast.Assign) and len(body[j].targets) == 1
                    and isinstance(body[j].targets[0], ast.Name)
                    and body[j].targets[0].id == target_var):
                init_idx = j
                break
        if init_idx == -1:
            return None
        init_val = body[init_idx].value

        # Determine pattern type
        is_any, is_all, is_find = False, False, False
        init_is_f = ((isinstance(init_val, ast.Constant) and init_val.value is False)
                     or (isinstance(init_val, ast.Name) and init_val.id == 'F'))
        init_is_t = ((isinstance(init_val, ast.Constant) and init_val.value is True)
                     or (isinstance(init_val, ast.Name) and init_val.id == 'T'))
        target_is_t = ((isinstance(target_val, ast.Constant) and target_val.value is True)
                       or (isinstance(target_val, ast.Name) and target_val.id == 'T'))
        target_is_f = ((isinstance(target_val, ast.Constant) and target_val.value is False)
                       or (isinstance(target_val, ast.Name) and target_val.id == 'F'))

        if init_is_f and target_is_t:
            is_any = True
        elif init_is_t and target_is_f:
            is_all = True
        elif isinstance(target_val, ast.Name):
            is_find = True
        if not (is_any or is_all or is_find):
            return None

        # Container & item ID
        if isinstance(node, ast.For):
            container = copy.deepcopy(node.iter)
            item_id = node.target.id
            k_init_idx = -1
        else:
            last_stmt = node.body[-1]
            if not (isinstance(last_stmt, ast.Assign) and len(last_stmt.targets) == 1
                    and isinstance(last_stmt.targets[0], ast.Name)):
                return None
            inc_var = last_stmt.targets[0].id
            if not (isinstance(node.test, ast.Compare) and len(node.test.ops) == 1):
                return None
            if not (isinstance(node.test.left, ast.Name) and node.test.left.id == inc_var):
                return None
            op = node.test.ops[0]
            limit_node = node.test.comparators[0]
            start_val, k_init_idx = None, -1
            for j in range(idx - 1, -1, -1):
                if j == init_idx:
                    continue
                if (isinstance(body[j], ast.Assign) and len(body[j].targets) == 1
                        and isinstance(body[j].targets[0], ast.Name)
                        and body[j].targets[0].id == inc_var):
                    k_init_idx = j
                    start_val = body[j].value
                    break
            if start_val is None:
                return None
            limit = copy.deepcopy(limit_node) if isinstance(op, ast.Lt) else (
                ast.BinOp(left=copy.deepcopy(limit_node), op=ast.Add(), right=ast.Constant(value=1))
                if isinstance(op, ast.LtE) else None
            )
            if limit is None:
                return None
            container = ast.Call(
                func=ast.Name(id='range', ctx=ast.Load()),
                args=([limit] if isinstance(start_val, ast.Constant) and start_val.value == 0
                      else [copy.deepcopy(start_val), limit]),
                keywords=[]
            )
            item_id = inc_var

        if is_find and target_val.id != item_id:
            return None

        # Inline local definitions into the condition
        local_defs = {}
        for s in node.body:
            if s == break_if:
                break
            if (isinstance(s, ast.Assign) and len(s.targets) == 1
                    and isinstance(s.targets[0], ast.Name)):
                local_defs[s.targets[0].id] = s

        def inline_node(n, defs):
            if isinstance(n, ast.Name) and n.id in defs:
                return inline_node(copy.deepcopy(defs[n.id].value), defs)
            for field, value in ast.iter_fields(n):
                if isinstance(value, list):
                    for i, item in enumerate(value):
                        if isinstance(item, ast.AST):
                            value[i] = inline_node(item, defs)
                elif isinstance(value, ast.AST):
                    setattr(n, field, inline_node(value, defs))
            return n

        cond = inline_node(copy.deepcopy(break_if.test), local_defs)

        # Construct replacement
        lambda_args = ast.arguments(
            posonlyargs=[], args=[ast.arg(arg=item_id)],
            kwonlyargs=[], kw_defaults=[], defaults=[]
        )
        if is_all:
            lambda_cond = ast.UnaryOp(op=ast.Not(), operand=cond)
            func = 'all_match'
            args = [container, ast.Lambda(args=lambda_args, body=lambda_cond)]
        elif is_any:
            func = 'any_match'
            args = [container, ast.Lambda(args=lambda_args, body=cond)]
        else:
            func = 'find_first'
            args = [container, ast.Lambda(args=lambda_args, body=cond), copy.deepcopy(init_val)]

        new_assign = ast.Assign(
            targets=[ast.Name(id=target_var, ctx=ast.Store())],
            value=ast.Call(func=ast.Name(id=func, ctx=ast.Load()), args=args, keywords=[])
        )
        consumed = {init_idx}
        if k_init_idx != -1:
            consumed.add(k_init_idx)
        return new_assign, consumed
