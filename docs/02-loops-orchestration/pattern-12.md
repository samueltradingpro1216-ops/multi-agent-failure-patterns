# Pattern #12 — Cascade Write in Single Cycle

**Category:** Loops & Orchestration
**Severity:** High
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 2 to 7 days (the config file appears valid at any single point, but the final value is wrong because of intermediate overwrites)

---

## 1. Observable Symptoms

A shared configuration file ends up with an **unexpected final value** after a processing cycle. Five different modules modify the same file during one cycle, and each one overwrites the previous module's changes. The final value in the file depends on which module ran last — not on any intentional logic.

Logs from individual modules look correct: each one reads the config, applies a reasonable change, and writes it back. But the aggregate effect is destructive: Module A sets `timeout=30`, Module B reads and changes `retry_count=5` (but also writes back `timeout=30`), Module C reads B's version and changes `batch_size=100` — but if Module A runs again after C, it overwrites C's `batch_size` with the stale value it cached at the start.

The symptom is intermittent because it depends on execution order. Some cycles produce correct results (modules happen to run in the right order), others produce corrupted configs (modules run in a different order and overwrite each other).

## 2. Field Story (anonymized)

A SaaS platform's multi-agent system had 5 modules that each updated different fields in a shared `tuning.json` file during the same 10-second processing cycle. Module A adjusted timeouts. Module B adjusted retry counts. Module C adjusted batch sizes. Module D adjusted confidence thresholds. Module E adjusted rate limits.

Each module did a full read-modify-write: read the entire JSON, change its field, write the entire JSON back. When modules B and D ran near-simultaneously, B read the file before D wrote, then B wrote back its version — erasing D's changes. The confidence threshold reverted to its old value every other cycle.

The team spent 5 days debugging why the confidence threshold "randomly reset." They were looking for a bug in Module D. The bug was in the architecture: full-file read-modify-write without coordination.

## 3. Technical Root Cause

The bug occurs when multiple modules perform **full-file read-modify-write** on the same configuration file within a single processing cycle:

```python
# Module A (runs at T=0.0s)
config = json.load(open("config.json"))      # Reads: {timeout: 30, batch: 50, retry: 3}
config["timeout"] = 45                         # Changes timeout
json.dump(config, open("config.json", "w"))   # Writes: {timeout: 45, batch: 50, retry: 3}

# Module B (runs at T=0.1s)
config = json.load(open("config.json"))      # Reads: {timeout: 45, batch: 50, retry: 3}
config["retry"] = 5                           # Changes retry
json.dump(config, open("config.json", "w"))   # Writes: {timeout: 45, batch: 50, retry: 5}

# Module A (runs AGAIN at T=0.2s — cached stale version)
# If Module A cached the config at T=0.0s and writes again:
json.dump(cached_config, open("config.json", "w"))  # Writes: {timeout: 45, batch: 50, retry: 3}
# Module B's retry=5 is LOST
```

The fundamental problem: each module writes the **entire file**, not just its field. Any concurrent or out-of-order writes overwrite other modules' changes. This is distinct from Pattern #11 (Race Condition) because it happens even without true concurrency — sequential writes within the same cycle are enough if modules cache stale versions.

## 4. Detection

### 4.1 Manual code audit

Count how many modules write to the same file per cycle:

```bash
# Find all write operations to the same config file
grep -rn "json\.dump\|write.*config\|save.*config" --include="*.py" | grep -v test

# Count unique writers per config file
grep -rn "config\.json" --include="*.py" | grep -i "write\|dump\|save" | cut -d: -f1 | sort -u | wc -l
```

If more than 1 module writes to the same file in the same cycle, cascade writes are possible.

### 4.2 Automated CI/CD

Test that concurrent modifications to different fields don't overwrite each other:

```python
def test_no_cascade_overwrite():
    """Verify that two modules updating different fields don't erase each other."""
    import json, tempfile, os

    config = {"timeout": 30, "retry": 3, "batch": 50}
    path = tempfile.mktemp(suffix=".json")
    with open(path, "w") as f:
        json.dump(config, f)

    # Module A changes timeout
    c = json.load(open(path))
    c["timeout"] = 45

    # Module B changes retry (reads before A writes)
    c2 = json.load(open(path))
    c2["retry"] = 5

    # A writes, then B writes
    json.dump(c, open(path, "w"))
    json.dump(c2, open(path, "w"))

    final = json.load(open(path))
    os.unlink(path)

    # B's write erased A's timeout change
    assert final["timeout"] == 45, f"CASCADE: timeout={final['timeout']}, expected 45"
    assert final["retry"] == 5, f"CASCADE: retry={final['retry']}, expected 5"
```

### 4.3 Runtime production

Log every config write with the writer's identity and detect multiple writers per cycle:

