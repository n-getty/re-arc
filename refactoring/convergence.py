import subprocess
import sys
import os

from refactoring.core import process_verifiers
from refactoring.transformers import (
    IterationTransformer,
    LambdaFoldTransformer,
    BboxTransformer,
    TupleUnpackTransformer,
    ForFoldTransformer,
    BFSTransformer,
)
from refactoring.apply import main as apply_refactors


# Ordered pipeline of transformer passes. Each pass runs one set of transformers,
# applies results, then nests helpers before the next pass.
PIPELINE = [
    ('iteration', [IterationTransformer]),
    ('lambda_fold', [LambdaFoldTransformer]),
    ('bbox+unpack', [BboxTransformer, TupleUnpackTransformer]),
    ('for_fold', [ForFoldTransformer]),
    ('bfs', [BFSTransformer]),
]


def run_pipeline(verifier_file='verifiers.py', output_dir='pending_refactors', passes=2):
    """Run all refactoring passes in sequence, applying after each. Repeat for convergence."""
    os.makedirs(output_dir, exist_ok=True)

    for pass_num in range(passes):
        print(f"\n{'='*60}")
        print(f"  Pass {pass_num + 1}/{passes}")
        print(f"{'='*60}")

        for name, transformers in PIPELINE:
            print(f"\n--- {name} ---")
            process_verifiers(verifier_file, output_dir, transformers)

            print("Applying changes...")
            apply_refactors(verifier_file, output_dir)

            print("Nesting helpers...")
            subprocess.run([sys.executable, 'nest_helpers.py'])

    print("\nFinal Validation...")
    subprocess.run([sys.executable, 'validate_all.py'])


if __name__ == '__main__':
    run_pipeline()
