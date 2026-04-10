# Pattern #32 — Iterate-Then-Skip

**Category:** Loops & Orchestration
**Severity:** Low
**Tags:** `loops`, `orchestration`, `filtering`, `performance`, `log-noise`

---

## 1. Observable Symptoms

The following symptoms appear individually or in combination. None alone is conclusive, but two or more appearing together strongly suggest this pattern.

**Log noise.** The application log fills with repetitive entries such as `"skipping disabled agent: bid_agent_07"` or `"agent inactive, continuing"`. These messages are emitted inside the main loop body, after iteration has already begun, rather than before the loop runs. When log aggregators are configured to alert on warning-level messages, these false signals create alert fatigue.

**Wasted CPU in profiling output.** A profiler trace shows that the top-of-loop guard (`if not agent.is_active: continue`) consumes a measurable fraction of total cycle time despite performing no useful work. In a cycle that runs every 10 seconds with 20 registered agents of which 5 are disabled, roughly 25–30% of the iteration budget is spent on agents that contribute zero output.

**Monitoring dashboards report inflated agent counts.** Metrics emitted inside the loop — such as `agents_processed_total` — count disabled agents alongside active ones. Downstream dashboards then report processing rates that are inconsistent with actual throughput, causing confusion during incident triage.

**Cycle drift under load.** When the scheduler is tight (cycle period close to average cycle duration), the overhead of iterating disabled agents causes occasional cycle overruns. The overruns appear intermittently and do not correlate with any increase in real work, making them difficult to diagnose without a profiler.

**Dead code accumulates around the skip block.** Because the skip is inside the loop, developers often add secondary guards, logging calls, and diagnostic counters around it. Over time this creates a cluster of code that exists solely to handle items that should never have entered the loop in the first place.

---

## 2. Field Story

A real-time bidding platform operated a multi-agent orchestration loop that ran on a 10-second cycle. Each cycle, the loop coordinator fetched the full agent registry — a list of 20 bid agents — and iterated over every entry. At the top of the loop body, a guard checked `agent.status == "active"` and issued a `continue` if the agent was disabled or suspended.

The platform had a standard operational procedure: when a demand partner reduced their budget, their associated bid agent was set to `"disabled"` in the registry but never removed. This kept the historical record intact and allowed quick reactivation. Over six months, 5 of the 20 agents accumulated in the disabled state.

During a routine performance audit, the on-call engineer noticed that the monitoring dashboard reported `agents_evaluated: 20` every cycle, but the bid submission log only ever showed activity from 15 agents. The discrepancy triggered a misrouted incident ticket claiming "5 agents are silently failing to submit bids."

Investigation revealed there was no failure. The loop was iterating all 20 agents, emitting a `WARN`-level log line for each of the 5 disabled ones, incrementing the `agents_evaluated` counter for all 20, and then submitting bids only for the 15 active ones. The fix — pre-filtering to `active_agents` before the loop — reduced the counter to 15, eliminated the spurious warnings, and recovered approximately 28% of per-cycle CPU time in the orchestrator process.

The incident also revealed that the alerting rule on `agents_evaluated < 20` would have paged the on-call engineer unnecessarily after the fix was deployed. The alerting threshold had been set to the total registry count rather than the expected active count.

---

## 3. Technical Root Cause

The root cause is a category error: the loop is used both as an iteration mechanism and as a filter. These are distinct concerns that should be separated.

In Python, `for item in collection` expresses "process every element of this collection." When the first line of the loop body is `if not item.should_process(): continue`, the author has acknowledged that not every element should be processed — but has not acted on that acknowledgment at the collection level.

```python
# Buggy pattern: filter inside loop body
def run_cycle(agents: list[Agent]) -> None:
    for agent in agents:                          # iterates ALL agents
        if agent.status != "active":              # skip happens AFTER iteration starts
            logger.warning("skipping disabled agent: %s", agent.id)
            continue
        result = agent.execute_bid()
        submit(result)
```

The iteration overhead is O(n_total) while the useful work is O(n_active). When n_disabled grows — as it does on any long-running platform — the ratio worsens over time without any code change.

A secondary cause is that logging the skip is treated as a useful diagnostic. In practice it is not: the disabled state is an expected operational condition, not an anomaly. Logging it at WARNING level promotes noise over signal. If any log record is warranted, it belongs at DEBUG level and ideally only once per state change, not once per cycle.

The tertiary cause is metric design: incrementing `agents_evaluated` for skipped agents conflates "entered the loop" with "did work," producing metrics that mislead dashboard consumers.

---

## 4. Detection

### 4.1 Manual Code Audit

Search for loops where the first substantive statement is a conditional `continue`. The pattern is most dangerous when:

- The skipped items are expected in normal operation (not error cases).
- The skip is accompanied by a log call at WARNING or INFO level.
- The collection iterated is fetched from an external source (database, registry, config file) rather than being pre-built by the caller.

Review every loop of the form:

```python
for item in fetch_all_items():
    if <steady-state-condition>:
        continue
    # ... real work
```

Ask: could `fetch_all_items()` be replaced by `fetch_active_items()`? If yes, the filter belongs at the source.

### 4.2 Automated CI/CD

Use an AST-based linter rule (e.g., a custom `pylint` checker or a `semgrep` rule) to flag loops where the first statement is a guard `continue` and the iterated collection name contains words like `all`, `full`, `every`, or `registry`.

```yaml
# semgrep rule: detect iterate-then-skip
rules:
  - id: iterate-then-skip
    patterns:
      - pattern: |
          for $ITEM in $COLLECTION:
              if $CONDITION:
                  continue
              ...
    message: >
      Loop iterates then immediately skips items. Consider pre-filtering
      $COLLECTION before the loop to avoid wasted iteration overhead.
    languages: [python]
    severity: WARNING
```

Add a metric assertion in integration tests: after a cycle runs, the counter `agents_evaluated` must equal the number of agents in the `active` state, not the total registry count.

### 4.3 Runtime Production

Emit two separate metrics: `agents_in_registry` (total) and `agents_evaluated` (processed without skip). Alert if `agents_evaluated / agents_in_registry < threshold` for an extended period, which indicates registry bloat. Add a periodic reconciliation job that logs (at INFO level, once per run) how many items in the registry are in each state. This surfaces disabled-item accumulation before it becomes a performance problem.

---

## 5. Fix

### 5.1 Immediate

Pre-filter the collection before the loop. This is a one-line change at the call site:

```python
def run_cycle(agents: list[Agent]) -> None:
    active_agents = [a for a in agents if a.status == "active"]
    for agent in active_agents:
        result = agent.execute_bid()
        submit(result)
```

If the collection is fetched from a data source, push the filter to the query:

```python
# Before: fetches all, filters in Python
agents = registry.get_all_agents()

# After: filter at the source
agents = registry.get_agents(status="active")
```

Remove the WARNING-level log for the skip. If operational visibility into disabled agents is needed, log a single INFO summary before the loop: `logger.info("cycle start: %d active / %d total agents", len(active), len(all_agents))`.

### 5.2 Robust

Introduce a typed filter at the registry layer so callers cannot accidentally fetch disabled items without explicitly opting in:

```python
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class AgentStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    SUSPENDED = "suspended"


@dataclass
class Agent:
    id: str
    status: AgentStatus
    config: dict = field(default_factory=dict)

    def execute_bid(self) -> dict:
        # Simulate bid execution
        return {"agent_id": self.id, "bid": 1.0}


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        self._agents[agent.id] = agent

    def get_active(self) -> list[Agent]:
        """Return only agents in ACTIVE status. This is the default accessor."""
        return [a for a in self._agents.values() if a.status == AgentStatus.ACTIVE]

    def get_all(self, *, include_inactive: bool = False) -> list[Agent]:
        """
        Return all agents. Callers must explicitly pass include_inactive=True
        to receive disabled or suspended agents. This prevents accidental
        full-registry iteration in hot paths.
        """
        if include_inactive:
            return list(self._agents.values())
        return self.get_active()

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for agent in self._agents.values():
            counts[agent.status] = counts.get(agent.status, 0) + 1
        return counts


def run_cycle(registry: AgentRegistry, metrics: dict) -> None:
    """
    Orchestration loop. Iterates only active agents.
    Registry summary is logged once per cycle at INFO level.
    """
    active_agents = registry.get_active()
    summary = registry.summary()
    logger.info(
        "cycle start: %d active / %d total agents | distribution: %s",
        len(active_agents),
        sum(summary.values()),
        summary,
    )

    metrics["agents_evaluated"] = 0

    for agent in active_agents:
        result = agent.execute_bid()
        submit(result, metrics)

    logger.info("cycle complete: %d bids submitted", metrics["agents_evaluated"])


def submit(result: dict, metrics: dict) -> None:
    metrics["agents_evaluated"] = metrics.get("agents_evaluated", 0) + 1
    logger.debug("submitted bid from agent %s", result["agent_id"])


# --- demonstration ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    registry = AgentRegistry()
    for i in range(1, 16):
        registry.register(Agent(id=f"bid_agent_{i:02d}", status=AgentStatus.ACTIVE))
    for i in range(16, 21):
        registry.register(Agent(id=f"bid_agent_{i:02d}", status=AgentStatus.DISABLED))

    metrics: dict = {}
    run_cycle(registry, metrics)
    # Output: cycle start: 15 active / 20 total agents
    # No "skipping disabled agent" warnings emitted.
    assert metrics["agents_evaluated"] == 15
```

---

## 6. Architectural Prevention

**Filter at the source, not at the consumer.** Every data source that vends collections (database queries, registry lookups, config loaders) should expose status-filtered accessors as the default API. Unfiltered accessors should require an explicit opt-in parameter (`include_inactive=True`) and should be documented as "for administrative use only."

