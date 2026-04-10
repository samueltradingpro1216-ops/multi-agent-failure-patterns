# Pattern #41 — Stub Never Implemented

**Category:** Dead Code & Architecture
**Severity:** Medium
**Tags:** stub, unimplemented, silent-failure, none-return, design-incomplete

---

## 1. Observable Symptoms

Unlike dead functions, unimplemented stubs are actively called and therefore produce real effects — the effect being that a critical operation silently does nothing:

- A function that should return a boolean validation result returns `None`, and the caller treats `None` as truthy in certain Python idioms (`if result is not False: proceed()`).
- Features that appear to work during manual testing because QA does not test the paths gated by the unimplemented stub.
- `NotImplementedError` is raised at runtime for a code path that was tested only at the happy-path level — but only after the system has already accepted and partially processed invalid input.
- Logs show no errors for operations that should have triggered warnings (validation ran, returned nothing, caller assumed success).
- Data pipelines produce outputs with no quality checks applied, because the check function was stubbed and never revisited.
- A stub returning `None` is called where a list is expected; the caller receives `None`, attempts to iterate it, and raises `TypeError: 'NoneType' object is not iterable` in production but not in tests (because tests mock the stub).
- Code reviews show `pass` or `return None` bodies alongside a docstring describing intended behavior that was never written.

---

## 2. Field Story

A content moderation pipeline at a mid-size media platform was built in three phases over eight months. The first phase handled basic keyword filtering. The second phase added ML-based classifier integration. The third phase was supposed to add a behavioral consistency check that flagged accounts exhibiting coordinated inauthentic behavior.

The behavioral check was designed as an interface contract first. An engineer added `check_behavioral_consistency(account_id, post_history)` to the moderation chain with a docstring explaining the algorithm and returned `None` as a placeholder:

```python
def check_behavioral_consistency(account_id: str, post_history: list) -> dict | None:
    """
    Analyze post_history for coordinated inauthentic behavior signals.
    Returns a dict with keys: 'flagged' (bool), 'confidence' (float), 'reasons' (list).
    Returns None if analysis cannot be performed.
    """
    pass
```

The calling code in the moderation orchestrator read:

```python
consistency_result = check_behavioral_consistency(account_id, post_history)
if consistency_result and consistency_result.get("flagged"):
    escalate_for_human_review(account_id)
```

This is a classic stub trap. `None` is falsy in Python, so `if consistency_result` evaluates to `False` every time, and no account is ever escalated via this check. The condition looks correct to a reader who assumes the function works.

Phase three was delayed by a platform re-architecture project, then absorbed into a different team's roadmap, then quietly dropped. The stub survived every subsequent code review because reviewers assumed it was implemented elsewhere or planned for the next sprint. The `pass` body was replaced at some point with `return None` during a linting pass, which made it look even more like a deliberate "not applicable" return rather than an unimplemented placeholder.

The gap was discovered fourteen months later during a post-incident review when the platform investigated a coordinated account behavior campaign. The moderation pipeline logs showed the orchestrator had called `check_behavioral_consistency` on thousands of accounts and logged nothing unusual, because there was nothing to log — the function returned `None` every time and the escalation branch was never entered.

---

## 3. Technical Root Cause

The root cause operates on two levels: technical and process.

**At the technical level,** Python functions implicitly return `None` when execution reaches the end of the function body without a `return` statement, or when `pass` is the entire body. This means a stub with `pass` is syntactically valid, passes static type checking if the return type includes `None` or `Optional[...]`, and raises no runtime errors. The function appears to work — it accepts arguments, returns a value, and exits cleanly.

The compound effect occurs in the caller. A common guard pattern is `if result:`, which evaluates `None` as falsy. When the stub always returns `None`, the guard always short-circuits the protected branch. The code path gated by the unimplemented feature is permanently bypassed with no error, warning, or log entry.

**At the process level,** the stub is created with an implicit promise ("implement this later") that is never formally tracked. No ticket is created. No CI check fails. No type checker raises an error (because `None` satisfies `Optional[dict]`). The stub does not decay over time — it remains exactly as placed, accruing no visible technical debt signals.

A contributing factor is the absence of `raise NotImplementedError("check_behavioral_consistency not yet implemented")` in the stub body. If the stub raised instead of returning `None`, the first end-to-end test would have caught it immediately. The choice to return `None` silently instead of raising loudly is the moment the bug is introduced.

---

## 4. Detection

### 4.1 AST-Based Stub Scanner

