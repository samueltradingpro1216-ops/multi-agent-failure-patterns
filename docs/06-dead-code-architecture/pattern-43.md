# Pattern #43 — Fragile Bare Import

**Category:** Dead Code & Architecture
**Severity:** High
**Tags:** import-error, sys-path, working-directory, deployment-failure, multi-agent

---

## 1. Observable Symptoms

Fragile bare imports produce one of the most confusing class of bugs: code that works perfectly in development and fails completely in production, with no code change between the two environments:

- `ModuleNotFoundError: No module named 'utils'` appears in production logs but never in local test runs or CI.
- The error occurs only when a script is launched from a specific directory, or when launched by a scheduler (cron, Airflow, Celery) rather than interactively.
- Two engineers running the same script get different outcomes depending on their working directory when they execute it.
- In a multi-agent system, an agent launched by an orchestrator fails to import while the same agent launched manually succeeds.
- `sys.path` printed at the start of a failing script shows the current working directory is `/app/agents/preprocessor/` rather than `/app/`, meaning `from utils import helpers` looks for `/app/agents/preprocessor/utils.py` instead of `/app/utils.py`.
- The error disappears when the developer adds `sys.path.insert(0, "/path/to/project/root")` at the top of the script — which is itself a symptom of the underlying problem.
- Docker containers built from the same code fail on import because the `WORKDIR` in the Dockerfile differs from the working directory used during development.
- A script works when run as `python src/agents/preprocessor.py` from the project root but fails when run as `python preprocessor.py` from `src/agents/`.

---

## 2. Field Story

An MLOps team at a mid-size AI company maintained a multi-agent pipeline for model training, evaluation, and deployment. The pipeline consisted of eight Python agents: a data fetcher, a preprocessor, a feature engineer, three model trainers (one per model variant), an evaluator, and a deployer. Each agent was a Python script in its own subdirectory under `src/agents/`.

All agents shared utility functions from `src/utils/`, including a `config_loader.py` that read environment-specific settings and a `metrics_logger.py` that emitted structured logs to the central observability platform. Each agent file began with:

```python
from config_loader import load_config
from metrics_logger import MetricsLogger
```

These imports worked without qualification because every developer ran scripts from the project root (`python src/agents/feature_engineer.py`), and the project root was implicitly on `sys.path` when the interpreter started.

The pipeline was migrated to a Kubernetes-based orchestration system. The orchestration system launched each agent with a command like:

```
cd /app/src/agents/feature_engineer && python feature_engineer.py
```

The `cd` into the agent's directory meant `sys.path[0]` was `/app/src/agents/feature_engineer/`, not `/app/`. `from config_loader import load_config` searched for a file called `config_loader.py` in `/app/src/agents/feature_engineer/` and found nothing. Every agent in the pipeline failed on startup with `ModuleNotFoundError`.

The incident was severe: the training pipeline was offline for six hours while the team diagnosed the issue. The first attempted fix was to add `sys.path.insert(0, "/app")` to the top of each agent script. This worked but hardcoded an absolute path, breaking the pipeline when a second deployment environment used `/workspace/` instead of `/app/`.

The second attempted fix was to change the Kubernetes pod command to always run from the project root. This worked for the Kubernetes deployment but broke a secondary Airflow DAG that was also orchestrating agents and used a different working directory convention. The fix for Kubernetes caused a new failure in Airflow.

The root cause was that the import structure depended on a convention (run from project root) that was never formally specified, never tested under different working directories, and was violated by every orchestration system the team adopted.

The permanent fix required converting all bare imports to absolute package imports, installing the project as a package with `pip install -e .`, and removing all `sys.path` manipulation from agent scripts entirely.

---

## 3. Technical Root Cause

When Python resolves an `import` statement, it searches `sys.path` in order. The first element of `sys.path` is typically the directory containing the script being executed (or an empty string `""`, which means the current working directory). This is a runtime value that changes depending on how the interpreter is invoked.

`from config_loader import load_config` is a bare import: it specifies no package path. Python searches each entry in `sys.path` for a module named `config_loader`. In development, because the script is launched from the project root, `sys.path[0]` is the project root, and `src/utils/config_loader.py` is found only if `src/utils/` is explicitly on `sys.path` — which it often is because the IDE (VS Code, PyCharm) automatically adds the project root and sometimes the `src/` directory to `sys.path` via launch configurations or `.pth` files.

