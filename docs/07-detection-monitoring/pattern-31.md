# Pattern #31 — Error Returns Success

| Field | Value |
|---|---|
| **ID** | 31 |
| **Category** | Detection & Monitoring |
| **Severity** | High |
| **Affected Frameworks** | LangChain / CrewAI / AutoGen / LangGraph / Custom |
| **Average Debugging Time (if undetected)** | 3 to 30 days |
| **Keywords** | swallowed exception, silent failure, phantom data, empty list ambiguity, ok=True on error, status=200 on failure, exception masking, error propagation, downstream corruption, recommendation engine |

---

## 1. Observable Symptoms

This pattern is systematically deceptive because the observability surface reports normal operation. Request counters increment. Response times are within normal range. No exceptions appear in error logs. The function that failed reported success. Only the data — or the absence of it — reveals the problem, and only to observers who know what the data should look like.

**Operational symptoms:**

- Downstream consumers receive empty results or zeroed-out values and interpret them as valid states: "no recommendations available," "no records found," "score is 0.0."
- Metrics that should fluctuate (recommendation click-through rate, model confidence scores, query result counts) flat-line at zero or minimum values without any corresponding alert.
- Users report degraded experiences (blank recommendation panels, unhelpful default content, missing personalisation) that are intermittent or correlated with infrastructure events (DB failover, network partition, upstream service restart).
- Engineers investigating user complaints find no errors in the application logs for the affected time window, making the issue initially appear to be a client-side problem or a data pipeline issue.
- Post-mortem analysis finds that a database query function caught a connection timeout, logged it at DEBUG level, and returned `[]`. The caller treated `[]` as "no results" and proceeded to serve default content for hours.
- In LLM agent pipelines: a retrieval step that should populate the context window for an LLM call silently fails and returns an empty list. The LLM receives an empty context and hallucinates or falls back to generic responses. The agent framework logs the LLM call as successful because the call itself succeeded — the failure was in the retrieval step upstream.
- Error rates, as measured by exception counts or 5xx response codes, remain at zero during the incident. The failure is invisible to standard error monitoring.

**Code-level symptoms:**

- Functions that query external systems (`db.query()`, `api.fetch()`, `cache.get()`) contain bare `except Exception` blocks that `return []`, `return {}`, `return None`, or `return {"ok": True, "data": []}`.
- The caller of such a function treats the empty return as a valid no-results state and has no code path for handling a query failure distinct from a genuine empty result.
- Log statements inside the `except` block use `logging.debug()` or `logging.warning()` rather than `logging.error()` or `logging.exception()`, or are absent entirely.
- No metrics are incremented on the failure path: the `exception_counter` metric is never incremented, the `query_error` gauge is never set.
- The function's return type annotation or docstring does not distinguish between "success with empty result" and "failure."

---

## 2. Field Story (Anonymized)

A mid-market e-commerce company operated a personalised recommendation engine — internally called "Lens" — to drive product discovery across its web and mobile surfaces. Lens used a multi-agent architecture: a retrieval agent fetched candidate products from a vector database, a ranking agent scored candidates using a fine-tuned model, and a filtering agent applied business rules (inventory, margin, geographic restrictions). The pipeline ran on LangGraph and served roughly 40,000 recommendation requests per hour at peak.

The retrieval agent's core function was:

```python
def fetch_candidates(user_id: str, context: dict) -> list[dict]:
    try:
        results = vector_db.query(
            user_id=user_id,
            embedding=context["embedding"],
            top_k=50,
        )
        return results
    except Exception as e:
        logger.debug("fetch_candidates failed: %s", e)
        return []
```

During a routine database maintenance window, the vector database's read replica was briefly unavailable (approximately 90 seconds). The retrieval agent's `fetch_candidates()` function received connection timeout exceptions, logged them at DEBUG, and returned empty lists. The ranking agent received empty candidate lists, had no items to rank, and returned an empty ranked list. The filtering agent received an empty list and returned an empty list. The LangGraph pipeline completed without error. Recommendation panels on the frontend displayed fallback content (a static "trending items" list) as designed — the fallback was intended for the case of "no personalised results," not for the case of "the retrieval system is broken."

