# Pattern #23 — Orphan Commands to Disabled Agents

**Category:** Loops & Orchestration
**Severity:** Low
**Affected Frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average Debugging Time If Undetected:** 3 to 14 days

---

## 1. Observable Symptoms

The following symptoms are individually ambiguous but form a recognizable cluster when two or more are present simultaneously.

- **Metrics divergence: commands sent > commands executed, persistently and without catching up.** A dashboard shows "commands dispatched: 4 800" and "commands acknowledged: 2 400" over the same 24-hour window. The gap widens linearly over time rather than resolving.
- **Queue depth grows monotonically without bound.** A message queue, a file-based command directory, or an in-memory deque grows at a rate proportional to the orchestrator's dispatch frequency. Consumer throughput is zero for the affected queue partitions.
- **Disk usage climbs steadily on nodes that host command queues or log files.** Operators receive low-disk alerts for partitions they did not expect to fill. Inspection reveals directories containing thousands of unprocessed command files with sequential filenames and recent creation timestamps.
- **Log files contain repeated "dispatching command" entries with no corresponding "command received" or "command executed" entries.** The receiving side is silent while the sending side is active.
- **Monitoring shows zero error rate on the consumer side.** Because the disabled agents are not running, they are not reporting errors. An absence of consumer errors coinciding with a build-up of unprocessed work is a signature symptom of this pattern.
- **Orchestrator health checks pass.** The orchestrator itself is functioning correctly. This is precisely why the pattern is difficult to detect: the system appears healthy from the orchestrator's perspective.
- **Latency for completed work increases gradually.** When the queue eventually shares resources with active agents (shared memory pool, shared disk partition, shared network buffer), the growing backlog degrades throughput for the agents that are still running.

---

## 2. Field Story (Anonymized)

A regional telecommunications company operated a multi-agent customer support system. The system consisted of a central orchestrator and a fleet of specialized agents: a billing inquiry agent, a technical support agent, a retention offer agent, and a complaint escalation agent. The orchestrator classified incoming support tickets and dispatched structured commands to the appropriate agent queue. Each agent ran as an independent process, polling its queue for new commands.

The company's support team decided to temporarily disable the retention offer agent while the legal team reviewed the terms of a new promotional offer. The agent process was stopped by the operations team and its systemd service was disabled. The operations team did not notify the orchestrator development team, who were in a separate department, and no change was made to the orchestrator's routing configuration.

The orchestrator continued classifying tickets for retention-related inquiries and dispatching commands to the retention agent's queue directory: `/var/app/agents/retention/queue/`. Because the agent process was stopped, no process was consuming from that directory. The files accumulated silently.

For the first three days, the disk usage alert threshold was not breached, so no alert fired. On day four, a disk usage alert triggered on the application server. The operations team investigated the alert and identified the queue directory as the source of growth — it contained over 14 000 unprocessed command files totalling 2.3 GB. They deleted the files without examining their contents, which resolved the disk alert.

The retention agent was re-enabled eleven days after the initial disablement, once the legal review completed. However, none of the 14 000 customers who had been identified as retention candidates during that period were ever contacted, because the commands had been deleted. This translated into measurable churn during the affected cohort's subsequent 30-day period.

The root causes were: (1) the orchestrator had no mechanism to detect that a target agent was not consuming its queue; (2) the queue had no retention policy or overflow handler; and (3) the disablement process had no cross-team communication step.

---

## 3. Technical Root Cause

Orphan command accumulation arises from an architectural assumption that is rarely made explicit: **the orchestrator assumes that every agent it can address is active and consuming**. This assumption is almost always unstated and untested.

**Root cause A — No agent liveness check before dispatch.**
The orchestrator selects a target agent based on routing logic (classification score, round-robin, capability matching) and dispatches the command immediately. It does not verify that the target is alive and consuming before writing to the queue.

