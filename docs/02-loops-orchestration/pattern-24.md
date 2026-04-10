# Pattern #24 — Double-Fire Same Cycle

**Category:** Loops & Orchestration
**Severity:** Medium
**Affected Frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average Debugging Time (if undetected):** 1 to 5 days

---

## 1. One-Sentence Summary

Two independent trigger mechanisms activate the same non-idempotent action within a single processing cycle, causing the action to execute twice — silently, without errors, and without any stack trace to follow.

---

## 2. Conceptual Explanation

In multi-agent systems and orchestration pipelines, actions are commonly triggered by one of two broad mechanisms: **timer-based** (a scheduler fires every N seconds) or **event-based** (a state change, queue message, or tool callback fires on condition). These two mechanisms are designed to be complementary and to cover different execution paths. The double-fire bug occurs when both mechanisms are simultaneously active for the same condition, and both fire within a single processing cycle without any deduplication layer between them.

The core danger is invisibility. When an action is **idempotent** — meaning that executing it twice produces the same result as executing it once — the bug causes no observable harm. The system behaves correctly. No one notices. The bug sits dormant until the day a non-idempotent action is added to the pipeline: sending a Slack message, charging a payment instrument, inserting a database record, dispatching an email, or incrementing a counter. At that point, the double execution produces a real, user-visible error, but the root cause — two wired triggers — can be extremely difficult to locate because neither trigger is wrong in isolation. Each trigger looks perfectly correct when examined alone.

The pattern is especially common in three scenarios:

1. **Migration or refactor.** An old timer-based trigger was the original mechanism. A developer adds an event-based trigger as an improvement. The timer is never removed. Both run.
2. **Configuration inheritance.** A base agent class registers a timer. A subclass adds an event hook for the same condition, unaware that the parent's timer is still active.
3. **LangGraph / StateGraph fan-out.** A graph node that dispatches a notification is reachable via two independent edge paths. Both paths converge on a common input state simultaneously, and the node executes once per path.

In all three cases, the executing code contains no bug. The bug is in the **wiring** — in how two correct mechanisms combine to produce one incorrect outcome.

---

## 3. Minimal Reproducible Example

The following example models a LangChain-style agent loop with a Slack notification tool. A timer-based scheduler and an event-based state-change hook are both wired to call `send_slack_alert`. When a high-severity incident is detected, both fire in the same cycle.

```python
import time
import threading
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Simulated Slack client (replaces real Slack SDK for reproducibility)
# ---------------------------------------------------------------------------

sent_messages: list[dict] = []  # audit log — will contain duplicates

def send_slack_message(channel: str, text: str) -> dict:
    """Simulates posting a message to Slack. NOT idempotent."""
    record = {
        "channel": channel,
        "text": text,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    sent_messages.append(record)
    print(f"[SLACK] → #{channel}: {text}")
    return record


# ---------------------------------------------------------------------------
# Shared pipeline state
# ---------------------------------------------------------------------------

class PipelineState:
    def __init__(self):
        self.incident_severity: Optional[str] = None
        self.last_alert_sent_at: Optional[datetime] = None
        self._lock = threading.Lock()

    def set_incident(self, severity: str):
        with self._lock:
            self.incident_severity = severity

    def get_severity(self) -> Optional[str]:
        with self._lock:
            return self.incident_severity


state = PipelineState()


# ---------------------------------------------------------------------------
# MECHANISM 1 — Timer-based poller (legacy, never removed after refactor)
# ---------------------------------------------------------------------------

def timer_based_alert_loop(state: PipelineState, interval_seconds: float = 1.0):
    """Polls state every interval. Fires alert when severity == 'high'."""
    while True:
        if state.get_severity() == "high":
            send_slack_message(
                channel="incidents",
                text="[TIMER] High-severity incident detected — immediate attention required.",
            )
        time.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# MECHANISM 2 — Event-based hook (added during refactor, intended to replace timer)
# ---------------------------------------------------------------------------

def on_severity_change(new_severity: str, state: PipelineState):
    """Called whenever the pipeline detects a severity change. Added in v2."""
    if new_severity == "high":
        send_slack_message(
            channel="incidents",
            text="[EVENT] High-severity incident detected — immediate attention required.",
        )


# ---------------------------------------------------------------------------
# Simulated agent cycle — both mechanisms active simultaneously (THE BUG)
# ---------------------------------------------------------------------------

def run_agent_cycle_with_bug():
    # Start the legacy timer in the background — it was never removed
    timer_thread = threading.Thread(
        target=timer_based_alert_loop,
        args=(state, 0.5),  # fires every 500ms
        daemon=True,
    )
    timer_thread.start()

    # Simulate the event-based path detecting a high-severity incident
    time.sleep(0.1)  # small startup delay
    new_severity = "high"
    state.set_incident(new_severity)

    # Event hook fires immediately
    on_severity_change(new_severity, state)

    # Timer fires within the next 500ms — second alert sent
    time.sleep(0.6)

    print(f"\n--- Audit log: {len(sent_messages)} message(s) sent ---")
    for msg in sent_messages:
        print(f"  {msg['sent_at']}  {msg['text'][:60]}")


if __name__ == "__main__":
    run_agent_cycle_with_bug()
```

