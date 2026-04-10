# Pattern #40 — Dead Functions

**Category:** Dead Code & Architecture
**Severity:** Low
**Tags:** dead-code, maintenance-burden, architectural-drift, static-analysis

---

## 1. Observable Symptoms

Dead functions rarely announce themselves with crashes or test failures. Their presence is felt in subtler ways:

- A new engineer asks in a code review, "Is `normalize_event_payload()` still used somewhere? I cannot find any callers." No one can answer quickly.
- Grep searches for a function name return only its definition — zero call sites.
- Coverage reports show entire functions with 0% line coverage despite passing test suites (indicating the tests themselves do not call the function, and neither does production code).
- Refactoring efforts stall because developers are afraid to delete code they do not understand. "Maybe it's called dynamically via `getattr`?"
- Documentation references functions that no longer appear in any import chain, creating drift between docs and reality.
- Static analysis tools (`vulture`, `pylint --disable=all --enable=W0611`) emit warnings that accumulate and get ignored.
- Pull request diffs grow unexpectedly large when someone finally tries to remove what turns out to be a cluster of interconnected dead functions.
- `git blame` shows dead functions authored years ago by engineers who have since left the organization, making intent impossible to reconstruct.

---

## 2. Field Story

An internal analytics toolkit at a mid-size data infrastructure company had grown organically for four years. The codebase contained roughly 18,000 lines of Python across forty modules. The team maintained dashboards for internal product usage, billing reconciliation, and capacity planning.

During a quarterly engineering audit, a senior engineer ran `vulture` for the first time on the full repository. It reported 47 potentially dead functions. Manual review confirmed 31 were genuinely unreachable from any production entry point.

The most instructive case was a function called `recalculate_session_weights()`. It had been written during a migration from one session-tracking backend to another. The migration completed, but this recalculation utility remained. Over 18 months, two other engineers had independently found it while searching for "session" and "weight" logic. Both had read it carefully, concluded it represented the "approved" way to handle that calculation, and copied its algorithm — bugs included — into new functions. The dead function had become a silent source of logic propagation.

A second cluster of dead functions, about nine in total, lived in a module originally built for a partner data export feature. The feature had been descoped without a corresponding code removal. These functions had been tested in isolation; all tests passed. The passing tests gave the team false confidence that the export pipeline was healthy, when in fact it was never invoked in production at all.

Removing the 31 dead functions reduced the codebase by approximately 900 lines. More significantly, it eliminated two instances of copied logic containing the same off-by-one error in a date-range boundary calculation.

---

## 3. Technical Root Cause

Dead functions accumulate through several distinct mechanisms, each worth understanding separately.

**Feature abandonment without cleanup.** A feature is scoped, partially implemented, then deprioritized. The entry-point wiring is removed (the route, the job scheduler call, the CLI command) but the underlying implementation functions remain. Python does not enforce that every defined symbol must be referenced.

**Refactoring without deletion.** A developer rewrites a function and introduces a replacement with a better name or signature. The old function is no longer called but is not deleted, either out of caution ("maybe someone uses it") or oversight.

**API surface over-exposure.** A module exposes many public functions intending external use. The external caller is never built or is built against only a subset. The remaining public functions become dead weight.

**Dynamic call obfuscation.** A function is written expecting to be called via `getattr(obj, method_name)()` or a dispatch table. The dispatch table is later removed but the function remains. No static analysis tool will flag this correctly without additional context.

**Copy-paste survival.** A function is dead in its original module but an identical or near-identical copy exists in another module and is actively called. The original is forgotten rather than consolidated.

At the Python language level, there is no compile-time elimination of unreachable symbols. The interpreter loads and compiles all definitions in a module upon import, regardless of whether those definitions are ever invoked. The cost is paid in developer cognition, not runtime performance — but that cognitive cost compounds over time.

---

## 4. Detection

### 4.1 Automated Static Analysis with `vulture`

