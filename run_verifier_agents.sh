#!/bin/bash
# Run verifier agents for all pending tasks, 10 at a time.
# Each agent gets a self-contained prompt and writes to pending_verifiers/.
# No orchestration - just fire and forget.

set -euo pipefail
cd "$(dirname "$0")"

MAX_PARALLEL=10
LOG_DIR="pending_verifiers/logs"
mkdir -p "$LOG_DIR"

# Get pending task IDs
PENDING=$(python3 -c "
import json
with open('data/verifier_progress.json') as f:
    p = json.load(f)
for k in sorted(k for k,v in p.items() if v == 'pending'):
    print(k)
")

TOTAL=$(echo "$PENDING" | wc -l)
echo "Starting $TOTAL verifier agents (max $MAX_PARALLEL parallel)..."

run_agent() {
    local tid="$1"
    local logfile="$LOG_DIR/${tid}.log"

    # Skip if verifier already exists and passes
    if [ -f "pending_verifiers/verify_${tid}.py" ]; then
        result=$(python3 validate_verifier.py "$tid" "pending_verifiers/verify_${tid}.py" 2>&1 || true)
        if echo "$result" | grep -q "^PASS"; then
            echo "[SKIP] $tid - already passing"
            return 0
        fi
    fi

    # Save summary if not already saved
    python3 -c "
import json, os, sys
sys.path.insert(0, '.')
try:
    from get_summary import get_summaries
    outf = 'pending_verifiers/summaries_${tid}.json'
    if not os.path.exists(outf):
        try:
            df = get_summaries('${tid}')
            records = [{'summary_name': r['summary_name'], 'model_name': r['model_name'],
                         'reasoning_level': r['reasoning_level'], 'completion': r['completion']}
                        for _, r in df.iterrows()]
            with open(outf, 'w') as f: json.dump(records, f, indent=2)
        except KeyError:
            with open(outf, 'w') as f: json.dump([], f)
except Exception:
    pass
" 2>/dev/null

    PROMPT="You are writing a verifier function for ARC-AGI task ${tid}.

STEPS:
1. Read the task data: First try reading from data/arc-prize-2025/arc-agi_training_challenges.json for key '${tid}'. If not found, try arc-agi_evaluation_challenges.json. Also read corresponding solutions file.
2. Read summaries: cat pending_verifiers/summaries_${tid}.json
3. Read DSL reference: cat dsl_reference.txt
4. Read verifiers.py lines 1-85 for coding style: head -85 verifiers.py

RULES for the verifier function:
- Function signature: def verify_${tid}(I: Grid) -> Grid:
- Use ONLY functions from dsl.py (see dsl_reference.txt)
- Variable names: x0, x1, x2, ... (short, no descriptive names)
- Use while loops instead of for loops
- Use tuples and frozensets only (no lists, no dicts)
- Helper functions must be prefixed with _${tid}_
- File must start with: from dsl import *
- Write the verifier to: pending_verifiers/verify_${tid}.py
- Test with: python3 validate_verifier.py ${tid} pending_verifiers/verify_${tid}.py
- Debug until you get PASS (max 10 attempts)"

    echo "[START] $tid"
    claude -p "$PROMPT" --allowedTools "Bash,Read,Write,Edit,Grep" --permission-mode "bypassPermissions" > "$logfile" 2>&1

    # Check result
    if [ -f "pending_verifiers/verify_${tid}.py" ]; then
        result=$(python3 validate_verifier.py "$tid" "pending_verifiers/verify_${tid}.py" 2>&1 || true)
        if echo "$result" | grep -q "^PASS"; then
            echo "[PASS] $tid"
        else
            echo "[FAIL] $tid - $result"
        fi
    else
        echo "[NONE] $tid - no verifier file produced"
    fi
}

export -f run_agent
export LOG_DIR

echo "$PENDING" | xargs -P "$MAX_PARALLEL" -I {} bash -c 'run_agent "$@"' _ {}

echo "All agents complete. Run merge_verifiers.py to merge passing results."