**Expected output (bug active):**
```
[SLACK] → #incidents: [EVENT] High-severity incident detected — immediate attention required.
[SLACK] → #incidents: [TIMER] High-severity incident detected — immediate attention required.

--- Audit log: 2 message(s) sent ---
```

Two Slack messages are delivered to the same channel for a single incident. If the channel is an on-call pager, this creates alert fatigue. If it is a billing webhook, the customer is charged twice.

---

## 4. Detection Checklist

Run through this checklist when a non-idempotent action produces unexpected duplicate results:

- [ ] Search the codebase for every location that calls the suspect function. Count the call sites. More than one is a signal.
- [ ] For each call site, identify whether it is timer-driven, event-driven, or graph-edge-driven.
- [ ] Check whether any timer was introduced before a later event hook that was meant to replace it. Confirm the timer was actually decommissioned.
- [ ] In LangGraph / StateGraph, trace every edge path that can reach the node executing the action. Draw the graph on paper if necessary.
- [ ] Add a correlation ID (request ID, incident ID, cycle ID) to every action call and log it. Check the log for duplicate correlation IDs.
- [ ] Check thread or async task registrations (e.g., `asyncio.create_task`, `threading.Thread`, `APScheduler`, Celery beat) for entries that overlap with event subscriptions on the same condition.
- [ ] Search for `subscribe`, `on_`, `register_hook`, `add_listener`, or equivalent patterns alongside any `schedule`, `cron`, `every`, or `interval` patterns in the same module or parent class.
- [ ] If the action is a tool call in LangChain or CrewAI, check whether the tool is registered in both the agent's tool list and a separate callback handler.

---

## 5. Root Cause Analysis Protocol

When the duplicate is confirmed, use the following structured protocol to identify the exact wiring defect:

**Step 1 — Instrument the action.**
Wrap the suspect function with logging that captures a stack trace on every invocation:

```python
import traceback

def send_slack_message(channel: str, text: str) -> dict:
    print("[TRACE] send_slack_message called from:")
    traceback.print_stack(limit=6)
    # ... original implementation
```

Run the pipeline once. The two stack traces will show exactly which code paths invoked the function.

**Step 2 — Map trigger ownership.**
For each stack trace, walk up the call stack and label each frame as belonging to either a timer/scheduler subsystem or an event/callback subsystem. If both subsystems appear in separate traces for the same function call, the double-fire is confirmed.

**Step 3 — Determine lifecycle.**
Identify when each trigger was introduced (via `git log -S "send_slack_message"` or equivalent). The older trigger is usually the legacy mechanism that was never removed. The newer one is usually the intended replacement.

**Step 4 — Confirm idempotency status.**
Check whether the action maintains any external state (database write, API call, message dispatch). If yes, it is non-idempotent and must be protected. If no, document this and monitor for future changes.

**Step 5 — Scope the blast radius.**
Search for all other non-idempotent actions in the same pipeline that may be reachable by the same pair of triggers. The double-fire may affect more than one action.

---

## 6. Fix Implementation

The canonical fix has two parts: **deduplication at the action layer** and **removal of the redundant trigger**. Both parts are required. Deduplication alone leaves dead code in production. Trigger removal alone fails if the deduplication gap is ever introduced again by a different refactor.

