from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env from project root (single source for OPENROUTER_*, etc.)
from dotenv import load_dotenv
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

from src.pipeline import AnalyticsPipeline
from scripts.gaming_csv_to_db import csv_to_sqlite
from scripts.gaming_csv_to_db import DEFAULT_CSV_PATH, DEFAULT_DB_PATH, DEFAULT_TABLE_NAME


def _ensure_gaming_db() -> Path:
    """Ensure gaming mental health DB exists and is valid; create from CSV if missing or invalid."""
    import sqlite3
    need_rebuild = not DEFAULT_DB_PATH.exists()
    if not need_rebuild:
        try:
            with sqlite3.connect(DEFAULT_DB_PATH) as conn:
                conn.execute("SELECT 1")
        except sqlite3.OperationalError as e:
            if "not a database" in str(e).lower() or "corrupt" in str(e).lower():
                need_rebuild = True
                DEFAULT_DB_PATH.unlink(missing_ok=True)
            else:
                raise
    if need_rebuild:
        if not DEFAULT_CSV_PATH.exists():
            raise FileNotFoundError(
                f"Database missing or invalid and CSV not found at {DEFAULT_CSV_PATH}. "
                "Download the dataset from Kaggle and place it in data/."
            )
        csv_to_sqlite(DEFAULT_CSV_PATH, DEFAULT_DB_PATH, DEFAULT_TABLE_NAME, if_exists="replace")
    return DEFAULT_DB_PATH


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3, help="Number of full prompt-set repetitions.")
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-prompt status and first few failure reasons.",
    )
    args = parser.parse_args()

    db_path = _ensure_gaming_db()
    root = Path(__file__).resolve().parents[1]
    prompts_path = root / "tests" / "public_prompts.json"

    pipeline = AnalyticsPipeline(db_path=db_path)
    prompts = json.loads(prompts_path.read_text(encoding="utf-8"))

    totals: list[float] = []
    token_totals: list[int] = []
    llm_calls_list: list[int] = []
    success = 0
    count = 0
    verbose_errors: list[tuple[str, str, str | None, str | None]] = []  # prompt, status, val_err, exec_err

    for run in range(args.runs):
        for prompt in prompts:
            result = pipeline.run(prompt)
            totals.append(result.timings["total_ms"])
            token_totals.append(result.total_llm_stats.get("total_tokens", 0) or 0)
            llm_calls_list.append(result.total_llm_stats.get("llm_calls", 0) or 0)
            success += int(result.status == "success")
            count += 1
            if args.verbose and result.status != "success":
                v_err = result.sql_validation.error if not result.sql_validation.is_valid else None
                e_err = result.sql_execution.error
                verbose_errors.append((prompt[:70], result.status, v_err, e_err))

    summary = {
        "runs": args.runs,
        "samples": count,
        "success_rate": round(success / count, 4) if count else 0.0,
        "avg_ms": round(statistics.fmean(totals), 2) if totals else 0.0,
        "p50_ms": round(percentile(totals, 50), 2),
        "p95_ms": round(percentile(totals, 95), 2),
        "avg_tokens_per_request": round(statistics.fmean(token_totals), 2) if token_totals else 0,
        "avg_llm_calls_per_request": round(statistics.fmean(llm_calls_list), 2) if llm_calls_list else 0,
    }
    print(json.dumps(summary, indent=2))

    if args.verbose and verbose_errors:
        print("\n--- Failure breakdown (first 15) ---")
        for i, (prompt, status, v_err, e_err) in enumerate(verbose_errors[:15]):
            print(f"\n{i + 1}. [{status}] {prompt!r}")
            if v_err:
                print(f"   validation: {v_err[:200]}")
            if e_err:
                print(f"   execution: {e_err[:200]}")


if __name__ == "__main__":
    main()
