import ast
import copy
import os
import subprocess


def run_validation(task_id, filepath):
    """Run validate_verifier.py and return (passed, stdout)."""
    result = subprocess.run(
        ['python3', 'validate_verifier.py', task_id, filepath],
        capture_output=True,
        text=True
    )
    return 'PASS' in result.stdout, result.stdout.strip()


def find_helpers(node, task_id, all_funcs):
    """Find all helper functions (_{task_id}_*) referenced by a verifier node."""
    found, src = set(), ast.unparse(node)
    for fn in all_funcs:
        if fn.startswith(f'_{task_id}_') and fn in src:
            found.add(fn)
    # Transitively find helpers used by helpers
    expanded = True
    while expanded:
        expanded = False
        for fn in list(found):
            hsrc = ast.unparse(all_funcs[fn])
            for on in all_funcs:
                if on.startswith(f'_{task_id}_') and on in hsrc and on not in found:
                    found.add(on)
                    expanded = True
    return [all_funcs[n] for n in found]


def process_verifiers(verifier_file, output_dir, transformer_classes, skip_ids=('0607ce86', 'f18ec8cc', 'fd4b2b02')):
    """Unified runner: apply transformer classes to all verify_* functions, validate, write to output_dir.

    Each transformer class must accept (task_id) or () as constructor args and expose a .changes attribute.
    """
    with open(verifier_file, 'r') as f:
        source = f.read()
    tree = ast.parse(source)
    all_funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    os.makedirs(output_dir, exist_ok=True)

    imports = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    header = "\n".join(imports) + "\n\n" if imports else "from dsl import *\n\n"

    cand, succ, fail = 0, 0, 0
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith('verify_'):
            continue
        task_id = node.name[7:]
        if task_id in skip_ids:
            continue

        current_node = copy.deepcopy(node)
        total_changes = 0
        applied = []

        for TClass in transformer_classes:
            # Some transformers take task_id, some don't
            try:
                trans = TClass(task_id)
            except TypeError:
                trans = TClass()
            current_node = trans.visit(current_node)
            if trans.changes > 0:
                total_changes += trans.changes
                applied.append(f"{TClass.__name__}({trans.changes})")

        if total_changes == 0:
            continue
        cand += 1

        helpers = find_helpers(current_node, task_id, all_funcs)
        hblock = "\n\n".join(ast.unparse(h) for h in helpers) + "\n\n" if helpers else ""
        out_p = os.path.join(output_dir, f"verify_{task_id}.py")
        ast.fix_missing_locations(current_node)
        with open(out_p, 'w') as f:
            f.write(header + hblock + ast.unparse(current_node) + "\n")

        ok, out = run_validation(task_id, out_p)
        if ok:
            print(f"  PASS  verify_{task_id}  [{', '.join(applied)}]  {out}")
            succ += 1
        else:
            print(f"  FAIL  verify_{task_id}  [{', '.join(applied)}]  {out}")
            os.remove(out_p)
            fail += 1

    print(f"\nRefactoring complete:\n  Candidates: {cand}\n  Passed:     {succ}\n  Failed:     {fail}")
