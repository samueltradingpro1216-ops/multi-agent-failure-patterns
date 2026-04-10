# Pattern #45 — Non-Standard Existence Check

**Category:** Detection & Monitoring
**Severity:** Low
**Tags:** validation, existence-check, logic-error, false-positive, filtering

---

## 1. Observable Symptoms

Non-standard existence checks produce silent correctness failures. No exception is raised. No error is logged. The system behaves as if it is validating data correctly, but the validation accepts inputs that should be rejected — or rejects inputs that should be accepted — at a rate that is invisible without deliberate measurement.

**Symptom class A — Validation that always passes.** A check written as `"confidence" in dir(obj)` will return `True` for virtually every Python object because `dir()` includes built-in attributes. The validation step runs, its return value is `True`, and execution continues with no indication that the check was meaningless. Logs show "validation passed: True" for every input, including structurally invalid ones.

**Symptom class B — Validation that rejects valid falsy values.** A check written as `hasattr(obj, "data") and obj.data` evaluates `obj.data` as a boolean. An empty list `[]`, an empty string `""`, or a zero `0` are all falsy but may be semantically valid. The check returns `False` for these cases, filtering out legitimate data.

**Symptom class C — Metrics that look healthy.** Because no exceptions are raised, error rate dashboards show zero failures. Throughput metrics may appear slightly lower than expected (class B), or quality metrics may degrade silently (class A passing bad data downstream). The issue typically surfaces through user-reported quality problems or through a code review that examines the validation logic directly.

**Symptom class D — Inconsistent behavior across object types.** `dir()` returns different sets of attributes for different object types. A check like `"predict" in dir(obj)` will return `True` for `obj` if any class in its MRO (method resolution order) defines `predict`, whether or not it is a user-defined attribute meaningful to the application. Tests that use objects with rich base classes may pass while production objects that use leaner types may fail, or vice versa.

---

## 2. Field Story

A company operating a customer-facing chatbot had built an intent detection layer that routed user messages to specialist handlers (billing, technical support, returns, etc.). The routing layer accepted any object that "had confidence," meaning a numeric confidence score attached by the intent classifier.

A developer wrote the following guard to prevent routing objects from classifiers that had not yet computed their confidence scores:

```python
def is_valid_intent_result(result: object) -> bool:
    return "confidence" in dir(result)
```

The intent was sensible: only route results that have a `confidence` attribute. However, `dir()` returns all attributes accessible on the object, including inherited ones. In the test suite, the `IntentResult` dataclass always had `confidence`, so all tests passed. In production, a misconfigured fallback classifier began emitting plain `dict` objects instead of `IntentResult` dataclasses. Plain `dict` objects also satisfy `"confidence" in dir({})` because `dict` does not have a `confidence` attribute, but the test dicts happened to include `"confidence"` as a key — and `"confidence" in dir({"confidence": 0.5})` is `False` (keys are not in `dir()`), while `"confidence" in dir(IntentResult(...))` is `True`.

The actual bug was the inverse: the check correctly rejected `dict` objects (because `confidence` is not in `dir({})`), but it accepted *any* `IntentResult` regardless of whether the `confidence` field had been populated, because the class definition itself put `confidence` into `dir()` even when the field was `None`.

The chatbot began routing low-confidence, ambiguous queries (confidence = `None`) to specialist handlers. Specialists received malformed routing packets, producing garbled auto-replies. Customer complaints about nonsensical automated responses spiked over three days before the routing layer was identified as the source.

A second bug was found in the same file:

```python
def has_entities(result: IntentResult) -> bool:
    return hasattr(result, "entities") and result.entities
```

This rejected results where `entities` was an empty list `[]` — a valid, meaningful state indicating "this intent was recognized but no entities were extracted." Handler logic that depended on `has_entities` silently skipped processing for a subset of intents.

---

## 3. Technical Root Cause

**Root cause 1: `dir()` checks attribute names, not instance-level values.**

