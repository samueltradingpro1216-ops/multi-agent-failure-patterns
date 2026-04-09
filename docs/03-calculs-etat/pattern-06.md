# Pattern #06 — Silent NameError in try/except

**Category:** Computation & State
**Severity:** High
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 5 to 30 days (the affected feature simply appears to "not exist" — nobody actively looks for it)

---

## 1. Observable Symptoms

A critical system feature **never executes**, yet no error appears in logs, monitoring, or dashboards. The system runs normally, all other features work, and the failing component is simply invisible.

Upon inspecting the data, one discovers that an entire branch of the code has been dead for weeks or months. An emergency mechanism never worked. A validation module never validated. A periodic clean-up never cleaned. The code is there, looks correct on inspection, but never runs.

The most insidious symptom: the bug only manifests **through the absence of something**. No error, no crash, no suspicious log entry. Just a feature that should exist but does not. It is the archetypal invisible bug.

## 2. Field Story (anonymized)

A multi-agent system had an emergency shutdown mechanism: when an agent was marked as disabled, the supervisor was supposed to force-close its in-progress tasks. The code had been in place for 6 months.

During an incident, a disabled agent had active tasks that kept running. The team activated the emergency mechanism — nothing happened. Inspecting the code, they found the bug: the variable `config` was used 30 lines before its assignment, inside a `try/except Exception: pass` block. The `NameError` had been silently swallowed on every cycle for 6 months. The emergency mechanism had **never worked** — nobody had noticed because no incident had strictly required it before.

The team realized with horror that if the incident had been more severe, they would have had no safety net.

## 3. Technical Root Cause

The bug occurs when a variable is **used before its assignment** inside a `try/except` block that catches `Exception` or uses a bare `except:`:

```python
def process_agent(agent_id: str, is_disabled: bool):
    try:
        # Step 1: emergency check
        if is_disabled:
            if config.get("has_active_tasks"):   # NameError! 'config' does not exist yet
                force_shutdown(agent_id)
                return

        # ... 30 lines of code ...

        # Step 2: read config (AFTER it is used)
        config = read_config(agent_id)

        # Step 3: normal processing
        process(config)

    except Exception:
        pass  # THE NAMEERROR IS SWALLOWED HERE
```

The mechanism is as follows:
1. Python enters the `try` block
2. At the line `config.get(...)`, `config` is not defined → `NameError`
3. The `except Exception` catches the `NameError` (which inherits from `Exception`)
4. The `pass` does nothing — no log, no alert
5. Execution continues as if the `if is_disabled` branch did not exist

The code **looks** correct on inspection: `config` is indeed defined in the function, just further down. But the execution order means the use precedes the assignment on the `is_disabled=True` path. And the generic `except` makes the bug completely invisible.

What makes this bug particularly dangerous: it can live **for months** undetected. The affected branch only executes in a specific case (here, `is_disabled=True`), and that case is rare under normal operation. When it does occur, the critical feature is silently dead.

## 4. Detection

### 4.1 Manual code audit

Search for dangerous `except` blocks:

```bash
# Bare except (catches everything, including SystemExit and KeyboardInterrupt)
grep -rn "except:" --include="*.py" | grep -v "except:\s*#"

# except Exception with pass (silently swallowed)
grep -B1 -A1 "except Exception" --include="*.py" -rn | grep -A1 "pass"

# except Exception without any logging
grep -A3 "except Exception" --include="*.py" -rn | grep -v "log\|print\|raise\|warning\|error"
```

For every `except Exception: pass` found, verify that all variables used inside the `try` are assigned before use across every conditional path.

### 4.2 Automated CI/CD

Configure linters to catch both components of the bug:

```toml
# pyproject.toml — ruff
[tool.ruff.lint]
select = [
    "E",     # pycodestyle errors
    "F821",  # undefined name
    "B",     # flake8-bugbear
    "BLE",   # flake8-blind-except (BLE001: bare except)
    "TRY",   # tryceratops
]

# Specific rules:
# F821: undefined name — catches variables not yet defined
# BLE001: blind except — forbids bare except
# TRY002: raise vanilla Exception — encourages specific exception types
# TRY003: raise within except — forbids raise Exception(...) inside an except
```

Add `mypy --strict`, which detects potentially undefined variables:

```bash
# mypy reports "possibly undefined" when a variable is assigned
# inside an if/elif but used afterward without a default
mypy --strict src/
# error: Name "config" may be undefined
```

### 4.3 Runtime production

Wrap critical `except` blocks with a logger that captures the full stack trace:

