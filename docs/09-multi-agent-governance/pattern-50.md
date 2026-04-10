# Pattern #50 — Agent State Lost on Restart

**Category:** Multi-Agent Governance
**Severity:** High
**Tags:** `state-persistence`, `warm-up-period`, `restart`, `calibration`, `degraded-decisions`

---

## 1. Observable Symptoms

The degraded window after a restart is brief relative to the overall uptime of the system, which makes this pattern easy to overlook in aggregate metrics but consequential in practice:

- **Daily performance dip at a fixed time.** Metric dashboards show a consistent degradation in decision quality (higher error rate, lower prediction accuracy, suboptimal actions) that begins at the scheduled restart time and recovers after 15–30 minutes.
- **Cold-start anomalies.** The agent's first decisions of the day are markedly different from decisions made after 30 minutes of operation. When inspected, the early decisions lack the context, learned offsets, or recent history that the later decisions incorporate.
- **Log gap after restart.** The agent's internal state log shows no entries between the last pre-restart record and the first post-restart record. Everything accumulated in memory during the previous session is absent.
- **Warm-up-dependent accuracy.** Offline benchmarks that reset the agent to its initial state consistently show lower accuracy than production benchmarks taken mid-session. The discrepancy is attributed to "warm-up effects" but never addressed structurally.
- **Alarm fatigue during the restart window.** Alerting systems configured on decision-quality metrics generate spurious alerts every morning. Operators learn to suppress or ignore alerts in the first 20 minutes after restart, which also suppresses legitimate alerts during the same window.

---

## 2. Field Story

A regional energy utility deployed an intelligent dispatch agent to manage load balancing across a network of 47 substations. The agent maintained a rolling 6-hour context window of consumption patterns, a set of learned per-substation demand offsets calibrated over days of operation, and a short-term anomaly buffer that tracked unusual load events from the past 90 minutes.

The utility's infrastructure team configured the agent to restart every night at 02:00 during the daily maintenance window. This was standard practice inherited from a previous non-AI control system and was never re-evaluated after the intelligent agent was deployed.

On restart, the agent came up with an empty context window, zeroed calibration offsets, and no anomaly buffer. The calibration rebuild required approximately 18 minutes of live data ingestion before the per-substation offsets converged to operationally useful values. During those 18 minutes, the agent's load-balancing decisions were based on generic defaults rather than learned substation-specific patterns.

The 02:00 restart coincided with an early-morning industrial load surge (a district with several factories starting shifts at 05:30). During the 18-minute rebuild window, the agent failed to anticipate the surge correctly on four separate mornings and issued suboptimal switching commands. On one occasion this contributed to a voltage sag affecting 2,300 residential customers for 11 minutes.

A post-incident review identified the restart-induced state loss as the root cause. The fix involved implementing a state snapshot written to durable storage at shutdown and loaded at startup, reducing the effective calibration rebuild time from 18 minutes to under 90 seconds.

---

## 3. Technical Root Cause

The root cause is the **lack of state persistence across process boundaries**. Three structural deficits combine to produce the pattern:

**3.1 State lives exclusively in memory.** The agent's learned calibration, context window, and accumulated metrics exist only as Python objects (dictionaries, NumPy arrays, deque structures) in the process heap. When the process exits — whether scheduled or due to a crash — all of this state is destroyed. The agent does not distinguish between "process exit" and "start fresh."

**3.2 No shutdown hook for state serialisation.** The restart sequence does not include a step that serialises in-memory state to durable storage before the process terminates. This is typically because the agent was initially prototyped without persistence, the feature was never added, and the system was put into production before the omission was noticed.

**3.3 No startup hook for state restoration.** Even if a previous snapshot exists, the agent does not attempt to load it on startup. The initialisation path unconditionally creates empty data structures. The snapshot, if it exists, is invisible to the running agent.

A secondary contributing factor is **maintenance window timing**: restart schedules inherited from earlier, stateless systems are applied without considering the state-rebuild cost specific to stateful agents.

---

## 4. Detection

### 4.1 Post-Restart Decision Quality Monitor

Compute a decision quality metric (e.g., prediction error, action optimality score) in two windows: the first N minutes after restart and the baseline window from steady-state operation. Alert when the post-restart window is significantly worse.