```python
# scripts/audit_dead_code.py
"""
Run vulture across the project and emit a structured report.
Requires: pip install vulture
Usage: python scripts/audit_dead_code.py
"""
import subprocess
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
SOURCE_DIR = PROJECT_ROOT / "src"
WHITELIST_FILE = PROJECT_ROOT / "vulture_whitelist.py"
MIN_CONFIDENCE = 80  # percent; lower values increase false positives


def run_vulture() -> list[dict]:
    cmd = [
        sys.executable, "-m", "vulture",
        str(SOURCE_DIR),
        str(WHITELIST_FILE) if WHITELIST_FILE.exists() else "",
        "--min-confidence", str(MIN_CONFIDENCE),
        "--sort-by-size",
    ]
    cmd = [c for c in cmd if c]  # remove empty strings

    result = subprocess.run(cmd, capture_output=True, text=True)
    findings = []
    for line in result.stdout.splitlines():
        # vulture output format: path:lineno: message (confidence%)
        if ": unused function " in line or ": unused method " in line:
            parts = line.split(":", 2)
            if len(parts) >= 3:
                findings.append({
                    "file": parts[0],
                    "line": int(parts[1]),
                    "message": parts[2].strip(),
                })
    return findings


def group_by_module(findings: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for finding in findings:
        grouped.setdefault(finding["file"], []).append(finding)
    return grouped


def main():
    findings = run_vulture()
    if not findings:
        print("No dead functions detected.")
        return

    grouped = group_by_module(findings)
    total = sum(len(v) for v in grouped.values())
    print(f"Dead function candidates: {total} across {len(grouped)} files\n")
    for module, items in sorted(grouped.items()):
        print(f"  {module} ({len(items)} candidates)")
        for item in items:
            print(f"    line {item['line']}: {item['message']}")

    # Machine-readable output for CI integration
    output_path = PROJECT_ROOT / "reports" / "dead_functions.json"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(json.dumps({"findings": findings}, indent=2))
    print(f"\nJSON report written to {output_path}")

    # Exit non-zero to allow CI gating (optional; remove if too aggressive)
    if total > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

### 4.2 Coverage-Based Detection

```python
# scripts/detect_uncovered_functions.py
"""
Parse a coverage.py XML report and identify functions with 0% line coverage.
These are strong dead-function candidates when combined with vulture output.
Requires: coverage run + coverage xml already executed.
Usage: python scripts/detect_uncovered_functions.py coverage.xml
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class UncoveredFunction:
    filename: str
    name: str
    start_line: int
    missed_lines: list[int] = field(default_factory=list)


def parse_coverage_xml(xml_path: Path) -> list[UncoveredFunction]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    results: list[UncoveredFunction] = []

    for cls in root.iter("class"):
        filename = cls.attrib.get("filename", "")
        for method in cls.iter("method"):
            name = method.attrib.get("name", "")
            lines = method.findall(".//line")
            if not lines:
                continue
            hits = [int(l.attrib.get("hits", 0)) for l in lines]
            if all(h == 0 for h in hits):
                line_numbers = [int(l.attrib.get("number", 0)) for l in lines]
                results.append(UncoveredFunction(
                    filename=filename,
                    name=name,
                    start_line=min(line_numbers),
                    missed_lines=line_numbers,
                ))
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python detect_uncovered_functions.py <coverage.xml>")
        sys.exit(1)

    xml_path = Path(sys.argv[1])
    if not xml_path.exists():
        print(f"File not found: {xml_path}")
        sys.exit(1)

    uncovered = parse_coverage_xml(xml_path)
    if not uncovered:
        print("All functions have at least partial coverage.")
        return

    print(f"Functions with 0% coverage: {len(uncovered)}\n")
    for fn in sorted(uncovered, key=lambda x: (x.filename, x.start_line)):
        print(f"  {fn.filename}:{fn.start_line} — {fn.name}()")


if __name__ == "__main__":
    main()
```

### 4.3 Git History Cross-Reference

```python
# scripts/dead_function_last_touched.py
"""
For each dead function identified by vulture, report the last git commit
that modified its defining line. Helps prioritize deletion: functions not
touched in 12+ months with no recent rationale are safe removal candidates.
Requires: git in PATH, vulture JSON report from audit_dead_code.py
Usage: python scripts/dead_function_last_touched.py reports/dead_functions.json
"""
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime


def git_blame_line(filepath: str, lineno: int) -> dict:
    result = subprocess.run(
        ["git", "log", "-1", "--pretty=format:%H|%an|%ai|%s",
         "-L", f"{lineno},{lineno}:{filepath}"],
        capture_output=True, text=True,
        cwd=Path(filepath).parent,
    )
    output = result.stdout.strip()
    if not output:
        return {"hash": "unknown", "author": "unknown", "date": "unknown", "subject": ""}
    parts = output.splitlines()[-1].split("|", 3)
    if len(parts) == 4:
        return {"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}
    return {"hash": "unknown", "author": "unknown", "date": "unknown", "subject": ""}


def age_in_days(date_str: str) -> int:
    try:
        dt = datetime.fromisoformat(date_str.strip())
        return (datetime.now(dt.tzinfo) - dt).days
    except (ValueError, TypeError):
        return -1


def main():
    if len(sys.argv) < 2:
        print("Usage: python dead_function_last_touched.py <dead_functions.json>")
        sys.exit(1)

    report_path = Path(sys.argv[1])
    data = json.loads(report_path.read_text())
    findings = data.get("findings", [])

    print(f"{'File:Line':<45} {'Function':<35} {'Age (days)':<12} {'Author'}")
    print("-" * 110)

    for item in findings:
        blame = git_blame_line(item["file"], item["line"])
        age = age_in_days(blame["date"])
        func_name = item["message"].split("'")[1] if "'" in item["message"] else item["message"]
        location = f"{item['file']}:{item['line']}"
        print(f"{location:<45} {func_name:<35} {age:<12} {blame['author']}")


if __name__ == "__main__":
    main()
```

---

## 5. Fix

### 5.1 Safe Deletion Procedure

```python
# scripts/safe_delete_function.py
"""
Helper to assist with safe removal of a dead function.
Steps: (1) confirm zero callers via AST search, (2) display the function
for review, (3) remove it from the source file, (4) run tests.
Usage: python scripts/safe_delete_function.py src/analytics/utils.py normalize_event_payload
"""
import ast
import sys
import textwrap
import subprocess
from pathlib import Path


def find_all_callers(project_root: Path, function_name: str) -> list[tuple[str, int]]:
    """Return (filepath, lineno) for every call to function_name in the project."""
    callers = []
    for py_file in project_root.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == function_name:
                    callers.append((str(py_file), node.lineno))
                elif isinstance(node.func, ast.Attribute) and node.func.attr == function_name:
                    callers.append((str(py_file), node.lineno))
    return callers


def extract_function_source(filepath: Path, function_name: str) -> tuple[int, int, str]:
    """Return (start_line, end_line, source_text) of the function definition."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == function_name:
                start = node.lineno - 1
                end = node.end_lineno
                snippet = "".join(lines[start:end])
                return start, end, snippet
    raise ValueError(f"Function '{function_name}' not found in {filepath}")


def remove_function_from_file(filepath: Path, start_line: int, end_line: int) -> None:
    lines = filepath.read_text(encoding="utf-8").splitlines(keepends=True)
    # Also remove a preceding blank line if present, to avoid double blank lines
    remove_from = start_line
    if remove_from > 0 and lines[remove_from - 1].strip() == "":
        remove_from -= 1
    new_lines = lines[:remove_from] + lines[end_line:]
    filepath.write_text("".join(new_lines), encoding="utf-8")


def run_tests(project_root: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--tb=short", "-q"],
        cwd=project_root,
        capture_output=True, text=True,
    )
    print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
    return result.returncode == 0


def main():
    if len(sys.argv) < 3:
        print("Usage: safe_delete_function.py <filepath> <function_name>")
        sys.exit(1)

    filepath = Path(sys.argv[1]).resolve()
    function_name = sys.argv[2]
    project_root = filepath.parents[1]  # adjust depth as needed

    print(f"Searching for callers of '{function_name}'...")
    callers = find_all_callers(project_root, function_name)
    if callers:
        print(f"ABORT: {len(callers)} caller(s) found:")
        for path, line in callers:
            print(f"  {path}:{line}")
        sys.exit(1)

    print("No callers found. Extracting function for review...")
    start, end, snippet = extract_function_source(filepath, function_name)
    print(textwrap.indent(snippet, "  "))

    confirm = input("\nDelete this function? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    remove_function_from_file(filepath, start, end)
    print(f"Removed '{function_name}' from {filepath}.")

    print("Running tests to verify removal safety...")
    if run_tests(project_root):
        print("Tests passed. Deletion is safe to commit.")
    else:
        print("Tests FAILED after deletion. Review manually.")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

### 5.2 Bulk Removal via Vulture Whitelist

When a function is called dynamically (via `getattr`, plugin systems, or serialization hooks), it is a legitimate false positive. The vulture whitelist pattern prevents noise:

```python
# vulture_whitelist.py
"""
Functions that appear dead to static analysis but are called dynamically.
Each entry must include a comment explaining WHY it is whitelisted.
Entries without explanations should be treated as candidates for deletion.
"""
from src.analytics import exporters, hooks, serializers  # noqa: F401

# Called by the plugin loader via getattr(module, handler_name)()
exporters.csv_export_handler  # noqa: F401
exporters.parquet_export_handler  # noqa: F401

# Called by the webhook dispatcher using event_type -> method_name mapping
hooks.on_user_created  # noqa: F401
hooks.on_session_expired  # noqa: F401

# Called by the serialization framework via __reduce__ protocol
serializers.EventRecord.__reduce__  # noqa: F401
```

---

## 6. Architectural Prevention

**Enforce deletion as part of feature flag removal.** When a feature flag is toggled off permanently, the associated code removal should be a required follow-up ticket, not optional cleanup. Link cleanup tasks directly to flag retirement in the project tracker.

**Use module `__all__` declarations.** Explicitly listing exported symbols forces authors to consciously decide what constitutes the public API. Symbols not in `__all__` that are also not called internally become visible as dead code immediately.

**Introduce a dead-code gate in CI.** Run vulture with `--min-confidence 90` as a non-blocking CI check initially, then progressively tighten as the backlog is cleared. Block merges once the backlog reaches zero.

**Adopt a "strangler fig" refactoring protocol.** When replacing a function, the new function goes live first, all callers migrate, then the old function is deleted in the same PR or an immediate follow-up. The old function must never survive past the migration PR.

**Conduct quarterly dead-code sprints.** Schedule a two-hour team session each quarter specifically to review and delete dead code surfaced by automated tools. Normalize deletion as positive contribution, not mere cleanup.

---

## 7. Anti-patterns

**Commenting out instead of deleting.** A commented-out function provides even less value than a live dead function: it cannot be tested, it does not appear in coverage, and it clutters the diff. Git history preserves deleted code; comments do not improve on history.

**Keeping dead code "just in case."** The argument "we might need it again someday" reflects a misunderstanding of version control. If the function is needed again, git restores it in seconds with full context. Keeping dead code to avoid a future `git log` search is not a trade-off worth making.

**Adding `# TODO: remove this` comments without follow-through.** TODO comments on dead code create the illusion of awareness while guaranteeing the code remains. A TODO with no owner, no deadline, and no linked ticket will survive indefinitely.

**Whitelisting without justification.** Adding a function to the vulture whitelist without a comment explaining the dynamic call mechanism defeats the purpose of the whitelist. The whitelist becomes a garbage bin for false positives that no one audits.

---

## 8. Edge Cases

**Entry points registered via decorators.** Functions decorated with `@app.route`, `@celery.task`, `@click.command`, or similar framework decorators are called externally by the framework, not by Python code in the project. Vulture and AST callers-search will both report these as dead. They must be whitelisted with the decorator name as justification.

**`__init__.py` re-exports.** A function defined in `module_a.py` but re-exported from `__init__.py` may appear dead in its defining file while still being part of the public API. Caller analysis must account for import aliasing.

**Test-only functions.** A utility function used exclusively in test files is not "dead" in the maintenance sense — it serves tests. However, if the tests themselves are dead (never run), then the utility is dead. Coverage combined with test execution counts resolves this.

**Functions used by `eval` or `exec`.** These cannot be detected by any static analysis. Code using `eval`/`exec` with dynamic function name construction must be manually audited or flagged with a whitelist comment.

**Inherited methods never overridden or called.** A method on a base class may be dead in the base class but required by the interface contract for subclasses. Deletion of the base method breaks the contract even if no current subclass calls it via `super()`.

---

## 9. Audit Checklist

```
DEAD FUNCTIONS AUDIT CHECKLIST
===============================
Repository: ___________________________
Auditor:    ___________________________
Date:       ___________________________

[ ] Run vulture --min-confidence 80 and save output to reports/dead_functions.json
[ ] Run coverage.py full suite and generate coverage.xml
[ ] Run detect_uncovered_functions.py against coverage.xml
[ ] Cross-reference vulture output with coverage output; list functions appearing in both
[ ] Run dead_function_last_touched.py to identify age of each candidate
[ ] For each candidate older than 180 days:
    [ ] Search for dynamic call patterns (getattr, dispatch tables, eval)
    [ ] Search for decorator-based registration (@app.route, @celery.task, etc.)
    [ ] Confirm function is not re-exported via __init__.py
    [ ] Confirm function is not referenced in non-Python config (YAML, JSON)
[ ] Add confirmed false positives to vulture_whitelist.py with comments
[ ] Delete confirmed dead functions using safe_delete_function.py
[ ] Run full test suite; confirm all tests pass
[ ] Update vulture_whitelist.py to remove entries for deleted functions
[ ] Commit deletions with message: "remove N dead functions identified by vulture"
[ ] Update CI pipeline to run vulture as non-blocking check if not already present
```

---

## 10. Further Reading

- **`vulture` documentation** — https://github.com/jendrikseipp/vulture — The reference tool for Python dead code detection. Pay particular attention to the whitelist mechanism and the `--sort-by-size` flag for prioritization.
- **`coverage.py` documentation** — https://coverage.readthedocs.io — Branch coverage and the `--show-missing` flag are relevant for identifying zero-coverage code paths.
- **Fowler, M. — *Refactoring: Improving the Design of Existing Code*, 2nd ed.** — Chapter on "Speculative Generality" covers the psychological dynamic that produces dead functions: code written for hypothetical future requirements.
- **PEP 8 — Style Guide for Python Code** — The `__all__` convention for module exports is documented here and is a structural prevention mechanism.
- **`ast` module documentation** — https://docs.python.org/3/library/ast.html — Essential for writing custom callers-search tooling beyond what vulture provides.
- **`pyflakes` documentation** — https://pypi.org/project/pyflakes/ — Complements vulture for detecting unused imports that often co-occur with dead functions.
