"""Unit tests for SQL validation (no OPENROUTER_API_KEY required)."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=BACKEND_ROOT / ".env")

import unittest
from src.pipeline import SQLValidator


class TestSQLValidator(unittest.TestCase):
    def test_select_allowed(self) -> None:
        out = SQLValidator.validate("SELECT * FROM gaming_mental_health LIMIT 5")
        self.assertTrue(out.is_valid)
        self.assertIsNone(out.error)

    def test_delete_rejected(self) -> None:
        out = SQLValidator.validate("DELETE FROM gaming_mental_health")
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_drop_rejected(self) -> None:
        out = SQLValidator.validate("DROP TABLE gaming_mental_health")
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_update_rejected(self) -> None:
        out = SQLValidator.validate("UPDATE gaming_mental_health SET x = 1")
        self.assertFalse(out.is_valid)

    def test_insert_rejected(self) -> None:
        out = SQLValidator.validate("INSERT INTO t VALUES (1)")
        self.assertFalse(out.is_valid)

    def test_none_rejected(self) -> None:
        out = SQLValidator.validate(None)
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_empty_rejected(self) -> None:
        out = SQLValidator.validate("   ")
        self.assertFalse(out.is_valid)


if __name__ == "__main__":
    unittest.main()