```python
import time
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


@dataclass
class RestartQualityMonitor:
    restart_window_minutes: float = 20.0
    baseline_window_minutes: float = 60.0
    degradation_threshold: float = 0.15  # 15% worse than baseline triggers alert
    _samples: Deque[tuple] = field(default_factory=deque)  # (timestamp, score)
    _restart_time: Optional[float] = None

    def record_restart(self) -> None:
        self._restart_time = time.monotonic()

    def record_score(self, score: float) -> None:
        self._samples.append((time.monotonic(), score))
        cutoff = time.monotonic() - (self.baseline_window_minutes * 60)
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def check(self) -> dict:
        if self._restart_time is None or not self._samples:
            return {"status": "no_data"}
        now = time.monotonic()
        restart_cutoff = self._restart_time + (self.restart_window_minutes * 60)
        post_restart = [s for t, s in self._samples if t <= restart_cutoff]
        baseline = [s for t, s in self._samples if t > restart_cutoff]
        if not post_restart or not baseline:
            return {"status": "insufficient_data",
                    "post_restart_samples": len(post_restart),
                    "baseline_samples": len(baseline)}
        pr_mean = statistics.mean(post_restart)
        bl_mean = statistics.mean(baseline)
        degradation = (bl_mean - pr_mean) / bl_mean if bl_mean != 0 else 0.0
        status = "DEGRADED" if degradation > self.degradation_threshold else "OK"
        return {
            "status": status,
            "post_restart_mean": round(pr_mean, 4),
            "baseline_mean": round(bl_mean, 4),
            "degradation_ratio": round(degradation, 4),
            "threshold": self.degradation_threshold,
            "seconds_since_restart": round(now - self._restart_time, 1),
        }
```

### 4.2 State Completeness Check on Startup

At startup, the agent evaluates whether its loaded state (from snapshot or cold start) is operationally complete. It logs a structured `STATE_READINESS` event that is queryable in the observability pipeline.

```python
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("agent.startup")


@dataclass
class AgentStateReadiness:
    context_window_entries: int
    calibration_offsets_loaded: bool
    anomaly_buffer_entries: int
    snapshot_age_seconds: Optional[float]
    required_context_entries: int = 100
    required_anomaly_entries: int = 20

    def evaluate(self) -> dict:
        issues = []
        if self.context_window_entries < self.required_context_entries:
            issues.append(
                f"context_window has {self.context_window_entries} entries "
                f"(need {self.required_context_entries})"
            )
        if not self.calibration_offsets_loaded:
            issues.append("calibration_offsets not loaded — using defaults")
        if self.anomaly_buffer_entries < self.required_anomaly_entries:
            issues.append(
                f"anomaly_buffer has {self.anomaly_buffer_entries} entries "
                f"(need {self.required_anomaly_entries})"
            )
        if self.snapshot_age_seconds is not None and self.snapshot_age_seconds > 3600:
            issues.append(
                f"snapshot is {self.snapshot_age_seconds:.0f}s old — may be stale"
            )
        readiness = "WARM" if not issues else "COLD"
        event = {
            "event": "STATE_READINESS",
            "readiness": readiness,
            "issues": issues,
            "context_window_entries": self.context_window_entries,
            "calibration_loaded": self.calibration_offsets_loaded,
            "anomaly_buffer_entries": self.anomaly_buffer_entries,
            "snapshot_age_seconds": self.snapshot_age_seconds,
            "timestamp": time.time(),
        }
        logger.info(json.dumps(event))
        return event
```

### 4.3 Rebuild Time Tracker

Measure the wall-clock time from agent startup to the moment the agent declares itself fully calibrated. Publish this as a metric. Alert if the rebuild time exceeds a threshold.

```python
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("agent.calibration")


@dataclass
class CalibrationTracker:
    agent_id: str
    alert_threshold_seconds: float = 300.0
    _start_time: float = field(default_factory=time.monotonic)
    _ready_time: Optional[float] = None

    def mark_ready(self, context: dict = None) -> dict:
        self._ready_time = time.monotonic()
        elapsed = self._ready_time - self._start_time
        event = {
            "event": "CALIBRATION_COMPLETE",
            "agent_id": self.agent_id,
            "rebuild_seconds": round(elapsed, 2),
            "alert_threshold_seconds": self.alert_threshold_seconds,
            "exceeded_threshold": elapsed > self.alert_threshold_seconds,
            "context": context or {},
        }
        if event["exceeded_threshold"]:
            logger.warning(f"Calibration rebuild exceeded threshold: {event}")
        else:
            logger.info(f"Calibration rebuild complete: {event}")
        return event

    @property
    def is_ready(self) -> bool:
        return self._ready_time is not None

    @property
    def rebuild_seconds(self) -> Optional[float]:
        if self._ready_time is None:
            return None
        return round(self._ready_time - self._start_time, 2)
```

---

## 5. Fix

### 5.1 State Snapshot: Serialise on Shutdown, Restore on Startup

Implement a `StateManager` that serialises the agent's in-memory state to a durable store (local file, Redis, S3) on a graceful shutdown signal, and restores it on startup.

