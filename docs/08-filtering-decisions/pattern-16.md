# Pattern #16 — Missing Guard on Critical Operation

| Field | Value |
|---|---|
| **ID** | Pattern-16 |
| **Category** | Filtering & Decisions |
| **Severity** | Critical |
| **Affected Frameworks** | LangChain / CrewAI / AutoGen / LangGraph / Custom |
| **Average Debugging Time (if undetected)** | 0 to 2 days |
| **Keywords** | guard, precondition, bounds check, destructive action, budget enforcement |

---

## 1. Observable Symptoms

The failure mode is deceptive: the system does not crash, no exception is raised, and logs show a completed operation. The damage is discovered only when observing downstream consequences.

**Immediate signals:**

- A critical external action (payment charge, position close, record deletion, API call) completes but the magnitude is wrong: zero units processed, negative quantities passed, a batch of size 0 sent, or a budget-exceeding call made.
- Monitoring dashboards show successful operation counts that do not match expected business volume (e.g., 47 charges processed, each for $0.00).
- An agent loop terminates cleanly but the side-effect it was supposed to produce is absent or inverted.
- Idempotency keys are consumed without the corresponding resource being created or modified.

**Delayed signals (hours to days later):**

- Reconciliation reports show mismatches between agent-reported outcomes and actual ledger state.
- A downstream agent receives a result that is technically valid (non-null, correct type) but semantically impossible (lot size of 0.0001 on a $500 000 position, a "completed" transfer of $0).
- On-call alerts fire for anomalous rates (charge success rate 100%, revenue $0).
- A human reviewer notices the operation ran but "did nothing", and no error was ever logged.

**The distinguishing characteristic of this pattern** is that the operation is executed with bad parameters rather than skipped or errored. Guards that are absent allow semantically invalid states to reach an executor that does not re-validate.

---

## 2. Field Story (Anonymized)

**Domain:** Payment gateway integration for a B2B SaaS platform.

An engineering team at a mid-sized logistics company integrated an AI agent to automate end-of-month invoice reconciliation. The agent would ingest a list of open invoices, compute amounts owed, and trigger batch payment charges via a payment gateway API.

The agent pipeline had three stages: (1) a data-fetching node that queried the invoicing database, (2) a computation node that grouped invoices by customer and summed amounts, and (3) an executor node that called the payment gateway's `charge_customer(customer_id, amount_cents)` endpoint.

During a routine deployment, a schema migration changed the invoicing table's `amount_due` column from storing values in cents (integer) to storing values in dollars (float). The computation node's summation logic was updated, but the conversion step that multiplied by 100 to produce `amount_cents` was removed under the assumption that the gateway would "handle it."

The gateway accepted any non-negative numeric value. The executor node had no guard checking that `amount_cents > 0` and no guard verifying that `amount_cents` was within a plausible range for the customer's historical billing (a simple bounds check: `min_expected <= amount_cents <= max_expected`). The operation was also missing a guard requiring explicit operator approval for any single charge above a configurable threshold.

The agent ran on the first of the following month. It successfully issued 312 charges. Every single charge was for an amount between $0.01 and $4.72 (the dollar values treated as cents), instead of the correct $1.00 to $472.00 range. Total revenue collected: $312.88 instead of $31 288.00. The gateway reported 312 successful transactions. No alert fired. The discrepancy was found during manual review four days later.

Recovery required issuing 312 corrective charges, negotiating with the gateway on duplicate-charge policies, and manually contacting 47 customers who had already closed their accounts payable for the period. The post-mortem identified three absent guards: (1) a lower-bound check on charge amount, (2) a plausibility check against historical averages, and (3) a human-approval gate for bulk operations above a dollar threshold.

---

## 3. Technical Root Cause

A guard is an explicit verification of a precondition that must be true before a critical operation is allowed to execute. Its absence creates a class of bugs where the system behaves "correctly" (no exception) with "wrong" inputs.

**Why guards are skipped:**

