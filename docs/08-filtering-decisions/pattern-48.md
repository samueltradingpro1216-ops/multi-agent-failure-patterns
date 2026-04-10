# Pattern #48 — Missing Decision Conditions

**Category:** Filtering & Decisions
**Severity:** Medium
**Tags:** `supervisor`, `executor`, `condition-mismatch`, `silent-rejection`, `state-asymmetry`

---

## 1. Observable Symptoms

The following symptoms appear in isolation or combination and are frequently misattributed to data quality issues or executor bugs:

- **Approval rate drops unexpectedly.** The supervisor logs show a high approval rate (e.g., 70–80%), but the executor's action completion rate is substantially lower (e.g., 30–40%). The gap is not explained by any logged error.
- **Silent discards.** Actions disappear after the supervisor emits `GO`. No exception is raised, no error is logged at the supervisor level. The executor simply does not act.
- **Non-deterministic behaviour under identical inputs.** Two events with the same confidence score and the same supervisor verdict produce different outcomes, because the executor's hidden condition (e.g., a multi-bar confirmation flag) evaluates differently.
- **Phantom approvals in audit logs.** Post-incident reviews show the supervisor approved dozens of actions that the executor never executed. Operations staff interpret this as executor failure, but the executor was behaving correctly — it was enforcing a condition the supervisor did not know existed.
- **Calibration asymmetry.** Tuning the supervisor's threshold improves its approval rate but has no measurable effect on end-to-end throughput, because the executor's independent gate remains the binding constraint.

---

## 2. Field Story

A mid-sized logistics company deployed an automated warehouse picking system. A supervisor agent monitored incoming pick orders and evaluated each one against a confidence model: if the model returned a score above 0.50, the supervisor emitted a `DISPATCH` command to the robotic arm executor.

After six weeks of operation, the operations team noticed that throughput was consistently 38% lower than the simulation predicted. The supervisor's logs showed it was approving roughly 72% of orders. The executor's logs showed it was completing only 34%. No errors, no alerts.

An engineer audited the executor's source code and found a three-condition gate that had been written during hardware integration:

1. Confidence score received from supervisor > 0.50 (mirroring the supervisor).
2. Aisle occupancy sensor: clear for at least 2 consecutive scan cycles.
3. Arm calibration drift: below 0.3 mm deviation on the last 5 movements.

The second and third conditions were never exposed to the supervisor. They had been added by the hardware integration team to prevent collisions and mechanical wear. They were documented in an internal hardware integration note that the software team had never seen.

The fix required the executor to publish its internal gate state to a shared message bus, and the supervisor to ingest that state before deciding. Throughput rose to within 4% of simulation targets within two days of deployment.

---

## 3. Technical Root Cause

The root cause is **condition-set divergence between the decision layer and the execution layer**. In a well-designed supervisor-executor architecture, the supervisor holds the complete set of conditions necessary to make a valid dispatch decision. When the executor holds additional conditions that are invisible to the supervisor, three failure modes arise:

**3.1 State asymmetry.** The supervisor and executor observe different slices of system state. The supervisor sees the model output. The executor sees the model output plus hardware telemetry. The dispatcher has no single authoritative view of readiness.

**3.2 No feedback on rejection.** Executor-side rejections are typically implemented as early returns or no-ops inside the executor's main loop. They are correct from the executor's perspective (it is protecting itself), but they do not propagate a structured rejection signal back to the supervisor. The supervisor cannot learn that its approval was overridden.

**3.3 Threshold drift.** Over time, operators tune the supervisor's confidence threshold trying to improve throughput, unaware that the executor's conditions are the actual constraint. This creates a false optimisation loop: the supervisor becomes more permissive, approval rates rise, but throughput is unchanged.

The technical precondition for this pattern is always the same: conditions accumulate in the executor during development (hardware integration, safety patches, workarounds) without a corresponding update to the supervisor's decision model.

---

## 4. Detection

### 4.1 Approval-to-Execution Rate Monitor

Compute the ratio of supervisor approvals to executor completions over a rolling window. A sustained gap larger than a configurable threshold (e.g., 10%) triggers an alert.

