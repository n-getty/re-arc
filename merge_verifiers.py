#!/usr/bin/env python3
"""Validate and merge all passing verifiers from pending_verifiers/ into verifiers.py."""

import re
import json
import os
import sys


def main():
    with open('data/verifier_progress.json') as f:
        progress = json.load(f)

    pending = sorted(k for k, v in progress.items() if v == 'pending')
    print(f"Checking {len(pending)} pending tasks...")

    passed = []
    failed = []
    missing = []

    for tid in pending:
        fpath = f'pending_verifiers/verify_{tid}.py'
        if not os.path.exists(fpath):
            missing.append(tid)
            continue

        # Validate
        from validate_verifier import validate
        try:
            p, t, errors = validate(tid, fpath)
            if p == t:
                passed.append(tid)
            else:
                failed.append((tid, f"{p}/{t}", errors))
        except Exception as e:
            failed.append((tid, "error", [str(e)]))

    print(f"\nResults: {len(passed)} pass, {len(failed)} fail, {len(missing)} missing")

    if not passed:
        print("Nothing to merge.")
        return

    print(f"\nMerging {len(passed)} passing verifiers...")

    with open('verifiers.py') as f:
        content = f.read()

    merged = 0
    for tid in passed:
        # Check if already merged
        if f'def verify_{tid}' in content:
            print(f"  {tid}: already in verifiers.py, skipping")
            progress[tid] = 'done'
            continue

        fpath = f'pending_verifiers/verify_{tid}.py'
        with open(fpath) as f:
            vcode = f.read()

        lines = vcode.split('\n')
        func_lines = []
        started = False
        for line in lines:
            if line.startswith('from dsl import'):
                continue
            if not started and line.strip() == '':
                continue
            started = True
            func_lines.append(line)
        func_code = '\n'.join(func_lines).rstrip()

        if not func_code:
            print(f"  {tid}: empty function code, skipping")
            continue

        # Find alphabetical insertion point
        matches = list(re.finditer(r'^def verify_([a-f0-9]{8})', content, re.MULTILINE))
        insert_pos = None
        for m in matches:
            if tid < m.group(1):
                insert_pos = m.start()
                break

        if insert_pos is None:
            content = content.rstrip() + '\n\n\n' + func_code + '\n'
        else:
            content = content[:insert_pos] + func_code + '\n\n\n' + content[insert_pos:]

        progress[tid] = 'done'
        merged += 1

    with open('verifiers.py', 'w') as f:
        f.write(content)

    with open('data/verifier_progress.json', 'w') as f:
        json.dump(progress, f, indent=2)

    done = sum(1 for v in progress.values() if v == 'done')
    print(f"\nMerged {merged} verifiers. Progress: {done}/609")

    if failed:
        print(f"\nFailed ({len(failed)}):")
        for tid, status, errors in failed:
            print(f"  {tid}: {status}")
            for e in errors[:2]:
                print(f"    {e}")

    if missing:
        print(f"\nMissing ({len(missing)}): no verifier file produced")


if __name__ == '__main__':
    main()
