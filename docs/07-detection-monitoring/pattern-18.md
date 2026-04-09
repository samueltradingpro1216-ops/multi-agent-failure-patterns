# Pattern #18 — Snapshot vs Sustained Check

| Field | Value |
|---|---|
| **ID** | 18 |
| **Category** | Detection & Monitoring |
| **Severity** | Medium |
| **Affected Frameworks** | LangChain / CrewAI / AutoGen / LangGraph / Custom |
| **Average Debugging Time (if undetected)** | 2 to 10 days |
| **Keywords** | snapshot, sustained, sliding window, health check, windowed average, burn rate, alerting noise |

---

## 1. Observable Symptoms

The symptoms of this pattern are deceptive because the system appears to be working correctly from the outside. Alerts fire, actions are taken, and logs show responses — yet the underlying behaviour is pathological.

**Operational symptoms:**

- Health alerts fire and immediately self-resolve within seconds, producing a noisy alert history with hundreds of entries per day that engineers learn to ignore.
- Emergency shutdown or scale-out actions are triggered during brief, harmless spikes (garbage collection, cold-start JIT compilation, large batch import) and the system recovers before the automated action even completes.
- Dashboards show alert counts in the hundreds per week, but post-incident reviews confirm almost none of them corresponded to genuine degradation experienced by users.
- On-call engineers report "alert fatigue" and begin silencing or delaying notifications, masking the eventual real incident.
- Mean time to detect (MTTD) for genuine sustained overloads paradoxically increases: the monitoring system has cried wolf so many times that the real signal is buried.
- Agent orchestration systems (LangGraph, AutoGen) restart worker pools repeatedly in response to instantaneous CPU spikes, causing worse average latency than the spikes themselves would have caused.
- CI/CD pipelines abort jobs mid-run because a resource check sampled during a compilation burst reported a value above threshold.

**Code-level symptoms:**

- Health check functions return a Boolean from a single `psutil.cpu_percent()` call or a single queue depth read.
- Alert rules in YAML/JSON compare `value > threshold` with no `for:` duration clause.
- Automated remediation callbacks (scale-down, circuit-breaker trip) are registered directly on the health check result with no hysteresis.

---

## 2. Field Story (Anonymized)

A mid-sized software company operated a multi-agent CI/CD orchestration platform — internally called "Pipeline Brain" — that managed build, test, and deployment jobs across several hundred repositories. The platform used a custom Python agent to monitor worker node health and redistribute jobs when a node was deemed unhealthy.

The health agent ran on a 10-second polling loop. Its core check was:

```python
if psutil.cpu_percent(interval=None) > 90:
    mark_node_unhealthy(node_id)
    redistribute_jobs(node_id)
```

For three weeks after a major framework upgrade (which added aggressive LRU cache warming at job start), the on-call rotation was paged an average of 40 times per day. Each alert resolved within 30 seconds. Engineers assumed the alerting was "too sensitive" and adjusted the threshold from 90 to 95, then to 98. The noise continued.

The real failure arrived unannounced on a Tuesday morning. A memory leak in the newly upgraded dependency caused genuine sustained CPU saturation — 94% for 22 minutes — across six nodes simultaneously. Because engineers had been conditioned to ignore rapid-fire alerts, the incident was not acknowledged for 47 minutes. By then, 1,800 build jobs had queued behind the saturated nodes and two production deployments were delayed.

The post-mortem finding: the health check had never measured anything meaningful. `psutil.cpu_percent(interval=None)` returns the CPU percentage since the last call, which in a 10-second polling loop can spike to 95% for the 200ms duration of job startup and return to 30% by the next sample. The monitoring system had been measuring noise, not load.

The fix took four hours to implement and one sprint to roll out across all nodes. The sustained-load incident that followed the fix — the first real one — was detected in under 3 minutes.

---

## 3. Technical Root Cause

**Why a single sample is unreliable:**

Modern operating systems and runtimes produce highly non-stationary CPU and memory signals. Activities that produce transient spikes include:

- Garbage collection pauses (Python GC, JVM GC stop-the-world)
- JIT compilation on first execution
- OS page fault storms during memory allocation
- Network interrupt coalescing bursts
- Cache miss waterfalls on cold start

