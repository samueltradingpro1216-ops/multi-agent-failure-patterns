# Pattern #42 — Orphan Module No Callers

**Category:** Dead Code & Architecture
**Severity:** Low
**Tags:** dead-code, orphan-module, false-coverage, abandoned-feature, import-graph

---

## 1. Observable Symptoms

An orphan module is invisible by definition — its symptoms are absences rather than failures:

- Running a full-project import graph analysis reveals one or more `.py` files that no other file imports, directly or transitively.
- `git log --follow` shows the module was last modified months or years ago, with commit messages referencing a feature name that no longer appears in active code.
- The project's test suite contains a `tests/test_orphan_module.py` file. All its tests pass. No one can name what production feature those tests exercise.
- A developer searching for how a particular domain operation works finds the orphan module, reads it thoroughly, and implements their new feature based on its patterns — which may be outdated or incorrect.
- Dependency analysis tools (`import-linter`, `pydeps`) show a disconnected component in the module graph.
- The `__init__.py` of the package containing the orphan does not import it, meaning it is invisible even to `from package import *`.
- Code coverage is inflated: the orphan's tests contribute to line coverage metrics, giving the impression that more of the codebase is tested than is actually reachable in production.
- Build artifacts (wheels, Docker images) include the orphan module, adding unnecessary bytes and occasionally pulling in dependencies that exist only for the orphan.

---

## 2. Field Story

A social media scheduling platform had an engineering team of twelve engineers maintaining a monorepo with approximately 35,000 lines of Python. The platform allowed brands to compose, schedule, and publish posts across multiple social networks via a unified API.

Eighteen months before the audit, the team had built a module called `src/connectors/rss_ingestion.py` to support automatic scheduling from RSS feeds. The feature was designed to pull content from a brand's blog RSS feed, format it into scheduled posts, and queue it for publishing. The module was fully implemented, had 94% test coverage, and the tests all passed.

The feature had been deployed to a beta cohort of five customers. After six weeks, the beta was abandoned: RSS ingestion generated too many duplicate posts and customers found the automated scheduling disruptive. The beta was shut down by removing the API endpoint that triggered the ingestion pipeline. No one removed `rss_ingestion.py` or its tests.

The module sat untouched for eighteen months. During that time:

1. A junior engineer working on a new content import feature found `rss_ingestion.py` and used it as the reference implementation for XML parsing. The XML parsing logic in `rss_ingestion.py` had a known encoding issue with non-UTF-8 feeds that had been identified during the beta but never fixed (the beta was abandoned before the fix was prioritized). The new feature inherited the same encoding bug.

2. The test suite reported 89% overall line coverage. Removing the orphan module and its tests revealed actual production-reachable coverage was 81%. The 8-point gap had been masking under-tested production paths.

3. A new dependency, `feedparser`, appeared in `requirements.txt` solely because `rss_ingestion.py` imported it. When a security advisory was issued for `feedparser`, the security team spent half a day investigating whether the platform was vulnerable — because `feedparser` appeared to be in use. It was not in use. The investigation cost was entirely avoidable.

The module was removed in a single PR. The `feedparser` dependency was removed. The encoding bug in the new feature was found and fixed during the same audit.

---

## 3. Technical Root Cause

Python's import system requires explicit import statements to establish module dependencies. A file that exists on disk but is never referenced by an `import` statement is never loaded by the Python runtime in production. Its definitions, its side effects, and its logic are completely absent from the running system.

The module exists because Python provides no mechanism — at either the language or tooling level — that enforces "every module must be imported by something." The interpreter loads only what is explicitly requested. Orphan modules do not cause `ImportError`; they simply do not participate in execution.

The compound effect on coverage metrics is a structural problem with how coverage tools work. `coverage.py` instruments files that are executed during a test run. If a test file imports and calls functions in the orphan module, those lines are marked as covered. Coverage reports the number as a percentage of all instrumented lines — which typically means all files reachable from the test runner's import path, not all files reachable from production entry points. The orphan module's tests inflate the numerator and denominator in a way that overstates production coverage.

