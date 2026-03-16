"""Type definitions for SQL Agent pipeline input/output structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Multi-turn conversation support
# ---------------------------------------------------------------------------


@dataclass
class ConversationTurn:
    """One user question and the pipeline's response (for follow-up context)."""
    question: str
    sql: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""

    def to_summary(self, max_rows: int = 5) -> str:
        """Short summary for LLM context (avoids huge payloads)."""
        parts = [f"Q: {self.question}"]
        if self.sql:
            parts.append(f"SQL: {self.sql}")
        if self.rows:
            sample = self.rows[:max_rows]
            parts.append(f"Rows ({len(self.rows)} total): {sample!r}")
        if self.answer:
            parts.append(f"Answer: {self.answer[:500]}")
        return "\n".join(parts)


@dataclass
class ConversationContext:
    """History of recent turns for context-aware follow-up handling."""
    turns: list[ConversationTurn] = field(default_factory=list)
    max_turns: int = 5

    def add_turn(self, question: str, sql: str | None, rows: list[dict[str, Any]], answer: str) -> None:
        self.turns.append(
            ConversationTurn(question=question, sql=sql, rows=rows or [], answer=answer or "")
        )
        if len(self.turns) > self.max_turns:
            self.turns.pop(0)

    def recent_for_prompt(self, last_n: int = 2) -> list[ConversationTurn]:
        return self.turns[-last_n:] if self.turns else []

    @classmethod
    def from_output(cls, output: "PipelineOutput", max_turns: int = 5) -> "ConversationContext":
        """Build a new context from a single pipeline output (e.g. first turn)."""
        ctx = cls(max_turns=max_turns)
        ctx.add_turn(
            output.question,
            output.sql,
            list(output.rows) if output.rows else [],
            output.answer or "",
        )
        return ctx

    def add_from_output(self, output: "PipelineOutput") -> None:
        """Append a pipeline output as the next turn (after a follow-up)."""
        self.add_turn(
            output.question,
            output.sql,
            list(output.rows) if output.rows else [],
            output.answer or "",
        )


# ---------------------------------------------------------------------------
# Pipeline input/output
# ---------------------------------------------------------------------------


@dataclass
class PipelineInput:
    """Input to the AnalyticsPipeline.run() method."""
    question: str
    request_id: str | None = None


@dataclass
class SQLGenerationOutput:
    """Output from the SQL generation stage.

    For complex solutions with multiple LLM calls (chain-of-thought, planning,
    query refinement), populate intermediate_outputs with per-call details.
    llm_stats aggregates all calls for efficient evaluation.
    """
    sql: str | None
    timing_ms: float
    llm_stats: dict[str, Any]  # Aggregated: {llm_calls, prompt_tokens, completion_tokens, total_tokens, model}
    intermediate_outputs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class SQLValidationOutput:
    """Output from the SQL validation stage."""
    is_valid: bool
    validated_sql: str | None
    error: str | None = None
    timing_ms: float = 0.0


@dataclass
class SQLExecutionOutput:
    """Output from the SQL execution stage."""
    rows: list[dict[str, Any]]
    row_count: int
    timing_ms: float
    error: str | None = None


@dataclass
class AnswerGenerationOutput:
    """Output from the answer generation stage.

    For complex solutions with multiple LLM calls (summarization, verification),
    populate intermediate_outputs with per-call details.
    llm_stats aggregates all calls for efficient evaluation.
    """
    answer: str
    timing_ms: float
    llm_stats: dict[str, Any]  # Aggregated: {llm_calls, prompt_tokens, completion_tokens, total_tokens, model}
    intermediate_outputs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class PipelineOutput:
    """Complete output from AnalyticsPipeline.run()."""
    # Status
    status: str  # "success" | "unanswerable" | "invalid_sql" | "error"
    question: str
    request_id: str | None

    # Stage outputs (for evaluation)
    sql_generation: SQLGenerationOutput
    sql_validation: SQLValidationOutput
    sql_execution: SQLExecutionOutput
    answer_generation: AnswerGenerationOutput

    # Convenience fields
    sql: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""

    # Aggregates
    timings: dict[str, float] = field(default_factory=dict)
    total_llm_stats: dict[str, Any] = field(default_factory=dict)
