import ast
import collections

class Normalizer(ast.NodeTransformer):
    def __init__(self):
        self.var_map = {}
        self.var_counter = 0
        self.builtins = {'range', 'len', 'enumerate', 'zip', 'print', 'int', 'str', 'list', 'set', 'dict', 'frozenset', 'tuple'}

    def visit_Name(self, node):
        if node.id in self.builtins:
            return node
        
        if node.id not in self.var_map:
            self.var_map[node.id] = f'VAR_{self.var_counter}'
            self.var_counter += 1
        
        return ast.Name(id=self.var_map[node.id], ctx=node.ctx)

    def visit_Attribute(self, node):
        node.value = self.visit(node.value)
        return node

def normalize_ast(node):
    normalizer = Normalizer()
    normalized_node = normalizer.visit(ast.parse(ast.unparse(node)))
    return ast.unparse(normalized_node)

def normalize_stmts(stmts):
    """Normalize a list of statements as a standalone block."""
    normalizer = Normalizer()
    mod = ast.Module(body=stmts, type_ignores=[])
    normalized = normalizer.visit(ast.parse(ast.unparse(mod)))
    return ast.unparse(normalized)


def find_whole_loop_clones(tree):
    """Original approach: find entire loops that match across functions."""
    loop_bodies = []
    
    for func in ast.iter_child_nodes(tree):
        if isinstance(func, ast.FunctionDef) and func.name.startswith('verify_'):
            for node in ast.walk(func):
                if isinstance(node, (ast.While, ast.For)):
                    if len(node.body) >= 1:
                        norm_body = normalize_ast(node)
                        loop_bodies.append((norm_body, func.name, node.lineno))

    body_to_funcs = collections.defaultdict(set)
    body_to_locations = collections.defaultdict(list)
    for body, func_name, lineno in loop_bodies:
        body_to_funcs[body].add(func_name)
        body_to_locations[body].append((func_name, lineno))

    cross_func = {body: funcs for body, funcs in body_to_funcs.items() if len(funcs) >= 2}
    sorted_patterns = sorted(cross_func.items(), key=lambda x: len(x[1]), reverse=True)

    print(f"Found {len(sorted_patterns)} whole-loop patterns shared across 2+ functions")
    print(f"(out of {len(body_to_funcs)} total distinct loop patterns)")
    print("=" * 60)
    for i, (body, funcs) in enumerate(sorted_patterns[:10]):
        print(f"\n--- Whole-Loop Pattern {i+1} (shared by {len(funcs)} functions) ---")
        print(body)
        print(f"\nFunctions: {sorted(funcs)}")
        locs = body_to_locations[body]
        print(f"Locations: {[(f, l) for f, l in locs[:10]]}")
        if len(locs) > 10:
            print(f"  ... and {len(locs) - 10} more")
        print("=" * 60)


def find_subpattern_clones(tree, source, window_sizes=[3, 4, 5], min_funcs=3):
    """Find recurring sub-patterns (sliding windows) inside loop bodies."""
    all_subs = []

    for func in ast.iter_child_nodes(tree):
        if isinstance(func, ast.FunctionDef) and func.name.startswith('verify_'):
            for node in ast.walk(func):
                if isinstance(node, (ast.While, ast.For)):
                    body = node.body
                    for w in window_sizes:
                        if len(body) < w:
                            continue
                        for i in range(len(body) - w + 1):
                            sub_stmts = body[i:i+w]
                            key = normalize_stmts(sub_stmts)
                            all_subs.append((key, w, func.name, node.lineno, body[i].lineno))

    sub_funcs = collections.defaultdict(set)
    sub_locs = collections.defaultdict(list)
    sub_sizes = {}
    for key, w, func_name, loop_lineno, stmt_lineno in all_subs:
        sub_funcs[key].add(func_name)
        sub_locs[key].append((func_name, loop_lineno, stmt_lineno))
        sub_sizes[key] = w

    shared = {k: f for k, f in sub_funcs.items() if len(f) >= min_funcs}

    # Deduplicate: if a longer pattern subsumes a shorter one with the same functions, prefer the longer
    # Sort by (window_size desc, num_funcs desc)
    sorted_shared = sorted(shared.items(), key=lambda x: (sub_sizes[x[0]], len(x[1])), reverse=True)

    print(f"\nFound {len(sorted_shared)} sub-patterns ({'-'.join(map(str, window_sizes))}-stmt windows) shared by {min_funcs}+ functions")
    print("=" * 60)

    lines = source.split('\n')
    shown = 0
    for i, (body, funcs) in enumerate(sorted_shared):
        if shown >= 20:
            break
        w = sub_sizes[body]
        print(f"\n--- Sub-Pattern {shown+1} (window={w}, shared by {len(funcs)} functions) ---")
        print(body)
        print(f"\nFunctions: {sorted(funcs)[:8]}")
        if len(funcs) > 8:
            print(f"  ... and {len(funcs) - 8} more")
        # Show one concrete example
        locs = sub_locs[body]
        ex_func, ex_loop, ex_start = locs[0]
        start_idx = ex_start - 1
        end_idx = min(start_idx + w + 1, len(lines))
        print(f"\nExample from {ex_func} (line {ex_start}):")
        for l in range(start_idx, end_idx):
            print(f"  {lines[l].rstrip()}")
        print("=" * 60)
        shown += 1


def main():
    verifier_file = 'verifiers.py'
    try:
        with open(verifier_file, 'r') as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception as e:
        print(f"Error reading {verifier_file}: {e}")
        return

    # Part 1: Whole-loop exact matches
    find_whole_loop_clones(tree)

    # Part 2: Sub-pattern matches (sliding windows of 3-5 statements in loop bodies)
    find_subpattern_clones(tree, source)


if __name__ == '__main__':
    main()