A single measurement has a sampling distribution with high variance. Comparing one sample to a fixed threshold is equivalent to a hypothesis test with a single observation — statistically unreliable regardless of threshold value.

**The correct signal: sustained exceedance**

The metric of interest is not "CPU > T at time t" but "CPU > T for duration D". Formally:

```
alert = ∀t' ∈ [t-D, t]: metric(t') > threshold
```

This is equivalent to a sliding window minimum: if `min(window) > threshold`, the entire window exceeds the threshold, meaning the condition has been sustained. In practice, a windowed average is often used because it is more noise-tolerant:

```
alert = mean(window[-D:]) > threshold
```

**Why automated remediation amplifies the problem:**

When a snapshot check triggers an action (job redistribution, node restart, scale-out), the action itself causes a secondary CPU spike on the target nodes receiving the redistributed load. This creates a positive feedback loop: spike → false alert → redistribution → new spike on new node → false alert. The system oscillates.

**Framework-specific aggravation:**

In LangGraph and CrewAI, agent nodes are stateful. Restarting a node mid-task due to a false health alert does not merely delay the task — it may corrupt shared state or require full re-execution from a checkpoint, multiplying the CPU cost of the false positive.

---

## 4. Detection

### 4.1 Manual Code Audit

Search for health check functions that read a single metric value and immediately compare it to a threshold without any windowing or duration requirement.

**Grep pattern — single-sample CPU check:**

```bash
grep -rn "cpu_percent\|cpu_usage\|memory_percent" --include="*.py" | \
  grep -v "window\|average\|rolling\|sustained\|history"
```

**Grep pattern — bare threshold comparison with no duration:**

```bash
grep -rn "if.*percent.*>\|if.*usage.*>" --include="*.py" | \
  grep -v "for.*seconds\|duration\|window\|count\|sustained"
```

**Code review checklist item:** Any health check that calls a metrics API and immediately returns True/False without accumulating a time series is a candidate for this pattern.

**Prometheus alert rule audit — missing `for:` clause:**

```bash
grep -rn "alert:" --include="*.yml" --include="*.yaml" -A 5 | \
  grep -B 3 "expr:" | grep -v "for:"
```

An alert rule with `expr:` but without `for:` fires on the first sample that satisfies the expression — a snapshot check.

### 4.2 Automated CI/CD

Add a static analysis step that enforces windowed metrics in health check modules.

```python
# ci_checks/check_snapshot_health.py
"""
CI gate: fails if any health-check file contains a bare threshold
comparison against a single-sample metric read.
"""
import ast
import sys
from pathlib import Path

SINGLE_SAMPLE_CALLS = {
    "cpu_percent", "cpu_usage", "memory_percent",
    "memory_usage", "queue_depth", "qsize",
}
WINDOW_INDICATORS = {
    "window", "history", "rolling", "average", "mean",
    "sustained", "duration", "deque", "samples",
}

def check_file(path: Path) -> list[str]:
    violations = []
    source = path.read_text(encoding="utf-8")
    # Heuristic: flag files that call a single-sample API
    # without any windowing keyword in the same function body.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        func_src = ast.get_source_segment(source, node) or ""
        has_sample_call = any(k in func_src for k in SINGLE_SAMPLE_CALLS)
        has_window = any(k in func_src for k in WINDOW_INDICATORS)
        if has_sample_call and not has_window:
            violations.append(
                f"{path}:{node.lineno}: function '{node.name}' reads "
                f"a single-sample metric without windowing."
            )
    return violations

def main(target_dirs: list[str]) -> int:
    all_violations = []
    for d in target_dirs:
        for py_file in Path(d).rglob("*.py"):
            if "health" in py_file.name or "monitor" in py_file.name:
                all_violations.extend(check_file(py_file))
    if all_violations:
        print("SNAPSHOT CHECK VIOLATIONS:")
        for v in all_violations:
            print(f"  {v}")
        return 1
    print("No snapshot-check violations found.")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["."]))
```

Add to CI pipeline:

```yaml
# .github/workflows/ci.yml (excerpt)
- name: Check for snapshot health patterns
  run: python ci_checks/check_snapshot_health.py src/ agents/
```

### 4.3 Runtime Production

Instrument health check functions to log both the instantaneous sample and the windowed average, then alert on divergence between the two. A large, persistent divergence means snapshot checks are being used in production.

