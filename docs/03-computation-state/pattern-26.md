# Pattern #26 — Counter Not Persisted Across Restart

**Category:** Computation & State
**Severity:** Medium
**Affected Frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average Debugging Time if Undetected:** 1 to 10 days

---

## 1. Observable Symptoms

The system behaves correctly under normal continuous operation but begins violating its own enforced limits whenever the process restarts — whether that restart is triggered by a deployment, a crash recovery, a scheduled maintenance window, or a container orchestrator rolling update.

**Immediate signals:**

- A daily or hourly cap on requests, API calls, or operations appears to be enforced correctly from a cold start, but is exceeded on days when the process restarts mid-period. The violation is proportional: a process that restarts at noon can issue up to twice the configured limit before the calendar day rolls over.
- A metric tracking cumulative consumption (requests, tokens, records processed, retries) resets to 0 in logs or dashboards exactly at restart time. Engineers assume this is normal telemetry behavior; the business impact is not noticed until downstream costs or third-party rate limit violations surface.
- A safety gate that should reject operations once a counter exceeds a threshold passes all operations immediately after restart, because the counter starts at 0. The rejection only resumes once the counter re-accumulates to the threshold within the new process lifetime.
- Downstream systems (third-party APIs, databases, external services) begin returning 429, quota-exceeded, or billing anomaly notifications on specific calendar days, not uniformly across all days. The affected days correlate with deployment schedules, which no one initially suspects.

**Delayed signals (days to weeks later):**

- API usage bills for a third-party provider are consistently higher on deployment days. A billing analyst flags the anomaly before any engineer does.
- A compliance report reveals that the daily request count exceeded the contractually guaranteed maximum on seven days in the past month, all of which were deployment days.
- An automated safety monitor that tracks consecutive error counts stops escalating after restarts. An incident that should have triggered an alert at 10 consecutive errors is silently reset by a mid-incident deployment, and the counter starts from 0 again.

**The distinguishing characteristic of this pattern** is that the violation window is bounded by the restart event and the period boundary (end of day, end of hour). On days without restarts, behavior is perfectly correct. The bug is invisible to developers who test from cold starts only, because a cold start at midnight is the one case where in-memory and persisted counters are equivalent.

---

## 2. Field Story (Anonymized)

**Domain:** API rate limiting for a data ingestion pipeline.

A data engineering team built an ingestion pipeline that pulled records from a third-party research data vendor and stored them in their internal data warehouse. The vendor contract allowed a maximum of 10,000 API requests per calendar day. Exceeding this limit incurred a significant overage fee and, after the third violation in a rolling 30-day period, risked suspension of the API key.

The team implemented a `DailyRequestCounter` class. At process startup it initialized `self.count = 0`. Before each API request, the pipeline called `counter.can_proceed()` which returned `False` if `self.count >= 10000`. After each successful request, it called `counter.increment()`. The logic was correct for a process that ran continuously from midnight to midnight. The counter was never written to disk.

For three months the system worked without incident. Then the team moved to a weekly deployment schedule, deploying every Tuesday at 2:00 PM via a blue-green rollout that restarted the pipeline process. For eight consecutive Tuesdays, the pipeline issued between 12,000 and 16,000 requests — the pre-restart count plus the post-restart count, both independently reaching the 10,000 ceiling before the end of the day. The vendor invoiced overage fees for each of those Tuesdays.

The initial hypothesis was a bug in the `can_proceed()` logic — perhaps an off-by-one error in the comparison. Engineers reviewed the code, ran unit tests, and found nothing wrong. The unit tests initialized the counter at 0 and exercised the threshold boundary correctly. No test simulated a mid-day restart.

On the third week of investigation, an engineer added process-restart timestamps to the deployment log and overlaid them with the daily API call totals from the vendor's dashboard. The correlation was immediate and unmistakable: every overage day had a deployment at 2:00 PM, and no non-deployment day had ever exceeded 10,000 requests. The root cause was identified in forty minutes once the data was assembled. Remediation took two hours. The delay was not technical difficulty — it was the two weeks spent looking in the wrong place.

