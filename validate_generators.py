#!/usr/bin/env python3
"""Validate generated generator functions against their verifiers.

For each generated generator, checks:
  - Syntax: does the code parse?
  - Runnable: does it execute without exceptions?
  - Valid grids: are input/output valid grids?
  - Correct: does verifier(input) == output?
  - Non-degenerate: is input != output?

Runs each generator N times and reports success rates.
"""

import argparse
import json
import signal
import sys
import traceback
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeout
from functools import partial

from dsl import *
from utils import *
import verifiers


def get_verifier_map():
    prefix = "verify_"
    return {
        name[len(prefix):]: getattr(verifiers, name)
        for name in dir(verifiers)
        if name.startswith(prefix)
    }


def compile_generator(task_id, code):
    """Try to compile generator code. Returns (func, error)."""
    try:
        compiled = compile(code, f"<generate_{task_id}>", "exec")
    except SyntaxError as e:
        return None, f"SyntaxError: {e}"

    namespace = {}
    # Inject DSL, utils, random, and the verifier into the namespace
    import dsl
    import utils
    import verifiers as _verifiers_module
    from random import choice, randint, sample, shuffle, uniform
    namespace.update({k: getattr(dsl, k) for k in dir(dsl) if not k.startswith("_")})
    namespace.update({k: getattr(utils, k) for k in dir(utils) if not k.startswith("_")})
    namespace.update({
        "choice": choice, "randint": randint, "sample": sample,
        "shuffle": shuffle, "uniform": uniform,
    })
    # Make verify_<task_id> callable from within the generator
    verify_name = f"verify_{task_id}"
    if hasattr(_verifiers_module, verify_name):
        namespace[verify_name] = getattr(_verifiers_module, verify_name)

    try:
        exec(compiled, namespace)
    except Exception as e:
        return None, f"ExecError: {e}"

    func_name = f"generate_{task_id}"
    if func_name not in namespace:
        return None, f"Function '{func_name}' not defined in generated code"
    return namespace[func_name], None


def _run_single_task(task_id, code, n_trials, timeout_per_trial):
    """Run validation for a single task (designed to run in a subprocess)."""
    result = {
        "task_id": task_id,
        "syntax_ok": False,
        "compile_error": None,
        "n_trials": n_trials,
        "n_run": 0,
        "n_valid_grid": 0,
        "n_correct": 0,
        "n_nondegenerate": 0,
        "n_passed": 0,  # all checks passed
        "errors": defaultdict(int),
    }

    # Compile
    func, err = compile_generator(task_id, code)
    if err:
        result["compile_error"] = err
        result["errors"][err] += 1
        return result
    result["syntax_ok"] = True

    # Get verifier
    verifier_map = get_verifier_map()
    verifier = verifier_map.get(task_id)
    if verifier is None:
        result["compile_error"] = f"No verifier found for {task_id}"
        return result

    # Set a per-trial alarm handler
    def timeout_handler(signum, frame):
        raise TimeoutError("Trial timed out")

    for trial in range(n_trials):
        try:
            # Set timeout
            old_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(timeout_per_trial)

            example = func(0.0, 1.0)
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

            result["n_run"] += 1

            # Valid grid check
            if not is_grid(example["input"]) or not is_grid(example["output"]):
                result["errors"]["InvalidGrid"] += 1
                continue
            result["n_valid_grid"] += 1

            # Correctness check
            verifier_output = verifier(example["input"])
            if verifier_output != example["output"]:
                result["errors"]["VerifierMismatch"] += 1
                continue
            result["n_correct"] += 1

            # Non-degeneracy check
            if example["input"] == example["output"]:
                result["errors"]["Degenerate"] += 1
                continue
            result["n_nondegenerate"] += 1
            result["n_passed"] += 1

        except TimeoutError:
            signal.alarm(0)
            result["errors"]["Timeout"] += 1
        except Exception as e:
            signal.alarm(0)
            err_type = type(e).__name__
            result["errors"][f"Runtime:{err_type}"] += 1

    result["errors"] = dict(result["errors"])
    return result


def run_validation(generated, n_trials, timeout_per_trial, max_workers):
    """Validate all generated generators."""
    results = []
    task_ids = sorted(generated.keys())

    # Use processes to isolate failures and enable timeouts
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for task_id in task_ids:
            f = executor.submit(
                _run_single_task, task_id, generated[task_id],
                n_trials, timeout_per_trial
            )
            futures[f] = task_id

        from tqdm import tqdm
        for f in tqdm(
            futures, total=len(futures), desc="Validating generators"
        ):
            try:
                result = f.result(timeout=n_trials * timeout_per_trial + 30)
            except Exception as e:
                task_id = futures[f]
                result = {
                    "task_id": task_id,
                    "syntax_ok": False,
                    "compile_error": f"ProcessError: {e}",
                    "n_trials": n_trials,
                    "n_run": 0,
                    "n_valid_grid": 0,
                    "n_correct": 0,
                    "n_nondegenerate": 0,
                    "n_passed": 0,
                    "errors": {"ProcessError": 1},
                }
            results.append(result)

    return results