```python
import time
import threading
import hashlib
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Idempotency key store (in production: replace with Redis or a DB table)
# ---------------------------------------------------------------------------

_sent_keys: set[str] = set()
_keys_lock = threading.Lock()


def _make_idempotency_key(channel: str, incident_id: str) -> str:
    raw = f"{incident_id}:{channel}"
    return hashlib.sha256(raw.encode()).hexdigest()


def send_slack_message_safe(channel: str, text: str, incident_id: str) -> Optional[dict]:
    """
    Posts a Slack message only if the (incident_id, channel) pair has not
    been sent before in this process lifetime. In production, the key store
    should be an external store (Redis SETNX with TTL) to survive restarts.
    """
    key = _make_idempotency_key(channel, incident_id)
    with _keys_lock:
        if key in _sent_keys:
            print(f"[DEDUP] Suppressed duplicate alert for incident={incident_id}")
            return None
        _sent_keys.add(key)

    record = {
        "channel": channel,
        "text": text,
        "incident_id": incident_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    print(f"[SLACK] → #{channel}: {text}")
    return record


# ---------------------------------------------------------------------------
# Corrected pipeline — only the event-based trigger remains
# ---------------------------------------------------------------------------

class PipelineState:
    def __init__(self):
        self.incident_severity: Optional[str] = None
        self._lock = threading.Lock()

    def set_incident(self, severity: str):
        with self._lock:
            self.incident_severity = severity

    def get_severity(self) -> Optional[str]:
        with self._lock:
            return self.incident_severity


def on_severity_change(new_severity: str, incident_id: str):
    """
    Single, authoritative event handler. The timer-based loop has been
    removed. This is the only code path that fires Slack alerts.
    """
    if new_severity == "high":
        send_slack_message_safe(
            channel="incidents",
            text=f"High-severity incident {incident_id} detected — immediate attention required.",
            incident_id=incident_id,
        )


def run_agent_cycle_fixed():
    state = PipelineState()

    # Simulate two rapid triggers for the same incident (e.g., race condition
    # during fix rollout where both mechanisms fire one final time)
    incident_id = "INC-20240901-0042"
    state.set_incident("high")

    # Both calls use the same incident_id — only the first will send
    on_severity_change("high", incident_id=incident_id)
    on_severity_change("high", incident_id=incident_id)  # suppressed

    print("\nPipeline cycle complete.")


if __name__ == "__main__":
    run_agent_cycle_fixed()
```

**Expected output (fix active):**
```
[SLACK] → #incidents: High-severity incident INC-20240901-0042 detected — immediate attention required.
[DEDUP] Suppressed duplicate alert for incident=INC-20240901-0042

Pipeline cycle complete.
```

**Key design decisions in the fix:**

- The idempotency key is derived from both the incident identifier and the destination channel. This means a legitimate second alert for a different incident will still go through.
- The key store uses `SETNX` semantics (check-then-set within a lock). In a distributed system, the in-process set must be replaced with a Redis `SETNX` call or a database unique constraint to prevent race conditions across workers.
- The legacy timer thread has been removed entirely. A comment in the codebase should document why: "Timer-based polling removed in v2. All alerts are event-driven via `on_severity_change`. Do not re-add the timer without auditing all non-idempotent action call sites."

---

## 7. Verification Test