The problem is that `sys.path` is not a property of the code — it is a property of the execution environment. Code that relies on `sys.path` containing a specific directory is making an implicit environmental assumption that is not expressed in the code itself, cannot be verified by static analysis, and is violated whenever the execution environment differs from the development environment.

This is distinct from a relative import (`from ..utils import config_loader`), which is resolved relative to the package structure, and from a fully qualified absolute import (`from src.utils.config_loader import load_config`), which is resolved against the installed package namespace. Both of these are environment-independent.

The multi-agent aggravation occurs because each agent is launched as a separate subprocess, often from a different working directory specified by the orchestrator's manifest file. A single misconfigured working directory in a pod spec or a DAG definition silently changes the resolution behavior of every import in that agent.

---

## 4. Detection

### 4.1 Static Scan for Bare Imports

```python
# scripts/find_bare_imports.py
"""
Find all import statements in the project that use bare module names
(no package prefix) and are not standard library or installed packages.
These are imports that resolve correctly only if sys.path includes the
directory containing the imported module.
Usage: python scripts/find_bare_imports.py src/ --project-packages src
"""
import ast
import sys
import sysconfig
import importlib.util
from pathlib import Path
import argparse
from dataclasses import dataclass


@dataclass
class BareImport:
    filepath: str
    lineno: int
    import_statement: str
    module_name: str
    resolution_risk: str  # 'confirmed_bare', 'possible_bare'


def get_stdlib_module_names() -> set[str]:
    """Return set of standard library top-level module names."""
    return set(sys.stdlib_module_names)  # Python 3.10+


def is_installed_package(module_name: str) -> bool:
    """Check if module_name is an installed third-party package."""
    try:
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            return False
        # If the module's origin is in site-packages, it's third-party
        if spec.origin:
            return "site-packages" in spec.origin or "dist-packages" in spec.origin
        return True  # namespace packages without origin — assume installed
    except (ModuleNotFoundError, ValueError):
        return False


def analyze_import_node(
    node: ast.Import | ast.ImportFrom,
    filepath: Path,
    project_root: Path,
    stdlib_names: set[str],
) -> list[BareImport]:
    results = []
    source_repr = ast.unparse(node)

    if isinstance(node, ast.Import):
        for alias in node.names:
            top_level = alias.name.split(".")[0]
            if top_level in stdlib_names:
                continue
            if is_installed_package(top_level):
                continue
            # Check if this module exists as a file in the project
            candidates = list(project_root.rglob(f"{top_level}.py"))
            if candidates:
                results.append(BareImport(
                    filepath=str(filepath),
                    lineno=node.lineno,
                    import_statement=source_repr,
                    module_name=alias.name,
                    resolution_risk="confirmed_bare",
                ))

    elif isinstance(node, ast.ImportFrom) and node.level == 0:
        # Absolute import with no package prefix
        if node.module is None:
            return results
        top_level = node.module.split(".")[0]
        if top_level in stdlib_names:
            return results
        if is_installed_package(top_level):
            return results
        # Check if it resolves to a file on the project path
        candidates = list(project_root.rglob(f"{top_level}.py")) + \
                     list(project_root.rglob(f"{top_level}/__init__.py"))
        if candidates:
            # Is the resolved file outside the package structure of the importing file?
            importer_package = filepath.parent
            for candidate in candidates:
                if not str(candidate).startswith(str(importer_package)):
                    results.append(BareImport(
                        filepath=str(filepath),
                        lineno=node.lineno,
                        import_statement=source_repr,
                        module_name=node.module,
                        resolution_risk="confirmed_bare",
                    ))
    return results


def scan_directory(source_root: Path, project_root: Path) -> list[BareImport]:
    stdlib_names = get_stdlib_module_names()
    all_bare: list[BareImport] = []

    for py_file in source_root.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                all_bare.extend(analyze_import_node(node, py_file, project_root, stdlib_names))

    return all_bare


def main():
    parser = argparse.ArgumentParser(description="Find fragile bare imports.")
    parser.add_argument("source_dir", help="Directory to scan")
    parser.add_argument("--project-root", default=".", help="Project root directory")
    args = parser.parse_args()

    source_root = Path(args.source_dir).resolve()
    project_root = Path(args.project_root).resolve()
    findings = scan_directory(source_root, project_root)

    if not findings:
        print("No bare imports found.")
        return

    print(f"Bare imports found: {len(findings)}\n")
    for item in sorted(findings, key=lambda x: (x.filepath, x.lineno)):
        print(f"  {item.filepath}:{item.lineno}")
        print(f"    {item.import_statement}")
        print(f"    Risk: {item.resolution_risk}")
        print()


if __name__ == "__main__":
    main()
```

