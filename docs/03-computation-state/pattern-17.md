# Pattern #17 — Division Edge Case

| Field | Value |
|---|---|
| **ID** | Pattern-17 |
| **Category** | Computation & State |
| **Severity** | Medium |
| **Affected Frameworks** | LangChain / CrewAI / AutoGen / LangGraph / Custom |
| **Average Debugging Time (if undetected)** | 1 to 10 days |
| **Keywords** | division by zero, silent default, error propagation, ZeroDivisionError, None propagation |

---

## 1. Observable Symptoms

This pattern is characterized by a system that appears healthy — no exceptions, no alerts, normal throughput — while producing numerical outputs that are subtly but consequentially wrong. The damage accumulates silently.

**Immediate signals:**

- A computed metric (ratio, rate, lot size, score, allocation) returns a constant default value (0, 1, `min_value`, `max_value`) for an extended period, regardless of varying inputs.
- Downstream agents or dashboards receive values that are technically valid (non-null, correct type, within accepted range) but semantically uniform: every customer has the same score, every trade the same lot size, every alert the same severity.
- A `try/except ZeroDivisionError` block in production logs nothing, but the value it returns (0 or `min_value`) appears in every downstream record for a window of time corresponding to when the denominator was zero.

**Delayed signals (days later):**

- A monitoring dashboard shows a KPI frozen at a minimum or maximum value for an interval that aligns with a data gap in the denominator source.
- A report comparing agent-computed values against ground truth reveals systematic understatement or overstatement during specific time windows.
- A downstream agent that uses the computed value as an input to its own calculation produces outputs that are proportionally wrong: if `lot_size` was always `min_lot` instead of the correct value, every position opened during the affected window is undersized by the same factor.
- An on-call engineer notices that a calculated rate "got stuck at zero" during an upstream service outage, and asks whether the dependent system "did anything" during that time. It did — it used 0 as a real value.

**The distinguishing characteristic of this pattern** is that the division is never allowed to raise. It is caught and converted to a default. The default is plausible (non-null, in-range), so it passes every downstream guard. The error is invisible from the outside.

---

## 2. Field Story (Anonymized)

**Domain:** Monitoring dashboard for a cloud infrastructure platform.

An SRE team at a large e-commerce company built an AI agent to compute a real-time "error budget burn rate" for each of their 200 microservices. The burn rate formula was:

```
burn_rate = (error_rate_1h / slo_target_error_rate) * (window_hours / slo_window_hours)
```

where `error_rate_1h` was the fraction of requests that failed in the last hour, and `slo_target_error_rate` was the configured SLO threshold (typically 0.001 for a 99.9% availability SLO).

The agent fetched metric data from a time-series database. On rare occasions — during cold starts, during metric pipeline outages, or for brand-new services with no traffic yet — the query for `error_rate_1h` returned `None` instead of a float. The computation node handled this case:

```python
def compute_burn_rate(error_rate_1h, slo_target_error_rate, window_hours, slo_window_hours):
    try:
        return (error_rate_1h / slo_target_error_rate) * (window_hours / slo_window_hours)
    except (TypeError, ZeroDivisionError):
        return 0.0
```

The intent was defensive: return 0.0 (no burn) when data is unavailable. The logic was: "if we have no data, assume everything is fine."

The flaw was exposed during a 40-minute outage of the metric collection pipeline. During this window, `error_rate_1h` returned `None` for all 200 services simultaneously. All 200 burn rates were computed as `0.0`. The agent's alerting node had a guard: `if burn_rate > alert_threshold`. With burn_rate = 0.0, no alert fired.

Three services were experiencing actual error spikes during that same 40-minute window (the outage was in the metric collection layer, not in the services themselves). Their burn rates were truly non-zero, but the computation node had no way to distinguish "burn rate is 0.0 because error rate is genuinely 0.0" from "burn rate is 0.0 because the metric query returned None and we silently defaulted."

The SLO breach was discovered post-hoc from access logs. The incident review identified that 40 minutes of burn had gone undetected, eroding the error budget for three services to the point where a subsequent deployment the following week tripped the budget exhaustion threshold with no remaining headroom.