1. **Upstream trust assumption.** The executor assumes a previous node validated the data. If that node is refactored, the assumption becomes silently false.
2. **Type safety illusion.** A value has the right type (`int`, `float`) but not the right domain (`amount > 0`, `batch_size <= MAX_BATCH`). Type checkers pass; domain checks are absent.
3. **Happy-path development.** Guards are written reactively after a production incident rather than proactively as part of the operation's contract.
4. **Distributed validation responsibility.** In multi-agent systems, every node assumes another node checked. The result is that no node checked.

**The structural problem in agent pipelines:**

In a LangGraph or CrewAI pipeline, each node receives a state dictionary. Nothing in the framework enforces that a node's preconditions are met before it executes. A node that calls `payment_gateway.charge(state["amount_cents"])` will execute regardless of whether `state["amount_cents"]` is 0, negative, or astronomically large. The framework's job is orchestration, not domain validation. That responsibility belongs explicitly to the developer.

**Why "safe" fallbacks make it worse:**

When the gateway SDK clips a negative amount to 0 and returns a success code, the executor node has no way to distinguish "charged $0 intentionally" from "charged $0 because of a data error." The absence of a guard means the error signal is fully suppressed.

---

## 4. Detection

### 4.1 Manual Code Audit

Search for executor functions that call external APIs, write to databases, or perform irreversible actions. For each such function, verify the following checklist before the call site:

- Is there an explicit `if` or `assert` verifying the primary magnitude parameter is in a valid domain?
- Is there a plausibility check (min/max bounds) against a known reference value?
- Is there a dry-run or preview mode used in staging?
- Is there a human-in-the-loop gate for operations above a configurable threshold?

**Grep patterns to identify unguarded executor calls:**

```bash
# Find functions that call charge/send/delete/close/execute without a preceding guard
grep -n "def.*execut\|def.*charge\|def.*send_batch\|def.*close_position" src/ -r

# For each match, check the function body for absence of assert/if before the critical call
grep -A 20 "def charge_customer" src/executor.py | grep -c "assert\|if.*> 0\|if.*<="
```

A function body that reaches the critical call without any conditional branching on the primary magnitude parameter is a candidate for this pattern.

### 4.2 Automated CI/CD

```python
# ci_guard_audit.py
# Static analysis script: flag executor functions missing precondition guards.
# Run as part of the pre-merge CI pipeline.

import ast
import sys
from pathlib import Path

CRITICAL_CALL_NAMES = {
    "charge_customer", "send_batch", "close_position",
    "delete_records", "publish_event", "execute_order",
    "transfer_funds", "trigger_webhook",
}

GUARD_INDICATORS = {"assert", "if", "raise", "Guard", "check_precondition"}


def has_guard_before_call(func_body: list[ast.stmt], call_name: str) -> bool:
    """
    Return True if a guard statement (assert/if/raise) appears
    before the first occurrence of call_name in func_body.
    """
    for stmt in func_body:
        # A guard is any assert, raise, or if-statement before the critical call
        if isinstance(stmt, (ast.Assert, ast.Raise)):
            return True
        if isinstance(stmt, ast.If):
            return True
        # Check if this statement contains the critical call
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else ""
                )
                if name in CRITICAL_CALL_NAMES:
                    return False  # Reached critical call with no prior guard
    return True  # Critical call not found; no issue


def audit_file(path: Path) -> list[str]:
    issues = []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not has_guard_before_call(node.body, node.name):
                issues.append(
                    f"{path}:{node.lineno} — function '{node.name}' reaches "
                    f"a critical call without a preceding guard."
                )
    return issues


def main(src_dirs: list[str]) -> int:
    all_issues: list[str] = []
    for src_dir in src_dirs:
        for py_file in Path(src_dir).rglob("*.py"):
            all_issues.extend(audit_file(py_file))

    if all_issues:
        print("GUARD AUDIT FAILURES:")
        for issue in all_issues:
            print(f"  {issue}")
        return 1

    print("Guard audit passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["src"]))
```

Add to `.github/workflows/ci.yml`:

```yaml
- name: Guard audit
  run: python ci_guard_audit.py src/
```

### 4.3 Runtime Production

