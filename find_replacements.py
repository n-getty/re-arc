import ast
import collections
import os
import sys


def check_for_2d_grid_iteration(node):
    """Detect nested while loops iterating over grid with I[r][c] indexing."""
    if not isinstance(node, ast.While):
        return False
    has_inner_while = any(isinstance(child, ast.While) for child in node.body)
    if has_inner_while:
        body_str = ast.unparse(node)
        if 'I[' in body_str and '][' in body_str:
            return True
    return False


def check_for_mostcommon_pattern(node):
    """Detect manual frequency counting (reimplements mostcommon/leastcommon)."""
    body_str = ast.unparse(node)
    if 'not in' in body_str and '.index(' in body_str and ' + 1' in body_str:
        return True
    return False


def check_for_fold_eligible_while(node):
    """Detect while loops that increment an index and use it for indexing.
    
    Pattern:
        i = ZERO
        while i < size(x) / len(x) / N:
            ... x[i] ...
            i = increment(i)  OR  i = i + 1
    
    Returns (inc_var, body_stmt_count) or None.
    """
    if not isinstance(node, ast.While):
        return None
    body = node.body
    if len(body) < 2:
        return None

    last_stmt = body[-1]
    inc_var = None

    # Check for: VAR = increment(VAR) or VAR = VAR + 1
    if (isinstance(last_stmt, ast.Assign) and len(last_stmt.targets) == 1
            and isinstance(last_stmt.targets[0], ast.Name)):
        target = last_stmt.targets[0].id
        val = last_stmt.value
        # increment(VAR)
        if (isinstance(val, ast.Call) and isinstance(val.func, ast.Name)
                and val.func.id == 'increment' and len(val.args) == 1
                and isinstance(val.args[0], ast.Name) and val.args[0].id == target):
            inc_var = target
        # VAR + 1
        elif (isinstance(val, ast.BinOp) and isinstance(val.op, ast.Add)
              and isinstance(val.left, ast.Name) and val.left.id == target
              and isinstance(val.right, ast.Constant) and val.right.value == 1):
            inc_var = target

    if inc_var is None:
        return None

    # Check body uses indexing with the increment variable
    body_str = ast.unparse(node)
    if f'[{inc_var}]' not in body_str:
        return None

    non_inc_stmts = len(body) - 1
    return (inc_var, non_inc_stmts)


def check_for_bbox_pattern(body, start_idx):
    """Detect 4 consecutive calls to uppermost/lowermost/leftmost/rightmost on the same arg.
    
    Pattern:
        a = uppermost(x)
        b = lowermost(x)
        c = leftmost(x)
        d = rightmost(x)
    
    Returns (matched_count, arg_name, target_vars) or None.
    """
    bbox_funcs = {'uppermost', 'lowermost', 'leftmost', 'rightmost'}
    
    if start_idx + 3 >= len(body):
        return None
    
    funcs_found = set()
    first_arg = None
    target_vars = []
    
    for j in range(4):
        stmt = body[start_idx + j]
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id in bbox_funcs
                and len(stmt.value.args) == 1):
            return None
        
        func_name = stmt.value.func.id
        if func_name in funcs_found:
            return None  # duplicate, not the pattern
        funcs_found.add(func_name)
        
        arg = ast.unparse(stmt.value.args[0])
        if first_arg is None:
            first_arg = arg
        elif arg != first_arg:
            return None  # different arguments
        
        target_vars.append(stmt.targets[0].id if isinstance(stmt.targets[0], ast.Name) else ast.unparse(stmt.targets[0]))
    
    if funcs_found == bbox_funcs:
        return (4, first_arg, target_vars)
    return None


def check_for_shift_fill_loop(node):
    """Detect loops containing both shift() and fill()/paint() — stamp patterns."""
    if not isinstance(node, (ast.While, ast.For)):
        return False
    body_str = ast.unparse(node)
    has_shift = 'shift(' in body_str
    has_fill_or_paint = 'fill(' in body_str or 'paint(' in body_str
    return has_shift and has_fill_or_paint


def check_for_tuple_unpacking(body, start_idx):
    """Detect consecutive indexing: x = arr[0]; y = arr[1]; z = arr[2]; ...
    
    Returns (count, array_var, target_vars) or None.
    """
    if start_idx + 2 >= len(body):
        return None
    
    array_var = None
    target_vars = []
    expected_idx = None
    
    for j in range(start_idx, len(body)):
        stmt = body[j]
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.value, ast.Subscript)
                and isinstance(stmt.value.slice, ast.Constant)
                and isinstance(stmt.value.slice.value, int)):
            break
        
        arr = ast.unparse(stmt.value.value)
        idx = stmt.value.slice.value
        
        if array_var is None:
            array_var = arr
            expected_idx = idx
        elif arr != array_var or idx != expected_idx:
            break
        
        target_vars.append(stmt.targets[0].id if isinstance(stmt.targets[0], ast.Name) else ast.unparse(stmt.targets[0]))
        expected_idx += 1
    
    count = len(target_vars)
    if count >= 3:
        return (count, array_var, target_vars)
    return None