The root cause was not the `try/except` itself, but the choice to return `0.0` — a value that is semantically indistinguishable from "system is healthy" — when the actual state was "data unavailable, health unknown."

---

## 3. Technical Root Cause

Division-by-zero is a signal. When a denominator is zero, the computation is undefined. The numerically "safe" responses — return 0, return `min_value`, clamp to a range — are all lies: they assert a specific value where the correct assertion is "I do not know."

**The three failure modes:**

1. **`try/except ZeroDivisionError: return 0`** — the most common. Zero is almost always a meaningful domain value (0% error rate, 0 lot size, 0 cost). Returning it when the computation is undefined conflates two very different states.

2. **`denominator = denominator or 1`** — replaces zero with 1 silently. The resulting quotient is numerically the same as the numerator. This is wrong in almost every domain but produces plausible-looking values.

3. **`if denominator == 0: return min_value`** — explicit branch, but `min_value` is still a domain value. If `min_value` is 0.001 and the correct value during normal operation is 0.003, the default is off by 3x and will propagate through any system that treats it as a real measurement.

**Why the default propagates invisibly:**

Once a fake value enters the state, every downstream node receives a typed, range-valid input. Guards that check `if value is not None` and `if value >= 0` pass. Downstream computations produce numerically correct results given their (incorrect) inputs. The audit trail shows a complete, traceable chain of correct-looking operations. The only thing missing is the semantic truth that the chain's first input was fabricated.

**The compounding problem:**

In agent pipelines, computed values are often used as inputs to other computations:

```
burn_rate = f(error_rate, slo_target)      # returns 0.0 (fabricated)
alert_level = g(burn_rate, threshold)     # correctly computes: 0.0 < threshold → no alert
escalation = h(alert_level, history)      # correctly computes: no alert → no escalation
report = i(escalation, time_window)       # correctly reports: no escalation occurred
```

Every function after the first is operating correctly. The only error is at `f`. But the audit of `g`, `h`, and `i` will all show clean, correct behaviour. Debugging requires tracing all the way back to `f` and asking: "did this actually return 0.0, or did it return 0.0 because of an error?"

---

## 4. Detection

### 4.1 Manual Code Audit

Search for division operations protected by `try/except` or `if denominator == 0` that return a concrete default value.

**Questions to ask at each site:**

- Is the default value (`0`, `1`, `min_value`, `float('inf')`) semantically distinguishable from a real computation result?
- Does any downstream code check whether the value it received was a real computation or a default?
- Is there a structured log entry emitted when the default is used?
- Is the default value filtered out by any downstream alert or guard?

**Grep patterns:**

```bash
# Find ZeroDivisionError handlers that return a concrete value
grep -n "ZeroDivisionError" src/ -r -A 3

# Find division with an explicit zero-guard returning a non-None value
grep -n "if.*== 0" src/ -r -A 2 | grep "return [^N]"

# Find the pattern: denominator = x or 1
grep -n "or 1\b\|or 1\.0\b" src/ -r
```

For each match, verify that the returned value is either `None`, a sentinel constant (not a domain value), or that it triggers a downstream `None`-check that halts further computation.

### 4.2 Automated CI/CD

