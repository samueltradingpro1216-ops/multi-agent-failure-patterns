# Pattern #19 — False Positive Crash Detection

| Field | Value |
|---|---|
| **ID** | 19 |
| **Category** | Detection & Monitoring |
| **Severity** | High |
| **Affected Frameworks** | LangChain / CrewAI / AutoGen / LangGraph / Custom |
| **Average Debugging Time (if undetected)** | 1 to 5 days |
| **Keywords** | crash detection, false positive, graceful shutdown, restart counter, watchdog, killswitch, exit code, SIGKILL, scheduled restart |

---

## 1. Observable Symptoms

This pattern is particularly insidious because the monitoring system appears to be functioning correctly: it detected "crashes," escalated appropriately, and took protective action. The system was doing exactly what it was programmed to do. The bug is in the definition of what constitutes a crash.

**Operational symptoms:**

- A healthy agent or service is terminated by its own watchdog without any upstream service degradation preceding the kill.
- Emergency killswitch events appear in the log correlated exactly with scheduled maintenance windows or deployment timestamps.
- Engineers investigating the "crash" find no error logs, no stack traces, no anomalous behaviour — only a clean shutdown record followed by watchdog termination.
- Alert storms fire in clusters at predictable times (daily 3AM restart, weekly deployment window) rather than randomly, which is the signature of a false-positive counter accumulating against scheduled events.
- Crash counter metrics in dashboards show a monotonically increasing value that never resets, even after clean deployments.
- Customer-facing systems (chatbots, support agents) go offline during business hours due to watchdog kills that were triggered by overnight maintenance cycles.
- The restart counter for a process deployed three months ago reads "90" — one per day, every day, all graceful, all misclassified as crashes.

**Code-level symptoms:**

- A crash counter is incremented inside an `except Exception` block or on any process exit, without checking the exit code.
- A watchdog uses `process.poll() is not None` (process has exited) as the sole criterion for "crash" without distinguishing exit code 0 from non-zero.
- No "graceful shutdown in progress" flag is set before intentional restarts; the watchdog observes an abrupt process disappearance and increments the crash counter.
- The crash threshold is a small integer (3, 5) with no time window — making it equivalent to "crash N times ever" rather than "crash N times in the last hour."

---

## 2. Field Story (Anonymized)

A financial services firm operated a multi-agent chatbot support platform — internally called "Resolve" — deployed across a cluster of eight worker nodes. Each node ran a Python-based LangChain agent process that handled customer queries. A watchdog process on each node monitored agent health and was empowered to trigger a killswitch (terminating all agent processes on the cluster) if it detected a crash loop.

The watchdog logic, simplified, was:

```python
if crash_counter >= CRASH_LOOP_THRESHOLD:
    trigger_killswitch()
```

`CRASH_LOOP_THRESHOLD` was set to 3. The crash counter was persisted to a local file and never reset. The counter was incremented any time the agent process was not running and then became running again — regardless of how it stopped.

The platform had a standard operational procedure: a daily 3AM restart to apply configuration updates and flush long-lived sessions. This restart was graceful: a SIGTERM was sent, the agent finished in-flight requests, and exited with code 0. From the watchdog's perspective, the process disappeared and then reappeared. Counter: +1.

On day one of deployment: counter = 1. Day two: counter = 2. Day three: counter = 3. At 3:04AM on day three, four minutes into business day pre-opening, the watchdog triggered the cluster-wide killswitch. All eight agent nodes were terminated. The chatbot support system went offline.

The on-call engineer was paged at 3:07AM. The incident log showed "CRASH_LOOP_DETECTED after 3 crashes." The engineer spent 90 minutes reviewing agent logs, network traces, and dependency health before determining that all three "crashes" were the scheduled 3AM maintenance restart. The fix was a one-line change. The diagnosis took 90 minutes because the watchdog log message said "crash" with complete confidence.

The post-mortem identified two contributing failures: (1) the counter incremented on graceful restarts, and (2) the counter had no time window — it was a lifetime total, not a rate. A process that had been running flawlessly for three months could still trigger the killswitch on day three of a new nightly maintenance schedule.

---

## 3. Technical Root Cause

**The fundamental conflation: exit vs. crash.**

A process can stop running for several reasons, only some of which indicate failure:

