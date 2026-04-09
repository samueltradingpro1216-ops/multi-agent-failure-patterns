# Pattern #15 — Command Timestamp Rejection

**Category:** Time & Synchronization
**Severity:** High
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 1 to 5 days (commands are silently dropped; the sender sees "sent", the receiver sees nothing)

---

## 1. Observable Symptoms

Commands sent by the orchestrator are **silently rejected** by the executor. The orchestrator logs show "command sent successfully." The executor logs show nothing — or show "command too old, rejected." The action never happens.

The time delta between the command's timestamp and the executor's clock exceeds a freshness threshold (e.g., 30 seconds). The executor, designed to ignore stale commands for safety, rejects the command as "expired." But the command isn't actually stale — the clocks are simply out of sync.

This typically manifests as a **one-directional failure**: commands from component A to component B work fine, but commands from component C to component B are always rejected. Component C's clock is ahead or behind by more than the freshness threshold.

## 2. Field Story (anonymized)

A multi-agent system had an orchestrator (Python, UTC) sending action commands to an executor (separate service, server time UTC+3). Each command included a `created_at` timestamp. The executor rejected commands older than 30 seconds to prevent replaying stale instructions.

The orchestrator generated `created_at` in UTC. The executor compared it to its local time (UTC+3). Every command appeared to be **10,800 seconds old** (3 hours) from the executor's perspective. 100% of commands were rejected with "command expired: age 10800s > max 30s."

The executor's rejection log was in a different file from the orchestrator's send log. Nobody correlated the two until a developer manually checked the executor's logs after 2 days of "the system isn't doing anything."

## 3. Technical Root Cause

The bug occurs when the **sender's timestamp** and the **receiver's clock** use different time references:

```python
# Sender (UTC)
command = {
    "action": "process_batch",
    "created_at": datetime.now(timezone.utc).isoformat(),  # "2026-04-09T14:30:00+00:00"
}
send_command(command)

# Receiver (UTC+3 local time, no conversion)
def receive_command(command):
    created = datetime.fromisoformat(command["created_at"])
    now = datetime.now()  # Local time: 17:30:00 (no timezone)
    age = (now - created.replace(tzinfo=None)).total_seconds()
    # age = 10800 seconds (3 hours) — command appears ancient
    if age > MAX_COMMAND_AGE:
        log.warning(f"Command expired: age {age}s > max {MAX_COMMAND_AGE}s")
        return  # SILENTLY REJECTED
```

The freshness check is a **safety mechanism** — it prevents replay attacks and ensures the system doesn't execute commands from hours ago. But when the sender and receiver have different clock references, every command fails the freshness check.

This is a specific variant of Pattern #01 (Timezone Mismatch) that's particularly dangerous because:
1. The rejection is **silent** — no exception, no error response to the sender
2. The safety mechanism works correctly (it's right to reject a 3-hour-old command) — the bug is in the clock comparison, not the mechanism
3. Both sides' logs look correct independently; only cross-referencing reveals the mismatch

## 4. Detection

### 4.1 Manual code audit

Find all command freshness checks and verify they compare UTC to UTC:

```bash
# Find timestamp comparisons with age/freshness logic
grep -rn "age\|fresh\|expire\|stale\|too old\|MAX.*AGE\|COMMAND.*TIMEOUT" --include="*.py"

# Check if the comparison uses timezone-aware datetimes
grep -B5 -A5 "age.*>" --include="*.py" -rn | grep -i "datetime\|now\|timestamp"
```

### 4.2 Automated CI/CD

Test that commands survive a simulated clock offset:

```python
from datetime import datetime, timezone, timedelta

def test_command_accepted_across_timezones():
    """Verify that a command created in UTC is accepted by a receiver in any timezone."""
    command_time = datetime.now(timezone.utc)

    # Simulate receivers in different timezones
    for offset_hours in [0, 2, 3, -5, 8]:
        receiver_time = datetime.now(timezone.utc)  # Must compare in UTC
        age = (receiver_time - command_time).total_seconds()
        assert age < 30, f"Command rejected with offset UTC+{offset_hours}: age={age}s"
```

### 4.3 Runtime production

Log the actual age calculation on both sides and alert on high rejection rates:

```python
import logging
from datetime import datetime, timezone

def receive_command_safe(command: dict, max_age: int = 30) -> bool:
    """Accept or reject a command with full diagnostics."""
    created_str = command.get("created_at", "")

    try:
        created = datetime.fromisoformat(created_str)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)  # Assume UTC if naive
    except (ValueError, TypeError):
        logging.error(f"Invalid timestamp in command: {created_str}")
        return False

    now = datetime.now(timezone.utc)  # Always compare in UTC
    age = (now - created).total_seconds()

    if age > max_age:
        logging.warning(
            f"COMMAND REJECTED: age={age:.1f}s > max={max_age}s | "
            f"command_time={created_str} | receiver_time={now.isoformat()}"
        )
        return False

    if age < -5:  # Command from the future (clock skew other direction)
        logging.warning(f"COMMAND FROM FUTURE: age={age:.1f}s — clock skew detected")

    return True
```

## 5. Fix

### 5.1 Immediate fix

Ensure both sender and receiver use UTC for timestamp comparison:

```python
# Sender: always include timezone in timestamp
command["created_at"] = datetime.now(timezone.utc).isoformat()

# Receiver: always parse as UTC-aware and compare in UTC
def check_freshness(created_str: str, max_age: int = 30) -> bool:
    created = datetime.fromisoformat(created_str)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds()
    return 0 <= age <= max_age
```

### 5.2 Robust fix

Use monotonic sequence numbers instead of timestamps for freshness:

```python
class CommandSequencer:
    """Uses sequence numbers instead of timestamps for command freshness."""

    def __init__(self):
        self._seq = 0
        self._last_seen: dict[str, int] = {}

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def is_fresh(self, source: str, seq: int) -> bool:
        """A command is fresh if its sequence > last seen from that source."""
        last = self._last_seen.get(source, 0)
        if seq <= last:
            return False  # Already processed or out of order
        self._last_seen[source] = seq
        return True
```

## 6. Architectural Prevention

Two approaches eliminate this bug class entirely:

**1. Mandatory UTC everywhere.** All timestamps in commands must be UTC with explicit timezone info. Receivers must compare in UTC. This is Pattern #01's prevention applied specifically to command freshness.

**2. Replace timestamps with sequence numbers.** If freshness is about "has this command already been processed?" rather than "is this command recent?", use monotonic sequence numbers. They're immune to clock skew because they don't depend on wall clocks at all.

For systems that need both (replay protection AND recency), use a compound check: sequence number for ordering + UTC timestamp for absolute recency.

## 7. Anti-patterns to Avoid

1. **Naive datetime comparison.** `datetime.now()` (local) compared to a UTC timestamp gives a wrong age equal to the timezone offset.

2. **Silent rejection without logging.** A command rejected for staleness should log the command's timestamp, the receiver's time, and the calculated age. Silent rejection makes debugging impossible.

3. **Too-tight freshness threshold.** A 5-second threshold on a system with 2-second network latency and 1-second clock drift leaves only 2 seconds of margin. Use at least 3x the expected latency.

4. **Assuming clocks are synchronized.** Even with NTP, clocks can drift by seconds. Never assume two machines have the same time. Always use explicit timezone-aware comparisons.

5. **Freshness check only on the receiver.** If the sender also logs the command's age at send time, cross-referencing reveals clock skew immediately.

## 8. Edge Cases and Variants

**Variant 1: Commands from the future.** If the sender's clock is ahead, commands arrive with a timestamp in the future from the receiver's perspective. The age is negative. Some implementations reject negative ages (commands that "haven't been created yet").

**Variant 2: Batch commands with stale timestamps.** A batch of 100 commands is generated over 5 seconds. The first command's timestamp is 5 seconds older than the last. With a 10-second freshness threshold, the first commands in a large batch may be rejected by the time they're processed.

**Variant 3: Clock jump.** NTP corrects a drifted clock by jumping it forward or backward. Commands in transit during the jump may suddenly appear very old or very new. Systems should tolerate clock jumps of up to ±5 seconds.

**Variant 4: DST transition.** During the DST "fall back" hour, local clocks repeat an hour. Commands timestamped in local time become ambiguous during this period — the same timestamp refers to two different moments.

## 9. Audit Checklist

- [ ] All command timestamps include explicit timezone information (ISO-8601 with `+00:00`)
- [ ] Receivers compare timestamps in UTC, never in local time
- [ ] Freshness threshold accounts for network latency + clock drift (at least 3x max expected latency)
- [ ] Rejected commands are logged with both the command's and the receiver's timestamps
- [ ] A health check monitors the rejection rate and alerts if > 10% of commands are rejected

## 10. Further Reading

- Short pattern: [Pattern 01 — Timezone Mismatch](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-01) (the parent pattern for all timestamp-related bugs)
- Related patterns: #01 (Timezone Mismatch — the root cause), #04 (Multi-File State Desync — stale timestamps in state files)
- Recommended reading:
  - Google's TrueTime API design paper — how Google solved clock uncertainty in distributed systems
  - "Time, Clocks, and the Ordering of Events in a Distributed System" (Leslie Lamport, 1978) — the foundational paper on why wall clocks are unreliable for ordering in distributed systems
