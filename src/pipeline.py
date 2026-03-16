from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

from src.llm_client import OpenRouterLLMClient, build_default_llm_client
from src.types import (
    ConversationContext,
    PipelineOutput,
    SQLExecutionOutput,
    SQLValidationOutput,
)


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "gaming_mental_health.sqlite"

logger = logging.getLogger(__name__)

# Only SELECT is allowed for this analytics pipeline (read-only).
_DANGEROUS_KEYWORDS = (
    "DELETE", "DROP", "UPDATE", "INSERT", "ALTER", "TRUNCATE",
    "CREATE", "REPLACE", "GRANT", "REVOKE", "EXEC", "EXECUTE",
)

# Question phrases that request destructive operations (reject even if LLM returns SELECT).
_DESTRUCTIVE_INTENT_PATTERN = re.compile(
    r"\b(delete|drop|remove|truncate|clear)\s+(all\s+)?(rows?|data|table)?",
    re.IGNORECASE,
)


def _question_requests_destructive(question: str) -> bool:
    """True if the question clearly asks for a destructive operation (e.g. delete all rows)."""
    return bool(_DESTRUCTIVE_INTENT_PATTERN.search(question.strip()))


class SQLValidationError(Exception):
    pass


def _normalize_sql_for_validation(sql: str) -> str:
    """Strip comments and normalize whitespace for validation."""
    sql = sql.strip()
    # Remove single-line comments (-- ...)
    sql = re.sub(r"--[^\n]*", " ", sql)
    # Remove multi-line comments (/* ... */)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # Collapse whitespace and strip
    sql = " ".join(sql.split())
    return sql.upper()


class SQLValidator:
    @classmethod
    def validate(cls, sql: str | None) -> SQLValidationOutput:
        start = time.perf_counter()

        if sql is None or not sql.strip():
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error="No SQL provided",
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        normalized = _normalize_sql_for_validation(sql)
        if not normalized:
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error="Empty SQL after removing comments",
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        # Must start with SELECT (read-only analytics only)
        if not normalized.startswith("SELECT"):
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error="Only SELECT queries are allowed",
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        # Reject dangerous keywords (as whole words to avoid false positives in strings)
        for keyword in _DANGEROUS_KEYWORDS:
            pattern = r"\b" + re.escape(keyword) + r"\b"
            if re.search(pattern, normalized):
                return SQLValidationOutput(
                    is_valid=False,
                    validated_sql=None,
                    error=f"Query contains disallowed keyword: {keyword}",
                    timing_ms=(time.perf_counter() - start) * 1000,
                )

        return SQLValidationOutput(
            is_valid=True,
            validated_sql=sql.strip(),
            error=None,
            timing_ms=(time.perf_counter() - start) * 1000,
        )


def _strip_incomplete_sql_trailer(sql: str) -> str:
    """Remove trailing incomplete clauses to avoid SQLite 'incomplete input'."""
    sql = sql.strip()
    # Fix unclosed single quote (SQLite treats as incomplete)
    if sql.count("'") % 2 == 1:
        last_quote = sql.rfind("'")
        if last_quote > 0:
            sql = sql[:last_quote].strip()
    incomplete_suffixes = (
        r"\s+ORDER\s+BY\s*$",
        r"\s+GROUP\s+BY\s*$",
        r"\s+HAVING\s*$",
        r"\s+WHERE\s*$",
        r"\s+AND\s*$",
        r"\s+OR\s*$",
        r"\s+LIMIT\s*$",
        r"\s+OFFSET\s*$",
        r"\s*,\s*$",
        r"\s+\(\s*$",
    )
    prev = ""
    while prev != sql:
        prev = sql
        sql = sql.strip()
        for pat in incomplete_suffixes:
            sql = re.sub(pat, "", sql, flags=re.IGNORECASE).strip()
    return sql


def _execute_sql_with_incomplete_retry(conn: sqlite3.Connection, sql: str, max_retries: int = 5) -> tuple[list[dict], str | None]:
    """Execute SQL; on 'incomplete input', strip trailing tokens and retry. Returns (rows, error)."""
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    for _ in range(max_retries + 1):
        try:
            cur.execute(sql)
            rows = [dict(r) for r in cur.fetchmany(100)]
            return (rows, None)
        except sqlite3.OperationalError as e:
            err = str(e).lower()
            if "incomplete input" in err or "unclosed" in err:
                # Strip last token and retry
                parts = sql.strip().rsplit(maxsplit=1)
                if len(parts) < 2:
                    return ([], str(e))
                sql = parts[0].strip()
                if not sql.upper().startswith("SELECT"):
                    return ([], str(e))
            else:
                return ([], str(e))
    return ([], "incomplete input after retries")


class SQLiteExecutor:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def run(self, sql: str | None) -> SQLExecutionOutput:
        start = time.perf_counter()
        error = None
        rows = []
        row_count = 0

        if sql is None:
            return SQLExecutionOutput(
                rows=[],
                row_count=0,
                timing_ms=(time.perf_counter() - start) * 1000,
                error=None,
            )

        sql = _strip_incomplete_sql_trailer(sql)

        try:
            with sqlite3.connect(self.db_path) as conn:
                rows, exec_error = _execute_sql_with_incomplete_retry(conn, sql)
                if exec_error:
                    error = exec_error
                    rows = []
                row_count = len(rows)
        except Exception as exc:
            error = str(exc)
            rows = []
            row_count = 0

        return SQLExecutionOutput(
            rows=rows,
            row_count=row_count,
            timing_ms=(time.perf_counter() - start) * 1000,
            error=error,
        )