Instrument executor nodes with a guard-failure counter. When a guard would have blocked the operation, increment the counter and log a structured warning even if the operation is allowed to proceed in a degraded mode.

```python
# runtime_guard_monitor.py
import logging
from dataclasses import dataclass
from typing import Any, Callable
import functools

logger = logging.getLogger("guard_monitor")


@dataclass
class GuardViolation:
    operation: str
    parameter: str
    value: Any
    constraint: str


def monitor_guard(operation_name: str, metric_sink: Callable[[GuardViolation], None]):
    """
    Decorator: wrap a guard function so violations are always reported,
    even when the calling code catches and suppresses the exception.
    """
    def decorator(guard_fn: Callable) -> Callable:
        @functools.wraps(guard_fn)
        def wrapper(*args, **kwargs):
            try:
                return guard_fn(*args, **kwargs)
            except ValueError as exc:
                violation = GuardViolation(
                    operation=operation_name,
                    parameter=str(exc).split(":")[0],
                    value=args[0] if args else None,
                    constraint=str(exc),
                )
                metric_sink(violation)
                logger.error(
                    "guard_violation",
                    extra={
                        "operation": operation_name,
                        "violation": str(exc),
                        "value": args[0] if args else None,
                    },
                )
                raise  # Never suppress; let the caller decide
        return wrapper
    return decorator
```

---

## 5. Fix

### 5.1 Immediate Fix

Add explicit inline guards at every executor call site. This is a surgical, low-risk change deployable without architectural refactoring.

```python
# BEFORE — unguarded executor (pattern exhibiting the bug)
def process_invoice_batch(state: dict) -> dict:
    for customer_id, amount_cents in state["charges"].items():
        result = payment_gateway.charge_customer(customer_id, amount_cents)
        state["results"][customer_id] = result
    return state


# AFTER — inline guards added
MIN_CHARGE_CENTS = 100          # $1.00 minimum
MAX_CHARGE_CENTS = 10_000_000   # $100 000.00 maximum per transaction
MAX_BATCH_SIZE   = 500


def process_invoice_batch(state: dict) -> dict:
    charges = state["charges"]

    # Guard 1: batch size
    if len(charges) > MAX_BATCH_SIZE:
        raise ValueError(
            f"Batch size {len(charges)} exceeds maximum allowed {MAX_BATCH_SIZE}. "
            "Split into smaller batches or obtain explicit approval."
        )

    for customer_id, amount_cents in charges.items():
        # Guard 2: amount lower bound
        if amount_cents < MIN_CHARGE_CENTS:
            raise ValueError(
                f"charge_customer({customer_id}): amount_cents={amount_cents} "
                f"is below minimum {MIN_CHARGE_CENTS}. Possible unit conversion error."
            )

        # Guard 3: amount upper bound
        if amount_cents > MAX_CHARGE_CENTS:
            raise ValueError(
                f"charge_customer({customer_id}): amount_cents={amount_cents} "
                f"exceeds maximum {MAX_CHARGE_CENTS}. Requires manual approval."
            )

        result = payment_gateway.charge_customer(customer_id, amount_cents)
        state["results"][customer_id] = result

    return state
```

### 5.2 Robust Fix — Guard Decorator Pattern

Encapsulate precondition logic in reusable, composable guard decorators. This makes guards impossible to forget: they are part of the function's published interface, not an implementation detail inside the body.