```python
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


@dataclass
class DecisionAuditMonitor:
    window_seconds: int = 300
    alert_threshold: float = 0.10  # alert if gap exceeds 10%
    _approvals: Deque[float] = field(default_factory=deque)
    _completions: Deque[float] = field(default_factory=deque)

    def record_approval(self) -> None:
        self._approvals.append(time.monotonic())
        self._evict(self._approvals)

    def record_completion(self) -> None:
        self._completions.append(time.monotonic())
        self._evict(self._completions)

    def _evict(self, q: Deque[float]) -> None:
        cutoff = time.monotonic() - self.window_seconds
        while q and q[0] < cutoff:
            q.popleft()

    def check(self) -> dict:
        approvals = len(self._approvals)
        completions = len(self._completions)
        if approvals == 0:
            return {"status": "no_data", "approvals": 0, "completions": 0}
        gap = (approvals - completions) / approvals
        return {
            "status": "ALERT" if gap > self.alert_threshold else "OK",
            "approvals": approvals,
            "completions": completions,
            "gap_ratio": round(gap, 4),
            "threshold": self.alert_threshold,
        }
```

### 4.2 Condition-Set Diff Scanner

At startup, compare the supervisor's declared condition set against the executor's registered gate conditions. Raise a `ConfigurationWarning` if they diverge.

```python
from dataclasses import dataclass
from typing import Set
import warnings


@dataclass
class ConditionRegistry:
    supervisor_conditions: Set[str]
    executor_conditions: Set[str]

    def audit(self) -> dict:
        supervisor_only = self.supervisor_conditions - self.executor_conditions
        executor_only = self.executor_conditions - self.supervisor_conditions
        shared = self.supervisor_conditions & self.executor_conditions
        issues = []
        if executor_only:
            msg = (
                f"Executor enforces conditions unknown to supervisor: {sorted(executor_only)}. "
                "Supervisor approvals may be silently rejected."
            )
            warnings.warn(msg, stacklevel=2)
            issues.append({"type": "hidden_executor_condition", "conditions": sorted(executor_only)})
        if supervisor_only:
            msg = (
                f"Supervisor checks conditions not enforced by executor: {sorted(supervisor_only)}. "
                "These checks have no effect on execution."
            )
            warnings.warn(msg, stacklevel=2)
            issues.append({"type": "phantom_supervisor_condition", "conditions": sorted(supervisor_only)})
        return {
            "shared": sorted(shared),
            "supervisor_only": sorted(supervisor_only),
            "executor_only": sorted(executor_only),
            "issues": issues,
        }


# Usage at startup
registry = ConditionRegistry(
    supervisor_conditions={"confidence_threshold", "queue_depth_ok"},
    executor_conditions={"confidence_threshold", "queue_depth_ok", "aisle_clear", "arm_drift_ok"},
)
report = registry.audit()
print(report)
```

### 4.3 Rejection Reason Instrumentation

Modify the executor to emit a structured rejection event whenever its internal gate blocks an approved action.

```python
import json
import logging
from dataclasses import dataclass, asdict
from typing import Optional


logger = logging.getLogger("executor.gate")


@dataclass
class GateResult:
    action_id: str
    supervisor_approved: bool
    gate_passed: bool
    blocking_condition: Optional[str]
    gate_state: dict

    def to_event(self) -> dict:
        return {
            "event": "GATE_RESULT",
            **asdict(self),
        }


def executor_gate(action_id: str, supervisor_approved: bool, telemetry: dict) -> GateResult:
    if not supervisor_approved:
        return GateResult(
            action_id=action_id,
            supervisor_approved=False,
            gate_passed=False,
            blocking_condition="supervisor_rejected",
            gate_state=telemetry,
        )
    if not telemetry.get("aisle_clear", False):
        result = GateResult(
            action_id=action_id,
            supervisor_approved=True,
            gate_passed=False,
            blocking_condition="aisle_clear",
            gate_state=telemetry,
        )
        logger.warning(json.dumps(result.to_event()))
        return result
    if telemetry.get("arm_drift_mm", 999) >= 0.3:
        result = GateResult(
            action_id=action_id,
            supervisor_approved=True,
            gate_passed=False,
            blocking_condition="arm_drift_ok",
            gate_state=telemetry,
        )
        logger.warning(json.dumps(result.to_event()))
        return result
    return GateResult(
        action_id=action_id,
        supervisor_approved=True,
        gate_passed=True,
        blocking_condition=None,
        gate_state=telemetry,
    )
```

---

## 5. Fix

### 5.1 Publish Executor Gate State to Supervisor

The executor publishes its internal readiness state to a shared bus before the supervisor makes its decision. The supervisor ingests this state and includes executor readiness as a first-class condition.

```python
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutorReadinessState:
    timestamp: float
    aisle_clear: bool
    arm_drift_mm: float
    arm_drift_ok: bool
    ttl_seconds: int = 5

    def is_valid(self) -> bool:
        return (time.monotonic() - self.timestamp) < self.ttl_seconds

    def is_ready(self) -> bool:
        return self.is_valid() and self.aisle_clear and self.arm_drift_ok


class SupervisorWithExecutorState:
    def __init__(self, confidence_threshold: float = 0.50):
        self.confidence_threshold = confidence_threshold
        self._executor_state: Optional[ExecutorReadinessState] = None

    def update_executor_state(self, state: ExecutorReadinessState) -> None:
        self._executor_state = state

    def decide(self, action_id: str, confidence: float) -> dict:
        reasons = []
        if confidence <= self.confidence_threshold:
            reasons.append(f"confidence {confidence:.3f} <= threshold {self.confidence_threshold}")
        if self._executor_state is None or not self._executor_state.is_valid():
            reasons.append("executor_state_stale_or_missing")
        elif not self._executor_state.is_ready():
            if not self._executor_state.aisle_clear:
                reasons.append("aisle_not_clear")
            if not self._executor_state.arm_drift_ok:
                reasons.append(f"arm_drift {self._executor_state.arm_drift_mm:.3f}mm >= 0.3mm")
        verdict = "GO" if not reasons else "NO_GO"
        return {"action_id": action_id, "verdict": verdict, "reasons": reasons}
```

### 5.2 Unified Condition Contract via Interface

Define a shared `ConditionSet` interface that both the supervisor and executor implement and validate against at startup. Any condition added to the executor must be declared in the shared contract, which forces the supervisor to acknowledge it.

```python
from abc import ABC, abstractmethod
from typing import FrozenSet, Mapping, Any


class ConditionEvaluator(ABC):
    @property
    @abstractmethod
    def declared_conditions(self) -> FrozenSet[str]:
        """All condition names this evaluator can check."""
        ...

    @abstractmethod
    def evaluate(self, context: Mapping[str, Any]) -> dict[str, bool]:
        """Return a pass/fail result for each declared condition."""
        ...


class SupervisorConditions(ConditionEvaluator):
    @property
    def declared_conditions(self) -> FrozenSet[str]:
        return frozenset({"confidence_threshold", "aisle_clear", "arm_drift_ok"})

    def evaluate(self, context: Mapping[str, Any]) -> dict[str, bool]:
        return {
            "confidence_threshold": context.get("confidence", 0) > 0.50,
            "aisle_clear": context.get("aisle_clear", False),
            "arm_drift_ok": context.get("arm_drift_mm", 999) < 0.3,
        }


class ExecutorConditions(ConditionEvaluator):
    @property
    def declared_conditions(self) -> FrozenSet[str]:
        return frozenset({"confidence_threshold", "aisle_clear", "arm_drift_ok"})

    def evaluate(self, context: Mapping[str, Any]) -> dict[str, bool]:
        return {
            "confidence_threshold": context.get("confidence", 0) > 0.50,
            "aisle_clear": context.get("aisle_clear", False),
            "arm_drift_ok": context.get("arm_drift_mm", 999) < 0.3,
        }


def assert_condition_parity(sup: ConditionEvaluator, exe: ConditionEvaluator) -> None:
    if sup.declared_conditions != exe.declared_conditions:
        diff = sup.declared_conditions.symmetric_difference(exe.declared_conditions)
        raise RuntimeError(
            f"Supervisor and executor condition sets diverge. Symmetric difference: {sorted(diff)}"
        )
```

---

## 6. Architectural Prevention