```python
import logging
import time
from collections import deque

logger = logging.getLogger("health.audit")

class HealthCheckAuditor:
    """
    Wraps a metric reader and emits a warning whenever a snapshot
    decision would differ from a windowed decision.
    """
    def __init__(self, metric_fn, window_seconds: int = 60, sample_interval: float = 5.0):
        self._metric_fn = metric_fn
        self._window = deque(maxlen=int(window_seconds / sample_interval))
        self._threshold = None

    def set_threshold(self, threshold: float):
        self._threshold = threshold

    def record_and_evaluate(self) -> dict:
        sample = self._metric_fn()
        self._window.append((time.monotonic(), sample))
        windowed_mean = sum(v for _, v in self._window) / len(self._window)
        result = {
            "snapshot": sample,
            "windowed_mean": windowed_mean,
            "window_size": len(self._window),
        }
        if self._threshold is not None:
            snapshot_alert = sample > self._threshold
            windowed_alert = windowed_mean > self._threshold
            if snapshot_alert != windowed_alert:
                logger.warning(
                    "SNAPSHOT_VS_WINDOWED_DIVERGENCE: "
                    "snapshot_alert=%s windowed_alert=%s "
                    "sample=%.1f mean=%.1f threshold=%.1f",
                    snapshot_alert, windowed_alert,
                    sample, windowed_mean, self._threshold,
                )
            result["snapshot_alert"] = snapshot_alert
            result["windowed_alert"] = windowed_alert
        return result
```

---

## 5. Fix

### 5.1 Immediate Fix

Replace the single-sample read with a short blocking average. `psutil.cpu_percent(interval=1)` blocks for 1 second and returns the average over that interval — a minimal improvement that eliminates sub-second noise at the cost of slightly increased check latency.

```python
# BEFORE (snapshot — DO NOT USE)
import psutil

def is_node_overloaded(threshold: float = 90.0) -> bool:
    return psutil.cpu_percent(interval=None) > threshold

# AFTER (1-second blocking average — immediate fix)
import psutil

def is_node_overloaded(threshold: float = 90.0) -> bool:
    # interval=1 blocks for 1 second and returns avg over that window.
    # This eliminates millisecond-scale noise but does not catch
    # sustained load. Use only as an emergency short-term fix.
    return psutil.cpu_percent(interval=1) > threshold
```

For Prometheus alert rules, add a minimum `for:` clause immediately:

```yaml
# BEFORE
- alert: HighCPU
  expr: node_cpu_usage_percent > 90

# AFTER (immediate fix — 2-minute minimum duration)
- alert: HighCPU
  expr: node_cpu_usage_percent > 90
  for: 2m
  labels:
    severity: warning
```

### 5.2 Robust Fix

Implement a sliding window health checker with configurable window duration, minimum sustained fraction (what percentage of samples in the window must exceed the threshold before alerting), and hysteresis (different thresholds for alert entry and alert recovery).