The false dependency problem (e.g., `feedparser` in `requirements.txt`) arises because Python package management operates at the file level, not the import graph level. A package listed in `requirements.txt` is installed regardless of whether any production-reachable code imports it. Static analysis of `requirements.txt` against the import graph is not performed by default in most Python projects.

---

## 4. Detection

### 4.1 Import Graph Analysis with `importlab`

```python
# scripts/find_orphan_modules.py
"""
Build the import graph of the project and identify modules with no importers.
Requires: pip install importlab networkx
Usage: python scripts/find_orphan_modules.py src/ --entry-points src/main.py src/worker.py
"""
import sys
import ast
from pathlib import Path
import argparse
import json


def collect_all_modules(source_root: Path) -> dict[str, Path]:
    """Return mapping of dotted module name -> file path for all .py files."""
    modules: dict[str, Path] = {}
    for py_file in source_root.rglob("*.py"):
        relative = py_file.relative_to(source_root)
        parts = list(relative.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][:-3]  # remove .py
        if parts:
            dotted = ".".join(parts)
            modules[dotted] = py_file
    return modules


def extract_imports(filepath: Path, source_root: Path) -> list[str]:
    """Extract all imported module names (dotted paths) from a file."""
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return []

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                level = node.level or 0
                if level > 0:
                    # Relative import — resolve against current package
                    current_pkg = filepath.relative_to(source_root).parts[:-1]
                    base = ".".join(current_pkg[: len(current_pkg) - (level - 1)])
                    imports.append(f"{base}.{node.module}" if base else node.module)
                else:
                    imports.append(node.module)
    return imports


def build_import_graph(
    all_modules: dict[str, Path], source_root: Path
) -> dict[str, set[str]]:
    """Return {module_name: set of modules it imports that are in this project}."""
    graph: dict[str, set[str]] = {name: set() for name in all_modules}
    module_names = set(all_modules.keys())

    for mod_name, filepath in all_modules.items():
        raw_imports = extract_imports(filepath, source_root)
        for imp in raw_imports:
            # Match against known modules (exact match or prefix match for packages)
            for known in module_names:
                if imp == known or imp.startswith(known + ".") or known.startswith(imp + "."):
                    graph[mod_name].add(known)
    return graph


def find_orphans(
    graph: dict[str, set[str]],
    entry_points: list[str],
) -> list[str]:
    """
    Find modules not reachable from any entry point via the import graph.
    Uses BFS from all entry points.
    """
    reachable: set[str] = set()
    queue: list[str] = list(entry_points)

    while queue:
        current = queue.pop()
        if current in reachable:
            continue
        reachable.add(current)
        for dependency in graph.get(current, set()):
            if dependency not in reachable:
                queue.append(dependency)

    all_modules = set(graph.keys())
    orphans = sorted(all_modules - reachable)
    return orphans


def main():
    parser = argparse.ArgumentParser(description="Find orphan modules with no importers.")
    parser.add_argument("source_root", help="Root directory of the Python source tree")
    parser.add_argument("--entry-points", nargs="+", required=True,
                        help="Entry-point .py files (e.g. src/main.py)")
    parser.add_argument("--json-output", help="Optional path for JSON report")
    args = parser.parse_args()

    source_root = Path(args.source_root).resolve()
    all_modules = collect_all_modules(source_root)
    print(f"Total modules found: {len(all_modules)}")

    graph = build_import_graph(all_modules, source_root)

    # Convert entry-point file paths to dotted module names
    entry_module_names = []
    for ep in args.entry_points:
        ep_path = Path(ep).resolve()
        try:
            relative = ep_path.relative_to(source_root)
            parts = list(relative.parts)
            parts[-1] = parts[-1][:-3]
            entry_module_names.append(".".join(parts))
        except ValueError:
            print(f"Warning: entry point {ep} is not under source_root, skipping")

    orphans = find_orphans(graph, entry_module_names)

    if not orphans:
        print("No orphan modules found.")
        return

    print(f"\nOrphan modules ({len(orphans)}):")
    for orphan in orphans:
        filepath = all_modules.get(orphan, "unknown")
        print(f"  {orphan:<50} {filepath}")

    if args.json_output:
        output = [{"module": o, "file": str(all_modules.get(o, ""))} for o in orphans]
        Path(args.json_output).write_text(json.dumps(output, indent=2))
        print(f"\nJSON report written to {args.json_output}")


if __name__ == "__main__":
    main()
```