```python
import logging
import traceback

logger = logging.getLogger(__name__)

def safe_except_handler(func):
    """Decorator that logs exceptions instead of silently catching them."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(
                f"Exception in {func.__name__}: {type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            # Re-raise or return a safe default based on policy
            return None
    return wrapper
```

## 5. Fix

### 5.1 Immediate fix

Move the assignment before the use:

```python
def process_agent(agent_id: str, is_disabled: bool):
    # FIX: read config FIRST
    config = read_config(agent_id)

    try:
        if is_disabled:
            if config.get("has_active_tasks"):
                force_shutdown(agent_id)
                return
        process(config)
    except Exception as e:
        logging.error(f"Error processing {agent_id}: {e}")
```

### 5.2 Robust fix

Initialize all variables at the top of the scope AND replace the bare except with specific exception types:

```python
def process_agent(agent_id: str, is_disabled: bool):
    config = None  # Explicit initialization

    try:
        config = read_config(agent_id)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.error(f"Cannot read config for {agent_id}: {e}")
        return

    # Code that depends on config is OUTSIDE the try block
    # If config is None here, it is explicit and verifiable
    if config is None:
        logging.error(f"No config available for {agent_id}")
        return

    if is_disabled and config.get("has_active_tasks"):
        force_shutdown(agent_id)
        return

    try:
        process(config)
    except ProcessingError as e:
        logging.error(f"Processing failed for {agent_id}: {e}")
```

## 6. Architectural Prevention

Prevention rests on three strict rules:

**1. Ban `except Exception: pass` at the project level.** Configure ruff/pylint to block bare excepts and overly broad excepts without logging. A pre-commit hook prevents committing code that silently swallows exceptions.

**2. Initialize variables at the top of their scope.** Every variable used inside a `try` block must be initialized before the `try`: `config = None`, `result = []`, `handlers = []`. This eliminates the entire class of "undefined variable in conditional path" bugs.

**3. Separate `try` blocks by responsibility.** Instead of one large `try` wrapping everything, use specific blocks for each risky operation. Each block catches only the exceptions expected for that operation.

## 7. Anti-patterns to Avoid

1. **`except Exception: pass`** — The premier bug silencer. It does not handle errors; it hides them. At minimum: `except Exception: logging.exception("...")`.

2. **A single `try` spanning 50 lines.** The larger the block, the greater the chance that an unexpected exception is swallowed. Break it into small, specific blocks.

3. **Catching `Exception` instead of the specific exception type.** `except FileNotFoundError` is precise. `except Exception` also catches `NameError`, `TypeError`, `AttributeError` — those are bugs, not expected errors.

4. **Variable assigned inside an `if` without an `else`.** If `config` is only assigned in `if condition:`, it is undefined when `condition` is false. Always initialize before the `if`, or add an `else`.

5. **Testing the feature only on the happy path.** If the emergency mechanism is only tested "when there is no emergency", the bug on the emergency path stays invisible.

## 8. Edge Cases and Variants

**Variant 1: Silent AttributeError.** `self.module.process()` when `self.module` is `None` → `AttributeError` swallowed by `except Exception`. The module was never initialized but the code behaves as if it were.

**Variant 2: TypeError inside a generator.** A generator that yields values is called with the wrong argument type. The `TypeError` is caught by the caller, the generator produces nothing, and the pipeline continues with an empty list.

**Variant 3: Masked ImportError.** `from optional_module import feature` inside a try/except. The module is not installed, the `ImportError` is caught, and `feature` is never defined. All subsequent references to `feature` raise `NameError` — caught again.

**Variant 4: Dead code from condition ordering.** Two `if` statements test the same condition. The first catches everything, the second is dead code. Not a NameError, but the same effect: code that never executes, invisibly.

## 9. Audit Checklist

- [ ] No `except Exception: pass` or `except: pass` anywhere in the codebase
- [ ] Every `except` block logs at minimum the exception type and message
- [ ] All variables used inside a `try` block are initialized before the `try`
- [ ] `ruff` is configured with rules BLE001, F821, TRY002
- [ ] `mypy --strict` reports no "possibly undefined" warnings
- [ ] Emergency/error paths are explicitly tested (not just the happy path)

## 10. Further Reading

- Corresponding short pattern: [Pattern 06 — Silent NameError in try/except](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-06)
- Related patterns: #08 (Data Pipeline Freeze — a consumer that does not crash but stops receiving data often has a silent except somewhere), #10 (Survival Mode Deadlock — a silent except in confidence scoring can mask a score that is always zero)
- Recommended reading:
  - "The Pragmatic Programmer" (Hunt & Thomas), section on exceptions: "Crash early, don't hide errors"
  - PEP 8 section on exceptions — the official Python guide recommends never using bare except
