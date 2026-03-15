import ast
import copy
import re


class BFSTransformer(ast.NodeTransformer):
    """Replace BFS/flood-fill patterns in verify_* functions with objects() + colorfilter().

    Detects loop+queue patterns with grid connectivity checks and replaces with DSL calls.
    """
    def __init__(self, task_id):
        super().__init__()
        self.task_id = task_id
        self.changes = 0

    def visit_FunctionDef(self, node):
        old_defs = {}
        for s in node.body:
            if isinstance(s, ast.Assign):
                for t in s.targets:
                    if isinstance(t, ast.Name):
                        old_defs[t.id] = s
        node.body = self._process_body(node.body)
        return self.generic_visit(node)

    def _process_body(self, body):
        i = 0
        while i < len(body):
            res = self._match_bfs_pattern(body, i)
            if res:
                new_body_stmts, consumed_indices = res
                actual_new_body = []
                for idx, s in enumerate(body):
                    if idx == i:
                        actual_new_body.extend(new_body_stmts)
                    elif idx not in consumed_indices:
                        actual_new_body.append(s)
                body = actual_new_body
                self.changes += 1
                continue
            i += 1
        return body

    def _match_bfs_pattern(self, body, idx):
        node = body[idx]
        if not isinstance(node, (ast.While, ast.For)):
            return None
        try:
            src = ast.unparse(node)
        except Exception:
            return None

        if not ('while' in src and 'append' in src and ('(-1, 0)' in src or '(-1,0)' in src) and 'I[' in src):
            return None

        # Detect color
        color_val = None
        m = re.search(r"I\[[^\]]+\]\[[^\]]+\]\s*==\s*([A-Z_0-9]+)", src)
        if m:
            color_val = m.group(1)
        else:
            m = re.search(r"I\[[^\]]+\]\[[^\]]+\]\s*!=\s*(ZERO|0)", src)
            if m:
                color_val = "None"

        if not color_val:
            return None

        # Detect diagonal connectivity
        diagonal = "T" if any(x in src for x in ["(-1, -1)", "(1, 1)", "UP_RIGHT", "(-1,-1)", "(1,1)"]) else "F"

        # Find result variable
        m_ret = re.search(r"(\w+)\.append", src)
        if not m_ret:
            m_ret = re.search(r"(\w+)\s*=\s*\1\s*\|\s*\w+", src)
        ret_var = m_ret.group(1) if m_ret else "x3"

        if color_val != "None":
            new_src = f"{ret_var} = tuple((frozenset((c for v, c in obj)) for obj in colorfilter(objects(I, T, {diagonal}, T), {color_val})))"
        else:
            new_src = f"{ret_var} = tuple((frozenset((c for v, c in obj)) for obj in objects(I, F, {diagonal}, T)))"

        try:
            consumed = set()
            # Consume initializers (seen = set(), ret_var = [])
            if 'set()' in src:
                m_seen = re.search(r"(\w+)\s*=\s*set\(\)", src)
                if m_seen:
                    seen_var = m_seen.group(1)
                    for j in range(idx - 1, -1, -1):
                        s_prev = body[j]
                        if (isinstance(s_prev, ast.Assign) and len(s_prev.targets) == 1
                                and isinstance(s_prev.targets[0], ast.Name)
                                and s_prev.targets[0].id == seen_var):
                            consumed.add(j)
                            break

            for j in range(idx - 1, -1, -1):
                s_prev = body[j]
                if (isinstance(s_prev, ast.Assign) and len(s_prev.targets) == 1
                        and isinstance(s_prev.targets[0], ast.Name)
                        and s_prev.targets[0].id == ret_var):
                    if ((isinstance(s_prev.value, ast.List) and not s_prev.value.elts) or
                            (isinstance(s_prev.value, ast.Constant) and s_prev.value.value == ())):
                        consumed.add(j)
                        break

            return ast.parse(new_src).body, consumed
        except Exception:
            return None


class HelperBFSTransformer(ast.NodeTransformer):
    """Replace BFS patterns in helper functions (not verify_* directly).

    Targets helper functions like _taskid_find_components that contain BFS loops.
    """
    def __init__(self, task_id):
        super().__init__()
        self.task_id = task_id
        self.changes = 0

    def visit_FunctionDef(self, node):
        if node.name.startswith('verify_'):
            return self.generic_visit(node)

        # Target helper functions
        try:
            src = ast.unparse(node)
        except Exception:
            return node

        if not ('while' in src and 'append' in src and 'I[' in src):
            return node
        if not any(x in src for x in ['(-1, 0)', '(0, -1)', '(-1,0)', '(0,-1)']):
            return node

        new_body = self._try_replace_bfs(node, src)
        if new_body:
            node.body = new_body
            self.changes += 1
        return node

    def _try_replace_bfs(self, node, src):
        color_val = None
        m = re.search(r"I\[[^\]]+\]\[[^\]]+\]\s*==\s*([A-Z_0-9]+)", src)
        if m:
            color_val = m.group(1)
        else:
            m = re.search(r"I\[[^\]]+\]\[[^\]]+\]\s*!=\s*(ZERO|0)", src)
            if m:
                color_val = "None"

        if not color_val:
            return None

        diagonal = "T" if any(x in src for x in ["(-1, -1)", "(1, 1)", "UP_RIGHT", "(-1,-1)", "(1,1)"]) else "F"

        ret_var = "x3"
        for sub in node.body:
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Name):
                ret_var = sub.value.id
                break

        if color_val != "None":
            new_source = (f"objs = objects(I, T, {diagonal}, T)\n"
                          f"target_objs = colorfilter(objs, {color_val})\n"
                          f"{ret_var} = tuple((frozenset((c for v, c in obj)) for obj in target_objs))\n"
                          f"return {ret_var}")
        else:
            new_source = (f"objs = objects(I, F, {diagonal}, T)\n"
                          f"{ret_var} = tuple((frozenset((c for v, c in obj)) for obj in objs))\n"
                          f"return {ret_var}")
        try:
            return ast.parse(new_source.strip()).body
        except Exception:
            return None