```python
import dataclasses
from typing import Optional

@dataclasses.dataclass
class IntentResult:
    intent: str
    confidence: Optional[float] = None  # may be None — not yet computed

result_unscored = IntentResult(intent="billing_query")
result_scored   = IntentResult(intent="billing_query", confidence=0.87)

# Both return True — dir() reflects class structure, not instance data
print("confidence" in dir(result_unscored))  # True
print("confidence" in dir(result_scored))    # True

# The correct check uses hasattr + is-not-None:
print(hasattr(result_unscored, "confidence") and result_unscored.confidence is not None)  # False
print(hasattr(result_scored,   "confidence") and result_scored.confidence is not None)    # True
```

`dir(obj)` is a debugging and introspection tool. It returns the names of all attributes accessible on the object, including those inherited from base classes and those defined at class level. It does not distinguish between class-level attribute definitions and instance-level values. It does not check whether the attribute's value is meaningful. Using `dir()` to validate instance state is a category error.

**Root cause 2: Truthiness conflated with existence.**

```python
result_no_entities  = IntentResult(intent="greeting", entities=[])
result_has_entities = IntentResult(intent="order_query", entities=["order_id"])

# Wrong: rejects the valid empty-list case
def has_entities_buggy(r: IntentResult) -> bool:
    return hasattr(r, "entities") and r.entities  # [] is falsy

# Correct: tests existence separately from content
def has_entities_fixed(r: IntentResult) -> bool:
    return hasattr(r, "entities") and r.entities is not None

# Even more precise: test what you actually care about
def has_entities_precise(r: IntentResult) -> bool:
    return hasattr(r, "entities") and isinstance(r.entities, list) and len(r.entities) > 0
```

Combining `hasattr` with an implicit boolean test of the value collapses "the attribute exists with a valid value" and "the attribute's value is truthy" into a single check. These are different predicates. An empty list is a valid, non-error state for many domain objects.

---

## 4. Detection

### 4.1 Static Analysis

Scan for `dir(` usage in boolean contexts (conditionals, `and`/`or` chains, `return` statements that feed into conditionals) and for `hasattr(...) and obj.attr` patterns where `obj.attr` is used as a boolean.

```python
# scan_existence_checks.py — AST scan for non-standard existence checks
import ast
import sys
from pathlib import Path


class ExistenceCheckVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.findings: list[str] = []

    def visit_Compare(self, node: ast.Compare) -> None:
        # Detect: "attr_name" in dir(obj)
        for op, comparator in zip(node.ops, node.comparators):
            if (
                isinstance(op, ast.In)
                and isinstance(comparator, ast.Call)
                and isinstance(comparator.func, ast.Name)
                and comparator.func.id == "dir"
            ):
                self.findings.append(
                    f"{self.filename}:{node.lineno} — "
                    f"'in dir(...)' used as existence check; "
                    f"use 'hasattr()' instead"
                )
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # Detect: hasattr(obj, "attr") and obj.attr  (implicit truthiness test)
        if isinstance(node.op, ast.And) and len(node.values) >= 2:
            for i, val in enumerate(node.values[:-1]):
                next_val = node.values[i + 1]
                if (
                    isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Name)
                    and val.func.id == "hasattr"
                    and isinstance(next_val, ast.Attribute)
                ):
                    # Check if the attribute matches what hasattr is checking
                    if (
                        len(val.args) >= 2
                        and isinstance(val.args[1], ast.Constant)
                        and isinstance(next_val, ast.Attribute)
                        and next_val.attr == val.args[1].value
                    ):
                        self.findings.append(
                            f"{self.filename}:{node.lineno} — "
                            f"hasattr() followed by implicit truthiness of "
                            f"'.{next_val.attr}'; falsy-but-valid values will "
                            f"be rejected (use 'is not None' for optional fields)"
                        )
        self.generic_visit(node)


def scan_file(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    visitor = ExistenceCheckVisitor(str(path))
    visitor.visit(tree)
    return visitor.findings


def main(root: str) -> None:
    all_findings: list[str] = []
    for path in Path(root).rglob("*.py"):
        all_findings.extend(scan_file(path))
    if all_findings:
        print(f"Non-standard existence checks found ({len(all_findings)}):")
        for f in all_findings:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("No non-standard existence check patterns found.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
```

