# Pattern #34 — Handle Recreated Every Call

**Category:** I/O & Persistence
**Severity:** Medium
**Tags:** `io`, `persistence`, `connection-pool`, `http-session`, `performance`, `resource-lifecycle`

---

## 1. Observable Symptoms

**Cycle time grows linearly with call count.** A function that "works" in isolation becomes the bottleneck in a high-frequency pipeline. Profiling reveals that the top contributor to wall-clock time is not the function's primary computation but rather its initialization code: `requests.Session()`, `psycopg2.connect()`, `boto3.client()`, `ModelLoader.load()`. Each of these operations takes 50–500 ms. When called 100 times per cycle, the initialization cost alone is 5–50 seconds, which exceeds most acceptable cycle periods.

**Connection count at the database or service endpoint is disproportionately high.** If the heavy resource is a database connection, the database server will show one new connection opened and immediately closed per function call. A function called 100 times per cycle will open 100 connections per cycle. Connection counts in database monitoring dashboards (e.g., `pg_stat_activity`) will be orders of magnitude higher than the application's actual concurrency.

**TLS handshake cost appears in network traces.** For HTTP clients (e.g., `requests.Session`, `httpx.Client`), each new session must negotiate a TLS handshake. In network traces, the application shows repeated full TLS round trips where persistent connections should be reused. This is visible in tools like Wireshark or in service mesh metrics showing high per-request connection setup time.

**ML model load time dominates inference latency.** For functions that load an ML model or LLM client on every call, model deserialization (reading weights from disk or memory-mapping a file) dominates inference time. The model "loads" correctly each time; the problem is that it should be loaded once and reused.

**Resource exhaustion under moderate load.** File descriptor limits, connection pool limits, or GPU memory limits are hit under load levels that should be well within capacity, because each concurrent invocation is holding its own handle to the resource instead of sharing a pooled one.

---

## 2. Field Story

A search engine indexing pipeline processed documents through a multi-stage function chain. One stage, `extract_entities()`, called an HTTP-based named-entity recognition (NER) service to tag documents with people, organizations, and locations. The function was written as a self-contained utility:

```python
def extract_entities(text: str) -> list[dict]:
    client = httpx.Client(timeout=10.0)          # created here
    response = client.post(NER_SERVICE_URL, json={"text": text})
    return response.json()["entities"]
```

During initial development, documents were processed in small batches and the function ran 5–10 times per minute. The overhead of creating a new `httpx.Client` on each call was approximately 80 ms (TLS handshake included) and was not perceptible.

When the pipeline was scaled to process 200 documents per minute, `extract_entities()` was called 200 times per minute. The 80 ms per-call overhead accumulated to 16 seconds of TLS handshake time per minute — 27% of the available processing time — spent on work that could have been amortized across all calls with a single persistent client.

The NER service operator noticed that the indexing pipeline was opening and closing approximately 200 TCP connections per minute to their endpoint. Their load balancer logged each connection as a separate "session," and their billing model charged per session. The pipeline owner received an unexpected invoice and a request to reduce connection churn.

The fix — moving `httpx.Client` instantiation to module level and reusing it across calls — reduced per-document latency from 180 ms to 95 ms, eliminated 200 TLS handshakes per minute, and reduced the NER service's connection log volume by 99%.

---

## 3. Technical Root Cause

The root cause is misplaced resource lifecycle management. A heavy resource has two phases: initialization (expensive, O(100ms)) and use (cheap, O(1ms)). When initialization is placed inside a function that is called repeatedly, the expensive phase runs on every call instead of once.

```python
# Buggy pattern: resource created on every call
import httpx

NER_SERVICE_URL = "https://ner.internal/extract"

def extract_entities(text: str) -> list[dict]:
    client = httpx.Client(timeout=10.0)   # 80ms TLS handshake every call
    response = client.post(NER_SERVICE_URL, json={"text": text})
    return response.json()["entities"]
```

This pattern typically emerges from three causes:

1. **Copy-paste from a script.** The function was written as a standalone script where initialization at the top of the file is idiomatic. When the script is refactored into a function, the initialization moves into the function body without the author noticing the lifecycle implication.

2. **Desire for encapsulation without lifecycle awareness.** The developer wants `extract_entities()` to be self-contained ("just pass it a string, get back entities"). Self-containment is a good goal, but it does not require creating a new resource on every call. A module-level or injected resource achieves both self-containment and reuse.

3. **Testing convenience.** Creating the resource inside the function makes it easy to test in isolation without mocking. But the test convenience comes at a severe production cost.

The secondary cause is the absence of a performance regression test. Because the function "works" (returns correct results), tests pass. No test measures per-call latency or connection count.

---

## 4. Detection

### 4.1 Manual Code Audit

Search for instantiations of known heavy resources inside function bodies. High-risk patterns:

```
httpx.Client(
requests.Session(
psycopg2.connect(
sqlite3.connect(
boto3.client(
boto3.resource(
openai.OpenAI(
anthropic.Anthropic(
torch.load(
joblib.load(
open(   # when the file is large or opened in a tight loop
```

For each match, determine whether the function containing the instantiation is called more than once per cycle. If yes, the instantiation should be moved to a higher scope.

Use a `grep` or `semgrep` rule to find these patterns:

```yaml
# semgrep rule: heavy resource created inside function body
rules:
  - id: handle-recreated-every-call
    patterns:
      - pattern: |
          def $FUNC(...):
              ...
              $VAR = $HEAVY_CLASS(...)
              ...
    pattern-where-either:
      - pattern: $HEAVY_CLASS(...)
        where: $HEAVY_CLASS in [httpx.Client, requests.Session, psycopg2.connect,
                                 boto3.client, openai.OpenAI, anthropic.Anthropic]
    message: >
      Heavy resource $HEAVY_CLASS instantiated inside function $FUNC.
      Consider moving to module scope or injecting via dependency injection.
    languages: [python]
    severity: WARNING
```

### 4.2 Automated CI/CD

Add a performance benchmark test that calls the function N times (N = 50) and asserts that the per-call average latency is below a threshold. If the resource is created inside the function, the latency will be dominated by initialization cost and will exceed the threshold:

```python
import time
import pytest

def test_extract_entities_per_call_latency():
    texts = [f"Document {i} mentions Alice and Acme Corp." for i in range(50)]
    start = time.perf_counter()
    for text in texts:
        extract_entities(text)
    elapsed = time.perf_counter() - start
    avg_ms = (elapsed / 50) * 1000
    # If client is recreated each call, avg_ms will be ~180ms (80ms init + 100ms request)
    # If client is reused, avg_ms will be ~100ms (request only)
    assert avg_ms < 120, f"Per-call latency {avg_ms:.1f}ms suggests resource recreation"
```

This test is not a unit test in the traditional sense; it is a regression guard for a performance contract.

### 4.3 Runtime Production

Instrument resource creation with a counter metric. Emit `resource_created_total{resource="httpx_client"}` every time a new client is instantiated. The expected production rate is approximately 1 per application start (module-level) or 1 per worker process start. If the counter increments at a rate proportional to request rate, the resource is being recreated on every call.

```python
import httpx
from prometheus_client import Counter

CLIENT_CREATED = Counter("httpx_client_created_total", "Number of httpx.Client instances created")

def extract_entities(text: str) -> list[dict]:
    CLIENT_CREATED.inc()  # temporary diagnostic counter — should fire once, not per call
    client = httpx.Client(timeout=10.0)
    response = client.post(NER_SERVICE_URL, json={"text": text})
    return response.json()["entities"]
```

If `httpx_client_created_total` increments every call, the bug is confirmed. Remove the counter after fixing.

---

## 5. Fix

### 5.1 Immediate

Move the resource instantiation to module scope. The resource is created once when the module is imported and reused across all calls:

```python
import httpx

NER_SERVICE_URL = "https://ner.internal/extract"
_ner_client = httpx.Client(timeout=10.0)   # created once at module load

def extract_entities(text: str) -> list[dict]:
    response = _ner_client.post(NER_SERVICE_URL, json={"text": text})
    return response.json()["entities"]
```

The leading underscore on `_ner_client` signals that it is module-private. This is the correct pattern for resources that are stateless (or whose state is managed by the resource itself, as is the case for HTTP connection pools).

### 5.2 Robust

For resources that require explicit lifecycle management (setup and teardown), use dependency injection combined with a context manager or application startup/shutdown hooks:

```python
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator, Optional

import httpx

logger = logging.getLogger(__name__)

NER_SERVICE_URL = "https://ner.internal/extract"


class NERClient:
    """
    Wrapper around the NER HTTP service.
    Manages a single persistent httpx.Client across all calls.
    The client is created once on construction and closed on shutdown().
    """

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url
        self._client = httpx.Client(timeout=timeout)
        self._call_count = 0
        logger.info("NERClient initialized with persistent connection to %s", base_url)

    def extract_entities(self, text: str) -> list[dict]:
        self._call_count += 1
        response = self._client.post(self._base_url, json={"text": text})
        response.raise_for_status()
        return response.json()["entities"]

    def shutdown(self) -> None:
        logger.info("NERClient shutting down after %d calls", self._call_count)
        self._client.close()

    def __enter__(self) -> "NERClient":
        return self

    def __exit__(self, *_) -> None:
        self.shutdown()


# Module-level singleton for use in non-DI contexts
_default_ner_client: Optional[NERClient] = None


def get_ner_client() -> NERClient:
    global _default_ner_client
    if _default_ner_client is None:
        _default_ner_client = NERClient(NER_SERVICE_URL)
    return _default_ner_client


def extract_entities(text: str, client: Optional[NERClient] = None) -> list[dict]:
    """
    Extract named entities from text.
    If client is not provided, uses the module-level singleton.
    Callers that manage their own lifecycle (e.g., tests) can inject a client.
    """
    ner = client or get_ner_client()
    return ner.extract_entities(text)


@contextmanager
def indexing_pipeline(documents: list[str]) -> Generator[None, None, None]:
    """
    Context manager that initializes the NER client once, processes all
    documents, and ensures the client is closed on exit.
    """
    with NERClient(NER_SERVICE_URL) as ner:
        for doc in documents:
            entities = ner.extract_entities(doc)
            yield entities   # caller processes each result
        logger.info("Pipeline complete. Total NER calls: %d", ner._call_count)


# --- demonstration ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # The client is created once. All 200 calls reuse the same TCP connection.
    client = NERClient(NER_SERVICE_URL)
    documents = [f"Document {i}: Alice met Bob at Acme Corp." for i in range(200)]

    import time
    start = time.perf_counter()
    results = [client.extract_entities(doc) for doc in documents]
    elapsed = time.perf_counter() - start

    client.shutdown()
    print(f"Processed {len(documents)} documents in {elapsed:.2f}s")
    print(f"Average per-document: {elapsed / len(documents) * 1000:.1f}ms")
    # With reuse: ~100ms avg. Without reuse: ~180ms avg.
```

---

## 6. Architectural Prevention

**Resource lifecycle must be explicit and visible.** Resources with significant initialization cost must be created at application startup (or module import) and destroyed at application shutdown. They must not be created inside hot-path functions. Document this constraint in the module's docstring and in the function's parameter list (injection pattern).

**Use dependency injection for testability without per-call recreation.** If a function needs a resource, accept it as a parameter with a default of `None` that falls back to a module-level singleton. This allows tests to inject mock resources without paying the initialization cost in production.

**Connection pooling for database resources.** For database connections, always use a connection pool (`sqlalchemy.create_engine` with pool settings, `psycopg2.pool.ThreadedConnectionPool`, `asyncpg.create_pool`). Never call `connect()` inside a per-document function.

**ML model loading at startup, not at inference.** For ML models and LLM clients, load once at application startup and store in a module-level variable or a model registry. For multi-model applications, use a lazy-loading registry that initializes each model on first use and caches it thereafter.

**Performance contracts in CI.** For any function that is called more than 10 times per second in production, define and enforce a per-call latency budget in CI. This prevents the "it works, just slowly" class of regressions from reaching production undetected.

---

## 7. Anti-patterns to Avoid

**Do not use `functools.lru_cache` on a function that creates a resource.** `lru_cache` caches based on argument values. A resource-creating function with no arguments will be called only once due to caching, but a function with any argument will create a new resource for each unique argument combination. This is a subtle variant of the same bug.

**Do not close and reopen the resource on every call "to be safe."** Closing a connection after each use and reopening it before the next use is equivalent to recreating it. Use connection pool semantics instead: the pool manages connection health and reuse.

**Do not create resources in `__init__` of a class that is instantiated frequently.** If a class is instantiated per-request or per-document, resources created in `__init__` are recreated per-request. Either use a singleton or move the resource to a class variable shared across instances.

**Do not pass resources through return values.** A function should not return the resource it created as part of its return value so the caller can "reuse it later." This leads to ad-hoc resource management. Use the injection pattern or module-level singleton instead.

**Do not log a resource creation at DEBUG level without also asserting it happens only once.** Many developers add `logger.debug("creating HTTP client")` to resource creation code, then forget about it. In production, this log line firing on every call is a signal of the bug — but only if someone is watching. Use a counter metric instead, which is persistent and alertable.

---

## 8. Edge Cases and Variants

**Thread safety of module-level resources.** Most HTTP clients (`httpx.Client`, `requests.Session`) are thread-safe for concurrent requests but not for concurrent initialization. Use a module-level lock around the singleton initialization in multi-threaded applications: `_lock = threading.Lock()`.

**Async contexts.** In async code, `httpx.AsyncClient` must be created once and shared, just as with the sync client. However, async clients must be closed with `await client.aclose()`, which requires a shutdown hook. Use `asyncio.get_event_loop().run_until_complete(client.aclose())` at process exit or register with the application's lifespan event.

**Resources that expire.** Some resources have a finite lifetime: OAuth tokens expire, database connections time out after idle periods, SSL certificates rotate. The module-level singleton must handle expiration by implementing a refresh mechanism rather than recreating the entire resource. For HTTP clients, connection-level expiration is handled by the underlying HTTP library; token expiration requires application-level refresh logic.

**Fork safety.** If the application forks (e.g., using `multiprocessing` or `gunicorn` with `fork`-based workers), module-level resources created before the fork are shared across worker processes, which causes race conditions. Create resources after the fork in the worker's initialization function, not at module import time.

**Resource creation failures.** When a module-level resource fails to initialize (e.g., the NER service is unreachable at startup), the application will fail to start entirely. This is usually preferable to silently running with degraded behavior. Implement a health check that verifies the resource is live before the application begins accepting requests.

---

## 9. Audit Checklist

- [ ] No instantiation of `httpx.Client`, `requests.Session`, database connection, `boto3.client`, ML model loader, or LLM client inside a function body that is called more than once per application lifecycle.
- [ ] Module-level singletons for heavy resources are documented with their expected lifecycle (initialized once at module import, closed at application shutdown).
- [ ] All functions that require a heavy resource accept it as an optional injected parameter (`client: Optional[NERClient] = None`) with a fallback to the module singleton.
- [ ] A performance benchmark test asserts that per-call latency for any function using a heavy resource is below a documented threshold, and that the threshold is calibrated for reuse (not per-call recreation).
- [ ] A production metric (`resource_created_total`) is emitted at resource creation time; its cardinality in production equals the number of worker processes, not the number of requests.
- [ ] Database access uses a connection pool with explicit pool size and overflow settings; `connect()` is never called in a per-document or per-request function.
- [ ] Thread-safety of module-level resources is documented; initialization in multi-threaded contexts uses a `threading.Lock`.
- [ ] Fork safety is addressed: resources are not created at module import time in applications that use process-based parallelism.
- [ ] Resource expiration (token refresh, idle connection timeout) is handled by a refresh mechanism, not by recreating the resource.
- [ ] Application startup fails with a clear error if a critical module-level resource cannot be initialized.

---

## 10. Further Reading

- Repository with annotated code examples for all patterns in this series: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns)
- `httpx` documentation on connection pooling and client reuse: [python-httpx.org/advanced/clients](https://www.python-httpx.org/advanced/clients/)
- SQLAlchemy connection pool documentation: [docs.sqlalchemy.org/en/20/core/pooling.html](https://docs.sqlalchemy.org/en/20/core/pooling.html)
- Python `threading.local` for per-thread resource instances in multi-threaded applications: [docs.python.org/3/library/threading.html#thread-local-data](https://docs.python.org/3/library/threading.html#thread-local-data)
- "Connection Pooling" — PostgreSQL documentation: [postgresql.org/docs/current/runtime-config-connection.html](https://www.postgresql.org/docs/current/runtime-config-connection.html)
