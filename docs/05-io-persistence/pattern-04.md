# Pattern #04 — Multi-File State Desync

**Category:** I/O & Persistence
**Severity:** Critical
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 3 to 15 days (the bug is masked by the fact that each component behaves correctly in isolation)

---

## 1. Observable Symptoms

Three system components give **three different answers** to the same question. The executor believes the system is in emergency shutdown. The supervisor believes everything is running normally. The dashboard crashes because a state file does not exist.

The most confusing symptom: each component is **correct according to its own data source**. The executor reads a file that says "SHUTDOWN". The supervisor reads a config that says "active". The dashboard looks for a JSON file that was never created. None of them is lying — they are simply reading different sources that are not synchronized.

Indirect consequences: commands are sent but never executed (the supervisor sends, the executor blocks), alerts fire without cause (the dashboard sees an inconsistent state), and the developer spends hours looking for a bug that lives not in the code but in the **data topology**.

## 2. Field Story (anonymized)

A multi-agent system had an emergency shutdown mechanism ("killswitch") whose state was stored in 3 places:
- A plain text file (`emergency.txt`) written by the executor
- A structured JSON file (`emergency_state.json`) for the dashboard
- A field in the shared config (`config.json → emergency_active: false`) for the supervisor

During an incident 10 days earlier, the executor had activated the emergency shutdown by writing "ACTIVE" to the text file. The incident was resolved manually by changing `emergency_active: false` in the config. But nobody thought to update the text file.

Result: 10 days later, the executor was still reading "ACTIVE" from its file and silently rejecting commands from the supervisor. The supervisor, seeing `emergency_active: false`, kept sending commands. It took 3 hours of debugging to understand that the problem was not in the code but in a stale text file that was 10 days old.

## 3. Technical Root Cause

State was **progressively duplicated** as the system evolved. Each new feature added its own representation of the state without synchronizing the existing ones:

```
V1 (month 1) : executor writes emergency.txt           ← plain text format
V2 (month 3) : supervisor uses config.json              ← JSON field
V3 (month 6) : dashboard reads emergency_state.json     ← structured JSON
```

At no point are these 3 files synchronized. Each component writes to "its own" file and reads only that one. The result is 3 sources of truth for a single state:

```
emergency.txt          → "ACTIVE"    (written 10 days ago, never cleaned up)
emergency_state.json   → absent      (never created)
config.json            → false       (updated manually)
```

The fundamental problem is the absence of a **Single Source of Truth** (SSOT). When a critical state is stored in N places, N synchronized operations are required to modify it. In practice, only 1 or 2 operations are performed, and the remaining N-1 or N-2 copies silently diverge.

## 4. Detection

### 4.1 Manual code audit

For each critical state in the system, list every source that stores it:

```bash
# Find all references to a specific state (e.g., "emergency", "killswitch")
grep -rn "emergency\|killswitch\|shutdown" --include="*.py" --include="*.json"

# Count files that WRITE this state
grep -rln "emergency.*=\|write.*emergency\|emergency.*write" --include="*.py"

# Count files that READ this state
grep -rln "read.*emergency\|emergency.*get\|load.*emergency" --include="*.py"
```

If the number of writers > 1 or the number of sources > 1, it is a candidate for desync.

### 4.2 Automated CI/CD

Document critical states in a reference file and verify in CI that each state has exactly one write source:

```python
# test_single_source_of_truth.py
CRITICAL_STATES = {
    "emergency_active": {
        "primary_source": "data/emergency_state.json",
        "expected_writers": 1,
    },
    "system_config": {
        "primary_source": "data/config.json",
        "expected_writers": 1,
    },
}

def test_no_duplicate_writers():
    """Each critical state should have exactly one writer module."""
    import pathlib, re
    for state, spec in CRITICAL_STATES.items():
        writers = []
        for py_file in pathlib.Path("src").rglob("*.py"):
            content = py_file.read_text()
            if re.search(rf'write.*{state}|{state}.*=|save.*{state}', content):
                writers.append(str(py_file))
        assert len(writers) <= spec["expected_writers"], (
            f"State '{state}' has {len(writers)} writers: {writers}. "
            f"Expected max {spec['expected_writers']}."
        )
```

### 4.3 Runtime production

At each cycle, compare the values of all copies of the same state:

```python
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

def audit_state_consistency(state_name: str, sources: dict[str, callable]) -> list[str]:
    """
    Compare values from multiple sources for the same state.
    sources: {"source_name": callable_that_returns_value}
    """
    alerts = []
    values = {}

    for name, reader in sources.items():
        try:
            values[name] = reader()
        except Exception as e:
            alerts.append(f"{state_name}/{name}: read failed ({e})")

    # Check consistency
    unique_values = set(str(v) for v in values.values())
    if len(unique_values) > 1:
        alerts.append(
            f"DESYNC: {state_name} has {len(unique_values)} different values: "
            + ", ".join(f"{k}={v}" for k, v in values.items())
        )

    return alerts
```

## 5. Fix

### 5.1 Immediate fix

Synchronize all copies from the primary source:

```python
def sync_emergency_state(primary_value: bool):
    """Force all copies to match the primary source."""
    # Primary
    with open("data/emergency_state.json", "w") as f:
        json.dump({"active": primary_value, "synced_at": datetime.now(timezone.utc).isoformat()}, f)

    # Legacy copies
    Path("data/emergency.txt").write_text("ACTIVE" if primary_value else "INACTIVE")

    config = json.loads(Path("data/config.json").read_text())
    config["emergency_active"] = primary_value
    with open("data/config.json", "w") as f:
        json.dump(config, f, indent=2)
```

