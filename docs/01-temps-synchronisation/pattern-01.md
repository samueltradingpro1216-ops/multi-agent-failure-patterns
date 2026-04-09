# Pattern #01 — Timezone Mismatch

**Category:** Time & Synchronization
**Severity:** Critical
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 2 to 10 days (often discovered by accident during an audit, not from an immediate symptom)

---

## 1. Observable Symptoms

The system acts at the **wrong hours**. A time filter meant to activate agents between 12:00 and 18:00 UTC actually activates them between 09:00 and 15:00 UTC — a 3-hour offset that nobody notices because the system appears to work normally.

The logs are misleading: the filter works, it blocks and allows requests. But the hours are shifted. The system misses its optimal activity window (e.g., peak user traffic, batch processing window, or the time slot when partner APIs are available) and acts during off-peak hours.

The most insidious symptom: **the bug is invisible in local testing** if the developer's machine is in UTC. The offset only appears in production, on a server whose system timezone is different (UTC+2 on a European VPS, UTC+8 on an Asian cloud provider, or the time of the third-party service being connected to). Tests pass, code review passes, and the bug lives in production for weeks until an audit reveals it.

Another common symptom: **timestamps between components don't match**. Agent A logs an event at "14:32:05", Agent B logs the same event at "17:32:07". The delta is close to a multiple of one hour (3h), not a normal network drift (< 1s). This signals a timezone mismatch, not a latency problem.

## 2. Field Story (anonymized)

A multi-agent production system used a time filter to limit agent activity to the 12:00–18:00 UTC window — the period when real-time data was most reliable and most voluminous. The filter had been running for 3 weeks without any alert.

During a performance audit, the team noticed the system was abnormally active between 09:00 and 15:00 UTC and silent between 15:00 and 18:00 UTC. Investigating, they discovered the executor component used `TimeCurrent()` (the local equivalent of `datetime.now()`) instead of `TimeGMT()` to evaluate the filter. The server was in UTC+3 (Eastern European data center), so the "12–18" bounds were evaluated in local time (12–18 server time = 09:00–15:00 UTC). The system was missing the 3 best hours of the day and operating during the 3 least reliable ones.

The bug was invisible because agents produced acceptable results even on the wrong window — just ~15–20% worse. It took a line-by-line audit to find it.

## 3. Technical Root Cause

The bug arises when a component uses the **server's local time** instead of explicit UTC for its time calculations. In a single-server system, this may work. In a multi-agent system, each component can run in a different timezone context:

- The Python orchestrator uses `datetime.now()` (VPS local time)
- The executor uses the time from its third-party service (API, database, cloud provider)
- Cron jobs use their own configured timezone
- Each component's logs are in its own process timezone

The typical buggy code:

```python
# BUG: datetime.now() returns local time, not UTC
current_hour = datetime.now().hour

# This filter is supposed to block outside 12–18 UTC
# But on a UTC+3 server, it blocks outside 12–18 LOCAL = 09:00–15:00 UTC
if not (12 <= current_hour < 18):
    return "BLOCKED — outside active window"
```

The fundamental problem is the **implicit timezone assumption**. The developer writes `datetime.now().hour` thinking "UTC time" because their dev machine is in UTC. In production, `datetime.now()` returns the server time, which could be anything.

The mismatch propagates throughout the system:

```
Component A (UTC)      : "It's 14:00, window active"
Component B (UTC+3)    : "It's 17:00, window active"
Component C (UTC-5)    : "It's 09:00, outside window"
                          -> 3 components, 3 different times, 3 different decisions
```

Variants of the same bug include:
- `time.time()` returning a POSIX timestamp (always UTC, correct) but converted to local time via `time.localtime()` instead of `time.gmtime()`
- `datetime.utcnow()` returning a **timezone-naive** object (no timezone info attached), making it impossible to compare reliably with a timezone-aware datetime
- Timestamps stored as "local time + Z suffix": `datetime.now().isoformat() + "Z"` produces a timestamp that **claims** to be UTC but is actually local time

## 4. Detection

### 4.1 Manual code audit

Search for all time sources in the codebase:

```bash
# Find datetime.now() calls without timezone
grep -rn "datetime\.now()" --include="*.py" | grep -v "timezone"

# Find deprecated utcnow() (Python 3.12+)
grep -rn "\.utcnow()" --include="*.py"

# Find potentially dangerous localtime conversions
grep -rn "localtime\|mktime\|strftime" --include="*.py"

# Find manually appended "Z" suffixes (fake UTC)
grep -rn '+ "Z"\|+"Z"\|+ "z"' --include="*.py"
```

