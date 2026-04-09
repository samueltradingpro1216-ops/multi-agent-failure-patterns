# Pattern #20 — Missing File Encoding

**Category:** Multi-Agent Governance
**Severity:** Medium
**Affected Frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average Debugging Time if Undetected:** 1 to 7 days

---

## 1. Observable Symptoms

The defect rarely announces itself loudly. Instead, it surfaces through a constellation of indirect signals that are easy to misattribute to model quality, network glitches, or downstream service failures.

**In development (UTF-8 default locale):** Nothing. Every test passes. Accented characters, emoji, and CJK ideographs round-trip cleanly through every agent's read/write cycle because the operating system's default encoding happens to match the data.

**In production or CI (Windows cp1252, macOS with non-UTF-8 locale, Docker with `LC_ALL=C`, or multi-locale cloud):**

- `UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d in position 412: character maps to <undefined>` appears in agent logs — but only for certain customer messages, seemingly at random.
- Agent output files are written with corrupted characters: `caf\xe9` instead of `café`, boxes instead of Japanese text, question marks replacing Arabic diacritics.
- A summarisation agent that reads back its own prior output raises an exception because it cannot decode what another agent wrote.
- Silent data loss: a `try/except UnicodeError: return ""` guard swallows the error and the downstream agent receives an empty string, producing hallucinated responses or no-ops.
- Intermittent failures on specific customer tickets that contain multilingual content (accented French names, emoji in subject lines, Arabic or Hebrew RTL text).
- The bug is not reproducible locally by any developer whose machine runs UTF-8.
- Error rate spikes Monday morning (tickets from weekend) then falls, creating a pattern that looks like a load issue.

---

## 2. Field Story (Anonymized)

A mid-size e-commerce company built a multilingual customer support bot using a four-agent LangChain pipeline: an **Intake Agent** that parsed incoming email tickets and wrote structured summaries to disk, a **Routing Agent** that read those summaries and assigned priority and department, a **Draft Agent** that generated reply text, and a **Review Agent** that logged approved drafts for human QA.

The system handled English tickets flawlessly for three weeks in production. The first sign of trouble was a spike in customer complaints from their French-speaking user base — replies were garbled or completely absent. The support team initially blamed the LLM ("the model doesn't know French"), upgraded the model tier, and saw no improvement.

An on-call engineer searched the logs and found `UnicodeDecodeError` traces, but only on a subset of tickets. The tickets that failed shared one property: the customer's name or the email body contained accented characters (`é`, `è`, `ç`, `ü`). The production server ran on a Windows Server 2022 instance provisioned by a third-party vendor whose base image set the system locale to `cp1252`. The development team's MacBooks all defaulted to UTF-8.

Tracing the pipeline: the Intake Agent called `open(summary_path, "w")` without an `encoding` parameter, so Python used the system default (`cp1252` on that server). The Draft Agent called `open(summary_path, "r")` — again without encoding. When the Intake Agent had written `café` under `cp1252`, the byte sequence was `63 61 66 e9`. When the Draft Agent tried to read it back assuming `cp1252` (consistent, so actually fine in this specific pair) but then wrote to a *different* log file under a Docker sidecar whose locale was `C` (ASCII), the sidecar raised `UnicodeEncodeError` and logged an empty string. A `try/except Exception: pass` buried four layers deep silently swallowed the error.

The net result: the Review Agent received an empty draft, generated a placeholder acknowledgment, and the customer never received a real answer. Debugging took four days because the error manifested at a different layer than its origin, and no agent produced a clear stack trace in the observability dashboard.

The fix was three lines of change across eight files: add `encoding="utf-8"` to every `open()` call, remove the bare `except Exception: pass` guard, and add a `PYTHONIOENCODING=utf-8` environment variable to the Docker compose file.

---

## 3. Technical Root Cause

Python's `open()` built-in selects its default encoding through `locale.getpreferredencoding(False)`. On Linux systems with `LC_ALL=C` or `LANG=POSIX`, this returns `ANSI_X3.4-1968` (ASCII). On Windows, it returns the system OEM or ANSI code page (commonly `cp1252` for Western European locales or `cp932` for Japanese). On macOS and most modern Linux distributions with a properly configured locale, it returns `UTF-8`.