```python
# guards.py — reusable guard infrastructure

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger("guards")

F = TypeVar("F", bound=Callable[..., Any])


class GuardError(ValueError):
    """Raised when a precondition guard fails. Never catch silently."""


def require(condition: bool, message: str) -> None:
    """Assert a precondition. Raises GuardError (not AssertionError) so it
    survives Python's -O optimisation flag."""
    if not condition:
        raise GuardError(message)


def bounded(
    param_name: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> Callable[[F], F]:
    """
    Decorator factory: enforce numeric bounds on a named keyword argument.

    Usage:
        @bounded("amount_cents", min_value=100, max_value=10_000_000)
        def charge_customer(customer_id: str, amount_cents: int) -> dict: ...
    """
    def decorator(fn: F) -> F:
        import inspect
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()
            value = bound_args.arguments.get(param_name)

            if value is None:
                raise GuardError(
                    f"{fn.__qualname__}: parameter '{param_name}' is missing."
                )

            if min_value is not None and value < min_value:
                raise GuardError(
                    f"{fn.__qualname__}: '{param_name}'={value} is below "
                    f"minimum allowed value {min_value}."
                )

            if max_value is not None and value > max_value:
                raise GuardError(
                    f"{fn.__qualname__}: '{param_name}'={value} exceeds "
                    f"maximum allowed value {max_value}."
                )

            logger.debug(
                "%s precondition passed: %s=%s in [%s, %s]",
                fn.__qualname__, param_name, value, min_value, max_value,
            )
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]
    return decorator


def non_empty_batch(param_name: str, max_size: int) -> Callable[[F], F]:
    """Decorator: reject empty or oversized batch parameters."""
    def decorator(fn: F) -> F:
        import inspect
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()
            batch = bound_args.arguments.get(param_name, [])

            require(len(batch) > 0, f"{fn.__qualname__}: '{param_name}' is empty.")
            require(
                len(batch) <= max_size,
                f"{fn.__qualname__}: '{param_name}' has {len(batch)} items, "
                f"exceeding max {max_size}.",
            )
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]
    return decorator


# executor.py — applying guards declaratively

from guards import bounded, non_empty_batch, GuardError


@bounded("amount_cents", min_value=100, max_value=10_000_000)
def charge_customer(customer_id: str, amount_cents: int) -> dict:
    """Charge a single customer. Guards enforced by decorator."""
    return payment_gateway.charge(customer_id, amount_cents)


@non_empty_batch("charges", max_size=500)
def process_invoice_batch(charges: list[tuple[str, int]]) -> list[dict]:
    """Process a batch of (customer_id, amount_cents) pairs."""
    results = []
    for customer_id, amount_cents in charges:
        results.append(charge_customer(customer_id, amount_cents))
    return results
```

**Why this is robust:**

- Guards are co-located with the function signature, not buried inside the body.
- Adding a new executor function requires explicitly choosing whether to apply `@bounded` or `@non_empty_batch`. The absence of a decorator is visible in code review.
- `GuardError` inherits from `ValueError`, not `AssertionError`, so it is not silenced by the `-O` flag or by broad `except Exception` clauses that log and continue.

---

## 6. Architectural Prevention

**Guard-first architecture:** treat guards as mandatory middleware, not optional defensive code. The principle is: no executor node may reach its critical call path without having passed through an explicit guard layer.

```
Input State
    │
    ▼
[Validation Node]  ← schema validation (Pydantic / TypedDict)
    │
    ▼
[Guard Node]       ← domain preconditions (bounds, business rules)
    │
    ▼
[Approval Gate]    ← human-in-the-loop for ops above threshold (optional)
    │
    ▼
[Executor Node]    ← critical operation; NEVER validates, only executes
    │
    ▼
[Audit Log Node]   ← structured record of what was executed and with what params
```

**LangGraph implementation of guard-as-node:**

```python
# langgraph_guard_node.py

from langgraph.graph import StateGraph, END
from typing import TypedDict


class InvoiceState(TypedDict):
    charges: list[tuple[str, int]]
    validated: bool
    results: list[dict]
    error: str | None


def guard_node(state: InvoiceState) -> InvoiceState:
    """
    Dedicated guard node in the LangGraph pipeline.
    Returns state with error set if any precondition fails.
    The executor node checks state["error"] and short-circuits if set.
    """
    errors = []

    if not state["charges"]:
        errors.append("Charge batch is empty.")

    for customer_id, amount_cents in state["charges"]:
        if amount_cents < 100:
            errors.append(
                f"Customer {customer_id}: amount_cents={amount_cents} < 100. "
                "Possible unit conversion error."
            )
        if amount_cents > 10_000_000:
            errors.append(
                f"Customer {customer_id}: amount_cents={amount_cents} > 10_000_000. "
                "Requires manual approval."
            )

    if errors:
        return {**state, "validated": False, "error": "; ".join(errors)}

    return {**state, "validated": True, "error": None}


def executor_node(state: InvoiceState) -> InvoiceState:
    """Executor never validates. It trusts the guard node completely."""
    if not state["validated"] or state["error"]:
        # Guard node already set the error; executor exits cleanly.
        return state

    results = []
    for customer_id, amount_cents in state["charges"]:
        results.append(payment_gateway.charge(customer_id, amount_cents))

    return {**state, "results": results}


def build_pipeline() -> StateGraph:
    graph = StateGraph(InvoiceState)
    graph.add_node("guard", guard_node)
    graph.add_node("executor", executor_node)
    graph.set_entry_point("guard")
    graph.add_edge("guard", "executor")
    graph.add_edge("executor", END)
    return graph.compile()
```

