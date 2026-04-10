# Pattern #33 — Value Written to Wrong Dict

**Category:** Computation & State
**Severity:** Medium
**Tags:** `state`, `dict`, `computation`, `silent-bug`, `wrong-consumer`, `a-b-testing`

---

## 1. Observable Symptoms

This pattern is characterized by the near-total absence of hard errors. The following symptoms are subtle and require deliberate investigation to surface.

**Metric divergence with no obvious cause.** Two consumers reading from separate dictionaries produce results that are inconsistent with each other, yet neither raises an exception. In an A/B testing context, variant A and variant B appear to produce statistically identical outcomes even when the assignment logic is verified to be splitting traffic correctly. The equivalence persists across restarts, deploys, and configuration changes.

**Stale or default values appear in production reads.** A consumer that reads from dictionary B consistently receives the default or zero-initialized value. The value looks plausible — it is within the valid range — so it does not trigger validation errors or alarm human reviewers. The bug manifests only when the expected value is compared against an independent source of truth (e.g., a database record or a log trace from the computation step).

**The computed value is not lost — it is misrouted.** Inspection of dictionary A (the wrong destination) reveals correct, up-to-date values that should have gone to dictionary B. Dictionary B contains values from a prior cycle or initialization. This asymmetry is the diagnostic fingerprint of this pattern.

**No exception, no traceback.** Python dicts accept writes to any key without complaint. The bug is entirely invisible at runtime unless the consuming code explicitly checks whether the value it received matches what was computed. Most code does not perform this cross-check.

**A/B experiment results are corrupted silently.** If the misrouted value is an experiment assignment counter or a feature-flag score, both experiment arms receive the same underlying data, making the experiment statistically invalid. The damage accumulates over the entire experiment run before anyone notices that the treatment has had no measurable effect.

---

## 2. Field Story

An A/B testing platform computed per-user feature scores every cycle. The platform maintained two state dictionaries: `control_scores` (users in the control group) and `treatment_scores` (users in the treatment group). A scoring function was refactored to accept a `context` parameter that included both dictionaries under the keys `"control"` and `"treatment"`.

During the refactor, the developer renamed the output variable from `score` to `computed_score` and updated the write line. The original line was:

```python
treatment_scores[user_id] = score
```

After the refactor, the line became:

```python
context["control"][user_id] = computed_score
```

The developer intended to write `context["treatment"][user_id]`. The typo (`"control"` instead of `"treatment"`) was not caught by tests because the test suite verified only that `context["control"]` was populated — it did not assert that `context["treatment"]` held the new value.

The bug ran in production for 11 days. During that period, three separate experiments concluded. All three reported null effects. Post-mortem analysis revealed that treatment users had been scored using the previous cycle's values (the stale defaults in `treatment_scores`), while `control_scores` accumulated a mix of control and treatment computed values. None of the three experiment results were usable. The business cost was the loss of three experiment cycles — approximately six weeks of planned experiment time — plus the engineering time to reproduce, diagnose, and remediate the issue.

---

## 3. Technical Root Cause

The root cause is that two structurally similar dictionaries coexist in the same scope, making it easy to target the wrong one without any language-level safeguard.

```python
# Buggy pattern
def score_users(
    users: list[dict],
    control_scores: dict[str, float],
    treatment_scores: dict[str, float],
) -> None:
    for user in users:
        computed_score = _compute_score(user)
        if user["group"] == "treatment":
            control_scores[user["id"]] = computed_score   # BUG: wrong dict
        else:
            control_scores[user["id"]] = computed_score
```

The specific failure modes that allow this pattern to persist:

1. **No write-side validation.** Python's `dict.__setitem__` accepts any hashable key and any value. There is no mechanism to say "this dict should only receive writes from function X."

2. **No cross-dict consistency check.** The code that reads from `treatment_scores` does not verify that the values were written in the current cycle. A stale value from the previous cycle is indistinguishable from a freshly computed one unless a timestamp or cycle ID is stored alongside the score.

3. **Test coverage gap.** Tests typically verify that the output dict is non-empty and contains values in the expected range. They rarely assert that the *other* dict was *not* modified, or that the written dict contains exactly the keys that should have been written.