### 4.2 Property-Based Testing

Property-based tests can expose the failure by generating objects in states that unit tests with manually constructed fixtures miss.

```python
# test_existence_checks_property.py
import dataclasses
import pytest
from typing import Optional
from hypothesis import given, strategies as st


@dataclasses.dataclass
class IntentResult:
    intent: str
    confidence: Optional[float] = None
    entities: Optional[list] = None


# The buggy validators under test
def is_valid_intent_buggy(result: object) -> bool:
    return "confidence" in dir(result)


def has_entities_buggy(result: IntentResult) -> bool:
    return hasattr(result, "entities") and result.entities


# The correct validators
def is_valid_intent_fixed(result: object) -> bool:
    return hasattr(result, "confidence") and result.confidence is not None


def has_entities_fixed(result: IntentResult) -> bool:
    return hasattr(result, "entities") and result.entities is not None


@given(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_dir_check_accepts_scored_results(score: float):
    result = IntentResult(intent="test", confidence=score)
    # Both checks should agree for valid scored results
    assert is_valid_intent_buggy(result) == is_valid_intent_fixed(result)


def test_dir_check_accepts_unscored_result():
    """The buggy check accepts an unscored result; the fixed check rejects it."""
    result = IntentResult(intent="test", confidence=None)
    assert is_valid_intent_buggy(result) is True   # BUG: should be False
    assert is_valid_intent_fixed(result) is False   # CORRECT


def test_empty_list_entities_is_valid():
    """An empty entity list is a valid state; the buggy check rejects it."""
    result = IntentResult(intent="greeting", entities=[])
    assert has_entities_buggy(result) is False   # BUG: rejects valid state
    assert has_entities_fixed(result) is True    # CORRECT: [] is not None


@given(st.lists(st.text(), min_size=1))
def test_nonempty_entity_list_passes_both_checks(entities: list):
    result = IntentResult(intent="query", entities=entities)
    # Both should accept non-empty lists
    assert has_entities_buggy(result) is True
    assert has_entities_fixed(result) is True
```

### 4.3 Behavioral Monitoring

Add a validation audit log that records, for a sample of inputs, both the validator's decision and the ground-truth outcome (determined later). Compare to detect systematic miscategorization.

```python
# validation_audit.py — audit wrapper for existence-check validators
import logging
import random
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


class ValidatorAudit:
    """
    Wraps a validator function and logs sampled decisions for offline analysis.
    Usage:
        audit = ValidatorAudit(is_valid_intent_fixed, sample_rate=0.01)
        if audit.validate(result):
            route(result)
    """

    def __init__(self, validator: Callable[[Any], bool], sample_rate: float = 0.05):
        self._validator = validator
        self._sample_rate = sample_rate
        self._total = 0
        self._accepted = 0
        self._rejected = 0

    def validate(self, obj: Any) -> bool:
        result = self._validator(obj)
        self._total += 1
        if result:
            self._accepted += 1
        else:
            self._rejected += 1

        if random.random() < self._sample_rate:
            log.info(
                "validator_audit validator=%s decision=%s obj_type=%s obj_repr=%.120r",
                self._validator.__name__,
                "ACCEPT" if result else "REJECT",
                type(obj).__name__,
                obj,
            )

        return result

    def acceptance_rate(self) -> float:
        return self._accepted / self._total if self._total > 0 else 0.0

    def log_summary(self) -> None:
        log.info(
            "validator_audit_summary validator=%s total=%d accepted=%d "
            "rejected=%d acceptance_rate=%.3f",
            self._validator.__name__,
            self._total, self._accepted, self._rejected,
            self.acceptance_rate(),
        )
```