```python
# ci_division_audit.py
# Static analysis: find division operations whose ZeroDivisionError handler returns
# a concrete numeric literal (rather than None or re-raising).

import ast
import sys
from pathlib import Path


SUSPICIOUS_RETURNS = {
    ast.Constant,   # return 0, return 0.0, return 1, return "default"
}


def is_concrete_return(node: ast.Return) -> bool:
    """Return True if the return value is a numeric literal (not None)."""
    if node.value is None:
        return False
    if isinstance(node.value, ast.Constant) and node.value.value is None:
        return False
    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
        return True
    return False


def audit_try_except(node: ast.Try, path: Path, lineno: int) -> list[str]:
    issues = []
    for handler in node.handlers:
        catches_zero_div = (
            handler.type is None  # bare except
            or (
                isinstance(handler.type, ast.Name)
                and handler.type.id in {"ZeroDivisionError", "Exception", "ArithmeticError"}
            )
            or (
                isinstance(handler.type, ast.Tuple)
                and any(
                    isinstance(t, ast.Name)
                    and t.id in {"ZeroDivisionError", "Exception", "ArithmeticError"}
                    for t in handler.type.elts
                )
            )
        )
        if not catches_zero_div:
            continue

        for stmt in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
            if isinstance(stmt, ast.Return) and is_concrete_return(stmt):
                issues.append(
                    f"{path}:{lineno} — ZeroDivisionError handler returns a "
                    f"concrete numeric literal ({ast.unparse(stmt.value)}). "
                    "Consider returning None or raising a domain-specific exception."
                )
    return issues


def audit_file(path: Path) -> list[str]:
    issues = []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            issues.extend(audit_try_except(node, path, node.lineno))

    return issues


def main(src_dirs: list[str]) -> int:
    all_issues: list[str] = []
    for src_dir in src_dirs:
        for py_file in Path(src_dir).rglob("*.py"):
            all_issues.extend(audit_file(py_file))

    if all_issues:
        print("DIVISION EDGE CASE AUDIT FAILURES:")
        for issue in all_issues:
            print(f"  {issue}")
        return 1

    print("Division edge case audit passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["src"]))
```

Add to `.github/workflows/ci.yml`:

```yaml
- name: Division edge case audit
  run: python ci_division_audit.py src/
```

### 4.3 Runtime Production

Emit a structured log event every time a division-safe fallback is used, with a metric counter that can be alerted on separately from domain-level alerts.

```python
# runtime_division_monitor.py
import logging
import functools
from typing import Any, Callable, TypeVar

logger = logging.getLogger("division_monitor")

F = TypeVar("F", bound=Callable[..., Any])

# Sentinel: distinct from any domain value, including 0 and None.
class _DataUnavailable:
    """Represents a computation that could not be performed due to a missing denominator."""
    def __repr__(self):
        return "DataUnavailable"

    def __bool__(self):
        return False


DATA_UNAVAILABLE = _DataUnavailable()


def safe_divide(
    numerator: float | None,
    denominator: float | None,
    *,
    context: str = "",
    epsilon: float = 1e-12,
) -> float | _DataUnavailable:
    """
    Divide numerator by denominator. Return DATA_UNAVAILABLE (never 0 or a default)
    if either operand is None or if denominator is zero or near-zero.

    Callers MUST handle the DATA_UNAVAILABLE case explicitly.
    Never pass DATA_UNAVAILABLE to a downstream computation without checking.
    """
    if numerator is None:
        logger.warning(
            "safe_divide: numerator is None. Returning DATA_UNAVAILABLE. context=%s", context
        )
        return DATA_UNAVAILABLE

    if denominator is None:
        logger.warning(
            "safe_divide: denominator is None. Returning DATA_UNAVAILABLE. context=%s", context
        )
        return DATA_UNAVAILABLE

    if abs(denominator) < epsilon:
        logger.warning(
            "safe_divide: denominator=%s is near-zero (epsilon=%s). "
            "Returning DATA_UNAVAILABLE. context=%s",
            denominator, epsilon, context,
        )
        return DATA_UNAVAILABLE

    return numerator / denominator
```

---

## 5. Fix

### 5.1 Immediate Fix

Replace `try/except ZeroDivisionError: return 0` with an explicit `None` return and a `None`-check at the call site.