This means that **the same Python source code behaves differently on different operating systems and locale configurations** — not because of a runtime error, but because of a silent encoding mismatch.

In a multi-agent system, the problem compounds:

1. **Agents run in different environments.** An orchestrator may run on the host OS while tool-executing agents run in Docker containers. Each environment may have a different locale.
2. **LLM outputs are fundamentally unpredictable.** A model asked to write a professional email may include an em dash (`—`, U+2014), a smart quote (`"`, U+201C), or respond in the user's language with characters outside ASCII. The agent developer cannot enumerate every possible output character.
3. **Intermediate files are shared state.** Agent A writes a file; Agent B reads it. If they resolve the encoding differently, the read either fails or produces mojibake silently.
4. **Error suppression hides the root cause.** Framework-level exception handlers that catch broad exceptions, combined with retry logic, mask the encoding error. The agent retries with the same file, fails the same way, and eventually times out or returns empty content.

**PEP 686** (adopted in Python 3.15 as the `UTF-8 Mode` default, but only opt-in via `PYTHONUTF8=1` or `-X utf8` in earlier versions) acknowledges this problem explicitly. Until UTF-8 mode is universal, the only reliable fix is to specify `encoding="utf-8"` on every text-mode `open()` call.

---

## 4. Detection

### 4.1 Manual Code Audit

Search the entire codebase for `open(` calls that lack an explicit `encoding=` keyword argument. Pay attention to:

- Any `open(path, "r")`, `open(path, "w")`, `open(path, "a")`, `open(path, "r+")` — all text-mode opens.
- `open(path)` with no mode argument (defaults to `"r"`, text mode).
- `pathlib.Path.open()`, `pathlib.Path.read_text()`, and `pathlib.Path.write_text()` without `encoding=`.
- `io.open()` without `encoding=`.
- `tempfile.NamedTemporaryFile(mode="w")` without `encoding=`.

Binary-mode opens (`"rb"`, `"wb"`) are safe and do not need an encoding parameter.

```python
# audit_encoding.py — run from repo root to find all unsafe text-mode opens
import ast
import sys
from pathlib import Path

UNSAFE_MODES = {"r", "w", "a", "r+", "w+", "a+", ""}  # "" = default text mode

def check_file(path: Path) -> list[str]:
    issues = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return issues

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # Match open(...) and io.open(...)
        func = node.func
        is_open_call = (
            (isinstance(func, ast.Name) and func.id == "open")
            or (isinstance(func, ast.Attribute) and func.attr == "open")
        )
        if not is_open_call:
            continue

        # Check if encoding= is present
        kwarg_names = {kw.keyword for kw in node.keywords if isinstance(kw, ast.keyword)}
        # ast.keyword has .arg attribute, not .keyword
        kwarg_names = {kw.arg for kw in node.keywords}
        if "encoding" in kwarg_names:
            continue

        # Check the mode argument — skip binary modes
        mode_val = None
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            mode_val = node.args[1].value
        else:
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode_val = kw.value.value

        if mode_val and ("b" in mode_val):
            continue  # binary mode, safe

        issues.append(
            f"  {path}:{node.lineno} — open() without encoding= "
            f"(mode={mode_val!r})"
        )
    return issues


def main(root: str = ".") -> None:
    all_issues: list[str] = []
    for py_file in Path(root).rglob("*.py"):
        if any(part.startswith(".") for part in py_file.parts):
            continue  # skip hidden dirs
        all_issues.extend(check_file(py_file))

    if all_issues:
        print(f"Found {len(all_issues)} open() call(s) without explicit encoding:\n")
        for issue in all_issues:
            print(issue)
        sys.exit(1)
    else:
        print("All open() calls specify encoding. OK.")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
```

### 4.2 Automated CI/CD

Add a `ruff` rule to the project linter configuration. Ruff implements rule `UP015` (rewrite `open` calls) and, more directly, `PLW1514` (open-without-encoding from Pylint's W1514).

```toml
# pyproject.toml
[tool.ruff.lint]
select = [
    "E", "F", "W",
    "PLW1514",   # open-without-encoding
    "UP015",     # unnecessary open mode argument (catches bare open())
]

[tool.ruff.lint.per-file-ignores]
# Ignore binary-only IO helpers if needed
"src/io_utils_binary.py" = ["PLW1514"]
```

Add to CI pipeline (GitHub Actions example):

```yaml
# .github/workflows/lint.yml (relevant step)
- name: Lint — check encoding hygiene
  run: |
    pip install ruff
    ruff check . --select PLW1514
```

Add a `pytest` fixture that temporarily sets the locale to `C` (ASCII) and runs all agent file I/O tests to catch regressions before they reach production:

```python
# tests/conftest.py
import locale
import os
import pytest

@pytest.fixture(scope="session", autouse=False)
def ascii_locale_env(tmp_path_factory):
    """
    Force ASCII locale for the duration of this test session.
    Agents that call open() without encoding= will fail here,
    not in production.
    """
    original = os.environ.copy()
    os.environ["PYTHONUTF8"] = "0"
    os.environ["LANG"] = "C"
    os.environ["LC_ALL"] = "C"
    yield
    os.environ.clear()
    os.environ.update(original)
```

### 4.3 Runtime Production

Instrument every agent's file write and read with a wrapper that asserts UTF-8 and logs violations:

```python
# agent_io.py — drop-in replacement for open() in agent code
import logging
from pathlib import Path
from typing import IO, Any

_log = logging.getLogger("agent_io")

def agent_open(
    path: str | Path,
    mode: str = "r",
    encoding: str = "utf-8",
    errors: str = "strict",
    **kwargs: Any,
) -> IO:
    """
    Wrapper around open() that always uses UTF-8 and logs
    any attempted override of the encoding parameter.
    Use this in every agent instead of the built-in open().
    """
    if "b" in mode:
        # Binary mode: encoding argument must not be passed
        return open(path, mode, **kwargs)  # noqa: WPS515

    if encoding.lower() not in ("utf-8", "utf8"):
        _log.warning(
            "agent_open called with non-UTF-8 encoding %r for %s. "
            "Overriding to utf-8.",
            encoding,
            path,
        )
        encoding = "utf-8"

    return open(path, mode, encoding=encoding, errors=errors, **kwargs)
```

Monitor `UnicodeDecodeError` and `UnicodeEncodeError` exception counts per agent in your APM tool (Datadog, New Relic, Sentry). A nonzero count is a direct indicator of this pattern.

---

## 5. Fix

### 5.1 Immediate

Add `encoding="utf-8"` to every `open()` call in the agent codebase. This is a mechanical change with no logic impact on correctly-functioning systems.

```python
# BEFORE — broken on non-UTF-8 hosts
with open(output_path, "w") as f:
    f.write(agent_response)

summary = open(summary_path).read()

# AFTER — correct everywhere
with open(output_path, "w", encoding="utf-8") as f:
    f.write(agent_response)

with open(summary_path, encoding="utf-8") as f:
    summary = f.read()
```

For `pathlib`, the equivalent fix:

```python
# BEFORE
Path(output_path).write_text(agent_response)
content = Path(input_path).read_text()

# AFTER
Path(output_path).write_text(agent_response, encoding="utf-8")
content = Path(input_path).read_text(encoding="utf-8")
```

Set `PYTHONUTF8=1` in every Docker image and process launcher as a defense-in-depth measure. This activates Python's UTF-8 mode (PEP 686), which causes `open()` to default to UTF-8 even without the explicit parameter. It does not replace explicit `encoding=` arguments — it is an additional safeguard.

```dockerfile
# Dockerfile
ENV PYTHONUTF8=1
ENV PYTHONIOENCODING=utf-8
```

### 5.2 Robust

Replace all direct `open()` usage in agent I/O with the `agent_open()` wrapper defined in Section 4.3. Add it to the project's shared utility library so all agents import from one place.

Remove all bare `except UnicodeError:` and `except Exception:` guards that silently discard encoding failures. Replace them with explicit logging and a re-raise or a structured error result:

```python
# BEFORE — silent corruption
def read_agent_output(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except UnicodeError:
        return ""  # WRONG: caller gets empty string with no indication of failure

# AFTER — explicit and observable
def read_agent_output(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError as exc:
        logger.error(
            "Encoding error reading agent output %s: %s. "
            "File may have been written without UTF-8 encoding.",
            path,
            exc,
        )
        raise  # propagate so the orchestrator can decide how to handle
```

Add a CI integration test that specifically writes non-ASCII content through the full agent pipeline under an ASCII locale (using the `ascii_locale_env` fixture from Section 4.2) and asserts the output is lossless.

---

## 6. Architectural Prevention

**Establish a project-wide IO contract.** Document in the architecture decision record (ADR) that all text files produced or consumed by agents use UTF-8 encoding. Enforce this at the framework level, not at developer discipline.

**Use a shared IO layer.** All agents import file read/write functions from a single `agent_io` module rather than calling `open()` directly. This module always uses UTF-8. New agents inherit the correct behavior automatically.

**Validate the runtime environment at startup.** The orchestrator should check the effective encoding before launching agents:

```python
# startup_checks.py
import locale
import sys
import logging

_log = logging.getLogger("startup")

def assert_utf8_environment() -> None:
    preferred = locale.getpreferredencoding(False)
    fs_encoding = sys.getfilesystemencoding()

    if preferred.lower().replace("-", "") not in ("utf8", "utf8"):
        _log.warning(
            "System preferred encoding is %r, not UTF-8. "
            "Set PYTHONUTF8=1 or LANG=en_US.UTF-8 to avoid encoding bugs. "
            "All agent_open() calls will override to UTF-8, "
            "but third-party libraries may not.",
            preferred,
        )

    if fs_encoding.lower().replace("-", "") not in ("utf8", "utf8"):
        _log.error(
            "Filesystem encoding is %r. This will cause failures for "
            "file paths containing non-ASCII characters.",
            fs_encoding,
        )
```

**Pin encoding in configuration files.** For agent output formats that use structured files (JSON, YAML, CSV), use libraries that always default to UTF-8 (`json.dump` with `ensure_ascii=False` and explicit `encoding="utf-8"` on the file handle; `csv.writer` with an explicit encoding wrapper).

**Test against a Windows CI runner.** Even if production is Linux, adding a Windows runner to the CI matrix catches encoding bugs before release, because Windows is the most common source of cp1252/cp932 locale surprises.

---

## 7. Anti-patterns to Avoid

**`open(path, "w")` without encoding.** The canonical form of this bug. It appears in tutorials, StackOverflow answers, and legacy code throughout the Python ecosystem. It works until the locale changes.

**`open(path).read()` as a one-liner without encoding.** Common in quick scripts and agent tool implementations. Concise but brittle.

**`except UnicodeError: return ""`**. The most dangerous anti-pattern. It transforms a detectable defect into silent data loss. The caller receives an empty string and may continue processing without any indication that something went wrong. Downstream agents may then hallucinate content to fill the gap.

**Assuming `sys.stdout.encoding` equals `open()` default encoding.** They are configured independently. A process may have UTF-8 stdout (because `PYTHONIOENCODING=utf-8` was set) but cp1252 file I/O (because `PYTHONUTF8` was not set).

**Encoding only the write side.** Some developers add `encoding="utf-8"` to write calls after encountering a `UnicodeEncodeError`, but leave the read calls unmodified. This creates an asymmetric encoding where writes are UTF-8 but reads use the system default — which will fail when reading UTF-8 files on a cp1252 system.

**Using `errors="ignore"` as a workaround.** This silently drops undecodable bytes rather than raising an error. It produces corrupted output without any observable signal, making the defect harder to detect than the original error.

---

## 8. Edge Cases and Variants

**BOM (Byte Order Mark) in UTF-8 files.** Some Windows tools (Notepad, Excel export) write UTF-8 files with a BOM prefix (`\xef\xbb\xbf`). Reading these with `encoding="utf-8"` preserves the BOM as part of the string. Use `encoding="utf-8-sig"` to strip it automatically on read. Agent outputs should never include a BOM; only use `utf-8-sig` for reading externally-sourced files.

**Files written by a previous agent version.** If an old agent version wrote cp1252 files to a shared filesystem and a new (fixed) agent tries to read them with `encoding="utf-8"`, it will raise `UnicodeDecodeError` on any non-ASCII content. A migration script is needed to re-encode legacy files.

**`subprocess` output.** When an agent calls a subprocess and reads its stdout, the encoding of the subprocess's output is controlled by the subprocess's environment, not the Python process. Use `subprocess.run(..., text=True, encoding="utf-8")` explicitly.

**`logging.FileHandler`.** Python's `logging.FileHandler` uses the system default encoding unless `encoding="utf-8"` is passed to the constructor. Agent log files can become unreadable if they contain LLM output with non-ASCII characters.

**JSON serialization.** `json.dumps()` with `ensure_ascii=True` (the default) escapes all non-ASCII characters as `\uXXXX`. This is safe across any encoding but produces verbose output. Use `json.dumps(data, ensure_ascii=False)` for human-readable output, then write to a file with `encoding="utf-8"`.

**Windows `os.environ` on Python < 3.12.** Environment variable values on Windows are UTF-16 internally. Reading them via `os.environ` is safe, but writing them to a file without specifying encoding can still trigger the defect.

---

## 9. Audit Checklist

Use this checklist when reviewing any agent that performs file I/O:

- [ ] Every `open()` call in agent source files includes `encoding="utf-8"` (or `encoding="utf-8-sig"` for externally-sourced files).
- [ ] Every `pathlib.Path.read_text()` and `Path.write_text()` call includes `encoding="utf-8"`.
- [ ] Every `tempfile.NamedTemporaryFile` or `tempfile.SpooledTemporaryFile` opened in text mode includes `encoding="utf-8"`.
- [ ] Every `logging.FileHandler` includes `encoding="utf-8"`.
- [ ] Every `subprocess.run()` or `subprocess.Popen()` that reads text output includes `encoding="utf-8"`.
- [ ] No `except UnicodeError: return ""` or `except UnicodeError: pass` guards exist.
- [ ] The `PYTHONUTF8=1` environment variable is set in all Docker images and process launchers.
- [ ] The ruff rule `PLW1514` (or equivalent) is enabled and enforced in CI.
- [ ] A CI test exercises the agent file I/O pipeline under an ASCII locale.
- [ ] The `agent_io.py` wrapper (or equivalent) is the only entry point for text file I/O across all agents.
- [ ] Architecture decision record documents the UTF-8 IO contract.
- [ ] Legacy files written before the fix were re-encoded or handled with a compatibility layer.

---

## 10. Further Reading

- Python documentation: [Text I/O and encoding](https://docs.python.org/3/library/functions.html#open)
- PEP 686 — UTF-8 Mode as default: [https://peps.python.org/pep-0686/](https://peps.python.org/pep-0686/)
- Ruff rule PLW1514 (open-without-encoding): [https://docs.astral.sh/ruff/rules/unspecified-encoding/](https://docs.astral.sh/ruff/rules/unspecified-encoding/)
- Python Unicode HOWTO: [https://docs.python.org/3/howto/unicode.html](https://docs.python.org/3/howto/unicode.html)
- Joel Spolsky — "The Absolute Minimum Every Software Developer Must Know About Unicode": [https://www.joelonsoftware.com/2003/10/08/the-absolute-minimum-every-software-developer-absolutely-positively-must-know-about-unicode-and-character-sets-no-excuses/](https://www.joelonsoftware.com/2003/10/08/the-absolute-minimum-every-software-developer-absolutely-positively-must-know-about-unicode-and-character-sets-no-excuses/)

**Related patterns in this playbook:**
- [Pattern #18 — Silent State Corruption](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/09-multi-agent-governance/pattern-18-silent-state-corruption.md)
- [Pattern #19 — Unhandled Agent Exception Propagation](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/09-multi-agent-governance/pattern-19-unhandled-exception-propagation.md)
- [Pattern #21 — LLM Pool Contention](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/09-multi-agent-governance/pattern-21-llm-pool-contention.md)
- [Pattern #05 — Filesystem Path Assumptions](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/05-io-persistence/pattern-05-filesystem-path-assumptions.md)
