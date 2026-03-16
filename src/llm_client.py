from __future__ import annotations

import json
import os
import time
from typing import Any

from src.types import (
    AnswerGenerationOutput,
    ConversationContext,
    SQLGenerationOutput,
)

DEFAULT_MODEL = "openai/gpt-5-nano"

# Full schema so the LLM uses only real columns (reduces "no such column" / invalid_sql).
SCHEMA_HINT = (
    "Table: gaming_mental_health. Columns (use only these): age, gender, income, "
    "daily_gaming_hours, weekly_sessions, years_gaming, sleep_hours, caffeine_intake, "
    "exercise_hours, stress_level, anxiety_score, depression_score, social_interaction_score, "
    "relationship_satisfaction, academic_performance, work_productivity, addiction_level, "
    "multiplayer_ratio, toxic_exposure, violent_games_ratio, mobile_gaming_ratio, night_gaming_ratio, "
    "weekend_gaming_hours, friends_gaming_count, online_friends, streaming_hours, esports_interest, "
    "headset_usage, microtransactions_spending, parental_supervision, loneliness_score, "
    "aggression_score, happiness_score, bmi, screen_time_total, eye_strain_score, back_pain_score, "
    "competitive_rank, internet_quality."
)


class OpenRouterLLMClient:
    """LLM client using the OpenRouter SDK for chat completions."""

    provider_name = "openrouter"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client = OpenRouter(api_key=api_key)
        self._stats = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _chat(self, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
        res = self._client.chat.send(
            messages=messages,
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )

        # Token counting from OpenRouter response (OpenAI-compatible usage object)
        self._stats["llm_calls"] = self._stats.get("llm_calls", 0) + 1
        usage = getattr(res, "usage", None)
        if usage is not None:
            self._stats["prompt_tokens"] = self._stats.get("prompt_tokens", 0) + int(
                getattr(usage, "prompt_tokens", 0) or 0
            )
            self._stats["completion_tokens"] = self._stats.get("completion_tokens", 0) + int(
                getattr(usage, "completion_tokens", 0) or 0
            )
            self._stats["total_tokens"] = self._stats.get("total_tokens", 0) + int(
                getattr(usage, "total_tokens", 0) or 0
            )
        choices = getattr(res, "choices", None) or []
        if self._stats.get("total_tokens", 0) == 0:
            # Fallback: approximate tokens from response content if usage missing
            content_len = 0
            if choices:
                msg = getattr(choices[0], "message", None)
                content_len = len((getattr(msg, "content", None) or "") or "")
            approx = max(1, content_len // 4)
            self._stats["prompt_tokens"] = self._stats.get("prompt_tokens", 0) + approx
            self._stats["completion_tokens"] = self._stats.get("completion_tokens", 0) + approx
            self._stats["total_tokens"] = self._stats.get("total_tokens", 0) + 2 * approx
        if not choices:
            raise RuntimeError("OpenRouter response contained no choices.")
        msg = getattr(choices[0], "message", None)
        content = getattr(msg, "content", None)
        text = self._content_to_str(content)
        if not text and msg is not None:
            # Some models (e.g. reasoning models) put output in reasoning when content is None
            reasoning = getattr(msg, "reasoning", None)
            text = self._content_to_str(reasoning) if reasoning else ""
        if not text:
            raise RuntimeError("OpenRouter response had no text content.")
        return text.strip()

    @staticmethod
    def _content_to_str(content: Any) -> str:
        """Normalize API content (str or list of blocks) to a single string."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif isinstance(block, dict) and "text" in block:
                    parts.append(block["text"] or "")
                elif hasattr(block, "text"):
                    parts.append(getattr(block, "text", "") or "")
            return "".join(parts)
        return str(content)

    @staticmethod
    def _extract_sql(text: str) -> str | None:
        import re as _re
        raw = text.strip()
        # Try JSON with "sql" key first
        if raw.startswith("{") and raw.endswith("}"):
            try:
                parsed = json.loads(raw)
                sql = parsed.get("sql")
                if isinstance(sql, str) and sql.strip():
                    return sql.strip()
                return None
            except json.JSONDecodeError:
                pass
        # Strip markdown code blocks (```sql ... ``` or ``` ... ```)
        for pattern in (
            _re.compile(r"```(?:sql)?\s*\n(.*?)```", _re.DOTALL | _re.IGNORECASE),
            _re.compile(r"```\s*\n(.*?)```", _re.DOTALL),
        ):
            match = pattern.search(raw)
            if match:
                block = match.group(1).strip()
                if "select " in block.lower():
                    return block.strip()
        def _clean_segment(segment: str) -> str | None:
            segment = segment.split(";")[0].strip()
            segment = segment.split("\n\n")[0].strip()
            # Truncate at first dangerous keyword (whole word) so we never include it
            # Truncate at statement-level keywords only (not REPLACE which is a function)
            danger_match = _re.search(
                r"\b(DELETE|DROP|UPDATE|INSERT|ALTER|TRUNCATE|CREATE)\b",
                segment,
                _re.IGNORECASE,
            )
            if danger_match:
                segment = segment[: danger_match.start()].strip()
            # Drop lines that contain dangerous keywords (avoid trailing prose)
            safe_lines = []
            for line in segment.split("\n"):
                if _re.search(r"\b(DELETE|DROP|UPDATE|INSERT|ALTER|TRUNCATE)\b", line, _re.IGNORECASE):
                    break
                safe_lines.append(line)
            segment = "\n".join(safe_lines).strip()
            return segment if segment.upper().startswith("SELECT") else None

        lower = raw.lower()
        for needle in ("select ", "select\n", "select\t"):
            idx = lower.find(needle)
            if idx >= 0:
                out = _clean_segment(raw[idx:])
                if out:
                    return out
        match = _re.search(r"\bselect\b", raw, _re.IGNORECASE)
        if match:
            return _clean_segment(raw[match.start() :])
        return None

    def _build_sql_prompt_with_conversation(
        self, question: str, conversation: ConversationContext
    ) -> str:
        recent = conversation.recent_for_prompt(last_n=2)
        if not recent:
            return f"Question: {question}\n\nGenerate a SQL query to answer this question."
        parts = ["Previous turn(s) for context:"]
        for t in recent:
            parts.append(t.to_summary(max_rows=5))
        parts.append(f"\nNew question: {question}")
        parts.append(
            "\nGenerate a single SQLite SELECT query for the new question. "
            "You may refine the previous query (e.g. add WHERE, change ORDER BY, filter to a subset) "
            "or write a new query. Return only the SQL or a JSON object with a 'sql' key."
        )
        return "\n".join(parts)

    def generate_sql(self, question: str, context: dict) -> SQLGenerationOutput:
        system_prompt = (
            "You are a SQL assistant. "
            "Generate SQLite SELECT queries from natural language questions. "
            f"Schema: {SCHEMA_HINT} "
            "Return only the SQL, or a JSON object with a 'sql' key, or SQL inside markdown code blocks."
        )
        conversation = context.get("conversation")
        if isinstance(conversation, ConversationContext) and conversation.turns:
            user_prompt = self._build_sql_prompt_with_conversation(question, conversation)
        else:
            user_prompt = f"Context: {context}\n\nQuestion: {question}\n\nGenerate a SQL query to answer this question."

        start = time.perf_counter()
        error = None
        sql = None

        try:
            text = self._chat(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.0,
                max_tokens=240,
            )
            sql = self._extract_sql(text)
            if sql is None and not conversation:
                strict_prompt = (
                    f"Question: {question}. Schema: {SCHEMA_HINT} "
                    "Return only one line: the SQLite SELECT query. No explanation."
                )
                text = self._chat(
                    messages=[{"role": "system", "content": "You are a SQL assistant. Return only the SQL query."}, {"role": "user", "content": strict_prompt}],
                    temperature=0.0,
                    max_tokens=240,
                )
                sql = self._extract_sql(text)
        except Exception as exc:
            error = str(exc)

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return SQLGenerationOutput(
            sql=sql,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            error=error,
        )

    def generate_answer(
        self,
        question: str,
        sql: str | None,
        rows: list[dict[str, Any]],
        conversation_context: ConversationContext | None = None,
    ) -> AnswerGenerationOutput:
        if not sql:
            return AnswerGenerationOutput(
                answer="I cannot answer this with the available table and schema. Please rephrase using known survey fields.",
                timing_ms=0.0,
                llm_stats={"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": self.model},
                error=None,
            )
        if not rows:
            return AnswerGenerationOutput(
                answer="Query executed, but no rows were returned.",
                timing_ms=0.0,
                llm_stats={"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": self.model},
                error=None,
            )

        system_prompt = (
            "You are a concise analytics assistant. "
            "Use only the provided SQL results. Do not invent data."
        )
        user_prompt = (
            f"Question:\n{question}\n\nSQL:\n{sql}\n\n"
            f"Rows (JSON):\n{json.dumps(rows[:30], ensure_ascii=True)}\n\n"
            "Write a concise answer in plain English."
        )
        if conversation_context and conversation_context.turns:
            prev = conversation_context.recent_for_prompt(last_n=1)
            if prev:
                t = prev[0]
                user_prompt = (
                    "Previous Q&A for context:\n"
                    f"Q: {t.question}\nA: {t.answer}\n\n"
                    "Current question and results:\n"
                    + user_prompt
                )

        start = time.perf_counter()
        error = None
        answer = ""

        try:
            answer = self._chat(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.2,
                max_tokens=220,
            )
        except Exception as exc:
            error = str(exc)
            answer = f"Error generating answer: {error}"

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return AnswerGenerationOutput(
            answer=answer,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            error=error,
        )

    def pop_stats(self) -> dict[str, Any]:
        out = dict(self._stats or {})
        self._stats = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        # Ensure all numeric stats are int for evaluation contract
        for key in ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            if key in out:
                out[key] = int(out[key])
        return out


def build_default_llm_client() -> OpenRouterLLMClient:
    # OPENROUTER_* are loaded from project .env via src/__init__.py
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required.")
    return OpenRouterLLMClient(api_key=api_key)