4. **Context parameter anti-pattern.** Passing a `context` dict that bundles multiple sub-dicts under string keys compounds the risk. A typo in the key string (`"control"` vs `"treatment"`) redirects all writes silently.

---

## 4. Detection

### 4.1 Manual Code Audit

Search for functions that accept multiple dict parameters with similar names (`control_*` / `treatment_*`, `cache_a` / `cache_b`, `primary` / `secondary`, `results` / `staging`). For each such function:

1. List every assignment of the form `dict_param[key] = value`.
2. Confirm that the assignment targets the dict that corresponds to the branch condition (e.g., inside `if group == "treatment"`, the write must go to `treatment_scores`).
3. Check whether any test asserts the *negative*: that the non-targeted dict was not modified.

Flag any function where a `context` or `state` dict bundles multiple sub-dicts under string keys. These are high-risk sites because a key string typo produces a silent wrong-dict write.

### 4.2 Automated CI/CD

Add cross-dict consistency assertions to unit tests. For every scoring or computation function that writes to one of N similar dicts:

```python
import pytest
from copy import deepcopy


def test_treatment_score_written_to_correct_dict():
    control_scores: dict[str, float] = {}
    treatment_scores: dict[str, float] = {}

    users = [
        {"id": "u1", "group": "control", "features": [1.0, 0.5]},
        {"id": "u2", "group": "treatment", "features": [1.2, 0.8]},
    ]

    score_users(users, control_scores, treatment_scores)

    # Positive assertion: correct dict received the value
    assert "u2" in treatment_scores, "treatment user score missing from treatment_scores"
    assert "u1" in control_scores, "control user score missing from control_scores"

    # Negative assertion: wrong dict was not contaminated
    assert "u2" not in control_scores, "treatment user score leaked into control_scores"
    assert "u1" not in treatment_scores, "control user score leaked into treatment_scores"
```

Use mutation testing (e.g., `mutmut`) to verify that swapping `control_scores` and `treatment_scores` in the write statement causes at least one test to fail. If mutation survives, the test suite is insufficient.

### 4.3 Runtime Production

Attach cycle IDs to written values. Instead of storing bare floats, store `(cycle_id, score)` tuples. The consumer checks that the cycle ID matches the current cycle before using the score. A mismatch means the value was not written in this cycle — either it is stale (from a prior cycle) or it was never written (default).

```python
import time

cycle_id = int(time.time())
treatment_scores[user_id] = (cycle_id, computed_score)

# In consumer:
stored = treatment_scores.get(user_id, (None, 0.0))
if stored[0] != current_cycle_id:
    logger.warning("stale or missing score for user %s in treatment_scores", user_id)
    score = 0.0
else:
    score = stored[1]
```

This does not prevent the bug but detects it on the first cycle in which it occurs, rather than after 11 days.

---

## 5. Fix

### 5.1 Immediate

Replace the string-key context dict with explicit named parameters. Make the write targets structurally distinct:

```python
# Before: context bundle (high typo risk)
def score_users(users, context):
    for user in users:
        computed_score = _compute_score(user)
        if user["group"] == "treatment":
            context["treatment"][user["id"]] = computed_score

# After: explicit parameters (typo produces NameError, not silent misdirection)
def score_users(users, control_scores, treatment_scores):
    for user in users:
        computed_score = _compute_score(user)
        if user["group"] == "treatment":
            treatment_scores[user["id"]] = computed_score
        else:
            control_scores[user["id"]] = computed_score
```

Add negative assertions to all relevant tests immediately (see Section 4.2).

### 5.2 Robust

Introduce a typed `ScoreStore` class that enforces write routing at the type level and provides cycle-stamped consistency checking:

