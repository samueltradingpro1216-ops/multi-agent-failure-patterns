# Pattern #39 — Identifier Collision

**Category:** Security & Secrets
**Severity:** High
**Tags:** `traceability`, `identifier`, `collision`, `session-id`, `incident-response`, `multi-agent`

---

## 1. Observable Symptoms

The system appears to function correctly under normal operation, but becomes unauditable when an incident occurs. Multiple agents or components share the same identifier, making it impossible to attribute an action to a specific agent during investigation.

- Logs from multiple agents contain identical `agent_id`, `session_id`, `magic_number`, or `request_id` values. Filtering the log by identifier returns entries from all agents simultaneously.
- During an incident, the team cannot determine which agent made a specific API call, wrote a specific record, or triggered a specific downstream action.
- Audit trail reconstruction is ambiguous or impossible. Compliance officers cannot produce a per-agent activity report.
- Security investigations that rely on log correlation (e.g., "show me all actions taken by the agent that processed order X") return results spanning all agents, polluting the search with unrelated activity.
- In distributed tracing systems, spans from different agents share the same root trace ID, causing the trace visualization to show a single agent performing actions that were actually distributed across many.
- Rate limiting and quota enforcement fail when multiple agents share a key prefix or API key. The quota is shared unintentionally, causing legitimate agents to be throttled when one misbehaving agent exhausts the limit.
- A post-incident review cannot definitively answer: "Which agent did this?"

---

## 2. Field Story

A supply chain company operated a logistics tracking platform that coordinated a fleet of software agents responsible for updating shipment status, triggering notifications, and reconciling inventory records. The platform had been developed iteratively over three years, with different teams adding agents at different times.

During a routine code review, an engineer discovered that six of the twelve active agents shared the same value for a constant named `AGENT_MAGIC`:

```python
AGENT_MAGIC = 24680201
```

This constant had been copied from the first agent ever written into every subsequent agent as a boilerplate identifier. Some agents used it as a log prefix, others as a field in API request headers, and one used it as a seed component in generating session identifiers. No documentation explained what the constant meant or that it was supposed to be unique.

A major incident occurred before the fix was deployed. A batch of 4,200 shipment records was incorrectly marked as "delivered" when they had not yet left the origin warehouse. The incorrect status triggered customer notifications and initiated downstream billing events. The investigation team needed to identify which agent had issued the erroneous status update.