| Reason | Exit Code | Signal | Is a Crash? |
|---|---|---|---|
| Normal completion | 0 | — | No |
| Graceful shutdown (SIGTERM handled) | 0 | SIGTERM | No |
| Scheduled restart by orchestrator | 0 | SIGTERM | No |
| Config reload (restart-in-place) | 0 | — | No |
| Deployment rollout | 0 | SIGTERM | No |
| Out-of-memory kill | 137 | SIGKILL | Yes |
| Unhandled exception | 1 | — | Yes |
| Segmentation fault | 139 | SIGSEGV | Yes |
| Killed by watchdog | 137 | SIGKILL | Depends |

A watchdog that defines "crash" as "process is no longer running" conflates all rows in this table. The correct definition of a crash is: **an unplanned, non-zero exit or a kill by an external signal that was not part of a coordinated shutdown protocol.**

**Why counters without time windows are dangerous.**

A counter that measures "number of crashes ever" is not an operational metric — it is a historical audit log being misused as a rate sensor. The correct crash-loop signal is: "N crashes within the last T minutes." A process that crashes once a day for three days is not in a crash loop. A process that crashes three times in 90 seconds is.

**Framework-specific aggravation:**

In LangGraph and CrewAI, agent nodes are frequently restarted as part of normal operation: task checkpointing, graph node reallocation, and worker pool scaling all cause process lifecycle events. A watchdog that counts every such event as a crash will fire within hours of a new deployment.

AutoGen's `ConversableAgent` supports restart-on-failure semantics that are designed to be transparent to the caller. If a watchdog sitting above AutoGen's restart mechanism double-counts restarts, the watchdog's counter will grow at double the actual rate.

**The alerting paradox:**

Because the watchdog is trusted as a safety system, its output is rarely questioned. When it says "CRASH_LOOP," engineers investigate for a crash. They will not find one, but the absence of evidence is hard to interpret quickly under incident pressure. The false-positive watchdog consumes significant incident response capacity on non-issues, degrading the organisation's ability to respond to real incidents.

---

## 4. Detection

### 4.1 Manual Code Audit

Search for crash counters that increment without checking exit code or graceful-shutdown flags.

**Grep pattern — crash counter increments without exit code check:**

```bash
grep -rn "crash_count\|crash_counter\|restart_count\|failure_count" \
  --include="*.py" -A 3 | grep -v "exit_code\|returncode\|graceful\|scheduled"
```

**Grep pattern — watchdog process monitoring without exit code gate:**

```bash
grep -rn "poll()\|returncode\|wait()" --include="*.py" -B 2 -A 5 | \
  grep -B 5 "crash\|kill\|alert\|counter" | grep -v "returncode != 0\|exit_code"
```

**Grep pattern — threshold comparison without time window:**

```bash
grep -rn "crash.*>=\|crash.*>\|counter.*>=\|counter.*>" --include="*.py" | \
  grep -v "within\|window\|minutes\|seconds\|since\|recent"
```

**Manual review checklist:**

- Locate all places where a crash/restart counter is incremented. For each: (a) is the exit code checked? (b) is there a graceful-shutdown flag check? (c) is there a time window on the counter?
- Locate the threshold comparison. Is there a `for:` equivalent — a minimum duration the threshold must be exceeded?
- Locate the graceful shutdown handler (SIGTERM handler or orchestrator shutdown hook). Does it set a flag that the watchdog will read before incrementing the counter?

### 4.2 Automated CI/CD