**Root cause B — Fire-and-forget queue semantics.**
File-based queues, unbounded in-memory queues, and some message broker configurations operate with fire-and-forget semantics: the producer receives no acknowledgment that a consumer exists or that the message was processed. From the producer's perspective, the write always succeeds.

**Root cause C — No dead-letter or expiry policy on the queue.**
Without a time-to-live (TTL) on messages or a dead-letter queue (DLQ) for unacknowledged messages, orphan commands accumulate indefinitely. The queue provides no back-pressure signal to the orchestrator.

**Root cause D — Agent registry not synchronized with the operational state of agent processes.**
The orchestrator maintains a registry of known agents (a configuration file, a service discovery record, or an environment variable). When an agent is disabled, the registry is not updated. The orchestrator continues to treat the agent as addressable.

**Root cause E — No visibility into per-agent queue depth in the monitoring layer.**
Monitoring is configured at the aggregate level ("total commands dispatched") rather than at the per-agent level ("commands dispatched to retention agent: 4 800; commands consumed by retention agent: 0"). The divergence is invisible at the level of granularity that is actually monitored.

---

## 4. Detection

### 4.1 Manual Code Audit

Examine the orchestrator's dispatch function and identify whether it performs any liveness check before writing to a queue:

```python
# UNSAFE pattern: dispatches without any liveness check
def dispatch_command(agent_id: str, command: dict) -> None:
    queue_path = f"/var/app/agents/{agent_id}/queue/"
    command_file = os.path.join(queue_path, f"{uuid.uuid4()}.json")
    with open(command_file, "w") as f:
        json.dump(command, f)
    # Returns immediately; no confirmation that agent is running
```

Inspect the agent registry for staleness indicators:

```python
# Audit: does the registry reflect operational state or only configuration intent?
# A registry that is only written at deployment time and never updated
# when agents are stopped is a structural gap.
AGENT_REGISTRY = {
    "billing": {"queue": "/var/app/agents/billing/queue/", "enabled": True},
    "technical": {"queue": "/var/app/agents/technical/queue/", "enabled": True},
    "retention": {"queue": "/var/app/agents/retention/queue/", "enabled": True},
    # "enabled: True" was set at deploy time and is never updated when
    # the agent process is stopped by operations
}
```

Scan queue directories for file age and accumulation patterns:

```bash
# Manual check: find command files older than 10 minutes in all agent queues
find /var/app/agents/*/queue/ -name "*.json" -mmin +10 | wc -l

# Per-agent breakdown
for agent_dir in /var/app/agents/*/queue/; do
    count=$(find "$agent_dir" -name "*.json" -mmin +10 | wc -l)
    echo "$agent_dir: $count stale files"
done
```

### 4.2 Automated CI/CD

Add a configuration validation step that verifies each agent listed in the registry has a reachable health endpoint defined:

```python
# ci_checks/validate_agent_registry.py
import json
import sys
import pathlib


REQUIRED_AGENT_FIELDS = {"queue", "enabled", "health_check_url", "max_queue_depth"}


def validate_registry(registry_path: str) -> list[str]:
    """Return a list of validation errors for the agent registry file."""
    errors = []
    registry = json.loads(pathlib.Path(registry_path).read_text(encoding="utf-8"))

    for agent_id, config in registry.items():
        missing = REQUIRED_AGENT_FIELDS - set(config.keys())
        if missing:
            errors.append(
                f"Agent '{agent_id}' is missing required fields: {sorted(missing)}"
            )
        if config.get("max_queue_depth") is None:
            errors.append(
                f"Agent '{agent_id}' has no max_queue_depth; "
                "queue overflow is unguarded."
            )
        if not config.get("health_check_url", "").startswith("http"):
            errors.append(
                f"Agent '{agent_id}' has no valid health_check_url; "
                "liveness checks cannot be performed."
            )
    return errors


if __name__ == "__main__":
    registry_path = sys.argv[1] if len(sys.argv) > 1 else "config/agent_registry.json"
    errors = validate_registry(registry_path)
    if errors:
        print("Agent registry validation failed:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    print("Agent registry validation passed.")
    sys.exit(0)
```