```python
# scripts/find_stubs.py
"""
Scan the project for functions whose body consists only of pass, return None,
or a docstring followed by pass/return None. These are stub candidates.
Usage: python scripts/find_stubs.py src/
"""
import ast
import sys
from pathlib import Path
from dataclasses import dataclass


@dataclass
class StubCandidate:
    filepath: str
    lineno: int
    name: str
    has_docstring: bool
    body_type: str  # 'pass', 'return_none', 'docstring_only'
    return_annotation: str


def classify_body(func_node: ast.FunctionDef) -> str | None:
    body = func_node.body
    # Filter out the docstring if present
    non_doc_body = body[:]
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        non_doc_body = body[1:]

    if not non_doc_body:
        return "docstring_only"

    if len(non_doc_body) == 1:
        stmt = non_doc_body[0]
        if isinstance(stmt, ast.Pass):
            return "pass"
        if (isinstance(stmt, ast.Return) and
                (stmt.value is None or
                 (isinstance(stmt.value, ast.Constant) and stmt.value.value is None))):
            return "return_none"

    return None  # not a stub pattern


def get_return_annotation(func_node: ast.FunctionDef) -> str:
    if func_node.returns is None:
        return "untyped"
    return ast.unparse(func_node.returns)


def scan_file(filepath: Path) -> list[StubCandidate]:
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return []

    results = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body_type = classify_body(node)
        if body_type is None:
            continue
        has_docstring = (
            bool(node.body) and
            isinstance(node.body[0], ast.Expr) and
            isinstance(node.body[0].value, ast.Constant)
        )
        results.append(StubCandidate(
            filepath=str(filepath),
            lineno=node.lineno,
            name=node.name,
            has_docstring=has_docstring,
            body_type=body_type,
            return_annotation=get_return_annotation(node),
        ))
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: find_stubs.py <directory>")
        sys.exit(1)

    search_root = Path(sys.argv[1])
    all_stubs: list[StubCandidate] = []
    for py_file in search_root.rglob("*.py"):
        all_stubs.extend(scan_file(py_file))

    if not all_stubs:
        print("No stub candidates found.")
        return

    print(f"Stub candidates: {len(all_stubs)}\n")
    print(f"{'Location':<55} {'Function':<35} {'Body':<15} {'Return type'}")
    print("-" * 120)
    for stub in sorted(all_stubs, key=lambda s: (s.filepath, s.lineno)):
        location = f"{stub.filepath}:{stub.lineno}"
        print(f"{location:<55} {stub.name:<35} {stub.body_type:<15} {stub.return_annotation}")


if __name__ == "__main__":
    main()
```

### 4.2 Runtime Stub Sentinel with Decorator

```python
# src/utils/stub_guard.py
"""
Decorators to make unimplemented stubs loud rather than silent.
Apply @stub_raises to any function intended for future implementation.
In production, logs a CRITICAL-level warning so stubs are never silent.
"""
import functools
import logging
import os
from typing import Callable, TypeVar, ParamSpec

P = ParamSpec("P")
R = TypeVar("R")
logger = logging.getLogger(__name__)

# Set to True in CI/staging to raise; False in prod to log-and-return-none
_STRICT_STUBS = os.environ.get("STRICT_STUBS", "true").lower() == "true"


def stub_raises(func: Callable[P, R]) -> Callable[P, R]:
    """
    Mark a function as an unimplemented stub.
    In strict mode (CI/staging): raises NotImplementedError immediately.
    In lenient mode (production fallback): logs CRITICAL and returns None.
    """
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        message = (
            f"UNIMPLEMENTED STUB called: {func.__module__}.{func.__qualname__} "
            f"| file: {func.__code__.co_filename}:{func.__code__.co_firstlineno}"
        )
        if _STRICT_STUBS:
            raise NotImplementedError(message)
        logger.critical(message)
        return None  # type: ignore[return-value]
    wrapper.__stub__ = True  # type: ignore[attr-defined]
    return wrapper


def assert_no_stubs_called(module) -> None:
    """
    Call from test setup to assert that no stub-decorated functions exist
    in the given module. Useful for integration test suites.
    """
    import inspect
    for name, obj in inspect.getmembers(module, inspect.isfunction):
        if getattr(obj, "__stub__", False):
            raise AssertionError(
                f"Stub function '{name}' in module '{module.__name__}' "
                f"must be implemented before this test suite runs."
            )
```

```python
# Usage example — applying the decorator to the problematic stub
from src.utils.stub_guard import stub_raises


@stub_raises
def check_behavioral_consistency(account_id: str, post_history: list) -> dict | None:
    """
    Analyze post_history for coordinated inauthentic behavior signals.
    Returns a dict with keys: 'flagged' (bool), 'confidence' (float), 'reasons' (list).
    """
    # Implementation pending — tracked in PROJ-4821
```