def print_report(results, n_trials):
    """Print a summary report of validation results."""
    total = len(results)
    syntax_ok = sum(1 for r in results if r["syntax_ok"])
    any_run = sum(1 for r in results if r["n_run"] > 0)
    any_correct = sum(1 for r in results if r["n_correct"] > 0)
    all_passed = sum(1 for r in results if r["n_passed"] == n_trials)
    high_pass = sum(1 for r in results if r["n_passed"] >= n_trials * 0.8)

    print()
    print("=" * 60)
    print("VALIDATION REPORT")
    print("=" * 60)
    print(f"Total generators:       {total}")
    print(f"Trials per generator:   {n_trials}")
    print()
    print(f"Syntax OK:              {syntax_ok:>4} / {total}  ({100*syntax_ok/total:.1f}%)")
    print(f"At least 1 run:         {any_run:>4} / {total}  ({100*any_run/total:.1f}%)")
    print(f"At least 1 correct:     {any_correct:>4} / {total}  ({100*any_correct/total:.1f}%)")
    print(f">=80% pass rate:        {high_pass:>4} / {total}  ({100*high_pass/total:.1f}%)")
    print(f"100% pass rate:         {all_passed:>4} / {total}  ({100*all_passed/total:.1f}%)")

    # Pass rate distribution
    print()
    print("Pass rate distribution:")
    buckets = {"0%": 0, "1-49%": 0, "50-79%": 0, "80-99%": 0, "100%": 0}
    for r in results:
        rate = r["n_passed"] / n_trials if n_trials > 0 else 0
        if rate == 0:
            buckets["0%"] += 1
        elif rate < 0.5:
            buckets["1-49%"] += 1
        elif rate < 0.8:
            buckets["50-79%"] += 1
        elif rate < 1.0:
            buckets["80-99%"] += 1
        else:
            buckets["100%"] += 1
    for label, count in buckets.items():
        bar = "#" * (count * 40 // max(total, 1))
        print(f"  {label:>7}: {count:>4}  {bar}")

    # Error breakdown
    print()
    print("Error breakdown:")
    error_totals = defaultdict(int)
    for r in results:
        for err, count in r.get("errors", {}).items():
            error_totals[err] += count
    for err, count in sorted(error_totals.items(), key=lambda x: -x[1]):
        print(f"  {err:<40} {count:>6}")

    # Worst failures (0% pass rate, show compile errors)
    failures = [r for r in results if r["n_passed"] == 0]
    if failures:
        print()
        print(f"Failed tasks ({len(failures)}):")
        for r in sorted(failures, key=lambda x: x["task_id"])[:20]:
            reason = r.get("compile_error") or next(iter(r.get("errors", {"?": 0})), "?")
            print(f"  {r['task_id']}: {reason}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")


def main():
    parser = argparse.ArgumentParser(
        description="Validate generated ARC generator functions"
    )
    parser.add_argument(
        "input", nargs="?", default="generated_generators.json",
        help="JSON file with generated generators (default: generated_generators.json)"
    )
    parser.add_argument(
        "--output", "-o", default="validation_results.json",
        help="Output JSON file for detailed results (default: validation_results.json)"
    )
    parser.add_argument(
        "--trials", "-n", type=int, default=20,
        help="Number of trials per generator (default: 20)"
    )
    parser.add_argument(
        "--timeout", type=int, default=10,
        help="Timeout in seconds per trial (default: 10)"
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=4,
        help="Number of parallel worker processes (default: 4)"
    )
    parser.add_argument(
        "--tasks", nargs="+", default=None,
        help="Validate only specific task IDs"
    )
    args = parser.parse_args()

    with open(args.input) as f:
        generated = json.load(f)
    print(f"Loaded {len(generated)} generated generators from {args.input}")

    if args.tasks:
        generated = {k: v for k, v in generated.items() if k in args.tasks}
        print(f"Filtered to {len(generated)} tasks")

    results = run_validation(generated, args.trials, args.timeout, args.workers)

    # Save detailed results
    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Detailed results saved to {args.output}")

    print_report(results, args.trials)


if __name__ == "__main__":
    main()
