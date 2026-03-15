import ast
import sys

def score_verifier(func_node):
    score = 0
    reasons = []
    
    for node in ast.walk(func_node):
        if isinstance(node, ast.For):
            score += 2
            reasons.append("for_loop")
        elif isinstance(node, ast.While):
            score += 3
            reasons.append("while_loop")
        elif isinstance(node, ast.FunctionDef) and node != func_node:
            score += 5
            reasons.append("helper_function")
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            score += 1
            reasons.append("comprehension")
        elif isinstance(node, ast.If):
            score += 0.5
            reasons.append("if_statement")
            
    return score, reasons

def main():
    verifier_file = 'verifiers.py'
    print("Parsing AST...", flush=True)
    try:
        with open(verifier_file, 'r') as f:
            tree = ast.parse(f.read())
        print("AST parsed.", flush=True)
    except Exception as e:
        print(f"Error reading {verifier_file}: {e}")
        return

    results = []
    print("Iterating functions...", flush=True)
    count = 0
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith('verify_'):
            score, reasons = score_verifier(node)
            # Count occurrences of reasons
            reason_counts = {}
            for r in reasons:
                reason_counts[r] = reason_counts.get(r, 0) + 1
            
            # format reasons
            reason_str = ", ".join([f"{k}:{v}" for k, v in reason_counts.items()])
            lines = node.end_lineno - node.lineno + 1
            results.append((score, lines, node.name, reason_str))

    # Sort primarily by score (descending), then by lines (descending)
    results.sort(key=lambda x: (x[0], x[1]), reverse=True)

    print("Top 20 Most Complex Verifiers:")
    print(f"{'Rank':<5} | {'Verifier':<15} | {'Score':<6} | {'Lines':<5} | {'Issues'}")
    print("-" * 80)
    for i, (score, lines, name, reasons) in enumerate(results[:20]):
        print(f"{i+1:<5} | {name:<15} | {score:<6.1f} | {lines:<5} | {reasons}")

if __name__ == '__main__':
    main()
