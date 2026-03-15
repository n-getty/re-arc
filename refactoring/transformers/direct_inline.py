import ast
import copy


class DirectCallInlineTransformer(ast.NodeTransformer):
    """Inline helper functions that are called exactly once (not as fold callbacks).

    Pattern:
        def _xxx_helper(arg):
            x0 = f(arg)
            return g(x0)

        x5 = _xxx_helper(x4)

    Becomes:
        x5_0 = f(x4)
        x5 = g(x5_0)
    """

    def __init__(self, task_id=None):
        super().__init__()
        self.task_id = task_id
        self.changes = 0

    def visit_FunctionDef(self, node):
        if node.name.startswith('verify_'):
            self.task_id = node.name[7:]
            self._inline_direct_calls(node)
        self.generic_visit(node)
        return node

    def _inline_direct_calls(self, verify_node):
        """Find helpers called exactly once and inline them."""
        changed = True
        while changed:
            changed = False

            # Collect all helper definitions
            helpers = {}
            for stmt in verify_node.body:
                if isinstance(stmt, ast.FunctionDef) and stmt.name.startswith('_'):
                    helpers[stmt.name] = stmt

            # Count calls to each helper (exclude fold callbacks and function defs)
            call_counts = {name: 0 for name in helpers}
            fold_callbacks = set()
            for stmt in verify_node.body:
                if isinstance(stmt, ast.FunctionDef):
                    # Don't count references within the helper's own def
                    # But do count calls from OTHER helpers
                    for node in ast.walk(stmt):
                        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                and node.func.id in call_counts and node.func.id != stmt.name):
                            call_counts[node.func.id] += 1
                    # Check for fold callbacks inside helper bodies
                    for node in ast.walk(stmt):
                        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                and node.func.id == 'fold' and len(node.args) == 3
                                and isinstance(node.args[2], ast.Name)):
                            fold_callbacks.add(node.args[2].id)
                elif isinstance(stmt, ast.Assign):
                    for node in ast.walk(stmt):
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                            if node.func.id in call_counts:
                                call_counts[node.func.id] += 1
                            if (node.func.id == 'fold' and len(node.args) == 3
                                    and isinstance(node.args[2], ast.Name)):
                                fold_callbacks.add(node.args[2].id)
                else:
                    for node in ast.walk(stmt):
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                            if node.func.id in call_counts:
                                call_counts[node.func.id] += 1
                            if (node.func.id == 'fold' and len(node.args) == 3
                                    and isinstance(node.args[2], ast.Name)):
                                fold_callbacks.add(node.args[2].id)

            # Find helpers called exactly once, not used as fold callbacks
            for name, count in call_counts.items():
                if count != 1 or name in fold_callbacks:
                    continue

                helper_def = helpers[name]
                if not self._is_inlineable(helper_def):
                    continue

                # Find the call site
                call_site = self._find_call_site(verify_node.body, name)
                if call_site is None:
                    continue

                stmt_idx, target_var, actual_args = call_site

                # Build inlined statements
                inlined = self._build_inlined_stmts(helper_def, actual_args, target_var)
                if inlined is None:
                    continue

                # Remove helper def and replace call site
                new_body = []
                for i, stmt in enumerate(verify_node.body):
                    if isinstance(stmt, ast.FunctionDef) and stmt.name == name:
                        continue
                    if i == stmt_idx:
                        new_body.extend(inlined)
                    else:
                        new_body.append(stmt)

                verify_node.body = new_body
                ast.fix_missing_locations(verify_node)
                self.changes += 1
                changed = True
                break  # restart scan since indices shifted

    def _is_inlineable(self, helper_def):
        """Check if a helper can be safely inlined."""
        body = helper_def.body

        # Must end with a return
        if not body or not isinstance(body[-1], ast.Return):
            return False

        # No nested function defs
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef):
                return False

        # No loops (while/for) — these can't be inlined as expressions
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, (ast.While, ast.For)):
                    return False

        # No early returns (only the final return)
        for stmt in body[:-1]:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Return):
                    return False

        # All non-return stmts must be assignments or if-stmts
        for stmt in body[:-1]:
            if not isinstance(stmt, (ast.Assign, ast.If, ast.AugAssign)):
                return False

        return True

    def _find_call_site(self, body, helper_name):
        """Find statement index where helper is called as x = helper(args).

        Returns (stmt_idx, target_var, actual_args) or None.
        """
        for i, stmt in enumerate(body):
            if isinstance(stmt, ast.FunctionDef):
                continue
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
                if isinstance(target, ast.Name) and isinstance(stmt.value, ast.Call):
                    call = stmt.value
                    if isinstance(call.func, ast.Name) and call.func.id == helper_name:
                        return (i, target.id, call.args)
        return None

    def _build_inlined_stmts(self, helper_def, actual_args, target_var):
        """Build the list of statements that replace the call.

        Renames internal variables to avoid collisions using target_var as prefix.
        """
        formal_params = [arg.arg for arg in helper_def.args.args]
        if len(formal_params) != len(actual_args):
            return None

        body = copy.deepcopy(helper_def.body)

        # Build param→actual_arg substitution
        param_subs = {}
        for param, arg in zip(formal_params, actual_args):
            param_subs[param] = copy.deepcopy(arg)

        # Find all local variables defined in the helper (excluding params)
        local_vars = set()
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id not in formal_params:
                        local_vars.add(t.id)
                    elif isinstance(t, ast.Tuple):
                        for elt in t.elts:
                            if isinstance(elt, ast.Name) and elt.id not in formal_params:
                                local_vars.add(elt.id)
            elif isinstance(stmt, ast.If):
                self._collect_if_vars(stmt, formal_params, local_vars)

        # Build rename map: internal vars → prefixed vars
        rename_map = {}
        for v in local_vars:
            rename_map[v] = f"{target_var}_{v}"

        # Combined substitution: params → actual args, locals → renamed
        all_subs = {}
        all_subs.update(param_subs)
        for old, new in rename_map.items():
            all_subs[old] = ast.Name(id=new, ctx=ast.Load())

        # Apply substitutions to body
        result = []
        for stmt in body[:-1]:  # skip final return
            new_stmt = copy.deepcopy(stmt)
            new_stmt = self._substitute_all(new_stmt, all_subs, rename_map)
            result.append(new_stmt)

        # Handle the return: assign return value to target_var
        ret_stmt = body[-1]
        ret_val = copy.deepcopy(ret_stmt.value)
        ret_val = self._substitute_expr(ret_val, all_subs)

        final_assign = ast.Assign(
            targets=[ast.Name(id=target_var, ctx=ast.Store())],
            value=ret_val
        )
        result.append(final_assign)
        for s in result:
            ast.fix_missing_locations(s)
        return result

    def _collect_if_vars(self, if_node, params, local_vars):
        """Collect variable assignments inside if/else blocks."""
        for stmt in if_node.body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id not in params:
                        local_vars.add(t.id)
            elif isinstance(stmt, ast.If):
                self._collect_if_vars(stmt, params, local_vars)
        for stmt in if_node.orelse:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id not in params:
                        local_vars.add(t.id)
            elif isinstance(stmt, ast.If):
                self._collect_if_vars(stmt, params, local_vars)

    def _substitute_expr(self, node, subs):
        """Replace Name(Load) nodes with substitution expressions."""
        if not subs:
            return node

        class ExprSubstitutor(ast.NodeTransformer):
            def visit_Name(self, n):
                if isinstance(n.ctx, ast.Load) and n.id in subs:
                    return copy.deepcopy(subs[n.id])
                return n

        return ExprSubstitutor().visit(node)

    def _substitute_all(self, stmt, subs, rename_map):
        """Replace both Load and Store references: loads get substituted, stores get renamed."""

        class FullSubstitutor(ast.NodeTransformer):
            def visit_Name(self, n):
                if isinstance(n.ctx, ast.Load) and n.id in subs:
                    return copy.deepcopy(subs[n.id])
                if isinstance(n.ctx, ast.Store) and n.id in rename_map:
                    return ast.Name(id=rename_map[n.id], ctx=ast.Store())
                return n

        return FullSubstitutor().visit(stmt)
