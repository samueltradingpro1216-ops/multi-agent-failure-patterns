# Pattern #29 — Log Never Rotated

**Category:** I/O & Persistence
**Severity:** Medium
**Frameworks affected:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debug time if undetected:** 7 to 60 days (slow disk fill, discovered only when the disk is full)

---

## 1. Observable Symptoms

The early symptoms are invisible. Disk usage climbs steadily — 3%, 7%, 12%, 19% — but no alarm fires because the system's disk usage alert threshold is set at 85% and nothing else on the host is writing significant volumes. The log file grows by 80–120 MB per day. At day 15, the file is 1.5 GB. No developer notices because log files are not in the standard monitoring dashboard and the agent is running correctly.

The first visible symptom appears weeks or months into operation, and it has nothing to do with logging. A database write fails. A temporary file cannot be created. A subprocess crashes on launch because it cannot write its PID file. An unhandled exception is raised in a component that has never failed before. The error message says something about a disk quota or an I/O failure. Developers investigate that component — not the disk, not the log file.

In a multi-agent system running on a shared host or container, the disk-full condition is shared. When the log file from the agent process consumes the last available gigabytes, every other process on the host that needs to write anything — the database, the message broker, the operating system itself — begins to fail. The root cause is one unbounded log file, but the failure manifests as an apparently unrelated cascade across multiple services.

## 2. Field Story (anonymized)

A team operating an IoT sensor monitoring platform deployed a multi-agent system to process readings from a fleet of environmental sensors. Three agents ran in a continuous cycle: one ingested raw sensor readings every 10 seconds, one validated and normalized the data, and one triggered alerts when readings crossed configured thresholds. Each cycle, every agent emitted DEBUG-level logs: sensor IDs, raw values, normalized values, threshold comparisons.

The platform ran correctly for six weeks. On day 43, the on-call engineer received a page from the alert pipeline: the threshold agent had stopped producing alerts. Investigation showed that the agent process was still running but that its alert delivery calls were failing with an I/O error. The delivery code wrote alert records to a SQLite database before dispatching them. SQLite reported that it could not acquire a write lock. The team spent four hours investigating SQLite configuration, WAL mode settings, and file permissions.

The actual cause was discovered when a senior engineer ran `df -h` on the host and found the data volume at 100% capacity. The agent log file — a single flat file opened in append mode with no size limit — had grown to 18 GB over 43 days. It occupied the entire data volume. Every write-dependent operation on the host had been failing for the same reason; SQLite's error was simply the first one the monitoring system caught. The fix took six minutes: truncate the log file, configure rotation. The post-mortem took two days.

## 3. Technical Root Cause

Python's `logging` module uses a `FileHandler` by default when logging to a file. `FileHandler` opens a file in append mode and writes indefinitely. It has no awareness of file size, disk capacity, time elapsed, or any other rotation trigger. The file grows until the developer explicitly configures rotation or until the disk fills:

```python
# This is what most developers write during initial setup
import logging

logging.basicConfig(
    filename="agent.log",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
```

This configuration is safe for a short-lived script. It is not safe for a long-running agent process. In a multi-agent system where each agent runs in a 10-second cycle and emits several log lines per cycle, this generates approximately:

```
6 calls/minute × 60 minutes × 24 hours = 8,640 log entries/day per agent
3 agents × 8,640 entries × ~150 bytes/entry ≈ 3.9 MB/day

# At DEBUG level with structured payloads (sensor readings, LLM responses):
3 agents × 8,640 entries × ~3,500 bytes/entry ≈ 90 MB/day
```

A 3 GB disk partition fills in 33 days. An 8 GB partition fills in 89 days.

The secondary root cause is that log volume increases non-linearly with system complexity. Each new agent, each new tool call, each LLM response that gets logged at DEBUG level adds to the baseline rate. A system that was "fine" at 50 MB/day during initial deployment may grow to 300 MB/day after three months of feature additions, without any developer noticing the change in write rate.

The tertiary root cause is how disk-full failures surface. When a write call fails because the disk is full, the operating system returns `ENOSPC` (error 28 on Linux). Python's `logging` module itself may fail to write this error to the log file (since the disk is full). SQLite, PostgreSQL, Redis AOF, and most other persistence layers translate `ENOSPC` into a generic I/O error, a lock error, or a timeout depending on the implementation. The original cause — disk full — is often not in the error message that the monitoring system captures.

```
Timeline on a 20 GB data volume, IoT platform scenario:
─────────────────────────────────────────────────────────
Day  0: Deployment. Disk: 2 GB used (10%).
Day 10: Disk: 3 GB used (15%). Log file: 1 GB. No alerts.
Day 20: Disk: 4 GB used (20%). Log file: 2 GB. No alerts.
Day 30: Disk: 5 GB used (25%). Log file: 3 GB. No alerts.
Day 43: Disk: 20 GB used (100%). Log file: 18 GB.
         ─ SQLite writes fail ─► alert pipeline stops ─► page fired.
         ─ Postgres WAL writes fail ─► query timeouts.
         ─ OS temp file creation fails ─► random process crashes.
Root cause: agent.log, line 1 through line 37,843,200.
```

## 4. Detection

### 4.1 Manual code audit

Audit every location where the `logging` module is configured to write to a file. `FileHandler` without rotation is the pattern to find:

```bash
# Find FileHandler usage — baseline audit
grep -rn "FileHandler" --include="*.py" .

# Find logging.basicConfig with a filename argument
grep -rn "basicConfig" --include="*.py" . | grep -i "filename"

# Find open() calls used to manually write logs
grep -rn "open.*\.log\|\.log.*open" --include="*.py" .

# Find RotatingFileHandler and TimedRotatingFileHandler — confirm they ARE present
grep -rn "RotatingFileHandler\|TimedRotatingFileHandler" --include="*.py" .
```

The audit finding is the presence of `FileHandler` or `basicConfig(filename=...)` without a corresponding `RotatingFileHandler` or `TimedRotatingFileHandler` anywhere in the same module. The absence of rotation configuration is the bug.

Also audit configuration files:

```bash
# Find logging configuration files
find . -name "logging.yaml" -o -name "logging.json" -o -name "logging.ini" | xargs grep -l "FileHandler"
```

Any `FileHandler` entry in a logging configuration file that is not of the `RotatingFileHandler` or `TimedRotatingFileHandler` class is a finding.

### 4.2 Automated CI/CD

A custom `ruff` rule or a Pytest audit test can enforce the presence of rotation. The most direct approach is a test that parses logging configuration at import time:

```python
# tests/test_logging_config.py
"""Enforce that all file-based logging handlers use rotation."""
import ast
import pathlib
import pytest

SOURCE_ROOT = pathlib.Path("src")

def _find_file_handler_usages(source: str) -> list[int]:
    """Return line numbers where logging.FileHandler is used directly."""
    tree = ast.parse(source)
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "logging"
                and node.attr == "FileHandler"
            ):
                findings.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == "FileHandler":
            # Direct import usage: from logging import FileHandler
            # Only flag it if it is not in the RotatingFileHandler family
            # Heuristic: if the name is exactly FileHandler, flag it
            findings.append(node.lineno)
    return findings


@pytest.mark.parametrize(
    "py_file",
    [p for p in SOURCE_ROOT.rglob("*.py") if "test" not in p.parts],
)
def test_no_bare_file_handler(py_file: pathlib.Path) -> None:
    """Fail if any source file uses logging.FileHandler without rotation."""
    source = py_file.read_text(encoding="utf-8")
    if "FileHandler" not in source:
        return  # Fast path — no handler references at all

    # Allow RotatingFileHandler and TimedRotatingFileHandler
    if "RotatingFileHandler" in source:
        return

    # FileHandler present but no rotation variant — check for bare usage
    lines = _find_file_handler_usages(source)
    assert not lines, (
        f"{py_file}: logging.FileHandler used at lines {lines} "
        "without rotation. Use RotatingFileHandler or "
        "TimedRotatingFileHandler instead."
    )
```