```python
# ci_checks/check_false_positive_crash.py
"""
CI gate: fails if any watchdog or health-monitor file increments a
crash/restart counter without an accompanying exit-code or
graceful-shutdown check.
"""
import ast
import sys
from pathlib import Path

COUNTER_KEYWORDS = {"crash_count", "crash_counter", "restart_count", "failure_count"}
GUARD_KEYWORDS = {
    "exit_code", "returncode", "graceful", "scheduled",
    "sigterm", "clean_exit", "planned_restart",
}
WATCHDOG_FILENAMES = {"watchdog", "monitor", "health", "supervisor", "guardian"}


def is_watchdog_file(path: Path) -> bool:
    return any(kw in path.stem.lower() for kw in WATCHDOG_FILENAMES)


def check_file(path: Path) -> list[str]:
    violations = []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_src = ast.get_source_segment(source, node) or ""
        has_counter = any(k in func_src for k in COUNTER_KEYWORDS)
        has_guard = any(k in func_src.lower() for k in GUARD_KEYWORDS)
        if has_counter and not has_guard:
            violations.append(
                f"{path}:{node.lineno}: function '{node.name}' increments a "
                f"crash/restart counter without an exit-code or "
                f"graceful-shutdown guard."
            )
    return violations


def main(target_dirs: list[str]) -> int:
    all_violations = []
    for d in target_dirs:
        for py_file in Path(d).rglob("*.py"):
            if is_watchdog_file(py_file):
                all_violations.extend(check_file(py_file))
    if all_violations:
        print("FALSE POSITIVE CRASH DETECTION VIOLATIONS:")
        for v in all_violations:
            print(f"  {v}")
        return 1
    print("No false-positive crash detection violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["."]))
```

```yaml
# .github/workflows/ci.yml (excerpt)
- name: Check for false-positive crash detection patterns
  run: python ci_checks/check_false_positive_crash.py src/ agents/ watchdogs/
```

### 4.3 Runtime Production

Instrument the watchdog to emit a structured log entry for every counter increment, including the exit code and whether a graceful-shutdown flag was observed. A log analytics query that finds increments where `exit_code == 0` immediately reveals false positives.

```python
import logging
import json

logger = logging.getLogger("watchdog.audit")

def log_restart_event(
    process_name: str,
    exit_code: int,
    graceful: bool,
    counter_before: int,
    counter_after: int,
    increment_reason: str,
) -> None:
    """
    Emit a structured log line for every crash/restart counter event.
    Query in log analytics: exit_code == 0 AND incremented == true
    to find false-positive crash counts.
    """
    event = {
        "event": "restart_counter_update",
        "process": process_name,
        "exit_code": exit_code,
        "graceful": graceful,
        "counter_before": counter_before,
        "counter_after": counter_after,
        "increment_reason": increment_reason,
        "is_false_positive_candidate": (exit_code == 0 or graceful),
    }
    if event["is_false_positive_candidate"] and counter_after > counter_before:
        logger.warning("POTENTIAL_FALSE_POSITIVE_CRASH_COUNT: %s", json.dumps(event))
    else:
        logger.info("restart_event: %s", json.dumps(event))
```

Add a daily alert: if `is_false_positive_candidate == true` appears more than twice in a 24-hour window, page the on-call engineer to review the watchdog configuration — not to investigate a crash.

---

## 5. Fix

### 5.1 Immediate Fix

Add an exit code check before incrementing the crash counter. This is a one-line change that eliminates the most common false-positive source immediately.

```python
# BEFORE (counts all restarts as crashes — DO NOT USE)
import subprocess
import time

crash_counter = 0
THRESHOLD = 3

def monitor(cmd: list[str]) -> None:
    global crash_counter
    while True:
        proc = subprocess.Popen(cmd)
        proc.wait()
        # BUG: increments on exit code 0 (graceful shutdown) too.
        crash_counter += 1
        if crash_counter >= THRESHOLD:
            trigger_killswitch()
        time.sleep(5)

# AFTER (immediate fix — only count non-zero exits as crashes)
import subprocess
import time

crash_counter = 0
THRESHOLD = 3

def monitor(cmd: list[str]) -> None:
    global crash_counter
    while True:
        proc = subprocess.Popen(cmd)
        proc.wait()
        if proc.returncode != 0:
            # Only non-zero exit codes are potential crashes.
            crash_counter += 1
            if crash_counter >= THRESHOLD:
                trigger_killswitch()
        else:
            # Graceful exit: optionally reset counter to reflect
            # that the process last stopped cleanly.
            crash_counter = 0
        time.sleep(5)
```

### 5.2 Robust Fix

A production-grade watchdog must handle: graceful shutdown coordination (SIGTERM flag), exit code classification, time-windowed crash rate, and a minimum crash-rate duration before triggering destructive actions.