```python
import time, json, logging
from collections import defaultdict

class ConfigWriteAuditor:
    """Detects multiple writes to the same config in one cycle."""

    def __init__(self, cycle_seconds: int = 10):
        self.cycle = cycle_seconds
        self.writes: dict[str, list] = defaultdict(list)

    def record_write(self, config_path: str, writer: str, fields_changed: list[str]):
        now = time.monotonic()
        self.writes[config_path].append({
            "time": now, "writer": writer, "fields": fields_changed
        })
        # Check for cascade in current cycle
        cutoff = now - self.cycle
        recent = [w for w in self.writes[config_path] if w["time"] > cutoff]
        if len(recent) > 1:
            writers = [w["writer"] for w in recent]
            logging.warning(
                f"CASCADE RISK: {config_path} written {len(recent)}x in {self.cycle}s by {writers}"
            )
```

## 5. Fix

### 5.1 Immediate fix

Use field-level updates instead of full-file rewrites:

```python
import json
import threading

_config_lock = threading.Lock()

def update_config_field(filepath: str, key: str, value, writer: str = "unknown"):
    """Update a single field without overwriting the entire file."""
    with _config_lock:
        with open(filepath) as f:
            config = json.load(f)
        config[key] = value
        with open(filepath, "w") as f:
            json.dump(config, f, indent=2)
```

### 5.2 Robust fix

Use a centralized config manager where modules propose changes and a single writer applies them atomically:

```python
import json, threading, tempfile, os
from datetime import datetime, timezone

class ConfigManager:
    """Centralized config with atomic updates and audit trail."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.lock = threading.Lock()
        self.pending: list[dict] = []

    def propose(self, writer: str, key: str, value):
        """Queue a field change (does not write immediately)."""
        self.pending.append({"writer": writer, "key": key, "value": value,
                             "time": datetime.now(timezone.utc).isoformat()})

    def apply_all(self):
        """Apply all pending changes in one atomic write."""
        with self.lock:
            with open(self.filepath) as f:
                config = json.load(f)

            for change in self.pending:
                config[change["key"]] = change["value"]

            # Atomic write
            dir_name = os.path.dirname(self.filepath) or "."
            fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp, self.filepath)

            applied = len(self.pending)
            self.pending.clear()
            return applied
```

## 6. Architectural Prevention

The rule: **no module ever writes an entire config file**. Modules propose field-level changes to a ConfigManager. The manager batches all proposals per cycle and applies them in one atomic write at the end of the cycle.

This eliminates cascade writes because there is exactly one write operation per cycle, containing all changes from all modules. No intermediate state exists that could be overwritten.

For systems that cannot use a centralized manager, an alternative is to split the config into per-module files: `config_timeout.json`, `config_retry.json`, etc. Each module owns its file exclusively. A merge step combines them into a unified view when needed.

## 7. Anti-patterns to Avoid

1. **Full-file read-modify-write for single-field changes.** Reading the entire JSON, changing one field, writing the entire JSON back is the root cause. Use field-level updates or a config manager.

2. **Multiple modules writing the same file per cycle.** If `grep` shows 5 different modules writing to `config.json`, cascade writes are inevitable.

3. **Caching config in memory across cycle boundaries.** A module that reads config at the start of a cycle and writes it at the end may overwrite changes made by other modules during the cycle.

4. **No audit trail on config writes.** Without knowing who wrote what and when, diagnosing a cascade requires reading 5 modules' code. Log every write with its source.

5. **Testing modules in isolation.** Each module's config update works perfectly alone. The bug only appears when multiple modules update in the same cycle.

## 8. Edge Cases and Variants

**Variant 1: Config merge on restart.** The system merges a default config with a runtime config on startup. If the merge logic uses full-file write, it can overwrite runtime changes accumulated since the last restart.

**Variant 2: Distributed config.** Two instances of the same system on different servers both write to a shared config (e.g., via NFS or S3). The cascade happens across machines, making it even harder to diagnose.

**Variant 3: Nested config fields.** Module A updates `config["llm"]["timeout"]` and Module B updates `config["llm"]["model"]`. If both read, modify their nested field, and write the entire `config["llm"]` block, they overwrite each other's nested changes.

**Variant 4: Environment variable overlay.** A config file is read, then env vars override some fields. If a module writes the config back without the env var overlay, the overrides are lost.

## 9. Audit Checklist

- [ ] Each config file has at most 1 writer module per cycle (or uses a ConfigManager)
- [ ] No module performs full-file read-modify-write for single-field changes
- [ ] Config writes are logged with the writer's identity and the fields changed
- [ ] A CI test verifies that concurrent field updates don't overwrite each other
- [ ] The config file has a `_last_modified_by` field for quick forensic analysis

## 10. Further Reading

- Short pattern: [Pattern 03 — Penalty Cascade](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-03) (related: cascading parameter modifications)
- Related patterns: #11 (Race Condition on Shared File — concurrent writes), #04 (Multi-File State Desync — cascading across multiple files)
- Recommended reading:
  - "Designing Data-Intensive Applications" (Martin Kleppmann), chapter 7 on transactions — the same read-modify-write problem at database scale
  - etcd documentation on atomic compare-and-swap — how distributed systems solve this exact problem