### 4.2 Runtime Path Invariant Test

```python
# tests/test_import_invariants.py
"""
Verify that all project modules can be imported from any working directory.
This test must be run from multiple working directories in CI to catch
environment-dependent import failures.
Add to CI matrix with different working directories.
"""
import subprocess
import sys
import os
import tempfile
from pathlib import Path
import pytest


PROJECT_ROOT = Path(__file__).parent.parent.resolve()
AGENTS_DIR = PROJECT_ROOT / "src" / "agents"
TMP_DIR = Path(tempfile.gettempdir())


ENTRY_POINT_MODULES = [
    "src.agents.preprocessor",
    "src.agents.feature_engineer",
    "src.agents.evaluator",
    "src.agents.deployer",
]

WORKING_DIRECTORIES = [
    PROJECT_ROOT,
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "src" / "agents",
    TMP_DIR,  # Completely unrelated directory — most aggressive test
]


@pytest.mark.parametrize("module", ENTRY_POINT_MODULES)
@pytest.mark.parametrize("working_dir", WORKING_DIRECTORIES)
def test_module_importable_from_any_directory(module: str, working_dir: Path):
    """
    Each entry-point module must be importable regardless of working directory.
    Failure here indicates a bare import that relies on sys.path convention.
    """
    if not working_dir.exists():
        pytest.skip(f"Working directory does not exist: {working_dir}")

    result = subprocess.run(
        [sys.executable, "-c", f"import {module}; print('OK')"],
        cwd=str(working_dir),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},  # Clear PYTHONPATH to test clean import
    )

    assert result.returncode == 0, (
        f"Module '{module}' failed to import from working directory '{working_dir}'.\n"
        f"stderr: {result.stderr}\n"
        f"This indicates a bare import that depends on sys.path including the project root.\n"
        f"Fix: use absolute package imports and install the package with 'pip install -e .'."
    )
```

### 4.3 Docker Working Directory Simulation

```python
# scripts/test_import_in_container_simulation.py
"""
Simulate container launch conditions by testing imports from the WORKDIR
declared in the Dockerfile. Catches bare import failures before deployment.
Usage: python scripts/test_import_in_container_simulation.py Dockerfile src/
"""
import re
import subprocess
import sys
import os
from pathlib import Path


def extract_workdir_from_dockerfile(dockerfile_path: Path) -> str | None:
    content = dockerfile_path.read_text()
    # Find the last WORKDIR directive
    matches = re.findall(r"^WORKDIR\s+(\S+)", content, re.MULTILINE)
    return matches[-1] if matches else None


def get_entry_point_from_dockerfile(dockerfile_path: Path) -> list[str]:
    content = dockerfile_path.read_text()
    # Look for CMD or ENTRYPOINT with python
    for pattern in [r'CMD\s+\["python[^"]*",\s*"([^"]+)"', r'ENTRYPOINT\s+\["python[^"]*",\s*"([^"]+)"']:
        match = re.search(pattern, content)
        if match:
            return [match.group(1)]
    return []


def test_imports_from_workdir(workdir: Path, source_root: Path) -> list[tuple[str, str]]:
    """Return list of (module_path, error) for modules that fail to import from workdir."""
    failures = []
    for py_file in source_root.rglob("*.py"):
        if "test" in py_file.parts or py_file.name.startswith("test_"):
            continue
        # Convert file path to module name
        try:
            rel = py_file.relative_to(source_root)
            parts = list(rel.parts)
            if parts[-1] == "__init__.py":
                continue
            parts[-1] = parts[-1][:-3]
            module_name = ".".join(parts)
        except ValueError:
            continue

        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path = ['']; import {module_name}"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        if result.returncode != 0 and "ModuleNotFoundError" in result.stderr:
            failures.append((module_name, result.stderr.strip().splitlines()[-1]))
    return failures


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("dockerfile", help="Path to Dockerfile")
    parser.add_argument("source_root", help="Source root directory")
    args = parser.parse_args()

    dockerfile = Path(args.dockerfile)
    source_root = Path(args.source_root).resolve()

    workdir_str = extract_workdir_from_dockerfile(dockerfile)
    if not workdir_str:
        print("No WORKDIR found in Dockerfile; using project root.")
        workdir = source_root.parent
    else:
        workdir = Path(workdir_str) if Path(workdir_str).is_absolute() else source_root.parent / workdir_str
        print(f"Simulating container WORKDIR: {workdir}")

    if not workdir.exists():
        print(f"Warning: WORKDIR {workdir} does not exist locally. Using source root parent.")
        workdir = source_root.parent

    failures = test_imports_from_workdir(workdir, source_root)
    if not failures:
        print("All modules import successfully from the container WORKDIR.")
        return

    print(f"\nImport failures from WORKDIR '{workdir}' ({len(failures)}):")
    for module, error in failures:
        print(f"  {module}: {error}")
    print("\nFix: use absolute package imports. Run: pip install -e . in the Dockerfile.")
    sys.exit(1)


if __name__ == "__main__":
    main()
```