Each occurrence found is suspect. Verify whether the execution context (server, container, cron) has the same timezone the code expects.

### 4.2 Automated CI/CD

Configure `ruff` to forbid `datetime.now()` calls without timezone:

```toml
# ruff.toml or pyproject.toml
[tool.ruff.lint]
select = ["DTZ"]  # datetime-timezone rules
# DTZ001: datetime.now() without tz
# DTZ002: datetime.utcnow() (deprecated)
# DTZ003: datetime.now(timezone.utc) preferred
# DTZ005: datetime.now().isoformat() without tz
```

Add a custom test that fails if any module uses `datetime.now()` without an argument:

```python
import ast
import pathlib

def test_no_naive_datetime_now():
    """Fail if any Python file calls datetime.now() without a timezone argument."""
    violations = []
    for py_file in pathlib.Path("src").rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "now"
                and not node.args and not node.keywords):
                violations.append(f"{py_file}:{node.lineno}")

    assert not violations, f"datetime.now() without tz found:\n" + "\n".join(violations)
```

### 4.3 Runtime production

Compare timestamps between components at each cycle. If the delta is close to a multiple of one hour, it's a timezone mismatch:

```python
from datetime import datetime, timezone, timedelta

def check_component_clock_drift(
    timestamps: dict[str, datetime],
    max_drift_seconds: int = 60
) -> list[str]:
    """
    Compare timestamps from different components.
    If delta is close to a multiple of 1 hour, flag as timezone mismatch.
    """
    alerts = []
    components = list(timestamps.keys())

    for i in range(len(components)):
        for j in range(i + 1, len(components)):
            a, b = components[i], components[j]
            delta = abs((timestamps[a] - timestamps[b]).total_seconds())

            if delta < max_drift_seconds:
                continue

            nearest_hour = round(delta / 3600)
            remainder = abs(delta - nearest_hour * 3600)

            if nearest_hour > 0 and remainder < 300:
                alerts.append(
                    f"TIMEZONE MISMATCH: {a} vs {b} — "
                    f"delta ~{nearest_hour}h (probably {a} or {b} not in UTC)"
                )

    return alerts
```

As a complement, add a `_tz_info` field in every inter-agent message indicating the sender's timezone. If a receiver sees a different `_tz_info` than its own, it can alert immediately.

## 5. Fix

### 5.1 Immediate fix

Replace every `datetime.now()` call with `datetime.now(timezone.utc)`:

```python
from datetime import datetime, timezone

# BEFORE (bug)
current_hour = datetime.now().hour

# AFTER (fix)
current_hour = datetime.now(timezone.utc).hour
```

For non-Python components (external executors, third-party services), explicitly convert their timestamps to UTC before any comparison:

```python
from datetime import datetime, timezone, timedelta

def external_time_to_utc(external_time: datetime, known_offset_hours: int) -> datetime:
    """Convert a time from an external system to UTC."""
    return external_time.replace(tzinfo=None) - timedelta(hours=known_offset_hours)
```

### 5.2 Robust fix

Centralize all time retrieval in a single module. No component calls `datetime.now()` directly — all go through this module:

```python
"""time_utils.py — Single source of truth for all time operations."""
from datetime import datetime, timezone, timedelta

class Clock:
    """Centralized time provider. All components use this, never datetime.now() directly."""

    OFFSETS = {
        "server_eu": 3,     # UTC+3
        "server_us": -5,    # UTC-5 (EST)
        "api_partner": 0,   # UTC
    }

    @staticmethod
    def now() -> datetime:
        """The ONLY way to get current time. Always UTC, always timezone-aware."""
        return datetime.now(timezone.utc)

    @classmethod
    def from_external(cls, external_time: datetime, system: str) -> datetime:
        """Convert an external system's time to UTC."""
        offset = cls.OFFSETS.get(system, 0)
        naive = external_time.replace(tzinfo=None)
        return (naive - timedelta(hours=offset)).replace(tzinfo=timezone.utc)

    @staticmethod
    def is_in_window(start_hour_utc: int, end_hour_utc: int) -> bool:
        """Check if current UTC hour is within a time window."""
        h = Clock.now().hour
        if start_hour_utc <= end_hour_utc:
            return start_hour_utc <= h < end_hour_utc
        else:  # Window crosses midnight
            return h >= start_hour_utc or h < end_hour_utc

# Usage across all components:
from time_utils import Clock

if Clock.is_in_window(12, 18):
    agent.execute()
```

## 6. Architectural Prevention

The fundamental rule: **one type of time throughout the entire system, and it's UTC**. No exceptions, no "just here it's local for readability", no "we'll convert at the last moment".

Concretely, this implies three architectural decisions:

**1. A centralized `Clock` module.** No component imports `datetime` directly. Everything goes through a module that guarantees UTC + timezone-aware. This module is the only one that knows external system offsets.

**2. Stored timestamps are always ISO-8601 with timezone.** Not `"2026-04-09 14:30:00"` without timezone. Always `"2026-04-09T14:30:00+00:00"` or `"2026-04-09T14:30:00Z"`. The explicit `+00:00` makes the bug impossible to ignore.

**3. Integration tests run in a non-UTC timezone.** Add `TZ=Pacific/Chatham` (UTC+12:45, the world's most exotic timezone) in CI to force implicit assumptions to explode immediately. If a test passes in UTC and fails in UTC+12:45, there's a timezone bug.

## 7. Anti-patterns to Avoid

1. **`datetime.now()` without a timezone argument.** This is the bug in its most common form. Systematically replace with `datetime.now(timezone.utc)`.

2. **`datetime.utcnow()`** (deprecated since Python 3.12). It returns a timezone-naive datetime that **pretends** to be UTC but doesn't carry the information. It cannot be reliably compared with a timezone-aware datetime.

3. **Manually appending `"Z"` to a timestamp.** `datetime.now().isoformat() + "Z"` produces a timestamp that says "UTC" but is actually in local time. It's a lie in the metadata.

4. **Comparing timestamps without checking their timezone.** `time_a > time_b` when one is UTC and the other is local gives a wrong result without raising an error.

5. **Assuming the server is in UTC.** Even if it's true today, a server migration, cloud provider change, or DST transition can silently change the timezone.

## 8. Edge Cases and Variants

**Variant 1: DST (Daylight Saving Time).** Some timezones shift from +2 to +3 seasonally (Europe). The bug can appear or disappear twice a year, making diagnosis extremely difficult. A Central European server is UTC+1 in winter and UTC+2 in summer — even with a known offset, it changes.

**Variant 2: Multi-cloud microservices.** Agent A runs on AWS (UTC by default), Agent B on GCP (UTC by default too, but the container may have a custom timezone), Agent C runs on an on-premise VPS (country's local timezone). Three different time sources for the same system.

**Variant 3: Timestamps in communication files.** An agent writes a JSON file with `"created_at": "2026-04-09 14:30"` in local time. Another agent reads this file and interprets the timestamp as UTC. The file contains no timezone information — impossible to know who's right without knowing the creation context.

**Variant 4: Databases with implicit timezone.** SQLite stores datetimes as text without timezone. PostgreSQL has `TIMESTAMP WITH TIME ZONE` but also `TIMESTAMP WITHOUT TIME ZONE`. If one component writes without timezone and another reads assuming UTC, the offset installs silently.

## 9. Audit Checklist

- [ ] No `datetime.now()` call without `tz=timezone.utc` argument in the codebase
- [ ] No `datetime.utcnow()` call (deprecated Python 3.12+)
- [ ] All stored timestamps (files, DB, messages) contain an explicit timezone
- [ ] CI tests run in a non-UTC timezone (e.g., `TZ=Pacific/Chatham`)
- [ ] A centralized `Clock` module or documented convention enforces UTC everywhere
- [ ] External component timestamps are converted to UTC before comparison
- [ ] A health check compares clocks between components and alerts if drift > 5min

## 10. Further Reading

- Short pattern: [Pattern 01 — Timezone Mismatch](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-01)
- Related patterns: #04 (Multi-File State Desync — timestamps in shared files can be in different timezones), #08 (Data Pipeline Freeze — a misinterpreted timestamp can make data appear fresh when it's stale)
- Recommended reading:
  - "Falsehoods programmers believe about time" (Zach Holman, 2015) — the reference on implicit assumptions in time handling
  - Python `datetime` documentation (timezone-aware vs timezone-naive section) — the fundamental distinction every Python developer must master