Every relevant log entry contained `agent_id: 24680201`. Six agents matched. The team spent 31 hours cross-referencing timestamp windows, hostname patterns, and database write logs to narrow down the responsible agent. Two of those hours were spent on a false suspect. The root cause (a timezone offset bug in one specific agent's timestamp comparison) was eventually found, but the resolution timeline was extended by a factor of four compared to what it would have been with unique agent identifiers.

The company's SLA required root-cause identification within 8 hours for Severity 1 incidents. The 31-hour timeline triggered a contract penalty.

---

## 3. Technical Root Cause

The root cause is the assignment of a static, hardcoded, non-unique identifier to each agent instance. The identifier is shared because it was copy-pasted from a template without a mechanism to enforce uniqueness.

```python
# agent_a.py — copied verbatim from the original template
AGENT_MAGIC = 24680201

class ShipmentStatusAgent:
    def update_status(self, shipment_id: str, status: str) -> None:
        logger.info(
            "Updating shipment status",
            extra={"agent_id": AGENT_MAGIC, "shipment_id": shipment_id, "status": status}
        )
        # ... business logic ...
```

```python
# agent_b.py — identical constant, different agent
AGENT_MAGIC = 24680201  # copied from agent_a.py, never changed

class InventoryReconciliationAgent:
    def reconcile(self, warehouse_id: str) -> None:
        logger.info(
            "Reconciling inventory",
            extra={"agent_id": AGENT_MAGIC, "warehouse_id": warehouse_id}
        )
```

Three compounding factors elevate this from a minor code smell to a high-severity operational risk:

**1. Identifier used as a log discriminator.** If the identifier served no filtering or correlation purpose, collision would be cosmetic. When logs are queried by `agent_id`, the collision makes the query meaningless. Every log query that should isolate one agent returns all agents.

**2. Identifier used in security-relevant contexts.** When `AGENT_MAGIC` is included in API request headers (as a client identifier for rate limiting, quota tracking, or audit logging at the API provider), multiple agents share a single identity at the external service. The external service cannot distinguish agent A from agent B. Abuse by one agent exhausts the quota or triggers rate-limit responses for all agents.

**3. Identifier used as a seed in derived identifiers.** If `AGENT_MAGIC` is combined with a timestamp or random bytes to generate session IDs, the collision may not manifest in the session ID itself (since the random component differentiates them), but the `magic_number` field in logs or request metadata remains ambiguous.

**The copy-paste propagation mechanism.** Agent boilerplate templates that contain hardcoded identifiers propagate collision silently. No tooling raises an error when a duplicate constant is introduced. The developer who copies the template has no indication that the value must be changed. In fast-moving teams with many agents, this results in widespread collision within months.

```python
# The derived identifier trap:
import hashlib
import time

AGENT_MAGIC = 24680201  # same in all agents

def generate_session_id() -> str:
    # The session_id is unique per session, but the embedded magic_number
    # is the same across all agents — logs with the magic field are still
    # ambiguous.
    raw = f"{AGENT_MAGIC}-{time.time_ns()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

---

## 4. Detection

### 4.1 Static Analysis: Detect Duplicate Constants Across Files

Scan the codebase for constant assignments with the same value and flag cases where the variable name suggests it is an identifier (contains "id", "magic", "key", "token", "agent", "session").

```python
import ast
import sys
import os
from pathlib import Path
from collections import defaultdict

IDENTIFIER_KEYWORDS = {"id", "magic", "key", "token", "agent", "session", "prefix", "tag"}

def is_identifier_name(name: str) -> bool:
    lower = name.lower()
    return any(kw in lower for kw in IDENTIFIER_KEYWORDS)

def collect_constants(source_path: str) -> list[dict]:
    """
    Return all module-level constant assignments in a Python file
    where the variable name suggests an identifier role.
    """
    source = Path(source_path).read_text(encoding="utf-8")
    tree   = ast.parse(source)
    constants = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if not is_identifier_name(target.id):
                continue
            value_node = node.value
            if isinstance(value_node, ast.Constant):
                constants.append({
                    "file":  source_path,
                    "line":  node.lineno,
                    "name":  target.id,
                    "value": value_node.value,
                })
    return constants

def find_duplicate_identifiers(directory: str) -> dict[tuple, list[dict]]:
    """
    Walk `directory` recursively and return a mapping from
    (name, value) to list of assignments — only entries with more than
    one assignment (duplicates) are returned.
    """
    all_constants: list[dict] = []

    for py_file in Path(directory).rglob("*.py"):
        try:
            all_constants.extend(collect_constants(str(py_file)))
        except SyntaxError:
            pass

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for c in all_constants:
        grouped[(c["name"], c["value"])].append(c)

    return {k: v for k, v in grouped.items() if len(v) > 1}

if __name__ == "__main__":
    directory = sys.argv[1] if len(sys.argv) > 1 else "."
    duplicates = find_duplicate_identifiers(directory)

    if not duplicates:
        print("No duplicate identifier constants found.")
        sys.exit(0)

    print(f"Found {len(duplicates)} duplicate identifier constant(s):\n")
    for (name, value), occurrences in sorted(duplicates.items()):
        print(f"  {name} = {value!r}  ({len(occurrences)} occurrences)")
        for occ in occurrences:
            print(f"    {occ['file']}:{occ['line']}")
    sys.exit(1)
```

### 4.2 Runtime Collision Detection at Agent Startup

Register each agent's identifier in a shared registry (in-process for single-host deployments, or a distributed key-value store for multi-host). Raise an error at startup if the identifier is already registered.

```python
import os
import uuid
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

class AgentIdentityRegistry:
    """
    In-process registry that enforces unique agent identifiers at startup.
    For multi-process or multi-host deployments, replace _store with
    a Redis or etcd-backed implementation.
    """

    _instance: Optional["AgentIdentityRegistry"] = None
    _lock     = threading.Lock()

    def __init__(self) -> None:
        self._store: dict[str, str] = {}  # agent_id -> agent_type
        self._mutex = threading.Lock()

    @classmethod
    def get(cls) -> "AgentIdentityRegistry":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    def register(self, agent_id: str, agent_type: str) -> None:
        """
        Register an agent. Raises ValueError if agent_id is already registered
        by a different agent type (indicating a collision).
        """
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError(f"agent_id must be a non-empty string, got: {agent_id!r}")
        with self._mutex:
            if agent_id in self._store:
                existing_type = self._store[agent_id]
                raise ValueError(
                    f"Identifier collision detected: agent_id='{agent_id}' is already "
                    f"registered by agent_type='{existing_type}'. "
                    f"Attempting to register: agent_type='{agent_type}'. "
                    f"Each agent must use a unique identifier."
                )
            self._store[agent_id] = agent_type
            logger.info(
                "Agent registered: agent_id=%s agent_type=%s total_registered=%d",
                agent_id, agent_type, len(self._store),
            )

    def deregister(self, agent_id: str) -> None:
        with self._mutex:
            self._store.pop(agent_id, None)

    def all_agents(self) -> dict[str, str]:
        with self._mutex:
            return dict(self._store)

def generate_unique_agent_id(agent_type: str, instance_index: Optional[int] = None) -> str:
    """
    Generate a unique, human-readable agent identifier.
    Format: <agent_type>-<instance_index_or_uuid4_prefix>
    """
    if instance_index is not None:
        return f"{agent_type}-{instance_index:04d}"
    short_uuid = str(uuid.uuid4()).replace("-", "")[:12]
    return f"{agent_type}-{short_uuid}"
```

### 4.3 Log Analysis: Post-Hoc Collision Audit

Query structured logs for any `agent_id` value that appears alongside more than one distinct hostname, process ID, or container ID within a time window. This detects collisions in deployed systems that predate the fix.

```python
import json
import sys
from collections import defaultdict
from pathlib import Path

def audit_log_for_id_collisions(
    log_path: str,
    id_field:   str = "agent_id",
    disambig_fields: list[str] = None,
) -> dict[str, set]:
    """
    Parse a JSONL log file and return a dict mapping each identifier value
    to the set of distinct disambiguating field values seen alongside it.
    Entries with more than one distinct value indicate a collision.

    Args:
        log_path:          Path to a JSONL log file.
        id_field:          The field name to check for collisions.
        disambig_fields:   Fields that should be unique per agent
                           (e.g., hostname, pid, container_id).
    """
    if disambig_fields is None:
        disambig_fields = ["hostname", "pid", "container_id"]

    id_to_disambig: dict[str, set] = defaultdict(set)

    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        id_value = entry.get(id_field)
        if id_value is None:
            continue
        key = str(id_value)
        for field in disambig_fields:
            if field in entry:
                id_to_disambig[key].add(f"{field}={entry[field]}")

    return {
        id_val: origins
        for id_val, origins in id_to_disambig.items()
        if len(origins) > 1
    }

if __name__ == "__main__":
    log_file = sys.argv[1] if len(sys.argv) > 1 else "/var/log/agents/agent.jsonl"
    collisions = audit_log_for_id_collisions(log_file)

    if not collisions:
        print("No identifier collisions detected in log.")
        sys.exit(0)

    print(f"COLLISION DETECTED: {len(collisions)} identifier value(s) shared across agents:\n")
    for id_val, origins in sorted(collisions.items()):
        print(f"  agent_id={id_val!r} seen from {len(origins)} distinct origins:")
        for origin in sorted(origins):
            print(f"    {origin}")
    sys.exit(1)
```

---

## 5. Fix

### 5.1 Immediate Fix: Assign Unique Identifiers at Agent Instantiation

Remove hardcoded identifier constants from agent classes. Assign a unique identifier at construction time, either from environment variables (for stable deployment-time identity) or from UUID generation (for ephemeral identity). Register the identifier at startup.

```python
import os
import uuid
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class ShipmentStatusAgent:
    """
    Agent with a unique, traceable identifier assigned at construction.
    The identifier is present in every log entry and every outbound request.
    """

    def __init__(self, agent_id: Optional[str] = None) -> None:
        # Prefer explicit injection (from environment or orchestrator).
        # Fall back to a UUID-based identifier that is guaranteed unique.
        if agent_id:
            self.agent_id = agent_id
        else:
            env_id = os.environ.get("AGENT_ID", "").strip()
            self.agent_id = env_id if env_id else self._generate_id()

        # Validate format before registering
        if not self.agent_id.startswith("shipment-status-"):
            raise ValueError(
                f"agent_id '{self.agent_id}' does not match expected prefix "
                f"'shipment-status-'. Verify AGENT_ID environment variable."
            )

        # Register with the global registry; raises on collision
        AgentIdentityRegistry.get().register(
            self.agent_id, agent_type="ShipmentStatusAgent"
        )

        self._logger = logging.LoggerAdapter(
            logger, extra={"agent_id": self.agent_id}
        )
        self._logger.info("Agent initialized")

    @staticmethod
    def _generate_id() -> str:
        short = str(uuid.uuid4()).replace("-", "")[:10]
        return f"shipment-status-{short}"

    def update_status(self, shipment_id: str, status: str) -> None:
        self._logger.info(
            "Updating shipment status: shipment_id=%s status=%s",
            shipment_id, status,
        )
        # All log entries now carry agent_id unambiguously

    def shutdown(self) -> None:
        AgentIdentityRegistry.get().deregister(self.agent_id)
        self._logger.info("Agent deregistered")
```

### 5.2 Centralized Identity Provisioning

For multi-host deployments, provision agent identities from a central source of truth (environment variable injected by the orchestrator, Kubernetes Downward API, or a secrets manager) rather than allowing agents to self-assign. This guarantees uniqueness at the infrastructure level.

```python
import os
import uuid
import logging

logger = logging.getLogger(__name__)

class AgentIdentity:
    """
    Encapsulates agent identity with validation and structured metadata.
    Constructed from environment variables injected by the deployment
    orchestrator (Kubernetes, ECS, Nomad, etc.).

    Expected environment variables:
        AGENT_ID        — unique identifier for this agent instance
                          (e.g., "shipment-status-prod-0042")
        AGENT_TYPE      — agent class/type name
        AGENT_VERSION   — semantic version of this agent's code
        DEPLOYMENT_ENV  — "prod", "staging", "dev"
    """

    def __init__(self) -> None:
        self.agent_id   = self._require_env("AGENT_ID")
        self.agent_type = self._require_env("AGENT_TYPE")
        self.version    = os.environ.get("AGENT_VERSION", "unknown")
        self.env        = os.environ.get("DEPLOYMENT_ENV", "unknown")

        self._validate()

        logger.info(
            "AgentIdentity loaded: agent_id=%s agent_type=%s version=%s env=%s",
            self.agent_id, self.agent_type, self.version, self.env,
        )

    @staticmethod
    def _require_env(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise EnvironmentError(
                f"Required environment variable '{name}' is not set or is empty. "
                f"The orchestrator must inject a unique value per agent instance."
            )
        return value

    def _validate(self) -> None:
        # Reject known collision-prone placeholder values
        banned = {"24680201", "0", "1", "test", "agent", "default", "placeholder"}
        if self.agent_id.lower() in banned:
            raise ValueError(
                f"agent_id '{self.agent_id}' is a known placeholder value. "
                f"Set a unique AGENT_ID in the deployment manifest."
            )
        # Enforce minimum length and format
        if len(self.agent_id) < 8:
            raise ValueError(
                f"agent_id '{self.agent_id}' is too short (minimum 8 characters). "
                f"Use the format '<type>-<env>-<index_or_uuid>'."
            )

    def as_log_context(self) -> dict:
        """Return a dict suitable for use as logging extra context."""
        return {
            "agent_id":   self.agent_id,
            "agent_type": self.agent_type,
            "version":    self.version,
            "env":        self.env,
        }

    def as_request_headers(self) -> dict:
        """Return HTTP headers for outbound API requests."""
        return {
            "X-Agent-ID":   self.agent_id,
            "X-Agent-Type": self.agent_type,
            "X-Agent-Env":  self.env,
        }
```

---

## 6. Architectural Prevention

**Orchestrator-assigned identities.** In Kubernetes, use the Downward API to inject the pod name (which is unique per pod) as `AGENT_ID`. Pod names are generated by the StatefulSet controller with a stable ordinal suffix (`shipment-status-agent-0`, `shipment-status-agent-1`), guaranteeing uniqueness without any agent-side logic.

```yaml
# kubernetes/deployment.yaml (excerpt)
env:
  - name: AGENT_ID
    valueFrom:
      fieldRef:
        fieldPath: metadata.name   # unique pod name
  - name: AGENT_TYPE
    value: "shipment-status"
  - name: DEPLOYMENT_ENV
    valueFrom:
      fieldRef:
        fieldPath: metadata.namespace
```

**Startup collision check in health probe.** Make the Kubernetes liveness or readiness probe fail until the agent has successfully registered its unique identity. An agent with a colliding identifier will fail its readiness probe and be taken out of service before it begins processing.

**Structured logging with mandatory identity fields.** Define a log schema that requires `agent_id` and `agent_type` in every log entry. Emit a startup log line that records the full identity context. Log aggregators can then validate that no two log streams share the same `agent_id` value.

**API gateway enforcement.** Configure the API gateway (Kong, AWS API Gateway, Envoy) to require a unique `X-Agent-ID` header and reject requests where the header matches a known-duplicate value or is absent. This enforces identity at the network layer, independent of application code.

---

## 7. Anti-patterns to Avoid

- **Hardcoded numeric constants as identifiers.** `AGENT_MAGIC = 24680201` communicates nothing about the agent and is inherently copy-pasteable. Use structured string identifiers with semantic components (`<type>-<env>-<index>`).
- **Using the same identifier for debugging and for operational correlation.** A constant used as a "magic number" in one context will inevitably be repurposed as an identifier in another. Separate debugging constants from operational identifiers from day one.
- **Generating identifiers from non-unique seeds.** `hashlib.md5(f"{type}-{start_time}".encode())` is not unique if two agents of the same type start at the same wall-clock second. Use UUIDs (version 4) for ephemeral identifiers and orchestrator-assigned stable names for persistent identifiers.
- **Relying on hostname alone as an identifier in containerized environments.** Container hostnames are not always unique. Pod names are unique within a namespace; hostnames set by Docker or Kubernetes may collide if configuration is incorrect.
- **Omitting the identifier from outbound API requests.** If only logs carry the identifier but API requests do not, it is impossible to correlate a downstream API event back to a specific agent. Include `X-Agent-ID` in all outbound HTTP requests.
- **Using a shared API key across all agents without per-agent sub-identifiers.** If all agents use the same API key, the external service's audit log shows all activity under one identity. Use per-agent API keys or include an agent sub-identifier in every request.

---

## 8. Edge Cases and Variants

**Session ID collision in stateful protocols.** If session IDs are generated from a shared seed (e.g., `AGENT_MAGIC + timestamp`), two agents starting at similar times may generate identical session IDs. A downstream stateful service that deduplicates by session ID will merge their sessions.

**Log tag collision in multi-tenant systems.** If multiple customers' agents all use the same default log tag (e.g., `tag: "agent"`), log aggregation across customers becomes impossible to partition. Per-tenant unique prefixes must be enforced at onboarding.

**Database row ownership ambiguity.** If a database table has an `agent_id` column used to denote which agent owns a row, identifier collision causes lock contention and incorrect ownership attribution. Agent A acquires a lock on records it does not own; agent B cannot acquire them.

**Rate limit sharing.** When multiple agents share an API key or client identifier, the external provider's rate limiter treats them as one client. A burst from one agent throttles all agents simultaneously. This manifests as intermittent 429 errors that cannot be attributed to a single agent.

**Identifier reuse across deployments.** If agent IDs are assigned as sequential integers (0, 1, 2...) and a new deployment resets the counter, the new agents collide with historical log entries from previous deployments. Include a deployment timestamp or version in the identifier: `shipment-status-20260409-0042`.

**Collision in distributed tracing.** If `AGENT_MAGIC` is used as a component of the trace ID or parent span ID, all agents' spans appear under the same root, making the distributed trace unreadable. Use the W3C Trace Context standard with UUID-based trace IDs.

---

## 9. Audit Checklist

- [ ] No two agent files in the codebase contain the same value for any variable whose name includes "id", "magic", "key", "token", "agent", or "session".
- [ ] The static analysis script (section 4.1) runs in CI and fails the build on any new duplicate identifier constant.
- [ ] Every agent reads its identifier from an environment variable (`AGENT_ID`) injected by the deployment orchestrator, not from a hardcoded constant.
- [ ] The deployment manifest (Kubernetes, ECS, Nomad) assigns a unique value to `AGENT_ID` for every agent instance, using the Downward API or equivalent.
- [ ] Agent startup raises `ValueError` or `EnvironmentError` if `AGENT_ID` is absent, empty, or matches a banned placeholder value.
- [ ] The `AgentIdentityRegistry` (or equivalent) is initialized at startup and raises on identifier collision before the agent begins processing.
- [ ] Every structured log entry includes `agent_id` and `agent_type` fields.
- [ ] Every outbound HTTP request includes an `X-Agent-ID` header.
- [ ] The log aggregation system has an alert that fires when the same `agent_id` value appears alongside more than one distinct `hostname` or `pod_name` in a 1-hour window.
- [ ] A post-incident runbook exists that documents how to filter logs, traces, and database records by `agent_id` to reconstruct a per-agent activity timeline within 30 minutes.

---

## 10. Further Reading

- RFC 9562 — Universally Unique IDentifiers (UUIDs): https://www.rfc-editor.org/rfc/rfc9562
- W3C Trace Context specification: https://www.w3.org/TR/trace-context/
- Kubernetes Downward API — exposing pod and container fields to containers: https://kubernetes.io/docs/concepts/workloads/pods/downward-api/
- The Twelve-Factor App — Processes (stateless, unique per instance): https://12factor.net/processes
- OpenTelemetry — Resource semantic conventions for service identity: https://opentelemetry.io/docs/specs/semconv/resource/
- NIST SP 800-92 — Guide to Computer Security Log Management: https://csrc.nist.gov/publications/detail/sp/800-92/final
- Python `uuid` module documentation: https://docs.python.org/3/library/uuid.html
- AWS Well-Architected Framework — Operational Excellence pillar, traceability: https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/traceability.html