The maintenance window lasted 90 seconds. The effect persisted for 4.5 hours.

The reason for the extended impact: the vector database used a result caching layer that cached empty results with a 4-hour TTL. After the database recovered, `fetch_candidates()` returned real results — but the empty results were already cached. For 4.5 hours, every user who had made a request during the 90-second outage received non-personalised fallback content. The cache saw `[]` as a valid result and stored it faithfully.

The post-mortem identified three compounding failures: the exception was swallowed and returned as `[]`; the caller could not distinguish `[]` (failure) from `[]` (genuine empty result); and the caching layer cached failure-state outputs as if they were success-state outputs.

The engineering team estimated that the personalisation gap during those 4.5 hours cost approximately 11% of expected click-through revenue for that day. Detection took 6 hours — a user complaint triggered a manual investigation. The error logs showed nothing.

---

## 3. Technical Root Cause

**The fundamental ambiguity of empty returns:**

In Python, and in most dynamically typed languages, an empty collection (`[]`, `{}`, `""`) serves double duty as both a valid data value and a convenient sentinel for "nothing happened." This overloading is the root of the problem. When a function returns `[]`, the caller faces an underdetermined interpretation:

1. The query succeeded and no records matched the criteria.
2. The query failed and the function is masking the failure.

These are semantically opposite states — one is expected and handled normally, the other is an error requiring escalation — but they are represented by the same value. The caller cannot distinguish them without additional context.

**Why the pattern is systematically seductive:**

Exception-swallowing with empty returns appears defensive. The developer who writes it is thinking: "I don't want one bad query to crash the whole pipeline." This reasoning is correct in narrow cases (retryable transient errors that are automatically recovered). It is catastrophically wrong when applied broadly, because it makes the failure mode of the system identical to a specific valid operational state.

The correct defensive pattern is not to return a success value on failure but to either:
- Raise a typed exception that the caller can catch explicitly and handle with full context, or
- Return a typed result object that carries both the outcome status and either the data or the error, forcing the caller to inspect the status before using the data.

**Why downstream corruption propagates silently:**

Empty-return masking is particularly damaging in pipelines with multiple stages, because each stage treats the upstream stage's empty output as valid and applies its own logic to it. The result is that the empty state propagates through the entire pipeline, reaches external systems (caches, databases, user-facing APIs), and is stored as if it were a real result. By the time the failure is detected, it has been written to durable storage and served to external consumers.

**Framework-specific aggravation:**

In LangGraph, nodes return dicts that update the pipeline state. A retrieval node that returns `{**state, "candidates": []}` on failure is indistinguishable from a retrieval node that returned zero candidates legitimately. The graph continues executing. Downstream nodes apply ranking and filtering logic to empty inputs, producing empty outputs, and the graph reaches `END` with a success status.

In CrewAI, a `Task` result is passed to the next task as context. If a retrieval task returns an empty string or empty list on failure, the next task's LLM call includes that empty context, and the LLM either generates hallucinated content or unhelpful generic output. The task is marked complete.

In LangChain chains, a step that returns `{"output": ""}` on failure propagates the empty output to all downstream steps. `LLMChain` does not validate its input; it calls the LLM with the empty input.

**The "no error" monitoring gap:**

Standard monitoring monitors errors: exception rates, 5xx counts, timeout counts. Error-returns-success failures produce none of these signals. The function reports success. The pipeline reports success. HTTP responses are 200. The only signal is in the data quality layer: recommendation counts, model confidence distributions, query result set sizes. These are business metrics, not technical metrics, and they are rarely monitored with the same rigor as error rates.

---

## 4. Detection

### 4.1 Manual Code Audit

Identify functions that interact with external systems (databases, APIs, caches, vector stores, message queues) and contain exception handlers that return empty values or success indicators.