- **Single source of truth for conditions.** Store the authoritative condition set in a shared configuration schema (e.g., a YAML contract or a Pydantic model). Both the supervisor and executor load from this schema at startup. Adding a condition requires updating the schema, which triggers a validation step for both components.
- **Bidirectional telemetry channels.** Design executor-to-supervisor telemetry as a first-class channel, not an afterthought. Define the channel's schema, latency guarantees, and staleness policy before any conditions are written.
- **Startup parity assertion.** Add an integration test and a runtime startup check that compares the supervisor's declared conditions against the executor's. Treat any divergence as a deployment blocker.
- **Rejection event contracts.** Require the executor to emit a structured rejection event (with a reason code from a controlled vocabulary) for every supervisor-approved action it does not execute. Route these events to the same observability pipeline as approvals.

---

## 7. Anti-patterns

- **Duplicating conditions silently.** Adding the same condition to both the supervisor and executor independently, with no shared definition, leads to drift: the two implementations will diverge over time as thresholds are tuned separately.
- **Using log scraping as the primary detection method.** Relying on grepping executor logs for rejection keywords is fragile. Rejection reason codes must be structured, queryable, and tied to the original approval event via a correlation ID.
- **Treating executor-side gates as implementation details.** Hardware integration gates, safety interlocks, and calibration checks are decision conditions. They belong in the decision model, not hidden in executor internals.
- **Increasing supervisor permissiveness to compensate.** Lowering the supervisor's confidence threshold to "push more approvals through" does not address hidden executor conditions. It increases the approval rate while leaving throughput unchanged, and reduces overall decision quality.

---

## 8. Edge Cases

- **Transient vs. persistent blocking conditions.** A condition such as `aisle_clear` may be true 95% of the time. The supervisor must distinguish between a condition that is structurally blocking (always false, indicating a misconfiguration) and one that is transiently blocking (intermittently false, indicating normal operational state). Metrics should track both.
- **Stale executor state.** If the executor's readiness state is published on a 1-second interval but the supervisor polls every 100ms, the supervisor may act on a state that is up to 1 second old. Define and enforce a TTL on executor state; treat expired state as "not ready."
- **Condition ordering interactions.** When multiple executor conditions are false simultaneously, the rejection reason should capture all blocking conditions, not just the first one encountered. A single-condition short-circuit in the executor produces misleading diagnostics.
- **Condition added during incident response.** Under operational pressure, engineers may add a condition directly to the executor as a quick safety measure. This is the highest-risk moment for condition divergence. Require that any executor-side condition change triggers an immediate supervisor-side review within 24 hours.

---

## 9. Audit Checklist

```text
[ ] Supervisor and executor condition sets are compared at startup; divergence fails deployment.
[ ] All executor gate conditions are declared in the shared condition schema.
[ ] Executor emits a structured rejection event for every supervisor-approved action it does not execute.
[ ] Rejection events include: action_id, blocking_condition (from controlled vocabulary), gate_state snapshot.
[ ] Approval-to-execution gap ratio is monitored with a configurable alert threshold.
[ ] Executor readiness state is published to supervisor with a defined TTL.
[ ] Supervisor ingests executor readiness state before making a dispatch decision.
[ ] No condition exists solely in executor code without a corresponding entry in the shared schema.
[ ] Integration tests assert end-to-end throughput matches simulation under known conditions.
[ ] Incident response procedure requires supervisor review within 24 hours of any executor-side condition change.
```

---

## 10. Further Reading

- Hollnagel, E. (2012). *FRAM: The Functional Resonance Analysis Method*. Ashgate. — On how conditions accumulate in complex sociotechnical systems without explicit coordination.
- Gamma, E., et al. (1994). *Design Patterns: Elements of Reusable Object-Oriented Software*. Addison-Wesley. — The Chain of Responsibility pattern as a formal model for multi-stage condition evaluation.
- Kleppmann, M. (2017). *Designing Data-Intensive Applications*. O'Reilly. — Chapter 8 on distributed system faults; relevant to state consistency between supervisor and executor.
- NIST SP 800-204B (2022). *Attribute-based Access Control for Microservices*. — Policy-as-code approaches that enforce condition parity between policy decision points and policy enforcement points; directly analogous to supervisor/executor separation.
- Sculley, D., et al. (2015). "Hidden Technical Debt in Machine Learning Systems." *NeurIPS 2015*. — Section on entanglement and hidden feedback loops in ML-based decision systems.