### 4.3 Runtime Production

Deploy a queue depth monitor that alerts when any agent's queue grows beyond a threshold without being consumed:

```python
# monitoring/queue_depth_monitor.py
import os
import time
import logging
import json
import pathlib
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentQueueState:
    agent_id: str
    queue_path: str
    max_queue_depth: int
    previous_depth: Optional[int] = None
    consecutive_growth_checks: int = 0


def count_queue_files(queue_path: str) -> int:
    """Count unprocessed command files in a file-based queue directory."""
    try:
        return sum(1 for f in pathlib.Path(queue_path).glob("*.json"))
    except FileNotFoundError:
        return 0


def check_queue_health(states: list[AgentQueueState]) -> list[str]:
    """
    Check each agent's queue for growth without consumption.
    Returns a list of alert messages for any queues that are unhealthy.
    """
    alerts = []
    for state in states:
        current_depth = count_queue_files(state.queue_path)

        if state.previous_depth is not None:
            if current_depth > state.previous_depth:
                state.consecutive_growth_checks += 1
            else:
                state.consecutive_growth_checks = 0

        if current_depth > state.max_queue_depth:
            alerts.append(
                f"ALERT: Agent '{state.agent_id}' queue depth {current_depth} "
                f"exceeds maximum {state.max_queue_depth}. "
                "Agent may be disabled or crashed."
            )
        elif state.consecutive_growth_checks >= 3:
            alerts.append(
                f"WARNING: Agent '{state.agent_id}' queue has grown for "
                f"{state.consecutive_growth_checks} consecutive checks "
                f"(current depth: {current_depth}). No consumption detected."
            )

        state.previous_depth = current_depth
    return alerts


def load_registry(registry_path: str) -> list[AgentQueueState]:
    registry = json.loads(pathlib.Path(registry_path).read_text(encoding="utf-8"))
    return [
        AgentQueueState(
            agent_id=agent_id,
            queue_path=config["queue"],
            max_queue_depth=config.get("max_queue_depth", 1000),
        )
        for agent_id, config in registry.items()
        if config.get("enabled", True)
    ]


def run_monitor(registry_path: str, check_interval_seconds: int = 60) -> None:
    states = load_registry(registry_path)
    logger.info("Queue depth monitor started. Watching %d agents.", len(states))
    while True:
        alerts = check_queue_health(states)
        for alert in alerts:
            logger.warning(alert)
        time.sleep(check_interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_monitor("config/agent_registry.json")
```

---

## 5. Fix

### 5.1 Immediate

Identify all queues associated with disabled agents and halt dispatch to them immediately. For file-based queues, move (do not delete) the accumulated files to an archive directory for later analysis:

```python
# immediate_fix/drain_orphan_queues.py
import pathlib
import shutil
import datetime
import logging
import json

logger = logging.getLogger(__name__)


def archive_orphan_queue(
    agent_id: str,
    queue_path: str,
    archive_base: str = "/var/app/archive/orphan_queues",
) -> int:
    """
    Move all files from an orphaned queue to a dated archive directory.
    Returns the number of files archived.
    """
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    archive_dir = pathlib.Path(archive_base) / agent_id / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    queue = pathlib.Path(queue_path)
    archived = 0
    for cmd_file in queue.glob("*.json"):
        shutil.move(str(cmd_file), str(archive_dir / cmd_file.name))
        archived += 1

    logger.info(
        "Archived %d orphan command files from agent '%s' to %s",
        archived, agent_id, archive_dir,
    )
    return archived


def disable_agent_in_registry(
    registry_path: str, agent_id: str
) -> None:
    """Update the agent registry to mark an agent as disabled."""
    registry_file = pathlib.Path(registry_path)
    registry = json.loads(registry_file.read_text(encoding="utf-8"))
    if agent_id in registry:
        registry[agent_id]["enabled"] = False
        registry_file.write_text(
            json.dumps(registry, indent=2), encoding="utf-8"
        )
        logger.info(
            "Agent '%s' marked as disabled in registry %s", agent_id, registry_path
        )
    else:
        logger.warning("Agent '%s' not found in registry.", agent_id)
```

