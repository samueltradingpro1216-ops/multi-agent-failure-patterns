# Pattern #10 — Survival Mode Deadlock

**Category:** Filtering & Decisions
**Severity:** Critical
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 1 to 7 days (the system is alive but idle — the root cause is not investigated as long as no one notices the absence of actions)

---

## 1. Observable Symptoms

The system is **alive but does nothing**. Logs show that evaluations are taking place, scores are being computed, decisions are being made — but the decision is always the same: **rejected**. 100% of proposals are vetoed. No action has been executed for hours, days, or even weeks.

System monitoring is green: normal CPU, normal memory, no crashes, no errors. The system is running, evaluating, deciding — it just always decides to do nothing. This is a deadlock that does not look like a deadlock because there are no blocked threads and no locked resources. It is a **logical deadlock**: the system is trapped in a state from which it cannot progress.

The most insidious symptom: the vicious cycle. Without executing actions, the system generates no new data. Without new data, metrics do not improve. Without improving metrics, the score remains below the threshold. The system is caught in a negative feedback loop it cannot escape on its own.

## 2. Field Story (anonymized)

An automated decision system had a confidence mechanism: each proposal received a score from 0 to 100, and only proposals with a score >= 50 were executed. The threshold of 50 had been chosen arbitrarily ("half sounds reasonable").

After a change in external conditions (a market parameter had shifted), the average score of proposals dropped from 55 to 42. Suddenly, 100% of proposals were below the threshold. The system entered "survival mode" — a state designed for difficult periods where it is safer to do nothing.

The problem: survival mode was permanent. Without executing actions, performance metrics stagnated (no new data). The scoring module, partially based on recent performance, kept producing low scores. The threshold, being static, did not move. The system remained stuck for 4 days before the team noticed that no action had been executed.

## 3. Technical Root Cause

The bug occurs when a **static** quality threshold is higher than the average score the system can produce under current conditions:

```
Normal conditions:
    Mean score:  55/100
    Threshold:   50/100
    Acceptance rate: ~70%     ← System functioning

Degraded conditions:
    Mean score:  42/100
    Threshold:   50/100
    Acceptance rate: ~2%      ← Near-deadlock

Adverse conditions:
    Mean score:  35/100
    Threshold:   50/100
    Acceptance rate: 0%       ← TOTAL DEADLOCK
```

The deadlock mechanism is a **4-step vicious cycle**:

```
1. Adverse conditions → mean score drops
2. Score < threshold → 100% of actions rejected
3. 0 actions executed → 0 new data
4. 0 new data → metrics stagnate → score stays low
→ Back to step 2: PERMANENT DEADLOCK
```

The fundamental problem is that the threshold is **decoupled from real-world conditions**. A threshold of 50 makes sense when scores normally range between 40 and 80. It no longer makes sense when scores range between 25 and 45 — a shift that can occur due to factors entirely external to the system.

## 4. Detection

### 4.1 Manual code audit

Search for static thresholds in decision mechanisms:

```bash
# Search for score/confidence comparisons against constants
grep -rn "confidence.*>=\|score.*>=\|threshold\|>= 50\|>= 0\.5" --include="*.py"

# Search for veto/rejection mechanisms
grep -rn "VETO\|REJECT\|BLOCK\|DENIED\|rejected" --include="*.py"

# Search for rejection counters (if the code tracks them)
grep -rn "reject.*count\|veto.*count\|blocked.*count" --include="*.py"
```

For each static threshold found, verify: does a fallback mechanism exist if the acceptance rate drops to 0%?

### 4.2 Automated CI/CD

Test that the system cannot remain stuck indefinitely:

