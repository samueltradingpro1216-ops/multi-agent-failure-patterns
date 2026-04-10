# Pattern #44 — Connection Leak on Error Path

**Category:** Detection & Monitoring
**Severity:** High
**Tags:** connection-pool, resource-leak, error-handling, database, http-client

---

## 1. Observable Symptoms

The failure surface of a connection leak is intentionally deceptive: the query or request that exhausts the pool is never the query that caused the leak. Symptoms arrive in three phases.

**Phase 1 — Silent accumulation (hours to days).**
No errors surface. Connection pool metrics show a slow upward drift in active connections. Under light traffic this drift is imperceptible. Under normal load it is visible in dashboards but easy to attribute to traffic growth.

**Phase 2 — Intermittent failures.**
A small percentage of requests begin failing with `sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached`, `psycopg2.OperationalError: FATAL: remaining connection slots are reserved`, or analogous HTTP client errors such as `urllib3.exceptions.MaxRetryError` or `ConnectionPool is full`. These errors appear on high-traffic endpoints that have nothing to do with the code path that leaked. Support tickets arrive: "checkout is broken." The checkout code is fine; its connection requests are being denied because the pool is full.

**Phase 3 — Full exhaustion.**
All pool slots are occupied. Every new connection attempt blocks until the `pool_timeout` is reached, then raises. The application becomes unresponsive. A restart clears the leak and symptoms vanish — until the next deployment, when the cycle begins again, causing the bug to be dismissed as a "transient infrastructure issue."

Additional observable indicators:
- PostgreSQL `pg_stat_activity` shows many connections in `idle` state with `application_name` matching the service, held by PIDs that are not actively executing queries.
- APM traces show the offending code path completing faster than expected (it returned early on the exception without performing cleanup).
- Connection wait time (`pool_checkout_timeout`) climbs steadily over the uptime of the process.
- Restarting the service resets connection counts immediately, providing false confirmation of an infrastructure cause.

---

## 2. Field Story

An e-commerce platform operated a Python-based inventory service responsible for reserving stock units during checkout. The service used SQLAlchemy with a connection pool of size 10 and overflow 20. Over several weeks, the on-call rotation began receiving alerts about checkout failures every three to four days, always resolved by restarting the inventory pod.

The team assumed the issue was a Kubernetes resource limit or a PostgreSQL server-side timeout. After three incidents and no root cause, a senior engineer attached a connection-level monitor to the PostgreSQL instance and observed that the connection count climbed from 12 to 28 over approximately 18 hours of normal traffic, then plateaued as the pool overflowed, then caused failures.

Tracing the connection count rise to deployment timestamps confirmed the leak was introduced in the service's code, not the infrastructure. The engineer searched for every location where a database session was acquired and found a reservation function that opened a session manually (rather than using the context manager) in order to run a raw SQL `SELECT FOR UPDATE`. The success path called `session.close()`. A bare `except Exception` block, added three weeks earlier to handle a vendor API timeout, logged the error and returned early — without closing the session.

The function executed tens of thousands of times per day. Each vendor API timeout (roughly 0.1% of calls under high concurrency) leaked one connection. At peak traffic, 20–30 connections leaked per hour, exhausting the pool within a business day.

The fix was applied in under 10 minutes once the location was identified. The post-mortem identified that code review had not flagged the missing `session.close()` because the reviewer focused on the new exception-handling logic and did not trace the resource lifecycle across branches.

---

## 3. Technical Root Cause

A connection leak on the error path is a resource lifecycle violation: a resource is acquired in one branch of control flow and released only in a subset of exit paths.

```python
# BUGGY — connection leaked when exception is raised
def reserve_stock(product_id: int, quantity: int) -> bool:
    session = Session()  # connection acquired here
    try:
        row = session.execute(
            text("SELECT stock FROM inventory WHERE id = :pid FOR UPDATE"),
            {"pid": product_id}
        ).fetchone()
        if row and row.stock >= quantity:
            session.execute(
                text("UPDATE inventory SET stock = stock - :qty WHERE id = :pid"),
                {"qty": quantity, "pid": product_id}
            )
            session.commit()
            session.close()  # released on success path only
            return True
        session.close()     # released on "not enough stock" path
        return False
    except VendorAPITimeout as exc:
        log.error("vendor timeout during reservation: %s", exc)
        return False        # session.close() never called — connection leaked
```

The root cause has two components:

**1. Manual resource acquisition without guaranteed release.** Any code that calls `session = Session()` (or `conn = pool.getconn()`, or `client = HTTPClient()`) outside of a context manager creates a resource that must be explicitly released on every exit path, including every `except` block and every early `return`. As function complexity grows — more exception types, more early returns — the probability of missing a release increases.

**2. Exception path as an afterthought.** The `except` block was added after the success/failure logic was already written and reviewed. The author's mental model focused on "what should happen when the vendor times out" (log and return False) rather than "what resources are currently held that must be released."

The connection pool has no mechanism to detect that the holder of a connection has exited without closing it. The pool's reference count is decremented only when `close()` is explicitly called (or when the connection object is garbage-collected, which is non-deterministic in CPython and absent in PyPy). Until one of those events occurs, the slot remains marked as in-use.

---

## 4. Detection

### 4.1 Static Analysis

Search the codebase for connection acquisition patterns that are not immediately inside a `with` statement or `try/finally` block.

```python
# detection_scan.py — AST-based scan for manual session/connection acquisition
import ast
import sys
from pathlib import Path

ACQUISITION_NAMES = {
    "Session", "sessionmaker", "connect", "getconn",
    "HTTPClient", "requests.Session", "create_connection"
}

class ResourceLeakVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.findings: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Look for assignments like: session = Session() or conn = pool.connect()
        assignments = [
            n for n in ast.walk(node)
            if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Call)
            and self._is_acquisition(n.value)
        ]
        for assign in assignments:
            # Check whether the assignment is inside a `with` or `try` block
            if not self._inside_context_or_try(node, assign):
                target = assign.targets[0]
                varname = target.id if isinstance(target, ast.Name) else "?"
                self.findings.append(
                    f"{self.filename}:{assign.lineno} — "
                    f"'{varname}' acquired outside with/try block in "
                    f"'{node.name}'"
                )
        self.generic_visit(node)

    def _is_acquisition(self, call: ast.Call) -> bool:
        if isinstance(call.func, ast.Name):
            return call.func.id in ACQUISITION_NAMES
        if isinstance(call.func, ast.Attribute):
            return call.func.attr in {"connect", "getconn", "Session", "get_session"}
        return False

    def _inside_context_or_try(self, func: ast.FunctionDef, target_node: ast.AST) -> bool:
        for node in ast.walk(func):
            if isinstance(node, (ast.With, ast.Try)):
                for child in ast.walk(node):
                    if child is target_node:
                        return True
        return False


def scan_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    visitor = ResourceLeakVisitor(str(path))
    visitor.visit(tree)
    return visitor.findings


def main(root: str) -> None:
    findings: list[str] = []
    for path in Path(root).rglob("*.py"):
        findings.extend(scan_file(path))
    if findings:
        print(f"Potential connection leaks found ({len(findings)}):")
        for f in findings:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("No connection leak candidates found.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
```

### 4.2 Runtime Monitoring

Instrument the connection pool to expose a metric that tracks the delta between connections checked out and connections returned. A positive and growing delta is diagnostic.

```python
# pool_monitor.py — SQLAlchemy event-based connection tracker
from collections import defaultdict
from sqlalchemy import event
from sqlalchemy.pool import Pool
import threading
import time


class ConnectionLeakMonitor:
    """
    Attaches to a SQLAlchemy pool and tracks checkout/checkin events.
    Reports connections held longer than `timeout_seconds`.
    """

    def __init__(self, pool: Pool, timeout_seconds: float = 30.0):
        self._pool = pool
        self._timeout = timeout_seconds
        self._active: dict[int, tuple[float, str]] = {}  # conn_id -> (checkout_time, stack)
        self._lock = threading.Lock()
        self._register()

    def _register(self) -> None:
        event.listen(self._pool, "checkout", self._on_checkout)
        event.listen(self._pool, "checkin", self._on_checkin)

    def _on_checkout(self, dbapi_conn, connection_record, connection_proxy) -> None:
        import traceback
        conn_id = id(dbapi_conn)
        stack = "".join(traceback.format_stack(limit=8))
        with self._lock:
            self._active[conn_id] = (time.monotonic(), stack)

    def _on_checkin(self, dbapi_conn, connection_record) -> None:
        conn_id = id(dbapi_conn)
        with self._lock:
            self._active.pop(conn_id, None)

    def report_leaks(self) -> list[dict]:
        now = time.monotonic()
        leaks = []
        with self._lock:
            for conn_id, (checkout_time, stack) in self._active.items():
                held_for = now - checkout_time
                if held_for > self._timeout:
                    leaks.append({
                        "conn_id": conn_id,
                        "held_seconds": round(held_for, 2),
                        "stack": stack,
                    })
        return leaks

    def start_background_reporter(self, interval_seconds: float = 60.0) -> None:
        import logging
        log = logging.getLogger(__name__)

        def _loop():
            while True:
                time.sleep(interval_seconds)
                leaks = self.report_leaks()
                if leaks:
                    log.warning(
                        "ConnectionLeakMonitor: %d connection(s) held > %.0fs",
                        len(leaks), self._timeout
                    )
                    for leak in leaks:
                        log.warning(
                            "  conn_id=%d held=%.2fs\n%s",
                            leak["conn_id"], leak["held_seconds"], leak["stack"]
                        )

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
```

### 4.3 Integration Test

Write a test that simulates the error path and asserts that the pool returns to its baseline connection count afterward.

```python
# test_connection_leak.py
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from unittest.mock import patch


class VendorAPITimeout(Exception):
    pass


def reserve_stock_buggy(session_factory, product_id: int, quantity: int) -> bool:
    """The buggy implementation — for test demonstration."""
    session = session_factory()
    try:
        row = session.execute(
            text("SELECT 1 AS stock")
        ).fetchone()
        if row:
            session.close()
            return True
        session.close()
        return False
    except VendorAPITimeout as exc:
        # BUG: no session.close() here
        return False


def reserve_stock_fixed(session_factory, product_id: int, quantity: int) -> bool:
    """The fixed implementation using try/finally."""
    session = session_factory()
    try:
        row = session.execute(
            text("SELECT 1 AS stock")
        ).fetchone()
        return bool(row)
    except VendorAPITimeout:
        return False
    finally:
        session.close()


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=0,
    )
    return eng


def _checked_out(engine) -> int:
    return engine.pool.checkedout()


def test_buggy_leaks_connection_on_error(engine):
    Session = sessionmaker(bind=engine)
    baseline = _checked_out(engine)

    with patch(
        "sqlalchemy.engine.base.Connection.execute",
        side_effect=VendorAPITimeout("simulated timeout"),
    ):
        result = reserve_stock_buggy(Session, product_id=1, quantity=1)

    assert result is False
    # The buggy version does NOT return the connection — pool count is elevated
    assert _checked_out(engine) > baseline, (
        "Expected a leaked connection but pool returned to baseline"
    )


def test_fixed_returns_connection_on_error(engine):
    Session = sessionmaker(bind=engine)
    baseline = _checked_out(engine)

    with patch(
        "sqlalchemy.engine.base.Connection.execute",
        side_effect=VendorAPITimeout("simulated timeout"),
    ):
        result = reserve_stock_fixed(Session, product_id=1, quantity=1)

    assert result is False
    assert _checked_out(engine) == baseline, (
        f"Fixed version leaked a connection: "
        f"expected {baseline}, got {_checked_out(engine)}"
    )
```

---

## 5. Fix

### 5.1 Immediate Fix — Guaranteed Release via `try/finally`

Replace any manual `session.close()` calls scattered across branches with a single `finally` block.

```python
# FIXED — guaranteed release on every exit path
def reserve_stock(product_id: int, quantity: int) -> bool:
    session = Session()
    try:
        row = session.execute(
            text("SELECT stock FROM inventory WHERE id = :pid FOR UPDATE"),
            {"pid": product_id}
        ).fetchone()
        if row and row.stock >= quantity:
            session.execute(
                text("UPDATE inventory SET stock = stock - :qty WHERE id = :pid"),
                {"qty": quantity, "pid": product_id}
            )
            session.commit()
            return True
        return False
    except VendorAPITimeout as exc:
        log.error("vendor timeout during reservation: %s", exc)
        session.rollback()
        return False
    finally:
        session.close()  # always executed, regardless of which branch was taken
```

### 5.2 Preferred Fix — Context Manager

Eliminate the manual lifecycle entirely by using the connection as a context manager. This is the canonical pattern and is immune to future additions of `except` branches.

```python
# PREFERRED — context manager eliminates the lifecycle problem entirely
from contextlib import contextmanager
from sqlalchemy.orm import Session as SASession


@contextmanager
def get_session():
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reserve_stock(product_id: int, quantity: int) -> bool:
    try:
        with get_session() as session:
            row = session.execute(
                text("SELECT stock FROM inventory WHERE id = :pid FOR UPDATE"),
                {"pid": product_id}
            ).fetchone()
            if row and row.stock >= quantity:
                session.execute(
                    text("UPDATE inventory SET stock = stock - :qty WHERE id = :pid"),
                    {"qty": quantity, "pid": product_id}
                )
                return True
            return False
    except VendorAPITimeout as exc:
        log.error("vendor timeout during reservation: %s", exc)
        return False
```

The `get_session()` context manager owns the lifecycle. The calling function cannot forget to close the session because it never holds a reference to the raw session object.

---

## 6. Architectural Prevention

**1. Enforce context-manager-only session access.** Configure the SQLAlchemy `Session` class or HTTP client factory to raise an error if accessed outside a `with` block. A custom `__enter__`/`__exit__` wrapper around `sessionmaker` that tracks whether it was entered as a context manager can enforce this at runtime during development.

**2. Pool exhaustion alerting before symptoms.** Set the alert threshold at 70–80% pool utilization, not at failure. Alert on `pool_checked_out / (pool_size + max_overflow) > 0.75` sustained for more than two minutes. This fires during Phase 1 (silent accumulation) and gives engineers time to diagnose before customers are affected.

**3. Connection age limits.** Configure `pool_recycle` (SQLAlchemy) or equivalent to forcibly close connections that have been checked out for longer than any reasonable request duration (e.g., 60 seconds). This converts a permanent leak into a bounded one and limits the blast radius of any single occurrence.

**4. Linting in CI.** Add the static analysis scanner (Section 4.1) as a required CI step. A failing scan blocks the merge. This catches the pattern before it reaches production.

**5. Scope connection acquisition to the unit of work.** Architectural guidance: database sessions must be acquired at the HTTP request boundary (middleware layer) and released when the response is sent, never inside individual business logic functions. Business logic functions receive an already-open session as a parameter. This centralizes lifecycle management and makes leaks structurally impossible inside business code.

---

## 7. Anti-patterns

**Anti-pattern A — Relying on garbage collection.** Python's CPython implementation uses reference counting and typically collects objects promptly, so a leaked session may be closed when it goes out of scope. This is not guaranteed behavior, does not apply to PyPy, and is not reliable under circular references. Do not depend on `__del__` for resource cleanup.

**Anti-pattern B — Pool size as a safety margin.** Increasing `max_overflow` to accommodate leaks is not a fix. It delays pool exhaustion but does not prevent it. It also increases the connection count on the database server, potentially causing its own resource pressure.

**Anti-pattern C — Blanket `except Exception: pass`.** Suppressing exceptions without releasing resources is doubly harmful: it hides the error that caused the exception and silently leaks the connection. Every `except` block that does not re-raise must explicitly release all held resources.

**Anti-pattern D — `session.close()` inside the `except` block only.** Adding `session.close()` to the `except` block without a `finally` means it will not be called if future code adds another exit path (another early `return` or `raise`) that bypasses the `except` block.

**Anti-pattern E — Checking pool metrics only in post-mortem.** Many teams inspect `pg_stat_activity` only after an outage. Pool metrics should be part of the standard service dashboard, visible alongside error rate and latency, so that drift is caught proactively.

---

## 8. Edge Cases

**Nested sessions.** If a function calls another function that acquires its own session, both sessions must be independently managed. A `finally` in the outer function does not close the inner session.

**Async contexts.** In `asyncio`-based applications using `asyncpg` or `aiosqlite`, the connection pool behavior is analogous but the lifecycle management uses `async with`. Synchronous `close()` calls in `finally` blocks do not apply. Use `async with engine.connect() as conn` throughout.

**Exception during `session.commit()`.** If the `commit()` itself raises (e.g., a serialization failure or a `UniqueViolation`), the `finally` block must still call `session.close()`. This is handled correctly by the `try/finally` pattern but can be missed in ad hoc implementations that call `close()` only after `commit()` succeeds.

**HTTP connection pools.** The pattern applies identically to HTTP clients. A `requests.Session` or `httpx.Client` that is not closed after use holds open TCP connections. Under high request rates, the OS-level socket exhaustion manifests as `ConnectionResetError` or `OSError: [Errno 24] Too many open files`.

**Thread-local sessions.** Some frameworks use thread-local session storage (Flask-SQLAlchemy's `db.session`). These are closed at the end of the request context, not by explicit `close()` calls. Mixing thread-local and manually created sessions in the same codebase creates confusion about which sessions are managed automatically and which require explicit lifecycle management.

---

## 9. Audit Checklist

- [ ] Search for `Session()`, `connect()`, `getconn()`, `HTTPClient()`, or equivalent not assigned inside a `with` block.
- [ ] For every such assignment, trace all exit paths (normal return, early return, each `except` clause, `raise`) and confirm `close()` is called on each.
- [ ] Confirm that `try/finally` (not `try/except`) is used to guarantee release, or that context managers are used exclusively.
- [ ] Verify that pool utilization metrics are exposed and included in operational dashboards.
- [ ] Verify that a pool utilization alert is configured at a threshold below 100% (recommended: 75%).
- [ ] Confirm `pool_recycle` or equivalent connection age limit is configured.
- [ ] Run the static analysis scanner (Section 4.1) against the full codebase.
- [ ] Confirm that integration tests for error paths (Section 4.3) exist and pass.
- [ ] Review any `except` blocks added in the past six months for missing resource release.
- [ ] Confirm that async paths use `async with` and not synchronous `close()` calls.

---

## 10. Further Reading

- SQLAlchemy documentation: "Connection Pooling" — `https://docs.sqlalchemy.org/en/20/core/pooling.html`
- Python documentation: `contextlib.contextmanager` — `https://docs.python.org/3/library/contextlib.html`
- PEP 343 — The `with` Statement: `https://peps.python.org/pep-0343/`
- PostgreSQL documentation: `pg_stat_activity` — `https://www.postgresql.org/docs/current/monitoring-stats.html`
- "Release It!" by Michael Nygard, Chapter 4: Stability Patterns (Circuit Breaker, Bulkhead) — provides architectural context for resource leak containment.
- OWASP: "Improper Resource Shutdown or Release" (CWE-404) — `https://cwe.mitre.org/data/definitions/404.html`
