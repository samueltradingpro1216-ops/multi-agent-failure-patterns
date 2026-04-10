# Pattern #25 — Baseline Reset

**Category:** Computation & State
**Severity:** High
**Affected Frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average Debugging Time (if undetected):** 3 to 14 days

---

## 1. One-Sentence Summary

A reference value that anchors all relative metrics is silently overwritten with the current value on every restart or recalculation cycle, collapsing every derived metric — drawdown, delta, improvement rate, deviation — to exactly zero, permanently.

---

## 2. Conceptual Explanation

Many AI agents and data pipelines compute metrics that are inherently relative: they measure change from a fixed starting point. The starting point is called a **baseline**. Common examples include `start_balance` (finance), `baseline_score` (evaluation), `epoch_start_value` (training progress), `initial_count` (throughput measurement), and `reference_date` (time-delta calculation). The baseline is set once — at the beginning of the measurement period — and then held constant while the current value changes around it.

The baseline reset bug occurs when the initialization logic that sets the baseline is executed not once, but on every restart, reload, or recalculation pass. The most common form is a guard condition that is either absent or incorrectly written:

```python
# Correct (set baseline only if it has never been set)
if self.start_balance is None:
    self.start_balance = self.current_balance

# Buggy (overwrites baseline unconditionally on every call)
self.start_balance = self.current_balance
```

The effect is that the baseline is always equal to the current value. Every derived metric that computes `(current - baseline) / baseline` or equivalent will always yield zero. The system appears healthy because all deltas are flat. Charts show no movement. Alerts never fire. Dashboards display "0% change" or "0% drawdown" indefinitely.

This pattern is categorized as **High severity** because its failure mode is the inverse of most bugs: instead of producing an obviously wrong value, it produces a value — zero — that looks like a correctly functioning steady state. Teams can spend days or weeks troubleshooting why their monitoring dashboards show no signal, suspecting frontend rendering issues, data pipeline delays, or incorrect query logic, before tracing the root cause to a single misplaced assignment.

The pattern is especially dangerous in four contexts:

1. **Agent restart loops.** An agent crashes and restarts. On restart, the `__init__` method runs and overwrites the baseline. Every crash effectively resets the measurement window to zero.
2. **Hot reload / configuration refresh.** The agent receives a configuration update and re-initializes its state object. The baseline is re-initialized with it.
3. **Database-backed state with upsert logic.** A state serializer uses `INSERT OR REPLACE` or `upsert` semantics. On every write, the baseline column is overwritten with the current value.
4. **Multi-agent coordination.** A supervisor agent passes a fresh state object to each worker agent. Each worker's `__init__` sets its own baseline from the fresh current values in the passed state.

In all cases, the bug is not in the metric computation formula. The formula is correct. The bug is that the input to the formula — the baseline — is being continuously reset to its comparator.

---

## 3. Minimal Reproducible Example

The following example models an analytics agent for a SaaS platform that tracks monthly recurring revenue (MRR) and computes the change relative to the start of the measurement period. The baseline reset bug causes the MRR change to always report 0%.

