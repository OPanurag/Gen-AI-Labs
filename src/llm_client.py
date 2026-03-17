from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Protocol, Union

# Seconds to wait before retry when OpenRouter returns rate limit (e.g. 8 req/min for free models).
RATE_LIMIT_RETRY_DELAY = 10
RATE_LIMIT_MAX_RETRIES = 2

from src.types import (
    AnswerGenerationOutput,
    ConversationContext,
    SQLGenerationOutput,
)

# Single shared model for all OpenRouter requests (override via OPENROUTER_MODEL in .env).
# openrouter/free = OpenRouter's free-tier router (auto-selects a working free model).
DEFAULT_MODEL = "openrouter/free"

# Default Gemini model when using GEMINI_API_KEY (override via GEMINI_MODEL in .env).
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Token budget for SQL generation (enough to avoid truncation of complex queries).
SQL_MAX_TOKENS = 400

logger = logging.getLogger(__name__)


class LLMClientProtocol(Protocol):
    """Protocol for LLM clients used by the pipeline (OpenRouter or Gemini)."""

    def generate_sql(self, question: str, context: dict) -> SQLGenerationOutput: ...
    def generate_answer(
        self,
        question: str,
        sql: str | None,
        rows: list[dict[str, Any]],
        conversation_context: ConversationContext | None,
    ) -> AnswerGenerationOutput: ...
    def pop_stats(self) -> dict[str, Any]: ...


def _extract_openrouter_error_message(exc: Exception) -> str | None:
    """When the OpenRouter SDK raises a validation error (API returned error body), extract the API message."""
    s = str(exc)
    if "input_value=" not in s or "'error'" not in s and '"error"' not in s:
        return None
    # Try to extract error.message from repr: input_value={'error': {'message': '...', 'code': 400}}
    for pattern in (
        r"['\"]message['\"]\s*:\s*['\"]([^'\"]+)['\"]",
        r"['\"]message['\"]\s*:\s*['\"]([^'\"]*(?:\\.[^'\"]*)*)['\"]",
    ):
        m = re.search(pattern, s)
        if m:
            msg = m.group(1).replace("\\'", "'").replace('\\"', '"')
            if msg:
                return f"OpenRouter API error: {msg}"
    return None


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


def _extract_sql_from_text(text: str) -> str | None:
    """Extract a single SELECT statement from LLM response text. Shared by OpenRouter and Gemini clients."""
    raw = text.strip()
    if raw.upper() == "UNANSWERABLE":
        return None
    _PROSE_TRIGGERS = re.compile(
        r"\s+(using|sqlite|query|this\s+query|to\s+get|to\s+find|to\s+return|on\s+the|we\s+get|you\s+can|"
        r"only\b|addresses\b|to\s+answer|to\s+show|to\b)",
        re.IGNORECASE,
    )

    def _strip_trailing_prose(segment: str) -> str:
        segment = segment.split(";")[0].strip()
        if ". " in segment:
            segment = segment.split(". ")[0].strip()
        m = _PROSE_TRIGGERS.search(segment)
        if m:
            segment = segment[: m.start()].strip()
        return segment

    if raw and "\n" not in raw and raw.upper().startswith("SELECT"):
        seg = _strip_trailing_prose(raw)
        return seg if seg and seg.upper().startswith("SELECT") else None
    if raw.startswith("{") and raw.endswith("}"):
        try:
            parsed = json.loads(raw)
            sql = parsed.get("sql")
            if isinstance(sql, str) and sql.strip():
                return sql.strip()
            return None
        except json.JSONDecodeError:
            pass
    for pattern in (
        re.compile(r"```(?:sql)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE),
        re.compile(r"```\s*\n(.*?)```", re.DOTALL),
    ):
        match = pattern.search(raw)
        if match:
            block = _strip_trailing_prose(match.group(1).strip())
            if block.upper().startswith("SELECT"):
                return block
            sel = re.search(r"\bSELECT\b", block, re.IGNORECASE)
            if sel:
                return _strip_trailing_prose(block[sel.start() :])
    def _clean_segment(segment: str) -> str | None:
        segment = _strip_trailing_prose(segment)
        segment = segment.split("\n\n")[0].strip()
        danger_match = re.search(
            r"\b(DELETE|DROP|UPDATE|INSERT|ALTER|TRUNCATE|CREATE)\b",
            segment,
            re.IGNORECASE,
        )
        if danger_match:
            segment = segment[: danger_match.start()].strip()
        safe_lines = []
        for line in segment.split("\n"):
            if re.search(r"\b(DELETE|DROP|UPDATE|INSERT|ALTER|TRUNCATE)\b", line, re.IGNORECASE):
                break
            safe_lines.append(line)
        segment = "\n".join(safe_lines).strip()
        if segment and not segment.upper().startswith("SELECT"):
            sel = re.search(r"\bSELECT\b", segment, re.IGNORECASE)
            if sel:
                segment = segment[sel.start() :].strip()
        segment = _strip_trailing_prose(segment)
        return segment if segment and segment.upper().startswith("SELECT") else None

    lower = raw.lower()
    for needle in ("select ", "select\n", "select\t"):
        idx = lower.find(needle)
        if idx >= 0:
            out = _clean_segment(raw[idx:])
            if out:
                return out
    match = re.search(r"\bselect\b", raw, re.IGNORECASE)
    if match:
        return _clean_segment(raw[match.start() :])
    for line in raw.split("\n"):
        line = line.strip()
        if line.upper().startswith("SELECT"):
            out = _strip_trailing_prose(line)
            if out and out.upper().startswith("SELECT"):
                return out
    return None


class OpenRouterLLMClient:
    """LLM client using the OpenRouter SDK for chat completions."""

    provider_name = "openrouter"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        _env_model = (os.getenv("OPENROUTER_MODEL") or "").strip()
        self.model = model or _env_model or DEFAULT_MODEL
        self._client = OpenRouter(api_key=api_key)
        self._stats = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _chat(self, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
        last_exc = None
        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            try:
                res = self._client.chat.send(
                    messages=messages,
                    model=self.model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=False,
                )
                break
            except Exception as exc:
                last_exc = exc
                err_str = str(exc).lower()
                if attempt < RATE_LIMIT_MAX_RETRIES and ("rate limit" in err_str or "limit_rpm" in err_str):
                    logger.warning("Rate limited, retrying in %ss (attempt %d/%d)", RATE_LIMIT_RETRY_DELAY, attempt + 1, RATE_LIMIT_MAX_RETRIES + 1)
                    time.sleep(RATE_LIMIT_RETRY_DELAY)
                    continue
                # OpenRouter SDK raises validation errors when API returns error body (e.g. 400)
                api_msg = _extract_openrouter_error_message(exc)
                if api_msg:
                    raise RuntimeError(api_msg) from exc
                raise
        else:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("No response from OpenRouter.")

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
        return _extract_sql_from_text(text)

    def generate_sql(self, question: str, context: dict) -> SQLGenerationOutput:
        # Short system prompt to reduce tokens and focus the model on one-line SQL
        system_prompt = (
            "You are a SQL assistant. Reply with exactly one line: the SQLite SELECT query. "
            "No explanation, no markdown, no code blocks. Use only the given schema. "
            "Use = and numbers in SQL (e.g. addiction_level >= 3), not words like 'high' or 'is'. "
            "If the question cannot be answered using only the given schema (e.g. it asks about data or concepts not in the table, like zodiac signs), reply with exactly: UNANSWERABLE"
        )
        conversation = context.get("conversation")
        complete_only = context.get("complete_only") is True
        schema_hint = _schema_hint_from_context(context)
        if isinstance(conversation, ConversationContext) and conversation.turns:
            user_prompt = _build_sql_prompt_with_conversation(question, conversation)
        else:
            user_prompt = (
                f"Schema: {schema_hint}\n\nQuestion: {question}\n\n"
                + (
                    "Output a complete, single-line SQLite SELECT query only. Ensure the query is not truncated."
                    if complete_only
                    else "Reply with exactly one line: the SQLite SELECT query only."
                )
            )

        start = time.perf_counter()
        error = None
        sql = None

        try:
            text = self._chat(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.0,
                max_tokens=SQL_MAX_TOKENS,
            )
            sql = self._extract_sql(text)
            if sql is None and not conversation:
                strict_prompt = (
                    f"Schema: {schema_hint}\n\nQuestion: {question}\n\n"
                    "Output only one line: the SQLite SELECT query, nothing else."
                )
                text = self._chat(
                    messages=[{"role": "system", "content": "You output only a single line of SQL. No explanation."}, {"role": "user", "content": strict_prompt}],
                    temperature=0.0,
                    max_tokens=SQL_MAX_TOKENS,
                )
                sql = self._extract_sql(text)
        except Exception as exc:
            error = str(exc)
            logger.warning("SQL generation failed: %s", error, exc_info=False)

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
            f"Rows (JSON):\n{json.dumps(rows[:20], ensure_ascii=True)}\n\n"
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
                max_tokens=180,
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


class GeminiLLMClient:
    """LLM client using Google Gemini API (for local/dev when GEMINI_API_KEY is set)."""

    provider_name = "gemini"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from google.genai import Client, types
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'google-genai'.") from exc
        self._types = types
        _env_model = (os.getenv("GEMINI_MODEL") or "").strip()
        self.model = model or _env_model or DEFAULT_GEMINI_MODEL
        self._client = Client(api_key=api_key)
        self._stats = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _chat(self, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
        system_instruction = None
        user_content = ""
        for m in messages:
            role = (m.get("role") or "").strip().lower()
            content = (m.get("content") or "").strip()
            if role == "system":
                system_instruction = content
            elif role == "user":
                user_content = content
                break
        if not user_content and messages:
            user_content = (messages[-1].get("content") or "").strip()
        config_kw: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system_instruction:
            config_kw["system_instruction"] = system_instruction
        config = self._types.GenerateContentConfig(**config_kw)
        res = self._client.models.generate_content(
            model=self.model,
            contents=user_content,
            config=config,
        )
        text = (getattr(res, "text", None) or "").strip()
        if not text:
            raise RuntimeError("Gemini response had no text content.")
        # Token counting from Gemini response (usage_metadata shape varies by SDK version)
        self._stats["llm_calls"] = self._stats.get("llm_calls", 0) + 1
        usage = getattr(res, "usage_metadata", None)
        if usage is not None:
            prompt_tok = getattr(usage, "prompt_token_count", None) or getattr(usage, "input_token_count", None) or 0
            compl_tok = (
                getattr(usage, "candidates_token_count", None)
                or getattr(usage, "output_token_count", None)
                or getattr(usage, "completion_token_count", None)
                or 0
            )
            total_tok = getattr(usage, "total_token_count", None) or (prompt_tok + compl_tok if (prompt_tok or compl_tok) else 0)
            self._stats["prompt_tokens"] = self._stats.get("prompt_tokens", 0) + int(prompt_tok or 0)
            self._stats["completion_tokens"] = self._stats.get("completion_tokens", 0) + int(compl_tok or 0)
            self._stats["total_tokens"] = self._stats.get("total_tokens", 0) + int(total_tok or 0)
        if self._stats.get("total_tokens", 0) == 0:
            approx = max(1, len(text) // 4)
            self._stats["prompt_tokens"] = self._stats.get("prompt_tokens", 0) + approx
            self._stats["completion_tokens"] = self._stats.get("completion_tokens", 0) + approx
            self._stats["total_tokens"] = self._stats.get("total_tokens", 0) + 2 * approx
        return text

    def generate_sql(self, question: str, context: dict) -> SQLGenerationOutput:
        system_prompt = (
            "You are a SQL assistant. Reply with exactly one line: the SQLite SELECT query. "
            "No explanation, no markdown, no code blocks. Use only the given schema. "
            "Use = and numbers in SQL (e.g. addiction_level >= 3), not words like 'high' or 'is'. "
            "If the question cannot be answered using only the given schema (e.g. it asks about data or concepts not in the table, like zodiac signs), reply with exactly: UNANSWERABLE"
        )
        conversation = context.get("conversation")
        complete_only = context.get("complete_only") is True
        schema_hint = _schema_hint_from_context(context)
        if isinstance(conversation, ConversationContext) and conversation.turns:
            user_prompt = _build_sql_prompt_with_conversation(question, conversation)
        else:
            user_prompt = (
                f"Schema: {schema_hint}\n\nQuestion: {question}\n\n"
                + (
                    "Output a complete, single-line SQLite SELECT query only. Ensure the query is not truncated."
                    if complete_only
                    else "Reply with exactly one line: the SQLite SELECT query only."
                )
            )
        start = time.perf_counter()
        error = None
        sql = None
        try:
            text = self._chat(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.0,
                max_tokens=SQL_MAX_TOKENS,
            )
            sql = _extract_sql_from_text(text)
            if sql is None and not conversation:
                strict_prompt = (
                    f"Schema: {schema_hint}\n\nQuestion: {question}\n\n"
                    "Output only one line: the SQLite SELECT query, nothing else."
                )
                text = self._chat(
                    messages=[
                        {"role": "system", "content": "You output only a single line of SQL. No explanation."},
                        {"role": "user", "content": strict_prompt},
                    ],
                    temperature=0.0,
                    max_tokens=SQL_MAX_TOKENS,
                )
                sql = _extract_sql_from_text(text)
        except Exception as exc:
            error = str(exc)
            logger.warning("SQL generation failed: %s", error, exc_info=False)
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
            f"Rows (JSON):\n{json.dumps(rows[:20], ensure_ascii=True)}\n\n"
            "Write a concise answer in plain English."
        )
        if conversation_context and conversation_context.turns:
            prev = conversation_context.recent_for_prompt(last_n=1)
            if prev:
                t = prev[0]
                user_prompt = (
                    "Previous Q&A for context:\n"
                    f"Q: {t.question}\nA: {t.answer}\n\n"
                    "Current question and results:\n" + user_prompt
                )
        start = time.perf_counter()
        error = None
        answer = ""
        try:
            answer = self._chat(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.2,
                max_tokens=180,
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
        for key in ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            if key in out:
                out[key] = int(out[key])
        return out


def _schema_hint_from_context(context: dict) -> str:
    """Schema string for SQL generation: use pipeline-provided columns when present, else fallback."""
    hint = (context.get("schema_hint") or "").strip()
    return hint if hint else SCHEMA_HINT


def _build_sql_prompt_with_conversation(question: str, conversation: ConversationContext) -> str:
    """Build user prompt for SQL generation when conversation context exists. Shared by both clients."""
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


def build_default_llm_client() -> Union[OpenRouterLLMClient, GeminiLLMClient]:
    """Build LLM client from env. Prefers GEMINI_API_KEY for local/dev; otherwise OPENROUTER_API_KEY (required for production)."""
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        return GeminiLLMClient(api_key=gemini_key)
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Either GEMINI_API_KEY or OPENROUTER_API_KEY is required. "
            "Set GEMINI_API_KEY for local/dev (Gemini API), or OPENROUTER_API_KEY for production (https://openrouter.ai/)."
        )
    return OpenRouterLLMClient(api_key=api_key)