### 4.2 Test-to-Production Coverage Gap Analysis

```python
# scripts/coverage_gap_analysis.py
"""
Compare coverage measured during test runs against coverage of only
production-reachable code (reachable from entry points, not test files).
Highlights the inflation caused by orphan module tests.
Requires: coverage.py XML report.
Usage: python scripts/coverage_gap_analysis.py coverage.xml reports/orphans.json
"""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_coverage_by_file(xml_path: Path) -> dict[str, dict]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    results: dict[str, dict] = {}
    for cls in root.iter("class"):
        filename = cls.attrib.get("filename", "")
        lines = cls.findall(".//line")
        total = len(lines)
        hits = sum(1 for l in lines if int(l.attrib.get("hits", 0)) > 0)
        results[filename] = {"total_lines": total, "covered_lines": hits}
    return results


def main():
    if len(sys.argv) < 3:
        print("Usage: coverage_gap_analysis.py <coverage.xml> <orphans.json>")
        sys.exit(1)

    coverage_data = parse_coverage_by_file(Path(sys.argv[1]))
    orphans = json.loads(Path(sys.argv[2]).read_text())
    orphan_files = {o["file"] for o in orphans}

    total_lines = sum(v["total_lines"] for v in coverage_data.values())
    total_covered = sum(v["covered_lines"] for v in coverage_data.values())

    orphan_total = sum(
        v["total_lines"] for f, v in coverage_data.items()
        if any(o in f for o in orphan_files)
    )
    orphan_covered = sum(
        v["covered_lines"] for f, v in coverage_data.items()
        if any(o in f for o in orphan_files)
    )

    real_total = total_lines - orphan_total
    real_covered = total_covered - orphan_covered

    reported_pct = (total_covered / total_lines * 100) if total_lines else 0
    real_pct = (real_covered / real_total * 100) if real_total else 0

    print(f"Reported coverage (including orphan modules): {reported_pct:.1f}%")
    print(f"Production-reachable coverage (orphans excluded): {real_pct:.1f}%")
    print(f"Coverage inflation from orphan modules: {reported_pct - real_pct:.1f} percentage points")
    print(f"\nOrphan module contribution: {orphan_covered} covered lines / {orphan_total} total lines")


if __name__ == "__main__":
    main()
```

### 4.3 Dead Dependency Detection

```python
# scripts/detect_orphan_dependencies.py
"""
Find packages in requirements.txt that are imported only by orphan modules.
These dependencies can be removed once the orphan modules are deleted.
Usage: python scripts/detect_orphan_dependencies.py requirements.txt reports/orphans.json src/
"""
import ast
import sys
from pathlib import Path
import json
import re


def parse_requirements(req_path: Path) -> list[str]:
    packages = []
    for line in req_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip version specifiers
        pkg = re.split(r"[>=<!~\[]", line)[0].strip().lower().replace("-", "_")
        packages.append(pkg)
    return packages


def get_imports_from_file(filepath: Path) -> set[str]:
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0].lower().replace("-", "_"))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                imports.add(node.module.split(".")[0].lower().replace("-", "_"))
    return imports


def main():
    if len(sys.argv) < 4:
        print("Usage: detect_orphan_dependencies.py <requirements.txt> <orphans.json> <src_dir>")
        sys.exit(1)

    req_path = Path(sys.argv[1])
    orphans_path = Path(sys.argv[2])
    src_dir = Path(sys.argv[3])

    packages = parse_requirements(req_path)
    orphans = json.loads(orphans_path.read_text())
    orphan_files = [Path(o["file"]) for o in orphans]

    # Imports used by orphan modules
    orphan_imports: set[str] = set()
    for f in orphan_files:
        if f.exists():
            orphan_imports |= get_imports_from_file(f)

    # Imports used by non-orphan production code
    all_production_imports: set[str] = set()
    for py_file in src_dir.rglob("*.py"):
        if not any(str(py_file).endswith(str(o)) for o in orphan_files):
            all_production_imports |= get_imports_from_file(py_file)

    orphan_only_packages = [
        pkg for pkg in packages
        if pkg in orphan_imports and pkg not in all_production_imports
    ]

    if not orphan_only_packages:
        print("No orphan-only dependencies found.")
        return

    print(f"Dependencies used only by orphan modules ({len(orphan_only_packages)}):")
    for pkg in sorted(orphan_only_packages):
        print(f"  {pkg} — safe to remove after orphan modules are deleted")


if __name__ == "__main__":
    main()
```

