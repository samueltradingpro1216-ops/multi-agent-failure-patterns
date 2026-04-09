# Pattern #05 — Unit Mismatch 100x

**Category:** Computation & State
**Severity:** Critical
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 1 to 5 days (consequences are often visible immediately, but the root cause is searched in the wrong place)

---

## 1. Observable Symptoms

An action is executed with a magnitude **10x to 100x higher** (or lower) than intended. An API call expected to consume 40 tokens consumes 4,000. A $10 budget is spent as $1,000. A 30-second timeout is interpreted as 30 milliseconds. The formula is correct, the percentages are correct, but the **unit value** is wrong.

The most insidious symptom: the system raises no error. The calculation is mathematically correct — it is the input that is wrong. If `unit_cost = 0.01` when it should be `1.0`, the result will be 100x too small, but Python has no way of knowing that `0.01` is wrong.

The consequences vary depending on the direction of the error. If the value is too large: resource overconsumption, blown budget, rate limits hit. If the value is too small: negligible actions, unusable results, the system appears to "do nothing".

## 2. Field Story (anonymized)

A multi-agent system managed LLM token budgets across several models. The main computation module used `cost_per_token = 0.001` (correct for model A). The monitoring module, developed separately, used `cost_per_token = 0.00001` (copy-pasted from the documentation of a much cheaper model).

The result: monitoring displayed costs 100x lower than reality. The team believed they were spending $50/day while the actual bill was $5,000/day. The bug was discovered upon receiving the first monthly invoice — a $150,000 shock instead of the estimated $1,500.

The cause: a copy-paste of a unit value from one model to another, without verification. The code was identical between the two modules; only the constant differed.

## 3. Technical Root Cause

The bug occurs when **two modules of the same system use different unit values** for the same entity. Each module hardcodes its own constant, copied from documentation or another module, with no centralized source:

```python
# Module A — budget calculation (correct)
COST_PER_TOKEN = 0.001  # $0.001 per token for Model X

# Module B — monitoring (BUG — copied from Model Y, 100x cheaper)
COST_PER_TOKEN = 0.00001  # Wrong! Copied from cheaper model docs

# The formula is identical, only the constant differs:
total_cost = tokens_used * COST_PER_TOKEN
# Module A: 4000 * 0.001 = $4.00 (correct)
# Module B: 4000 * 0.00001 = $0.04 (100x too low)
```

The fundamental problem is the **absence of a single source of truth** for unit values. Each module has its own copy of the constant, and nothing guarantees they are identical. Copy-paste is the infection vector: the constant is correct in the original module, incorrect in the copy.

Variants include:
- Mixing dollars and cents (`amount = 500`: is this $500 or 500 cents?)
- Mixing seconds and milliseconds (`timeout = 30`: 30s or 30ms?)
- Mixing tokens and kiloTokens (`budget = 4`: 4 tokens or 4,000?)
- Using test values in production (`unit_cost = 0.0` or `unit_cost = 1.0` instead of the real value)

## 4. Detection

### 4.1 Manual code audit

List all unit constants and compare them across modules:

```bash
# Search for unit constant definitions
grep -rn "COST_PER\|PRICE_PER\|UNIT_\|_PER_TOKEN\|_PER_UNIT" --include="*.py"

# Search for hardcoded values in calculations
grep -rn "\* 0\.0\|\* 1\.0\|/ 0\.0\|/ 1\.0" --include="*.py"

# Search for suspicious copy-pastes (same variable in multiple files)
for var in COST_PER_TOKEN UNIT_PRICE POINT_VALUE; do
    echo "=== $var ===" && grep -rn "$var" --include="*.py"
done
```

### 4.2 Automated CI/CD

Centralize unit values and test at startup that all modules use the same source:

```python
# test_unit_consistency.py
from config import UNIT_REGISTRY  # Single source of truth

def test_no_hardcoded_units():
    """Ensure no module hardcodes unit values that should come from the registry."""
    import pathlib, re

    # Known unit constants that must come from UNIT_REGISTRY
    forbidden_patterns = [
        r'COST_PER_TOKEN\s*=\s*[\d.]',
        r'PRICE_PER_UNIT\s*=\s*[\d.]',
        r'POINT_VALUE\s*=\s*[\d.]',
    ]

    violations = []
    for py_file in pathlib.Path("src").rglob("*.py"):
        if py_file.name == "config.py":
            continue  # Skip the registry itself
        content = py_file.read_text()
        for pattern in forbidden_patterns:
            for match in re.finditer(pattern, content):
                violations.append(f"{py_file}:{content[:match.start()].count(chr(10))+1}")

    assert not violations, f"Hardcoded unit values found:\n" + "\n".join(violations)
```

### 4.3 Runtime production

Guard on every action: verify that the magnitude is within an expected range before executing:

