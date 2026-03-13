#!/usr/bin/env python3
"""Validate a verifier function against original ARC task examples."""

import json
import importlib.util
import sys
import os

def load_task(task_id):
    """Load challenge and solution data for a task."""
    base = os.path.dirname(os.path.abspath(__file__))
    challenges = {}
    solutions = {}
    for prefix in ['arc-agi_training', 'arc-agi_evaluation']:
        cpath = os.path.join(base, 'data', 'arc-prize-2025', f'{prefix}_challenges.json')
        spath = os.path.join(base, 'data', 'arc-prize-2025', f'{prefix}_solutions.json')
        if os.path.exists(cpath):
            with open(cpath) as f:
                challenges.update(json.load(f))
        if os.path.exists(spath):
            with open(spath) as f:
                solutions.update(json.load(f))
    if task_id not in challenges:
        raise KeyError(f"Task {task_id} not found in challenges")
    return challenges[task_id], solutions.get(task_id, [])


def load_verifier(task_id, filepath):
    """Dynamically load a verifier function from a file."""
    spec = importlib.util.spec_from_file_location(f"verify_{task_id}", filepath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn_name = f"verify_{task_id}"
    if not hasattr(mod, fn_name):
        raise AttributeError(f"Function {fn_name} not found in {filepath}")
    return getattr(mod, fn_name)


def validate(task_id, filepath):
    """Run verifier against all train + test examples. Returns (passed, total, errors)."""
    task, test_solutions = load_task(task_id)
    fn = load_verifier(task_id, filepath)

    passed = 0
    total = 0
    errors = []

    # Train examples
    for i, ex in enumerate(task['train']):
        inp = tuple(tuple(r) for r in ex['input'])
        expected = tuple(tuple(r) for r in ex['output'])
        total += 1
        try:
            result = fn(inp)
            if result == expected:
                passed += 1
            else:
                errors.append(f"Train {i}: output mismatch")
        except Exception as e:
            errors.append(f"Train {i}: {type(e).__name__}: {e}")

    # Test examples
    for i, ex in enumerate(task['test']):
        inp = tuple(tuple(r) for r in ex['input'])
        if i < len(test_solutions):
            expected = tuple(tuple(r) for r in test_solutions[i])
            total += 1
            try:
                result = fn(inp)
                if result == expected:
                    passed += 1
                else:
                    errors.append(f"Test {i}: output mismatch")
            except Exception as e:
                errors.append(f"Test {i}: {type(e).__name__}: {e}")

    return passed, total, errors


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <task_id> <verifier_file>")
        sys.exit(1)
    task_id, filepath = sys.argv[1], sys.argv[2]
    passed, total, errors = validate(task_id, filepath)
    status = "PASS" if passed == total else "FAIL"
    print(f"{status}: {passed}/{total}")
    for e in errors:
        print(f"  {e}")
    sys.exit(0 if passed == total else 1)