Update the orchestrator's dispatch function to check the registry before writing:

```python
# immediate_fix/guarded_dispatch.py
import os
import uuid
import json
import logging

logger = logging.getLogger(__name__)


def dispatch_command_guarded(
    agent_id: str,
    command: dict,
    registry: dict,
) -> bool:
    """
    Dispatch a command only if the target agent is enabled in the registry.
    Returns True if dispatched, False if suppressed.
    """
    agent_config = registry.get(agent_id)
    if agent_config is None:
        logger.error(
            "Dispatch suppressed: agent '%s' is not in registry.", agent_id
        )
        return False

    if not agent_config.get("enabled", True):
        logger.warning(
            "Dispatch suppressed: agent '%s' is marked disabled in registry.",
            agent_id,
        )
        return False

    queue_path = agent_config["queue"]
    command_file = os.path.join(queue_path, f"{uuid.uuid4()}.json")
    with open(command_file, "w", encoding="utf-8") as fh:
        json.dump(command, fh)

    logger.debug("Command dispatched to agent '%s': %s", agent_id, command_file)
    return True
```

### 5.2 Robust

Implement a full dispatch pipeline with liveness verification, TTL-based expiry, a dead-letter queue, and back-pressure limiting:

```python
# robust_fix/resilient_dispatcher.py
import os
import uuid
import json
import time
import logging
import pathlib
import datetime
import requests
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    success: bool
    agent_id: str
    command_id: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class AgentConfig:
    agent_id: str
    queue_path: str
    health_check_url: str
    enabled: bool = True
    max_queue_depth: int = 500
    ttl_seconds: int = 3600          # Commands expire after 1 hour if not consumed
    health_check_timeout: float = 2.0


class ResilientDispatcher:
    def __init__(
        self,
        registry: dict[str, AgentConfig],
        dlq_path: str = "/var/app/dlq/",
        liveness_cache_ttl: float = 30.0,
    ):
        self.registry = registry
        self.dlq_path = pathlib.Path(dlq_path)
        self.dlq_path.mkdir(parents=True, exist_ok=True)
        self.liveness_cache_ttl = liveness_cache_ttl
        self._liveness_cache: dict[str, tuple[bool, float]] = {}

    def _is_agent_alive(self, config: AgentConfig) -> bool:
        """
        Check agent liveness via HTTP health endpoint.
        Results are cached for `liveness_cache_ttl` seconds to avoid
        health-check storms.
        """
        cached = self._liveness_cache.get(config.agent_id)
        if cached is not None:
            result, timestamp = cached
            if time.monotonic() - timestamp < self.liveness_cache_ttl:
                return result

        alive = False
        try:
            resp = requests.get(
                config.health_check_url,
                timeout=config.health_check_timeout,
            )
            alive = resp.status_code == 200
        except requests.RequestException as exc:
            logger.warning(
                "Health check failed for agent '%s': %s", config.agent_id, exc
            )

        self._liveness_cache[config.agent_id] = (alive, time.monotonic())
        return alive

    def _queue_depth(self, config: AgentConfig) -> int:
        return sum(1 for _ in pathlib.Path(config.queue_path).glob("*.json"))

    def _send_to_dlq(
        self,
        agent_id: str,
        command: dict,
        command_id: str,
        reason: str,
    ) -> None:
        dlq_entry = {
            "command_id": command_id,
            "intended_agent": agent_id,
            "reason": reason,
            "timestamp_utc": datetime.datetime.utcnow().isoformat(),
            "command": command,
        }
        dlq_file = self.dlq_path / f"{command_id}.json"
        dlq_file.write_text(json.dumps(dlq_entry, indent=2), encoding="utf-8")
        logger.warning(
            "Command %s routed to DLQ (agent: '%s', reason: %s)",
            command_id, agent_id, reason,
        )

    def dispatch(self, agent_id: str, command: dict) -> DispatchResult:
        command_id = str(uuid.uuid4())
        config = self.registry.get(agent_id)

        if config is None:
            self._send_to_dlq(agent_id, command, command_id, "agent_not_in_registry")
            return DispatchResult(False, agent_id, command_id, "agent_not_in_registry")

        if not config.enabled:
            self._send_to_dlq(agent_id, command, command_id, "agent_disabled")
            return DispatchResult(False, agent_id, command_id, "agent_disabled")

        if not self._is_agent_alive(config):
            self._send_to_dlq(agent_id, command, command_id, "agent_not_alive")
            return DispatchResult(False, agent_id, command_id, "agent_not_alive")

        current_depth = self._queue_depth(config)
        if current_depth >= config.max_queue_depth:
            self._send_to_dlq(agent_id, command, command_id, "queue_full")
            return DispatchResult(False, agent_id, command_id, "queue_full")

        envelope = {
            "command_id": command_id,
            "expires_at_utc": (
                datetime.datetime.utcnow()
                + datetime.timedelta(seconds=config.ttl_seconds)
            ).isoformat(),
            "payload": command,
        }
        cmd_file = pathlib.Path(config.queue_path) / f"{command_id}.json"
        cmd_file.write_text(json.dumps(envelope, indent=2), encoding="utf-8")

        logger.info(
            "Command %s dispatched to agent '%s' (queue depth: %d/%d).",
            command_id, agent_id, current_depth + 1, config.max_queue_depth,
        )
        return DispatchResult(True, agent_id, command_id)
```

Implement a queue reaper that discards expired commands and prevents unbounded accumulation during agent downtime:

```python
# robust_fix/queue_reaper.py
import json
import logging
import datetime
import pathlib
import time

logger = logging.getLogger(__name__)


def reap_expired_commands(
    queue_path: str,
    dlq_path: str,
    dry_run: bool = False,
) -> int:
    """
    Move expired command files from the queue to the DLQ.
    Returns the number of files reaped.
    """
    queue = pathlib.Path(queue_path)
    dlq = pathlib.Path(dlq_path)
    dlq.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.utcnow()
    reaped = 0

    for cmd_file in queue.glob("*.json"):
        try:
            envelope = json.loads(cmd_file.read_text(encoding="utf-8"))
            expires_at = datetime.datetime.fromisoformat(
                envelope.get("expires_at_utc", "9999-01-01T00:00:00")
            )
            if now > expires_at:
                if not dry_run:
                    cmd_file.rename(dlq / cmd_file.name)
                logger.info(
                    "Reaped expired command %s (expired at %s).",
                    cmd_file.name, expires_at.isoformat(),
                )
                reaped += 1
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Could not parse command file %s: %s", cmd_file, exc)

    return reaped


def run_reaper(
    queue_paths: list[str],
    dlq_path: str,
    interval_seconds: int = 300,
) -> None:
    logger.info("Queue reaper started. Monitoring %d queues.", len(queue_paths))
    while True:
        total_reaped = 0
        for qp in queue_paths:
            total_reaped += reap_expired_commands(qp, dlq_path)
        if total_reaped:
            logger.info("Reaper cycle complete: %d expired commands moved to DLQ.", total_reaped)
        time.sleep(interval_seconds)
```

---

## 6. Architectural Prevention

**Treat agent availability as a runtime property, not a deployment-time constant.** The agent registry must be updated atomically whenever an agent is enabled or disabled, through a controlled API rather than a manual file edit. The orchestrator reads availability state from this registry on every dispatch cycle.

