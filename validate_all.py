import ast
import multiprocessing
import subprocess
import sys
import os
import json
import time

def validate_task(task_id):
    """Run validation for a single task."""
    try:
        result = subprocess.run(
            ['python3', 'validate_verifier.py', task_id, 'verifiers.py'],
            capture_output=True,
            text=True,
            timeout=30 # Safety timeout
        )
        passed = 'PASS' in result.stdout
        return task_id, passed, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return task_id, False, "TIMEOUT"
    except Exception as e:
        return task_id, False, str(e)

def main():
    verifier_file = 'verifiers.py'
    if not os.path.exists(verifier_file):
        print(f"Error: {verifier_file} not found.")
        return

    # Parse function names from verifiers.py
    with open(verifier_file, 'r') as f:
        tree = ast.parse(f.read())
    
    task_ids = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith('verify_'):
            task_ids.append(node.name[7:])
    
    print(f"Total verifiers found: {len(task_ids)}")
    
    # Use multiprocessing to speed up validation
    num_processes = multiprocessing.cpu_count()
    print(f"Running validation using {num_processes} processes...")
    
    start_time = time.time()
    
    with multiprocessing.Pool(processes=num_processes) as pool:
        results = pool.map(validate_task, task_ids)
    
    end_time = time.time()
    
    passed_count = sum(1 for _, passed, _ in results if passed)
    failed_results = [(tid, out) for tid, passed, out in results if not passed]
    
    print("\n" + "="*40)
    print(f"VALIDATION SUMMARY")
    print("="*40)
    print(f"Total:      {len(task_ids)}")
    print(f"Passed:     {passed_count}")
    print(f"Failed:     {len(failed_results)}")
    print(f"Time:       {end_time - start_time:.2f} seconds")
    print("="*40)
    
    if failed_results:
        print("\nFailed Tasks:")
        for tid, out in failed_results:
            print(f"  - {tid}: {out}")
        sys.exit(1)
    else:
        print("\nAll verifiers passed successfully!")
        sys.exit(0)

if __name__ == '__main__':
    main()