**Separate registry from execution list.** Maintain two data structures: a persistent registry (all items, all statuses, full history) and an execution manifest (only items eligible to run in the current cycle). The manifest is derived from the registry once per cycle or on change, not inline during the loop.

**Metrics must reflect work done, not items visited.** Define counter semantics precisely in a metrics contract document. A counter named `agents_evaluated` should increment only when an agent performs real work. A separate gauge `agents_in_registry_by_status{status="disabled"}` tracks the disabled count without conflating it with throughput.

**Periodic registry hygiene.** Implement a background job that archives agents that have been in a non-active state for longer than a configurable retention window (e.g., 90 days). This prevents the disabled population from growing unboundedly, which is the condition that turns a minor inefficiency into a measurable performance problem.

---

## 7. Anti-patterns to Avoid

**Do not log inside the skip block at WARNING level.** Disabled status is an expected operational state. WARNING implies something unexpected has occurred. Use INFO or DEBUG. If you use INFO, emit it once per cycle in an aggregate summary, not once per skipped item.

**Do not increment throughput metrics for skipped items.** Every counter that lives inside the loop body will count skipped items unless explicitly guarded. Place metric emissions after the pre-filter, not before it.

**Do not rely on the skip-continue pattern as a substitute for data hygiene.** Adding a `continue` guard is a workaround, not a fix. Over time, the number of items that hit the guard grows if no cleanup policy exists.

**Do not pre-filter with a list comprehension inside the loop condition.** The construct `for agent in [a for a in agents if a.is_active]` iterates the full list twice: once to build the filtered list, once for the loop. Call `get_active()` once and assign to a variable.

**Do not use the same metric name for "items seen" and "items processed."** These are different quantities. Name them distinctly.

---

## 8. Edge Cases and Variants

**Status transitions mid-cycle.** If an agent's status can change from active to disabled while the cycle is running (e.g., due to a concurrent admin operation), the pre-filtered list becomes stale. Mitigate by taking a snapshot of active agents at cycle start and logging a reconciliation warning if an agent's status has changed by the time the loop body executes.

**Dynamic registry growth.** If new agents are registered during a running cycle, they should not be included in the current cycle's execution manifest. The snapshot approach handles this naturally: fetch once, iterate the snapshot.

**All agents disabled.** When `get_active()` returns an empty list, the loop body never executes. Ensure the cycle's health check emits a clear metric (`active_agent_count = 0`) rather than silently completing with no bids submitted. An empty active set is likely an operational error that should trigger an alert.

**Hierarchical skip conditions.** Some loops have multiple levels of skip: disabled, suspended, rate-limited, quota-exceeded. Each level added to the loop body increases the iterate-then-skip overhead. The correct response is to consolidate all eligibility checks into a single `is_eligible()` predicate that is applied at the pre-filter stage.

**Legacy code that cannot change the data source.** If the registry query cannot be modified (e.g., it is provided by a third-party SDK), apply the pre-filter immediately after the fetch: `active = [a for a in sdk.get_all_agents() if a.status == "active"]`. This is inferior to filtering at the source but superior to filtering inside the loop body.

---

## 9. Audit Checklist

- [ ] Every `for` loop that operates on a collection fetched from an external source begins with a pre-filter, not an in-loop `if ... continue`.
- [ ] No WARNING or INFO log lines are emitted inside a loop body for items in a normal, expected non-processing state.
- [ ] Throughput metrics (items evaluated, bids submitted, tasks completed) count only items that performed real work, not all items iterated.
- [ ] Each registry or collection accessor has a clearly named "active only" variant that is the default; the "all items" variant requires explicit opt-in.
- [ ] A data retention or archival policy exists for items in non-active states, preventing unbounded registry growth.
- [ ] Integration tests assert that metric counters equal the active item count, not the total registry count.
- [ ] The cycle health check emits a separate gauge for `active_count` distinct from `total_count`.
- [ ] No list comprehension inside a loop condition (double iteration): pre-filter is assigned to a variable before the loop.
- [ ] Mid-cycle status change behavior is documented and tested (snapshot semantics vs. live query semantics).
- [ ] Alert thresholds on item-count metrics are based on expected active counts, not total registry counts.

---

## 10. Further Reading

- Repository with annotated code examples for all patterns in this series: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns)
- Python documentation on iterators and generator expressions: [docs.python.org/3/glossary.html#term-iterator](https://docs.python.org/3/glossary.html#term-iterator)
- `itertools.filterfalse` and `filter()` as alternatives to list-comprehension pre-filtering for large collections: [docs.python.org/3/library/itertools.html](https://docs.python.org/3/library/itertools.html)
- Semgrep rule writing guide for AST-based pattern detection: [semgrep.dev/docs/writing-rules/overview](https://semgrep.dev/docs/writing-rules/overview)
- "USE method" for metrics design (Utilization, Saturation, Errors) — Brendan Gregg: [brendangregg.com/usemethod.html](https://www.brendangregg.com/usemethod.html)