**Implement queue-side back-pressure.** Every agent queue must have a maximum depth limit enforced by the writing side (the orchestrator). When the limit is reached, new commands are immediately routed to the DLQ rather than allowed to accumulate. This converts an invisible unbounded growth problem into a visible, bounded routing decision.

**Use a message broker with native TTL and DLQ support.** Message brokers such as RabbitMQ (per-message TTL, dead-letter exchanges), Amazon SQS (message retention period, redrive policy), or Apache Kafka (log retention by time or size) handle expiry and dead-letter routing natively. File-based queues require all of this logic to be implemented in application code, which is a maintenance liability.

**Separate "agent known to the orchestrator" from "agent currently active".** Service discovery systems (Consul, etcd, Kubernetes Endpoints) provide a runtime view of which services are currently running. The orchestrator should consult the service discovery layer — not a static configuration file — to determine which agents are addressable.

**Include agent disablement in the change management process.** Any operation that stops or disables an agent process must include a step to update the orchestrator routing configuration. This is a process control, not a technical control, but it is the most reliable prevention for operationally-induced orphan queues.

---

## 7. Anti-patterns to Avoid

- **Writing to an agent's queue without first verifying the agent is consuming.** Fire-and-forget dispatch is only safe when queue capacity is bounded and a DLQ exists for overflow.
- **Using unbounded queues.** Any queue without a maximum depth or a TTL policy will eventually exhaust disk or memory given a persistent producer and a stopped consumer.
- **Deleting orphaned command files without archiving them.** Orphaned commands represent real work that was requested but never completed. Deleting them without analysis hides the scope of the impact.
- **Monitoring at aggregate level only.** "Total commands dispatched" tells you nothing about which specific agent is accumulating a backlog. Monitor per-agent queue depth independently.
- **Assuming a stopped agent process constitutes an error.** A stopped agent is operationally normal during maintenance, updates, and scaling events. The system must handle it gracefully rather than treating it as an unexpected failure.
- **Re-enabling a disabled agent and letting it process stale commands.** Commands dispatched hours or days earlier may no longer be valid. The agent should check the `expires_at_utc` field before executing any command retrieved from the queue.
- **Hardcoding agent IDs in the orchestrator routing logic.** If the routing logic contains `if ticket_type == "retention": dispatch("retention_agent", ...)`, adding or removing an agent type requires a code change. Use a registry-driven routing table instead.

---

## 8. Edge Cases and Variants

**Partial agent disablement.** An agent process is running but has paused consumption intentionally (e.g., a circuit breaker has opened due to a downstream dependency failure). The agent is alive and passes health checks, but commands accumulate because the agent is not processing. A liveness check alone is insufficient; the health endpoint must also report readiness to consume.

```python
# Edge case: distinguish liveness from readiness in the health endpoint
# GET /health/live   -> 200 if process is alive (used by Kubernetes liveness probe)
# GET /health/ready  -> 200 if agent is ready to consume commands
# The dispatcher should check /health/ready, not /health/live
```

**Agent removed from the codebase but still present in the registry.** A refactoring removes an agent entirely. Its registry entry and queue directory remain. The orchestrator continues dispatching to a queue that will never be consumed by any process. The fix is a registry cleanup step in the agent retirement runbook.

**Orchestrator restart with a command buffer.** If the orchestrator maintains an in-memory dispatch buffer and restarts unexpectedly, buffered commands that were not yet written to queues are lost. Commands that were written to queues but not yet consumed by now-disabled agents are orphaned. Both cases require a post-restart reconciliation step.

**Multi-tenancy: one queue per tenant per agent.** In systems where each customer tenant has its own agent instance, a single tenant's agent being disabled creates orphaned commands only for that tenant's queue. The monitoring system must operate at the tenant-agent granularity, not at the agent-type granularity, to detect the issue.

**Message broker partition rebalancing.** When a Kafka-based system loses a consumer (agent disabled), Kafka triggers a rebalance. Partitions previously assigned to the disabled consumer are reassigned to remaining consumers. If no consumer remains for an agent type (all instances disabled), the partition is unassigned and messages accumulate. Kafka's consumer group lag metric (`CONSUMER_LAG`) provides the equivalent of the queue depth metric described in the detection section.

```python
# Edge case: detect Kafka consumer group lag for a specific agent group
# Using kafka-python's AdminClient to check consumer group offsets:
from kafka import KafkaAdminClient
from kafka.admin import NewTopic

def get_consumer_lag(
    bootstrap_servers: list[str],
    group_id: str,
    topic: str,
) -> dict[int, int]:
    """
    Returns a dict mapping partition ID to lag (unprocessed message count).
    A lag > 0 on all partitions with no recent decrease indicates a disabled consumer.
    """
    from kafka import KafkaConsumer, TopicPartition

    consumer = KafkaConsumer(
        bootstrap_servers=bootstrap_servers,
        group_id=group_id,
        enable_auto_commit=False,
    )
    partitions = consumer.partitions_for_topic(topic) or set()
    topic_partitions = [TopicPartition(topic, p) for p in partitions]

    end_offsets = consumer.end_offsets(topic_partitions)
    committed = {
        tp: consumer.committed(tp) or 0 for tp in topic_partitions
    }

    lag = {
        tp.partition: end_offsets[tp] - committed[tp]
        for tp in topic_partitions
    }
    consumer.close()
    return lag
```

---

## 9. Audit Checklist

- [ ] Every agent in the orchestrator's routing table has a corresponding health check endpoint defined and tested.
- [ ] The orchestrator checks the agent registry for `enabled` status before each dispatch; disabled agents are never written to.
- [ ] All agent queues have a maximum depth limit enforced by the producer (orchestrator), not only by the consumer.
- [ ] All queued commands carry an `expires_at_utc` field; the consumer discards commands past their expiry time.
- [ ] A dead-letter queue (DLQ) exists for each agent queue; commands that cannot be delivered are routed to the DLQ rather than silently dropped or indefinitely retained.
- [ ] Per-agent queue depth is exported as a metric and monitored independently from aggregate dispatch metrics.
- [ ] An alert fires when any agent's queue depth grows for three consecutive monitoring intervals without a corresponding decrease.
- [ ] The DLQ is monitored for accumulation; an alert fires when DLQ depth exceeds a defined threshold.
- [ ] The agent disablement runbook includes a step to update the orchestrator registry before or at the same time as stopping the agent process.
- [ ] The agent re-enablement runbook includes a step to review DLQ contents and determine whether accumulated commands should be replayed, discarded, or triaged manually.
- [ ] A queue reaper process runs periodically and moves expired commands from agent queues to the DLQ.
- [ ] Monitoring dashboards display "commands sent" and "commands consumed" as separate per-agent time series, not only as a ratio.
- [ ] Integration tests include a scenario where the target agent is disabled and assert that the orchestrator routes to the DLQ rather than writing to the dead queue.
- [ ] Agent IDs in the routing table are registry-driven; no agent ID is hardcoded in orchestrator application logic.
- [ ] For Kafka-based systems, consumer group lag is tracked per group and per topic partition, and an alert fires on persistent lag growth.

---

## 10. Further Reading

- RabbitMQ dead-letter exchanges and per-message TTL: https://www.rabbitmq.com/docs/dlx
- Amazon SQS redrive policies and visibility timeouts: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html
- Apache Kafka consumer group lag monitoring: https://kafka.apache.org/documentation/#monitoring
- Kubernetes liveness vs. readiness vs. startup probes: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
- Service mesh health checking patterns (Envoy, Istio): https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/health_checking
- Related failure patterns in the multi-agent failure patterns repository: https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns
