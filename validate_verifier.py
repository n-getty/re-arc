#!/usr/bin/env python3
"""Robust validator for ARC verifiers.
Prioritizes legacy arc_original data for tasks that exist there to maintain bug-fix compatibility.
"""

import json
import importlib.util
import sys
import os
from utils import fix_bugs, format_task

def load_task(task_id):
    """Load task data, prioritizing arc_original for legacy compatibility."""
    base = os.path.dirname(os.path.abspath(__file__))
    
    # 1. TRY ORIGINAL ARC (ARC AGI 1) FIRST - This matches original Bitbucket environment
    for folder in ['training', 'evaluation']:
        opath = os.path.join(base, 'data', 'arc_original', folder, f'{task_id}.json')
        if os.path.exists(opath):
            with open(opath) as f:
                data = json.load(f)
            # Use original utils formatting and fixes
            dataset = {task_id: format_task(data)}
            fix_bugs(dataset)
            return dataset[task_id]

    # 2. FALLBACK TO PRIZE 2025 (ARC AGI 2)
    challenges, solutions = {}, {}
    for prefix in ['arc-agi_training', 'arc-agi_evaluation']:
        cpath = os.path.join(base, 'data', 'arc-prize-2025', f'{prefix}_challenges.json')
        spath = os.path.join(base, 'data', 'arc-prize-2025', f'{prefix}_solutions.json')
        if os.path.exists(cpath):
            with open(cpath) as f: challenges.update(json.load(f))
        if os.path.exists(spath):
            with open(spath) as f: solutions.update(json.load(f))
    
    if task_id in challenges:
        task = challenges[task_id]
        test_sols = solutions.get(task_id, [])
        test_cases = []
        for i in range(min(len(task['test']), len(test_sols))):
            raw_ex = task['test'][i]
            inp = raw_ex['input'] if (isinstance(raw_ex, dict) and 'input' in raw_ex) else raw_ex
            test_cases.append({'input': inp, 'output': test_sols[i]})
        
        data = {'train': task['train'], 'test': test_cases}
        dataset = {task_id: format_task(data)}
        fix_bugs(dataset)
        return dataset[task_id]

    raise KeyError(f"Task {task_id} not found in any data source.")


def validate(task_id, filepath):
    """Run verifier against all examples."""
    try:
        task = load_task(task_id)
    except Exception as e:
        return 0, 0, [f"Data Error: {e}"]

    # Use a unique module name per task_id to avoid collision
    module_name = f"verify_mod_{task_id}"
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    
    fn_name = f"verify_{task_id}"
    if not hasattr(mod, fn_name):
        return 0, 0, [f"Function {fn_name} not found in {filepath}"]
    
    fn = getattr(mod, fn_name)

    passed, total, errors = 0, 0, []
    
    for label in ['train', 'test']:
        for i, ex in enumerate(task[label]):
            total += 1
            try:
                if fn(ex['input']) == ex['output']:
                    passed += 1
                else:
                    errors.append(f"{label} {i} fail")
            except Exception as e:
                errors.append(f"{label} {i} error: {type(e).__name__}: {e}")
                
    return passed, total, errors


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <task_id> <verifier_file>")
        sys.exit(1)
    task_id, verifier_file = sys.argv[1], sys.argv[2]
    p, t, errs = validate(task_id, verifier_file)
    print(f"{'PASS' if p==t and t > 0 else 'FAIL'}: {p}/{t}")
    for e in errs: print(f"  {e}")
    sys.exit(0 if p==t and t > 0 else 1)