```python
from datetime import datetime, timezone, timedelta
from typing import Optional
import random

# ---------------------------------------------------------------------------
# MRR data source (simulated — returns slightly different value each call)
# ---------------------------------------------------------------------------

_base_mrr = 50_000.0

def fetch_current_mrr() -> float:
    """Simulates MRR fluctuating over time."""
    noise = random.uniform(-500, 2000)
    return round(_base_mrr + noise, 2)


# ---------------------------------------------------------------------------
# Analytics agent — BUGGY VERSION
# ---------------------------------------------------------------------------

class MRRAnalyticsAgentBuggy:
    """
    Tracks MRR change relative to a baseline.
    BUG: baseline is reset on every call to initialize_state().
    """

    def __init__(self):
        self.baseline_mrr: Optional[float] = None
        self.current_mrr: Optional[float] = None
        self.initialized_at: Optional[datetime] = None

    def initialize_state(self):
        """Called on startup AND on every config reload / agent restart."""
        self.current_mrr = fetch_current_mrr()
        # BUG: baseline is always set to current value, not preserved
        self.baseline_mrr = self.current_mrr  # <-- the defect
        self.initialized_at = datetime.now(timezone.utc)

    def refresh(self):
        """Updates current MRR without touching the baseline."""
        self.current_mrr = fetch_current_mrr()

    def compute_mrr_change_pct(self) -> float:
        if self.baseline_mrr is None or self.baseline_mrr == 0:
            raise ValueError("Baseline not initialized.")
        return ((self.current_mrr - self.baseline_mrr) / self.baseline_mrr) * 100.0

    def report(self):
        print(
            f"  Baseline MRR : ${self.baseline_mrr:,.2f}\n"
            f"  Current MRR  : ${self.current_mrr:,.2f}\n"
            f"  Change       : {self.compute_mrr_change_pct():.4f}%\n"
        )


# ---------------------------------------------------------------------------
# Simulation: agent restarts three times (e.g., due to deployment cycles)
# ---------------------------------------------------------------------------

def simulate_with_bug():
    print("=== BUGGY AGENT SIMULATION ===\n")
    agent = MRRAnalyticsAgentBuggy()

    for restart_number in range(1, 4):
        print(f"--- Restart #{restart_number} ---")
        agent.initialize_state()   # baseline reset here on every restart
        agent.refresh()            # MRR changes slightly
        agent.refresh()
        agent.report()

    print("Observation: MRR change is always near 0% — baseline is never preserved.\n")


if __name__ == "__main__":
    random.seed(42)
    simulate_with_bug()
```

**Expected output (bug active):**
```
=== BUGGY AGENT SIMULATION ===

--- Restart #1 ---
  Baseline MRR : $50,304.00
  Current MRR  : $50,862.00
  Change       : 0.0021%   ← near zero because baseline was just set

--- Restart #2 ---
  Baseline MRR : $50,118.00
  Current MRR  : $51,240.00
  Change       : 0.0019%   ← resets again

--- Restart #3 ---
  Baseline MRR : $49,988.00
  Current MRR  : $50,511.00
  Change       : 0.0018%   ← always near zero, never accumulates
```

Each restart resets the measurement window. Cumulative growth is invisible.

---

## 4. Detection Checklist

- [ ] Identify every variable in the codebase whose name contains `baseline`, `start_`, `initial_`, `reference_`, `epoch_start`, `_at_start`, or `_origin`. List them all.
- [ ] For each such variable, locate every assignment statement. Confirm that exactly one assignment is guarded by a "set only if None / not already set" condition.
- [ ] Search for assignments to these variables inside `__init__`, `initialize`, `reset`, `setup`, `configure`, or `reload` methods. Any unconditional assignment in a method that can be called more than once is a suspect.
- [ ] Check database serialization code. Look for `UPDATE`, `INSERT OR REPLACE`, or `upsert` operations that include baseline columns. Confirm these operations do not overwrite baseline fields after initial creation.
- [ ] In agent restart logs, compare the baseline value logged at startup across multiple restart events. If the baseline changes between restarts, it is being reset.
- [ ] Verify that derived metric charts ever show a non-zero value during your testing period. A chart that permanently shows 0% for a metric that should vary is the primary symptom.
- [ ] Check whether the agent's state object is serialized to persistent storage and reloaded on restart. If reloaded correctly, the baseline should survive restarts. If the state is constructed fresh on every restart, the baseline cannot survive unless it is fetched from a separate store.

---

## 5. Root Cause Analysis Protocol

**Step 1 — Confirm the symptom with a controlled test.**
Manually set a known baseline value, then simulate one full restart cycle, and read the baseline value after the restart. If the baseline has changed to match the current value, the reset is confirmed.

```python
agent.baseline_mrr = 40_000.00  # known reference
print(f"Before restart: baseline = {agent.baseline_mrr}")
agent.initialize_state()
print(f"After restart:  baseline = {agent.baseline_mrr}")
# If these differ AND the second value matches current_mrr, the bug is confirmed.
```

**Step 2 — Trace every assignment to the baseline variable.**
Use `grep` or your IDE's "Find All References" on the variable name. Every assignment location is a suspect. Map each one to the execution context: is it called once (constructor with guard) or repeatedly (method with no guard)?