---

## 3. Technical Root Cause

The bug is a state persistence boundary mismatch. The counter enforces a limit that is defined over a real-world time period (a calendar day). The counter's state lives in process memory. Process memory is destroyed on restart. The time period is not.

```python
# BROKEN — counter lives only in process memory
class DailyRequestCounter:
    def __init__(self, daily_limit: int):
        self.daily_limit = daily_limit
        self.count = 0               # reset to 0 on every process start
        self.date = date.today()     # also reset — but to today's actual date

    def can_proceed(self) -> bool:
        if date.today() != self.date:
            self.count = 0           # correct midnight rollover
            self.date = date.today()
        return self.count < self.daily_limit

    def increment(self):
        self.count += 1
```

The `date` check correctly handles the calendar day boundary: if the process runs continuously and midnight passes, the counter resets. But `date.today()` at startup is always today. If the process restarts at 2:00 PM on Tuesday, the new instance initializes `self.count = 0` and `self.date = tuesday`. It has no knowledge that the previous process instance had already consumed 7,000 requests against Tuesday's quota. The new instance believes Tuesday is fresh.

The fundamental error is treating in-memory state as sufficient to enforce a constraint whose scope exceeds a single process lifetime. This error is most common with:

1. **Daily or hourly request budgets.** Any limit that spans a real-world period longer than a single process run is at risk.
2. **Consecutive error counters.** A counter tracking consecutive errors to trigger escalation resets on restart, silencing an ongoing incident.
3. **Retry counts for idempotent operations.** A per-record retry count stored in memory resets on restart, allowing the same record to be retried indefinitely across restarts.
4. **Cooldown timers.** A "do not call this endpoint more than once per 5 minutes" timer resets on restart, allowing immediate re-calls.

In multi-instance or container deployments, the problem is compounded: each instance has its own in-memory counter. A system with 4 replicas can issue up to 4× the configured limit if each replica tracks independently.

---

## 4. Detection

### 4.1 Manual Code Audit

Search for counter or accumulator variables initialized to 0 (or to `None`, then set to 0 in `__init__`) that are used to gate operations against a threshold:

```bash
# Find in-memory counters used with limits
grep -rn "self\.\w*count\w*\s*=\s*0\|self\.\w*total\w*\s*=\s*0" --include="*.py" -l

# Find threshold comparisons against such counters
grep -rn ">=.*limit\|>=.*max_\|< self\.\w*limit" --include="*.py" -A 2 -B 2

# Find classes that have a 'count' or 'total' attribute but no file/db write
grep -rn "class.*Counter\|class.*Tracker\|class.*Budget" --include="*.py" -l \
    | xargs grep -L "open\|sqlite\|redis\|json\.dump\|pickle\|shelve"
```

For each counter found, ask the following questions:

- What real-world time period does this counter span? (per-request, per-day, per-hour, per-session?)
- If the process restarts mid-period, what happens to the accumulated value?
- Is there a test that restarts the counter mid-period and verifies the limit is still respected?
- In a multi-replica deployment, does each replica share this counter or maintain its own?

Any counter that spans a period longer than the expected process uptime, and is not persisted, is a candidate for this bug.

### 4.2 Automated CI/CD

Test counter behavior explicitly across a simulated restart. The key assertion is that after a restart, the limit for the current period accounts for what was consumed before the restart:

