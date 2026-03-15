import ast
import copy


class TupleUnpackTransformer(ast.NodeTransformer):
    """Replace sequential indexing (x = arr[0]; y = arr[1]; ...) with tuple unpacking.

    Uses a window-based approach to find indexed accesses on the same array,
    even if they aren't strictly consecutive.
    """
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
            res = self._match_unpack_window(body, i)
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

    def _match_unpack_window(self, body, start):
        s0 = body[start]
        if not (isinstance(s0, ast.Assign) and len(s0.targets) == 1
                and isinstance(s0.value, ast.Subscript)
                and isinstance(s0.value.slice, ast.Constant)
                and isinstance(s0.value.slice.value, int)):
            return None

        arr_str = ast.unparse(s0.value.value)
        arr_node = s0.value.value
        found = {s0.value.slice.value: (s0.targets[0], start)}

        for j in range(start + 1, min(start + self.window, len(body))):
            s = body[j]
            if (isinstance(s, ast.Assign) and len(s.targets) == 1
                    and isinstance(s.value, ast.Subscript)
                    and isinstance(s.value.slice, ast.Constant)
                    and isinstance(s.value.slice.value, int)
                    and ast.unparse(s.value.value) == arr_str):
                idx = s.value.slice.value
                if idx not in found:
                    found[idx] = (s.targets[0], j)

        if len(found) < 2:
            return None

        sorted_indices = sorted(found.keys())
        if sorted_indices != list(range(sorted_indices[0], sorted_indices[0] + len(found))):
            return None

        elts = []
        for _ in range(sorted_indices[0]):
            elts.append(ast.Name(id='_', ctx=ast.Store()))
        for idx in sorted_indices:
            elts.append(copy.deepcopy(found[idx][0]))

        val = ast.Subscript(
            value=copy.deepcopy(arr_node),
            slice=ast.Slice(upper=ast.Constant(value=sorted_indices[-1] + 1)),
            ctx=ast.Load()
        )
        new_assign = ast.Assign(
            targets=[ast.Tuple(elts=elts, ctx=ast.Store())],
            value=val
        )

        indices_to_remove = {found[idx][1] for idx in found}
        indices_to_remove.discard(start)
        return new_assign, indices_to_remove