```python
# BEFORE — silently returns 0.0 when denominator is zero or data is missing
def compute_burn_rate(error_rate_1h, slo_target_error_rate, window_hours, slo_window_hours):
    try:
        return (error_rate_1h / slo_target_error_rate) * (window_hours / slo_window_hours)
    except (TypeError, ZeroDivisionError):
        return 0.0


# AFTER — returns None when the computation is undefined; caller handles explicitly
import logging

logger = logging.getLogger(__name__)


def compute_burn_rate(
    error_rate_1h: float | None,
    slo_target_error_rate: float | None,
    window_hours: float,
    slo_window_hours: float,
) -> float | None:
    """
    Compute error budget burn rate.

    Returns None if any input is missing or if slo_target_error_rate is zero.
    Callers must handle None explicitly — do not treat it as 0 (no burn).
    """
    if error_rate_1h is None:
        logger.warning(
            "compute_burn_rate: error_rate_1h is None (metric unavailable). "
            "Returning None — downstream must treat this as UNKNOWN, not 0."
        )
        return None

    if slo_target_error_rate is None or slo_target_error_rate == 0.0:
        logger.warning(
            "compute_burn_rate: slo_target_error_rate=%s is zero or None. "
            "Burn rate is undefined. Returning None.",
            slo_target_error_rate,
        )
        return None

    if slo_window_hours == 0.0:
        logger.warning(
            "compute_burn_rate: slo_window_hours is 0. "
            "Burn rate is undefined. Returning None."
        )
        return None

    return (error_rate_1h / slo_target_error_rate) * (window_hours / slo_window_hours)


# Call site: explicit None handling
def alerting_node(state: dict) -> dict:
    burn_rate = compute_burn_rate(
        state.get("error_rate_1h"),
        state.get("slo_target_error_rate"),
        state.get("window_hours", 1.0),
        state.get("slo_window_hours", 720.0),
    )

    if burn_rate is None:
        # Data unavailable: escalate to a "data gap" alert, not a burn alert.
        # Do NOT treat as burn_rate = 0 (no burn).
        return {
            **state,
            "alert": "DATA_GAP",
            "alert_detail": "Burn rate could not be computed. Metric data unavailable.",
        }

    if burn_rate > state.get("alert_threshold", 1.0):
        return {**state, "alert": "BURN_RATE_EXCEEDED", "burn_rate": burn_rate}

    return {**state, "alert": None, "burn_rate": burn_rate}
```

### 5.2 Robust Fix — Option/Result Type Pattern

Use a typed `Result` container that forces every caller to handle both the success and failure cases at compile time (via type checkers like `mypy`). This makes it structurally impossible to pass a division error downstream as a real value.

```python
# result_type.py — lightweight Result/Option implementation

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True)
class Ok(Generic[T]):
    """Represents a successful computation with a value."""
    value: T

    def is_ok(self) -> bool:
        return True

    def unwrap(self) -> T:
        return self.value

    def unwrap_or(self, default: T) -> T:
        return self.value

    def map(self, fn: Callable[[T], T]) -> "Ok[T]":
        return Ok(fn(self.value))


@dataclass(frozen=True)
class Err(Generic[E]):
    """Represents a failed computation with an error description."""
    error: E

    def is_ok(self) -> bool:
        return False

    def unwrap(self):
        raise ValueError(f"Called unwrap() on an Err: {self.error}")

    def unwrap_or(self, default):
        return default

    def map(self, fn) -> "Err[E]":
        return self  # errors pass through unchanged


Result = Ok | Err


# computation.py — using Result types for division

import logging
from result_type import Ok, Err, Result

logger = logging.getLogger(__name__)


def safe_divide(
    numerator: float,
    denominator: float,
    *,
    context: str = "",
    epsilon: float = 1e-12,
) -> Result:
    """
    Divide numerator by denominator.

    Returns Ok(result) on success.
    Returns Err(description) if denominator is zero or near-zero.
    Never returns a default numeric value on error.
    """
    if abs(denominator) < epsilon:
        msg = (
            f"Division undefined: denominator={denominator} is near-zero "
            f"(epsilon={epsilon}). context={context!r}"
        )
        logger.warning(msg)
        return Err(msg)

    return Ok(numerator / denominator)


def compute_burn_rate(
    error_rate_1h: float | None,
    slo_target_error_rate: float | None,
    window_hours: float,
    slo_window_hours: float,
) -> Result:
    if error_rate_1h is None:
        return Err("error_rate_1h is None — metric query returned no data.")

    if slo_target_error_rate is None:
        return Err("slo_target_error_rate is None — SLO configuration missing.")

    rate_ratio = safe_divide(error_rate_1h, slo_target_error_rate, context="rate_ratio")
    if not rate_ratio.is_ok():
        return rate_ratio  # propagate the error; do not substitute a default

    window_ratio = safe_divide(window_hours, slo_window_hours, context="window_ratio")
    if not window_ratio.is_ok():
        return window_ratio

    return Ok(rate_ratio.unwrap() * window_ratio.unwrap())


# alerting_node.py — Result forces explicit handling

from computation import compute_burn_rate
from result_type import Ok, Err


def alerting_node(state: dict) -> dict:
    result = compute_burn_rate(
        state.get("error_rate_1h"),
        state.get("slo_target_error_rate"),
        state.get("window_hours", 1.0),
        state.get("slo_window_hours", 720.0),
    )

    match result:
        case Ok(burn_rate):
            if burn_rate > state.get("alert_threshold", 1.0):
                return {**state, "alert": "BURN_RATE_EXCEEDED", "burn_rate": burn_rate}
            return {**state, "alert": None, "burn_rate": burn_rate}

        case Err(error):
            # The type system forces this branch to be handled.
            # It is impossible to accidentally treat the error as burn_rate=0.
            return {
                **state,
                "alert": "DATA_GAP",
                "alert_detail": f"Burn rate unavailable: {error}",
            }
```

