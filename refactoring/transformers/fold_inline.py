import ast
import copy


class FoldInlineTransformer(ast.NodeTransformer):
    """Inline fold helper functions into lambda expressions.

    Handles three sub-cases:
    4a: Pure-assign helpers → lambda (no ifs, no loops)
    4b: If-branch helpers → branch() expressions
    4c: State-tuple unpacking helpers
    """

    def __init__(self, task_id=None):
        super().__init__()
        self.task_id = task_id
        self.changes = 0

    def visit_FunctionDef(self, node):
        if node.name.startswith('verify_'):
            self.task_id = node.name[7:]
            self._inline_fold_helpers(node)
        self.generic_visit(node)
        return node

    def _inline_fold_helpers(self, verify_node):
        """Find fold(coll, init, helper) calls and inline the helper."""
        changed = True
        while changed:
            changed = False
            helpers = {}
            for stmt in verify_node.body:
                if isinstance(stmt, ast.FunctionDef):
                    helpers[stmt.name] = stmt

            new_body = []
            removed_helpers = set()
            for stmt in verify_node.body:
                if isinstance(stmt, ast.FunctionDef) and stmt.name in removed_helpers:
                    continue

                fold_info = self._find_fold_call(stmt)
                if fold_info:
                    helper_name = fold_info
                    if helper_name in helpers:
                        helper_def = helpers[helper_name]
                        lambda_node = self._try_inline(helper_def)
                        if lambda_node is not None:
                            self._replace_fold_helper(stmt, helper_name, lambda_node)
                            removed_helpers.add(helper_name)
                            self.changes += 1
                            changed = True

                if isinstance(stmt, ast.FunctionDef) and stmt.name in removed_helpers:
                    continue
                new_body.append(stmt)

            verify_node.body = new_body
            ast.fix_missing_locations(verify_node)

    def _find_fold_call(self, stmt):
        """Find fold(coll, init, helper_name) in an assignment. Returns helper_name or None."""
        if not isinstance(stmt, ast.Assign):
            return None
        call = stmt.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == 'fold' and len(call.args) == 3):
            return None
        helper_arg = call.args[2]
        if isinstance(helper_arg, ast.Name) and not helper_arg.id.startswith('lambda'):
            return helper_arg.id
        return None

    def _try_inline(self, helper_def):
        """Try to convert a fold helper to a lambda expression. Returns Lambda node or None."""
        # Must have exactly 2 params
        if len(helper_def.args.args) != 2:
            return None

        acc_param = helper_def.args.args[0].arg
        item_param = helper_def.args.args[1].arg

        body = helper_def.body

        # Check for state tuple unpacking at start: (a, b) = _state
        state_vars = None
        effective_body = body
        if (len(body) >= 2 and isinstance(body[0], ast.Assign)
                and len(body[0].targets) == 1
                and isinstance(body[0].targets[0], ast.Tuple)
                and isinstance(body[0].value, ast.Name)
                and body[0].value.id == acc_param):
            state_vars = [elt.id for elt in body[0].targets[0].elts
                          if isinstance(elt, ast.Name)]
            effective_body = body[1:]

        # Try 4a: pure assign → lambda
        result = self._try_pure_assign(effective_body, acc_param, item_param, state_vars)
        if result is not None:
            return result

        # Try 4b: if-branch → branch()
        result = self._try_if_branch(effective_body, acc_param, item_param, state_vars)
        if result is not None:
            return result

        return None

    def _try_pure_assign(self, body, acc_param, item_param, state_vars):
        """4a: All statements are assignments, last is return. Build lambda via substitution."""
        if len(body) < 1:
            return None
        if not isinstance(body[-1], ast.Return):
            return None

        # All non-return stmts must be simple assignments
        for stmt in body[:-1]:
            if not isinstance(stmt, ast.Assign):
                return None
            if len(stmt.targets) != 1:
                return None
            # No nested function defs, no loops, no ifs
            for node in ast.walk(stmt.value):
                if isinstance(node, (ast.FunctionDef, ast.While, ast.For, ast.If,
                                     ast.ListComp, ast.SetComp, ast.DictComp)):
                    return None

        # Build substitution map: var -> expression
        # Process assignments in order, substituting earlier into later
        subs = {}
        for stmt in body[:-1]:
            target = stmt.targets[0]
            if not isinstance(target, ast.Name):
                return None
            var = target.id
            expr = copy.deepcopy(stmt.value)
            expr = self._substitute_vars(expr, subs)
            subs[var] = expr

        # Build return expression with all substitutions
        ret_expr = copy.deepcopy(body[-1].value)
        ret_expr = self._substitute_vars(ret_expr, subs)

        # Handle state tuple: acc_param → state_vars need to be replaced with subscripts
        if state_vars:
            state_subs = {}
            for i, v in enumerate(state_vars):
                state_subs[v] = ast.Subscript(
                    value=ast.Name(id=acc_param, ctx=ast.Load()),
                    slice=ast.Constant(value=i),
                    ctx=ast.Load()
                )
            ret_expr = self._substitute_vars(ret_expr, state_subs)

        lambda_node = ast.Lambda(
            args=ast.arguments(
                posonlyargs=[],
                args=[ast.arg(arg=acc_param), ast.arg(arg=item_param)],
                kwonlyargs=[], kw_defaults=[], defaults=[]
            ),
            body=ret_expr
        )
        ast.fix_missing_locations(lambda_node)
        return lambda_node

    def _try_if_branch(self, body, acc_param, item_param, state_vars):
        """4b: Pattern: optional assigns, then if/else modifying acc, then return acc.

        Converts to: branch(condition, if_value, else_value)
        """
        if len(body) < 2:
            return None
        if not isinstance(body[-1], ast.Return):
            return None

        # Find the if statement
        if_idx = None
        for i, stmt in enumerate(body[:-1]):
            if isinstance(stmt, ast.If):
                if_idx = i
                break

        if if_idx is None:
            return None

        if_stmt = body[if_idx]
        pre_stmts = body[:if_idx]

        # Pre-statements must all be simple assignments with Name targets
        for stmt in pre_stmts:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                return None
            if not isinstance(stmt.targets[0], ast.Name):
                return None
            for node in ast.walk(stmt.value):
                if isinstance(node, (ast.FunctionDef, ast.While, ast.For)):
                    return None

        # The if body and else body must be simple: only assignments, no nested ifs/loops/funcdefs
        if not self._is_simple_if_body(if_stmt.body):
            return None
        if if_stmt.orelse and not self._is_simple_if_body(if_stmt.orelse):
            return None

        # Must have an else branch (for branch() we need both sides)
        # If no else, the implicit else is "keep acc unchanged"
        ret_expr = body[-1].value
        ret_var = None
        if isinstance(ret_expr, ast.Name):
            ret_var = ret_expr.id
        elif isinstance(ret_expr, ast.Tuple):
            pass  # tuple return - more complex case

        # Only handle single-var acc for now
        if ret_var is None:
            return None

        # Check that the if modifies the return var
        if_assigns = self._extract_var_assign(if_stmt.body, ret_var)
        if if_assigns is None:
            return None

        if if_stmt.orelse:
            else_assigns = self._extract_var_assign(if_stmt.orelse, ret_var)
            if else_assigns is None:
                return None
        else:
            # No else: acc stays unchanged
            else_assigns = ast.Name(id=ret_var, ctx=ast.Load())

        # After the if, there should be no more stmts before return (except the return itself)
        post_if_stmts = body[if_idx + 1:-1]
        if post_if_stmts:
            return None

        # Build substitution from pre-stmts
        subs = {}
        for stmt in pre_stmts:
            var = stmt.targets[0].id
            expr = copy.deepcopy(stmt.value)
            expr = self._substitute_vars(expr, subs)
            subs[var] = expr

        condition = copy.deepcopy(if_stmt.test)
        condition = self._substitute_vars(condition, subs)
        if_val = copy.deepcopy(if_assigns)
        if_val = self._substitute_vars(if_val, subs)
        else_val = copy.deepcopy(else_assigns)
        else_val = self._substitute_vars(else_val, subs)

        # Handle state tuple subscript replacement
        if state_vars:
            state_subs = {}
            for i, v in enumerate(state_vars):
                state_subs[v] = ast.Subscript(
                    value=ast.Name(id=acc_param, ctx=ast.Load()),
                    slice=ast.Constant(value=i),
                    ctx=ast.Load()
                )
            condition = self._substitute_vars(condition, state_subs)
            if_val = self._substitute_vars(if_val, state_subs)
            else_val = self._substitute_vars(else_val, state_subs)

        # Build: branch(condition, if_val, else_val)
        branch_call = ast.Call(
            func=ast.Name(id='branch', ctx=ast.Load()),
            args=[condition, if_val, else_val],
            keywords=[]
        )

        lambda_node = ast.Lambda(
            args=ast.arguments(
                posonlyargs=[],
                args=[ast.arg(arg=acc_param), ast.arg(arg=item_param)],
                kwonlyargs=[], kw_defaults=[], defaults=[]
            ),
            body=branch_call
        )
        ast.fix_missing_locations(lambda_node)
        return lambda_node

    def _is_simple_if_body(self, stmts):
        """Check that all stmts are simple assignments (no nested ifs, loops, funcdefs)."""
        for s in stmts:
            if isinstance(s, ast.Assign) and len(s.targets) == 1:
                for node in ast.walk(s.value):
                    if isinstance(node, (ast.FunctionDef, ast.While, ast.For, ast.If)):
                        return False
            elif isinstance(s, ast.Return):
                pass  # returns in if body are ok
            else:
                return False
        return True

    def _extract_var_assign(self, stmts, var_name):
        """Extract the expression assigned to var_name from a list of stmts.

        If the variable is assigned multiple times (chained), substitute through.
        Returns the final expression or None if var_name is never assigned.
        """
        subs = {}
        found = False
        for s in stmts:
            if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
                v = s.targets[0].id
                expr = copy.deepcopy(s.value)
                expr = self._substitute_vars(expr, subs)
                subs[v] = expr
                if v == var_name:
                    found = True
            elif isinstance(s, ast.Return):
                # If there's a return in the if body, use that value
                ret = copy.deepcopy(s.value)
                ret = self._substitute_vars(ret, subs)
                return ret

        if found and var_name in subs:
            return subs[var_name]
        return None

    def _substitute_vars(self, node, subs):
        """Replace Name nodes in an AST with their substitution expressions."""
        if not subs:
            return node

        class Substitutor(ast.NodeTransformer):
            def visit_Name(self, n):
                if n.id in subs and isinstance(n.ctx, ast.Load):
                    return copy.deepcopy(subs[n.id])
                return n

        return Substitutor().visit(node)

    def _replace_fold_helper(self, stmt, helper_name, lambda_node):
        """Replace fold(coll, init, helper_name) with fold(coll, init, lambda ...)."""
        call = stmt.value
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == 'fold' and len(call.args) == 3
                and isinstance(call.args[2], ast.Name) and call.args[2].id == helper_name):
            call.args[2] = lambda_node
            ast.fix_missing_locations(stmt)
