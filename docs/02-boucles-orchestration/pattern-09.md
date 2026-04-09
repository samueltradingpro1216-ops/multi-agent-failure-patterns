# Pattern #09 — Agent Infinite Loop

**Category:** Loops & Orchestration
**Severity:** Critical
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 0 to 1 day (financial consequences are immediate — the LLM bill explodes or the system crashes due to OOM/CPU saturation)

---

## 1. Observable Symptoms

An agent consumes **100% CPU** or exhausts the API call quota within minutes. Logs show the same message repeated hundreds of times. The LLM bill jumps from $5/day to $500 in an hour. The entire system slows down because the looping agent monopolizes shared resources (HTTP connection pool, rate limits, memory).

Unlike the Rapid-Fire Loop (Pattern #02), which is triggered by an **external refresh**, here it is the agent itself that re-invokes itself. The agent has a reflection, retry, or re-delegation mechanism that loops indefinitely:

```
Agent → "My response is not good enough" → re-invocation
     → "My response is not good enough" → re-invocation
     → "My response is not good enough" → re-invocation
     → ... (indefinitely)
```

The symptom is typically discovered via a **quota exceeded** error from the LLM provider, a Python `RecursionError`, or a system monitoring alert (CPU > 95% for > 5 minutes).

## 2. Field Story (anonymized)

A multi-agent system had a "reflection" agent that evaluated the quality of its own responses before forwarding them. If the quality score was below 90%, the agent would regenerate its response. The threshold was ambitious but reasonable for simple cases.

On a Friday evening, a user submitted an ambiguous request for which no perfect answer existed. The agent generated a response (score: 72%), evaluated it, regenerated (score: 68%), re-evaluated, regenerated (score: 74%)... The score oscillated around 70%, never reaching 90%. Within 15 minutes, the agent had made 2,000 LLM calls, exhausted the daily quota, and the provider bill showed $340.

The system had no depth counter, no global timeout, and no per-agent maximum budget. The agent was "free to retry as many times as it wanted" — a freedom that proved dangerous.

## 3. Technical Root Cause

The agent has an **unbounded** re-invocation mechanism. Three common variants:

**Variant 1: Direct recursion.** The agent calls itself:

```python
class ReflectionAgent:
    def run(self, task: str) -> str:
        response = self.llm.generate(task)
        quality = self.evaluate(response)

        if quality < 0.9:
            return self.run(task)  # Unbounded recursion

        return response
```

**Variant 2: Delegation loop.** Agent A delegates to agent B, which re-delegates to agent A:

```
Agent A → "I don't know, let's ask B" → Agent B
Agent B → "I don't know, let's ask A" → Agent A
→ Infinite ping-pong between A and B
```

**Variant 3: Side-effect that re-triggers.** A cron job launches an agent. The agent writes a file. The cron job detects the new file and relaunches the agent:

```
Cron → launches Agent → Agent writes output.json → Cron detects change → relaunches Agent
```

The fundamental problem is the absence of a **guaranteed termination condition**. The agent can always find a reason to retry, and nothing prevents it from doing so.

## 4. Detection

### 4.1 Manual code audit

Search for recursive calls and unbounded loops:

```bash
# Direct recursion: method that calls itself
grep -rn "def \(\w\+\).*:" --include="*.py" | while read line; do
    func=$(echo "$line" | grep -oP "def \K\w+")
    file=$(echo "$line" | cut -d: -f1)
    if grep -q "self\.$func\|$func(" "$file" 2>/dev/null; then
        echo "RECURSION: $line"
    fi
done

# while True loops without a clear conditional break
grep -rn "while True\|while 1:" --include="*.py" -A5 | grep -v "break"

# Agents that re-invoke themselves via a dispatch
grep -rn "dispatch\|invoke\|delegate\|re.*run" --include="*.py"
```

### 4.2 Automated CI/CD

Test that every agent has a max_depth and a timeout:

```python
def test_all_agents_have_depth_limit():
    """Every agent that can recurse must have a max_depth parameter."""
    from agents import ALL_AGENTS

    for agent_cls in ALL_AGENTS:
        sig = inspect.signature(agent_cls.run)
        has_depth = "max_depth" in sig.parameters or "depth" in sig.parameters
        has_timeout = "timeout" in sig.parameters

        assert has_depth or has_timeout, (
            f"{agent_cls.__name__}.run() has no depth/timeout parameter. "
            f"Add max_depth or timeout to prevent infinite loops."
        )
```

### 4.3 Runtime production

Circuit breaker + invocation monitor:

```python
import time
from collections import defaultdict

class AgentCircuitBreaker:
    """Cuts an agent after N invocations in T seconds."""

    def __init__(self, max_invocations: int = 10, window_seconds: int = 60, cooldown_seconds: int = 300):
        self.max = max_invocations
        self.window = window_seconds
        self.cooldown = cooldown_seconds
        self.history: dict[str, list[float]] = defaultdict(list)
        self.tripped_at: dict[str, float] = {}

    def can_invoke(self, agent_id: str) -> bool:
        now = time.monotonic()

        # Check cooldown
        if agent_id in self.tripped_at:
            if now - self.tripped_at[agent_id] < self.cooldown:
                return False
            del self.tripped_at[agent_id]

        # Clean old entries
        cutoff = now - self.window
        self.history[agent_id] = [t for t in self.history[agent_id] if t > cutoff]

        if len(self.history[agent_id]) >= self.max:
            self.tripped_at[agent_id] = now
            return False

        self.history[agent_id].append(now)
        return True
```

## 5. Fix

### 5.1 Immediate fix

Add a depth counter:

```python
def run(self, task: str, _depth: int = 0, max_depth: int = 3) -> str:
    if _depth >= max_depth:
        return self.last_response or "Max depth reached"

    response = self.llm.generate(task)
    quality = self.evaluate(response)
    self.last_response = response

    if quality < 0.9:
        return self.run(task, _depth=_depth + 1, max_depth=max_depth)

    return response
```

### 5.2 Robust fix

Combine three mechanisms: depth, timeout, and budget:

```python
import time

class SafeAgent:
    """Agent with triple anti-loop protection: depth, timeout, budget."""

    def __init__(self, max_depth: int = 5, timeout_seconds: float = 30.0, max_llm_calls: int = 20):
        self.max_depth = max_depth
        self.timeout = timeout_seconds
        self.max_calls = max_llm_calls
        self.call_count = 0
        self.start_time = None

    def run(self, task: str, _depth: int = 0) -> str:
        if self.start_time is None:
            self.start_time = time.monotonic()

        # Guard 1: depth
        if _depth >= self.max_depth:
            return f"[DEPTH_LIMIT] Best effort after {_depth} iterations"

        # Guard 2: timeout
        elapsed = time.monotonic() - self.start_time
        if elapsed > self.timeout:
            return f"[TIMEOUT] Stopped after {elapsed:.1f}s"

        # Guard 3: budget
        self.call_count += 1
        if self.call_count > self.max_calls:
            return f"[BUDGET_EXHAUSTED] Stopped after {self.call_count} LLM calls"

        response = self.generate(task)
        quality = self.evaluate(response)

        if quality < 0.9:
            return self.run(task, _depth=_depth + 1)

        return response
```

## 6. Architectural Prevention

Prevention is grounded in the principle of **bounded execution**: no agent can run indefinitely, regardless of inputs or conditions.

**1. Per-agent budget.** Each agent receives an execution budget (tokens, API calls, CPU seconds) at creation time. When the budget is exhausted, the agent returns its best result and stops. The budget is managed by the framework, not by the agent itself.

**2. Separation of reflection and execution.** The agent that generates a response is not the same as the one that decides whether to regenerate. An external "judge" decides whether the quality is sufficient. The judge has its own budget (typically 2–3 evaluations maximum).

**3. Worst-case return.** Every agent must be able to return a result at any time, even if that result is imperfect. "Best effort after N iterations" is always preferable to "infinite loop in pursuit of perfection".

## 7. Anti-patterns to Avoid

1. **Recursion without max_depth.** `self.run(task)` without a counter is a time bomb. Always pass `_depth + 1` and check it at the top.

2. **Unreachable quality threshold.** If the average score is 70% and the threshold is 95%, the agent will almost always loop. The threshold must be calibrated against the system's real observed scores.

3. **Agent that catches its own errors and retries.** `except Exception: return self.run(task)` — every error triggers a retry, which can generate a new error, which retries...

4. **No invocation monitoring.** If nobody counts how many times an agent executes per minute, loops go unnoticed until the bill arrives.

5. **Trusting Python's `sys.setrecursionlimit()`.** The default (1000) is high for recursive code. An LLM agent that loops 1,000 times before hitting `RecursionError` will have already made 1,000 API calls.

## 8. Edge Cases and Variants

**Variant 1: Multi-agent loop.** Three agents form a cycle: A → B → C → A. Each has an individual max_depth, but the full cycle has no limit. Requires a **global depth** counter that propagates across delegations.

**Variant 2: Asynchronous loop.** The agent posts a message to a queue; a worker processes it and posts the result to another queue that the agent is listening on. The loop passes through external infrastructure (the queue) and is not visible as recursion in the code.

**Variant 3: File-based loop.** The agent writes a file, a file watcher detects the change and relaunches the agent. The loop passes through the filesystem and is not detectable by static code analysis.

**Variant 4: Slow loop.** The agent does not loop in milliseconds but once per minute. Over 24 hours it has made 1,440 LLM calls. A 5-minute monitoring window detects nothing (1 call/minute = below the threshold).

## 9. Audit Checklist

- [ ] Every recursive agent has a max_depth parameter (default: 3–5)
- [ ] Every agent has a global timeout (default: 30–60 seconds)
- [ ] An LLM call budget per agent is defined and monitored
- [ ] A circuit breaker cuts agents that exceed N invocations in T seconds
- [ ] Delegation cycles between agents are identified and bounded

## 10. Further Reading

- Corresponding short pattern: [Pattern 09 — Agent Infinite Loop](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-09)
- Related patterns: #02 (Rapid-Fire Loop — loop triggered by external refresh, not by the agent), #10 (Survival Mode Deadlock — an unreachable threshold causing the loop)
- Recommended reading:
  - LangGraph documentation on "recursion limits" — the framework has a `recursion_limit` parameter specifically for this pattern
  - "Building LLM Powered Applications" (Valentino Gagliardi, 2024) — chapter on guardrails for recursive agents
