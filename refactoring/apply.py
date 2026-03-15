import ast
import os
import re


def get_function_range(source, func_name):
    """Get the start and end line (1-indexed) of a function in source."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node.lineno, node.end_lineno
    return None


def main(target_file='verifiers.py', refactor_dir='pending_refactors'):
    if not os.path.exists(refactor_dir):
        print(f"Directory {refactor_dir} not found.")
        return

    with open(target_file, 'r') as f:
        content = f.read()

    refactored_files = sorted([
        f for f in os.listdir(refactor_dir)
        if f.startswith('verify_') and f.endswith('.py')
    ])
    print(f"Found {len(refactored_files)} refactored verifiers to apply.")

    tree = ast.parse(content)
    lines = content.split('\n')

    def get_source(node):
        return '\n'.join(lines[node.lineno-1:node.end_lineno])

    # Build parts list: (type, name, source)
    parts = []
    last_end = 0
    for node in tree.body:
        gap = '\n'.join(lines[last_end:node.lineno-1])
        if gap.strip() or (node.lineno-1 > last_end):
            parts.append(('white', None, gap))

        if isinstance(node, ast.FunctionDef):
            parts.append(('function', node.name, get_source(node)))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            parts.append(('import', None, get_source(node)))
        else:
            parts.append(('other', None, get_source(node)))
        last_end = node.end_lineno

    if last_end < len(lines):
        parts.append(('white', None, '\n'.join(lines[last_end:])))

    applied_count = 0

    for fname in refactored_files:
        task_id = fname[7:-3]
        v_func_name = f"verify_{task_id}"

        p = os.path.join(refactor_dir, fname)
        if not os.path.exists(p):
            continue
        with open(p, 'r') as f:
            refactored_full = f.read()

        ref_tree = ast.parse(refactored_full)
        ref_parts = []
        for node in ref_tree.body:
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                ref_parts.append(ast.unparse(node))

        refactored_code = '\n\n'.join(ref_parts)

        new_parts = []
        main_replaced = False

        for ptype, pname, psource in parts:
            if ptype == 'function':
                if pname == v_func_name:
                    new_parts.append(('function', pname, refactored_code))
                    main_replaced = True
                elif pname.startswith(f'_{task_id}_'):
                    continue
                else:
                    new_parts.append((ptype, pname, psource))
            else:
                new_parts.append((ptype, pname, psource))

        if main_replaced:
            parts = new_parts
            applied_count += 1
        else:
            print(f"Warning: Could not find {v_func_name} in {target_file}")

    new_content = ""
    for ptype, pname, psource in parts:
        new_content += psource + '\n'

    new_content = re.sub(r'\n{4,}', '\n\n\n', new_content)

    with open(target_file, 'w') as f:
        f.write(new_content)

    print(f"Successfully applied {applied_count} refactors to {target_file}.")


if __name__ == '__main__':
    main()