### 4.3 Type-Aware Caller Analysis

```python
# scripts/detect_stub_callers.py
"""
Find all call sites where the return value of a stub-decorated function
is used in a boolean context (if result:, while result:, assert result).
These are the highest-risk sites where None silently bypasses logic.
Usage: python scripts/detect_stub_callers.py src/ src/moderation/checks.py check_behavioral_consistency
"""
import ast
import sys
from pathlib import Path


class BooleanContextVisitor(ast.NodeVisitor):
    def __init__(self, target_func: str):
        self.target_func = target_func
        self.risky_sites: list[tuple[int, str]] = []

    def _is_target_call(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == self.target_func:
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr == self.target_func:
                return True
        return False

    def _check_test_expr(self, test_node: ast.expr, lineno: int, context: str) -> None:
        # Direct call in boolean context: if check_behavioral_consistency(...):
        if self._is_target_call(test_node):
            self.risky_sites.append((lineno, f"direct boolean test in {context}"))
            return
        # Variable that might receive stub return value — not detectable purely via AST;
        # flag assignment sites where the return value is later used in boolean context
        if isinstance(test_node, ast.Name):
            self.risky_sites.append((
                lineno,
                f"variable '{test_node.id}' in boolean {context} — verify it cannot be None"
            ))

    def visit_If(self, node: ast.If) -> None:
        self._check_test_expr(node.test, node.lineno, "if")
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._check_test_expr(node.test, node.lineno, "while")
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._check_test_expr(node.test, node.lineno, "assert")
        self.generic_visit(node)


def scan_for_risky_callers(search_root: Path, stub_func_name: str) -> None:
    for py_file in search_root.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            continue

        visitor = BooleanContextVisitor(stub_func_name)
        visitor.visit(tree)
        for lineno, reason in visitor.risky_sites:
            print(f"  RISK {py_file}:{lineno} — {reason}")


def main():
    if len(sys.argv) < 3:
        print("Usage: detect_stub_callers.py <search_root> <stub_function_name>")
        sys.exit(1)
    search_root = Path(sys.argv[1])
    stub_name = sys.argv[2]
    print(f"Scanning for risky callers of '{stub_name}'...\n")
    scan_for_risky_callers(search_root, stub_name)


if __name__ == "__main__":
    main()
```

---

## 5. Fix

### 5.1 Immediate Remediation: Raise or Gate

```python
# src/moderation/checks.py — BEFORE (silent stub)
def check_behavioral_consistency(account_id: str, post_history: list) -> dict | None:
    pass


# src/moderation/checks.py — AFTER option A: raise until implemented
def check_behavioral_consistency(account_id: str, post_history: list) -> dict | None:
    raise NotImplementedError(
        "check_behavioral_consistency is not implemented. "
        "Tracked in PROJ-4821. Do not deploy this code path to production "
        "until this function is implemented."
    )


# src/moderation/checks.py — AFTER option B: explicit feature flag bypass
import os

_BEHAVIORAL_CHECK_ENABLED = os.environ.get("FEATURE_BEHAVIORAL_CHECK", "false") == "true"


def check_behavioral_consistency(account_id: str, post_history: list) -> dict | None:
    if not _BEHAVIORAL_CHECK_ENABLED:
        # Feature not implemented; return explicit disabled sentinel, not None.
        return {"flagged": False, "confidence": 0.0, "reasons": ["feature_disabled"]}
    raise NotImplementedError("Implementation pending PROJ-4821")


# src/moderation/orchestrator.py — caller update to handle the disabled case correctly
def run_moderation_checks(account_id: str, post_history: list) -> None:
    consistency_result = check_behavioral_consistency(account_id, post_history)

    # Explicit None guard — fail loudly if stub slips through
    if consistency_result is None:
        raise RuntimeError(
            f"check_behavioral_consistency returned None for account {account_id}. "
            "This indicates an unimplemented stub was called in production."
        )

    if consistency_result.get("flagged"):
        escalate_for_human_review(account_id, consistency_result)
```

### 5.2 Full Implementation with Regression Test

