# Production Readiness Checklist

**Instructions:** Complete all sections below. Check the box when an item is implemented, and provide descriptions where requested. This checklist is a required deliverable.

**Assignment tasks (README):** All six assignment tasks and hard requirements are addressed: (1) production-ready behaviour, (2) accurate SQL via schema discovery and UNANSWERABLE for out-of-schema questions, (3) answer correctness and edge cases, (4) observability (logging, metrics, tracing), (5) validation (SQL, answer quality, result consistency), (6) efficiency (token counting, schema discovery, retry). Public tests pass unmodified; token counting implemented; output contract preserved.

---

## Approach

Describe how you approached this assignment and what key problems you identified and solved.

- **System works correctly end-to-end**

**What were the main challenges you identified?**

```
(1) Token counting was not implemented—required for efficiency evaluation and total_llm_stats contract.
(2) SQL validation was a stub—all queries were accepted, so DELETE/DROP etc. were not rejected (breaking test_invalid_sql_is_rejected).
(3) Benchmark script used result["status"] on a dataclass, causing TypeError—fixed to result.status.
(4) tests/ and scripts/ lacked __init__.py, causing import errors when running unittest discover.
(5) OpenRouter free tier was timing out (rate limits / latency), so a direct Gemini API key option was needed for development and benchmarking while keeping OpenRouter available for production.
```

**What was your approach?**

```
Implemented token counting in llm_client._chat() by reading usage from the OpenRouter response (usage.prompt_tokens, completion_tokens, total_tokens) with a character-based fallback when usage is missing. Ensured pop_stats() and pipeline aggregates return ints for the evaluation contract. 

Implemented SQL validation in SQLValidator: only allow SELECT; reject empty SQL and dangerous keywords (DELETE, DROP, UPDATE, INSERT, ALTER, TRUNCATE, CREATE, etc.) using normalized SQL and whole-word matching. Added structured logging at pipeline start and completion. Fixed benchmark.py and added package __init__.py files so tests run.

Added support for a direct Gemini API key (GEMINI_API_KEY): when set, the pipeline uses the Google Gemini API for all LLM calls instead of OpenRouter. This was used for benchmarking and development because OpenRouter’s free tier was timing out due to rate limits; OpenRouter remains the production path when GEMINI_API_KEY is not set.

Instructed the LLM to return exactly UNANSWERABLE when a question cannot be answered from the given schema (e.g. zodiac sign); the SQL extractor treats this as no SQL so the pipeline returns status unanswerable and the standard "cannot answer" message, satisfying test_unanswerable_prompt_is_handled.
```

---

## Observability

- **Logging**
  - Description: Python `logging` in `src/pipeline.py`. Pipeline run start (question snippet, request_id) and completion (status, request_id, total_ms) are logged at INFO so runs are observable without changing the output contract.
- **Metrics**
  - Description: Per-run metrics are logged at INFO on pipeline completion: status, request_id, total_ms, llm_calls, and total_tokens. Enables aggregation via log parsing; no separate metrics backend.
- **Tracing**
  - Description: request_id is logged at pipeline start and completion. DEBUG-level stage spans (sql_generation, sql_validation, sql_execution, answer_generation) log request_id and stage name for correlation when log level is DEBUG.

---

## Validation & Quality Assurance

- **SQL validation**
  - Description: Only SELECT is allowed. SQL is normalized (strip comments, collapse whitespace, uppercase for checks). Rejected: empty SQL; statements not starting with SELECT; presence of dangerous keywords as whole words (DELETE, DROP, UPDATE, INSERT, ALTER, TRUNCATE, CREATE, REPLACE, GRANT, REVOKE, EXEC, EXECUTE). Returns is_valid=False and a clear error message for evaluation and debugging. Schema-incompatible questions (e.g. zodiac) are handled by instructing the LLM to reply UNANSWERABLE when the question cannot be answered from the schema; extractor treats UNANSWERABLE as no SQL so status is unanswerable.
- **Answer quality**
  - Description: When status would be success, the pipeline verifies the answer is non-empty; if empty, status is set to error and a fallback message is used so the contract is preserved.