---

## 5. Fix

### 5.1 Convert to Absolute Package Imports

```python
# pyproject.toml — declare the package so it can be installed
# [build-system]
# requires = ["setuptools>=68", "wheel"]
# build-backend = "setuptools.backends.legacy:build"
#
# [project]
# name = "mlops-pipeline"
# version = "0.1.0"
#
# [tool.setuptools.packages.find]
# where = ["src"]
#
# Install in development mode: pip install -e .
# This adds the src/ directory to the Python path via a .pth file,
# making all packages under src/ importable regardless of working directory.


# src/agents/feature_engineer.py — BEFORE (fragile bare imports)
# from config_loader import load_config          # FRAGILE: needs project root in sys.path
# from metrics_logger import MetricsLogger       # FRAGILE: same issue
# from utils.transforms import normalize_features  # FRAGILE: same issue


# src/agents/feature_engineer.py — AFTER (absolute package imports)
from mlops_pipeline.utils.config_loader import load_config
from mlops_pipeline.utils.metrics_logger import MetricsLogger
from mlops_pipeline.utils.transforms import normalize_features

# Now works from ANY working directory because these resolve against the installed
# package namespace, not against sys.path[0].


# src/utils/config_loader.py — BEFORE (cross-references also had bare imports)
# from secrets_manager import get_secret   # FRAGILE


# src/utils/config_loader.py — AFTER
from mlops_pipeline.utils.secrets_manager import get_secret
```

```dockerfile
# Dockerfile — BEFORE (fragile)
# FROM python:3.11-slim
# WORKDIR /app/src/agents/feature_engineer
# COPY . /app
# RUN pip install -r /app/requirements.txt
# CMD ["python", "feature_engineer.py"]
# ^ Launches from /app/src/agents/feature_engineer/ — breaks bare imports


# Dockerfile — AFTER (correct)
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml /app/
COPY src/ /app/src/
RUN pip install -e /app
# The -e install adds src/ to sys.path via a .pth file, independent of WORKDIR
CMD ["python", "-m", "mlops_pipeline.agents.feature_engineer"]
# python -m resolves relative to installed packages, not working directory
```

### 5.2 Automated Migration Script