```python
# src/moderation/checks.py — full implementation replacing stub
from __future__ import annotations
import statistics
from datetime import datetime, timezone


_COORDINATION_WINDOW_HOURS = 2
_COORDINATION_THRESHOLD = 0.75
_MIN_POSTS_FOR_ANALYSIS = 5


def check_behavioral_consistency(account_id: str, post_history: list[dict]) -> dict:
    """
    Analyze post_history for coordinated inauthentic behavior signals.

    Args:
        account_id: The account identifier (used for logging only).
        post_history: List of dicts with keys 'timestamp' (ISO-8601 str),
                      'content_hash' (str), 'topic_cluster' (str).

    Returns:
        dict with keys:
            'flagged' (bool): True if coordination signals detected.
            'confidence' (float): 0.0–1.0 confidence score.
            'reasons' (list[str]): Human-readable signal descriptions.
    """
    if len(post_history) < _MIN_POSTS_FOR_ANALYSIS:
        return {"flagged": False, "confidence": 0.0, "reasons": ["insufficient_history"]}

    reasons: list[str] = []
    signals: list[float] = []

    # Signal 1: Temporal clustering — many posts within a short window
    try:
        timestamps = [
            datetime.fromisoformat(p["timestamp"]).replace(tzinfo=timezone.utc)
            for p in post_history
        ]
        timestamps.sort()
        intervals_hours = [
            (timestamps[i+1] - timestamps[i]).total_seconds() / 3600
            for i in range(len(timestamps) - 1)
        ]
        mean_interval = statistics.mean(intervals_hours)
        if mean_interval < _COORDINATION_WINDOW_HOURS:
            score = 1.0 - (mean_interval / _COORDINATION_WINDOW_HOURS)
            signals.append(score)
            reasons.append(f"temporal_clustering(mean_interval={mean_interval:.2f}h)")
    except (KeyError, ValueError):
        pass

    # Signal 2: Topic concentration — majority of posts on single cluster
    try:
        clusters = [p["topic_cluster"] for p in post_history]
        if clusters:
            most_common = max(set(clusters), key=clusters.count)
            concentration = clusters.count(most_common) / len(clusters)
            if concentration > _COORDINATION_THRESHOLD:
                signals.append(concentration)
                reasons.append(f"topic_concentration({most_common},{concentration:.0%})")
    except KeyError:
        pass

    if not signals:
        return {"flagged": False, "confidence": 0.0, "reasons": ["no_signals_detected"]}

    confidence = statistics.mean(signals)
    flagged = confidence >= _COORDINATION_THRESHOLD
    return {"flagged": flagged, "confidence": round(confidence, 4), "reasons": reasons}


# tests/moderation/test_checks.py — regression test ensuring stub is gone
import pytest
from src.moderation.checks import check_behavioral_consistency


def test_returns_dict_not_none():
    result = check_behavioral_consistency("acct_001", [])
    assert result is not None, "Function must never return None"
    assert isinstance(result, dict)
    assert "flagged" in result
    assert "confidence" in result
    assert "reasons" in result


def test_flags_temporal_clustering():
    from datetime import timedelta
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    posts = [
        {"timestamp": (base + timedelta(minutes=i*10)).isoformat(),
         "content_hash": f"h{i}", "topic_cluster": "politics"}
        for i in range(10)
    ]
    result = check_behavioral_consistency("acct_002", posts)
    assert result["flagged"] is True
    assert result["confidence"] > 0.5


def test_insufficient_history_not_flagged():
    result = check_behavioral_consistency("acct_003", [{"timestamp": "2024-01-01T00:00:00",
                                                         "content_hash": "abc",
                                                         "topic_cluster": "sports"}])
    assert result["flagged"] is False
    assert "insufficient_history" in result["reasons"]
```

---

## 6. Architectural Prevention

**Stubs must raise `NotImplementedError`, never `pass` or `return None`.** Establish this as a non-negotiable coding standard. Linters can be configured to flag functions whose entire body is `pass` or `return None` without a preceding docstring that includes a tracking ticket number.

**Require a linked ticket in stub docstrings.** A stub without a ticket reference is a wish, not a plan. The ticket provides an audit trail and enables automated staleness detection (stubs with closed or cancelled tickets that are still `pass`).

**Feature flag stubs explicitly.** A stub that is conditionally bypassed behind a disabled feature flag is safer than a silent `return None`. The flag name and disabled state are visible in configuration; a `pass` body is invisible in behavior.

**Integration tests must exercise the real implementation, not mocks of stubs.** If test fixtures mock `check_behavioral_consistency`, the integration test provides no assurance that the function works. At least one test per stub must call the real function or assert that the stub decorator raises `NotImplementedError`.

**Track stubs in a dedicated register.** A `STUBS.md` or a stub registry JSON file lists every known unimplemented function, its owning team, and its target implementation date. Review this file in every sprint planning session.

---

## 7. Anti-patterns