---

## 5. Fix

### 5.1 Staged Deletion with Safety Net

```python
# scripts/safe_delete_module.py
"""
Safely delete an orphan module and its associated test file.
Steps: (1) verify zero imports, (2) backup to .orphan_archive/, (3) delete,
(4) remove orphan-only dependencies, (5) run tests.
Usage: python scripts/safe_delete_module.py src/connectors/rss_ingestion.py
"""
import ast
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime


def verify_no_imports(module_path: Path, project_root: Path) -> list[tuple[Path, int]]:
    """Return list of (file, lineno) for any file that imports this module."""
    module_stem = module_path.stem
    callers: list[tuple[Path, int]] = []

    for py_file in project_root.rglob("*.py"):
        if py_file == module_path:
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if module_stem in alias.name:
                        callers.append((py_file, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.module and module_stem in node.module:
                    callers.append((py_file, node.lineno))
    return callers


def find_test_file(module_path: Path, project_root: Path) -> Path | None:
    stem = module_path.stem
    for candidate in project_root.rglob(f"test_{stem}.py"):
        return candidate
    for candidate in project_root.rglob(f"{stem}_test.py"):
        return candidate
    return None


def archive_file(filepath: Path, project_root: Path) -> Path:
    archive_dir = project_root / ".orphan_archive" / datetime.now().strftime("%Y%m%d")
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / filepath.name
    shutil.copy2(filepath, dest)
    return dest


def run_tests(project_root: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--tb=short", "-q"],
        cwd=project_root, capture_output=True, text=True
    )
    print(result.stdout[-2000:])
    return result.returncode == 0


def main():
    if len(sys.argv) < 2:
        print("Usage: safe_delete_module.py <module_path>")
        sys.exit(1)

    module_path = Path(sys.argv[1]).resolve()
    project_root = Path(__file__).parent.parent.resolve()

    print(f"Checking for imports of '{module_path.name}'...")
    callers = verify_no_imports(module_path, project_root)
    if callers:
        print(f"ABORT: {len(callers)} import(s) found:")
        for f, lineno in callers:
            print(f"  {f}:{lineno}")
        sys.exit(1)

    test_file = find_test_file(module_path, project_root)

    confirm = input(
        f"Delete {module_path}"
        + (f" and {test_file}" if test_file else "")
        + "? [y/N] "
    ).strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    # Archive before deletion
    archive_path = archive_file(module_path, project_root)
    print(f"Archived to {archive_path}")
    module_path.unlink()

    if test_file:
        test_archive = archive_file(test_file, project_root)
        print(f"Archived test to {test_archive}")
        test_file.unlink()

    print("Running tests...")
    if run_tests(project_root):
        print("Tests passed. Module deletion is safe to commit.")
        print("Next steps: run detect_orphan_dependencies.py to remove unused packages.")
    else:
        print("Tests FAILED. Restoring from archive...")
        shutil.copy2(archive_path, module_path)
        if test_file:
            shutil.copy2(test_archive, test_file)
        print("Restored. Review failures manually.")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

### 5.2 CI Import Graph Gate

```python
# scripts/ci_import_graph_check.py
"""
CI gate: fail the build if any module in src/ is unreachable from
the declared entry points. Add to CI pipeline as a non-blocking advisory
check initially, then convert to blocking once the backlog is cleared.
Exit code 0 = clean, 1 = orphans found.
Usage: python scripts/ci_import_graph_check.py
"""
import sys
import os
from pathlib import Path