```python
# scripts/migrate_bare_imports.py
"""
Automatically rewrite bare imports to absolute package imports.
Maps known bare module names to their correct absolute paths based on
the project's package structure.
Usage: python scripts/migrate_bare_imports.py src/ --package-root mlops_pipeline
IMPORTANT: Run on a clean git branch. Review all changes before committing.
"""
import ast
import sys
from pathlib import Path
import argparse


def build_bare_to_absolute_map(src_root: Path, package_root: str) -> dict[str, str]:
    """
    Scan src_root and build a mapping of bare module name -> absolute module path.
    E.g.: 'config_loader' -> 'mlops_pipeline.utils.config_loader'
    """
    mapping: dict[str, str] = {}
    for py_file in src_root.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            rel = py_file.relative_to(src_root)
        except ValueError:
            continue
        parts = list(rel.parts)
        parts[-1] = parts[-1][:-3]  # strip .py
        bare_name = parts[-1]  # just the filename stem
        absolute_name = package_root + "." + ".".join(parts)
        mapping[bare_name] = absolute_name
    return mapping


class BareImportRewriter(ast.NodeTransformer):
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.changes: list[str] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        if node.level != 0 or node.module is None:
            return node
        top_level = node.module.split(".")[0]
        if top_level in self.mapping:
            old_module = node.module
            new_module = self.mapping[top_level]
            # Handle sub-attributes: 'utils.transforms' -> 'mlops_pipeline.utils.transforms'
            if "." in old_module:
                suffix = old_module[len(top_level):]
                new_module = new_module + suffix
            self.changes.append(f"  from {old_module} -> from {new_module}")
            node.module = new_module
        return node

    def visit_Import(self, node: ast.Import) -> ast.AST:
        new_names = []
        for alias in node.names:
            top_level = alias.name.split(".")[0]
            if top_level in self.mapping:
                old_name = alias.name
                new_name = self.mapping[top_level]
                self.changes.append(f"  import {old_name} -> import {new_name}")
                new_names.append(ast.alias(name=new_name, asname=alias.asname))
            else:
                new_names.append(alias)
        node.names = new_names
        return node


def rewrite_file(filepath: Path, mapping: dict[str, str], dry_run: bool) -> int:
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return 0

    rewriter = BareImportRewriter(mapping)
    new_tree = rewriter.visit(tree)
    if not rewriter.changes:
        return 0

    print(f"{filepath}:")
    for change in rewriter.changes:
        print(change)

    if not dry_run:
        ast.fix_missing_locations(new_tree)
        import astor  # pip install astor
        new_source = astor.to_source(new_tree)
        filepath.write_text(new_source, encoding="utf-8")

    return len(rewriter.changes)


def main():
    parser = argparse.ArgumentParser(description="Rewrite bare imports to absolute package imports.")
    parser.add_argument("source_dir")
    parser.add_argument("--package-root", required=True,
                        help="Top-level package name (e.g. mlops_pipeline)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show changes without writing files")
    args = parser.parse_args()

    src_root = Path(args.source_dir).resolve()
    mapping = build_bare_to_absolute_map(src_root, args.package_root)
    print(f"Module mapping built: {len(mapping)} entries\n")

    total_changes = 0
    for py_file in src_root.rglob("*.py"):
        total_changes += rewrite_file(py_file, mapping, args.dry_run)

    mode = "dry run" if args.dry_run else "applied"
    print(f"\nTotal changes ({mode}): {total_changes}")
    if args.dry_run and total_changes > 0:
        print("Run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
```

---

## 6. Architectural Prevention

**Install the project as a package in all environments.** `pip install -e .` in development and `pip install .` in production containers eliminates `sys.path` manipulation entirely. The package's import namespace is resolved via the installed `.pth` file, not via working directory convention.

**Ban `sys.path.insert` and `sys.path.append` in non-test code.** These calls are almost always a symptom of a bare import problem. A linter rule or pre-commit hook that flags `sys.path` mutations in `src/` provides an automated gate.

**Mandate `python -m package.module` over `python script.py` for agent launch.** The `-m` flag resolves the module against the installed package namespace, not against the working directory. All orchestration system manifests (Dockerfiles, Airflow DAGs, Kubernetes pod specs) should use `-m`.

**Enforce absolute imports via `from __future__ import annotations` + flake8-absolute-import.** The `flake8-absolute-import` plugin flags any non-absolute import in project source files. Combined with a CI gate, this prevents new bare imports from being introduced.

**Test in a clean virtual environment with `PYTHONPATH=""`.** CI pipelines should run at least one job with `PYTHONPATH` explicitly cleared, to ensure the test suite does not pass only because a prior CI step added something to `PYTHONPATH`.

---

## 7. Anti-patterns

**Adding `sys.path.insert(0, os.path.dirname(__file__))` to each agent script.** This is a band-aid that hardcodes a relative path assumption. It fails when the script is imported rather than executed directly, and it propagates the underlying problem rather than solving it.

**Setting `PYTHONPATH` in the deployment environment as the fix.** Setting `PYTHONPATH=/app` in a Docker environment variable or Kubernetes env block solves the immediate symptom but creates a fragile, invisible dependency. Any new deployment context must remember to set the same variable, or the failure recurs.

**Copying utility modules into each agent's directory.** Solving the import problem by duplicating `config_loader.py` into every agent's directory creates N copies of the same logic that must be maintained in sync. This transforms an import architecture bug into a code duplication maintenance burden.

**Using `__init__.py` to re-export everything at the top level.** Creating a top-level `__init__.py` that imports every utility module (`from .utils.config_loader import *`) allows bare imports to resolve against the package root. This hides the architectural problem and creates a god-object `__init__.py`.

---

## 8. Edge Cases

**Namespace packages (no `__init__.py`).** PEP 420 namespace packages do not require `__init__.py`. The `find_bare_imports.py` script must account for directories without `__init__.py` that are still valid packages. The `pip install -e .` fix requires `tool.setuptools.packages.find` to be configured to discover namespace packages.