def main():
    verifier_file = sys.argv[1] if len(sys.argv) > 1 else 'verifiers.py'
    if not os.path.exists(verifier_file):
        print(f"Error: {verifier_file} not found.")
        return
    with open(verifier_file, 'r') as f:
        source = f.read()
    tree = ast.parse(source)

    # Counters for summary
    counts = collections.Counter()
    results = []

    for func in ast.iter_child_nodes(tree):
        if not isinstance(func, ast.FunctionDef) or not func.name.startswith('verify_'):
            continue

        for node in ast.walk(func):
            # 1. Manual mostcommon/frequency counting
            if isinstance(node, ast.While) and check_for_mostcommon_pattern(node):
                counts['mostcommon'] += 1
                results.append((func.name, node.lineno, 'mostcommon',
                    'Manual frequency counting → use mostcommon()/leastcommon()'))

            # 2. Manual 2D grid iteration
            if check_for_2d_grid_iteration(node):
                counts['2d_grid'] += 1
                results.append((func.name, node.lineno, '2d_grid',
                    'Manual 2D grid iteration → use crop/subgrid/colorcount/ofcolor/etc'))

            # 3. Fold-eligible while loops
            if isinstance(node, ast.While):
                fold_result = check_for_fold_eligible_while(node)
                if fold_result:
                    inc_var, stmt_count = fold_result
                    complexity = 'simple' if stmt_count <= 2 else ('medium' if stmt_count <= 4 else 'complex')
                    counts[f'fold_{complexity}'] += 1
                    counts['fold_total'] += 1
                    results.append((func.name, node.lineno, f'fold_{complexity}',
                        f'While+increment loop ({stmt_count} stmts) → use fold() [{complexity}]'))

            # 4. Shift+fill/paint loop
            if isinstance(node, (ast.While, ast.For)) and check_for_shift_fill_loop(node):
                counts['shift_fill'] += 1
                results.append((func.name, node.lineno, 'shift_fill',
                    'Loop with shift()+fill()/paint() → candidate for fold() with stamp'))

            # 5. Bbox pattern and tuple unpacking (check statement sequences)
            if isinstance(node, (ast.While, ast.For, ast.FunctionDef)):
                body = node.body
                i = 0
                while i < len(body):
                    # Bbox pattern
                    bbox_result = check_for_bbox_pattern(body, i)
                    if bbox_result:
                        matched, arg, targets = bbox_result
                        counts['bbox'] += 1
                        results.append((func.name, body[i].lineno, 'bbox',
                            f'4× uppermost/lowermost/leftmost/rightmost({arg}) → use bbox({arg})'))
                        i += matched
                        continue

                    # Tuple unpacking
                    unpack_result = check_for_tuple_unpacking(body, i)
                    if unpack_result:
                        count, arr, targets = unpack_result
                        counts['tuple_unpack'] += 1
                        results.append((func.name, body[i].lineno, 'tuple_unpack',
                            f'{count}× sequential indexing {arr}[0..{count-1}] → use tuple unpacking'))
                        i += count
                        continue

                    i += 1

    # Print summary
    print("=" * 70)
    print("REPLACEMENT OPPORTUNITY SUMMARY")
    print("=" * 70)
    print(f"  fold (total):     {counts['fold_total']:4d}  while+increment → fold()")
    print(f"    simple (1-2):   {counts['fold_simple']:4d}")
    print(f"    medium (3-4):   {counts['fold_medium']:4d}")
    print(f"    complex (5+):   {counts['fold_complex']:4d}")
    print(f"  bbox:             {counts['bbox']:4d}  4× uppermost/etc → bbox()")
    print(f"  shift+fill/paint: {counts['shift_fill']:4d}  stamp loops → fold() candidate")
    print(f"  2d_grid:          {counts['2d_grid']:4d}  nested while I[][] → DSL primitives")
    print(f"  tuple_unpack:     {counts['tuple_unpack']:4d}  arr[0],arr[1],... → destructuring")
    print(f"  mostcommon:       {counts['mostcommon']:4d}  manual freq count → mostcommon()")
    print("=" * 70)

    # Print detailed results grouped by category
    print(f"\nDetailed Results ({len(results)} total):\n")

    for category in ['fold_simple', 'fold_medium', 'bbox', 'shift_fill', '2d_grid', 'tuple_unpack', 'mostcommon', 'fold_complex']:
        cat_results = [(fn, ln, cat, msg) for fn, ln, cat, msg in results if cat == category]
        if not cat_results:
            continue
        print(f"--- {category} ({len(cat_results)} hits) ---")
        for fn, ln, cat, msg in cat_results[:15]:
            print(f"  {fn} (line {ln}): {msg}")
        if len(cat_results) > 15:
            print(f"  ... and {len(cat_results) - 15} more")
        print()


if __name__ == '__main__':
    main()