```python
"""
robust_watchdog.py

Production-grade process watchdog with:
- Graceful shutdown coordination via a shared flag file.
- Exit code classification (crash vs. clean exit vs. OOM).
- Time-windowed crash counter (crashes per hour, not crashes ever).
- Minimum sustained crash rate before killswitch activation.
- Structured logging for post-incident audit.

Suitable for use in LangChain, CrewAI, AutoGen, LangGraph,
or any custom Python agent framework.
"""
import os
import signal
import subprocess
import time
import logging
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Exit code classification
GRACEFUL_EXIT_CODES = {0}
OOM_EXIT_CODE = 137          # SIGKILL, commonly from OOM killer
CRASH_EXIT_CODES = set(range(1, 256)) - GRACEFUL_EXIT_CODES


def classify_exit(returncode: int) -> str:
    if returncode in GRACEFUL_EXIT_CODES:
        return "graceful"
    if returncode == OOM_EXIT_CODE:
        return "oom_kill"
    if returncode < 0:
        # Negative returncode in Python = killed by signal (-N = signal N).
        return f"signal_kill:{-returncode}"
    return "crash"


@dataclass
class WatchdogConfig:
    # Path to the managed process command.
    command: list[str]
    # Name for logging.
    process_name: str = "agent"
    # Path to the graceful-shutdown flag file. The managed process
    # must create this file before exiting cleanly (e.g., on SIGTERM).
    graceful_flag_path: str = "/tmp/agent_graceful_shutdown.flag"
    # How many crashes in the time window before alerting.
    crash_threshold: int = 3
    # Time window in seconds for the crash counter.
    crash_window_seconds: float = 3600.0   # 1 hour
    # Seconds to wait between restart attempts.
    restart_delay: float = 5.0
    # If True, trigger_killswitch() is called when threshold is reached.
    enable_killswitch: bool = True


class RobustWatchdog:
    """
    Monitors a subprocess and distinguishes graceful shutdowns from
    crashes using exit code classification and a graceful-shutdown
    flag file written by the managed process.

    Crash counting uses a sliding time window so that a process that
    has been running cleanly for an hour is not penalised for a
    crash that happened yesterday.
    """

    def __init__(self, config: WatchdogConfig):
        self._cfg = config
        # Deque of crash timestamps within the sliding window.
        self._crash_timestamps: deque[float] = deque()
        self._running = True
        self._killswitch_triggered = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main watchdog loop. Blocks until killswitch or stop()."""
        logger.info(
            "[%s] Watchdog starting. crash_threshold=%d crash_window=%ds",
            self._cfg.process_name,
            self._cfg.crash_threshold,
            int(self._cfg.crash_window_seconds),
        )
        while self._running and not self._killswitch_triggered:
            self._clear_graceful_flag()
            proc = self._start_process()
            exit_code = self._wait_for_process(proc)
            classification = self._classify_with_flag(exit_code)
            self._log_exit_event(exit_code, classification)

            if classification == "graceful":
                # Clean exit: reset crash window, no action needed.
                self._crash_timestamps.clear()
                logger.info("[%s] Clean exit. Crash window reset.", self._cfg.process_name)
            else:
                # Actual crash: record timestamp and check rate.
                self._record_crash()
                crash_rate = self._current_crash_count()
                logger.warning(
                    "[%s] Crash detected (classification=%s exit_code=%d). "
                    "Crashes in window: %d/%d",
                    self._cfg.process_name, classification, exit_code,
                    crash_rate, self._cfg.crash_threshold,
                )
                if crash_rate >= self._cfg.crash_threshold:
                    self._handle_crash_loop(crash_rate)
                    break

            if self._running:
                time.sleep(self._cfg.restart_delay)

    def stop(self) -> None:
        """Signal the watchdog loop to exit without triggering killswitch."""
        self._running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_process(self) -> subprocess.Popen:
        logger.info("[%s] Starting process: %s", self._cfg.process_name, self._cfg.command)
        return subprocess.Popen(
            self._cfg.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _wait_for_process(self, proc: subprocess.Popen) -> int:
        proc.wait()
        return proc.returncode

    def _classify_with_flag(self, exit_code: int) -> str:
        """
        Combine exit code classification with graceful-flag check.

        The graceful flag is written by the managed process's SIGTERM
        handler. Even if the exit code is non-zero (e.g., due to a
        mid-shutdown error), the presence of the flag indicates a
        coordinated, intentional shutdown.
        """
        flag_present = Path(self._cfg.graceful_flag_path).exists()
        code_classification = classify_exit(exit_code)

        if flag_present:
            # Managed process signalled graceful intent.
            return "graceful"
        return code_classification

    def _clear_graceful_flag(self) -> None:
        try:
            Path(self._cfg.graceful_flag_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not clear graceful flag: %s", exc)

    def _record_crash(self) -> None:
        now = time.monotonic()
        self._crash_timestamps.append(now)
        # Evict timestamps outside the window.
        cutoff = now - self._cfg.crash_window_seconds
        while self._crash_timestamps and self._crash_timestamps[0] < cutoff:
            self._crash_timestamps.popleft()

    def _current_crash_count(self) -> int:
        now = time.monotonic()
        cutoff = now - self._cfg.crash_window_seconds
        return sum(1 for t in self._crash_timestamps if t >= cutoff)

    def _handle_crash_loop(self, crash_count: int) -> None:
        logger.error(
            "[%s] CRASH_LOOP_DETECTED: %d crashes in %ds window. "
            "Triggering killswitch=%s.",
            self._cfg.process_name, crash_count,
            int(self._cfg.crash_window_seconds),
            self._cfg.enable_killswitch,
        )
        self._killswitch_triggered = True
        if self._cfg.enable_killswitch:
            trigger_killswitch(self._cfg.process_name)

    def _log_exit_event(self, exit_code: int, classification: str) -> None:
        event = {
            "event": "process_exit",
            "process": self._cfg.process_name,
            "exit_code": exit_code,
            "classification": classification,
            "graceful_flag": Path(self._cfg.graceful_flag_path).exists(),
            "crash_window_count": self._current_crash_count(),
        }
        logger.info("process_exit_event: %s", json.dumps(event))


def trigger_killswitch(process_name: str) -> None:
    """
    Stub for the cluster-wide killswitch. Replace with your actual
    implementation (e.g., Kubernetes API call, PagerDuty alert, etc.).
    In production, this should also notify the on-call engineer with
    a structured alert that includes the crash classification and timeline.
    """
    logger.critical("[%s] KILLSWITCH ACTIVATED", process_name)


# ------------------------------------------------------------------
# Managed process side: SIGTERM handler that writes the graceful flag
# ------------------------------------------------------------------

GRACEFUL_FLAG_PATH = "/tmp/agent_graceful_shutdown.flag"

def install_graceful_shutdown_handler(flag_path: str = GRACEFUL_FLAG_PATH) -> None:
    """
    Install in the managed agent process (not the watchdog).
    On SIGTERM, write the graceful-shutdown flag before exiting.

    Usage (in your agent's main.py):
        install_graceful_shutdown_handler()
    """
    def _handler(signum, frame):
        logger.info("SIGTERM received. Writing graceful shutdown flag.")
        try:
            Path(flag_path).write_text("graceful", encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to write graceful flag: %s", exc)
        # Re-raise to allow the default SIGTERM behaviour (process exit).
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGTERM, _handler)
    logger.debug("Graceful shutdown handler installed (flag: %s)", flag_path)


# ------------------------------------------------------------------
# Example: LangChain agent main entry point with graceful handler
# ------------------------------------------------------------------

def agent_main() -> None:
    """
    Entry point for the managed LangChain/CrewAI agent process.
    Install the graceful shutdown handler at startup so the watchdog
    can distinguish scheduled restarts from genuine crashes.
    """
    install_graceful_shutdown_handler()
    # ... agent initialisation and run loop ...
    logger.info("Agent started. Graceful shutdown handler active.")


# ------------------------------------------------------------------
# Example: launching the watchdog
# ------------------------------------------------------------------

def launch_watchdog() -> None:
    config = WatchdogConfig(
        command=["python", "agent_main.py"],
        process_name="support_chatbot_agent",
        graceful_flag_path="/tmp/agent_graceful_shutdown.flag",
        crash_threshold=3,          # 3 crashes ...
        crash_window_seconds=3600,  # ... within 1 hour = crash loop
        restart_delay=5.0,
        enable_killswitch=True,
    )
    watchdog = RobustWatchdog(config)
    watchdog.run()
```

---

## 6. Architectural Prevention

**Principle: health monitors must be explicitly aware of the operational lifecycle of the processes they monitor.**

1. **Define and document the process lifecycle.** Before writing a watchdog, enumerate all valid process exit scenarios: scheduled restart, deployment rollout, config reload, on-demand maintenance, graceful shutdown on idle timeout, and genuine crash. Each scenario must have a distinct classification path in the watchdog.

2. **Use a shutdown coordination protocol.** The managed process must signal its intent before exiting. The two recommended mechanisms are:
   - **Flag file:** managed process writes a flag file in its SIGTERM handler; watchdog checks for the file before classifying the exit.
   - **PID file with state:** managed process updates its PID file to include a `status: "shutting_down"` field before exit; watchdog reads the field.
   - **Shared in-memory state (same host):** managed process sets a shared memory flag (e.g., `multiprocessing.Value`) before exiting; watchdog reads the flag.

3. **Time-window all crash counters.** No crash counter should ever be a lifetime total. The correct metric is "crash rate per unit time." Implement using a ring buffer of crash timestamps and query it as "number of events in the last T seconds."

4. **Separate crash-loop detection from crash notification.** A single crash should page the on-call engineer for awareness. A crash loop (N crashes in T minutes) should trigger protective action. These are two distinct alert levels with distinct thresholds and actions.

5. **Integrate with the deployment pipeline.** The CI/CD system should notify the watchdog before initiating a restart: call a watchdog API endpoint to record `{"event": "planned_restart", "initiator": "deployment", "timestamp": "..."}`. The watchdog uses this record to suppress crash counting for the expected restart.

6. **Test the graceful shutdown path in CI.** Add an integration test that sends SIGTERM to the agent process and asserts: (a) the graceful flag is created, (b) the watchdog does not increment the crash counter, (c) the crash counter remains at zero after restart.

```python
# tests/test_robust_watchdog.py
import os
import signal
import subprocess
import time
import tempfile
from pathlib import Path
import pytest
from robust_watchdog import RobustWatchdog, WatchdogConfig, classify_exit


def test_graceful_exit_does_not_increment_crash_counter():
    """Exit code 0 with graceful flag must not count as a crash."""
    with tempfile.NamedTemporaryFile(suffix=".flag", delete=False) as f:
        flag_path = f.name
    # Pre-write the graceful flag to simulate the managed process doing so.
    Path(flag_path).write_text("graceful")

    config = WatchdogConfig(
        command=["python", "-c", "import sys; sys.exit(0)"],
        process_name="test_agent",
        graceful_flag_path=flag_path,
        crash_threshold=3,
        crash_window_seconds=3600,
        restart_delay=0.1,
        enable_killswitch=False,
    )
    watchdog = RobustWatchdog(config)
    # Run one cycle by patching the loop to stop after first restart.
    watchdog._running = False  # Stop after first process exit.
    watchdog.run()
    assert watchdog._current_crash_count() == 0, (
        "Graceful exit must not increment the crash counter"
    )
    os.unlink(flag_path)


def test_crash_exit_increments_counter():
    """Exit code 1 without graceful flag must count as a crash."""
    with tempfile.NamedTemporaryFile(suffix=".flag", delete=False) as f:
        flag_path = f.name
    Path(flag_path).unlink()  # Ensure flag does not exist.

    config = WatchdogConfig(
        command=["python", "-c", "import sys; sys.exit(1)"],
        process_name="test_agent",
        graceful_flag_path=flag_path,
        crash_threshold=10,       # High threshold so killswitch is not triggered.
        crash_window_seconds=3600,
        restart_delay=0.1,
        enable_killswitch=False,
    )
    watchdog = RobustWatchdog(config)
    watchdog._running = False
    watchdog.run()
    assert watchdog._current_crash_count() == 1, (
        "Non-zero exit without graceful flag must count as a crash"
    )


def test_crash_rate_window_excludes_old_crashes():
    """Crashes outside the time window must not contribute to the count."""
    config = WatchdogConfig(
        command=["python", "-c", "pass"],
        process_name="test_agent",
        graceful_flag_path="/tmp/nonexistent_flag",
        crash_threshold=3,
        crash_window_seconds=1.0,   # 1-second window for fast test.
        restart_delay=0.0,
        enable_killswitch=False,
    )
    watchdog = RobustWatchdog(config)
    # Manually inject an old crash timestamp.
    import time
    watchdog._crash_timestamps.append(time.monotonic() - 10.0)  # 10 seconds ago.
    assert watchdog._current_crash_count() == 0, (
        "Crash outside the time window must not count toward threshold"
    )


def test_classify_exit_codes():
    assert classify_exit(0) == "graceful"
    assert classify_exit(1) == "crash"
    assert classify_exit(137) == "oom_kill"
    assert classify_exit(-15) == "signal_kill:15"
```

---

## 7. Anti-patterns to Avoid

**Anti-pattern 1: Crash counter as a lifetime total.**
A counter that never resets is not an operational signal — it is an audit log. After enough time, any healthy process will accumulate enough entries to trigger any finite threshold. Always use a time-windowed count (crashes per hour, crashes per day).

**Anti-pattern 2: Incrementing the counter on any process absence.**
A process that is absent (not running) can be absent because it finished, was updated, was paused, or was temporarily removed from the service mesh. "Not running" is not the same as "crashed." Always check why the process stopped before classifying the exit.

**Anti-pattern 3: Setting the crash threshold below the expected restart frequency.**
If a process restarts daily for maintenance and the crash threshold is 3, the threshold will be reached in three days. Always set the threshold to be substantially higher than the maximum expected graceful restart rate within the crash window. Example: if the process restarts once per day (once per 24 hours) and the crash window is 1 hour, the maximum graceful restarts in the window is effectively 0 (only one restart every 24 hours). If the crash window is 24 hours, the threshold must be > 1.

**Anti-pattern 4: No graceful shutdown handler in the managed process.**
A managed process that does not write a graceful flag (or use an equivalent coordination mechanism) forces the watchdog to rely solely on exit code. Some frameworks (LangGraph, AutoGen) catch exceptions internally and may exit with code 0 even after a genuine failure. The flag mechanism provides an additional, explicit signal.

**Anti-pattern 5: Killswitch without human-in-the-loop confirmation for novel crash patterns.**
Emergency killswitches are appropriate for well-understood crash loops. For the first time a new crash pattern is observed (new deployment, new dependency version), require a human acknowledgement before executing the killswitch. Automated killswitches on untested patterns frequently cause more damage than the original crash.

**Anti-pattern 6: Using process restart count as a proxy for process health.**
Restart count is a lagging indicator of process health. A process can be restarting successfully and frequently but serving requests correctly between restarts (fast-restart pattern). Use request success rate and latency as the primary health signal; use restart count as a secondary supporting signal only.

---

## 8. Edge Cases and Variants

**Variant A: Container orchestrator restart confusion.**
Kubernetes restarts containers on OOM, liveness probe failure, and node eviction. The container's own watchdog process may see these as "crashes" when Kubernetes is actually managing the lifecycle. In containerised deployments, rely on Kubernetes restart policies and liveness/readiness probes rather than an in-process watchdog. If a custom watchdog is required, integrate it with the Kubernetes lifecycle hooks (`preStop`, `postStart`) to receive explicit lifecycle events.

**Variant B: Multi-process agent systems.**
In CrewAI and AutoGen multi-agent deployments, a single logical "agent" may consist of several cooperating processes. A watchdog monitoring one subprocess may see it exit because a sibling process sent it a shutdown signal as part of normal task hand-off. The watchdog must understand the inter-process communication protocol to correctly classify these exits.

**Variant C: Crash during graceful shutdown.**
A managed process may receive SIGTERM, begin its shutdown sequence, and then crash during shutdown (e.g., during final state flush to disk). In this case, the graceful flag may have been written, but the process exited with a non-zero code. The correct classification is "crash-during-shutdown" — not "graceful" and not "clean crash." This variant should increment a separate counter and alert the operations team without triggering the crash-loop killswitch.

**Variant D: Watchdog itself crashes.**
If the watchdog process crashes and restarts, it loses its in-memory crash counter. If the counter is not persisted (or is persisted but the persistence file is corrupted), the watchdog resets to zero — effectively hiding a pre-existing crash loop from the new watchdog instance. Persist crash timestamps to a durable store (database, structured log file) and reload them on watchdog startup with an age filter (discard entries older than the crash window).

**Variant E: Distributed watchdog disagreement.**
In a cluster where multiple watchdog instances monitor the same logical service, each instance may classify the same restart event differently (e.g., one sees the graceful flag, one does not due to a race condition in flag file cleanup). Implement a consensus protocol: require a majority of watchdog instances to agree on the crash classification before incrementing a shared counter.

**Variant F: Scheduled restart via external system with no watchdog notification.**
A CI/CD pipeline or configuration management system restarts the agent process directly (e.g., `systemctl restart agent`) without notifying the watchdog. From the watchdog's perspective, the process disappeared without a graceful flag. Integrate the CI/CD restart procedure with the watchdog's planned-restart API (see Section 6, point 5) to suppress crash counting for known, externally-initiated restarts.

---

## 9. Audit Checklist

Use this checklist during code review of any watchdog or health monitoring component that manages process lifecycle.

- [ ] **Exit code is checked before incrementing crash counter.** Exit code 0 must never increment the counter unless a specific protocol requires it.
- [ ] **Graceful shutdown flag or equivalent coordination mechanism exists.** The managed process writes a flag before exiting on SIGTERM.
- [ ] **Crash counter uses a time window, not a lifetime total.** The counter represents "crashes in the last N minutes/hours," not "crashes since deployment."
- [ ] **Crash threshold is higher than the maximum expected graceful restart rate** within the crash window.
- [ ] **Crash counter resets or discards entries** when a clean exit is observed.
- [ ] **Watchdog restores counter state from durable storage** on startup (to survive watchdog crashes).
- [ ] **Structured log entries are emitted for every counter increment**, including exit code, graceful flag state, and classification.
- [ ] **Log query or dashboard exists** to detect false positives: counter increments where `exit_code == 0 OR graceful_flag == true`.
- [ ] **Integration test exists** that sends SIGTERM, asserts graceful flag is written, and asserts crash counter remains at 0.
- [ ] **Integration test exists** that sends SIGKILL (crash simulation), asserts crash counter increments to 1.
- [ ] **CI/CD restart procedures notify the watchdog** via a planned-restart API or flag before initiating a restart.
- [ ] **Killswitch requires human confirmation** for first occurrence of a new crash pattern.
- [ ] **Crash-during-shutdown variant is handled** as a distinct classification from both clean crash and graceful exit.

---

## 10. Further Reading

**Primary references:**

- Burns, B. et al. *Kubernetes: Up and Running*, 3rd ed. (O'Reilly, 2022). Chapter on liveness and readiness probes — the Kubernetes design explicitly separates "process not running" from "process is unhealthy" from "process is not ready to serve traffic." Directly applicable to custom watchdog design.
- Kubernetes documentation: [Configure Liveness, Readiness and Startup Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/) — liveness probe failures trigger container restart, but the `failureThreshold` and `periodSeconds` parameters implement exactly the windowed, minimum-sustained-failure semantics described in this pattern.
- Nygard, M. T. *Release It!*, 2nd ed. (Pragmatic Bookshelf, 2018). Chapter 4: "Stability Patterns." The Supervisor pattern and Circuit Breaker pattern described there provide the architectural foundation for distinguishing transient failures from sustained instability.
- Linux man page: `signal(7)` — reference for signal numbers, default dispositions, and the distinction between SIGTERM (request graceful shutdown) and SIGKILL (unconditional termination). Understanding signal semantics is prerequisite to implementing a correct graceful-shutdown handler.

**Related patterns in this playbook:**

- Pattern #18 — Snapshot vs Sustained Check: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-18-snapshot-vs-sustained.md](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-18-snapshot-vs-sustained.md)
- Pattern #20 — Metric Cardinality Explosion: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-20-metric-cardinality-explosion.md](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-20-metric-cardinality-explosion.md)
- Pattern #12 — Circuit Breaker Misconfiguration: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/05-resilience/pattern-12-circuit-breaker-misconfiguration.md](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/05-resilience/pattern-12-circuit-breaker-misconfiguration.md)
- Pattern #21 — Watchdog Cascade Failure: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-21-watchdog-cascade-failure.md](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-21-watchdog-cascade-failure.md)