```python
"""
robust_health_check.py

Sliding-window health checker with hysteresis and minimum sustained
fraction. Suitable for use in LangChain, CrewAI, AutoGen, LangGraph,
or any custom Python agent framework.
"""
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class WindowConfig:
    """Configuration for a windowed health check."""
    # Duration of the sliding window in seconds.
    window_seconds: float = 120.0
    # How often to collect a sample (seconds).
    sample_interval: float = 5.0
    # Fraction of samples in the window that must exceed alert_threshold
    # before the check transitions to ALERTING state.
    alert_fraction: float = 0.8
    # Threshold for entering ALERTING state.
    alert_threshold: float = 90.0
    # Threshold for leaving ALERTING state (hysteresis — must drop below
    # this before the alert clears). Must be <= alert_threshold.
    recovery_threshold: float = 75.0

    def __post_init__(self):
        assert self.recovery_threshold <= self.alert_threshold, (
            "recovery_threshold must be <= alert_threshold for hysteresis"
        )
        assert 0.0 < self.alert_fraction <= 1.0
        assert self.window_seconds > 0 and self.sample_interval > 0


class WindowedHealthCheck:
    """
    Maintains a time-stamped rolling window of metric samples and
    evaluates alert/recovery conditions with hysteresis.

    Usage:
        import psutil
        checker = WindowedHealthCheck(
            metric_fn=lambda: psutil.cpu_percent(interval=1),
            config=WindowConfig(window_seconds=120, alert_threshold=90),
            name="cpu",
        )
        # Call record() on your polling interval.
        checker.record()
        if checker.is_alerting:
            handle_overload()
    """

    def __init__(
        self,
        metric_fn: Callable[[], float],
        config: WindowConfig,
        name: str = "metric",
    ):
        self._metric_fn = metric_fn
        self._config = config
        self._name = name
        max_samples = int(config.window_seconds / config.sample_interval) + 1
        self._samples: deque[tuple[float, float]] = deque(maxlen=max_samples)
        self._alerting: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def record(self) -> float:
        """Collect one sample, update window, return current value."""
        value = self._metric_fn()
        now = time.monotonic()
        self._samples.append((now, value))
        self._evict_stale(now)
        self._evaluate()
        return value

    @property
    def is_alerting(self) -> bool:
        return self._alerting

    @property
    def windowed_mean(self) -> float:
        if not self._samples:
            return 0.0
        return sum(v for _, v in self._samples) / len(self._samples)

    @property
    def window_size(self) -> int:
        return len(self._samples)

    @property
    def sustained_fraction(self) -> float:
        """Fraction of samples currently exceeding alert_threshold."""
        if not self._samples:
            return 0.0
        count_above = sum(
            1 for _, v in self._samples
            if v > self._config.alert_threshold
        )
        return count_above / len(self._samples)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_stale(self, now: float) -> None:
        cutoff = now - self._config.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _evaluate(self) -> None:
        cfg = self._config
        frac = self.sustained_fraction
        mean = self.windowed_mean

        if not self._alerting:
            # Enter alerting state only when the sustained fraction
            # exceeds the configured minimum.
            if frac >= cfg.alert_fraction:
                self._alerting = True
                logger.warning(
                    "[%s] ALERT TRIGGERED: sustained_fraction=%.0f%% "
                    "mean=%.1f threshold=%.1f window_samples=%d",
                    self._name, frac * 100, mean,
                    cfg.alert_threshold, self.window_size,
                )
        else:
            # Leave alerting state only when mean drops below
            # recovery_threshold (hysteresis).
            if mean < cfg.recovery_threshold:
                self._alerting = False
                logger.info(
                    "[%s] ALERT CLEARED: mean=%.1f recovery_threshold=%.1f",
                    self._name, mean, cfg.recovery_threshold,
                )


# ------------------------------------------------------------------
# Example: integration with a CrewAI / LangGraph node monitor
# ------------------------------------------------------------------

def build_node_health_monitor(node_id: str) -> WindowedHealthCheck:
    """
    Factory for a production-grade CPU health checker for an
    agent worker node.
    """
    import psutil

    config = WindowConfig(
        window_seconds=120,      # 2-minute window
        sample_interval=5,       # sample every 5 seconds
        alert_fraction=0.8,      # 80% of samples must exceed threshold
        alert_threshold=90.0,    # alert at 90% CPU
        recovery_threshold=70.0, # clear alert when mean drops below 70%
    )
    return WindowedHealthCheck(
        metric_fn=lambda: psutil.cpu_percent(interval=1),
        config=config,
        name=f"cpu:{node_id}",
    )


def polling_loop_example(node_id: str, stop_after: int = 30) -> None:
    """Demonstrates a polling loop using WindowedHealthCheck."""
    import time
    checker = build_node_health_monitor(node_id)
    for _ in range(stop_after):
        value = checker.record()
        print(
            f"node={node_id} cpu={value:.1f}% "
            f"mean={checker.windowed_mean:.1f}% "
            f"frac={checker.sustained_fraction:.0%} "
            f"alerting={checker.is_alerting}"
        )
        time.sleep(checker._config.sample_interval)
```

---

## 6. Architectural Prevention

**Principle: metrics pipelines must always produce windowed outputs before they reach decision logic.**

1. **Separate collection from evaluation.** Metric collection (reading CPU, queue depth, etc.) must be decoupled from threshold evaluation. A collector writes raw samples to a time-series store or in-memory ring buffer. An evaluator reads from that store and computes aggregated statistics. Decision logic reads only from the evaluator, never from the collector.