**Key principle:** the executor node contains zero validation logic. All domain knowledge about what constitutes a valid input lives in the guard node. This makes the guard auditable and testable in isolation.

---

## 7. Anti-Patterns to Avoid

**Anti-pattern 1: Trusting upstream validation.**

```python
# WRONG — assumes the orchestrator already validated the amount
def executor_node(state):
    # "The LLM output parser already checked this was positive"
    payment_gateway.charge(state["customer_id"], state["amount_cents"])
```

The orchestrator's validation is a contract that can be broken by any refactoring of the upstream node. The executor must own its own preconditions independently.

**Anti-pattern 2: Catching the exception and continuing.**

```python
# WRONG — the exception tells us the charge is invalid; swallowing it hides the bug
def safe_charge(customer_id, amount_cents):
    try:
        return payment_gateway.charge(customer_id, amount_cents)
    except PaymentGatewayError:
        return {"status": "skipped"}  # silently skips; no alert raised
```

A skipped charge looks identical to a successful $0 charge in aggregate metrics. Both produce 0 revenue. Only structured error propagation makes the difference auditable.

**Anti-pattern 3: Using `assert` for production guards.**

```python
# WRONG — assert is silenced by python -O; never use for production preconditions
assert amount_cents > 0, "Amount must be positive"
payment_gateway.charge(customer_id, amount_cents)
```

Use `GuardError(ValueError)` explicitly. It cannot be optimised away.

**Anti-pattern 4: Validating only the type, not the domain.**

```python
# WRONG — Pydantic confirms int, but not that the int is in a valid range
class ChargeRequest(BaseModel):
    customer_id: str
    amount_cents: int   # accepts 0, -1, 999_999_999 without complaint
```

Use `Annotated[int, Field(ge=100, le=10_000_000)]` in Pydantic v2 to express domain bounds at the model level.

**Anti-pattern 5: One global try/except around the entire batch.**

```python
# WRONG — a single failure in the batch silently skips all remaining charges
try:
    for customer_id, amount_cents in charges:
        payment_gateway.charge(customer_id, amount_cents)
except Exception:
    logger.warning("Batch partially failed")
```

Guard and execute each item individually. Track per-item success and failure explicitly.

---

## 8. Edge Cases and Variants

**Variant A — Zero budget remaining.**
An LLM agent checks `remaining_budget > 0` but does not check `remaining_budget >= estimated_cost`. The call proceeds, the budget goes negative, and subsequent budget checks still pass because the check is `> 0` not `>= 0`.

Fix: guard must be `remaining_budget >= estimated_cost`, not merely `remaining_budget > 0`.

**Variant B — Off-by-one in batch size.**
`batch_size <= MAX_BATCH` should be `batch_size < MAX_BATCH` if the gateway counts from 1. A batch of exactly `MAX_BATCH` items succeeds but triggers a rate-limit penalty that only appears in the next billing cycle.

Fix: use closed-interval bounds explicitly. Document whether the bound is inclusive or exclusive in the guard's docstring.

**Variant C — Currency precision mismatch.**
`amount_cents` is computed as `float(dollars) * 100` and rounded to the nearest integer. For `$9.999`, this produces `999` instead of `1000`. The guard `amount_cents > 0` passes, but the customer is undercharged by $0.01 at scale across millions of transactions.