```python
import unittest
from unittest.mock import patch, MagicMock
import threading

# Re-import the fixed module components for isolated testing
# (In a real project, import from your module path)

class TestDoubleFirePrevention(unittest.TestCase):

    def setUp(self):
        # Reset the global key store before each test
        _sent_keys.clear()

    def test_single_alert_sent_for_single_incident(self):
        results = []
        original = send_slack_message_safe

        def capturing_send(channel, text, incident_id):
            result = original(channel, text, incident_id)
            if result is not None:
                results.append(result)
            return result

        capturing_send("incidents", "Test alert", "INC-001")
        self.assertEqual(len(results), 1)

    def test_duplicate_suppressed_for_same_incident(self):
        sent_count = [0]

        def count_sends(channel, text, incident_id):
            result = send_slack_message_safe(channel, text, incident_id)
            if result is not None:
                sent_count[0] += 1

        count_sends("incidents", "Alert A", "INC-002")
        count_sends("incidents", "Alert A again", "INC-002")  # same incident_id

        self.assertEqual(sent_count[0], 1, "Duplicate alert was not suppressed")

    def test_different_incidents_both_send(self):
        sent_count = [0]

        def count_sends(channel, text, incident_id):
            result = send_slack_message_safe(channel, text, incident_id)
            if result is not None:
                sent_count[0] += 1

        count_sends("incidents", "Alert for INC-003", "INC-003")
        count_sends("incidents", "Alert for INC-004", "INC-004")

        self.assertEqual(sent_count[0], 2, "Two distinct incidents should both send")

    def test_concurrent_duplicate_suppression(self):
        """Confirms thread-safety of the deduplication key store."""
        sent_count = [0]
        lock = threading.Lock()

        def attempt_send():
            result = send_slack_message_safe("incidents", "Concurrent alert", "INC-005")
            if result is not None:
                with lock:
                    sent_count[0] += 1

        threads = [threading.Thread(target=attempt_send) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sent_count[0], 1, "Only one send should succeed under concurrency")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

All four tests must pass before closing the bug:

- `test_single_alert_sent_for_single_incident` — confirms baseline behavior.
- `test_duplicate_suppressed_for_same_incident` — confirms the deduplication logic.
- `test_different_incidents_both_send` — confirms no over-suppression.
- `test_concurrent_duplicate_suppression` — confirms thread-safety under 20 simultaneous callers.

---

## 8. Prevention Guidelines

**Design rules:**

1. **Single trigger ownership.** Every non-idempotent action must have exactly one designated trigger mechanism. Document that mechanism in a comment at the call site. If a second trigger is added, the first must be explicitly decommissioned.

2. **Idempotency by default.** All actions that cross a process boundary (API call, message dispatch, database write) must accept an idempotency key parameter. This key must be checked before execution. Design this in from the first implementation, not as a retrofit.

3. **Trigger inventory.** Maintain a module-level or class-level comment block listing every registered trigger and the actions it controls. Review this block during code review whenever a new trigger is added.

**Architectural rules:**

4. **Exactly-once semantics at the scheduler layer.** If using APScheduler, Celery beat, or a LangGraph scheduled node, enable coalesce mode (`coalesce=True` in APScheduler) and set `max_instances=1` on every job that calls a non-idempotent action.

5. **Graph edge audit for LangGraph.** Before merging any LangGraph state graph change, draw or print the full graph (`graph.get_graph().print_ascii()`) and count the number of distinct paths that can reach each node that calls a non-idempotent action. More than one path requires a deduplication node inserted before the action node.

6. **Alerting on duplicate keys.** The idempotency key store should emit a metric or log event every time a duplicate is suppressed. A suppression rate above zero in production is always a bug signal, not an expected steady state.

---

## 9. Field Story

A team building an infrastructure monitoring agent for a mid-sized cloud platform had a well-functioning alert pipeline for approximately four months. The agent polled a metrics store every 60 seconds and sent a Slack alert to the on-call channel when any service's error rate exceeded a threshold. This was a simple, reliable timer-based loop.

During a sprint focused on reducing alert latency, an engineer added an event-based trigger: whenever the metrics aggregator emitted a `threshold_exceeded` event, the alert function would fire immediately, rather than waiting for the next 60-second poll. The change was reviewed, approved, and merged. The timer-based loop was not removed from the code because the engineer assumed a senior colleague had already scheduled its removal in a separate ticket. That ticket had been closed as a duplicate and never acted upon.

For two weeks after the deployment, the pipeline was quiet. No thresholds were breached. Both triggers sat dormant and harmless.

Then a database node degraded. The event-based trigger fired and sent the alert within three seconds. The timer-based loop fired 47 seconds later and sent an identical alert. The on-call engineer received two pages in rapid succession, acknowledged the first, and — seeing the second arrive — assumed a second node had also failed. The engineer spent 40 minutes investigating a phantom second failure before a colleague noticed that both alert messages had identical content and identical timestamps on the metrics store.

The investigation traced backward through the on-call channel history. Twelve prior incidents over the previous two weeks had also produced double pages that were never reported because the on-call rotation had attributed them to flapping. The total waste was estimated at 90 engineer-minutes across the rotation, plus measurable on-call fatigue in post-incident surveys.

The fix took 20 minutes: remove the timer thread, add an idempotency key to the alert function backed by a Redis key with a 5-minute TTL, and add a `pytest` test covering concurrent invocation. The team also added a standing item to their sprint review checklist: "For any merged change that adds a trigger or hook, confirm that no prior trigger for the same action remains active."

No further duplicate pages were reported in the following quarter.

---

## 10. Quick Reference Card

```
PATTERN #24 — DOUBLE-FIRE SAME CYCLE
=====================================

SYMPTOM
  Non-idempotent action executes twice per cycle.
  No exception. No error log. Clean execution traces.

TRIGGER SIGNATURE
  Timer-based trigger  +  Event-based trigger
  Both active. Both wired to the same action.

COMMON LOCATIONS
  - Legacy scheduler never removed after event hook added
  - Parent class timer + subclass event hook
  - LangGraph: two edge paths converge on one action node

DETECTION
  1. grep/search all call sites of the suspect function
  2. Print stack trace inside the function on every call
  3. Run pipeline once — read both stack traces
  4. Label each stack frame: timer subsystem vs event subsystem

FIX STEPS
  1. Remove the redundant trigger (keep event-based; remove timer)
  2. Add idempotency key to the action function
  3. Back key store with Redis SETNX + TTL in distributed systems
  4. Write concurrent deduplication test
  5. Emit a metric when a duplicate key is suppressed

IDEMPOTENCY KEY PATTERN
  key = sha256(f"{incident_id}:{channel}")
  if key in store: suppress and log
  else: store.add(key); execute action

PREVENTION
  - One trigger per non-idempotent action (enforced by comment + review)
  - APScheduler: coalesce=True, max_instances=1
  - LangGraph: audit edge count to action nodes before merge

SEVERITY SCALE BY ACTION TYPE
  Send Slack message   → Alert fatigue, missed incidents
  Charge customer      → Financial loss, chargeback risk
  Insert DB record     → Data integrity violation
  Dispatch email       → User trust erosion
  Increment counter    → Metric corruption
```