2. **Mandate `for:` clauses in all Prometheus alert rules.** Encode this as a linter rule in CI (see Section 4.2). No alert rule should be merged without a minimum duration. A sane default for most agent-platform metrics is `for: 5m`; critical path metrics may use `for: 2m`; infrastructure-wide saturation may use `for: 10m`.

3. **Use burn-rate alerting for long-lived services.** Burn-rate alerting (from the Google SRE book) measures how fast an error budget is being consumed, which is inherently a windowed calculation. A 1-hour burn-rate alert is immune to transient spikes by construction.

4. **Implement hysteresis at the architectural level.** Alert on high threshold, recover on low threshold. This prevents alert flapping at the boundary condition and is a standard control-systems pattern.

5. **Add an alert dampening layer.** Even with windowed checks, group alerts by signal correlation and suppress duplicate notifications within a configurable cool-down window (e.g., 10 minutes). Tools: Prometheus Alertmanager's `group_wait` and `repeat_interval`; PagerDuty alert grouping.

6. **Test health checks with synthetic spikes.** Include a chaos test in CI that injects a 3-second CPU spike and asserts that the health checker does NOT transition to alerting state. This test would have caught the pattern described in the field story.

```python
# tests/test_windowed_health_check.py
import pytest
from unittest.mock import patch
import time
from robust_health_check import WindowedHealthCheck, WindowConfig

def test_transient_spike_does_not_alert():
    """A single 2-second spike must not trigger an alert."""
    call_count = 0

    def mock_cpu():
        nonlocal call_count
        call_count += 1
        # Spike only on the first call, then return to baseline.
        return 95.0 if call_count == 1 else 30.0

    config = WindowConfig(
        window_seconds=60,
        sample_interval=5,
        alert_fraction=0.8,
        alert_threshold=90.0,
        recovery_threshold=70.0,
    )
    checker = WindowedHealthCheck(mock_cpu, config, name="test_cpu")
    # Record 10 samples: 1 spike + 9 baseline.
    for _ in range(10):
        checker.record()
    assert not checker.is_alerting, (
        "A single spike must not trigger an alert with alert_fraction=0.8"
    )

def test_sustained_overload_alerts():
    """80% sustained exceedance must trigger an alert."""
    config = WindowConfig(
        window_seconds=60,
        sample_interval=5,
        alert_fraction=0.8,
        alert_threshold=90.0,
        recovery_threshold=70.0,
    )
    checker = WindowedHealthCheck(lambda: 95.0, config, name="test_cpu")
    # Record enough samples to fill the window fraction.
    for _ in range(10):
        checker.record()
    assert checker.is_alerting, (
        "Sustained 95% CPU must trigger an alert"
    )
```

---

## 7. Anti-patterns to Avoid

**Anti-pattern 1: Lowering the threshold instead of adding a window.**
When snapshot checks produce false positives, the instinct is to raise the threshold (90 → 95 → 98). This makes the alert less sensitive to real sustained overloads and does not address the root cause. Always add a window before adjusting a threshold.

**Anti-pattern 2: Using `psutil.cpu_percent(interval=None)` in any production code path.**
`interval=None` returns delta since the last call. If the last call was 0.1 seconds ago (common in fast loops), this reflects a 100ms window — effectively a snapshot. Always use `interval >= 1` or accumulate samples externally.

**Anti-pattern 3: Alert rules without `for:` in Prometheus/Grafana.**
The default behaviour when `for:` is absent is to fire on the first sample. This is the Prometheus equivalent of a snapshot check. Every production alert rule must have a `for:` clause.

**Anti-pattern 4: Triggering destructive actions (restart, scale-down, killswitch) directly from a health check without a cooldown or confirmation step.**
Destructive actions must require either (a) a sustained alert state for a minimum duration, or (b) manual confirmation, or (c) both. Snapshot-triggered destructive actions compound the oscillation problem described in Section 3.

**Anti-pattern 5: No hysteresis in alert recovery.**
Recovering from an alert at the same threshold at which it was triggered causes alert flapping at the boundary. Always recover at a lower threshold than the alert threshold.

---

## 8. Edge Cases and Variants