Fix: use `round(float(dollars) * 100)` and add a plausibility check against the original dollar value: `abs(amount_cents / 100 - dollars) < 0.01`.

**Variant D — Idempotency key reuse.**
A retry mechanism reuses the same idempotency key for a corrected charge after a guard failure. The gateway deduplicates and returns the original (wrong) charge as a success.

Fix: generate a new idempotency key on every retry. Include the attempt number in the key: `f"{invoice_id}-attempt-{attempt}"`.

**Variant E — Guard passes but action is idempotent-unsafe.**
The guard checks that the amount is valid, but does not check whether this specific `(customer_id, invoice_id)` pair has already been charged. The agent runs twice due to an orchestration retry, and the customer is charged twice.

Fix: add an idempotency guard: query the ledger for an existing charge with the same `invoice_id` before issuing a new one.

---

## 9. Audit Checklist

Use this checklist during code review for any function that performs an irreversible or resource-consuming operation.

- [ ] Every executor function has at least one explicit guard (`if` / `assert` / `require`) before the critical call.
- [ ] Guards use `GuardError(ValueError)`, not bare `assert`, so they survive `-O` and are not silenced by broad `except Exception`.
- [ ] Numeric parameters have both a lower bound and an upper bound guard. Neither bound is `0` unless `0` is a genuinely valid domain value.
- [ ] Batch operations guard on batch size (both `> 0` and `<= MAX_SIZE`).
- [ ] Pydantic models for executor inputs use `Field(ge=..., le=...)` to express domain bounds, not just type annotations.
- [ ] There is no `try/except` that catches a `GuardError` and continues silently.
- [ ] LangGraph / CrewAI pipelines have a dedicated guard node that runs before the executor node.
- [ ] High-value operations (above a configurable dollar or unit threshold) have a human-approval gate.
- [ ] Guard failures are emitted as structured log events with the operation name, parameter name, and actual value.
- [ ] There is a dry-run mode for batch operations, exercised in staging before every production deployment.
- [ ] Idempotency is checked as a guard: the system verifies the operation has not already been applied before applying it.
- [ ] CI pipeline runs `ci_guard_audit.py` and fails the build if unguarded executor functions are detected.

---

## 10. Further Reading

**Internal cross-references:**

- [Pattern #17 — Division Edge Case](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/01-categories/03-computation-state/pattern-17-division-edge-case.md): a related failure mode where a "safe" default returned from a division error is accepted by a downstream guard because it is technically non-zero, causing the guard to pass on a semantically wrong value.
- [Pattern #09 — Silent State Mutation](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/01-categories/03-computation-state/pattern-09-silent-state-mutation.md): guards can fail to catch mutations to shared state that happen between the guard and the executor in concurrent pipelines.
- [Pattern #12 — Trusting LLM-Parsed Numbers](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/01-categories/08-filtering-decisions/pattern-12-trusting-llm-parsed-numbers.md): LLM output parsers do not enforce domain bounds; guards are the last line of defence against hallucinated magnitudes.

**External references:**

- Martin, Robert C. *Clean Code: A Handbook of Agile Software Craftsmanship*. Prentice Hall, 2008. Chapter 7 (Error Handling) and Chapter 9 (Unit Tests) on precondition specification.
- OWASP. *Input Validation Cheat Sheet*. https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html — the principle that validation must occur at the point of use, not only at the point of entry, applies directly to executor nodes in agent pipelines.
- Meyer, Bertrand. *Object-Oriented Software Construction*, 2nd ed. Prentice Hall, 1997. Chapter 11: Design by Contract — the canonical source for precondition, postcondition, and invariant theory.
- Python Software Foundation. `pydantic` v2 documentation: `Field` constraints. https://docs.pydantic.dev/latest/concepts/fields/#numeric-constraints
- LangGraph documentation: Node design patterns. https://langchain-ai.github.io/langgraph/concepts/
- All patterns in this playbook: https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns
