# Pattern #38 — json.load Without Context Manager

**Category:** I/O & Persistence
**Severity:** Low
**Tags:** `file-handles`, `resource-leak`, `ulimit`, `context-manager`, `long-running`

---

## 1. Observable Symptoms

The system runs correctly for hours or days, then crashes with an error on an operation that appears completely unrelated to JSON parsing. The crash is not reproducible in short-lived test runs. It surfaces only after the process has been running long enough to accumulate leaked file descriptors.

- The process terminates or begins refusing operations with `OSError: [Errno 24] Too many open files`.
- The error appears on a network socket `connect()`, a database connection open, or a `subprocess.Popen()` — not on the JSON load line. This misleads engineers into investigating the wrong subsystem.
- `lsof -p <pid> | wc -l` shows a file descriptor count much higher than expected: hundreds or thousands of open handles, many pointing to JSON files that should have been closed.
- The issue does not appear in unit tests because tests are short-lived. Each test process starts with a fresh file descriptor table.
- Restarting the process resolves the error immediately. The issue returns after a predictable time interval that corresponds to the volume of JSON loads performed.
- In multi-agent deployments, one agent crashes first (the busiest one), while others continue running. This causes engineers to initially blame workload distribution rather than resource leaks.

---

## 2. Field Story

A legal technology company operated a document processing pipeline that extracted structured data from uploaded contracts. The pipeline was built as a multi-agent system: an orchestrator dispatched each incoming document to a pool of worker agents, and each agent loaded a set of extraction schema files (JSON) to determine which fields to extract from that document type.

The schema files changed rarely — perhaps once per week during model updates — but the agent reloaded them on every document because a previous caching bug had caused stale schemas to be applied after updates. The reload was considered safe overhead since the schemas were small.

The pattern used was:

```python
schema = json.load(open(f"/app/schemas/{doc_type}.json"))
```

Under normal conditions this worked. Under error conditions — when the JSON was malformed during an incremental schema update — `json.load` raised a `json.JSONDecodeError`. The `open()` call had already succeeded and returned a file object. That file object was never assigned to a variable and never closed. The exception propagated up, the agent logged the error and moved to the next document, and the leaked file handle remained open until process exit.

Schema validation errors occurred approximately once every 500 documents during update windows. The pipeline processed about 3,000 documents per day. After 72 hours, each agent had accumulated several hundred leaked handles. The process-level file descriptor limit was 1,024. When an agent tried to open a network connection to the database to write extracted results, the OS refused with `[Errno 24] Too many open files`. The agent appeared to die from a database connectivity problem. Three engineers spent four hours investigating database connection pooling before one of them ran `lsof` on the failing process.

---

## 3. Technical Root Cause

The defect is the use of `json.load(open(...))` without a `with` statement. In Python, a file object returned by `open()` is closed when its reference count drops to zero (CPython, which uses reference counting) or when garbage collection runs (PyPy, Jython, or CPython with circular references). When an exception is raised inside `json.load()`, the file object may have no remaining references and in CPython will typically be closed promptly — but this is an implementation detail, not a language guarantee. More importantly, in exception paths the behavior diverges across implementations.

```python
# BUGGY: file handle leaked on json.JSONDecodeError
import json

def load_schema(doc_type: str) -> dict:
    # open() succeeds, returns a file object
    # json.load() raises JSONDecodeError on malformed content
    # The file object has no name binding; Python's reference counting
    # may or may not close it before the next GC cycle.
    return json.load(open(f"/app/schemas/{doc_type}.json", encoding="utf-8"))
```

**Why CPython's reference counting is not a reliable substitute for `with`.** In CPython, when `json.load(open(...))` is called, the file object's reference count is 1 (held by the `open()` return value passed directly to `json.load`). When `json.load` raises, the exception propagates and the temporary reference is dropped. CPython typically closes the file at that point. However:

1. If the file object is captured anywhere in a traceback frame (e.g., in a local variable of an internal `json.load` frame, an `inspect` call, or a debugger hook), the reference count stays above zero.
2. In long-lived threads with deep call stacks, traceback frames may persist longer than expected.
3. In PyPy, Jython, or IronPython, reference counting is not used. File objects are only closed when the garbage collector runs, which is non-deterministic.
4. Any production code that targets CPython exclusivity is fragile by design.

**The accumulation mechanism.** A single leaked handle per error occurrence is harmless. The problem is that errors recur at a steady rate. At 1 error per 500 documents and 3,000 documents per day, the process accumulates ~6 leaked handles per day. After 170 days — or during an error storm where the rate spikes — the limit is hit. In the field story, a schema update window caused a 10x temporary error rate spike, compressing 170 days of normal accumulation into 17 hours.

**The `ulimit` interaction.** The OS enforces a per-process file descriptor limit (`ulimit -n`, visible as `/proc/<pid>/limits`). The default soft limit on many Linux distributions is 1,024. Each open file, socket, pipe, and epoll handle counts against this limit. A long-running service uses a baseline of ~50–100 handles for sockets, pipes, and framework internals. The leaked JSON handles consume the remaining capacity.

```python
# Demonstrate the leak scenario:
import json
import io
import gc
import os

def count_open_fds() -> int:
    """Count open file descriptors for the current process (Linux)."""
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except PermissionError:
        return -1

def load_json_unsafe(content: str) -> dict:
    """Simulates json.load(open(...)) with a StringIO to avoid real files."""
    f = io.StringIO(content)
    # If json.load raises, f is never explicitly closed
    return json.load(f)

# With valid JSON: typically fine (CPython closes f immediately)
try:
    result = load_json_unsafe('{"valid": true}')
except Exception:
    pass

# With malformed JSON: exception raised inside json.load
# The StringIO object may remain alive in a traceback frame
try:
    result = load_json_unsafe('{malformed json}')
except json.JSONDecodeError:
    pass  # f may still be referenced in the current exception's traceback
```

---

## 4. Detection

### 4.1 Static Analysis: Detect Unguarded `json.load(open(...))`

Parse the AST to find calls where `json.load` or `json.loads` receives a bare `open()` call as its first argument, without being inside a `with` block.

```python
import ast
import sys
from pathlib import Path

def find_unguarded_json_open(source_path: str) -> list[dict]:
    """
    Detect json.load(open(...)) calls that are not inside a `with` block.
    Returns a list of violation dicts with file path, line number, and snippet.
    """
    source   = Path(source_path).read_text(encoding="utf-8")
    lines    = source.splitlines()
    tree     = ast.parse(source)
    findings = []

    # Collect line numbers of all `with open(...)` blocks
    with_open_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            ctx_expr = item.context_expr
            if isinstance(ctx_expr, ast.Call):
                func = ctx_expr.func
                if isinstance(func, ast.Name) and func.id == "open":
                    # Mark all lines in this with block as "guarded"
                    for lineno in range(node.lineno, node.end_lineno + 1):
                        with_open_lines.add(lineno)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_json_load = (
            isinstance(func, ast.Attribute) and
            func.attr == "load" and
            isinstance(func.value, ast.Name) and
            func.value.id == "json"
        )
        if not is_json_load:
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        is_open_arg = (
            isinstance(first_arg, ast.Call) and
            isinstance(first_arg.func, ast.Name) and
            first_arg.func.id == "open"
        )
        if not is_open_arg:
            continue
        if node.lineno in with_open_lines:
            continue  # already guarded
        findings.append({
            "file":    source_path,
            "line":    node.lineno,
            "snippet": lines[node.lineno - 1].strip(),
        })

    return findings

if __name__ == "__main__":
    for path in sys.argv[1:]:
        for v in find_unguarded_json_open(path):
            print(
                f"[FD-LEAK] {v['file']}:{v['line']} — "
                f"unguarded json.load(open(...)): {v['snippet']}"
            )
```

### 4.2 Runtime File Descriptor Monitoring

In a long-running service, periodically sample the open file descriptor count and emit a warning when it exceeds a threshold. This provides early warning before the system hits the hard limit.