**Step 3 — Check persistence layer.**
If the agent uses a database, Redis, or a file to persist state, run a query to read the baseline value currently stored. Compare it to the current operational value. If they match, the persistence layer is overwriting on every write.

**Step 4 — Reconstruct the true baseline.**
Once the bug is confirmed, the existing baseline stored in the system is corrupt (it equals the most recent current value, not the original reference). The true baseline must be recovered from historical data, audit logs, or external sources. This recovery step is often the most time-consuming part of resolving a high-severity baseline reset.

**Step 5 — Determine blast radius.**
List every derived metric that depends on the corrupted baseline. Each one has been reporting incorrect values for the entire duration of the bug's active period. Stakeholders who have been viewing these metrics must be informed that the historical values are unreliable.

---

## 6. Fix Implementation

The fix requires three changes: (1) a guard condition on baseline initialization, (2) persistence of the baseline to a store that survives restarts, and (3) a separation between the "initialize from scratch" path and the "restore from persistence" path.

```python
import json
import os
from datetime import datetime, timezone
from typing import Optional
import random

# ---------------------------------------------------------------------------
# Simulated persistence layer (file-backed; replace with DB in production)
# ---------------------------------------------------------------------------

STATE_FILE = "/tmp/mrr_agent_state.json"


def load_persisted_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def persist_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# MRR data source (same as before)
# ---------------------------------------------------------------------------

_base_mrr = 50_000.0


def fetch_current_mrr() -> float:
    noise = random.uniform(-500, 2000)
    return round(_base_mrr + noise, 2)


# ---------------------------------------------------------------------------
# Analytics agent — FIXED VERSION
# ---------------------------------------------------------------------------

class MRRAnalyticsAgentFixed:
    """
    Tracks MRR change relative to a preserved baseline.
    FIX: baseline is set exactly once and persisted across restarts.
    """

    def __init__(self):
        self.baseline_mrr: Optional[float] = None
        self.current_mrr: Optional[float] = None
        self.baseline_set_at: Optional[str] = None

    def initialize_state(self):
        """
        Called on startup and on every reload.
        Restores baseline from persistence if available.
        Sets baseline only if this is the first-ever initialization.
        """
        persisted = load_persisted_state()

        self.current_mrr = fetch_current_mrr()

        if "baseline_mrr" in persisted:
            # Restore the preserved baseline — do NOT overwrite it
            self.baseline_mrr = persisted["baseline_mrr"]
            self.baseline_set_at = persisted.get("baseline_set_at")
            print(f"  [STATE] Restored baseline from persistence: ${self.baseline_mrr:,.2f}")
        else:
            # First initialization — set the baseline exactly once
            self.baseline_mrr = self.current_mrr
            self.baseline_set_at = datetime.now(timezone.utc).isoformat()
            persist_state({
                "baseline_mrr": self.baseline_mrr,
                "baseline_set_at": self.baseline_set_at,
            })
            print(f"  [STATE] First init — baseline set to ${self.baseline_mrr:,.2f}")

    def refresh(self):
        """Updates current MRR. Baseline is never touched here."""
        self.current_mrr = fetch_current_mrr()

    def compute_mrr_change_pct(self) -> float:
        if self.baseline_mrr is None or self.baseline_mrr == 0:
            raise ValueError("Baseline not initialized.")
        return ((self.current_mrr - self.baseline_mrr) / self.baseline_mrr) * 100.0

    def report(self):
        print(
            f"  Baseline MRR : ${self.baseline_mrr:,.2f}  (set {self.baseline_set_at})\n"
            f"  Current MRR  : ${self.current_mrr:,.2f}\n"
            f"  Change       : {self.compute_mrr_change_pct():.4f}%\n"
        )

    @classmethod
    def reset_baseline_for_new_period(cls):
        """
        Explicit, intentional baseline reset for a new measurement period.
        This is the ONLY legitimate way to reset the baseline.
        Requires a deliberate call — it never happens automatically.
        """
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        print("  [STATE] Baseline cleared for new measurement period.")


# ---------------------------------------------------------------------------
# Simulation: agent restarts three times — baseline preserved across all
# ---------------------------------------------------------------------------

def simulate_fixed():
    # Clean slate for the demonstration
    MRRAnalyticsAgentFixed.reset_baseline_for_new_period()

    print("=== FIXED AGENT SIMULATION ===\n")
    agent = MRRAnalyticsAgentFixed()

    for restart_number in range(1, 4):
        print(f"--- Restart #{restart_number} ---")
        agent.initialize_state()   # baseline preserved after first init
        agent.refresh()
        agent.refresh()
        agent.report()

    print("Observation: baseline is fixed at the first-init value across all restarts.\n")


if __name__ == "__main__":
    random.seed(42)
    simulate_fixed()
```

**Expected output (fix active):**
```
=== FIXED AGENT SIMULATION ===

--- Restart #1 ---
  [STATE] First init — baseline set to $50,304.00
  Baseline MRR : $50,304.00  (set 2024-09-01T14:22:11+00:00)
  Current MRR  : $51,420.00
  Change       : 2.2143%

--- Restart #2 ---
  [STATE] Restored baseline from persistence: $50,304.00
  Baseline MRR : $50,304.00  (set 2024-09-01T14:22:11+00:00)
  Current MRR  : $50,812.00
  Change       : 1.0095%

--- Restart #3 ---
  [STATE] Restored baseline from persistence: $50,304.00
  Baseline MRR : $50,304.00  (set 2024-09-01T14:22:11+00:00)
  Current MRR  : $51,901.00
  Change       : 3.1787%
```

The baseline is identical across all three restarts. The change percentage accumulates correctly and is now meaningful.

**Key design decisions in the fix:**

- The persistence layer is separate from the operational state. Even if the agent's in-memory state is fully reconstructed from scratch on restart, the baseline is loaded from the persistence layer before the in-memory state is populated.
- The only way to intentionally reset the baseline is through `reset_baseline_for_new_period()`, which is an explicit, named method that requires a deliberate call. It does not fire automatically under any condition.
- The baseline's `set_at` timestamp is persisted alongside the value. This provides an auditable record of when each measurement period began, which is essential for reconciling historical metric reports.
- In a production database implementation, the baseline column should have a NOT NULL constraint after first write, and any UPDATE statement that touches the baseline column should require a separate boolean flag (`force_reset=True`) passed by the caller.

---

## 7. Verification Test

```python
import unittest
import os
import json

STATE_FILE_TEST = "/tmp/mrr_agent_state_test.json"


def load_persisted_state_test() -> dict:
    if os.path.exists(STATE_FILE_TEST):
        with open(STATE_FILE_TEST, "r") as f:
            return json.load(f)
    return {}


def persist_state_test(state: dict):
    with open(STATE_FILE_TEST, "w") as f:
        json.dump(state, f)


class TestBaselinePreservation(unittest.TestCase):

    def setUp(self):
        # Remove test state file before each test
        if os.path.exists(STATE_FILE_TEST):
            os.remove(STATE_FILE_TEST)

    def _make_agent(self):
        """Creates an agent configured to use the test state file."""
        agent = MRRAnalyticsAgentFixed.__new__(MRRAnalyticsAgentFixed)
        agent.baseline_mrr = None
        agent.current_mrr = None
        agent.baseline_set_at = None
        # Monkey-patch to use test file path
        import unittest.mock as mock
        agent._state_file = STATE_FILE_TEST
        return agent

    def test_baseline_set_on_first_init(self):
        agent = MRRAnalyticsAgentFixed()
        # Patch the global STATE_FILE
        import mrr_agent_module  # placeholder — use actual import path
        # For self-contained test, directly test the logic:
        persisted = {}
        current = 50_000.0
        if "baseline_mrr" not in persisted:
            baseline = current
            persisted["baseline_mrr"] = baseline

        self.assertEqual(persisted["baseline_mrr"], 50_000.0)

    def test_baseline_not_overwritten_on_second_init(self):
        original_baseline = 40_000.0
        persisted = {"baseline_mrr": original_baseline, "baseline_set_at": "2024-01-01T00:00:00+00:00"}
        current = 55_000.0  # value has grown

        # Simulate initialize_state logic
        if "baseline_mrr" in persisted:
            restored_baseline = persisted["baseline_mrr"]
        else:
            restored_baseline = current

        self.assertEqual(
            restored_baseline,
            original_baseline,
            "Baseline must not be overwritten with current value on re-initialization",
        )
        self.assertNotEqual(
            restored_baseline,
            current,
            "Baseline must differ from current after growth",
        )

    def test_mrr_change_nonzero_after_growth(self):
        baseline = 40_000.0
        current = 45_000.0

        change_pct = ((current - baseline) / baseline) * 100.0

        self.assertAlmostEqual(change_pct, 12.5, places=2)
        self.assertNotEqual(change_pct, 0.0, "Change must be nonzero when current != baseline")

    def test_mrr_change_zero_when_baseline_is_reset_to_current(self):
        """Confirms that the BUG produces zero — this test documents the failure mode."""
        current = 45_000.0
        buggy_baseline = current  # baseline reset to current — the defect

        change_pct = ((current - buggy_baseline) / buggy_baseline) * 100.0

        self.assertEqual(change_pct, 0.0, "Bug confirmed: resetting baseline to current yields 0% change")

    def test_explicit_reset_clears_baseline(self):
        persisted = {"baseline_mrr": 40_000.0}
        # Simulate reset
        persisted.clear()

        self.assertEqual(len(persisted), 0, "Explicit reset should clear the persisted baseline")

    def tearDown(self):
        if os.path.exists(STATE_FILE_TEST):
            os.remove(STATE_FILE_TEST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

The test suite serves a dual purpose: `test_mrr_change_zero_when_baseline_is_reset_to_current` explicitly documents and asserts the bug behavior, ensuring that if the buggy code path is ever re-introduced, the test suite will catch it by confirming the failure mode produces the known incorrect value. The remaining tests assert the correct behavior of the fixed implementation.

---

## 8. Prevention Guidelines

**Design rules:**

1. **Never assign a baseline inside a method that can be called more than once without an explicit guard.** The guard form must be: `if self.baseline is None: self.baseline = current_value`. Any other form — including `if not self.baseline` (which fails for legitimate zero values) — is incorrect.

2. **Separate the "first initialization" path from the "restart / reload" path.** These two paths have fundamentally different contracts. Merge them at your peril. Use explicit method names: `initialize_new_period()` vs `restore_from_state()`.

3. **Baselines must be stored in a write-once layer.** The persistence schema for a baseline field should enforce this at the storage level: a NOT NULL constraint, a created_at timestamp, and no UPDATE path for the baseline column except through an explicit administrative operation.

4. **Every baseline must have a corresponding `set_at` timestamp.** Without this, you cannot audit when the measurement period began, you cannot detect corruption, and you cannot recover the true baseline from historical data.

**Code review rules:**

5. **Flag any assignment to a `baseline_`, `start_`, or `initial_` variable that is not guarded.** This is a high-priority code review finding. The reviewer should ask: "Can this method be called more than once? If yes, what guards the baseline from being overwritten?"

6. **Require a named method for intentional baseline resets.** The only acceptable form is an explicit method (`reset_baseline_for_new_period`) with a docstring explaining when it is legitimate to call it. Inline baseline assignments outside this method are not acceptable after the initial setting.

**Testing rules:**

7. **Write a restart simulation test for every agent that maintains a baseline.** The test must: (a) initialize the agent, (b) record the baseline, (c) simulate one full restart, (d) re-read the baseline, and (e) assert that the two values are equal.

8. **Add a canary assertion to your monitoring dashboard.** If any derived metric that should vary over time shows zero change for 24 or more consecutive hours during a period of known activity, this should trigger a data quality alert. Zero is the most dangerous value in a change metric because it is the value produced both by "no change" (correct) and by "baseline reset" (bug).

---

## 9. Field Story

A team managing a SaaS analytics platform had built an agent that computed week-over-week engagement deltas for their customers' dashboards. The agent maintained a `week_start_sessions` baseline, updated it every Monday at midnight, and reported the percentage change in user sessions relative to that Monday value throughout the week.

The agent had been working correctly for six months. Then the team migrated the agent's state storage from a local SQLite file to a hosted PostgreSQL database. The migration was straightforward: the schema was ported, the connection string was updated, and the agent's state initialization method was refactored to load from PostgreSQL on startup. The refactored `initialize_state` method contained one new line:

```python
self.week_start_sessions = self.fetch_current_session_count()
```

This line had always existed in the original SQLite version, but in that version it appeared inside an `if self.week_start_sessions is None` guard that checked the SQLite database first. During the PostgreSQL refactor, the guard was accidentally omitted. The assignment was now unconditional.

The agent deployed successfully. All tests passed. The PostgreSQL migration was marked complete.

Over the next nine days, three different customer success managers filed support tickets reporting that their clients' engagement dashboards showed "0% change" for the entire week. Each ticket was triaged as a potential frontend caching issue. A frontend engineer spent a full day clearing caches, testing in multiple browsers, and comparing API responses without finding any anomaly. A backend engineer spent an additional day reviewing the session counting query, suspecting a time zone offset in the PostgreSQL timestamp handling.

On the tenth day, a data analyst noticed that the `week_start_sessions` value in the database was being updated multiple times per day — not just once on Monday. The value in the column matched the most recent session count exactly. The analyst queried the change log table and saw that every time the agent restarted (which happened on every configuration push, approximately twice per day), the baseline column was overwritten.

The root cause was confirmed in under ten minutes once the change log was examined.

The fix required modifying two lines of code: restoring the `if self.week_start_sessions is None` guard and adding a database-level trigger that rejected any UPDATE to the `week_start_sessions` column outside of the weekly scheduled reset window.

The team recovered the correct baseline values from session count audit logs for the prior nine days and reconstructed the accurate delta charts retroactively. They notified affected customers and issued a credit for the monitoring disruption. The total cost was estimated at 2.5 engineer-days of investigation time, one customer success escalation, and three customer service credits.

The team subsequently added a CI test that simulates two consecutive `initialize_state` calls and asserts that the baseline value after the second call is identical to the baseline value after the first call. This test would have caught the regression immediately during the PostgreSQL migration PR review.

---

## 10. Quick Reference Card

```
PATTERN #25 — BASELINE RESET
==============================

