# Pattern #02 — Rapid-Fire Loop (Refresh Zombie)

**Category:** Loops & Orchestration
**Severity:** High
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 1 to 3 days (the symptom appears quickly in logs or billing, but the root cause is subtle)

---

## 1. Observable Symptoms

The executor repeats the same action **in a tight loop**, every N seconds, without interruption. Logs show a repetitive pattern: the same command sent, executed, completed, then immediately re-sent. What should have been a single action turns into hundreds of identical executions within a few hours.

The consequences scale with the marginal cost of each execution. If each iteration costs an LLM API call, the bill explodes within minutes. If each iteration consumes disk I/O, logs balloon to hundreds of MB per day. If each iteration performs a real-world action (sending a message, writing to a database), the system pollutes its own data with duplicates.

The most deceptive symptom: each individual execution **appears correct**. The log shows "action executed successfully". The repetition is the bug, not the action itself. Any monitoring that does not count executions per time window will detect nothing.

## 2. Field Story (anonymized)

A multi-agent monitoring system generated analysis reports every 10 seconds. When a report was generated and the consumer had no active task, the refresh mechanism re-sent the generation command.

The problem: when the consumer finished its report quickly (in 2–3 seconds), its status returned to "idle". The refresh saw "idle" and re-sent the command, assuming it had never been executed. Over the course of one day, the system generated 124 identical reports instead of one. Each report cost an LLM call. The daily bill was multiplied by 100.

The bug was discovered by looking at the LLM provider invoice, not the logs — the logs showed 124 "normal" executions.

## 3. Technical Root Cause

The bug originates when a refresh/retry mechanism fails to distinguish two states that share the same external representation:

```
State 1: "The command has NOT YET been executed"  → status = IDLE
State 2: "The command was executed and COMPLETED"  → status = IDLE

The refresh sees IDLE and re-sends in both cases.
```

The implicit lifecycle looks like this:

```
Signal → Command written → Executor reads → Execution → End
                                                         ↓
                                                    status = IDLE
                                                         ↓
                                               Refresh sees IDLE
                                                         ↓
                                               Re-writes the command
                                                         ↓
                                               Executor re-executes → LOOP
```

The fundamental problem is the **absence of an explicit state machine**. The system uses a boolean (`has_active_task`) instead of a 4-value enum (`IDLE → PENDING → EXECUTING → COMPLETED`). When `has_active_task` goes back to `False` after completion, it is indistinguishable from "never executed".

Variants include:
- A LangChain orchestrator that re-dispatches a task because the result was consumed (the callback read and deleted it)
- A CrewAI agent that re-delegates a task to a sub-agent because the `task_output` was cleared by the garbage collector
- A cron job that restarts an agent "if no recent result", without checking whether a result was already produced and archived

## 4. Detection

### 4.1 Manual code audit

Look for refresh/retry patterns that do not track execution history:

```bash
# Look for retry loops based on a boolean status
grep -rn "while.*not.*done\|if.*status.*idle\|if not.*active" --include="*.py"

# Look for command re-send mechanisms
grep -rn "refresh.*command\|retry.*send\|re.*dispatch" --include="*.py"

# Look for files/flags deleted after execution (loss of memory)
grep -rn "os\.remove\|\.unlink\|delete.*after" --include="*.py"
```

For each match, verify: does the code distinguish "never executed" from "executed and completed"?

### 4.2 Automated CI/CD

Add an integration test that simulates the full cycle command → execution → completion → verification that the refresh does not re-send:

```python
def test_no_rapid_fire_after_completion():
    """Verify that a completed task is not re-dispatched by the refresh mechanism."""
    orchestrator = Orchestrator()
    executor = MockExecutor()

    # Send a task
    orchestrator.dispatch("task-1", executor)
    assert executor.execution_count == 1

    # Simulate task completion
    executor.complete("task-1")

    # Run 10 refresh cycles
    for _ in range(10):
        orchestrator.refresh()

    # Should still be 1, not 11
    assert executor.execution_count == 1, (
        f"Rapid-fire detected: task executed {executor.execution_count} times"
    )
```

### 4.3 Runtime production

Count executions per task identifier within a sliding window:

```python
import time
from collections import defaultdict

class RapidFireDetector:
    """Detects when the same task is executed too many times in a window."""

    def __init__(self, window_seconds: int = 300, max_executions: int = 5):
        self.window = window_seconds
        self.max = max_executions
        self.history: dict[str, list[float]] = defaultdict(list)

    def record(self, task_id: str) -> bool:
        """Record an execution. Returns False if rapid-fire detected."""
        now = time.monotonic()
        cutoff = now - self.window

        # Clean old entries
        self.history[task_id] = [t for t in self.history[task_id] if t > cutoff]

        if len(self.history[task_id]) >= self.max:
            return False  # RAPID-FIRE

        self.history[task_id].append(now)
        return True

# Integration in the execution pipeline:
detector = RapidFireDetector(window_seconds=300, max_executions=5)

def execute_task(task_id: str, payload: dict):
    if not detector.record(task_id):
        logging.critical(f"RAPID-FIRE blocked: {task_id} executed {detector.max}+ times in {detector.window}s")
        return
    # ... actual execution
```

## 5. Fix

### 5.1 Immediate fix

Add a cooldown between two executions of the same task:

```python
import time

_last_execution: dict[str, float] = {}
COOLDOWN_SECONDS = 60

def should_execute(task_id: str) -> bool:
    """Block re-execution if the same task ran recently."""
    now = time.monotonic()
    last = _last_execution.get(task_id, 0)
    if now - last < COOLDOWN_SECONDS:
        return False
    _last_execution[task_id] = now
    return True
```

### 5.2 Robust fix

Implement an explicit state machine for each command:

```python
from enum import Enum
from datetime import datetime, timezone

class TaskState(Enum):
    PENDING = "pending"       # Command created, not yet read by the executor
    EXECUTING = "executing"   # Executor has started processing
    COMPLETED = "completed"   # Processing finished successfully
    FAILED = "failed"         # Processing failed
    EXPIRED = "expired"       # Timeout exceeded without execution

class TaskTracker:
    """Tracks the lifecycle of each task. Refresh only re-sends PENDING tasks."""

    def __init__(self, max_age_seconds: int = 120):
        self.tasks: dict[str, dict] = {}
        self.max_age = max_age_seconds

    def create(self, task_id: str, payload: dict):
        self.tasks[task_id] = {
            "state": TaskState.PENDING,
            "payload": payload,
            "created_at": datetime.now(timezone.utc),
            "executed_at": None,
            "completed_at": None,
        }

    def mark_executing(self, task_id: str):
        if task_id in self.tasks:
            self.tasks[task_id]["state"] = TaskState.EXECUTING
            self.tasks[task_id]["executed_at"] = datetime.now(timezone.utc)

    def mark_completed(self, task_id: str):
        if task_id in self.tasks:
            self.tasks[task_id]["state"] = TaskState.COMPLETED
            self.tasks[task_id]["completed_at"] = datetime.now(timezone.utc)

    def should_refresh(self, task_id: str) -> bool:
        """Only PENDING tasks should be refreshed. Never COMPLETED or EXECUTING."""
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task["state"] != TaskState.PENDING:
            return False
        # Expire old pending tasks
        age = (datetime.now(timezone.utc) - task["created_at"]).total_seconds()
        if age > self.max_age:
            task["state"] = TaskState.EXPIRED
            return False
        return True
```

## 6. Architectural Prevention

The prevention pattern is an **explicit state machine with a unique identifier per command**. Each command is assigned a UUID at creation time. The executor confirms execution by returning that UUID. The refresh only re-sends commands whose UUID has not been confirmed.

This architecture eliminates the problem at its root: there is no longer any ambiguity between "not yet executed" and "executed and completed". The UUID serves as proof of execution.

As a complement, an **execution budget per time window** acts as a safety net. Even if the state machine has a bug, the budget prevents more than N executions of the same task per hour. This is the last-resort circuit breaker.

Finally, the refresh mechanism should never be the only way to trigger an execution. If the executor needs a new task, it **requests** one (pull) rather than receiving it passively (push). Pull eliminates refresh zombies because the executor controls the pace.

## 7. Anti-patterns to Avoid

1. **Using a boolean to track command state.** `is_active = True/False` cannot distinguish 4 states (pending, executing, completed, failed). Use an enum or a string with explicit values.

2. **Deleting the command file after execution.** The absence of a file is ambiguous: "never created" or "created and deleted after execution"? Always keep a completion trace.

3. **Timer-based refresh with no memory.** "Every 10 seconds, check if a task is active" without tracking whether the task has already been executed is the recipe for rapid-fire.

4. **No execution limit per task.** Even with a correct state machine, a hard cap (e.g., max 3 executions per task) prevents infinite loops in case of a bug.

5. **Logging "execution succeeded" without counting.** If the log says "task executed successfully" 200 times in one hour for the same task, that is a bug — but nobody sees it without a counter.

## 8. Edge Cases and Variants

**Variant 1: Retry after failure that does not detect partial success.** The agent executes 80% of a task and fails on the remaining 20%. The retry restarts the entire task, including the 80% already done. Result: duplicated actions (emails sent twice, data inserted twice).

**Variant 2: Lost callback.** The executor completes the task and sends a "done" callback. The callback is lost (network issue, full queue, receiver crash). The orchestrator never receives the confirmation and re-sends. This requires an idempotency mechanism on the executor side.

**Variant 3: Temporal race condition.** The executor finishes at T=10.001s. The refresh triggers at T=10.000s, just before completion. It re-sends the command because at the time of the check, the task was still "in progress". This requires a lock between the refresh and the completion callback.

**Variant 4: Agent cascade.** Agent A triggers agent B. Agent B fails and returns a signal to agent A. Agent A re-triggers agent B. Agent B fails again. Without a depth limit, this is a two-actor loop.

## 9. Audit Checklist

- [ ] Every command/task has an explicit state machine (not a boolean)
- [ ] The refresh only re-sends tasks in PENDING state (never COMPLETED or EXECUTING)
- [ ] A cooldown or counter limits executions of the same task
- [ ] Monitoring counts executions per task and alerts if > N in T seconds
- [ ] Integration tests verify that completion → refresh does not re-execute

## 10. Further Reading

- Short pattern: [Pattern 02 — Rapid-Fire Loop](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-02)
- Related patterns: #09 (Agent Infinite Loop — loop at the agent level itself, not the refresh), #03 (Penalty Cascade — repeated re-executions can accumulate penalties)
- Recommended reading:
  - "Designing Data-Intensive Applications" (Martin Kleppmann, 2017), chapter 11 on idempotence and exactly-once processing
  - LangChain documentation on retry policies and RunnableWithFallbacks — where this exact pattern is documented as a known pitfall
