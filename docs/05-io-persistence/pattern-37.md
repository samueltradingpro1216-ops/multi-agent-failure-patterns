# Pattern #37 — I/O on Hot Path

**Category:** I/O & Persistence
**Severity:** Medium
**Tags:** `performance`, `hot-path`, `disk-io`, `batching`, `latency`

---

## 1. Observable Symptoms

The system is functionally correct but unacceptably slow. Performance symptoms are consistent and reproducible under load, not intermittent.

- End-to-end latency for a single evaluation unit (one transaction scored, one frame analyzed, one event classified) is 10–100x higher than the theoretical compute budget.
- CPU utilization is low or moderate while the process is slow — the bottleneck is not computation.
- `strace` or `perf` reveals a high rate of `write()`, `fsync()`, or `open()` syscalls correlated to each unit of work.
- Profiling with `cProfile` or `py-spy` shows a disproportionate wall-clock time in logging, metric emission, or state persistence functions — not in business logic.
- Disk I/O wait (`%iowait` in `iostat`) is elevated. Disk queue depth is nonzero during normal operation.
- Throughput does not scale linearly with additional CPU cores because the bottleneck is a serialized I/O resource.
- Reducing the evaluation rate (artificially throttling input) improves latency per unit, which confirms the bottleneck is write throughput contention rather than CPU saturation.

---

## 2. Field Story

A financial technology company operated a real-time fraud detection system. The core loop evaluated each incoming payment authorization against a set of rule-based and model-based signals and produced a risk score within a target SLA of 20 milliseconds.

The system worked well at low volume. As transaction throughput scaled toward 200 authorizations per second, the median evaluation latency climbed from 8ms to 180ms. P99 latency exceeded 400ms. The SLA was breached. Chargebacks increased because approvals were being timed out by the upstream card network before the risk score could be returned.

An engineer profiling the system found that the `evaluate()` function was writing a structured JSON log entry to a local file on every call. The write was positioned immediately after score computation, inside the evaluation loop, and was intended to maintain an audit trail for regulatory compliance. Each write took between 2ms and 12ms depending on disk pressure. At 200 evaluations per second, the total I/O time consumed 400–2400ms per second of wall clock — far exceeding the available budget.

The write had been added during a compliance review. The author had measured it at 3ms in a test environment with a fast SSD and no concurrent writers, and judged it acceptable. In production, the disk was a network-attached volume shared among multiple services, and the measured latency was 5–15x higher under concurrent load.

The fix batched writes into a background thread with a bounded queue. Evaluation latency returned to 7–9ms. The audit trail was preserved. The compliance requirement was met.

---

## 3. Technical Root Cause

The defect is the placement of a blocking I/O operation inside a tight evaluation loop. The I/O operation is individually small but executed at a frequency that causes its cumulative cost to dominate cycle time.

```python
import json
import time
import os

AUDIT_LOG_PATH = os.environ.get("AUDIT_LOG_PATH", "/var/log/fraud/audit.jsonl")

def evaluate(transaction: dict) -> float:
    """Score a transaction. Returns risk score in [0.0, 1.0]."""
    score = _compute_score(transaction)  # ~3ms — fast

    # BUG: blocking file write on every call — ~8ms at production volume
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts":             time.time(),
            "transaction_id": transaction["id"],
            "score":          score,
        }) + "\n")

    return score

def _compute_score(transaction: dict) -> float:
    # Placeholder for the actual model inference
    time.sleep(0.003)
    return 0.12
```

The write inside `evaluate()` is a blocking call. The calling thread cannot proceed until the OS has accepted the data into the write buffer or, if `O_SYNC` / `fsync` is in use, until the data has been flushed to the physical medium.

**Why the write is slow in production and fast in development.** A local NVMe SSD may complete a single append in under 1ms. A network-attached filesystem (NFS, EBS, Azure Files) may take 5–20ms for the same operation because the write traverses a network round-trip. Multi-tenant storage adds queuing jitter. The function was benchmarked on a developer laptop and approved; the production environment was categorically different.

**Why `with open(..., "a")` is especially expensive in a loop.** Opening a file in append mode on every call issues an `open()` syscall, a `write()` syscall, and a `close()` syscall per entry. The `close()` may flush kernel buffers to the storage layer. Keeping the file open across calls eliminates two of the three syscalls but does not eliminate the fundamental problem of a synchronous write in the hot path.

**Measurement illusion.** A single call to `evaluate()` in a unit test with no concurrent I/O may measure at 3–4ms total. At 200 calls per second with concurrent I/O from other services on the same volume, the write alone measures 8–15ms. The function's performance is not a fixed property — it is load-dependent and environment-dependent.

---

## 4. Detection

### 4.1 Profiling the Hot Path

Use `cProfile` to identify which functions in the evaluation loop consume disproportionate wall-clock time. Sort by `cumtime` (cumulative time) to expose I/O-heavy callees that are not immediately visible at the top of the call stack.

```python
import cProfile
import pstats
import io
import json
import time
import os

AUDIT_LOG_PATH = "/tmp/audit_test.jsonl"

def evaluate_with_io(transaction: dict) -> float:
    score = 0.12  # simulated compute
    time.sleep(0.003)
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": transaction["id"], "score": score}) + "\n")
    return score

def profile_hot_path(n_iterations: int = 500) -> None:
    """
    Profile n_iterations of evaluate() and print functions that consume
    more than 5% of total wall-clock time.
    """
    transactions = [{"id": f"txn_{i}", "amount": 100 + i} for i in range(n_iterations)]

    profiler = cProfile.Profile()
    profiler.enable()

    for txn in transactions:
        evaluate_with_io(txn)

    profiler.disable()

    stream = io.StringIO()
    stats  = pstats.Stats(profiler, stream=stream).sort_stats("cumulative")
    stats.print_stats(20)
    output = stream.getvalue()

    total_time = sum(
        row[3] for row in stats.stats.values()
        if row[3] > 0
    )

    print("=== Hot Path Profile ===")
    for line in output.splitlines():
        print(line)

    print(f"\nTotal profiled time: {total_time:.3f}s over {n_iterations} iterations")
    print(f"Avg per iteration:   {total_time / n_iterations * 1000:.2f}ms")

if __name__ == "__main__":
    profile_hot_path()
```

### 4.2 Static Detection: I/O Inside Loop Bodies

Parse the AST to detect `open()`, `logging.*`, and `json.dump*` calls that appear directly inside `for` or `while` loop bodies at any nesting depth within functions decorated or named as evaluators.

```python
import ast
import sys
from pathlib import Path

IO_CALL_NAMES = {"open", "write", "dump", "dumps", "fsync", "flush"}
LOG_ATTRS     = {"info", "debug", "warning", "error", "critical", "exception"}

def find_io_in_loops(source_path: str) -> list[dict]:
    """
    Report I/O-producing calls found inside loop bodies in the given file.
    """
    source = Path(source_path).read_text(encoding="utf-8")
    tree   = ast.parse(source)
    findings = []

    class LoopIOVisitor(ast.NodeVisitor):
        def __init__(self):
            self._in_loop_depth = 0

        def _enter_loop(self, node):
            self._in_loop_depth += 1
            self.generic_visit(node)
            self._in_loop_depth -= 1

        visit_For   = _enter_loop
        visit_While = _enter_loop

        def visit_Call(self, node):
            if self._in_loop_depth == 0:
                self.generic_visit(node)
                return
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr

            if name in IO_CALL_NAMES or name in LOG_ATTRS:
                findings.append({
                    "file": source_path,
                    "line": node.lineno,
                    "call": name,
                })
            self.generic_visit(node)

    LoopIOVisitor().visit(tree)
    return findings

if __name__ == "__main__":
    for path in sys.argv[1:]:
        for finding in find_io_in_loops(path):
            print(
                f"[IO-IN-LOOP] {finding['file']}:{finding['line']} "
                f"— call to '{finding['call']}' inside loop body"
            )
```

### 4.3 Runtime Latency Instrumentation

Wrap the hot-path function with a decorator that records the time spent in I/O sub-calls relative to the total function time and emits a warning when the ratio exceeds a threshold.

```python
import time
import functools
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_io_time_budget_seconds = 0.002  # 2ms per call is the max acceptable I/O overhead

@contextmanager
def measure_io_time(label: str):
    """Context manager that measures elapsed time and logs it."""
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    if elapsed > _io_time_budget_seconds:
        logger.warning(
            "IO_OVERHEAD_EXCEEDED: label=%s elapsed_ms=%.2f budget_ms=%.2f",
            label, elapsed * 1000, _io_time_budget_seconds * 1000,
        )

def hot_path_io_guard(io_budget_ms: float = 2.0):
    """
    Decorator. Measures total function time and separately measures any
    explicitly-guarded I/O sub-operations. Logs a warning if I/O fraction
    exceeds 25% of total call time.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            t0     = time.perf_counter()
            result = func(*args, **kwargs)
            total  = time.perf_counter() - t0
            if total * 1000 > io_budget_ms * 4:
                logger.warning(
                    "HOT_PATH_SLOW: func=%s total_ms=%.2f budget_ms=%.2f",
                    func.__qualname__, total * 1000, io_budget_ms,
                )
            return result
        return wrapper
    return decorator
```

---

## 5. Fix

### 5.1 Immediate Fix: Background Write Queue

Move the I/O operation off the hot path by placing audit log entries on a bounded `queue.Queue` and draining the queue from a dedicated background thread. The evaluation thread never blocks on disk.

```python
import json
import queue
import threading
import time
import os
import logging
import atexit
from typing import Optional

logger = logging.getLogger(__name__)

class AuditLogger:
    """
    Thread-safe audit logger that writes to disk from a dedicated background
    thread. The evaluation hot path enqueues a dict and returns immediately.

    If the queue is full (maxsize exceeded), the entry is dropped and a counter
    is incremented so that data loss is visible in metrics.
    """

    def __init__(
        self,
        log_path: str,
        maxsize: int = 10_000,
        flush_interval_seconds: float = 0.5,
    ) -> None:
        self._log_path       = log_path
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._dropped        = 0
        self._flush_interval = flush_interval_seconds
        self._stop_event     = threading.Event()

        self._thread = threading.Thread(
            target=self._writer_loop,
            name="audit-logger-writer",
            daemon=True,
        )
        self._thread.start()
        atexit.register(self.shutdown)
        logger.info("AuditLogger started: path=%s maxsize=%d", log_path, maxsize)

    def log(self, entry: dict) -> None:
        """Enqueue an audit entry. Returns immediately. Never blocks."""
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.error(
                    "AuditLogger queue full: dropped=%d log_path=%s",
                    self._dropped, self._log_path,
                )

    def dropped_count(self) -> int:
        return self._dropped

    def _writer_loop(self) -> None:
        with open(self._log_path, "a", encoding="utf-8", buffering=65536) as f:
            while not self._stop_event.is_set():
                batch = []
                # Drain available entries up to 500 at a time
                try:
                    while len(batch) < 500:
                        batch.append(self._queue.get_nowait())
                except queue.Empty:
                    pass

                if batch:
                    f.write("\n".join(json.dumps(e) for e in batch) + "\n")
                    f.flush()
                else:
                    time.sleep(self._flush_interval)

            # Drain remaining entries on shutdown
            remaining = []
            while True:
                try:
                    remaining.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if remaining:
                f.write("\n".join(json.dumps(e) for e in remaining) + "\n")
                f.flush()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("AuditLogger writer thread did not shut down cleanly.")

# Module-level singleton — initialize once at startup
_audit_logger: Optional[AuditLogger] = None

def init_audit_logger(log_path: str) -> None:
    global _audit_logger
    _audit_logger = AuditLogger(log_path)

def evaluate(transaction: dict) -> float:
    """Hot-path evaluation function. I/O is now fully off the critical path."""
    score = _compute_score(transaction)  # ~3ms

    if _audit_logger is not None:
        _audit_logger.log({
            "ts":             time.time(),
            "transaction_id": transaction["id"],
            "score":          score,
        })

    return score

def _compute_score(transaction: dict) -> float:
    time.sleep(0.003)
    return 0.12
```

### 5.2 Benchmarking the Fix

Measure throughput and per-call latency before and after the batching fix so that the improvement is quantified and documented.

```python
import time
import statistics
import json
import os
import tempfile

def benchmark_evaluate(evaluate_fn, n: int = 1000) -> dict:
    """
    Run evaluate_fn n times and return throughput and latency statistics.
    """
    transactions = [{"id": f"txn_{i}", "amount": 50 + i} for i in range(n)]
    latencies    = []

    wall_start = time.perf_counter()
    for txn in transactions:
        t0  = time.perf_counter()
        evaluate_fn(txn)
        latencies.append((time.perf_counter() - t0) * 1000)
    wall_total = time.perf_counter() - wall_start

    return {
        "n":            n,
        "total_s":      round(wall_total, 3),
        "throughput":   round(n / wall_total, 1),
        "mean_ms":      round(statistics.mean(latencies), 2),
        "median_ms":    round(statistics.median(latencies), 2),
        "p99_ms":       round(sorted(latencies)[int(n * 0.99)], 2),
        "max_ms":       round(max(latencies), 2),
    }

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "audit.jsonl")

        # Benchmark the synchronous (buggy) version
        def slow_evaluate(txn):
            score = 0.12
            time.sleep(0.003)
            with open(log_path, "a") as f:
                f.write(json.dumps({"id": txn["id"], "score": score}) + "\n")
            return score

        print("=== Synchronous I/O (buggy) ===")
        r = benchmark_evaluate(slow_evaluate, n=200)
        for k, v in r.items():
            print(f"  {k:15s}: {v}")

        # Benchmark the async queue (fixed) version
        init_audit_logger(log_path)

        print("\n=== Async Queue I/O (fixed) ===")
        r2 = benchmark_evaluate(evaluate, n=200)
        for k, v in r2.items():
            print(f"  {k:15s}: {v}")

        _audit_logger.shutdown()
```

---

## 6. Architectural Prevention

**Designate I/O tiers explicitly.** In the system design document, classify every function as either "hot path" (latency-critical, no blocking I/O allowed) or "background" (throughput-oriented, I/O permitted). Enforce the classification in code review.

**Use structured logging with async handlers.** Replace direct file writes with Python's `logging` module configured with a `QueueHandler` and `QueueListener` (stdlib, no third-party dependencies):

```python
import logging
import logging.handlers
import queue

def configure_async_logging(log_path: str) -> None:
    log_queue   = queue.Queue(maxsize=50_000)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    listener = logging.handlers.QueueListener(log_queue, file_handler, respect_handler_level=True)
    listener.start()

    root = logging.getLogger()
    root.addHandler(logging.handlers.QueueHandler(log_queue))
    root.setLevel(logging.DEBUG)
    # Store listener reference to call listener.stop() on shutdown
    return listener
```

**Budget I/O at the function level in tests.** Add a time-based assertion to every test of a hot-path function:

```python
import time

MAX_EVALUATE_MS = 10.0

def test_evaluate_meets_latency_budget():
    txn = {"id": "txn_test", "amount": 150}
    t0 = time.perf_counter()
    evaluate(txn)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < MAX_EVALUATE_MS, (
        f"evaluate() took {elapsed_ms:.1f}ms, exceeds budget of {MAX_EVALUATE_MS}ms"
    )
```

---

## 7. Anti-patterns to Avoid

- **Benchmarking on a developer workstation with a local NVMe SSD.** Production storage is almost always slower. Always benchmark on infrastructure that matches production, or mock storage with artificially increased latency.
- **Using `logging.basicConfig` at the root logger in production.** The default `StreamHandler` and `FileHandler` are synchronous. Every `logger.info()` in the hot path is a blocking write.
- **Opening the file on every loop iteration.** `open()` issues an `open()` syscall, allocates a file descriptor, and on `close()` may flush. Use a long-lived file handle or a dedicated writer thread.
- **Wrapping I/O in `try/except` and silently continuing.** Suppressed I/O errors hide write failures. If the audit log is dropped silently, compliance guarantees are violated without anyone knowing.
- **Adding metrics emission inside the loop without rate-limiting.** Sending a metric (via UDP, HTTP, or a local socket) on every iteration also adds latency. Emit aggregated metrics (count, mean, p99) at a fixed interval from a separate thread.

---

## 8. Edge Cases and Variants

**`fsync` on every write.** If the storage driver or filesystem is mounted with `sync` option, every write is durable before returning. A durable write to a network volume may take 20–50ms. This variant produces even more severe latency degradation.

**Metric emission to a local agent.** Sending a metric via UDP to a local `statsd` agent feels like a lightweight operation, but at 1000 calls/second the socket syscalls add measurable overhead. Batch metric counters in-process and flush to the agent every 100ms.

**Database writes as audit log.** Replacing the flat file with an `INSERT` into a database in the hot path moves the bottleneck from disk I/O to network I/O plus database lock contention. The fix (background queue) applies equally.

**GIL contention with background thread.** In CPython, the background writer thread competes for the GIL when serializing JSON. If the evaluation loop is CPU-bound, GIL contention can slow the main thread. Use `json.dumps` in the main thread (fast) and pass the pre-serialized string to the queue to minimize background thread GIL time.

**Queue backpressure under sustained overload.** If the writer thread falls behind and the queue fills, the `put_nowait` in the hot path will drop entries. Size the queue large enough to absorb burst traffic but small enough to bound memory usage. Expose `dropped_count()` as a metric.

---

## 9. Audit Checklist

- [ ] Every function on the evaluation hot path contains no blocking `open()`, `write()`, `flush()`, or `fsync()` calls.
- [ ] Audit and trace logging from hot-path functions is routed through a `QueueHandler` or equivalent asynchronous mechanism.
- [ ] Hot-path functions have a latency budget assertion in their unit tests that is enforced in CI.
- [ ] The latency budget was measured on infrastructure representative of production (network-attached storage, shared volumes, container resource limits).
- [ ] The background I/O queue has a bounded maximum size; entries dropped when the queue is full are counted and exposed as a metric.
- [ ] The background writer thread is named and its health (alive/dead) is monitored.
- [ ] Graceful shutdown flushes the queue before process exit.
- [ ] No metric or observability call (statsd, Prometheus, OpenTelemetry) executes synchronously inside the evaluation loop.
- [ ] `logging.basicConfig` is not used in any service that handles more than 10 requests per second.
- [ ] File handles used for audit output are opened once at startup, not on every evaluation.

---

## 10. Further Reading

- Python documentation — `logging.handlers.QueueHandler`: https://docs.python.org/3/library/logging.handlers.html#logging.handlers.QueueHandler
- Python documentation — `queue.Queue`: https://docs.python.org/3/library/queue.html
- `py-spy` sampling profiler for production Python: https://github.com/benfred/py-spy
- `perf` Linux performance analysis: https://perf.wiki.kernel.org/index.php/Main_Page
- Martin Thompson — "Mechanical Sympathy" blog on I/O and latency: https://mechanical-sympathy.blogspot.com
- Brendan Gregg — "Systems Performance" (2nd ed.), Chapter 9 (Disk I/O)
- LMAX Disruptor pattern — ring buffer for low-latency inter-thread communication: https://lmax-exchange.github.io/disruptor/