```python
def test_no_permanent_deadlock():
    """Simulate worst-case scores and verify the system eventually acts."""
    scorer = MockScorer(mean=35, std=5)  # Scores always below 50
    gate = ConfidenceGate(threshold=50)

    # Simulate 200 cycles
    actions_taken = 0
    for _ in range(200):
        score = scorer.generate()
        if gate.evaluate(score):
            actions_taken += 1

    assert actions_taken > 0, (
        "DEADLOCK: 0 actions in 200 cycles with scores mean=35. "
        "The gate has no fallback mechanism."
    )
```

### 4.3 Runtime production

Monitor the acceptance rate in real time and alert if 0% for more than N cycles:

```python
import time
from collections import deque

class DeadlockDetector:
    """Detects when acceptance rate drops to 0% for too long."""

    def __init__(self, window_size: int = 100, alert_after_zero_pct: int = 50):
        self.window = deque(maxlen=window_size)
        self.alert_threshold = alert_after_zero_pct

    def record(self, accepted: bool):
        self.window.append(accepted)

    def check(self) -> dict:
        if len(self.window) < self.alert_threshold:
            return {"deadlock": False, "reason": "not enough data"}

        recent = list(self.window)[-self.alert_threshold:]
        acceptance_rate = sum(recent) / len(recent)

        if acceptance_rate == 0:
            return {
                "deadlock": True,
                "zero_streak": self.alert_threshold,
                "message": f"DEADLOCK: 0% acceptance over {self.alert_threshold} cycles",
            }

        return {"deadlock": False, "acceptance_rate": acceptance_rate}
```

## 5. Fix

### 5.1 Immediate fix

Add a guaranteed minimum threshold: if the acceptance rate is 0% for N cycles, temporarily lower the threshold:

```python
class EmergencyThresholdOverride:
    """Temporarily lowers threshold when deadlocked."""

    def __init__(self, normal_threshold: float, emergency_threshold: float, trigger_after: int = 50):
        self.normal = normal_threshold
        self.emergency = emergency_threshold
        self.trigger = trigger_after
        self.consecutive_rejects = 0

    def evaluate(self, score: float) -> bool:
        threshold = self.normal

        if self.consecutive_rejects >= self.trigger:
            threshold = self.emergency  # Emergency mode

        if score >= threshold:
            self.consecutive_rejects = 0
            return True
        else:
            self.consecutive_rejects += 1
            return False
```

### 5.2 Robust fix

Implement an adaptive threshold with an exploration window:

```python
class AdaptiveGate:
    """
    Three mechanisms to prevent deadlock:
    1. Adaptive threshold that decays when acceptance is 0%
    2. Exploration window: accept 1 in N regardless of score
    3. Percentile-based threshold instead of absolute value
    """

    def __init__(
        self,
        initial_threshold: float = 50.0,
        min_threshold: float = 20.0,
        decay_per_reject: float = 0.5,
        explore_every: int = 20,
    ):
        self.threshold = initial_threshold
        self.initial = initial_threshold
        self.minimum = min_threshold
        self.decay = decay_per_reject
        self.explore_every = explore_every
        self.total_evals = 0
        self.consecutive_rejects = 0
        self.scores_history = []

    def evaluate(self, score: float) -> dict:
        self.total_evals += 1
        self.scores_history.append(score)

        # Mechanism 1: adaptive decay
        if self.consecutive_rejects > 0 and self.consecutive_rejects % 10 == 0:
            self.threshold = max(self.minimum, self.threshold - self.decay)

        # Mechanism 2: exploration window
        if self.total_evals % self.explore_every == 0 and self.consecutive_rejects > 0:
            self.consecutive_rejects = 0
            return {"accepted": True, "reason": "exploration", "threshold": self.threshold}

        # Normal evaluation
        if score >= self.threshold:
            self.consecutive_rejects = 0
            # Recovery: slowly raise threshold back to initial
            self.threshold = min(self.initial, self.threshold + self.decay * 0.1)
            return {"accepted": True, "reason": "above_threshold", "threshold": self.threshold}
        else:
            self.consecutive_rejects += 1
            return {"accepted": False, "reason": "below_threshold", "threshold": self.threshold}
```

