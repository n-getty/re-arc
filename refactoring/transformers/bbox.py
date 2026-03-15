import ast
import copy


class BboxTransformer(ast.NodeTransformer):
    """Replace uppermost/lowermost/leftmost/rightmost calls on the same object with bbox().

    Uses a window-based approach to find all 4 calls within a configurable window,
    even if they aren't consecutive.
    """
    BBOX_FUNCS = {'uppermost', 'lowermost', 'leftmost', 'rightmost'}

    def __init__(self, task_id=None, window=10):
        super().__init__()
        self.changes = 0
        self.window = window

    def visit_FunctionDef(self, node):
        node.body = self._process_body(node.body)
        return self.generic_visit(node)

    def _process_body(self, body):
        i = 0
        while i < len(body):
            res = self._match_bbox_window(body, i)
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

    def _match_bbox_window(self, body, start):
        found = {}  # func_name -> (target_name, index)
        obj_name = None
        for j in range(start, min(start + self.window, len(body))):
            s = body[j]
            if (isinstance(s, ast.Assign) and len(s.targets) == 1
                    and isinstance(s.targets[0], ast.Name)
                    and isinstance(s.value, ast.Call)
                    and isinstance(s.value.func, ast.Name)
                    and s.value.func.id in self.BBOX_FUNCS
                    and s.value.args):
                curr_obj = ast.unparse(s.value.args[0])
                if obj_name is None:
                    obj_name = curr_obj
                elif curr_obj != obj_name:
                    continue
                if s.value.func.id not in found:
                    found[s.value.func.id] = (s.targets[0].id, j)

        if set(found.keys()) == self.BBOX_FUNCS:
            targets = [
                ast.Name(id=found[f][0], ctx=ast.Store())
                for f in ['uppermost', 'lowermost', 'leftmost', 'rightmost']
            ]
            indices = {found[f][1] for f in found}
            indices.discard(start)
            new_assign = ast.Assign(
                targets=[ast.Tuple(elts=targets, ctx=ast.Store())],
                value=ast.Call(
                    func=ast.Name(id='bbox', ctx=ast.Load()),
                    args=[ast.Name(id=obj_name, ctx=ast.Load())],
                    keywords=[]
                )
            )
            return new_assign, indices
        return None