# Inline the import graph logic rather than depending on the scripts/ directory
# being on sys.path in CI.
sys.path.insert(0, str(Path(__file__).parent))
from find_orphan_modules import collect_all_modules, build_import_graph, find_orphans  # type: ignore


ENTRY_POINTS_ENV = "IMPORT_GRAPH_ENTRY_POINTS"
DEFAULT_ENTRY_POINTS = ["main", "worker", "scheduler", "cli"]


def main():
    source_root = Path(os.environ.get("SOURCE_ROOT", "src")).resolve()
    entry_points_raw = os.environ.get(ENTRY_POINTS_ENV, "").split(",")
    entry_points = [e.strip() for e in entry_points_raw if e.strip()] or DEFAULT_ENTRY_POINTS

    all_modules = collect_all_modules(source_root)
    graph = build_import_graph(all_modules, source_root)
    orphans = find_orphans(graph, entry_points)

    # Exclude test files and scripts from orphan check
    orphans = [
        o for o in orphans
        if not any(segment in o for segment in ("test", "tests", "conftest", "scripts"))
    ]

    if not orphans:
        print("Import graph check passed: no orphan modules found.")
        sys.exit(0)

    print(f"Import graph check FAILED: {len(orphans)} orphan module(s) found:")
    for orphan in orphans:
        print(f"  {orphan}")
    print("\nEach module must be reachable from a declared entry point.")
    print(f"Entry points checked: {entry_points}")
    print("To suppress a legitimate orphan (e.g., a plugin loaded via importlib),")
    print("add it to .orphan_allowlist.txt with a justification comment.")
    sys.exit(1)


if __name__ == "__main__":
    main()
```

---

## 6. Architectural Prevention

**Maintain explicit entry-point declarations.** Use `pyproject.toml` `[project.scripts]` and `[project.entry-points]` to declare all production entry points. Any module not reachable from these declarations is a candidate orphan.

**Adopt `import-linter` for architectural boundary enforcement.** `import-linter` allows specifying that only certain modules may import from certain packages, and can be extended to verify that all modules participate in the import graph.

**Separate dead-feature code deletion into the feature flag retirement checklist.** When a feature flag is disabled and removed, the associated module must be deleted in the same PR. Code that is disabled and code that is removed are not equivalent states.

**Use `__init__.py` as an explicit surface area declaration.** Modules not exported from their package's `__init__.py` should be considered internal. An automated check can flag internal modules that are also not imported by any other internal module.

**Archive rather than comment out.** When a feature is paused rather than abandoned, move the module to an `archive/` directory outside the `src/` tree. It remains in git history, is not loaded by the runtime, and is not counted in coverage. The archive directory convention signals intent clearly.

---

## 7. Anti-patterns

**Keeping the orphan "in case we need it later."** Version control preserves deleted code indefinitely. The argument for keeping an orphan in the active codebase because it might be needed is a version control literacy issue, not a legitimate justification.

**Running tests against orphan modules without noting they are orphaned.** A test file for an orphan module should be deleted along with the module. Keeping the tests produces false coverage confidence and false CI assurance.

**Adding the orphan to `__init__.py` imports to "fix" the orphan status.** Importing an orphan to make the import graph checker happy without actually using the module in any production path is a workaround that restores the original problem. The check exists to ensure reachability from a production path, not just from `__init__.py`.

**Leaving orphan dependencies in `requirements.txt`.** An orphan module's dependencies should be removed from `requirements.txt` when the module is deleted. Leaving them creates security audit noise and inflates the attack surface of the installed package set.

---

## 8. Edge Cases

**Plugin architectures loaded via `importlib`.** A module loaded dynamically via `importlib.import_module("src.plugins.rss_ingestion")` at runtime is not detectable by static import graph analysis. These modules are legitimately unreachable in the static graph. They must be registered in an `.orphan_allowlist.txt` with an explanation of the dynamic load mechanism.

**`__init__.py` star exports.** If `src/connectors/__init__.py` contains `from . import *`, then all modules in `src/connectors/` are imported by the package init, making none of them orphans in the graph sense — even if no production code ever calls into them. This is a reason to avoid `import *` in package inits.

**Modules imported only in type-checking blocks.** Code inside `if TYPE_CHECKING:` blocks is not executed at runtime. A module imported only within `TYPE_CHECKING` guards is functionally an orphan at runtime even though it appears in import statements. Coverage for these modules will show 0% at runtime.

**`conftest.py` files.** pytest's `conftest.py` files are loaded automatically by pytest discovery, not by explicit import. They are structurally orphans with respect to static import analysis but are actively used during testing.

**Modules loaded by environment-specific configuration.** A module that is imported only when a specific environment variable is set (e.g., `if os.getenv("ENABLE_RSS"): import rss_ingestion`) may appear as an orphan in static analysis but is conditionally reachable at runtime.

---

## 9. Audit Checklist

```
ORPHAN MODULE AUDIT CHECKLIST
==============================
Repository: ___________________________
Auditor:    ___________________________
Date:       ___________________________

[ ] Run find_orphan_modules.py with all production entry points declared
[ ] Save output to reports/orphans.json
[ ] Run coverage_gap_analysis.py to quantify coverage inflation
[ ] For each orphan candidate:
    [ ] Check git log for last modification date and commit message
    [ ] Determine whether the module is loaded dynamically (importlib, eval)
    [ ] Determine whether the module is a plugin registered via framework config
    [ ] Determine whether it is imported under TYPE_CHECKING blocks only
    [ ] Check if a corresponding test file exists — note if it inflates coverage
[ ] For confirmed orphans (not dynamically loaded, not framework plugins):
    [ ] Run detect_orphan_dependencies.py to identify removable packages
    [ ] Run safe_delete_module.py to delete module and its test file
    [ ] Remove orphan-only packages from requirements.txt
    [ ] Run full test suite to confirm no breakage
[ ] For legitimate dynamic loads:
    [ ] Add to .orphan_allowlist.txt with explanation and owning team
[ ] Add ci_import_graph_check.py to CI pipeline (non-blocking initially)
[ ] After backlog is cleared, convert CI check to blocking
[ ] Update CODEOWNERS or ARCHITECTURE.md to document entry-point declarations
```

---

## 10. Further Reading

- **`pydeps` documentation** — https://pydeps.readthedocs.io — Generates visual import dependency graphs for Python projects. Useful for communicating the scope of orphan modules to non-technical stakeholders.
- **`import-linter` documentation** — https://import-linter.readthedocs.io — Enforces architectural boundaries in the import graph and can be extended to gate on unreachable modules.
- **`coverage.py` — `--source` flag** — https://coverage.readthedocs.io/en/latest/cmd.html — The `--source` flag limits coverage measurement to a specified set of modules. Restricting `--source` to production-reachable modules eliminates orphan inflation.
- **Python `importlib` documentation** — https://docs.python.org/3/library/importlib.html — Essential reading for understanding dynamic import mechanisms that create legitimate orphans not detectable by static analysis.
- **`pyproject.toml` entry points specification (PEP 517/518)** — https://packaging.python.org/en/latest/specifications/pyproject-toml/ — Using `[project.scripts]` and `[project.entry-points]` provides a machine-readable declaration of all production entry points that import graph tools can consume.
- **Kerievsky, J. — *Refactoring to Patterns*,** "Move Accumulation to Collecting Parameter" — Discusses the accumulation patterns that produce orphan modules: features are added to the codebase faster than deprecated features are removed, creating a net positive drift in dead code.