```python
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ExperimentGroup(str, Enum):
    CONTROL = "control"
    TREATMENT = "treatment"


@dataclass
class ScoredEntry:
    cycle_id: int
    score: float


class ScoreStore:
    """
    Typed, cycle-aware store for A/B experiment scores.
    Separate stores are created for each experiment group, eliminating
    the possibility of a string-key typo routing writes to the wrong
    group without a runtime error.
    """

    def __init__(self, group: ExperimentGroup, cycle_id: int) -> None:
        self.group = group
        self.cycle_id = cycle_id
        self._data: dict[str, ScoredEntry] = {}

    def write(self, user_id: str, score: float) -> None:
        self._data[user_id] = ScoredEntry(cycle_id=self.cycle_id, score=score)

    def read(self, user_id: str, current_cycle_id: int) -> Optional[float]:
        entry = self._data.get(user_id)
        if entry is None:
            logger.warning(
                "no score for user %s in %s store", user_id, self.group.value
            )
            return None
        if entry.cycle_id != current_cycle_id:
            logger.warning(
                "stale score for user %s in %s store: cycle %d vs current %d",
                user_id,
                self.group.value,
                entry.cycle_id,
                current_cycle_id,
            )
            return None
        return entry.score

    def user_ids(self) -> set[str]:
        return set(self._data.keys())

    def __len__(self) -> int:
        return len(self._data)


def _compute_score(user: dict) -> float:
    """Placeholder scoring function."""
    return sum(user.get("features", [0.0]))


def score_users(
    users: list[dict],
    control_store: ScoreStore,
    treatment_store: ScoreStore,
) -> None:
    """
    Routes each user's computed score to the correct typed store.
    The stores are distinct objects; there is no string key that can be
    mistyped to redirect a write.
    """
    for user in users:
        computed_score = _compute_score(user)
        user_id = user["id"]
        if user["group"] == ExperimentGroup.TREATMENT.value:
            treatment_store.write(user_id, computed_score)
        else:
            control_store.write(user_id, computed_score)


def run_experiment_cycle(users: list[dict]) -> tuple[ScoreStore, ScoreStore]:
    cycle_id = int(time.time())
    control_store = ScoreStore(ExperimentGroup.CONTROL, cycle_id)
    treatment_store = ScoreStore(ExperimentGroup.TREATMENT, cycle_id)
    score_users(users, control_store, treatment_store)
    logger.info(
        "cycle %d complete: %d control, %d treatment scores written",
        cycle_id,
        len(control_store),
        len(treatment_store),
    )
    return control_store, treatment_store


# --- demonstration ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    users = [
        {"id": f"u{i}", "group": "treatment" if i % 2 == 0 else "control",
         "features": [float(i), 0.5]}
        for i in range(1, 11)
    ]

    ctrl, treat = run_experiment_cycle(users)

    # Verify no cross-contamination
    ctrl_ids = ctrl.user_ids()
    treat_ids = treat.user_ids()
    overlap = ctrl_ids & treat_ids
    assert not overlap, f"Users appeared in both stores: {overlap}"
    print(f"Control users: {sorted(ctrl_ids)}")
    print(f"Treatment users: {sorted(treat_ids)}")
```

---

## 6. Architectural Prevention

**One dict per logical role, never bundled.** Dictionaries that serve distinct roles must be distinct objects passed as distinct parameters. Bundling them under string keys inside a `context` or `state` dict creates a class of silent routing bugs that the type system cannot catch.

**Typed stores over raw dicts for shared state.** When a dict is written by one function and read by another, wrapping it in a class adds a layer of intent documentation and enables cycle-ID or version stamping. Raw dicts are appropriate for local, short-lived state. Shared, long-lived state deserves a named class.

**Negative test coverage as a first-class requirement.** Every test that verifies a value was written to dict B should also verify that dict A was not modified. Make this a code review checklist item for any function that accepts two or more structurally similar dict parameters.

**Mutation testing in CI.** Mutation testing tools (e.g., `mutmut`, `cosmic-ray`) automatically swap identifiers and verify that the swap is caught by at least one test. Running mutation testing on scoring and computation modules surfaces weak test suites before bugs reach production.

---

## 7. Anti-patterns to Avoid

**Do not use a `context` dict to bundle multiple writable sub-dicts.** Read-only context (configuration, feature flags) is appropriate in a context dict. Writable state dicts must be named parameters.

**Do not rely on range-validation to detect wrong-dict writes.** A value written to the wrong dict is typically valid by range — it is the correct type and within bounds. Range checks will not detect it.

**Do not initialize both dicts with the same default value.** If both dicts start at `0.0` and the bug writes the computed value to the wrong dict, the correct dict retains `0.0`, which happens to be the default. Readers see a plausible value and do not raise an alarm.

**Do not skip the negative assertion in tests.** The positive assertion (`assert "u2" in treatment_scores`) is necessary but not sufficient. The negative assertion (`assert "u2" not in control_scores`) is what catches this specific bug.

**Do not treat statistically null experiment results as definitive.** A null effect can be a true null effect or a data routing bug. When multiple consecutive experiments all show null effects, audit the data pipeline before concluding that the feature has no impact.

---

## 8. Edge Cases and Variants

**Multiple levels of nesting.** In deeply nested state structures, the wrong-dict write may occur several levels down: `state["experiment"]["cohorts"]["treatment"]["scores"][user_id]` vs `state["experiment"]["cohorts"]["control"]["scores"][user_id]`. The same diagnostic fingerprint applies: one leaf dict is over-populated, the other is stale.

**Write occurs in a helper function.** If the write is in a helper that accepts a single dict parameter, the routing decision is made at the call site. The bug becomes: `_write_score(control_scores, user_id, score)` when `_write_score(treatment_scores, ...)` was intended. The fix is identical: use typed stores so the call site cannot accidentally pass the wrong object.

**Partial cycles.** If the bug affects only a subset of users (e.g., only users processed after a certain point in the list), the wrong-dict write may be intermittent. Cycle-ID stamping is the most reliable detection mechanism in this case.

**Concurrent writes.** In multi-threaded or multi-process scoring, the wrong-dict write introduces a race condition: two threads may write to `control_scores` concurrently (one correct, one misrouted), causing unpredictable overwrites. The typed store should use a threading lock around `write()` when used in concurrent contexts.

**The variant where the value is also read from the wrong dict.** If both the writer and a reader target the same wrong dict, the system may appear self-consistent while producing incorrect business outputs. This is detected only by comparing outputs against an independent ground truth, not by internal consistency checks.

---

## 9. Audit Checklist

- [ ] No function accepts two or more structurally similar `dict` parameters under a bundled `context` or `state` parent dict where any of them are written to inside the function.
- [ ] Every computation function that writes to one of N similar dicts has at least one test that asserts the *other* N-1 dicts were not modified.
- [ ] Shared, long-lived state dicts are wrapped in typed classes with named `write()` and `read()` methods rather than accessed as raw dicts.
- [ ] Cycle IDs or version counters are stored alongside values in any dict that is written once per cycle and read in a separate step.
- [ ] Mutation testing (e.g., `mutmut`) is run on all scoring and state-routing modules and reports zero surviving mutants on write-target lines.
- [ ] Any experiment that reports null effects for two or more consecutive cycles triggers an automatic data pipeline audit.
- [ ] Code review checklist includes: "for each dict write, confirm the target dict corresponds to the branch condition."
- [ ] No `context["key1"]` and `context["key2"]` pattern exists in functions where both keys represent writable, distinct output targets.
- [ ] Integration tests include a cross-dict contamination check: `assert set(control_scores.keys()).isdisjoint(set(treatment_scores.keys()))` when users are exclusive to one group.
- [ ] Read consumers verify that the value they receive has a cycle ID matching the current cycle before using it in downstream computation.

---

## 10. Further Reading

- Repository with annotated code examples for all patterns in this series: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns)
- Python `dataclasses` module for typed state containers: [docs.python.org/3/library/dataclasses.html](https://docs.python.org/3/library/dataclasses.html)
- `mutmut` mutation testing tool: [mutmut.readthedocs.io](https://mutmut.readthedocs.io)
- "Testing Without Mocks" — James Shore, on testing state routing explicitly: [jamesshore.com/v2/blog/2018/testing-without-mocks](https://www.jamesshore.com/v2/blog/2018/testing-without-mocks)
- Hypothesis property-based testing for invariant checking across dict operations: [hypothesis.readthedocs.io](https://hypothesis.readthedocs.io)