**Why this is robust:**

- `mypy` or `pyright` will warn if a caller calls `.unwrap()` on a `Result` without first checking `.is_ok()`.
- `Err` cannot be passed to arithmetic operations (it is not a float), so any attempt to use it as if it were a real value will raise a `TypeError` at runtime rather than producing a silent wrong answer.
- The error message inside `Err` preserves the full context of why the computation failed, making post-hoc debugging tractable.

---

## 6. Architectural Prevention

**Principle:** treat computation nodes in agent pipelines as pure functions that must declare whether their output is valid. A node that cannot compute a value must return an explicit "no value" signal, not a fabricated value.

```
Input State
    │
    ▼
[Data Fetch Node]     ← returns None for unavailable fields, never a default
    │
    ▼
[Computation Node]    ← returns Result[float] | None; never a hardcoded default
    │
    ▼
[Validity Gate Node]  ← routes: Ok value → downstream; None/Err → data-gap handler
    │           │
    ▼           ▼
[Normal Path] [Data-Gap Path]  ← distinct alert type, distinct action
    │
    ▼
[Action Node]         ← only receives confirmed-valid values
```

**LangGraph implementation of the validity gate:**

```python
# langgraph_validity_gate.py

from langgraph.graph import StateGraph, END
from typing import TypedDict, Literal
from computation import compute_burn_rate
from result_type import Ok, Err


class BurnRateState(TypedDict):
    error_rate_1h: float | None
    slo_target_error_rate: float | None
    window_hours: float
    slo_window_hours: float
    burn_rate: float | None
    computation_error: str | None
    alert: str | None


def computation_node(state: BurnRateState) -> BurnRateState:
    result = compute_burn_rate(
        state["error_rate_1h"],
        state["slo_target_error_rate"],
        state["window_hours"],
        state["slo_window_hours"],
    )
    match result:
        case Ok(value):
            return {**state, "burn_rate": value, "computation_error": None}
        case Err(error):
            return {**state, "burn_rate": None, "computation_error": error}


def route_on_validity(state: BurnRateState) -> Literal["alert_node", "data_gap_node"]:
    """Conditional edge: route based on whether computation succeeded."""
    if state["burn_rate"] is not None:
        return "alert_node"
    return "data_gap_node"


def alert_node(state: BurnRateState) -> BurnRateState:
    threshold = 1.0
    alert = "BURN_RATE_EXCEEDED" if state["burn_rate"] > threshold else None
    return {**state, "alert": alert}


def data_gap_node(state: BurnRateState) -> BurnRateState:
    return {
        **state,
        "alert": "DATA_GAP",
    }


def build_pipeline() -> StateGraph:
    graph = StateGraph(BurnRateState)
    graph.add_node("computation", computation_node)
    graph.add_node("alert_node", alert_node)
    graph.add_node("data_gap_node", data_gap_node)
    graph.set_entry_point("computation")
    graph.add_conditional_edges("computation", route_on_validity)
    graph.add_edge("alert_node", END)
    graph.add_edge("data_gap_node", END)
    return graph.compile()
```

