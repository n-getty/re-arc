import ast
import copy
from collections import defaultdict

def nest_helpers(source_file):
    with open(source_file, 'r') as f:
        source = f.read()
    
    tree = ast.parse(source)
    
    # 1. Separate nodes
    imports = []
    verifiers = {}
    helpers = {}
    other = []
    
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, ast.FunctionDef):
            if node.name.startswith('verify_'):
                verifiers[node.name] = node
            elif node.name.startswith('_'):
                helpers[node.name] = node
            else:
                other.append(node)
        else:
            other.append(node)

    # 2. Find dependencies
    def get_used_helpers(root, all_helpers):
        used = set()
        for node in ast.walk(root):
            if isinstance(node, ast.Name) and node.id in all_helpers:
                used.add(node.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in all_helpers:
                used.add(node.func.id)
        
        # Recursive check
        added = True
        while added:
            added = False
            for hname in list(used):
                hnode = all_helpers[hname]
                for node in ast.walk(hnode):
                    if isinstance(node, ast.Name) and node.id in all_helpers and node.id not in used:
                        used.add(node.id)
                        added = True
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in all_helpers and node.func.id not in used:
                        used.add(node.func.id)
                        added = True
        return used

    # 3. Create new verifier nodes with nested helpers
    new_tree_body = []
    new_tree_body.extend(imports)
    new_tree_body.extend(other)
    
    # Sort verifiers by line number to maintain some order
    sorted_v_names = sorted(verifiers.keys(), key=lambda n: verifiers[n].lineno)
    
    for vname in sorted_v_names:
        vnode = copy.deepcopy(verifiers[vname])
        needed = get_used_helpers(vnode, helpers)
        
        # Insert helpers at the top of the body
        # We should sort helpers by some criteria if needed, or by original lineno
        h_nodes = [copy.deepcopy(helpers[hn]) for hn in sorted(list(needed), key=lambda x: helpers[x].lineno)]
        
        vnode.body = h_nodes + vnode.body
        new_tree_body.append(vnode)

    # 4. Remove redundant helpers from the top level (done by not adding them to new_tree_body)
    
    new_tree = ast.Module(body=new_tree_body, type_ignores=[])
    return ast.unparse(new_tree)

if __name__ == '__main__':
    nested_code = nest_helpers('verifiers.py')
    with open('verifiers.py', 'w') as f:
        f.write(nested_code)
    print("Successfully nested all helper functions in verifiers.py")