**`pass` as a "temporary" body without a raise.** The moment `pass` is committed without `raise NotImplementedError`, the function is indistinguishable from a function that intentionally returns `None`. Future readers cannot tell the difference.

**Caller code that treats `None` as "operation not applicable."** Writing `if result is not None: use(result)` in the caller makes the stub silently correct — the moderation check not running is treated the same as "no issues found." This conflates "not implemented" with "no issues," which are completely different states.

**Mocking stubs in tests.** If a test mocks `check_behavioral_consistency` to return `{"flagged": True, ...}`, the test passes without ever exercising the real function. The mock mask over the stub means the stub can remain indefinitely without any test failing.

**Implementing only the happy path and leaving edge-case branches as `pass`.** A function can be partially stubbed: the main logic is present, but error handling branches, edge cases, or secondary features are `pass`. These partial stubs are harder to detect than fully empty functions.

---

## 8. Edge Cases

**Abstract base class methods.** `abc.ABCMeta` methods with no implementation are intentional stubs — the protocol demands that subclasses implement them. These should use `raise NotImplementedError` explicitly and should be excluded from stub-detection scans if they are decorated with `@abc.abstractmethod`.

**Protocol methods.** Similarly, `typing.Protocol` class methods that define an interface contract are structural stubs, not implementation stubs.

**Stubs in third-party type-stub packages.** `.pyi` stub files for typing purposes use `...` (Ellipsis) as the body convention, not `pass` or `return None`. Stub detection tools should exclude `.pyi` files.

**Generator stubs.** A generator function with only `pass` in the body does not return `None` — it returns an empty generator. Callers iterating the result get an empty sequence, not an error and not `None`. This is a different failure mode: the operation silently produces no output.

**Async stubs.** `async def stub(): pass` returns a coroutine that resolves to `None` when awaited. The same boolean-context trap applies, compounded by the fact that an unawaited coroutine also silently does nothing.

---

## 9. Audit Checklist

```
STUB NEVER IMPLEMENTED AUDIT CHECKLIST
=======================================
Repository: ___________________________
Auditor:    ___________________________
Date:       ___________________________

[ ] Run find_stubs.py across src/ and record all candidates
[ ] For each stub candidate:
    [ ] Confirm it is called from at least one other location (not dead code)
    [ ] Check whether the caller uses the return value in a boolean context
    [ ] Check whether the caller checks for None before using the return value
    [ ] Check whether any test mocks this function (masking the stub)
    [ ] Locate the tracking ticket mentioned in the docstring, if any
    [ ] Determine ticket status: open/closed/cancelled/absent
[ ] For stubs with closed or cancelled tickets: escalate for implementation decision
[ ] For stubs with no ticket: create a ticket and add its reference to the docstring
[ ] Replace all stub bodies with raise NotImplementedError(ticket_reference)
[ ] Add STRICT_STUBS=true to CI environment variables
[ ] Add assert_no_stubs_called() to integration test suite setup
[ ] Verify that test suite catches the NotImplementedError for each stub
[ ] Prioritize implementation of stubs whose callers use return value in boolean context
[ ] After implementation: add regression test that asserts return is not None
[ ] Remove NotImplementedError raise once function is fully implemented
```

---

## 10. Further Reading

- **Python `abc` module documentation** — https://docs.python.org/3/library/abc.html — Covers the distinction between intentional interface stubs (`@abstractmethod`) and accidental implementation stubs. Understanding `ABCMeta` enforcement clarifies why `raise NotImplementedError` is the correct pattern for interface contracts.
- **`mypy` documentation — Return type checking** — https://mypy.readthedocs.io — Strict mypy configuration (`--strict`) combined with non-`Optional` return types will flag functions that can return `None` when the annotation says otherwise. The `--warn-return-any` flag is particularly relevant.
- **Fowler, M. — *Refactoring*, 2nd ed., "Replace Temp with Query"** — Discusses the risk of intermediate variables that mask the provenance of a value, directly relevant to the caller pattern `result = stub(); if result:`.
- **Gamma et al. — *Design Patterns*, "Template Method"** — The Template Method pattern provides a principled approach to defining stub methods in base classes with explicit `raise AbstractMethodError` semantics.
- **`pylint` — `W0107 unnecessary-pass`** — https://pylint.readthedocs.io — Pylint can be configured to flag empty function bodies. Combined with `--disable=all --enable=W0107`, this provides a lightweight CI gate.
- **Python Feature Flags libraries: `flipper`, `django-waffle`, `gatekeeper`** — Feature flags provide the correct mechanism for shipping code with unimplemented paths — the flag disables the path explicitly, rather than a stub silently returning `None`.