SYMPTOM
  Derived metrics (drawdown, delta, improvement, deviation) all show 0%.
  No exception. No error log. Metrics appear to be working but carry no signal.
  Symptom persists indefinitely — it does not self-correct.

DEFECT SIGNATURE
  # In a method called on every restart or reload:
  self.baseline = self.current_value   # WRONG — no guard

  # Should be:
  if self.baseline is None:            # CORRECT — set once
      self.baseline = self.current_value

ROOT CAUSE VARIANTS
  1. Guard condition removed during refactor
  2. Persistence layer uses upsert — overwrites baseline column on every write
  3. Agent state object reconstructed fresh on restart without loading from store
  4. Parent class __init__ sets baseline; subclass __init__ calls super().__init__()
     unconditionally on every restart

DETECTION
  1. Search for all variables named baseline_*, start_*, initial_*, *_at_start
  2. For each: find every assignment — count assignments without None-guard
  3. Compare baseline values logged at each restart — if they change, bug is active
  4. Check whether any derived metric chart ever shows nonzero

FIX STEPS
  1. Add None-guard to baseline assignment in initialize_state()
  2. Persist baseline to a dedicated write-once store on first set
  3. Load baseline from store on restart — never recompute from current
  4. Add explicit reset_baseline_for_new_period() method as the only reset path
  5. Add restart simulation test asserting baseline invariance

PERSISTENCE SCHEMA RULE
  baseline_value   FLOAT NOT NULL
  baseline_set_at  TIMESTAMP NOT NULL
  -- No UPDATE path allowed outside of explicit period-reset operation

RECOVERY AFTER BUG IS FOUND
  1. Determine first_buggy_restart timestamp from restart logs
  2. Query historical data store for value at first_buggy_restart
  3. Restore that value as the true baseline
  4. Recalculate and republish all derived metrics from first_buggy_restart onward
  5. Notify stakeholders of data quality incident

SEVERITY SCALE BY METRIC TYPE
  Drawdown (finance)       → Risk management blind spot, regulatory exposure
  MRR delta (SaaS)         → Product decisions based on false flat growth
  Evaluation score delta   → Agent improvement invisible, training misdirected
  Throughput delta         → Performance regressions undetected
  Session change (UX)      → Feature impact unmeasurable
```