For YAML-based logging configuration (common in LangChain and AutoGen deployments), add a validation step:

```python
# tests/test_logging_yaml_config.py
import yaml
import pathlib
import pytest

def _collect_handler_classes(config: dict) -> list[str]:
    handlers = config.get("handlers", {})
    return [h.get("class", "") for h in handlers.values()]


@pytest.mark.parametrize(
    "config_file",
    list(pathlib.Path(".").rglob("logging*.yaml")),
)
def test_no_bare_file_handler_in_yaml(config_file: pathlib.Path) -> None:
    config = yaml.safe_load(config_file.read_text())
    handler_classes = _collect_handler_classes(config)
    bare = [c for c in handler_classes if c.endswith("FileHandler")
            and "Rotating" not in c and "Timed" not in c]
    assert not bare, (
        f"{config_file}: non-rotating FileHandler found: {bare}. "
        "Replace with RotatingFileHandler or TimedRotatingFileHandler."
    )
```

### 4.3 Runtime production monitoring

Monitor log file size as a metric and alert before the disk fills. A background thread that periodically checks log file size provides early warning at a fraction of the operational cost of recovering from a disk-full incident:

```python
"""log_monitor.py — Background log size monitor for multi-agent systems."""
import logging
import os
import pathlib
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


class LogSizeMonitor:
    """
    Background thread that monitors log file sizes and triggers
    alerts when files exceed configured thresholds.

    Install once at process startup, before agents are initialized.
    """

    def __init__(
        self,
        log_dir: str | pathlib.Path,
        warn_threshold_mb: float = 500.0,
        critical_threshold_mb: float = 1000.0,
        check_interval_seconds: int = 300,
        alert_callback: Callable[[str, float, str], None] | None = None,
    ) -> None:
        """
        Args:
            log_dir: Directory to monitor for .log files.
            warn_threshold_mb: File size that triggers a warning.
            critical_threshold_mb: File size that triggers a critical alert.
            check_interval_seconds: How often to check (default: every 5 min).
            alert_callback: Called with (filename, size_mb, severity).
                            Defaults to logging output.
        """
        self.log_dir = pathlib.Path(log_dir)
        self.warn_threshold_bytes = int(warn_threshold_mb * 1024 * 1024)
        self.critical_threshold_bytes = int(critical_threshold_mb * 1024 * 1024)
        self.check_interval = check_interval_seconds
        self.alert_callback = alert_callback or self._default_alert
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="LogSizeMonitor",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        logger.info(
            "LogSizeMonitor started: dir=%s, warn=%.0f MB, critical=%.0f MB, "
            "interval=%ds",
            self.log_dir,
            self.warn_threshold_bytes / (1024 * 1024),
            self.critical_threshold_bytes / (1024 * 1024),
            self.check_interval,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop_event.wait(self.check_interval):
            self._check_log_sizes()
            self._check_disk_space()

    def _check_log_sizes(self) -> None:
        for log_file in self.log_dir.glob("**/*.log"):
            try:
                size = log_file.stat().st_size
            except OSError:
                continue

            size_mb = size / (1024 * 1024)
            if size >= self.critical_threshold_bytes:
                self.alert_callback(str(log_file), size_mb, "CRITICAL")
            elif size >= self.warn_threshold_bytes:
                self.alert_callback(str(log_file), size_mb, "WARNING")

    def _check_disk_space(self) -> None:
        try:
            stat = os.statvfs(str(self.log_dir))
        except AttributeError:
            # os.statvfs not available on Windows — use shutil
            import shutil
            total, used, free = shutil.disk_usage(str(self.log_dir))
            pct_used = used / total * 100
        else:
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            pct_used = (total - free) / total * 100

        if pct_used >= 90:
            self.alert_callback(
                str(self.log_dir), pct_used, "CRITICAL"
            )
            logger.critical(
                "DISK SPACE CRITICAL: %s is %.1f%% full. "
                "Immediate action required to prevent system failure.",
                self.log_dir, pct_used,
            )
        elif pct_used >= 75:
            logger.warning(
                "Disk space warning: %s is %.1f%% full.",
                self.log_dir, pct_used,
            )

    @staticmethod
    def _default_alert(path: str, value: float, severity: str) -> None:
        msg = (
            f"[{severity}] Log file {path} is {value:.1f} MB. "
            "Consider increasing rotation frequency or reducing log verbosity."
            if "MB" in str(value)
            else f"[{severity}] Disk at {path} is {value:.1f}% full."
        )
        if severity == "CRITICAL":
            logger.critical(msg)
        else:
            logger.warning(msg)
```

## 5. Fix

### 5.1 Immediate fix

Replace `FileHandler` with `RotatingFileHandler`. This is a one-line change per handler. The rotation is immediate on the next handler instantiation:

```python
# BEFORE (unbounded growth)
import logging

logging.basicConfig(
    filename="agent.log",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# AFTER (bounded: max 100 MB across 5 files = 500 MB ceiling)
import logging
from logging.handlers import RotatingFileHandler

handler = RotatingFileHandler(
    filename="agent.log",
    maxBytes=100 * 1024 * 1024,   # 100 MB per file
    backupCount=5,                  # Keep 5 rotated files
    encoding="utf-8",
)
handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"
))

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(handler)
```

For time-based rotation (preferred in systems where log analysis is time-segmented):

```python
from logging.handlers import TimedRotatingFileHandler

handler = TimedRotatingFileHandler(
    filename="agent.log",
    when="midnight",       # Rotate at midnight
    interval=1,            # Every 1 day
    backupCount=30,        # Keep 30 days of logs
    encoding="utf-8",
    utc=True,              # Use UTC timestamps for rotation
)
```

### 5.2 Robust fix

A centralized logging factory that configures rotation, compression of old logs, and a structured JSON format for log analysis — applied uniformly to all agents in the system:

```python
"""logging_setup.py — Centralized logging configuration for multi-agent systems."""
import gzip
import logging
import os
import pathlib
import shutil
from logging.handlers import RotatingFileHandler
from typing import Any


# --- Compression hook ---

def _compress_rotated_log(source: str, dest: str) -> None:
    """
    Compress rotated log files to .gz to reduce disk consumption.
    Called automatically by RotatingFileHandler after each rotation.
    """
    with open(source, "rb") as f_in:
        with gzip.open(dest + ".gz", "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    os.remove(source)


# --- Structured JSON formatter ---

class JsonFormatter(logging.Formatter):
    """Emit log records as JSON lines for downstream analysis."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        import traceback

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "agent": getattr(record, "agent", "unknown"),
        }

        if record.exc_info:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info))

        return json.dumps(payload, default=str)


# --- Public API ---

def configure_agent_logging(
    log_dir: str | pathlib.Path = "./logs",
    agent_name: str = "agent",
    log_level: int = logging.INFO,
    max_bytes_per_file: int = 50 * 1024 * 1024,   # 50 MB
    backup_count: int = 10,                          # 10 files = 500 MB max
    enable_console: bool = True,
    enable_compression: bool = True,
) -> logging.Logger:
    """
    Configure and return a logger for a named agent.

    All agents share the same log directory. Each agent writes to its
    own file: logs/agent-orchestrator.log, logs/agent-executor.log, etc.
    Rotation is applied independently per file.

    Args:
        log_dir: Directory for log files. Created if absent.
        agent_name: Short identifier used in the filename and log records.
        log_level: Minimum level to emit (default: INFO for production).
        max_bytes_per_file: Maximum log file size before rotation.
        backup_count: Number of rotated files to retain.
        enable_console: Also emit logs to stdout.
        enable_compression: Compress rotated files with gzip.

    Returns:
        A configured logging.Logger instance.
    """
    log_path = pathlib.Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    log_file = log_path / f"agent-{agent_name}.log"

    # --- File handler with rotation ---
    file_handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=max_bytes_per_file,
        backupCount=backup_count,
        encoding="utf-8",
        delay=False,   # Open file immediately — fail fast if path is invalid
    )
    file_handler.setFormatter(JsonFormatter())

    if enable_compression:
        file_handler.rotator = _compress_rotated_log

    # --- Optional console handler ---
    handlers: list[logging.Handler] = [file_handler]
    if enable_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
        ))
        handlers.append(console_handler)

    # --- Configure the named logger ---
    logger = logging.getLogger(f"agent.{agent_name}")
    logger.setLevel(log_level)
    logger.handlers.clear()   # Avoid duplicate handlers on re-initialization
    for handler in handlers:
        logger.addHandler(handler)

    logger.propagate = False  # Do not pass to root logger

    logger.info(
        "Logging configured: file=%s, max_size=%d MB, backup_count=%d, "
        "compression=%s",
        log_file,
        max_bytes_per_file // (1024 * 1024),
        backup_count,
        enable_compression,
    )
    return logger


# --- Usage example ---

if __name__ == "__main__":
    from log_monitor import LogSizeMonitor

    # Configure all agents
    orchestrator_log = configure_agent_logging(
        log_dir="./logs",
        agent_name="orchestrator",
        log_level=logging.INFO,
    )
    executor_log = configure_agent_logging(
        log_dir="./logs",
        agent_name="executor",
        log_level=logging.INFO,
    )

    # Start size monitor
    monitor = LogSizeMonitor(
        log_dir="./logs",
        warn_threshold_mb=200.0,
        critical_threshold_mb=400.0,
        check_interval_seconds=300,
    )
    monitor.start()

    # Simulate agent activity
    for i in range(5):
        orchestrator_log.info("Processing sensor batch %d", i)
        executor_log.info("Executing validation for batch %d", i)
```

## 6. Architectural Prevention

The architectural prevention for this pattern is to treat disk space as a constrained resource with a known, bounded allocation — not as a background concern. Every long-running component that writes to disk must have a configured write budget: a maximum file size, a maximum number of retained files, and a total ceiling for the directory.

In a containerized deployment, this budget has a natural enforcement point: container storage limits. Setting a volume claim of 2 GB for the log volume in the container orchestration configuration creates a hard ceiling. However, this does not solve the problem on its own — it changes a slow disk-fill failure into a faster one. The correct approach is to set the rotation configuration so that total log volume stays well below the volume limit, and to alert when the log directory approaches 70% of the volume.

For multi-agent systems with variable log verbosity — systems where DEBUG logging is enabled during incident investigation but INFO is used during normal operation — log level should be a runtime-configurable parameter, not a constant. A log level that is set to DEBUG in the source code and never changed to INFO in production is a common driver of unexpectedly high log volume. Configuration via environment variable (`LOG_LEVEL=INFO`) allows operators to control verbosity without redeployment.

Finally, log shipping to a centralized log aggregation service (a managed service, an ELK stack, or equivalent) removes the disk consumption problem from the application host entirely. Agents write to stdout/stderr, a log collector forwards to the aggregation service, and disk usage on the application host becomes negligible. This is the architectural approach that eliminates the problem at its root rather than managing its symptoms.

## 7. Anti-patterns to Avoid

1. **`logging.basicConfig(filename="agent.log")`** as the sole logging configuration in a long-running agent. This is a one-way door to unbounded log growth. `basicConfig` with a filename argument always creates a bare `FileHandler`.

2. **Setting log level to DEBUG permanently in production.** DEBUG output from a 10-second-cycle agent generates 5–20x more volume than INFO. DEBUG should be a temporary diagnostic tool, not a default.

3. **`backup_count=0` in `RotatingFileHandler`.** This is a valid argument that means "keep no backup files — delete on rotation." It bounds the file size but provides no log history for debugging. Set `backup_count` to a non-zero value.

4. **Assuming that log rotation configured in `supervisord` or `systemd` is sufficient.** These tools rotate the active log file by renaming it and sending `SIGHUP` to the process. Python's `logging` module may continue writing to the renamed file descriptor unless it handles `SIGHUP` explicitly. Use `WatchedFileHandler` or Python's built-in rotation instead.

5. **Not monitoring the log directory separately from overall disk usage.** A general disk usage alert at 85% fires too late. By the time the overall disk reaches 85%, the log file may already be the dominant consumer and the margin to respond is narrow.

## 8. Edge Cases and Variants

**Variant 1: Multiple agents writing to the same log file.** When several agents share one log file path, rotation interactions become unpredictable. Agent A rotates the file, Agent B still holds the old file descriptor open. Log records from Agent B continue to go to the renamed backup file. The active `agent.log` grows slowly, but the backup files grow faster than expected. Use one log file per agent.

**Variant 2: Log file on an NFS or network-mounted volume.** Network volume write latency spikes can cause `RotatingFileHandler` to block agent threads during rotation. In high-throughput agents, this is a latency hazard. Use `QueueHandler` with a background thread to decouple logging from agent execution.

**Variant 3: LangChain verbose mode combined with `FileHandler`.** LangChain's `verbose=True` flag sends the full text of every prompt and response to the Python logger. LLM responses can be 2,000–8,000 tokens. At 10-second cycles, this generates 50–200 MB/day of log data from LLM output alone — before any application-level logging. Always disable `verbose=True` in production configurations and audit `LANGCHAIN_VERBOSE` environment variable settings.

**Variant 4: Crash loop with log-before-crash.** An agent that crashes and restarts every few minutes may emit a burst of error logs immediately before each crash. If the agent is managed by a supervisor that restarts it immediately, the log write rate is determined by the crash frequency, not the normal cycle frequency. A crash loop in a verbose agent can fill a disk in hours.

## 9. Audit Checklist

- [ ] No `logging.FileHandler` or `logging.basicConfig(filename=...)` without rotation appears in any production module
- [ ] All `RotatingFileHandler` instances have `maxBytes > 0` and `backupCount > 0`
- [ ] All `TimedRotatingFileHandler` instances have `backupCount > 0`
- [ ] Total maximum log volume (maxBytes × backupCount, summed across all agents) is less than 70% of the available disk partition
- [ ] Log level in production is INFO or higher (DEBUG is not permanently enabled)
- [ ] A log size monitor or disk usage alert is configured with a threshold below 80% disk usage
- [ ] `LANGCHAIN_VERBOSE` and `verbose=True` flags are explicitly set to `False` in production configuration
- [ ] Log directory is separate from the data directory (log growth cannot directly cause database write failures)

## 10. Further Reading

- Corresponding short pattern: [Pattern 29 — Log Never Rotated](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-29)
- Related patterns: #08 (Data Pipeline Freeze — disk-full conditions can freeze pipeline writes for the same underlying reason), #14 (Stale Lock File — lock files accumulate in the same directories as log files and compound disk usage)
- Recommended reading:
  - Python documentation — `logging.handlers.RotatingFileHandler` and `TimedRotatingFileHandler` (official reference for all rotation parameters including `namer` and `rotator` hooks)
  - "The Art of Monitoring" by James Turnbull, Chapter 5: "Logging" — covers log shipping architectures that eliminate local disk consumption as a failure mode
