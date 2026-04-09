# Pattern #13 — Silent Config Override

**Category:** Computation & State
**Severity:** High
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 3 to 14 days (the developer sets a parameter manually, it appears to work, then silently reverts within minutes)

---

## 1. Observable Symptoms

A configuration parameter that was **manually set** to a specific value keeps reverting to a different value. The developer sets `timeout=60` in the config file, verifies it, and 5 minutes later `timeout=15` again. No error, no log entry explaining why.

The system has an automated process — a dynamic adjuster, an auto-tuner, an emergency module, or a health check — that periodically overwrites config values based on its own logic. The automated process doesn't log its changes, or logs them in a different file that nobody monitors.

The most frustrating symptom: the developer "fixes" the parameter, tests successfully, walks away, and the bug reappears within minutes. They fix it again, it reverts again. After 3 cycles of this, they start suspecting a ghost in the machine.

## 2. Field Story (anonymized)

A data pipeline team set the LLM `temperature` parameter to 0.1 for deterministic outputs. The pipeline worked correctly for 2 hours, then started producing wildly different outputs for the same inputs. Checking the config, `temperature` was now 0.7.

They reset it to 0.1, ran the pipeline again — correct results. Left for lunch. Came back to find `temperature=0.7` again and inconsistent outputs piling up.

After 3 days, they discovered an "auto-optimization" agent that ran every 30 minutes. This agent analyzed output diversity metrics and concluded that temperature 0.1 produced "insufficiently diverse" results, so it bumped the temperature to 0.7 for "better exploration." The agent had been deployed 2 months earlier by a team member who had since left. It had no log of its changes and no flag in the config file indicating automatic modification.

## 3. Technical Root Cause

The bug occurs when an automated process writes to the same config file that humans (or other modules) also modify, **without any coordination or visibility**:

```python
# Manual fix by developer:
config["temperature"] = 0.1
save_config(config)  # Works! Developer verifies and moves on.

# 30 minutes later — auto-optimizer runs:
def auto_optimize():
    config = load_config()
    diversity = compute_diversity_score()
    if diversity < 0.5:
        config["temperature"] = 0.7   # "Improve diversity"
    save_config(config)               # Silently overwrites the manual fix
    # No log. No flag. No trace.
```

The fundamental problem: **the config file has no concept of "who set this value" or "should this value be protected from automatic changes."** All writers are equal — a manual fix and an automated optimizer have the same access level. The automated process doesn't know (or care) that a human just set this value intentionally.

Variants include:
- A health check that resets parameters to "safe defaults" when it detects anomalies — resetting the intentional change along with the anomaly
- An emergency agent that reduces all budgets to minimum during incidents — and never restores them afterward
- A cron job that syncs config from a template, overwriting runtime customizations

## 4. Detection

### 4.1 Manual code audit

Find all automated config writers:

```bash
# Find all config write operations
grep -rn "save_config\|write_config\|json\.dump.*config\|config\[.*\]\s*=" --include="*.py"

# Filter for automated/scheduled writers (cron, loop, timer)
grep -rn "schedule\|cron\|timer\|periodic\|every.*minute\|setInterval" --include="*.py" -l

# Cross-reference: which scheduled modules also write config?
```

### 4.2 Automated CI/CD

Test that setting a config value manually survives an automated cycle:

```python
def test_manual_config_survives_auto_cycle():
    """Verify that a manually set config value is not overwritten by auto-processes."""
    config_manager = ConfigManager("test_config.json")

    # Simulate manual set
    config_manager.set("temperature", 0.1, source="manual")

    # Simulate one full auto-optimization cycle
    auto_optimizer.run(config_manager)

    # The manual value should survive
    assert config_manager.get("temperature") == 0.1, (
        f"SILENT OVERRIDE: temperature changed to {config_manager.get('temperature')} "
        f"after auto-optimizer ran"
    )
```

### 4.3 Runtime production

Track every config write with its source and alert on unexpected overwrites:

```python
import json, logging
from datetime import datetime, timezone

class AuditedConfig:
    """Config wrapper that logs every write with source attribution."""

    def __init__(self, filepath: str):
        self.filepath = filepath

    def set(self, key: str, value, source: str):
        config = self._load()
        old_value = config.get(key)
        config[key] = value
        config.setdefault("_audit", {})[key] = {
            "set_by": source,
            "set_at": datetime.now(timezone.utc).isoformat(),
            "previous_value": old_value,
        }
        self._save(config)

        if old_value != value:
            logging.info(f"CONFIG: {key} changed {old_value} -> {value} by {source}")

    def get(self, key: str, default=None):
        return self._load().get(key, default)

    def _load(self):
        with open(self.filepath) as f:
            return json.load(f)

    def _save(self, config):
        with open(self.filepath, "w") as f:
            json.dump(config, f, indent=2)
```

## 5. Fix

### 5.1 Immediate fix

Add a `_locked_by` field that prevents automated processes from overwriting manual changes:

```python
def safe_auto_update(config_path: str, key: str, value, source: str):
    """Auto-update that respects manual locks."""
    with open(config_path) as f:
        config = json.load(f)

    lock_info = config.get("_locks", {}).get(key)
    if lock_info and lock_info.get("locked_by") == "manual":
        logging.info(f"AUTO-UPDATE BLOCKED: {key} is manually locked by {lock_info.get('locked_at')}")
        return False

    config[key] = value
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return True
```

### 5.2 Robust fix

Implement a config system with explicit priority levels:

```python
from enum import IntEnum

class ConfigPriority(IntEnum):
    AUTO_DEFAULT = 10      # Automated defaults
    AUTO_OPTIMIZER = 20    # Automated optimization
    MANUAL = 50            # Human-set values
    EMERGENCY = 90         # Emergency overrides
    INVARIANT = 100        # Never changeable

class PriorityConfig:
    """Config where higher-priority writes cannot be overwritten by lower-priority ones."""

    def __init__(self, filepath: str):
        self.filepath = filepath

    def set(self, key: str, value, source: str, priority: ConfigPriority):
        config = self._load()
        meta = config.setdefault("_meta", {})
        current_priority = meta.get(key, {}).get("priority", 0)

        if priority < current_priority:
            return False  # Lower priority cannot overwrite

        config[key] = value
        meta[key] = {
            "priority": int(priority),
            "source": source,
            "set_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save(config)
        return True
```

## 6. Architectural Prevention

The core principle: **every config write must carry a source attribution and a priority level**. Manual changes have higher priority than automated ones. Automated processes cannot overwrite manual changes unless explicitly designed to do so (e.g., emergency overrides).

Implement this via a config manager that wraps all access. No module reads or writes the raw JSON file directly. The manager enforces priority rules, logs every change, and provides a `history(key)` function to trace who changed what and when.

For extra safety, add a `_frozen_until` timestamp: when a human sets a value, it's frozen for N hours (configurable). No automated process can change it during the freeze period.

## 7. Anti-patterns to Avoid

1. **Automated config writers with no logging.** An auto-optimizer that changes parameters silently is a time bomb. Every automated change must be logged with source, old value, new value, and reason.

2. **No distinction between manual and automated writes.** If all writers have equal access, manual fixes are ephemeral — they survive only until the next automated cycle.

3. **Auto-optimization without bounds.** An optimizer that can change any parameter to any value is dangerous. Bound each parameter with min/max ranges and rate-of-change limits.

4. **Deploying automated config writers and forgetting about them.** The auto-optimizer deployed 2 months ago by a departed team member is a classic. Document all automated writers and review them quarterly.

5. **No "who changed this?" forensics.** Without audit metadata in the config, diagnosing a silent override requires reading every module's code. Add `_audit` fields to every config value.

## 8. Edge Cases and Variants

**Variant 1: Env var override conflict.** The config file says `timeout=60`. An env var says `TIMEOUT=15`. The app reads the env var (higher precedence) but the config UI shows 60. The developer thinks the fix worked but the app uses 15.

**Variant 2: Config sync from template.** A deployment script syncs config from a template on every deploy. Any runtime customization is overwritten. The fix is to merge (not replace) during sync.

**Variant 3: Multiple auto-writers fighting.** Auto-optimizer A wants `temperature=0.7` for diversity. Auto-optimizer B wants `temperature=0.1` for consistency. They overwrite each other every 30 minutes, creating oscillating behavior.

**Variant 4: Emergency mode that never un-emergencies.** An emergency agent reduces all parameters to safe minimums during a crisis. The crisis resolves, but the emergency agent has no "restore previous values" logic. Parameters stay at minimum indefinitely.

## 9. Audit Checklist

- [ ] Every config write includes source attribution (who/what changed it)
- [ ] Manual changes have higher priority than automated changes
- [ ] Automated config writers are documented and reviewed regularly
- [ ] A `config history` command shows the last N changes with sources and timestamps
- [ ] Config parameters have defined min/max bounds that automated writers respect

## 10. Further Reading

- Short pattern: [Pattern 03 — Penalty Cascade](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-03) (related: automated parameter adjustments)
- Related patterns: #12 (Cascade Write — multiple writers in one cycle), #10 (Survival Mode Deadlock — an auto-optimizer that lowers thresholds can cause deadlock)
- Recommended reading:
  - HashiCorp Consul documentation on config watches and atomic updates — how production systems handle config coordination
  - "Infrastructure as Code" (Kief Morris) — the principle of immutable config with explicit override mechanisms