class AnalyticsPipeline:
    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        llm_client: OpenRouterLLMClient | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.llm = llm_client or build_default_llm_client()
        self.executor = SQLiteExecutor(self.db_path)

    def run(
        self,
        question: str,
        request_id: str | None = None,
        conversation_context: ConversationContext | None = None,
    ) -> PipelineOutput:
        start = time.perf_counter()
        logger.info("Pipeline run started question=%s request_id=%s", question[:80], request_id)

        # Stage 1: SQL Generation (with optional conversation context for follow-ups)
        sql_gen_context = (
            {"conversation": conversation_context} if conversation_context else {}
        )
        sql_gen_output = self.llm.generate_sql(question, sql_gen_context)
        sql = sql_gen_output.sql

        # Stage 2: SQL Validation
        validation_output = SQLValidator.validate(sql)
        if not validation_output.is_valid:
            sql = None

        # Stage 3: SQL Execution
        execution_output = self.executor.run(sql)
        rows = execution_output.rows

        # Stage 4: Answer Generation (with optional context for explanation follow-ups)
        answer_output = self.llm.generate_answer(
            question, sql, rows, conversation_context=conversation_context
        )

        # Determine status
        status = "success"
        if _question_requests_destructive(question):
            status = "invalid_sql"
            if validation_output.is_valid:
                validation_output = SQLValidationOutput(
                    is_valid=False,
                    validated_sql=None,
                    error="Disallowed operation requested. Only SELECT queries are allowed.",
                    timing_ms=validation_output.timing_ms,
                )
        elif sql_gen_output.sql is None and sql_gen_output.error:
            status = "unanswerable"
        elif not validation_output.is_valid:
            status = "invalid_sql"
        elif execution_output.error:
            err_lower = (execution_output.error or "").lower()
            if "no such column" in err_lower or "no such table" in err_lower:
                status = "unanswerable"
            else:
                status = "error"
        elif sql is None:
            status = "unanswerable"

        final_answer = answer_output.answer
        if status == "unanswerable" and execution_output.error:
            err_lower = (execution_output.error or "").lower()
            if "no such column" in err_lower or "no such table" in err_lower:
                final_answer = "I cannot answer this with the available table and schema. Please rephrase using known survey fields."

        total_ms = (time.perf_counter() - start) * 1000
        logger.info("Pipeline completed status=%s request_id=%s total_ms=%.2f", status, request_id, total_ms)

        # Build timings aggregate
        timings = {
            "sql_generation_ms": sql_gen_output.timing_ms,
            "sql_validation_ms": validation_output.timing_ms,
            "sql_execution_ms": execution_output.timing_ms,
            "answer_generation_ms": answer_output.timing_ms,
            "total_ms": (time.perf_counter() - start) * 1000,
        }

        # Build total LLM stats (ints required by evaluation contract)
        total_llm_stats = {
            "llm_calls": int(sql_gen_output.llm_stats.get("llm_calls", 0) or 0) + int(answer_output.llm_stats.get("llm_calls", 0) or 0),
            "prompt_tokens": int(sql_gen_output.llm_stats.get("prompt_tokens", 0) or 0) + int(answer_output.llm_stats.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(sql_gen_output.llm_stats.get("completion_tokens", 0) or 0) + int(answer_output.llm_stats.get("completion_tokens", 0) or 0),
            "total_tokens": int(sql_gen_output.llm_stats.get("total_tokens", 0) or 0) + int(answer_output.llm_stats.get("total_tokens", 0) or 0),
            "model": sql_gen_output.llm_stats.get("model") or answer_output.llm_stats.get("model", "unknown"),
        }

        return PipelineOutput(
            status=status,
            question=question,
            request_id=request_id,
            sql_generation=sql_gen_output,
            sql_validation=validation_output,
            sql_execution=execution_output,
            answer_generation=answer_output,
            sql=sql,
            rows=rows,
            answer=final_answer,
            timings=timings,
            total_llm_stats=total_llm_stats,
        )


class ConversationPipeline:
    """
    Wrapper that maintains conversation context for multi-turn follow-up questions.
    Use ask(question) for each turn; context is updated automatically after each response.
    """

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        llm_client: OpenRouterLLMClient | None = None,
        max_turns: int = 5,
    ) -> None:
        self._pipeline = AnalyticsPipeline(db_path=db_path, llm_client=llm_client)
        self._context = ConversationContext(max_turns=max_turns)

    def ask(self, question: str, request_id: str | None = None) -> PipelineOutput:
        """Run one turn: pass current context for follow-up awareness, then append this turn to context."""
        result = self._pipeline.run(
            question,
            request_id=request_id,
            conversation_context=self._context,
        )
        self._context.add_from_output(result)
        return result

    def reset(self) -> None:
        """Clear conversation history (next question will be treated as first turn)."""
        self._context = ConversationContext(max_turns=self._context.max_turns)

    @property
    def context(self) -> ConversationContext:
        return self._context