**`__main__.py` modules.** A module run with `python -m package` executes `package/__main__.py`. Imports inside `__main__.py` resolve against the installed package, not the working directory. However, if `__main__.py` uses `from . import submodule` (relative import), the package must be properly installed for the relative import to resolve. Running `python package/__main__.py` directly (without `-m`) breaks relative imports.

**Conditional imports inside functions.** A bare import inside a function body (`def run(): from config_loader import load_config`) is only evaluated when the function is called, not at module load time. Import scanners that only check top-level statements will miss these. The runtime invariant test (Section 4.2) will catch them.

**`importlib.import_module` with dynamic module names.** `importlib.import_module("config_loader")` has the same bare-import fragility as `import config_loader` but is invisible to AST-based scanners because the module name is a string, not a syntactic import node. These require manual audit.

**Editable installs in multi-Python-version environments.** `pip install -e .` generates a `.pth` file specific to the Python version and virtual environment used during installation. If agents in the same system use different virtual environments or Python versions, each must have the package installed separately.

---

## 9. Audit Checklist

```
FRAGILE BARE IMPORT AUDIT CHECKLIST
=====================================
Repository: ___________________________
Auditor:    ___________________________
Date:       ___________________________

[ ] Run find_bare_imports.py against all src/ directories
[ ] Identify all confirmed_bare findings (module exists in project, not installed package)
[ ] Check for sys.path.insert / sys.path.append in any non-test source files
[ ] Check CI environment for PYTHONPATH being set (mask for underlying problem)
[ ] Confirm pyproject.toml or setup.py declares the project as an installable package
[ ] Confirm 'pip install -e .' is used in development environment setup docs
[ ] For each confirmed bare import:
    [ ] Identify the correct absolute package path for the imported module
    [ ] Verify no other bare import chains exist in the same file
[ ] Run migrate_bare_imports.py with --dry-run to preview changes
[ ] Apply migrations on a dedicated git branch
[ ] Add PYTHONPATH="" to at least one CI job to test clean imports
[ ] Run test_import_invariants.py from all declared working directories
[ ] Run test_import_in_container_simulation.py against each Dockerfile
[ ] Confirm all agents launch using 'python -m package.module', not 'python script.py'
[ ] Add pre-commit hook to flag sys.path mutations in src/
[ ] Add flake8-absolute-import to linting pipeline
[ ] Update deployment manifests (Kubernetes, Airflow, cron) to use python -m
[ ] Run full test suite in CI with PYTHONPATH cleared
[ ] Document the package install step in the deployment runbook
```

---

## 10. Further Reading

- **Python Packaging User Guide — "Packaging Python Projects"** — https://packaging.python.org/en/latest/tutorials/packaging-projects/ — The canonical reference for creating an installable package with `pyproject.toml`. The editable install (`pip install -e .`) mechanism is the correct long-term fix for bare import fragility.
- **PEP 328 — Imports: Multi-Line and Absolute/Relative** — https://peps.python.org/pep-0328/ — Defines the semantics of absolute versus relative imports in Python. Understanding this PEP clarifies exactly what `from config_loader import ...` resolves to and why.
- **Python `sys.path` documentation** — https://docs.python.org/3/library/sys.html#sys.path — Documents the initialization order of `sys.path`, including the role of `PYTHONPATH`, `.pth` files, and the script's directory. Essential for understanding why working directory matters.
- **`flake8-absolute-import`** — https://pypi.org/project/flake8-absolute-import/ — flake8 plugin that enforces absolute imports across the codebase. Lightweight CI gate for preventing new bare imports.
- **`python -m` documentation** — https://docs.python.org/3/using/cmdline.html#cmdoption-m — Documents the difference between `python script.py` and `python -m package.module`. The key distinction is that `-m` inserts the current directory into `sys.path[0]` rather than the script's directory, and resolves against the installed package namespace.
- **PEP 517/518 and `pyproject.toml`** — https://peps.python.org/pep-0517/ https://peps.python.org/pep-0518/ — Modern Python packaging specifications. Using `pyproject.toml` with `setuptools` or `hatchling` provides a declarative, reproducible package installation that eliminates `sys.path` manipulation.
- **Kubernetes Pod spec — `command` and `args` fields** — https://kubernetes.io/docs/tasks/inject-data-application/define-command-argument-container/ — The Kubernetes documentation on pod launch commands, directly relevant to ensuring agents are launched with `python -m` rather than `python script.py` and from a consistent working directory.