## 6. Architectural Prevention

The fundamental principle: **a system must never be able to halt indefinitely due to a quality threshold**. Even in the worst case, it must execute a minimum number of actions to generate data and be able to self-correct.

**1. Adaptive threshold, never static.** The threshold must adjust to real-world conditions. If mean scores drop, the threshold drops as well (with a floor). If scores recover, the threshold rises.

**2. Mandatory exploration window.** Even in ultra-conservative mode, the system accepts at least 1 proposal out of every N (e.g., 1 in 20). This "exploration" guarantees that new data is generated, breaking the vicious cycle.

**3. Percentile-based threshold instead of absolute value.** Accept the "top 10% of scores" rather than "score >= 50". A percentile guarantees a minimum acceptance rate regardless of the absolute level of scores.

**4. Acceptance rate monitored as a critical KPI.** The acceptance rate must be monitored with the same priority as latency or error rate. A rate of 0% for more than 1 hour is a P1 alert.

## 7. Anti-patterns to Avoid

1. **Static threshold with no fallback mechanism.** `if score >= 50` with no alternative when the mean score is 42 = guaranteed deadlock.

2. **Permanent "survival mode".** A conservative mode with no automatic exit condition. If survival mode requires human intervention to exit, it is permanent outside business hours.

3. **Score based solely on recent performance.** If the score depends on the results of the last N actions and there are no actions, the score is based on increasingly stale data — it will never improve.

4. **No separation between scoring and gating.** The module that computes the score should not be the same one that decides whether to accept or reject. The gate can have exploration rules that the scorer knows nothing about.

5. **Testing the gate only with high scores.** If tests never simulate a mean score below the threshold, the deadlock is never detected before production.

## 8. Edge Cases and Variants

**Variant 1: Partial deadlock.** The system accepts actions of type A (high score) but blocks 100% of actions of type B (low score). Global monitoring shows an acceptance rate above 0%, but type B is completely dead.

**Variant 2: Oscillating deadlock.** The adaptive threshold decays, a few actions pass through, metrics improve marginally, the threshold rises again, everything is re-blocked. The system oscillates between "blocked" and "near-blocked" without ever stabilizing.

**Variant 3: Deadlock by veto accumulation.** The system has 5 independent gates (confidence, regime, pattern, timing, adversarial). Each has an acceptance rate of 80%. The combined rate is `0.8^5 = 32%`. If one gate drops to 60%, the combined rate drops to `0.6 * 0.8^4 = 24%`. Adding a 6th gate at 90% brings the combined rate down to 22%. The accumulation of "reasonable" gates creates an ultra-restrictive filter.

**Variant 4: Seasonal deadlock.** The score depends on external factors (market conditions, user load, API availability). During certain periods (weekends, holidays, maintenance windows), scores are systematically low. The deadlock appears on Friday evening and resolves on Monday morning — without anyone noticing.

## 9. Audit Checklist

- [ ] No decision threshold is purely static (each one has an adaptive mechanism or a fallback)
- [ ] The acceptance rate is monitored and alerts are fired if 0% for more than 1 hour
- [ ] An exploration window guarantees at least 1 action per N cycles
- [ ] The worst case is tested: what happens if the mean score is 50% below the threshold?
- [ ] The deadlock detector is active in production and sends alerts

## 10. Further Reading

- Corresponding short pattern: [Pattern 10 — Survival Mode Deadlock](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-10)
- Related patterns: #03 (Penalty Cascade — a cascade that lowers the score can trigger the deadlock), #09 (Agent Infinite Loop — an agent that loops searching for an impossible score)
- Recommended reading:
  - "Reinforcement Learning: An Introduction" (Sutton & Barto), chapter on exploration vs. exploitation — the fundamental dilemma this pattern illustrates
  - AutoGen documentation on "termination conditions" — how to define when an agent should stop even without a perfect result