**Variant A: Queue depth snapshot check.**
The same pattern applies to message queue depth checks. A queue that momentarily peaks at 1000 messages during a burst import (lasting 5 seconds) triggers the same "queue overflow" alert as a genuinely stuck queue at 1000 messages for 30 minutes. Fix: windowed average queue depth with minimum duration, or use the queue drain rate as the signal (messages/second trending toward zero is more informative than absolute depth).

**Variant B: Memory snapshot check and OOM killer false positives.**
`psutil.virtual_memory().percent` can spike transiently when a large allocation is made and immediately released. A snapshot check that triggers an OOM-prevention response (killing the largest process) may kill a healthy process during a normal allocation peak. Fix: use `windowed_mean > 90%` sustained for 5+ minutes as the OOM-prevention trigger.

**Variant C: Multi-agent system with heterogeneous polling intervals.**
In LangChain/LangGraph pipelines where different agents poll health at different rates, a fast-polling agent (1-second interval) and a slow-polling agent (30-second interval) will disagree on instantaneous state. The slow-polling agent is effectively observing a longer window by default. Standardise polling intervals and window durations across all agents.

**Variant D: Distributed health check disagreement.**
When multiple health-check instances run on different nodes (e.g., in a CrewAI multi-node deployment), they may observe different instantaneous values for shared resources (network bandwidth, shared DB connection pool). Define a quorum rule: require that a majority of health-check instances agree on the sustained condition before any action is taken.

**Variant E: Cold-start window inflation.**
During the first `window_seconds` of system startup, the window is not yet full. A naive implementation will alert if even a single sample exceeds the threshold (because `1/1 = 100% > 80%`). Add a minimum window fill requirement: `if self.window_size < min_samples: return  # window not yet valid`.

---

## 9. Audit Checklist

Use this checklist during code review of any health monitoring component.

- [ ] **No bare `psutil.cpu_percent(interval=None)` calls** in health check functions.
- [ ] **No bare `psutil.virtual_memory().percent` comparisons** without a windowed history.
- [ ] **All Prometheus alert rules have a `for:` clause** of at least 2 minutes.
- [ ] **All automated remediation actions** (restart, scale-down, redistribute) require a sustained alert state, not a single sample trigger.
- [ ] **Hysteresis is implemented**: alert threshold > recovery threshold.
- [ ] **Minimum window fill guard** is present: checks are suppressed until the window contains at least N samples.
- [ ] **Unit tests exist** for both transient-spike (must not alert) and sustained-overload (must alert) scenarios.
- [ ] **CI gate exists** that flags health check files lacking windowing keywords.
- [ ] **Alert dampening** is configured to prevent duplicate pages within a cooldown window.
- [ ] **Burn-rate or error-budget alerting** is used for SLO-bound services rather than raw threshold alerting.
- [ ] **Queue depth checks** use drain rate or windowed average, not instantaneous depth.
- [ ] **Cold-start suppression** is implemented for all windowed checkers.

---

## 10. Further Reading

**Primary references:**

- Beyer, B. et al. *Site Reliability Engineering* (Google, 2016). Chapter 6: "Monitoring Distributed Systems." The burn-rate alerting model described there is a direct solution to this pattern.
- Prometheus documentation: [Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) — use recording rules to pre-aggregate time series before writing alert expressions.
- Prometheus documentation: [Alerting Rules — `for` clause](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/) — mandatory reading before writing any alert rule.
- `psutil` documentation: [`cpu_percent(interval)`](https://psutil.readthedocs.io/en/latest/#psutil.cpu_percent) — the `interval` parameter semantics are critical; `None` is almost never correct for health checks.

**Related patterns in this playbook:**

- Pattern #17 — Polling Interval Mismatch: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-17-polling-interval-mismatch.md](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-17-polling-interval-mismatch.md)
- Pattern #19 — False Positive Crash Detection: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-19-false-positive-crash.md](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-19-false-positive-crash.md)
- Pattern #20 — Metric Cardinality Explosion: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-20-metric-cardinality-explosion.md](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-20-metric-cardinality-explosion.md)
- Pattern #12 — Circuit Breaker Misconfiguration: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/05-resilience/pattern-12-circuit-breaker-misconfiguration.md](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/05-resilience/pattern-12-circuit-breaker-misconfiguration.md)
