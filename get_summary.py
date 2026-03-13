#!/usr/bin/env python3
"""Retrieve NVARC puzzle summaries for a given ARC task ID."""

import pandas as pd

SUMMARIES_DIR = '../data/datasets/sorokin/nvarc-artifacts-puzzles/versions/1/summaries/'

_df_cache = None

def _load_summaries():
    global _df_cache
    if _df_cache is None:
        _df_cache = pd.read_parquet(SUMMARIES_DIR)
    return _df_cache


def get_summaries(task_id: str) -> pd.DataFrame:
    """Return all summary rows for a given task ID.

    Parameters
    ----------
    task_id : str
        8-character hex task ID (e.g. '00d62c1b').

    Returns
    -------
    pd.DataFrame with columns: summary_name, puzzle_name, model_name,
        reasoning_level, prompt, reasoning, completion.
    """
    df = _load_summaries()
    rows = df[df['puzzle_name'] == task_id]
    if rows.empty:
        raise KeyError(f"No summaries found for task '{task_id}'")
    return rows.reset_index(drop=True)


def print_summaries(task_id: str):
    """Print all summaries for a task to stdout."""
    rows = get_summaries(task_id)
    for i, row in rows.iterrows():
        header = f"[{row['summary_name']}] model={row['model_name']}  reasoning={row['reasoning_level']}"
        print(header)
        print('=' * len(header))
        print(row['completion'])
        print()


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <task_id> [task_id ...]")
        sys.exit(1)
    for tid in sys.argv[1:]:
        print_summaries(tid)