```python
class MagnitudeGuard:
    """Blocks actions whose magnitude exceeds expected bounds."""

    def __init__(self, bounds: dict[str, tuple[float, float]]):
        self.bounds = bounds  # {"action_name": (min, max)}

    def check(self, action: str, value: float) -> bool:
        if action not in self.bounds:
            return True
        low, high = self.bounds[action]
        if value < low or value > high:
            raise ValueError(
                f"MAGNITUDE GUARD: {action}={value} outside bounds [{low}, {high}]. "
                f"Probable unit mismatch."
            )
        return True

# Usage:
guard = MagnitudeGuard({
    "token_budget": (100, 100_000),
    "api_cost_usd": (0.001, 100.0),
    "timeout_seconds": (1, 300),
})
guard.check("api_cost_usd", 5000.0)  # Raises: probable unit mismatch
```

## 5. Fix

### 5.1 Immediate fix

Identify the correct unit value and fix it in the offending module:

```python
# BEFORE (bug: copied from another model)
COST_PER_TOKEN = 0.00001

# AFTER (correct: value from the right model)
COST_PER_TOKEN = 0.001
```

Then add a guard that blocks out-of-range values:

```python
def compute_cost(tokens: int, cost_per_token: float) -> float:
    cost = tokens * cost_per_token
    if cost > 1000:  # Hard cap: no action should cost more than $1,000
        raise ValueError(f"Cost too high: ${cost:.2f} for {tokens} tokens. Check unit value.")
    return cost
```

### 5.2 Robust fix

Centralize all unit values in a single registry:

```python
"""unit_registry.py — Single source of truth for all unit values."""

UNITS = {
    "gpt-4": {"cost_per_token": 0.00003, "max_tokens": 128000},
    "gpt-4o": {"cost_per_token": 0.0000025, "max_tokens": 128000},
    "claude-opus": {"cost_per_token": 0.000015, "max_tokens": 200000},
    "llama-70b": {"cost_per_token": 0.0, "max_tokens": 8192},  # Free tier
}

def get_unit(model: str, unit: str) -> float:
    """The ONLY way to get a unit value. Raises if model/unit unknown."""
    if model not in UNITS:
        raise ValueError(f"Unknown model: {model}. Add it to unit_registry.py")
    if unit not in UNITS[model]:
        raise ValueError(f"Unknown unit '{unit}' for model {model}")
    return UNITS[model][unit]

# Usage across all modules:
cost = tokens * get_unit("gpt-4", "cost_per_token")
```

## 6. Architectural Prevention

Prevention rests on two principles: **centralization** and **guards**.

**Centralization**: all unit values live in a single file/module (`unit_registry.py` or `config.yaml`). No other module is permitted to define its own unit constants. A CI test verifies that no module hardcodes a value that should come from the registry.

**Guards**: before every action whose magnitude depends on a unit value, a `MagnitudeGuard` verifies that the result is within an expected range. If a calculation produces a token budget of 4 million or a cost of $50,000, this is almost certainly a unit bug — the guard blocks it and raises an alert.

In addition, the unit values in the registry should be validated at system startup by a health check that compares them against an external source (provider API, official documentation).

## 7. Anti-patterns to Avoid

1. **Copy-pasting unit values between modules.** This is the primary infection vector. Always import from the registry.

2. **No guard on computed values.** A `total = budget / unit_value` without a bounds check is a time bomb. If `unit_value` is 100x too small, `total` is 100x too large.

3. **Mixing units within the same pipeline.** A module passing an amount in cents to a module expecting dollars creates a silent mismatch.

4. **Using test values in production.** `unit_cost = 0.0` for "free in dev" that remains in prod = actions with no apparent cost = no alert on overconsumption.

5. **No cross-audit at startup.** The system should verify at boot that all modules share the same unit values for the same entities.

## 8. Edge Cases and Variants

**Variant 1: Pricing change.** The LLM provider changes its prices. The registry is updated, but a secondary module has its own hardcoded copy that remains at the old price. Monitoring displays incorrect costs for weeks.

**Variant 2: Different units by region.** The same service costs $0.01/call in the US and EUR0.01/call in the EU. If the system does not convert currencies, costs are compared without conversion — 1 USD != 1 EUR.

**Variant 3: Floating-point precision.** `0.1 + 0.2 = 0.30000000000000004` in Python. Over millions of operations, precision drift can accumulate significant errors. Use `decimal.Decimal` for financial calculations.

**Variant 4: Zero unit values.** `unit_cost = 0.0` (free tier) in a `budget / unit_cost` calculation → `ZeroDivisionError`. The guard must also check for zero values.

## 9. Audit Checklist

- [ ] All unit values are centralized in a single registry
- [ ] No module hardcodes a unit value (verified by CI test)
- [ ] A guard verifies magnitude before every critical action
- [ ] The registry is validated at startup against a reference source
- [ ] Units are explicitly documented (dollars vs. cents, seconds vs. milliseconds)

## 10. Further Reading

- Corresponding short pattern: [Pattern 05 — Unit Mismatch 100x](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-05)
- Related patterns: #03 (Penalty Cascade — a unit mismatch can amplify a cascade), #04 (Multi-File State Desync — the unit value can be stored in multiple places with different values)
- Recommended reading:
  - "Mars Climate Orbiter" (NASA, 1999) — the most famous historical example of a unit mismatch (imperial vs. metric), which caused the loss of a $125 million satellite
  - OpenAI/Anthropic documentation on per-token pricing — units vary between models and between input/output
