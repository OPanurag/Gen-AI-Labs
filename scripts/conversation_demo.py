"""
Multi-turn conversation demo.

Usage (from project root, with .env and DB set up):
  PYTHONPATH=. python3 scripts/conversation_demo.py

Shows follow-up questions using ConversationPipeline, which keeps context
so the LLM can refine queries (e.g. "what about males?" after "distribution by gender").
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

from src import ConversationPipeline


def main() -> int:
    from scripts.gaming_csv_to_db import DEFAULT_DB_PATH, DEFAULT_CSV_PATH, DEFAULT_TABLE_NAME
    from scripts.gaming_csv_to_db import csv_to_sqlite

    if not DEFAULT_DB_PATH.exists():
        print("Creating DB from CSV (one-time)...")
        csv_to_sqlite(DEFAULT_CSV_PATH, DEFAULT_DB_PATH, DEFAULT_TABLE_NAME, if_exists="replace")

    cp = ConversationPipeline(db_path=DEFAULT_DB_PATH)

    questions = [
        "What is the addiction level distribution by gender?",
        "What about males specifically?",
        "Now sort by anxiety score instead.",
    ]
    for q in questions:
        print(f"\n>>> {q}")
        result = cp.ask(q)
        print(f"    Status: {result.status}")
        if result.sql:
            print(f"    SQL: {result.sql[:120]}...")
        print(f"    Answer: {result.answer[:300]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