```python
# tests/test_counter_persistence.py
import os
import json
import tempfile
from datetime import date
from myagents.rate_counter import DailyRequestCounter


def test_counter_survives_process_restart():
    """
    Verify that a counter initialized mid-day reflects pre-restart consumption.

    Simulates: process A runs, consumes 6000 of 10000 daily requests, then
    restarts. Process B (same calendar day) must start from 6000, not from 0.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        state_path = f.name

    try:
        daily_limit = 10_000

        # --- Process A lifecycle ---
        counter_a = DailyRequestCounter(daily_limit=daily_limit, state_path=state_path)
        for _ in range(6_000):
            assert counter_a.can_proceed(), "Should be able to proceed (6000 < 10000)"
            counter_a.increment()

        assert counter_a.count == 6_000

        # Simulate restart: del counter_a (process memory gone), counter_b = new instance
        del counter_a

        # --- Process B lifecycle (same calendar day) ---
        counter_b = DailyRequestCounter(daily_limit=daily_limit, state_path=state_path)

        # Must load pre-restart count, not start from 0
        assert counter_b.count == 6_000, (
            f"COUNTER NOT PERSISTED: after restart counter_b.count={counter_b.count}, "
            f"expected 6000. 4000 remaining budget has become 10000."
        )

        # Verify remaining budget is 4000, not 10000
        remaining = sum(1 for _ in range(10_000) if counter_b.can_proceed() and not counter_b.increment())  # noqa
        # Simpler: just check that the 10001st request is rejected
        counter_b2 = DailyRequestCounter(daily_limit=daily_limit, state_path=state_path)
        for _ in range(4_000):
            counter_b2.increment()
        assert not counter_b2.can_proceed(), (
            "After 10000 total requests (6000 pre-restart + 4000 post-restart), "
            "further requests must be rejected."
        )
    finally:
        os.unlink(state_path)


def test_counter_resets_at_midnight():
    """
    Verify that the counter resets to 0 at the calendar day boundary, not on restart.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        state_path = f.name

    try:
        counter = DailyRequestCounter(daily_limit=10_000, state_path=state_path)
        for _ in range(9_000):
            counter.increment()

        # Simulate the state file being written with yesterday's date
        with open(state_path) as f:
            state = json.load(f)
        from datetime import timedelta
        state["date"] = (date.today() - timedelta(days=1)).isoformat()
        with open(state_path, "w") as f:
            json.dump(state, f)

        # New instance: yesterday's count should not carry over
        counter_new_day = DailyRequestCounter(daily_limit=10_000, state_path=state_path)
        assert counter_new_day.count == 0, (
            f"Counter should reset at midnight, got count={counter_new_day.count}"
        )
    finally:
        os.unlink(state_path)
```

Add to CI:

```yaml
# .github/workflows/ci.yml
- name: Counter persistence tests
  run: pytest tests/test_counter_persistence.py -v
```

### 4.3 Runtime Production

Emit a structured log event at process startup that reports the loaded counter value. If the value loaded from persistence is significantly lower than expected for the time of day, emit a warning. Monitor the counter value and remaining budget as a time-series metric:

```python
# In DailyRequestCounter.__init__, after loading state:
import logging

logger = logging.getLogger("rate_counter")

class DailyRequestCounter:
    def __init__(self, daily_limit: int, state_path: str):
        self.daily_limit = daily_limit
        self.state_path = state_path
        self._load()

        logger.info(
            "DailyRequestCounter initialized. date=%s count=%d limit=%d remaining=%d",
            self.date.isoformat(),
            self.count,
            self.daily_limit,
            self.daily_limit - self.count,
        )

        # Warn if we are resuming deep into the day but the counter is suspiciously low
        from datetime import datetime, timezone
        hour_of_day = datetime.now(timezone.utc).hour
        expected_minimum = (hour_of_day / 24) * self.daily_limit * 0.5
        if self.count < expected_minimum and hour_of_day > 4:
            logger.warning(
                "COUNTER RESUME ANOMALY: loaded count=%d but hour=%d suggests at least "
                "%.0f requests should have been made. Possible cold-start on existing day.",
                self.count,
                hour_of_day,
                expected_minimum,
            )
```

Expose `daily_requests_consumed` and `daily_requests_remaining` as Prometheus gauges. Alert if `daily_requests_remaining` jumps upward mid-day (a signature of counter reset).

---

## 5. Fix

### 5.1 Immediate Fix

Persist the counter state to a local JSON file. Load it at startup. Write it after every increment. Add a date check to reset the counter when the calendar day rolls over:

```python
# rate_counter.py — persisted daily request counter
import json
import logging
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


class DailyRequestCounter:
    """
    A daily request counter that persists its state to disk.

    Survives process restarts within the same calendar day.
    Automatically resets at midnight (UTC).

    Args:
        daily_limit: Maximum requests allowed per calendar day.
        state_path:  Path to the JSON file where state is persisted.
                     Must be on a volume that survives process restarts.
    """

    def __init__(self, daily_limit: int, state_path: str):
        self.daily_limit = daily_limit
        self.state_path = Path(state_path)
        self._load()

    def can_proceed(self) -> bool:
        """Return True if a request may be issued under the current daily budget."""
        self._maybe_reset()
        return self.count < self.daily_limit

    def increment(self) -> None:
        """Record one request against the daily budget and persist immediately."""
        self._maybe_reset()
        self.count += 1
        self._save()

    def remaining(self) -> int:
        """Return remaining requests for today."""
        self._maybe_reset()
        return max(0, self.daily_limit - self.count)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_reset(self) -> None:
        today = date.today().isoformat()
        if self.date != today:
            logger.info(
                "DailyRequestCounter: new day detected (was %s, now %s). Resetting count.",
                self.date,
                today,
            )
            self.date = today
            self.count = 0
            self._save()

    def _load(self) -> None:
        today = date.today().isoformat()
        if self.state_path.exists():
            try:
                with open(self.state_path) as f:
                    state = json.load(f)
                loaded_date = state.get("date", "")
                loaded_count = state.get("count", 0)
                if loaded_date == today:
                    self.date = loaded_date
                    self.count = loaded_count
                    logger.info(
                        "DailyRequestCounter: resumed from disk. date=%s count=%d remaining=%d",
                        self.date,
                        self.count,
                        self.daily_limit - self.count,
                    )
                    return
                else:
                    logger.info(
                        "DailyRequestCounter: persisted date %s is not today (%s). "
                        "Starting fresh.",
                        loaded_date,
                        today,
                    )
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                logger.warning(
                    "DailyRequestCounter: could not load state from %s (%s). "
                    "Starting from 0. This may allow a burst above the daily limit.",
                    self.state_path,
                    exc,
                )
        else:
            logger.info(
                "DailyRequestCounter: no persisted state at %s. Starting from 0.",
                self.state_path,
            )
        self.date = today
        self.count = 0
        self._save()

    def _save(self) -> None:
        tmp_path = self.state_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump({"date": self.date, "count": self.count}, f)
            os.replace(tmp_path, self.state_path)  # atomic on POSIX; best-effort on Windows
        except OSError as exc:
            logger.error(
                "DailyRequestCounter: failed to persist state to %s: %s. "
                "Counter is in memory only until next successful save.",
                self.state_path,
                exc,
            )
```

Usage:

```python
counter = DailyRequestCounter(
    daily_limit=10_000,
    state_path="/var/lib/myagent/daily_request_counter.json",
)

def call_vendor_api(payload: dict) -> dict:
    if not counter.can_proceed():
        raise DailyLimitExceeded(
            f"Daily request limit ({counter.daily_limit}) reached. "
            f"Requests will resume tomorrow."
        )
    counter.increment()
    return vendor_client.post(payload)
```

### 5.2 Robust Fix — Redis-Backed Counter with Atomic Expiry

For multi-replica deployments, use a Redis counter with an atomic expiry tied to the end of the current UTC day. All replicas share one counter; the TTL-based expiry handles midnight rollover automatically without any cron job or date comparison:

```python
# redis_rate_counter.py
import logging
import time
from datetime import datetime, timezone

import redis

logger = logging.getLogger(__name__)


class RedisDAilyRequestCounter:
    """
    Daily request counter backed by Redis.

    Uses INCR + EXPIREAT for atomicity. Safe for multi-replica deployments.
    All replicas share the same counter; Redis TTL handles midnight rollover.

    Args:
        redis_client:  A connected redis.Redis (or redis.asyncio.Redis) instance.
        key_prefix:    Namespace prefix for the Redis key (default: "rate:daily").
        daily_limit:   Maximum requests allowed per UTC calendar day.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        daily_limit: int,
        key_prefix: str = "rate:daily",
    ):
        self.redis = redis_client
        self.daily_limit = daily_limit
        self.key_prefix = key_prefix

    def _key(self) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"{self.key_prefix}:{today}"

    def _end_of_day_ts(self) -> int:
        """Unix timestamp of 00:00:00 UTC tomorrow."""
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = tomorrow.replace(day=now.day + 1) if now.day < 28 else (
            # Safer: add 86400 seconds and truncate to midnight
            datetime.fromtimestamp(now.timestamp() + 86_400, tz=timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        return int(tomorrow.timestamp())

    def can_proceed(self) -> bool:
        """Return True if the daily budget has not been exhausted."""
        key = self._key()
        current = self.redis.get(key)
        count = int(current) if current is not None else 0
        return count < self.daily_limit

    def increment_and_check(self) -> bool:
        """
        Atomically increment the counter and return True if the request is within budget.

        Uses a Lua script to ensure the check-and-increment is atomic:
        no two callers can both see count=9999 and both proceed.
        """
        lua_script = """
        local key = KEYS[1]
        local limit = tonumber(ARGV[1])
        local expiry = tonumber(ARGV[2])
        local current = redis.call('INCR', key)
        if current == 1 then
            redis.call('EXPIREAT', key, expiry)
        end
        if current <= limit then
            return 1
        else
            redis.call('DECR', key)   -- roll back: we exceeded the limit
            return 0
        end
        """
        key = self._key()
        expiry = self._end_of_day_ts()
        result = self.redis.eval(lua_script, 1, key, self.daily_limit, expiry)
        allowed = bool(result)
        if not allowed:
            logger.warning(
                "RedisDAilyRequestCounter: daily limit %d reached for key %s.",
                self.daily_limit,
                key,
            )
        return allowed

    def remaining(self) -> int:
        key = self._key()
        current = self.redis.get(key)
        count = int(current) if current is not None else 0
        return max(0, self.daily_limit - count)
```

Usage in a LangGraph node:

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict
import redis
from redis_rate_counter import RedisDAilyRequestCounter

redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
counter = RedisDAilyRequestCounter(
    redis_client=redis_client,
    daily_limit=10_000,
    key_prefix="ingestion:daily",
)


class IngestionState(TypedDict):
    record_batch: list[dict]
    result: list[dict]
    error: str | None


def ingestion_node(state: IngestionState) -> IngestionState:
    results = []
    for record in state["record_batch"]:
        if not counter.increment_and_check():
            return {
                **state,
                "error": "Daily API limit reached. Remaining records deferred to tomorrow.",
                "result": results,
            }
        api_result = vendor_client.fetch(record)
        results.append(api_result)
    return {**state, "result": results, "error": None}
```

---

## 6. Architectural Prevention

**Principle:** any counter that enforces a limit spanning a real-world time period must be stored outside process memory. The boundary between in-process state and persistent state should be explicit and documented.

Apply this principle at the design level by categorizing all counters at design time:

| Counter type | Scope | Correct storage |
|---|---|---|
| Requests in current HTTP call | Single request | In-memory (request context) |
| Retries for one record | One pipeline run | In-memory (task context) |
| Hourly rate limit | 60-minute window | Redis (INCR + TTL) or DB |
| Daily quota | Calendar day | Redis (INCR + EXPIREAT) or DB |
| Consecutive errors | Unbounded until reset | Persisted + cleared on recovery |
| Cumulative billing | Month-to-date | DB (with transactional writes) |

For LangGraph pipelines, store quota counters in the graph's persistent checkpointer backend (e.g., `SqliteSaver` or `PostgresSaver`), not in the `TypedDict` state that is rebuilt from scratch on each invocation:

```python
# langgraph_with_persistent_counter.py
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph

# The checkpointer persists graph state to SQLite across process restarts.
# Counter values stored in state survive restarts automatically.
checkpointer = SqliteSaver.from_conn_string("/var/lib/myagent/graph_state.db")