```python
import json
import os
import signal
import time
import pathlib
import logging
from dataclasses import dataclass, field
from collections import deque
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger("agent.state")


@dataclass
class SubstationCalibration:
    offsets: Dict[str, float] = field(default_factory=dict)
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"offsets": self.offsets, "last_updated": self.last_updated}

    @classmethod
    def from_dict(cls, d: dict) -> "SubstationCalibration":
        obj = cls()
        obj.offsets = d.get("offsets", {})
        obj.last_updated = d.get("last_updated", 0.0)
        return obj


@dataclass
class AgentState:
    context_window: Deque[dict] = field(default_factory=lambda: deque(maxlen=360))
    calibration: SubstationCalibration = field(default_factory=SubstationCalibration)
    anomaly_buffer: Deque[dict] = field(default_factory=lambda: deque(maxlen=90))

    def to_dict(self) -> dict:
        return {
            "context_window": list(self.context_window),
            "calibration": self.calibration.to_dict(),
            "anomaly_buffer": list(self.anomaly_buffer),
            "snapshot_timestamp": time.time(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AgentState":
        obj = cls()
        obj.context_window = deque(d.get("context_window", []), maxlen=360)
        obj.calibration = SubstationCalibration.from_dict(d.get("calibration", {}))
        obj.anomaly_buffer = deque(d.get("anomaly_buffer", []), maxlen=90)
        return obj


class StateManager:
    def __init__(self, snapshot_path: str, max_age_seconds: float = 7200.0):
        self.snapshot_path = pathlib.Path(snapshot_path)
        self.max_age_seconds = max_age_seconds

    def save(self, state: AgentState) -> None:
        tmp_path = self.snapshot_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state.to_dict(), indent=2))
        tmp_path.replace(self.snapshot_path)  # atomic on POSIX
        logger.info(f"State snapshot written to {self.snapshot_path}")

    def load(self) -> Optional[AgentState]:
        if not self.snapshot_path.exists():
            logger.info("No snapshot found — starting cold.")
            return None
        raw = json.loads(self.snapshot_path.read_text())
        age = time.time() - raw.get("snapshot_timestamp", 0.0)
        if age > self.max_age_seconds:
            logger.warning(
                f"Snapshot is {age:.0f}s old (max {self.max_age_seconds}s) — discarding."
            )
            return None
        logger.info(f"State snapshot loaded from {self.snapshot_path} (age {age:.0f}s).")
        return AgentState.from_dict(raw)


class DispatchAgent:
    def __init__(self, snapshot_path: str = "/var/agent/state_snapshot.json"):
        self.state_manager = StateManager(snapshot_path)
        self.state = self.state_manager.load() or AgentState()
        self._register_shutdown_hook()

    def _register_shutdown_hook(self) -> None:
        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT, self._on_shutdown)

    def _on_shutdown(self, signum: int, frame: Any) -> None:
        logger.info(f"Received signal {signum} — saving state before exit.")
        self.state_manager.save(self.state)
        raise SystemExit(0)
```

### 5.2 Periodic Checkpoint During Operation

In addition to shutdown-triggered saves, write a checkpoint every N minutes so that a crash (not a graceful shutdown) still preserves most of the agent's state.

```python
import threading
import time
import logging
from typing import Callable

logger = logging.getLogger("agent.checkpoint")


class PeriodicCheckpointer:
    def __init__(
        self,
        save_fn: Callable[[], None],
        interval_seconds: float = 120.0,
    ):
        self.save_fn = save_fn
        self.interval_seconds = interval_seconds
        self._thread: threading.Thread = threading.Thread(
            target=self._run, daemon=True, name="state-checkpointer"
        )

    def start(self) -> None:
        self._thread.start()
        logger.info(
            f"Periodic checkpointer started (interval={self.interval_seconds}s)."
        )

    def _run(self) -> None:
        while True:
            time.sleep(self.interval_seconds)
            try:
                self.save_fn()
                logger.debug("Periodic checkpoint written.")
            except Exception as exc:
                logger.error(f"Checkpoint failed: {exc}", exc_info=True)


# Usage within DispatchAgent.__init__:
# self.checkpointer = PeriodicCheckpointer(
#     save_fn=lambda: self.state_manager.save(self.state),
#     interval_seconds=120,
# )
# self.checkpointer.start()
```

---

## 6. Architectural Prevention

- **State persistence as a first-class design requirement.** Before an agent enters production, define its state model explicitly: what data lives in memory, what its operational lifetime is, what the rebuild cost is from cold start. If rebuild cost exceeds an acceptable degradation window, persistence is mandatory, not optional.
- **Stateless vs. stateful agent classification.** Classify every agent in the system as stateless (can restart freely with no degradation) or stateful (restart causes a degradation window). Stateful agents require a persistence strategy as a deployment prerequisite.
- **Maintenance window scheduling adapted to agents.** Avoid scheduling restarts immediately before high-stakes operational windows (shift changes, peak demand periods, market open). If a restart must occur, schedule it far enough in advance that the agent is fully rebuilt before the high-stakes period begins.
- **Health endpoint exposing warm-up status.** The agent exposes a `/health/ready` endpoint that returns `503 Service Unavailable` (with a `Retry-After` header) until the warm-up period is complete. The load balancer or orchestrator routes traffic away from the agent until it declares itself ready.

---

## 7. Anti-patterns

- **Relying on graceful shutdown alone.** SIGKILL, OOM kills, and hardware failures do not trigger graceful shutdown handlers. Periodic checkpointing is required as a complement to shutdown hooks, not a replacement.
- **Snapshot TTL that is too short.** A snapshot TTL shorter than the restart cycle (e.g., a 1-hour TTL on a system that restarts every 24 hours) results in stale snapshots being discarded and the agent always starting cold. TTL must be longer than the maximum expected inter-restart interval.
- **Serialising state to the same disk as the application.** If the restart is triggered by a disk failure or a corrupted deployment, the snapshot may be inaccessible. State snapshots must be written to a separate, durable store (dedicated volume, object storage, Redis with persistence enabled).
- **Treating warm-up degradation as acceptable.** "The agent takes 20 minutes to warm up" is not an operational characteristic to document and accept — it is a defect to fix. Accepting warm-up degradation masks the structural absence of persistence and prevents the issue from being prioritised.

---

## 8. Edge Cases

- **Schema migration after code update.** The agent code is updated and the state schema changes (a new field is added, an existing field is renamed). The snapshot written by the old version cannot be loaded by the new version. Implement a versioned snapshot format and a migration loader that upgrades old snapshots to the current schema.
- **Corrupted snapshot.** A crash during the checkpoint write operation can produce a truncated or malformed snapshot file. The `load` method must catch all deserialisation exceptions and fall back to cold start with a logged warning, rather than crashing.
- **Clock skew after restart.** The snapshot records wall-clock timestamps. If the system clock is adjusted after a restart (e.g., NTP correction), snapshot age calculations may be incorrect. Use `time.time()` for snapshot metadata but monotonic clocks for internal rate calculations.
- **Multi-instance deployments.** If multiple agent instances run in parallel (for redundancy or throughput), each instance writes its own snapshot. On restart, each instance must load its own snapshot, not a sibling's. Snapshot file paths must include the instance ID.
- **State accumulated during an anomalous period.** If the agent was running during a period of abnormal system behaviour (a network outage, a data feed failure), its calibration may have absorbed the anomaly as a baseline. Restoring this snapshot resumes the miscalibrated state. Implement an optional snapshot validity check that compares the snapshot's baseline against recent data before loading.

---

## 9. Audit Checklist

```text
[ ] Every agent is classified as stateless or stateful in the system design document.
[ ] All stateful agents have a documented state model (fields, types, rebuild cost from cold start).
[ ] The agent serialises state to durable storage on SIGTERM/SIGINT via a registered shutdown hook.
[ ] A periodic checkpoint runs every N minutes (N <= acceptable data loss window) as a crash safety net.
[ ] The agent loads its snapshot on startup and logs a STATE_READINESS event with readiness classification.
[ ] Snapshots have a version field; the loader handles schema migration and gracefully falls back on parse errors.
[ ] Snapshot files are stored on a separate durable store, not on the application's local disk.
[ ] The agent exposes a /health/ready endpoint that returns 503 until warm-up is complete.
[ ] Post-restart decision quality is monitored; a sustained degradation triggers an alert.
[ ] Maintenance window scheduling accounts for the agent's warm-up time relative to operational high-stakes windows.
[ ] Multi-instance deployments use instance-scoped snapshot paths.
```

---

## 10. Further Reading

- Kleppmann, M. (2017). *Designing Data-Intensive Applications*. O'Reilly. — Chapter 3 (storage engines) and Chapter 7 (transactions); foundational treatment of durability and crash recovery.
- Gray, J., & Reuter, A. (1992). *Transaction Processing: Concepts and Techniques*. Morgan Kaufmann. — The canonical reference on checkpointing and write-ahead logging as mechanisms for state recovery.
- Kubernetes Documentation: Container Lifecycle Hooks. https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/ — `preStop` hooks as the orchestrator-level mechanism for triggering graceful shutdown and state serialisation.
- Redis Documentation: Persistence. https://redis.io/docs/management/persistence/ — RDB snapshots and AOF logging as off-the-shelf durable state stores for agent state that must survive process restarts.
- Sutton, R. S., & Barto, A. G. (2018). *Reinforcement Learning: An Introduction* (2nd ed.). MIT Press. — Chapter 9 on function approximation and the distinction between online learning (state is in the weights) and episodic reset; relevant to understanding why stateful agents require explicit persistence.