- **Result consistency**
  - Description: When multiple rows are returned, the pipeline checks that every row has the same set of keys as the first row; a warning is logged on inconsistency (no status change).
- **Error handling**
  - Description: Pipeline propagates and surfaces errors via stage outputs (sql_generation.error, sql_validation.error, sql_execution.error, answer_generation.error) and final status (unanswerable, invalid_sql, error). LLM and DB exceptions are caught and converted to error strings; validation and execution failures set status so the contract is preserved.

---

## Maintainability

- **Code organization**
  - Description: Validation logic is in `SQLValidator` and a small `_normalize_sql_for_validation()` helper; LLM and token logic in `OpenRouterLLMClient`; pipeline orchestration in `AnalyticsPipeline`. Types in `src/types.py`; no business logic in route/script entry points.
- **Configuration**
  - Description: Model and API key via env: `GEMINI_API_KEY` (optional, for Gemini) or `OPENROUTER_API_KEY` with `OPENROUTER_MODEL`; DB path configurable via `AnalyticsPipeline(db_path=...)` and default `DEFAULT_DB_PATH`.
- **Error handling**
  - Description: Same as in Validation & Quality Assurance—errors captured per stage and in final status; no uncaught exceptions in the main pipeline flow.
- **Documentation**
  - Description: README and this checklist document setup and behavior. Inline comments in validation, token-counting, and pipeline logic. .env.example documents GEMINI_API_KEY and OPENROUTER_API_KEY. No separate API or runbook docs added.

---

## LLM Efficiency

- **Token usage optimization**
  - Description: Token counting implemented so usage is measurable. `total_llm_stats` (prompt_tokens, completion_tokens, total_tokens, llm_calls) is populated from OpenRouter response `usage` when present, with a character-based fallback so the contract is always satisfied. Enables benchmarking and future prompt/size optimizations.
- **Efficient LLM requests**
  - Description: Schema is discovered from the DB and passed to the LLM so only actual column names are used (fewer invalid queries). Retry on "incomplete input" avoids wasted runs. Row sample for answer generation is capped at 20. Optional Gemini (GEMINI_API_KEY) or OpenRouter (OPENROUTER_API_KEY) via env.

---

## Testing

- **Unit tests**
  - Description: Added `tests/test_sql_validation.py` for SQLValidator (SELECT allowed; DELETE, DROP, UPDATE, INSERT, empty, None rejected). No API key required; run with `python3 -m unittest tests.test_sql_validation -v`.
- **Integration tests**
  - Description: Existing public tests in `tests/test_public.py` are the integration tests; they must pass and were not modified. They cover answerable prompt, unanswerable prompt, invalid SQL rejection, timings, and output contract.
- **Performance tests**
  - Description: `scripts/benchmark.py` provides latency and success-rate metrics; run manually with `python3 scripts/benchmark.py --runs 3`. Not wired as an automated test (requires API key and data).
- **Edge case coverage**
  - Description: Public tests cover invalid SQL (DELETE), unanswerable question (zodiac), and contract/timings. Validation covers empty SQL, non-SELECT, and dangerous keywords.

---

## Optional: Multi-Turn Conversation Support

**Only complete this section if you implemented the optional follow-up questions feature.**

- **Intent detection for follow-ups**
  - Description: We do not classify intent explicitly. Every follow-up is passed to the same pipeline with conversation context. The LLM receives the previous question(s), SQL, result summary, and answer, and the new question; it can then generate either refined SQL (filter/sort) or new SQL. For “explain” style follow-ups, the answer-generation step also receives the previous Q&A so it can elaborate without requiring new SQL.
- **Context-aware SQL generation**
  - Description: When `conversation_context` is present, `generate_sql` builds a prompt that includes the last 1–2 turns: previous question, previous SQL, a short result summary (column names + up to 5 sample rows), and previous answer. The system prompt instructs the model to refine the previous query (e.g. add WHERE, change ORDER BY) or write a new SELECT as needed for the new question.
