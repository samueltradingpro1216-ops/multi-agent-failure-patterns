# Pattern #03 — Penalty Cascade (Multiplicative Effect)

**Category:** Loops & Orchestration
**Severity:** High
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 3 to 14 days (the system keeps running but underperforms; the root cause is rarely traced back to parameter adjustments)

---

## 1. Observable Symptoms

A critical system parameter (token budget, timeout, confidence score, batch size) drops to an **absurdly low value** with no error raised. The system keeps running — it does not crash, it raises no exception — but its output is negligible.

A token budget of 4000 drops to 280. A 30-second timeout drops to 2.1 seconds. A confidence threshold of 0.25 drops to 0.018. Actions are technically executed but with parameters so low that the result is useless: LLM responses are truncated, requests time out before completing, scores all fall below the threshold.

The symptom is intermittent. It manifests when **several negative conditions coincide**: a specific day of the week, during off-peak hours, after a series of failures. Each condition independently triggers a reasonable adjustment. Their simultaneous combination creates the failure.

## 2. Field Story (anonymized)

A multi-agent data analysis pipeline used 5 modules that dynamically adjusted the LLM token budget based on current conditions. One module reduced it by 20% on Tuesdays (historically the noisiest day). Another reduced it by 50% after 3 consecutive errors. A third reduced it by 30% outside business hours. A fourth reduced it by 50% if the overall error rate exceeded 5%. A fifth reduced it by 50% during nightly maintenance.

On a Tuesday night at 11 PM, after a series of errors, all 5 modules triggered simultaneously: `0.8 x 0.5 x 0.7 x 0.5 x 0.5 = 0.07`. The budget dropped to 7% of its nominal value — from 4000 tokens to 280. LLM responses were truncated mid-sentence, rendering the analyses unusable. The system ran in this state for 6 hours before a quality alert fired the next morning.

## 3. Technical Root Cause

The bug occurs when **N modules independently adjust the same parameter** within the same processing cycle. Each module performs a read-modify-write without awareness of the other modules' adjustments:

```python
# Module 1: time-based adjustment
config = load_config()
config["token_budget"] *= 0.8  # Tuesday = -20%
save_config(config)

# Module 2: error-based adjustment (same cycle, 2 seconds later)
config = load_config()  # Reads the value ALREADY reduced by Module 1
config["token_budget"] *= 0.5  # 3 errors = -50%
save_config(config)

# Modules 3, 4, 5: same pattern...
# Final result: 0.8 * 0.5 * 0.7 * 0.5 * 0.5 = 0.07 of nominal
```

The fundamental problem is that each module believes it is adjusting from the **nominal** value, when in fact it is adjusting from the value **already reduced** by prior modules. Multipliers are applied in series instead of being accumulated and applied once.

This is a **broken commutativity** problem: the execution order of the modules changes the outcome. If module 1 runs before module 2, the result differs from the reverse order. And since the order depends on scheduling, the bug can appear or disappear in an apparently random fashion.

## 4. Detection

### 4.1 Manual code audit

Look for locations where a parameter is modified via read-modify-write:

```bash
# Search for multiplication/division patterns on config parameters
grep -rn "\*=\s*0\.\|/=\s*[0-9]" --include="*.py" | grep -i "config\|param\|budget\|threshold"

# Search for multiple writes to the same config file
grep -rn "save_config\|write_config\|dump.*json" --include="*.py"

# Count how many files modify the same parameter
grep -rln "token_budget\|risk_percent\|timeout" --include="*.py" | wc -l
```

If more than 2 files modify the same parameter, it is a cascade candidate.

### 4.2 Automated CI/CD

Add a test that simulates all adjustment modules running and verifies the parameter does not drop below a floor:

```python
def test_parameter_floor_after_all_adjustments():
    """Ensure no parameter drops below 20% of nominal after all adjustments."""
    nominal = {"token_budget": 4000, "timeout": 30, "confidence": 0.5}

    # Simulate worst case: all adjusters trigger simultaneously
    adjusted = apply_all_adjustments(nominal, worst_case=True)

    for param, value in adjusted.items():
        ratio = value / nominal[param]
        assert ratio >= 0.2, (
            f"CASCADE DETECTED: {param} dropped to {ratio:.1%} of nominal "
            f"({value} vs {nominal[param]})"
        )
```

### 4.3 Runtime production

Log each adjustment with its source and detect cascades in real time:

```python
import time
from collections import defaultdict

class CascadeDetector:
    """Detects cascading modifications to the same parameter."""

    def __init__(self, window_seconds: int = 60, max_ratio: float = 0.2):
        self.window = window_seconds
        self.max_ratio = max_ratio
        self.writes: dict[str, list[tuple]] = defaultdict(list)

    def record_write(self, param: str, old_value: float, new_value: float, source: str):
        now = time.monotonic()
        self.writes[param].append((now, old_value, new_value, source))
        # Clean old entries
        cutoff = now - self.window
        self.writes[param] = [w for w in self.writes[param] if w[0] > cutoff]

        # Check for cascade
        entries = self.writes[param]
        if len(entries) >= 3:
            first_old = entries[0][1]
            last_new = entries[-1][2]
            if first_old > 0:
                ratio = last_new / first_old
                if ratio < self.max_ratio:
                    sources = [e[3] for e in entries]
                    return {
                        "cascade": True,
                        "param": param,
                        "ratio": ratio,
                        "sources": sources,
                        "message": f"CASCADE: {param} at {ratio:.1%} of nominal via {sources}"
                    }
        return {"cascade": False}
```

## 5. Fix

### 5.1 Immediate fix

Add a hard floor to each parameter. Regardless of reductions, the parameter never drops below X% of nominal:

```python
FLOORS = {
    "token_budget": 1000,    # Minimum 1000 tokens
    "timeout": 5.0,          # Minimum 5 seconds
    "confidence": 0.1,       # Minimum 10%
}

def safe_adjust(param: str, current: float, multiplier: float) -> float:
    """Apply an adjustment with a hard floor."""
    adjusted = current * multiplier
    floor = FLOORS.get(param, current * 0.2)  # Default floor: 20% of current
    return max(adjusted, floor)
```

### 5.2 Robust fix

Replace independent read-modify-write operations with an **accumulative pipeline**:

```python
from dataclasses import dataclass, field

@dataclass
class AdjustmentPipeline:
    """Accumulates adjustments, applies once with a cumulative floor."""

    base_value: float
    min_ratio: float = 0.3   # Cumulative floor: never below 30% of base
    adjustments: list = field(default_factory=list)

    def propose(self, source: str, multiplier: float, reason: str):
        """Register an adjustment WITHOUT applying it."""
        clamped = max(0.3, min(2.0, multiplier))  # Individual clamp
        self.adjustments.append({"source": source, "mult": clamped, "reason": reason})

    def compute(self) -> dict:
        """Apply all adjustments with cumulative floor."""
        cumulative = 1.0
        for adj in self.adjustments:
            cumulative *= adj["mult"]

        # Cumulative floor
        cumulative = max(self.min_ratio, cumulative)
        final = self.base_value * cumulative

        return {
            "base": self.base_value,
            "cumulative_multiplier": round(cumulative, 4),
            "final": round(final, 2),
            "adjustments": self.adjustments,
        }

# Usage: modules propose, pipeline applies once
pipeline = AdjustmentPipeline(base_value=4000, min_ratio=0.3)
pipeline.propose("time_module", 0.8, "Tuesday penalty")
pipeline.propose("error_module", 0.5, "3 consecutive errors")
pipeline.propose("night_module", 0.7, "Off-hours")
result = pipeline.compute()  # final = 4000 * 0.3 = 1200 (floored), not 4000 * 0.28 = 1120
```

## 6. Architectural Prevention

Prevention rests on one principle: **modules never modify a parameter directly**. They submit adjustment proposals to a centralized pipeline that aggregates them and applies the result once per cycle.

This pipeline acts as a **config middleware**: it receives proposals from N modules, computes the cumulative multiplier, applies a configurable floor, and writes the final value. Modules have no direct write access to the parameter.

Additionally, the pipeline's audit trail (which module proposed which adjustment, and what the final result was) enables immediate diagnosis of why a parameter is low. Instead of searching across 5 separate files, everything is centralized in a single log.

## 7. Anti-patterns to Avoid

1. **Independent read-modify-write on a shared parameter.** This is the direct cause of the cascade. Each module must propose a relative adjustment, not write an absolute value.

2. **No floor on critical parameters.** A token budget of 0 or a timeout of 0.1s are values that break the system. Every parameter must have a defined minimum.

3. **Unbounded multipliers.** A module applying `*= 0.01` can on its own reduce a parameter to 1% of nominal. Each individual multiplier must be clamped (e.g., between 0.3 and 2.0).

4. **No audit trail for adjustments.** Without a log of who changed what and when, diagnosing a cascade is a nightmare. Log every adjustment with source, old value, new value, and timestamp.

5. **Testing adjustment modules in isolation.** Each module tested separately works perfectly. The combination is what creates the bug. The cascade test must simulate all modules firing simultaneously.

## 8. Edge Cases and Variants

**Variant 1: Upward cascade.** Instead of reducing, modules increase a parameter (e.g., timeout, budget). The timeout climbs from 30s to 300s, requests each take 5 minutes, and the system is technically functional but extremely slow.

**Variant 2: Cascade on interdependent parameters.** Module A reduces the token budget. Module B increases the retry count because responses are truncated (budget too low). Module C further reduces the budget because the retry count is too high. A feedback loop that amplifies the cascade.

**Variant 3: Temporal cascade.** Modules do not trigger in the same cycle but on successive cycles. The parameter drops by 5% per cycle, too slowly to fire an instant alert, but after 50 cycles it is at 7% of nominal. The "slow cascade" is the hardest to detect.

## 9. Audit Checklist

- [ ] Every critical parameter has an absolute floor defined and documented
- [ ] Adjustment modules propose multipliers instead of writing absolute values
- [ ] A centralized pipeline aggregates adjustments and applies a cumulative floor
- [ ] The audit trail records every adjustment (source, old value, new value, timestamp)
- [ ] An integration test simulates the worst case (all modules fire) and verifies the floor

## 10. Further Reading

- Short pattern: [Pattern 03 — Cascade de Penalites](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-03)
- Related patterns: #11 (Race Condition on Shared File — concurrent read-modify-writes are the same mechanism), #10 (Survival Mode Deadlock — a cascade reducing a confidence threshold can lead to deadlock)
- Recommended reading:
  - "Release It!" (Michael T. Nygard, 2018), chapter on cascading failures and stability patterns
  - CrewAI documentation on shared config management between agents — the "shared state pitfalls" section