**Grep pattern — except blocks that return empty collections:**

```bash
grep -rn -A 3 "except.*Exception\|except.*Error" --include="*.py" . \
  | grep -E "return \[\]|return \{\}|return None|return \"\"|return ''|return 0"
```

**Grep pattern — functions returning ok=True or status=200 inside except:**

```bash
grep -rn -B 2 -A 5 "except" --include="*.py" . \
  | grep -E "ok.*True|status.*200|success.*True" \
  | grep -v "^--$"
```

**Code review checklist item:** Any `except` block in a function that calls an external system must either re-raise the exception, raise a typed domain exception, or return a typed result object. It must never return a value that the caller would interpret as a successful no-results response.

### 4.2 Automated CI/CD

The following AST-based linter detects functions that interact with external systems and contain exception handlers returning empty values.

```python
# ci_checks/check_error_returns_success.py
"""
CI gate: detects functions that catch exceptions and return
values that are ambiguous with a successful empty result.

A violation is flagged when:
  1. A function contains a call to a known external-system method
     (query, fetch, get, execute, request, search, find), AND
  2. The function contains an except block, AND
  3. The except block contains a return statement returning
     an empty collection, None, zero, or an explicit success indicator.

Usage:
    python ci_checks/check_error_returns_success.py src/
"""
import ast
import sys
from pathlib import Path


# Method names commonly associated with external system calls.
EXTERNAL_CALL_INDICATORS = {
    "query", "fetch", "get", "execute", "request", "search",
    "find", "scan", "lookup", "retrieve", "load", "read",
}

# Return values that are ambiguous with successful empty results.
AMBIGUOUS_EMPTY_RETURNS = {
    "[]", "{}", '""', "''", "None", "0", "0.0", "False",
}

# Patterns in return dicts that indicate false success signaling.
FALSE_SUCCESS_KEYS = {"ok", "success", "status", "error"}


def _is_ambiguous_return(node: ast.Return) -> bool:
    if node.value is None:
        return True
    # Return []
    if isinstance(node.value, ast.List) and len(node.value.elts) == 0:
        return True
    # Return {}
    if isinstance(node.value, ast.Dict) and len(node.value.keys) == 0:
        return True
    # Return None (as Name node)
    if isinstance(node.value, ast.Constant) and node.value.value is None:
        return True
    # Return "" or ''
    if isinstance(node.value, ast.Constant) and node.value.value == "":
        return True
    # Return 0 or 0.0
    if isinstance(node.value, ast.Constant) and node.value.value == 0:
        return True
    return False


def _has_external_call(func_node: ast.FunctionDef) -> bool:
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            # Check for method calls: obj.query(), obj.fetch(), etc.
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in EXTERNAL_CALL_INDICATORS:
                    return True
    return False


def check_file(path: Path) -> list[str]:
    violations = []
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return violations

    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _has_external_call(func_node):
            continue

        # Walk the function body looking for except handlers.
        for node in ast.walk(func_node):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # Check all return statements inside this handler.
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and _is_ambiguous_return(child):
                    violations.append(
                        f"{path}:{func_node.lineno}: "
                        f"function '{func_node.name}' catches an exception "
                        f"and returns an empty/null value that is ambiguous "
                        f"with a successful empty result. "
                        f"Raise a typed exception or return a Result object instead."
                    )
                    break  # One violation per function is sufficient.

    return violations


def main(target_dirs: list[str]) -> int:
    all_violations: list[str] = []
    for d in target_dirs:
        for py_file in Path(d).rglob("*.py"):
            all_violations.extend(check_file(py_file))

    if all_violations:
        print("ERROR-RETURNS-SUCCESS VIOLATIONS:")
        for v in all_violations:
            print(f"  {v}")
        print(
            f"\n{len(all_violations)} violation(s) found. "
            "Functions that call external systems must not swallow exceptions "
            "by returning empty values. Use typed exceptions or Result objects."
        )
        return 1

    print("No error-returns-success violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["."]))
```

**CI pipeline integration:**

```yaml
# .github/workflows/ci.yml (excerpt)
- name: Error-returns-success static analysis
  run: python ci_checks/check_error_returns_success.py src/ agents/ pipeline/
```

### 4.3 Runtime Production

Instrument external-system call functions with a result quality monitor that distinguishes between success-with-empty and failure-masked-as-empty by tracking result set size distributions and alerting when the empty-result rate spikes anomalously.

```python
# monitoring/result_quality.py
"""
Runtime monitor for external-system call result quality.

Wraps a data-fetching function and tracks:
  - Call count
  - Exception count (actual exceptions that propagate)
  - Empty-result count (successful calls returning empty collections)
  - Non-empty result count

Emits a warning when the empty-result rate within a rolling window
exceeds a configured threshold, which may indicate masked failures.
"""
import functools
import logging
import time
from collections import deque
from typing import Any, Callable

logger = logging.getLogger("monitoring.result_quality")


class ResultQualityMonitor:
    """
    Decorator/wrapper that monitors the empty-result rate of a
    data-fetching function and warns when it spikes.

    Usage:
        monitor = ResultQualityMonitor(
            name="fetch_candidates",
            empty_rate_threshold=0.3,   # warn if >30% of calls return empty
            window_seconds=300,          # 5-minute rolling window
        )

        @monitor.wrap
        def fetch_candidates(user_id: str, context: dict) -> list[dict]:
            ...
    """

    def __init__(
        self,
        name: str,
        empty_rate_threshold: float = 0.25,
        window_seconds: float = 300.0,
    ):
        self._name = name
        self._threshold = empty_rate_threshold
        self._window_seconds = window_seconds
        # Each entry: (timestamp, is_empty: bool)
        self._history: deque[tuple[float, bool]] = deque()

    def _record(self, is_empty: bool) -> None:
        now = time.monotonic()
        self._history.append((now, is_empty))
        # Evict entries outside the window.
        cutoff = now - self._window_seconds
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

        if len(self._history) < 10:
            # Not enough data to compute a meaningful rate.
            return

        empty_count = sum(1 for _, e in self._history if e)
        total = len(self._history)
        empty_rate = empty_count / total

        if empty_rate > self._threshold:
            logger.warning(
                "RESULT_QUALITY_ALERT name=%s empty_rate=%.1f%% "
                "threshold=%.1f%% window_calls=%d window_seconds=%.0f "
                "— high empty-result rate may indicate masked failures.",
                self._name,
                empty_rate * 100,
                self._threshold * 100,
                total,
                self._window_seconds,
            )

    def wrap(self, func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)
            is_empty = (
                result is None
                or result == []
                or result == {}
                or result == ""
                or (hasattr(result, "__len__") and len(result) == 0)
            )
            self._record(is_empty)
            return result
        return wrapper
```

---

## 5. Fix

### 5.1 Immediate Fix

Replace the ambiguous empty return with an explicit exception. This is the minimum correct fix: it stops the silent propagation and makes the failure visible to standard error monitoring.

```python
# BEFORE — swallowed exception (DO NOT USE)
def fetch_candidates(user_id: str, context: dict) -> list[dict]:
    try:
        results = vector_db.query(
            user_id=user_id,
            embedding=context["embedding"],
            top_k=50,
        )
        return results
    except Exception as e:
        logger.debug("fetch_candidates failed: %s", e)
        return []


# AFTER — exception propagates (immediate fix)
import logging

logger = logging.getLogger(__name__)


class RetrievalError(Exception):
    """Raised when the candidate retrieval step fails."""


def fetch_candidates(user_id: str, context: dict) -> list[dict]:
    try:
        results = vector_db.query(
            user_id=user_id,
            embedding=context["embedding"],
            top_k=50,
        )
        return results
    except Exception as e:
        # Log at ERROR level so the failure is visible to monitoring.
        logger.error(
            "fetch_candidates FAILED for user_id=%s: %s",
            user_id, e,
            exc_info=True,
        )
        # Re-raise as a typed domain exception so callers can
        # catch RetrievalError specifically without catching all exceptions.
        raise RetrievalError(f"Candidate retrieval failed for user {user_id}") from e
```

### 5.2 Robust Fix

Use a typed `Result` object that carries both the outcome status and the data or error, forcing every caller to inspect the status before using the data. This pattern is particularly effective in pipeline architectures where intermediate results are passed between stages.

```python
"""
pipeline/result.py

Typed result container for pipeline stage outputs.

Forces callers to explicitly handle both success and failure
cases, eliminating the ambiguity between "success with empty data"
and "failure returning empty data."

Usage:
    result = fetch_candidates(user_id, context)
    if result.is_error:
        handle_retrieval_error(result.error)
        return
    candidates = result.data   # guaranteed non-error at this point
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Result(Generic[T]):
    """
    Typed outcome container.

    Exactly one of `data` or `error` is populated.
    `is_error` is the authoritative check.
    """
    _data: T | None = field(default=None, repr=True)
    _error: Exception | None = field(default=None, repr=True)
    _error_message: str = field(default="", repr=True)

    @classmethod
    def ok(cls, data: T) -> Result[T]:
        return cls(_data=data)

    @classmethod
    def err(cls, error: Exception, message: str = "") -> Result[T]:
        return cls(_error=error, _error_message=message or str(error))

    @property
    def is_error(self) -> bool:
        return self._error is not None

    @property
    def data(self) -> T:
        if self.is_error:
            raise RuntimeError(
                f"Attempted to access .data on an error Result. "
                f"Error was: {self._error_message}. "
                f"Always check .is_error before accessing .data."
            )
        return self._data  # type: ignore[return-value]

    @property
    def error(self) -> Exception:
        if not self.is_error:
            raise RuntimeError("Attempted to access .error on a successful Result.")
        return self._error  # type: ignore[return-value]

    @property
    def error_message(self) -> str:
        return self._error_message
```

**Updated retrieval function using `Result`:**

```python
# pipeline/retrieval.py
import logging
from pipeline.result import Result

logger = logging.getLogger(__name__)


class RetrievalError(Exception):
    """Raised when the candidate retrieval step fails."""


def fetch_candidates(user_id: str, context: dict) -> Result[list[dict]]:
    """
    Fetch candidate products for a user from the vector database.

    Returns:
        Result.ok(candidates) — list may be empty if no candidates match.
        Result.err(error)     — query failed; caller must handle this case.

    The caller MUST check result.is_error before using result.data.
    An empty list in result.data means "no candidates found," not "query failed."
    """
    try:
        candidates = vector_db.query(
            user_id=user_id,
            embedding=context["embedding"],
            top_k=50,
        )
        logger.info(
            "fetch_candidates OK user_id=%s candidate_count=%d",
            user_id, len(candidates),
        )
        return Result.ok(candidates)
    except Exception as e:
        logger.error(
            "fetch_candidates FAILED user_id=%s error=%s",
            user_id, e,
            exc_info=True,
        )
        return Result.err(
            RetrievalError(str(e)),
            message=f"Vector DB query failed for user {user_id}: {e}",
        )
```

**Updated pipeline node that handles the Result correctly:**

```python
# pipeline/nodes.py
import logging
from pipeline.retrieval import fetch_candidates
from pipeline.result import Result

logger = logging.getLogger(__name__)


def retrieval_node(state: dict) -> dict:
    """
    LangGraph node: retrieves candidates for the current user.

    On retrieval failure, sets pipeline state to error and routes
    to the error handler node instead of the ranking node.
    This makes failure explicit and prevents phantom data from
    propagating through the pipeline.
    """
    user_id = state["user_id"]
    context = state["context"]

    result: Result[list[dict]] = fetch_candidates(user_id, context)

    if result.is_error:
        # Failure is explicit in the state. Downstream nodes check
        # this flag and do not attempt to rank or filter empty data.
        return {
            **state,
            "candidates": [],
            "retrieval_failed": True,
            "retrieval_error": result.error_message,
        }

    return {
        **state,
        "candidates": result.data,
        "retrieval_failed": False,
        "retrieval_error": None,
    }


def ranking_node(state: dict) -> dict:
    """
    LangGraph node: ranks candidates.

    Refuses to rank if the retrieval step failed.
    """
    if state.get("retrieval_failed"):
        logger.warning(
            "ranking_node skipped: upstream retrieval failed for user_id=%s. "
            "Error: %s",
            state.get("user_id"),
            state.get("retrieval_error"),
        )
        return {**state, "ranked_candidates": [], "ranking_skipped": True}

    candidates = state["candidates"]
    if not candidates:
        # Genuine empty result: no candidates matched. This is valid.
        return {**state, "ranked_candidates": [], "ranking_skipped": False}

    ranked = _rank(candidates, state["context"])
    return {**state, "ranked_candidates": ranked, "ranking_skipped": False}
```

---

## 6. Architectural Prevention

**Principle: failure must be represented as a distinct type, not as a value in the success domain.**

1. **Adopt the Result type pattern at pipeline stage boundaries.** Every function that crosses a system boundary (database, external API, cache, vector store, message queue) must return a `Result[T]` rather than `T`. This is a structural enforcement: the caller cannot accidentally treat the return value as data without first checking `is_error`. This principle is equivalent to Rust's `Result<T, E>`, Haskell's `Either`, and Go's `(T, error)` idiom.

2. **Define a canonical empty-result type for each domain.** If "no recommendations found" is a valid and meaningful state, represent it with a dedicated type (`EmptyRecommendations`) rather than an empty list. A pipeline stage that receives `EmptyRecommendations` knows exactly what it means; a pipeline stage that receives `[]` does not.

3. **Prohibit silent exception swallowing in code review.** Any `except` block that does not re-raise, does not raise a typed exception, and does not return a `Result.err()` object is a code review rejection. This rule must be explicit in the team's code review guidelines.

4. **Instrument result cardinality at every pipeline stage.** Track and alert on: result set size distribution, empty-result rate, p99 result count. These are data quality metrics. An anomalous spike in empty-result rate is a leading indicator of masked failures — it often precedes user-facing impact by minutes. Set an alert threshold: if the empty-result rate for `fetch_candidates` exceeds 15% over any 5-minute window, page on-call.

5. **Do not cache error states.** The caching layer must not cache empty results without TTL validation or a result-type check. Cache keys for empty results should use a shorter TTL than cache keys for non-empty results, and the cache layer must be aware of the distinction between `Result.ok([])` and `Result.err(...)`.

```python
# caching/candidate_cache.py
"""
Caching wrapper that applies differentiated TTLs based on result quality.

Empty results from successful queries are cached with a short TTL
(the empty result may change as inventory updates).
Error results are never cached (they should not be served to users).
Non-empty results are cached with the standard TTL.
"""
import json
import logging
import time
from pipeline.result import Result

logger = logging.getLogger("caching.candidate_cache")

# Seconds
TTL_NONEMPTY = 3600       # 1 hour for real results
TTL_EMPTY_SUCCESS = 60    # 1 minute for genuine empty results
TTL_ERROR = 0             # Do not cache error results


class CandidateCache:
    def __init__(self, redis_client):
        self._redis = redis_client

    def get(self, user_id: str) -> Result[list[dict]] | None:
        """Returns None on cache miss."""
        key = f"candidates:{user_id}"
        raw = self._redis.get(key)
        if raw is None:
            return None
        payload = json.loads(raw)
        if payload.get("is_error"):
            # Defensive: if an error result was somehow cached, treat as miss.
            logger.warning("Found cached error result for user_id=%s — ignoring.", user_id)
            return None
        return Result.ok(payload["data"])

    def set(self, user_id: str, result: Result[list[dict]]) -> None:
        key = f"candidates:{user_id}"
        if result.is_error:
            logger.debug(
                "Not caching error result for user_id=%s: %s",
                user_id, result.error_message,
            )
            return

        data = result.data
        ttl = TTL_NONEMPTY if data else TTL_EMPTY_SUCCESS
        payload = json.dumps({"is_error": False, "data": data})
        self._redis.setex(key, ttl, payload)
        logger.debug(
            "Cached %d candidates for user_id=%s ttl=%ds",
            len(data), user_id, ttl,
        )
```

---

## 7. Anti-patterns to Avoid

**Anti-pattern 1: `except Exception: return []` in any external-system call.**
This is the canonical form of the pattern. It is never correct to return an empty collection in response to an exception from an external system. The exception must propagate (as itself, or wrapped in a typed domain exception) or be explicitly represented in a `Result` object.

**Anti-pattern 2: Logging at DEBUG inside an except block and returning normally.**
`logger.debug("query failed: %s", e)` followed by `return []` does two damaging things: it hides the failure from error monitoring (DEBUG is typically filtered in production log aggregation), and it hides the failure from the caller. Log at ERROR minimum. Do not return a success value.

**Anti-pattern 3: Using `None` as both "not yet computed" and "error occurred."**
`None` already serves as "not present" in Python's type system. Using it additionally to mean "an error occurred during computation" collapses three distinct states — "not computed," "computed successfully with no value," and "computation failed" — into one. Use a typed Result object.

**Anti-pattern 4: Trusting result cardinality without validating freshness.**
Even if `fetch_candidates()` raises correctly, a stale cache may return real data from a previous successful call. Data freshness is separate from data correctness. Monitor result cardinality across time; a sustained zero-count for a user who historically receives non-zero results is a signal even if no exceptions are logged.

**Anti-pattern 5: Catching and ignoring `KeyError` or `IndexError` in pipeline state access.**
`state.get("candidates", [])` silently returns `[]` if `"candidates"` was never set in the state (for example, because the retrieval node errored and returned early without setting the key). This is the same pattern in a different form. Pipeline nodes must assert that required state keys are present and non-null, or use explicit default handling that logs the missing key.

**Anti-pattern 6: Integration tests that only test the happy path.**
An integration test that sends a request to a pipeline with a healthy database and asserts a non-empty response does not cover this pattern. Integration tests must include a scenario where the external dependency returns an error, and must assert that the pipeline returns an error response — not an empty success response.

---

## 8. Edge Cases and Variants

**Variant A: Function returns `{"ok": True, "data": []}` on exception.**
A structured response that explicitly sets `ok=True` during exception handling is more insidious than returning `[]` because it is a deliberate false assertion. The caller checks `response["ok"]` and proceeds. Detection: grep for `ok.*True` or `success.*True` inside `except` blocks.

**Variant B: HTTP client returns status 200 with empty body on network error.**
An HTTP client library that catches a `requests.Timeout` exception and returns a `Response` object with `status_code=200` and `content=b""` would cause any caller that checks `response.status_code == 200` to proceed with empty content. This is a variant in HTTP client wrappers. Detection: audit HTTP wrapper classes for exception handlers that construct synthetic response objects.

**Variant C: Async function swallows exceptions via `asyncio.gather(return_exceptions=True)`.**
`asyncio.gather(*tasks, return_exceptions=True)` returns exceptions as values in the results list rather than raising them. Code that iterates over results without checking `isinstance(r, Exception)` silently discards failures. All callers of `asyncio.gather(..., return_exceptions=True)` must inspect each result.

**Variant D: LangChain tool returns error message as tool output string.**
A LangChain `@tool` function that catches an exception and returns `"Error: database unavailable"` as a string rather than raising passes the error string to the LLM as if it were data. The LLM may incorporate the error string into its response ("I couldn't retrieve your order because the database was unavailable") or may hallucinate data to fill the gap. Tools must raise `ToolException` on failure.

**Variant E: Retry logic that returns the last empty result after exhausting retries.**
A retry wrapper that retries a failing query N times and, after all retries are exhausted, returns the last return value (which was `[]` from the last failed attempt) instead of raising. The retry logic itself swallows the failure. Retry wrappers must re-raise the last exception after exhausting retries, not return the last return value.

**Variant F: Database ORM returning empty queryset vs. raising on connection failure.**
Django ORM's `Model.objects.filter()` returns an empty queryset if no records match, and also returns an empty queryset-like object in some misconfiguration scenarios. The distinction between "no records" and "query failed to execute" is not always surfaced at the ORM level. Wrap ORM calls in a service layer that explicitly validates that the query executed (by checking connection state or using `connection.ensure_connection()` before queries).

---

## 9. Audit Checklist

Use this checklist during code review of any function that calls an external system.

- [ ] **No `except` block returns `[]`, `{}`, `None`, `""`, or `0`** in a function that queries an external system.
- [ ] **No `except` block returns a dict with `ok=True`, `success=True`, or `status=200`** in a function that may fail.
- [ ] **All exceptions are logged at `ERROR` level or above** with `exc_info=True` to capture the stack trace.
- [ ] **Exceptions propagate as typed domain exceptions** (e.g., `RetrievalError`, `QueryFailedError`) or are represented in a `Result.err()` object.
- [ ] **Callers of external-system functions handle both the empty-result case and the error case with distinct code paths.**
- [ ] **Pipeline state keys set by a stage are asserted to be present by downstream stages**, not accessed with `.get(key, [])` silently.
- [ ] **Result cardinality metrics are instrumented**: call count, empty-result count, error count are tracked and alerted on separately.
- [ ] **Caching layer applies differentiated TTLs**: error results are not cached; empty successful results use a shorter TTL than non-empty results.
- [ ] **Integration tests cover the failure path**: at least one test injects an external dependency failure and asserts the pipeline returns an error, not an empty success response.
- [ ] **`asyncio.gather(return_exceptions=True)` results are inspected** for `isinstance(r, Exception)` before use.
- [ ] **LangChain `@tool` functions raise `ToolException`** rather than returning error strings.
- [ ] **HTTP wrapper classes do not construct synthetic 200 responses** from exception handlers.
- [ ] **CI gate `check_error_returns_success.py` passes** for all modified files.

---

## 10. Further Reading

**Primary references:**

- Nygard, M. *Release It! Design and Deploy Production-Ready Software*, 2nd ed. (Pragmatic Programmers, 2018). Chapter 4: "Stability Antipatterns" — the "Cascading Failures" section directly describes how silent failure in one layer propagates to corrupt downstream layers.
- Raymond, E. S. *The Art of Unix Programming* (Addison-Wesley, 2003). Rule of Repair: "Repair what you can — but when you must fail, fail noisily and as soon as possible." The error-returns-success pattern is a direct violation of this principle.
- Klabnik, S. and Nichols, C. *The Rust Programming Language* (No Starch Press, 2019). Chapter 9: "Error Handling." Rust's `Result<T, E>` type is the canonical language-level solution to this pattern — it makes handling the error case mandatory.
- Fowler, M. "Notification" pattern (martinfowler.com). A `Notification` object accumulates errors and passes them to the caller, similar to the `Result` pattern described in this document.

**Related patterns in this playbook:**

- Pattern #18 — Snapshot vs. Sustained Check: a monitoring function that returns incorrect health status due to measurement methodology rather than exception swallowing — related in that the monitoring signal is wrong.
- Pattern #19 — False Positive Crash Detection: a watchdog that misclassifies a success state as a failure — the inverse direction of this pattern (success classified as failure vs. failure classified as success).
- Pattern #30 — Critical Module Never Imported: a safety module that is absent from the call path — related in that a safety signal (the exception, the error flag) that should have been generated is never generated.
- Pattern #16 — Missing Guard: a guard condition that evaluates to no-op and allows unsafe data through — structurally similar in that protective logic is bypassed, allowing invalid data to propagate downstream.
