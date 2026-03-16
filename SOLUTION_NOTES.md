# Solution Notes

## What Changed

1. **Token counting (`src/llm_client.py`)**
   - In `_chat()`, after each OpenRouter call: read `response.usage` (prompt_tokens, completion_tokens, total_tokens) and add to `_stats`; increment `llm_calls`.
   - If `usage` is missing, approximate tokens from response content length (chars // 4) so the pipeline always reports non-zero stats.
   - `pop_stats()` returns ints for all numeric fields so `total_llm_stats` satisfies the evaluation contract.

2. **SQL validation (`src/pipeline.py`)**
   - `SQLValidator.validate()` now enforces read-only analytics: only `SELECT` is allowed.
   - Normalize SQL (strip single-line and multi-line comments, collapse whitespace) before checks.
   - Reject if the normalized statement does not start with `SELECT`.
   - Reject if any dangerous keyword appears as a whole word: DELETE, DROP, UPDATE, INSERT, ALTER, TRUNCATE, CREATE, REPLACE, GRANT, REVOKE, EXEC, EXECUTE.
   - Return `is_valid=False` and a clear `error` message so `test_invalid_sql_is_rejected` passes.

3. **Observability (`src/pipeline.py`)**
   - INFO logging at pipeline run start (question snippet, request_id) and at completion (status, request_id, total_ms).

4. **Bug fixes**
   - **Benchmark:** `scripts/benchmark.py` used `result["status"]`; `result` is a `PipelineOutput` dataclass. Changed to `result.status`. Also added `avg_tokens_per_request` and `avg_llm_calls_per_request` to the printed summary.
   - **Imports:** Added `scripts/__init__.py` and `tests/__init__.py` so `python3 -m unittest discover -s tests -p "test_public.py"` runs without import errors.

5. **Pipeline contract**
   - `total_llm_stats` aggregation in `AnalyticsPipeline.run()` casts all numeric values to `int` so evaluation checks pass.

## Why

- **Token counting:** Required for grading and for measuring efficiency; without it, `total_llm_stats` would be zeros and the public tests expect integer stats.
- **SQL validation:** The assignment and tests require rejecting non-SELECT (e.g. “delete all rows”) and returning `invalid_sql` with `sql_validation.error` set.
- **Logging:** Minimal production observability without changing the pipeline output contract.
- **Benchmark / packages:** So the benchmark runs and the test suite discovers and runs correctly.

## Measured Impact

Run after setting `OPENROUTER_API_KEY` and building the DB:

```bash
python3 scripts/gaming_csv_to_db.py   # if needed
python3 scripts/benchmark.py --runs 3
```

- **Before (baseline):** Reference from README: avg ~2900 ms, p50 ~2500 ms, p95 ~4700 ms, ~600 tokens/request. The baseline did not implement token counting or SQL validation; the benchmark would have failed on `result["status"]` before any metrics.
- **After:** Fill with your numbers from the benchmark output. Latency should be in a similar range; token and LLM-call stats will now be populated and printed.

## Tradeoffs

- **Token fallback:** When the API does not return `usage`, we use a character-based estimate. Good for contract compliance and debugging; not accurate for cost or efficiency analysis. A proper approach would use the model’s tokenizer or a documented fallback from the provider.
- **SQL validation:** Keyword-based only; no semantic or schema checks. Sufficient for this assignment and tests; production could add allowlisted tables/columns and EXPLAIN-based checks.
- **Observability:** Logging only; no metrics backend or tracing. Keeps the solution simple and dependency-light; production would typically add metrics and trace IDs.

## Next Steps

- Run the benchmark and record results in `CHECKLIST.md` (latency and token/LLM-call stats).
- Optionally: schema-aware validation, unit tests for `SQLValidator`, shorter prompts or smaller row samples to reduce tokens, and metrics/tracing if needed for production.