- **Context persistence**
  - Description: `ConversationContext` holds a list of `ConversationTurn` (question, sql, rows, answer), capped at `max_turns` (default 5). Callers can (1) use `AnalyticsPipeline.run(question, conversation_context=ctx)` and manually build/update `ConversationContext` from each `PipelineOutput` via `ctx.add_from_output(result)`, or (2) use `ConversationPipeline`, which keeps a single context and exposes `ask(question)` so each response is appended automatically. `ConversationPipeline.reset()` clears history.
- **Ambiguity resolution**
  - Description: Ambiguous references like “what about males?” are resolved by the LLM using the previous turn: the prompt includes the previous question (“distribution by gender”), previous SQL, and result summary. The model infers that “males” means adding a filter such as `WHERE gender = 'Male'` (or the actual column/value in the schema). No explicit co-reference module; the same context-aware prompt handles it.

**Approach summary:**

```
We added ConversationTurn and ConversationContext in src/types.py. 

The pipeline’s run() accepts an optional conversation_context. 

When present, it is passed to the LLM client: generate_sql() gets previous turn summaries so it can refine or replace the query; generate_answer() gets the previous Q&A for explanation-style follow-ups. 

A ConversationPipeline wrapper keeps context in memory and exposes ask(question) for a simple multi-turn API. Context is stored in memory only (no DB). 

Demo: scripts/conversation_demo.py.
```

---

## Production Readiness Summary

**What makes your solution production-ready?**

```
Correct output contract (PipelineOutput and stage types); 

token counting so efficiency is measurable; 

SQL validation so only read-only SELECT is executed; 

structured logging for pipeline runs; 

error handling that preserves status and errors; 

public tests pass with GEMINI_API_KEY or OPENROUTER_API_KEY and real data; 

unanswerable/schema-incompatible questions (e.g. zodiac) handled via LLM UNANSWERABLE; benchmark script fixed and runnable.
```

**Key improvements over baseline:**

```
(1) Token counting: real usage from API + fallback so total_llm_stats is always populated with ints. 

(2) SQL validation: only SELECT allowed; dangerous keywords rejected with clear errors. 

(3) Observability: INFO logging at pipeline start and completion with metrics (llm_calls, total_tokens); DEBUG stage tracing. 

(4) Answer quality: success requires non-empty answer. 

(5) Result consistency: row key consistency checked and warned. 

(6) Schema discovery from DB + retry on incomplete input for higher accuracy. 

(7) Unanswerable detection: LLM instructed to return UNANSWERABLE for schema-incompatible questions; pipeline treats as unanswerable with "cannot answer" message so public tests pass. 

(8) Bug fixes: benchmark result.status; scripts/ and tests/ __init__.py for imports.
```

**Known limitations or future work:**

```
No separate metrics/tracing backend (metrics via logs). 

Performance benchmark run manually. 

Multi-turn is implemented (ConversationPipeline + context-aware SQL/answer generation). 

Optional: automated performance test in CI, persistent conversation storage (e.g. DB) for long sessions.
```

---

## Benchmark Results

Include your before/after benchmark results here.

**Baseline (README reference):**

- Average latency: `~2900 ms`
- p50 latency: `~2500 ms`
- p95 latency: `~4700 ms`
- Success rate: (not measured)
- ~600 tokens/request

**Your solution (measured, before improvements):**

- Average latency:`14571 ms`
- p50 latency: `15506 ms`
- p95 latency: `23892 ms`
- Success rate: `19.4%`
- Average tokens per request: `664`
- Average LLM calls per request: `1.64`

**Your solution (after improvements):** `python3 scripts/benchmark.py --runs 3 --verbose` (using Gemini API key; OpenRouter free tier was timing out).

- Average latency: `3927 ms`
- p50 latency: `3418 ms`
- p95 latency: `5260 ms`
- Success rate: `91.67%`
- Average tokens per request: `819.22`
- Average LLM calls per request: `1.97`

---

**Completed by:** Anurag Mishra
**Date:** March 17, 2026  
**Time spent:** 3 hours 15 minutes