**Key invariant:** the `alert_node` is structurally guaranteed to receive a non-None `burn_rate`. The `data_gap_node` is structurally guaranteed to receive a `None` `burn_rate`. Neither node needs to check; the routing ensures correctness. This is the architectural equivalent of the Result type: the graph topology encodes the validity contract.

---

## 7. Anti-Patterns to Avoid

**Anti-pattern 1: Returning 0 on any arithmetic error.**

```python
# WRONG — 0 is a valid domain value (no burn, no loss, no rate)
def burn_rate(error_rate, slo_target):
    try:
        return error_rate / slo_target
    except ZeroDivisionError:
        return 0  # silently asserts "no burn" when data is actually missing
```

**Anti-pattern 2: Using `or` to replace zero denominators.**

```python
# WRONG — returns numerator/1 = numerator when denominator is 0
def ratio(numerator, denominator):
    return numerator / (denominator or 1)
```

This is particularly dangerous because the result is numerically close to correct when `denominator` is near 1, making it hard to detect in testing.

**Anti-pattern 3: Clamping to a minimum value.**

```python
# WRONG — lot_size=MIN_LOT is a real valid trade size; using it as a default hides the error
def compute_lot(budget, distance, unit_value):
    MIN_LOT = 0.01
    try:
        return budget / (distance * unit_value)
    except ZeroDivisionError:
        return MIN_LOT
```

The position is opened with `MIN_LOT`, which is a real action with real financial consequences.

**Anti-pattern 4: Checking `== 0` but not `is None` or near-zero.**

```python
# WRONG — misses None and near-zero (e.g., 0.0000001 from a floating point rounding)
def safe_div(a, b):
    if b == 0:
        return None
    return a / b  # still raises or returns inf if b is 1e-300
```

Use `abs(b) < epsilon` with a domain-appropriate epsilon.

**Anti-pattern 5: Logging the default but not the context.**

```python
# WRONG — log says "default used" but not WHICH computation, with WHICH inputs, at WHAT time
except ZeroDivisionError:
    logger.warning("Using default value")
    return 0
```

A log entry without the operation name, input values, and timestamp is useless for post-hoc debugging.

---

## 8. Edge Cases and Variants

**Variant A — Near-zero denominator (float precision).**
`denominator = 0.1 + 0.1 + 0.1 - 0.3` evaluates to `5.551115123125783e-17` in IEEE 754 arithmetic, not `0.0`. A check `if denominator == 0` does not catch it. The division returns `1.8e+16`, which silently corrupts any downstream computation.

Fix: use `abs(denominator) < epsilon` where `epsilon` is chosen based on the domain (e.g., `1e-9` for financial calculations, `1e-6` for sensor data).

**Variant B — Integer division truncating to zero.**
`lot = int(budget) // int(distance * unit_value)`. If `budget = 50` and `distance * unit_value = 60`, the result is `0`, not `ZeroDivisionError`. The guard `if denominator != 0` passes, but the result is semantically wrong (zero lots).

Fix: use float division for intermediate calculations, then convert to the target type only at the final step. Add a post-division guard: `if result == 0 and numerator != 0: raise ComputationError(...)`.

**Variant C — Denominator is `None` from an LLM output.**
An LLM-powered extraction node returns `{"distance": None}` when the model cannot parse a value. The computation node receives `None` and the division raises `TypeError`, not `ZeroDivisionError`. A handler that only catches `ZeroDivisionError` will not catch this.

Fix: check for `None` explicitly before the division. Catch both `TypeError` and `ZeroDivisionError` if using `try/except`, but always return `None` or `Err`, never a default.

**Variant D — Chained divisions amplifying the error.**
`result = (a / b) / (c / d)`. If `c / d` returns `0.0` (silently defaulted), the outer division raises `ZeroDivisionError`. Now the outer handler also returns `0.0`. Two consecutive defaults have been applied, and the debugging trace shows only the outer exception — the inner one is invisible.

Fix: use the `Result` type. `Err` propagates through the chain without being caught and re-defaulted.

**Variant E — Division inside a list comprehension or `map()`.**
`scores = [a / b for a, b in zip(numerators, denominators)]`. A single zero denominator raises `ZeroDivisionError` and truncates the list at that index. Callers receiving a shorter list than expected may silently use index-based access with wrong offsets.

Fix: use a safe division function that returns `None` for each element; filter `None` values explicitly before further processing; or fail fast and reject the entire batch.

---

## 9. Audit Checklist

Use this checklist during code review for any function that performs division or computes a ratio.

- [ ] No division operation returns a concrete numeric literal (0, 0.0, 1, `min_value`) when the denominator is zero or near-zero.
- [ ] Division-by-zero and near-zero conditions return `None` or a typed `Err`/`DataUnavailable` sentinel, never a domain value.
- [ ] Zero-denominator checks use `abs(denominator) < epsilon`, not `denominator == 0`, for float denominators.
- [ ] `None`-denominator is checked explicitly before the division, not only by catching `TypeError`.
- [ ] Every call site of a division function explicitly handles the `None` / `Err` return path and does not treat it as `0`.
- [ ] Downstream alert and guard nodes have separate branches for "value is zero" and "value is unavailable."
- [ ] LangGraph / CrewAI pipelines route `None`-valued computation results to a dedicated data-gap handler node, not to the normal action node.
- [ ] Log entries for division edge cases include: operation name, numerator value, denominator value, and timestamp.
- [ ] There is a structured metric counter for division-edge-case events, alertable independently from business-level alerts.
- [ ] Integer division results are validated post-division: a result of 0 when the numerator is non-zero triggers an error, not a silent action with size 0.
- [ ] The `Result` type (or equivalent `Optional` with documented `None` semantics) is used in function signatures so type checkers can enforce handling.
- [ ] CI pipeline runs `ci_division_audit.py` and fails the build if `ZeroDivisionError` handlers return concrete numeric literals.

---

## 10. Further Reading

**Internal cross-references:**

- [Pattern #16 — Missing Guard on Critical Operation](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/01-categories/08-filtering-decisions/pattern-16-missing-guard.md): the guard that was supposed to catch a bad computed value (e.g., `lot_size = 0` from a defaulted division) is the complementary defence layer. Both patterns must be fixed together: the division must return `None`, and the guard must reject `None`.
- [Pattern #09 — Silent State Mutation](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/01-categories/03-computation-state/pattern-09-silent-state-mutation.md): defaulted division values written into shared agent state infect all nodes that read that state, including those that run concurrently.
- [Pattern #07 — Type Confusion in State Handoff](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/01-categories/03-computation-state/pattern-07-type-confusion-state-handoff.md): the distinction between `int` 0 and `None` is erased when state is serialized to JSON (`null` vs. `0`), causing a deserialized `None` to become `0` and pass downstream checks.

**External references:**

- Hoare, C. A. R. "Null References: The Billion Dollar Mistake." Keynote at QCon London, 2009. The foundational argument for why returning a "safe" default (including `null`) instead of an explicit error is a design error, not a defensive strategy.
- Python Software Foundation. `decimal` module documentation. https://docs.python.org/3/library/decimal.html — the `decimal.InvalidOperation` exception and `decimal.ROUND_HALF_UP` rounding mode provide a standards-compliant alternative to IEEE 754 float arithmetic for financial computations.
- Python Software Foundation. `math.isfinite`, `math.isnan`, `math.isinf`. https://docs.python.org/3/library/math.html — use these to detect `inf` and `nan` results from near-zero denominators that did not raise but produced IEEE 754 special values.
- Wadler, Philip. "Theorems for Free!" *FPCA '89: Proceedings of the 4th international conference on functional programming languages and computer architecture*, 1989. The theoretical basis for why `Optional[T]` (or `Result[T, E]`) is a more honest type signature than `T` for computations that can fail.
- `returns` library for Python: https://returns.readthedocs.io/en/latest/ — production-grade `Result`, `Maybe`, and `IO` container types with full `mypy` support, implementing the patterns described in Section 5.2 of this document.
- All patterns in this playbook: https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns
