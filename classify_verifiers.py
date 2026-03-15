"""
Classify new verifiers (those without generators) by generator difficulty.

Tiers:
  1 - "Generate-then-verify": Pure DSL transformation, any valid grid works.
      Generator = random grid + call verifier for output.
  2 - "Constrained construction": Needs structured input but the constraints
      are extractable from the verifier's DSL usage.
  3 - "Inverse problem": Complex logic (deep folds, helpers, imperative loops)
      that requires understanding the task semantics to build valid inputs.

Signals extracted per verifier:
  - Line count (body only)
  - DSL function usage (which ones, how many distinct)
  - Helper functions (nested defs)
  - fold/branch/mapply counts
  - Imperative constructs (while loops, for loops, if statements, indexing)
  - Whether it uses objects/partition (needs structured multi-object input)
  - Whether it uses color-specific constants (needs specific palette)
"""

import ast
import sys
import json
import re
from collections import defaultdict


def get_generator_task_ids(path="generators.py"):
    with open(path) as f:
        tree = ast.parse(f.read())
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("generate_"):
            ids.add(node.name[len("generate_"):])
    return ids


def get_verifier_functions(path="verifiers.py"):
    """Extract all verify_* functions as AST nodes with source lines."""
    with open(path) as f:
        source = f.read()
    tree = ast.parse(source)
    source_lines = source.split("\n")

    verifiers = {}
    # Get top-level function defs only
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("verify_"):
            task_id = node.name[len("verify_"):]
            verifiers[task_id] = node
    return verifiers, source_lines


# DSL functions that indicate the verifier needs structured input
OBJECT_DSL = {
    "objects", "partition", "colorfilter", "sizefilter", "replace",
    "underpaint", "paint", "underfill", "overfill", "recolor",
    "normalize", "dneighbors", "ineighbors", "neighbors",
    "frontiers", "compress", "hperiod", "vperiod",
    "outbox", "inbox", "box", "backdrop", "delta",
    "gravitate", "cover",
}

# DSL functions that are pure grid->grid transforms (any input works)
PURE_TRANSFORM_DSL = {
    "vmirror", "hmirror", "dmirror", "cmirror",
    "rot90", "rot180", "rot270",
    "hconcat", "vconcat",
    "tophalf", "bottomhalf", "lefthalf", "righthalf",
    "upscale", "downscale", "hupscale", "vupscale",
    "trim", "crop",
}

# Higher-order / iteration DSL
ITERATION_DSL = {
    "fold", "mapply", "apply", "rapply",
    "sfilter", "mfilter", "extract",
    "argmax", "argmin",
    "compose", "chain", "fork",
    "lbind", "rbind", "power", "repeat",
}


def analyze_verifier(func_node, source_lines):
    """Extract complexity signals from a verifier AST node."""
    info = {}

    # Body line count
    body_start = func_node.body[0].lineno
    body_end = func_node.end_lineno
    info["lines"] = body_end - body_start + 1

    # Collect all names referenced (DSL function calls)
    calls = set()
    call_counts = defaultdict(int)
    names_used = set()

    # Count nested defs (helper functions)
    helper_count = 0
    helper_lines = 0

    # Imperative construct counts
    while_count = 0
    for_count = 0
    if_count = 0
    subscript_count = 0  # I[r][c] style indexing
    list_comp_count = 0
    dict_count = 0  # dict literal or {} usage
    fold_count = 0
    branch_count = 0
    append_count = 0

    for node in ast.walk(func_node):
        # Function calls
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
                call_counts[node.func.id] += 1
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr == "append":
                    append_count += 1

        # Names
        if isinstance(node, ast.Name):
            names_used.add(node.id)

        # Nested function defs
        if isinstance(node, ast.FunctionDef) and node != func_node:
            helper_count += 1
            helper_lines += (node.end_lineno - node.lineno + 1)

        # Imperative constructs
        if isinstance(node, ast.While):
            while_count += 1
        if isinstance(node, ast.For):
            for_count += 1
        if isinstance(node, ast.If):
            if_count += 1
        if isinstance(node, ast.Subscript):
            subscript_count += 1
        if isinstance(node, ast.ListComp) or isinstance(node, ast.GeneratorExp):
            list_comp_count += 1
        if isinstance(node, ast.Dict):
            dict_count += 1

    fold_count = call_counts.get("fold", 0)
    branch_count = call_counts.get("branch", 0)

    info["dsl_calls"] = sorted(calls)
    info["num_distinct_dsl"] = len(calls)
    info["helper_count"] = helper_count
    info["helper_lines"] = helper_lines
    info["while_count"] = while_count
    info["for_count"] = for_count
    info["if_count"] = if_count
    info["subscript_count"] = subscript_count
    info["list_comp_count"] = list_comp_count
    info["dict_count"] = dict_count
    info["fold_count"] = fold_count
    info["branch_count"] = branch_count
    info["append_count"] = append_count

    # Categorize DSL usage
    info["uses_object_dsl"] = bool(calls & OBJECT_DSL)
    info["uses_pure_transform"] = bool(calls & PURE_TRANSFORM_DSL)
    info["uses_iteration_dsl"] = bool(calls & ITERATION_DSL)
    info["object_dsl_used"] = sorted(calls & OBJECT_DSL)
    info["pure_transform_used"] = sorted(calls & PURE_TRANSFORM_DSL)

    # Color constants used (hard-coded color expectations)
    color_consts = {"ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE",
                    "SIX", "SEVEN", "EIGHT", "NINE", "TEN"}
    info["color_constants"] = sorted(names_used & color_consts)
    info["num_color_constants"] = len(names_used & color_consts)

    # Imperative score: how much non-DSL imperative code is there
    info["imperative_score"] = (
        while_count * 3 +
        for_count * 2 +
        if_count * 1 +
        append_count * 1 +
        dict_count * 2 +
        list_comp_count * 1
    )

    return info


def classify(info):
    """
    Assign a tier based on extracted signals.

    Tier 1:  Simple transform — short, mostly pure transforms, no imperative code
    Tier 2:  Constrained — uses object/color DSL, moderate complexity
    Tier 3a: Moderate inverse — short folds/helpers, limited imperative
    Tier 3b: Hard inverse — medium-length, helpers + folds + moderate imperative
    Tier 3c: Very hard inverse — long, heavy imperative, multiple helpers
    """
    score = 0

    # Length penalty
    if info["lines"] > 50:
        score += 3
    elif info["lines"] > 25:
        score += 2
    elif info["lines"] > 12:
        score += 1

    # Helper functions are a strong complexity signal
    score += info["helper_count"] * 3

    # Folds indicate iteration that's hard to invert
    score += info["fold_count"] * 2

    # Imperative code (while/for/if/append/dict)
    if info["imperative_score"] >= 10:
        score += 3
    elif info["imperative_score"] >= 4:
        score += 2
    elif info["imperative_score"] >= 1:
        score += 1

    # Object DSL means structured input needed
    if info["uses_object_dsl"]:
        score += 1

    # Many branches = conditional logic
    if info["branch_count"] >= 3:
        score += 2
    elif info["branch_count"] >= 1:
        score += 1

    # Many distinct DSL calls = complex pipeline
    if info["num_distinct_dsl"] >= 20:
        score += 2
    elif info["num_distinct_dsl"] >= 12:
        score += 1

    # Classify
    if score <= 2:
        return "1"
    elif score <= 5:
        return "2"
    else:
        # Sub-tier Tier 3
        if info["lines"] <= 30 and info["imperative_score"] <= 4:
            return "3a"
        elif info["lines"] <= 60 and info["imperative_score"] <= 15:
            return "3b"
        else:
            return "3c"


TIER_LABELS = {
    "1":  "Tier 1  — Generate-then-verify (template-able)",
    "2":  "Tier 2  — Constrained construction (semi-automated)",
    "3a": "Tier 3a — Moderate inverse (short folds/helpers)",
    "3b": "Tier 3b — Hard inverse (medium complexity)",
    "3c": "Tier 3c — Very hard inverse (long, heavy imperative)",
}

TIER_COST_PER_TASK = {
    "1":  50_000,     # near-zero if templated
    "2":  200_000,
    "3a": 500_000,
    "3b": 1_000_000,
    "3c": 1_500_000,
}


def format_task_line(task_id, r):
    flags = []
    if r.get("helper_count"):
        flags.append(f"helpers={r['helper_count']}")
    if r.get("fold_count"):
        flags.append(f"folds={r['fold_count']}")
    if r.get("imperative_score"):
        flags.append(f"imp={r['imperative_score']}")
    if r.get("uses_pure_transform"):
        flags.append("pure_xform")
    if r.get("uses_object_dsl"):
        flags.append("objects")
    if r.get("branch_count"):
        flags.append(f"branch={r['branch_count']}")
    flag_str = f"  [{', '.join(flags)}]" if flags else ""
    return (f"  {task_id}  {r['lines']:3d} lines  "
            f"{r['num_distinct_dsl']:2d} DSL fns{flag_str}")


def main():
    gen_ids = get_generator_task_ids()
    verifiers, source_lines = get_verifier_functions()

    new_ids = sorted(set(verifiers.keys()) - gen_ids)
    existing_ids = sorted(set(verifiers.keys()) & gen_ids)

    print(f"Total verifiers: {len(verifiers)}")
    print(f"Existing generators: {len(gen_ids)}")
    print(f"New verifiers (no generator): {len(new_ids)}")
    print()

    # Analyze and classify
    results = {}
    tier_counts = defaultdict(int)
    tier_tasks = defaultdict(list)

    for task_id in new_ids:
        info = analyze_verifier(verifiers[task_id], source_lines)
        tier = classify(info)
        info["tier"] = tier
        results[task_id] = info
        tier_counts[tier] += 1
        tier_tasks[tier].append(task_id)

    # Also classify existing verifiers for comparison
    existing_tiers = defaultdict(int)
    for task_id in existing_ids:
        info = analyze_verifier(verifiers[task_id], source_lines)
        existing_tiers[classify(info)] += 1

    # Summary table
    print("=" * 70)
    print("CLASSIFICATION SUMMARY")
    print("=" * 70)
    print(f"{'Tier':<42} {'New':>5} {'%':>6}  {'Existing':>8}")
    print("-" * 70)
    all_tiers = ["1", "2", "3a", "3b", "3c"]
    for tier in all_tiers:
        n = tier_counts[tier]
        e = existing_tiers[tier]
        pct = 100 * n / len(new_ids) if new_ids else 0
        label = TIER_LABELS[tier]
        print(f"  {label:<40} {n:>5} {pct:>5.1f}%  {e:>8}")
    t3_total = tier_counts["3a"] + tier_counts["3b"] + tier_counts["3c"]
    t3e_total = existing_tiers["3a"] + existing_tiers["3b"] + existing_tiers["3c"]
    print(f"  {'(Tier 3 combined)':<40} {t3_total:>5} {100*t3_total/len(new_ids):>5.1f}%  {t3e_total:>8}")
    print()

    # Stats per tier
    for tier in all_tiers:
        tasks = tier_tasks[tier]
        if not tasks:
            continue
        tier_results = [results[t] for t in tasks]
        avg_lines = sum(r["lines"] for r in tier_results) / len(tier_results)
        avg_helpers = sum(r["helper_count"] for r in tier_results) / len(tier_results)
        avg_folds = sum(r["fold_count"] for r in tier_results) / len(tier_results)
        avg_imp = sum(r["imperative_score"] for r in tier_results) / len(tier_results)
        has_obj = sum(1 for r in tier_results if r["uses_object_dsl"])

        print(f"--- {TIER_LABELS[tier]} ({len(tasks)} tasks) ---")
        print(f"  Avg lines: {avg_lines:.0f}  |  helpers: {avg_helpers:.1f}  "
              f"|  folds: {avg_folds:.1f}  |  imperative: {avg_imp:.1f}  "
              f"|  objects: {has_obj}/{len(tasks)}")

    # Cost estimation
    print()
    print("=" * 70)
    print("ESTIMATED TOKEN COST (agentic approach)")
    print("=" * 70)
    total = 0
    for tier in all_tiers:
        n = tier_counts[tier]
        cost = n * TIER_COST_PER_TASK[tier]
        total += cost
        print(f"  {TIER_LABELS[tier]:<42} {n:>4} x {TIER_COST_PER_TASK[tier]/1e6:.1f}M = "
              f"{cost/1e6:>6.0f}M tokens")
    print(f"  {'TOTAL':<42} {'':>4}   {'':>4}   {total/1e6:>6.0f}M tokens")
    template_savings = tier_counts["1"] * TIER_COST_PER_TASK["1"]
    print(f"\n  If Tier 1 templated (saves {template_savings/1e6:.0f}M): "
          f"{(total - template_savings)/1e6:.0f}M tokens total")

    # Task listings per tier
    for tier in all_tiers:
        tasks = tier_tasks[tier]
        if not tasks:
            continue
        print()
        print("=" * 70)
        print(TIER_LABELS[tier].upper())
        print("=" * 70)
        for task_id in sorted(tasks, key=lambda t: results[t]["lines"]):
            print(format_task_line(task_id, results[task_id]))

    # Save JSON
    output = {
        "summary": {
            "total_verifiers": len(verifiers),
            "existing_generators": len(gen_ids),
            "new_verifiers": len(new_ids),
            "tier_counts": dict(tier_counts),
            "existing_tier_counts": dict(existing_tiers),
        },
        "tasks": {}
    }
    for task_id in new_ids:
        r = results[task_id]
        output["tasks"][task_id] = {
            "tier": r["tier"],
            "lines": r["lines"],
            "num_distinct_dsl": r["num_distinct_dsl"],
            "helper_count": r["helper_count"],
            "fold_count": r["fold_count"],
            "branch_count": r["branch_count"],
            "imperative_score": r["imperative_score"],
            "while_count": r["while_count"],
            "uses_object_dsl": r["uses_object_dsl"],
            "uses_pure_transform": r["uses_pure_transform"],
            "object_dsl_used": r["object_dsl_used"],
            "pure_transform_used": r["pure_transform_used"],
            "color_constants": r["color_constants"],
        }
    with open("verifier_classification.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nDetailed results saved to verifier_classification.json")


if __name__ == "__main__":
    main()
