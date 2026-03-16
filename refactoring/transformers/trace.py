import ast


class TraceTransformer(ast.NodeTransformer):
    """Replace ray-casting while loops with trace() DSL call.

    Pattern (4 instances in verify_256b0a75):
        x52 = decrement(x46)         # start position
        x53 = frozenset()            # accumulator init
        x54 = x52                    # position var = start
        while greater(x54, NEG_ONE): # or while greater(limit, x54)
            x55 = astuple(x54, x47)  # make cell coord
            x56 = index(I, x55)      # read grid color
            if greater(x56, ZERO):   # barrier check
                break
            x53 = insert(x55, x53)   # collect cell
            x54 = decrement(x54)     # step

    → x53 = trace(astuple(x52, x47), direction, I, ZERO)

    Direction is determined by:
      - astuple(varying, fixed) + decrement → UP = (-1, 0)
      - astuple(varying, fixed) + increment → DOWN = (1, 0)
      - astuple(fixed, varying) + decrement → LEFT = (0, -1)
      - astuple(fixed, varying) + increment → RIGHT = (0, 1)
    """

    def __init__(self, task_id=None):
        super().__init__()
        self.task_id = task_id
        self.changes = 0

    def visit_FunctionDef(self, node):
        node.body = self._process_body(node.body)
        return self.generic_visit(node)

    def _process_body(self, body):
        new_body = []
        i = 0
        while i < len(body):
            stmt = body[i]
            # Recurse into if/elif/else
            if isinstance(stmt, ast.If):
                stmt.body = self._process_body(stmt.body)
                if stmt.orelse:
                    stmt.orelse = self._process_body(stmt.orelse)
                new_body.append(stmt)
                i += 1
                continue
            if isinstance(stmt, (ast.For, ast.While)):
                stmt.body = self._process_body(stmt.body)
                new_body.append(stmt)
                i += 1
                continue

            res = self._match_trace_pattern(body, i)
            if res:
                replacement_stmts, skip_count = res
                new_body.extend(replacement_stmts)
                i += skip_count
                self.changes += 1
                continue

            new_body.append(stmt)
            i += 1
        return new_body

    def _match_trace_pattern(self, body, idx):
        """Match the 3-statement trace pattern:
            [idx]   start_var = increment/decrement(origin)
            [idx+1] accum_var = frozenset()
            [idx+2] pos_var = start_var
            [idx+3] while greater(...):
                      cell = astuple(...)
                      color = index(grid, cell)
                      if greater(color, ZERO): break
                      accum = insert(cell, accum)
                      pos = increment/decrement(pos)
        """
        if idx + 3 >= len(body):
            return None

        start_stmt = body[idx]
        accum_stmt = body[idx + 1]
        pos_stmt = body[idx + 2]
        while_stmt = body[idx + 3]

        # Validate structure
        if not all(isinstance(s, ast.Assign) for s in [start_stmt, accum_stmt, pos_stmt]):
            return None
        if not isinstance(while_stmt, ast.While):
            return None

        start_var = self._get_target(start_stmt)
        accum_var = self._get_target(accum_stmt)
        pos_var = self._get_target(pos_stmt)
        if not (start_var and accum_var and pos_var):
            return None

        # start = increment/decrement(origin)
        start_step = self._get_step_call(start_stmt.value)
        if not start_step:
            return None
        origin_var, start_direction = start_step  # e.g. ('x46', 'decrement')

        # accum = frozenset()
        if not self._is_empty_frozenset(accum_stmt.value):
            return None

        # pos = start_var
        if not (isinstance(pos_stmt.value, ast.Name) and pos_stmt.value.id == start_var):
            return None

        # Parse while body
        result = self._parse_while_body(while_stmt, pos_var, accum_var)
        if not result:
            return None
        cell_args, grid_name, match_color, step_fn = result

        # Determine direction from cell construction and step function
        direction = self._determine_direction(cell_args, pos_var, step_fn)
        if not direction:
            return None

        # Determine the fixed coordinate
        fixed_var = cell_args[1] if cell_args[0] == pos_var else cell_args[0]

        # Build trace call:
        # trace(astuple(start_expr, fixed) or astuple(fixed, start_expr), DIRECTION, grid, match_color)
        # Use the start expression (e.g. decrement(x46)) as the varying coordinate
        import copy
        start_expr = copy.deepcopy(start_stmt.value)
        if cell_args[0] == pos_var:
            start_cell = self._make_call('astuple', [
                start_expr,
                ast.Name(id=fixed_var, ctx=ast.Load())
            ])
        else:
            start_cell = self._make_call('astuple', [
                ast.Name(id=fixed_var, ctx=ast.Load()),
                start_expr
            ])

        trace_call = self._make_call('trace', [
            start_cell,
            ast.Name(id=direction, ctx=ast.Load()),
            ast.Name(id=grid_name, ctx=ast.Load()),
            match_color
        ])

        new_assign = ast.Assign(
            targets=[ast.Name(id=accum_var, ctx=ast.Store())],
            value=trace_call,
            lineno=0
        )
        return [new_assign], 4  # consumed start_stmt, accum_stmt, pos_stmt, while_stmt

    def _parse_while_body(self, while_node, pos_var, accum_var):
        """Parse the while body for trace pattern.

        Expected body (4 stmts + if-break):
          cell = astuple(pos_var, fixed) or astuple(fixed, pos_var)
          color = index(grid, cell)
          if greater(color, ZERO): break
          accum = insert(cell, accum)
          pos = increment(pos) or decrement(pos)

        Returns (cell_args, grid_name, match_color, step_fn) or None.
        """
        wbody = while_node.body
        if len(wbody) != 5:
            return None

        # Statement 0: cell = astuple(a, b)
        s0 = wbody[0]
        if not isinstance(s0, ast.Assign):
            return None
        cell_var = self._get_target(s0)
        if not self._is_func_call(s0.value, 'astuple', 2):
            return None
        cell_args = []
        for arg in s0.value.args:
            if not isinstance(arg, ast.Name):
                return None
            cell_args.append(arg.id)
        if pos_var not in cell_args:
            return None

        # Statement 1: color = index(grid, cell)
        s1 = wbody[1]
        if not isinstance(s1, ast.Assign):
            return None
        color_var = self._get_target(s1)
        if not self._is_func_call(s1.value, 'index', 2):
            return None
        if not isinstance(s1.value.args[0], ast.Name):
            return None
        grid_name = s1.value.args[0].id
        if not (isinstance(s1.value.args[1], ast.Name) and s1.value.args[1].id == cell_var):
            return None

        # Statement 2: if greater(color, ZERO): break
        s2 = wbody[2]
        if not isinstance(s2, ast.If):
            return None
        if not (len(s2.body) == 1 and isinstance(s2.body[0], ast.Break)):
            return None
        # Extract the barrier condition to determine match color
        match_color = self._extract_match_color(s2.test, color_var)
        if match_color is None:
            return None

        # Statement 3: accum = insert(cell, accum)
        s3 = wbody[3]
        if not isinstance(s3, ast.Assign):
            return None
        if self._get_target(s3) != accum_var:
            return None
        if not self._is_func_call(s3.value, 'insert', 2):
            return None
        if not (isinstance(s3.value.args[0], ast.Name) and s3.value.args[0].id == cell_var):
            return None

        # Statement 4: pos = increment(pos) or decrement(pos)
        s4 = wbody[4]
        if not isinstance(s4, ast.Assign):
            return None
        if self._get_target(s4) != pos_var:
            return None
        step = self._get_step_call(s4.value)
        if not step:
            return None
        step_target, step_fn = step
        if step_target != pos_var:
            return None

        return cell_args, grid_name, match_color, step_fn

    def _extract_match_color(self, test, color_var):
        """Extract the match color from barrier condition.

        greater(color, ZERO) means stop when color > 0, so match = ZERO
        """
        if self._is_func_call(test, 'greater', 2):
            if isinstance(test.args[0], ast.Name) and test.args[0].id == color_var:
                # greater(color, X) → match = X
                return test.args[1]
        return None

    def _determine_direction(self, cell_args, pos_var, step_fn):
        """Determine direction constant from cell construction and step function.

        cell_args[0] is first arg to astuple, cell_args[1] is second.
        If pos_var is first arg (row varies):
          decrement → UP, increment → DOWN
        If pos_var is second arg (col varies):
          decrement → LEFT, increment → RIGHT
        """
        if cell_args[0] == pos_var:
            # Row varies
            return 'UP' if step_fn == 'decrement' else 'DOWN'
        elif cell_args[1] == pos_var:
            # Col varies
            return 'LEFT' if step_fn == 'decrement' else 'RIGHT'
        return None

    # ── Helpers ──

    def _get_target(self, stmt):
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
                isinstance(stmt.targets[0], ast.Name)):
            return stmt.targets[0].id
        return None

    def _is_empty_frozenset(self, node):
        return (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Name) and
                node.func.id == 'frozenset' and
                len(node.args) == 0)

    def _is_func_call(self, node, name, nargs):
        return (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Name) and
                node.func.id == name and
                len(node.args) == nargs)

    def _get_step_call(self, node):
        """If node is increment(var) or decrement(var), return (var_name, fn_name)."""
        if not isinstance(node, ast.Call):
            return None
        if not isinstance(node.func, ast.Name):
            return None
        if node.func.id not in ('increment', 'decrement'):
            return None
        if len(node.args) != 1:
            return None
        if not isinstance(node.args[0], ast.Name):
            return None
        return (node.args[0].id, node.func.id)

    def _make_call(self, func_name, args):
        return ast.Call(
            func=ast.Name(id=func_name, ctx=ast.Load()),
            args=args,
            keywords=[]
        )