A validator with a 100% acceptance rate on all inputs is a strong signal of an always-true check (class A). A validator with an unexpectedly low acceptance rate may be rejecting falsy-but-valid values (class B).

---

## 5. Fix

### 5.1 Immediate Fix — Replace `dir()` with `hasattr()` and explicit null checks

```python
# BEFORE (buggy)
def is_valid_intent_result(result: object) -> bool:
    return "confidence" in dir(result)

def has_entities(result: IntentResult) -> bool:
    return hasattr(result, "entities") and result.entities

# AFTER (fixed)
def is_valid_intent_result(result: object) -> bool:
    """Returns True only if result has a non-None confidence score."""
    return hasattr(result, "confidence") and result.confidence is not None

def has_entities(result: IntentResult) -> bool:
    """Returns True if entities field is present and not None (may be empty)."""
    return hasattr(result, "entities") and result.entities is not None
```

### 5.2 Preferred Fix — Typed Validation with `isinstance` and Pydantic/dataclasses

Avoid runtime introspection checks entirely. Validate structure at the boundary using typed constructors. If the object was constructed as the correct type, structural validity is guaranteed by the type system.

```python
# PREFERRED — type-safe validation using isinstance + typed dataclass
import dataclasses
from typing import Optional


@dataclasses.dataclass
class IntentResult:
    intent: str
    confidence: float          # not Optional — must be provided at construction
    entities: list[str]        # not Optional — must be provided at construction

    def __post_init__(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )


def route_intent(result: object) -> None:
    """Route only if result is a well-formed IntentResult."""
    if not isinstance(result, IntentResult):
        raise TypeError(
            f"Expected IntentResult, got {type(result).__name__}: {result!r}"
        )
    # At this point, result.confidence and result.entities are guaranteed
    # by the constructor — no further existence checks needed.
    if result.confidence >= 0.7:
        dispatch_to_specialist(result)
    else:
        dispatch_to_fallback(result)


def dispatch_to_specialist(result: IntentResult) -> None:
    pass  # implementation omitted


def dispatch_to_fallback(result: IntentResult) -> None:
    pass  # implementation omitted
```

For API boundaries where you cannot control what types arrive, use Pydantic's `model_validate` (v2) or equivalent to enforce shape at the deserialization point. This makes invalid structures fail loudly at ingress rather than silently at validation.

---

## 6. Architectural Prevention

**1. Parse, don't validate.** Validation functions that check for the presence of fields on arbitrary objects indicate that the system is receiving data whose shape is not enforced at ingress. The architectural fix is to parse untrusted data into typed domain objects at the earliest possible point (HTTP handler, queue consumer, classifier output processor) and fail loudly if the shape is wrong. Downstream code then operates on typed objects and needs no existence checks.

**2. Banned function list in linting.** Add `dir(` to the project's flake8/ruff banned-function list when used in boolean comparisons. The static scanner (Section 4.1) can also be run as a pre-commit hook.

**3. Explicit contracts in function signatures.** Type annotations that express optionality (`Optional[float]` vs `float`) communicate to readers that a field may be absent. Coupling this with runtime validation in `__post_init__` or Pydantic validators makes structural constraints self-documenting and enforced.

**4. Separate structural validation from semantic validation.** A function like `is_valid_intent_result` should not need to check whether `confidence` exists — that is a structural concern handled by the type. It should check semantic constraints: is the confidence score in a valid range? Is the intent name a known value? Mixing structural and semantic validation in ad hoc introspection functions is a sign that structural enforcement is missing upstream.

---

## 7. Anti-patterns

**Anti-pattern A — Using `dir()` for any runtime check.** `dir()` is a debugging and REPL tool. It is not appropriate for runtime validation in production code. There is no correct use of `dir()` in a production validator.