### 5.2 Robust fix

Implement a **StateManager** that is the sole interface for reading and modifying state. It writes to all copies on every update and reads exclusively from the primary source:

```python
import json
from pathlib import Path
from datetime import datetime, timezone

class StateManager:
    """Single point of read/write for critical state. Syncs all copies."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.primary = self.data_dir / "state.json"
        self.legacy_copies = [
            self.data_dir / "emergency.txt",
        ]

    def get(self, key: str, default=None):
        """Read from primary source only."""
        if self.primary.exists():
            with open(self.primary) as f:
                return json.load(f).get(key, default)
        return default

    def set(self, key: str, value, source: str = "unknown"):
        """Write to primary AND all legacy copies."""
        # Read current state
        state = {}
        if self.primary.exists():
            with open(self.primary) as f:
                state = json.load(f)

        # Update
        state[key] = value
        state["_last_modified_by"] = source
        state["_last_modified_at"] = datetime.now(timezone.utc).isoformat()

        # Write primary
        with open(self.primary, "w") as f:
            json.dump(state, f, indent=2)

        # Sync legacy copies
        self._sync_legacy(state)

    def _sync_legacy(self, state: dict):
        for copy in self.legacy_copies:
            try:
                if copy.suffix == ".txt":
                    val = state.get("emergency_active", False)
                    copy.write_text("ACTIVE" if val else "INACTIVE")
                elif copy.suffix == ".json":
                    with open(copy, "w") as f:
                        json.dump(state, f, indent=2)
            except Exception:
                pass  # Log but don't fail on legacy sync

    def audit(self) -> list[str]:
        """Check all copies are consistent with primary."""
        issues = []
        primary_value = self.get("emergency_active")
        for copy in self.legacy_copies:
            if not copy.exists():
                issues.append(f"{copy.name}: missing")
                continue
            content = copy.read_text().strip()
            copy_value = content.upper() == "ACTIVE"
            if copy_value != primary_value:
                issues.append(f"{copy.name}: {copy_value} vs primary {primary_value}")
        return issues
```

## 6. Architectural Prevention

The founding principle is **Single Source of Truth**: each critical state has exactly **one write source**. All other representations are read-only copies synchronized automatically.

In practice, this means migrating critical states to a database (SQLite for a local system, PostgreSQL for a distributed system) and eliminating text/JSON files as state storage. Files may remain for compatibility with legacy components, but they are generated by the StateManager and are never read as a source of truth.

A periodic sync (every 60 seconds) re-aligns legacy copies with the primary source. Even if a component writes directly to a legacy file (bypassing the StateManager), the next sync will overwrite its change. This is blunt but effective.

## 7. Anti-patterns to Avoid

1. **Adding a new state file without synchronizing existing ones.** Every new representation of an existing state must be managed by the StateManager or removed.

2. **Resolving an incident by manually editing a file.** If `config.json` is changed by hand but `emergency.txt` is not, a desync is created. Always go through a script or the StateManager.

3. **Reading a file without checking its freshness.** A file that has not been modified in 10 days is likely stale. Every state file should contain a `last_synced` timestamp.

4. **Assuming that a missing file equals the default state.** A missing file can mean "never created" (initial state) or "accidentally deleted" (corruption). Handle both cases explicitly.

5. **No periodic consistency audit.** Even with a StateManager, files can diverge (direct writes, crash mid-sync). An audit every minute that compares copies detects divergences in under 60 seconds.

## 8. Edge Cases and Variants

**Variant 1: Config shared between agents.** A `config.json` file read by 4 agents, each modifying different sections. Without a lock, agent A can overwrite agent B's changes. This is an intra-file desync, not an inter-file one.

**Variant 2: Application cache out of sync.** The state is correct in the DB but an agent's in-memory cache holds the old value. The cache is not invalidated after a modification. Solution: short TTL on the cache or explicit invalidation.

**Variant 3: Distributed state across machines.** Two instances of the same agent run on two VPS. Each has its own local copy of the state. After a modification on instance A, instance B retains the old value. Solution: centralized storage (Redis, Consul, or a DB).

**Variant 4: Stale state file after a crash.** The StateManager writes to the primary file, then crashes before synchronizing the legacy copies. On restart, the copies are desynchronized. Solution: the sync must be the first action at boot, before any read.

## 9. Audit Checklist

- [ ] Each critical state has a designated and documented primary source
- [ ] No component reads a legacy state file directly (all go through the StateManager)
- [ ] A periodic sync re-aligns legacy copies with the primary source
- [ ] Each state file contains a `last_synced` or `last_modified` timestamp
- [ ] A consistency audit compares all copies at every cycle and alerts on divergence

## 10. Further Reading

- Short pattern: [Pattern 04 — Multi-File State Desync](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-04)
- Related patterns: #11 (Race Condition on Shared File — concurrent writes to the same file), #08 (Data Pipeline Freeze — a stale file can be a symptom of desync), #01 (Timezone Mismatch — timestamps in desynchronized files may be in different timezones)
- Recommended reading:
  - "Designing Data-Intensive Applications" (Martin Kleppmann), chapter 5 on replication and consistency
  - HashiCorp Consul documentation on distributed consensus — the same problems at a larger scale