```python
import os
import logging
import threading
import time

logger = logging.getLogger(__name__)

def get_open_fd_count() -> int:
    """Return the number of open file descriptors for this process."""
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except (PermissionError, FileNotFoundError):
        import resource
        # Fallback: iterate potential FD range
        count = 0
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        for fd in range(soft_limit):
            try:
                os.fstat(fd)
                count += 1
            except OSError:
                pass
        return count

def get_fd_limit() -> int:
    """Return the soft file descriptor limit for this process."""
    import resource
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    return soft

class FDLeakMonitor:
    """
    Background thread that samples the open FD count and warns when it
    approaches the process limit.
    """

    def __init__(
        self,
        warn_fraction: float = 0.75,
        check_interval_seconds: float = 30.0,
    ) -> None:
        self._warn_fraction = warn_fraction
        self._interval      = check_interval_seconds
        self._stop          = threading.Event()
        self._thread        = threading.Thread(
            target=self._run,
            name="fd-leak-monitor",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        limit = get_fd_limit()
        warn_threshold = int(limit * self._warn_fraction)
        logger.info("FDLeakMonitor started: limit=%d warn_at=%d", limit, warn_threshold)

        while not self._stop.wait(self._interval):
            current = get_open_fd_count()
            pct     = current / limit * 100
            if current >= warn_threshold:
                logger.warning(
                    "FD_LEAK_WARNING: open_fds=%d limit=%d usage_pct=%.1f",
                    current, limit, pct,
                )
            else:
                logger.debug(
                    "FD monitor: open_fds=%d limit=%d usage_pct=%.1f",
                    current, limit, pct,
                )

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
```

### 4.3 Integration Test: Verify No Leak Under Error Conditions

Write a test that simulates repeated JSON load failures (malformed input) and verifies that the file descriptor count does not grow.

```python
import json
import os
import gc
import tempfile
import pytest

def load_json_buggy(path: str) -> dict:
    """The buggy pattern: no context manager."""
    return json.load(open(path, encoding="utf-8"))

def load_json_safe(path: str) -> dict:
    """The correct pattern: context manager ensures close on exception."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def _count_fds() -> int:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except Exception:
        return -1

@pytest.mark.parametrize("loader,expect_leak", [
    (load_json_buggy, True),
    (load_json_safe,  False),
])
def test_fd_leak_on_json_error(loader, expect_leak, tmp_path):
    """
    Write a malformed JSON file and call the loader 50 times, catching
    the JSONDecodeError each time. Verify whether the FD count grows.
    """
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not: valid json!!!}", encoding="utf-8")

    gc.collect()
    fd_before = _count_fds()
    if fd_before == -1:
        pytest.skip("Cannot read /proc/self/fd on this platform")

    for _ in range(50):
        try:
            loader(str(malformed))
        except json.JSONDecodeError:
            pass

    gc.collect()
    fd_after = _count_fds()
    leaked   = fd_after - fd_before

    if expect_leak:
        # CPython may close immediately via refcount; document the behavior
        # rather than asserting a specific leak count
        print(f"Buggy loader leaked {leaked} FDs (CPython may close promptly)")
    else:
        assert leaked <= 2, (
            f"Safe loader leaked {leaked} FDs — expected 0. "
            f"fd_before={fd_before} fd_after={fd_after}"
        )
```

---

## 5. Fix

### 5.1 Immediate Fix: Always Use a `with` Block

Replace every occurrence of `json.load(open(...))` with a `with open(...) as f: json.load(f)` pattern. This is guaranteed to close the file object regardless of whether `json.load` succeeds or raises.

```python
import json
import os
import logging

logger = logging.getLogger(__name__)

# BEFORE (buggy):
def load_schema_buggy(doc_type: str) -> dict:
    return json.load(open(f"/app/schemas/{doc_type}.json", encoding="utf-8"))

# AFTER (correct):
def load_schema(doc_type: str) -> dict:
    """
    Load a schema JSON file. The with block guarantees the file handle is
    closed even if json.load raises json.JSONDecodeError or any other exception.
    """
    schema_path = os.path.join(
        os.environ.get("SCHEMA_DIR", "/app/schemas"),
        f"{doc_type}.json",
    )
    try:
        with open(schema_path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("Schema file not found: path=%s doc_type=%s", schema_path, doc_type)
        raise
    except json.JSONDecodeError as exc:
        logger.error(
            "Malformed schema JSON: path=%s line=%d col=%d error=%s",
            schema_path, exc.lineno, exc.colno, exc.msg,
        )
        raise
```

### 5.2 Caching to Eliminate Redundant Reads

For files that change infrequently, cache the parsed result and reload only when the file modification time changes. This eliminates both the resource leak risk and the unnecessary I/O overhead.

```python
import json
import os
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

class JsonFileCache:
    """
    Thread-safe cache for JSON files. Reloads from disk only when the file's
    mtime changes. File handles are always closed via context manager.

    Usage:
        cache = JsonFileCache()
        schema = cache.get("/app/schemas/contract.json")
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}  # path -> (mtime, data)
        self._lock  = threading.Lock()

    def get(self, path: str) -> Any:
        """
        Return the parsed JSON content of `path`. Reloads from disk
        if the file has been modified since the last load.
        """
        real_path = os.path.realpath(path)

        try:
            current_mtime = os.path.getmtime(real_path)
        except OSError as exc:
            raise FileNotFoundError(
                f"Cannot stat JSON file: {real_path}"
            ) from exc

        with self._lock:
            if real_path in self._cache:
                cached_mtime, cached_data = self._cache[real_path]
                if cached_mtime == current_mtime:
                    logger.debug("JsonFileCache hit: path=%s", real_path)
                    return cached_data

        # Load outside the lock to avoid blocking other threads on I/O
        logger.info("JsonFileCache loading: path=%s mtime=%f", real_path, current_mtime)
        with open(real_path, encoding="utf-8") as f:
            data = json.load(f)
        # File is closed here regardless of json.load outcome

        with self._lock:
            self._cache[real_path] = (current_mtime, data)

        return data

    def invalidate(self, path: str) -> None:
        real_path = os.path.realpath(path)
        with self._lock:
            self._cache.pop(real_path, None)

    def size(self) -> int:
        with self._lock:
            return len(self._cache)

# Module-level singleton
_schema_cache = JsonFileCache()

def load_schema(doc_type: str) -> dict:
    schema_dir  = os.environ.get("SCHEMA_DIR", "/app/schemas")
    schema_path = os.path.join(schema_dir, f"{doc_type}.json")
    return _schema_cache.get(schema_path)
```

---

## 6. Architectural Prevention

**Lint enforcement.** Add a `flake8` plugin or `pylint` rule that flags `json.load(open(...))` as a style violation in CI. The `flake8-bugbear` plugin (rule B007 and related) catches some resource leak patterns. A custom AST-based check (as shown in section 4.1) can be added as a pre-commit hook.

**`pathlib` as the canonical API.** `pathlib.Path.read_text()` combined with `json.loads()` avoids file handles entirely for small files:

```python
from pathlib import Path
import json

def load_schema(doc_type: str) -> dict:
    schema_dir = Path(os.environ.get("SCHEMA_DIR", "/app/schemas"))
    content    = (schema_dir / f"{doc_type}.json").read_text(encoding="utf-8")
    return json.loads(content)
    # No file handle; Path.read_text() opens and closes internally
```

**File descriptor limit increase.** For long-running services that legitimately need many open files, increase the limit in the systemd unit or container manifest. This is a mitigation, not a fix — the underlying leak should still be corrected.

```ini
# systemd unit file
[Service]
LimitNOFILE=65536
```

**Process restart with leak detection.** If a legacy codebase cannot be fully audited immediately, deploy a watchdog that restarts the process when open FD count exceeds a threshold. Log the restart reason so the accumulation rate can be tracked over time.

---

## 7. Anti-patterns to Avoid

- **Relying on CPython's reference counting for file cleanup.** `del f` or allowing `f` to go out of scope closes the file in CPython today, but this is an implementation detail. Code that depends on it will break under PyPy, in the presence of circular references, or if the object is captured by a debugger or profiler.
- **Using `try/finally` as a substitute for `with`.** `try: f = open(...); json.load(f) finally: f.close()` has a subtle bug: if `open()` itself raises (e.g., `PermissionError`), `f` is never assigned and `f.close()` in the `finally` block raises `NameError`. The `with` statement handles this correctly.
- **Wrapping the entire `json.load(open(...))` in a broad `except Exception: pass`.** This suppresses the `JSONDecodeError` but still leaks the handle. It also hides malformed configuration files, which may go undetected until the system silently uses stale cached data.
- **Calling `gc.collect()` as a fix.** Forcing garbage collection may close leaked file objects in CPython, but it adds CPU overhead, is non-deterministic in timing, and does not fix the underlying code defect.
- **Increasing `ulimit -n` as the primary remediation.** Raising the limit defers the crash to a higher threshold but does not eliminate the leak. The root cause must be fixed.

---

## 8. Edge Cases and Variants

**`open()` inside a generator expression passed to `json.load`.** If a generator creates file objects that are partially consumed and then abandoned, Python may not close them promptly. Always use explicit `with` blocks when opening files in generators.

**`tarfile.open()`, `zipfile.ZipFile()`, and other resource-returning calls.** The same pattern applies to any resource-returning function. `zipfile.ZipFile("archive.zip").read("entry.json")` leaks the ZipFile handle on exception. Always use `with zipfile.ZipFile(...) as zf:`.

**`importlib.resources.open_binary()`.** In Python 3.8 and earlier, `importlib.resources` returned file-like objects that required explicit closure. In Python 3.9+, the context manager API (`importlib.resources.as_file()`) is preferred.

**Threads and CPython reference counting.** In a multi-threaded application, a file object referenced in one thread's exception traceback will not be collected until that traceback is cleared. This is a real source of delayed file handle leaks in threaded services.

**`json.load` on a socket or network stream.** Passing a socket wrapped in a file-like object to `json.load` leaks the socket if parsing fails. Sockets are also counted against the file descriptor limit (they are file descriptors on Linux). The same `with` fix applies: `with sock.makefile() as f: json.load(f)`.

**High-volume error injection during testing.** If a test suite injects `JSONDecodeError` at high frequency for robustness testing, it can exhaust file descriptors within the test process itself. Always use `with` in test fixtures as well as production code.

---

## 9. Audit Checklist

- [ ] No `json.load(open(...))` call exists in the codebase without an enclosing `with` block.
- [ ] The static analysis script (section 4.1) runs in CI and fails the build on any new violation.
- [ ] All JSON file loading is routed through a centralized utility function or class that enforces `with` usage.
- [ ] The `FDLeakMonitor` (or equivalent) is deployed in all long-running services and emits a warning at 75% of the FD limit.
- [ ] An integration test verifies that JSON load failures (malformed files) do not increase the open FD count.
- [ ] `pathlib.Path.read_text()` + `json.loads()` is used where a file handle is not required beyond parsing.
- [ ] `tarfile`, `zipfile`, `csv`, `configparser`, and other resource-returning file APIs are audited for the same pattern.
- [ ] The systemd unit or container manifest sets `LimitNOFILE` to a value appropriate for the service's legitimate FD usage.
- [ ] Schema and configuration files loaded repeatedly in loops are cached with mtime-based invalidation.
- [ ] No exception handler silently swallows `JSONDecodeError` without logging the file path, line, and column.

---

## 10. Further Reading

- Python documentation — Context Managers and the `with` statement: https://docs.python.org/3/reference/compound_stmts.html#the-with-statement
- Python documentation — `io` module and file object lifecycle: https://docs.python.org/3/library/io.html
- PEP 343 — The "with" Statement: https://peps.python.org/pep-0343/
- Python documentation — `pathlib.Path.read_text`: https://docs.python.org/3/library/pathlib.html#pathlib.Path.read_text
- `flake8-bugbear` — opinionated linting rules including resource leak detection: https://github.com/PyCQA/flake8-bugbear
- Linux `ulimit` and `/proc/<pid>/limits`: https://man7.org/linux/man-pages/man2/setrlimit.2.html
- CPython garbage collection and the cyclic GC: https://devguide.python.org/internals/garbage-collector/
- `resource` module — Python interface to Unix resource limits: https://docs.python.org/3/library/resource.html