**Anti-pattern B — `getattr(obj, "attr", None)` then testing truthiness.** `getattr(obj, "attr", None)` returns `None` if the attribute is absent, but also returns `None` if the attribute is present and set to `None`. Testing the result with a boolean check (`if getattr(...)`) conflates absence with falsy values. Use `sentinel = object(); val = getattr(obj, "attr", sentinel); val is not sentinel` to distinguish absence from falsy presence.

**Anti-pattern C — Validator functions with no test coverage for falsy-but-valid inputs.** Test suites for validators typically use positive examples (valid objects) and structurally invalid examples (wrong type, missing field). They rarely test domain-valid falsy values (`0`, `[]`, `""`, `False`). Property-based testing (Section 4.2) systematically covers this gap.

**Anti-pattern D — Silent validator failures.** A validator that returns `False` for a rejected input with no logging makes it impossible to distinguish "validator correctly rejected invalid input" from "validator incorrectly rejected valid input" without instrumenting the validator post-hoc.

---

## 8. Edge Cases

**Inherited class attributes.** `hasattr(obj, "predict")` returns `True` if `predict` is defined anywhere in the MRO, including base classes like `object` or framework base classes. Always verify that `hasattr` is checking for attributes specific to the domain type, not accidentally inherited ones.

**Properties and descriptors.** `hasattr(obj, "attr")` calls the property getter to check if an `AttributeError` is raised. A property that raises `AttributeError` will cause `hasattr` to return `False` even though the attribute is defined. A property with a side effect will execute that side effect during the `hasattr` call. Be aware of this when validating objects with non-trivial property implementations.

**Dataclasses with `field(default=dataclasses.MISSING)`.** If a dataclass field has no default and the object was constructed via `__init__`, the field is always present. However, if the object was constructed by deserializing incomplete JSON (e.g., via `dacite` or a custom factory), fields may be absent even on typed objects. Deserialization boundaries are the correct place to enforce completeness.

**MRO and `dir()` stability.** The set of names returned by `dir()` is defined to be "sorted and deduplicated" but its exact contents are implementation-defined and may vary between Python versions. Code that relies on `dir()` for validation is fragile across Python version upgrades.

---

## 9. Audit Checklist

- [ ] Search codebase for `"<name>" in dir(` patterns; replace with `hasattr()`.
- [ ] Search codebase for `hasattr(obj, "attr") and obj.attr` patterns; review each for falsy-but-valid cases and replace with `is not None` or type-specific checks.
- [ ] Verify that validator functions have test coverage for falsy-but-valid inputs (`0`, `0.0`, `[]`, `""`, `False`).
- [ ] Confirm that objects crossing subsystem boundaries are parsed into typed domain objects at ingress, not validated with introspection at use.
- [ ] Add `dir(` to the linting banned-function list for boolean contexts.
- [ ] Add property-based tests (Hypothesis) to any validator that accepts numeric, list, or string values.
- [ ] Verify that validator functions log rejected inputs at an appropriate level for operational visibility.
- [ ] Review the acceptance rate of each production validator (Section 4.3) — a 100% or near-100% acceptance rate warrants inspection.
- [ ] Confirm that `__post_init__` (dataclasses) or Pydantic validators enforce value-level constraints at construction time.
- [ ] Document which validators check structural constraints vs. semantic constraints, and confirm structural constraints are enforced upstream.

---

## 10. Further Reading

- Python documentation: `dir()` — `https://docs.python.org/3/library/functions.html#dir`
- Python documentation: `hasattr()` — `https://docs.python.org/3/library/functions.html#hasattr`
- Hypothesis documentation (property-based testing) — `https://hypothesis.readthedocs.io/`
- Pydantic documentation: validators and model validation — `https://docs.pydantic.dev/latest/`
- "Parse, Don't Validate" by Alexis King — `https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/`
- Python documentation: `dataclasses.__post_init__` — `https://docs.python.org/3/library/dataclasses.html`
- CWE-1283: Mutable Attestation or Measurement Reporting Data — `https://cwe.mitre.org/data/definitions/1283.html`