graph = build_ingestion_graph()
app = graph.compile(checkpointer=checkpointer)
```

For multi-replica deployments, use a distributed store (Redis, DynamoDB, PostgreSQL with `SELECT FOR UPDATE`) as the authoritative counter. Never rely on local-to-replica state for cross-replica limits.

---

## 7. Anti-patterns to Avoid

**Anti-pattern 1: Initializing a period-spanning counter to 0 in `__init__` without loading persisted state.**

```python
# WRONG
def __init__(self, daily_limit: int):
    self.daily_limit = daily_limit
    self.count = 0  # always 0 on startup — persisted value is discarded
```

Every restart grants a fresh budget regardless of what was consumed in the same period.

**Anti-pattern 2: Storing the counter in a process-level global variable.**

```python
# WRONG — module-level global, reset on every process start
_daily_count = 0

def can_proceed():
    return _daily_count < DAILY_LIMIT
```

This is equivalent to `__init__` initialization: process-scoped, not period-scoped.

**Anti-pattern 3: Relying on log files as the persistence mechanism.**

```python
# WRONG — reconstructing a counter from logs is fragile and not atomic
def load_count_from_logs(log_path: str, today: str) -> int:
    count = 0
    with open(log_path) as f:
        for line in f:
            if today in line and "API_REQUEST" in line:
                count += 1
    return count
```

Log files may be rotated, truncated, buffered, or simply not present on a new container instance. They are not a reliable source of truth for counter state.

**Anti-pattern 4: Using a file lock without an atomic write.**

```python
# WRONG — non-atomic write; a crash between open() and close() corrupts the file
with open(state_path, "w") as f:
    json.dump({"count": self.count}, f)
    # if the process dies here, the file is partially written
```

Always write to a `.tmp` file and use `os.replace()` (atomic on POSIX; best-effort on Windows) to swap it in.

**Anti-pattern 5: Treating a per-replica counter as if it were a per-system counter.**

In a 3-replica deployment, each replica maintains its own in-memory counter initialized to 0. If the daily limit is 10,000, the system can issue up to 30,000 requests per day — one full limit per replica. This error is particularly hard to notice during single-instance testing.

**Anti-pattern 6: Resetting the counter on restart instead of loading it.**

```python
# WRONG — explicitly discards persisted state
def __init__(self, state_path: str):
    self.count = 0
    self._save()  # overwrites any persisted value with 0
```

This is the same as not persisting at all, but actively more dangerous because it destroys evidence of prior consumption.

---

## 8. Edge Cases and Variants

**Variant A — Consecutive error counter reset silencing escalation.**
A pipeline tracks consecutive errors to trigger an alert after 10 in a row. A deployment at error 8 resets the counter to 0. Errors 9 and 10 of the run are now errors 1 and 2 of the new process. The alert never fires. Persisting consecutive error counters alongside the date of last success prevents this. Clear the counter only on explicit success, never on restart.

**Variant B — Multi-replica counter doubling.**
Three pipeline replicas each track daily requests independently. The daily limit of 10,000 becomes effectively 30,000. Fix: use a shared Redis counter keyed to the date, not a per-process counter. If Redis is unavailable, fail closed: reject all requests rather than issuing an uncounted burst.

**Variant C — Container restarts in Kubernetes.**
A Kubernetes pod is OOMKilled and restarted. The `/tmp` path where the counter file was written is local to the container — not a mounted volume. The file is gone. The new container starts from 0. Fix: mount a persistent volume at the counter file path. Alternatively, use an external store (Redis, PostgreSQL) that is not local to the container.

**Variant D — Time-zone mismatch in the midnight reset.**
The counter uses `date.today()` which returns the system local date. In a containerized environment, the system timezone may be UTC. Business-day limits are defined in the customer's local timezone (e.g., Eastern). The counter resets at UTC midnight (8:00 PM Eastern), allowing a second full-budget window between 8:00 PM and midnight Eastern. Fix: always use `datetime.now(timezone.utc).date()` and define all period boundaries in UTC.

**Variant E — Retry count not persisted, enabling infinite retries across restarts.**
A pipeline processes records and retries failed ones up to 3 times before sending to a dead-letter queue. The retry count is stored in memory keyed by record ID. A restart clears all retry counts. Records that failed 3 times before the restart are retried 3 more times after the restart. This can continue indefinitely across deployments. Fix: persist the retry count alongside the record in the database or message queue metadata.

**Variant F — Hourly window counter straddling a restart.**
An hourly limit uses `datetime.now().hour` for the window key. A restart at 3:45 PM resets the counter. Between 3:45 PM and 4:00 PM, the system has a fresh 15-minute window with a full hourly budget. Fix: store the window start timestamp, not just the hour integer. Load it from persistence on startup. A request's window is valid only if `now < window_start + window_duration`.

---

## 9. Audit Checklist

Use this checklist during code review for any class that tracks a counter used to gate operations against a threshold.

- [ ] All counters that enforce time-period-based limits (hourly, daily, monthly) are persisted to disk, a database, or a distributed cache — not stored only in process memory.
- [ ] At process startup, the counter loads its persisted value and logs the loaded count and remaining budget.
- [ ] The persistence layer uses atomic writes (write-to-temp then rename/replace) to prevent partial-write corruption.
- [ ] Counter state files are stored on a volume that survives process restarts (not `/tmp` or a non-mounted container filesystem).
- [ ] The calendar-day (or period) rollover is implemented by comparing a stored date to today's date, not by assuming the process lifetime aligns with the period.
- [ ] In multi-replica deployments, all replicas share a single counter in a distributed store; there is no per-replica counter for system-wide limits.
- [ ] A CI test simulates a mid-period restart and asserts the post-restart counter correctly reflects pre-restart consumption.
- [ ] A startup log entry reports the loaded counter value, making unexpected resets visible in deployment logs.
- [ ] A monitoring metric exposes the counter value and remaining budget; an alert fires if the remaining budget increases mid-period (signaling an unexpected reset).
- [ ] Consecutive error and retry counters are cleared only on explicit success or explicit operator action, never on restart.

---

## 10. Further Reading

**Internal cross-references:**

- [Pattern #04 — Multi-File State Desync](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/05-io-persistence/pattern-04-multi-file-state-desync.md): when the persisted counter file and the in-memory counter diverge (e.g., due to a failed write), the state desync pattern applies. Both patterns require atomic writes and a reconciliation strategy at startup.
- [Pattern #09 — Silent State Mutation](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/03-computation-state/pattern-09-silent-state-mutation.md): a counter reset is a form of silent state mutation — the value changes without any explicit signal to the operators or the system.
- [Pattern #14 — Stale Lock File](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/05-io-persistence/pattern-14-stale-lock-file.md): both patterns involve persistent files that encode process-lifetime assumptions; stale lock files fail in the opposite direction (old state is mistakenly treated as current).
- [Pattern #21 — LLM Pool Contention](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/09-multi-agent-governance/pattern-21-llm-pool-contention.md): multi-replica counter issues are a contributing factor to pool contention; fixing the per-replica counter isolation is a prerequisite to meaningful rate coordination.

**External references:**

- Redis documentation — `INCR`, `EXPIREAT`, and Lua scripting for atomic operations: https://redis.io/commands/incr/ and https://redis.io/docs/manual/programmability/lua-api/
- Python `os.replace()` documentation (atomic rename): https://docs.python.org/3/library/os.html#os.replace — the standard cross-platform mechanism for atomic file writes in Python.
- LangGraph persistence documentation — `SqliteSaver` and `PostgresSaver` checkpointers: https://langchain-ai.github.io/langgraph/concepts/persistence/ — the LangGraph-native approach to surviving restarts without manual state file management.
- "Designing Data-Intensive Applications" (Martin Kleppmann, O'Reilly, 2017) — Chapter 7 on transactions and Chapter 11 on stream processing. The distinction between process-scoped and period-scoped state is a specific case of the broader principle that durable state must be stored in a durable medium.
- All patterns in this playbook: https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns
