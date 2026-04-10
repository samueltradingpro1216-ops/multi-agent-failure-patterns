# Pattern #30 — Critical Module Never Imported

| Field | Value |
|---|---|
| **ID** | 30 |
| **Category** | Dead Code & Architecture |
| **Severity** | High |
| **Affected Frameworks** | LangChain / CrewAI / AutoGen / LangGraph / Custom |
| **Average Debugging Time (if undetected)** | 7 to 90 days |
| **Keywords** | dead module, never imported, orphaned code, silent gap, missing safety net, risk manager, rate limiter, circuit breaker, integration test gap, import coverage |

---

## 1. Observable Symptoms

This pattern is among the most difficult to detect through standard observability because nothing is broken in the conventional sense. No exceptions are raised. No tests fail. No metrics diverge. The system operates at full capacity — minus the safety net that should have been there.

**Operational symptoms:**

- A safety-related incident occurs (regulatory breach, rate-limit violation, cascading failure, runaway API spend) and post-mortem investigation reveals that a module specifically written to prevent that class of incident was present in the repository but never called.
- The module's unit tests pass in CI. The main application's integration tests pass. There is no test that exercises the two together.
- Engineers initially assume the module was added to a wrong branch or deployed incorrectly. Closer inspection shows the file is present on every node in production — it is simply never imported.
- The module's author is often a different team or contractor from the team that owns the main application. The hand-off between the two never included a verification that the integration was complete.
- Audit log analysis shows zero calls to any function inside the module over its entire deployment lifetime, which can span months.
- The module contains correct, well-tested logic. It would have worked. It simply was never wired in.
- In LLM agent contexts: a compliance filter agent, a spend-cap enforcer, or an output safety classifier is implemented and deployed but never registered in the orchestration graph. Every LLM call bypasses it silently.

**Code-level symptoms:**

- `import risk_manager` (or equivalent) appears only in `risk_manager_test.py` and nowhere else.
- The module defines a class or function with an obvious entry point (e.g., `RiskManager.evaluate()`, `RateLimiter.check()`, `CircuitBreaker.call()`) but grep across the non-test codebase returns zero matches for that entry point.
- The module is present in `requirements.txt` or listed as a project package, but no `__init__.py` re-exports its symbols into the package namespace where the main app would naturally encounter them.
- Architecture documentation describes the module as "integrated," but no integration test exercises the main application code path that passes through it.

---

## 2. Field Story (Anonymized)

A fintech company built an automated compliance monitoring system — internally called "Sentinel" — to support its lending operations. Sentinel used a multi-agent pipeline to ingest transaction events, assess regulatory compliance signals, and generate alerts for human review. The pipeline was built on a custom orchestration layer and gradually migrated toward LangGraph.

During a major sprint to add real-time transaction monitoring, a senior engineer on the risk team wrote `compliance_gate.py`: a module that evaluated each transaction event against a configurable rule set covering transaction velocity limits, jurisdiction-specific reporting thresholds, and sanctions list cross-referencing. The module included a `ComplianceGate.evaluate(event)` method that returned a structured result with a pass/fail decision and a list of triggered rules. Thirty-two unit tests covered edge cases meticulously.

A separate team — the pipeline team — owned the main orchestration code in `pipeline/orchestrator.py`. Their sprint focused on throughput and latency improvements. The integration of `compliance_gate.py` was listed as a task in the sprint board under the risk team's column, marked "done" when the module's unit tests passed.

The missing line was never written:

```python
# This import existed in compliance_gate_test.py
from compliance_gate import ComplianceGate

# This import was never added to pipeline/orchestrator.py
# from compliance_gate import ComplianceGate  ← MISSING
```

The orchestrator processed transactions for 11 weeks without calling `ComplianceGate.evaluate()`. During that period, 340 transactions that would have triggered velocity-limit flags passed without review. The gap was discovered during a regulatory examination when an auditor asked to see the compliance gate's decision log. The log was empty — not because the gate failed to log, but because it was never called.

The immediate remediation required a retrospective review of 11 weeks of transaction data and manual re-evaluation of all flagged records. The compliance team spent six weeks on that work. The total cost of the incident, including regulatory liaison time, engineer hours, and the cost of a third-party audit, was substantial. All of it stemmed from one missing import statement and one missing integration test.

---

## 3. Technical Root Cause

**Why the gap exists:**

The critical module never imported pattern arises at the boundary between two independently verified subsystems. Each subsystem satisfies its own test suite in isolation. The integration — the act of calling one subsystem from the other — is assumed to have been done but was never independently verified.

This is a manifestation of the **test boundary problem**: unit tests verify that a module does what it claims to do given inputs. They do not verify that the module is called. Integration tests verify that two modules interact correctly given their interfaces. They do not verify that the integration is present. Import coverage — whether the module is reachable from the application's entry point — falls into neither category by default.

**Why it is especially dangerous for safety modules:**

Safety modules (rate limiters, circuit breakers, compliance gates, spend caps, output filters) share a structural property: the system appears to function correctly without them. A rate limiter, by definition, only matters when the rate limit is approached. A compliance gate only matters when a non-compliant event occurs. During normal operation, the module's absence is indistinguishable from its presence — unless the unsafe condition is actively tested.

This creates a false signal: "the system is working" means "the system is processing events successfully," not "the system is applying the safety policy."

**Framework-specific aggravation:**

In LangGraph, agents are registered in a graph definition (`graph.add_node`, `graph.add_edge`). A safety node written as a standalone class is not part of the graph until explicitly added. There is no warning when the graph is compiled without it — the graph is simply a different graph from the one the architect intended.

In CrewAI, a guard agent written as a `@tool` or as a `Task` is inactive unless it appears in the `Crew` task list. The crew runs to completion without it and raises no exception.

In LangChain, a callback handler written to intercept LLM calls does nothing unless passed to the `callbacks=` parameter at invocation. A `SafetyCallbackHandler` class sitting in its own file is inert.

**The import statement as integration contract:**

An import statement is not merely a technical mechanism — it is an integration contract. `from compliance_gate import ComplianceGate` in `orchestrator.py` asserts: "this orchestrator uses the compliance gate." The absence of that statement means the contract was never signed, regardless of what the documentation says.

---

## 4. Detection

### 4.1 Manual Code Audit

The most direct detection method is an import coverage sweep: for each module identified as a safety-critical component, verify that it is imported somewhere in the non-test application code.

**Shell command — find all Python files that import a target module:**

```bash
grep -rn "from compliance_gate\|import compliance_gate" \
  --include="*.py" \
  . | grep -v "_test.py" | grep -v "test_"
```

If this returns zero results, the module is never imported by production code.

**Systematic sweep for all modules in a safety directory:**

```bash
# List every module in the safety/ directory, then check whether
# each is imported anywhere outside the test suite.
for module in safety/*.py; do
  name=$(basename "$module" .py)
  count=$(grep -rl "import $name\|from $name" \
    --include="*.py" . \
    | grep -v "_test.py" | grep -v "test_" \
    | wc -l)
  echo "$name: $count non-test importers"
done
```

Any module reporting `0 non-test importers` is a candidate for this pattern.

### 4.2 Automated CI/CD

The following script implements an import coverage gate that can be integrated into any CI pipeline. It accepts a list of "required modules" — modules that must be reachable via import from the application's entry point — and fails the build if any are missing.

```python
# ci_checks/check_import_coverage.py
"""
CI gate: verifies that each module listed in REQUIRED_IMPORTS
is imported (directly or transitively) by at least one non-test
Python source file in the specified application root.

Usage:
    python ci_checks/check_import_coverage.py \
        --app-root src/ \
        --required compliance_gate rate_limiter circuit_breaker

Exit code 1 if any required module has zero non-test importers.
"""
import argparse
import ast
import sys
from pathlib import Path


def collect_imports(file_path: Path) -> set[str]:
    """Return the set of top-level module names imported by a file."""
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Take the root module name: "from a.b import c" → "a"
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def is_test_file(path: Path) -> bool:
    return path.stem.startswith("test_") or path.stem.endswith("_test")


def find_importers(
    module_name: str,
    app_root: Path,
    include_tests: bool = False,
) -> list[Path]:
    """Return all non-test files in app_root that import module_name."""
    importers = []
    for py_file in app_root.rglob("*.py"):
        if not include_tests and is_test_file(py_file):
            continue
        if module_name in collect_imports(py_file):
            importers.append(py_file)
    return importers


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that required modules are imported in application code."
    )
    parser.add_argument("--app-root", required=True, help="Root of application source.")
    parser.add_argument(
        "--required",
        nargs="+",
        required=True,
        help="Module names that must be imported by at least one non-test file.",
    )
    args = parser.parse_args()
    app_root = Path(args.app_root)

    failures: list[str] = []
    for module_name in args.required:
        importers = find_importers(module_name, app_root)
        if importers:
            print(f"  OK  {module_name}: imported by {len(importers)} file(s)")
            for f in importers:
                print(f"        {f}")
        else:
            print(f"  FAIL  {module_name}: imported by 0 non-test files")
            failures.append(module_name)

    if failures:
        print(
            f"\nIMPORT COVERAGE FAILURE: {len(failures)} required module(s) "
            f"have no non-test importers: {failures}"
        )
        print(
            "These modules exist in the repository but are never called "
            "by production code. Add the missing import and integration test."
        )
        return 1

    print(f"\nAll {len(args.required)} required module(s) are imported. OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**CI pipeline integration:**

```yaml
# .github/workflows/ci.yml (excerpt)
- name: Import coverage gate — safety modules
  run: |
    python ci_checks/check_import_coverage.py \
      --app-root src/ \
      --required compliance_gate rate_limiter circuit_breaker spend_cap output_filter
```

### 4.3 Runtime Production

At application startup, emit a structured log line for each safety module confirming it was loaded and wired. The following decorator provides this guarantee without modifying the module itself.

```python
# safety/registration.py
"""
Safety module registration registry.

Each safety module calls SafetyRegistry.register() at import time.
The application entry point calls SafetyRegistry.assert_all_registered()
after imports are complete, failing fast if any required module was
never imported.
"""
import logging
from typing import ClassVar

logger = logging.getLogger("safety.registry")


class SafetyRegistry:
    _registered: ClassVar[set[str]] = set()
    _required: ClassVar[set[str]] = set()

    @classmethod
    def register(cls, name: str) -> None:
        cls._registered.add(name)
        logger.info("SAFETY_MODULE_REGISTERED: %s", name)

    @classmethod
    def require(cls, *names: str) -> None:
        """Declare modules that must be registered before startup completes."""
        cls._required.update(names)

    @classmethod
    def assert_all_registered(cls) -> None:
        missing = cls._required - cls._registered
        if missing:
            raise RuntimeError(
                f"APPLICATION STARTUP ABORTED: the following required safety "
                f"modules were never imported and are not active in this "
                f"process: {sorted(missing)}. "
                f"Add the missing import to the application entry point."
            )
        logger.info(
            "SAFETY_REGISTRY_OK: all %d required module(s) registered: %s",
            len(cls._required),
            sorted(cls._required),
        )
```

**In each safety module (e.g., `compliance_gate.py`):**

```python
# compliance_gate.py — top of file, after imports
from safety.registration import SafetyRegistry
SafetyRegistry.register("compliance_gate")
```

**In the application entry point:**

```python
# main.py
from safety.registration import SafetyRegistry

# Declare which modules are required for this process.
SafetyRegistry.require("compliance_gate", "rate_limiter", "circuit_breaker")

# Import application modules — including all safety modules.
# If compliance_gate is not imported here, it will not register.
from pipeline import orchestrator          # must import compliance_gate internally
from safety import rate_limiter            # explicit import
from safety import circuit_breaker        # explicit import

# This call raises RuntimeError immediately if any required module
# was never imported, preventing the application from starting in
# an unsafe state.
SafetyRegistry.assert_all_registered()
```

---

## 5. Fix

### 5.1 Immediate Fix

Add the missing import and a direct call to the module's entry point at the appropriate location in the main application.

```python
# BEFORE — orchestrator.py (compliance gate missing)

class TransactionOrchestrator:
    def process_event(self, event: dict) -> dict:
        enriched = self._enrich(event)
        result = self._run_pipeline(enriched)
        self._persist(result)
        return result


# AFTER — orchestrator.py (compliance gate wired in)

from compliance_gate import ComplianceGate   # ← the missing line

class TransactionOrchestrator:
    def __init__(self):
        self._compliance_gate = ComplianceGate.from_config("config/compliance.yaml")

    def process_event(self, event: dict) -> dict:
        # Compliance evaluation happens before any downstream processing.
        compliance_result = self._compliance_gate.evaluate(event)
        if not compliance_result.passed:
            self._log_compliance_block(event, compliance_result)
            return {"status": "blocked", "rules_triggered": compliance_result.triggered_rules}

        enriched = self._enrich(event)
        result = self._run_pipeline(enriched)
        self._persist(result)
        return result
```

### 5.2 Robust Fix

For LangGraph pipelines, register the safety node explicitly in the graph definition and add a graph structure test that verifies the safety node is present.

```python
"""
pipeline/graph_definition.py

Defines the LangGraph computation graph for the transaction
compliance pipeline. The compliance gate is a required node;
its absence from the graph is caught at startup by the graph
structure assertion.
"""
from langgraph.graph import StateGraph, END
from compliance_gate import ComplianceGate
from pipeline.nodes import enrich_node, scoring_node, persist_node
from typing import TypedDict


class PipelineState(TypedDict):
    event: dict
    enriched: dict
    compliance_result: dict
    score: float
    persisted: bool


def compliance_node(state: PipelineState) -> PipelineState:
    gate = ComplianceGate.from_config("config/compliance.yaml")
    result = gate.evaluate(state["event"])
    return {**state, "compliance_result": result.to_dict()}


def route_after_compliance(state: PipelineState) -> str:
    if not state["compliance_result"].get("passed", False):
        return END
    return "enrich"


def build_graph() -> StateGraph:
    graph = StateGraph(PipelineState)
    graph.add_node("compliance_check", compliance_node)
    graph.add_node("enrich", enrich_node)
    graph.add_node("score", scoring_node)
    graph.add_node("persist", persist_node)

    graph.set_entry_point("compliance_check")
    graph.add_conditional_edges("compliance_check", route_after_compliance)
    graph.add_edge("enrich", "score")
    graph.add_edge("score", "persist")
    graph.add_edge("persist", END)
    return graph


def assert_graph_contains_safety_nodes(graph: StateGraph) -> None:
    """
    Structural assertion: raises AssertionError if any required
    safety node is missing from the graph. Called at startup.
    """
    required_nodes = {"compliance_check"}
    present_nodes = set(graph.nodes.keys())
    missing = required_nodes - present_nodes
    assert not missing, (
        f"Graph is missing required safety node(s): {missing}. "
        f"The application will not start in an unsafe state."
    )


# Entry point usage:
if __name__ == "__main__":
    g = build_graph()
    assert_graph_contains_safety_nodes(g)
    compiled = g.compile()
```

---

## 6. Architectural Prevention

**Principle: safety modules must be structurally impossible to bypass.**

1. **Co-locate import and contract.** Define a `REQUIRED_SAFETY_MODULES` list at the top of the application entry point. This list is the authoritative declaration of which safety modules must be active. CI enforces it; startup enforces it; code review enforces it. The list is never implicit.

2. **Use dependency injection, not optional discovery.** Safety modules should be required constructor parameters, not optional plugins. `TransactionOrchestrator(compliance_gate=ComplianceGate(...))` makes the dependency explicit and raises `TypeError` at instantiation if it is missing. A module that discovers safety plugins at runtime can silently skip them.

3. **Implement graph structure tests for agent frameworks.** For LangGraph, CrewAI, and AutoGen, write tests that assert the compiled graph or crew contains required nodes before any behavior is tested. A graph structure test is cheaper to write than an integration test and catches this pattern immediately.

4. **Require cross-team integration tests at the sprint boundary.** When a safety module is developed by one team and integrated by another, the definition of "done" for the integration task must include a passing integration test in the main application's test suite, not just the module's own test suite.

5. **Add import verification to the deployment checklist.** Before promoting to production, a deployment verification script should confirm that the safety module's entry point is reachable from the application's entry point. This is a one-time addition to the deployment pipeline and takes under an hour to implement.

6. **Use structured startup logs as a contract.** Every safety module should log its activation at startup with a canonical message (e.g., `COMPLIANCE_GATE_ACTIVE version=1.4.2 rules=32`). Production monitoring should alert if this log line is absent after a deployment. Absence of the startup log is a deployment anomaly.

```python
# Startup log contract pattern
# Each safety module logs its activation as a structured event.
# A Datadog / Splunk monitor checks for this event after each deployment.

import logging
import json

logger = logging.getLogger("safety.startup")


class ComplianceGate:
    def __init__(self, rules: list[dict], version: str):
        self._rules = rules
        self._version = version
        logger.info(
            json.dumps({
                "event": "COMPLIANCE_GATE_ACTIVE",
                "version": version,
                "rule_count": len(rules),
            })
        )

    @classmethod
    def from_config(cls, config_path: str) -> "ComplianceGate":
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        return cls(rules=cfg["rules"], version=cfg["version"])

    def evaluate(self, event: dict) -> "ComplianceResult":
        triggered = [r for r in self._rules if self._matches(event, r)]
        return ComplianceResult(passed=len(triggered) == 0, triggered_rules=triggered)

    def _matches(self, event: dict, rule: dict) -> bool:
        # Rule evaluation logic — implementation depends on rule schema.
        field = rule.get("field")
        operator = rule.get("operator")
        threshold = rule.get("threshold")
        value = event.get(field)
        if value is None or threshold is None:
            return False
        if operator == "gt":
            return float(value) > float(threshold)
        if operator == "eq":
            return str(value) == str(threshold)
        return False


class ComplianceResult:
    def __init__(self, passed: bool, triggered_rules: list[dict]):
        self.passed = passed
        self.triggered_rules = triggered_rules

    def to_dict(self) -> dict:
        return {"passed": self.passed, "triggered_rules": self.triggered_rules}
```

---

## 7. Anti-patterns to Avoid

**Anti-pattern 1: Treating unit test passage as integration proof.**
A module's unit tests verify the module in isolation. They prove that `ComplianceGate.evaluate()` returns correct results given inputs. They do not prove that `ComplianceGate.evaluate()` is ever called. These are orthogonal properties. The project definition of "done" must explicitly include integration evidence.

**Anti-pattern 2: Documenting integration without testing it.**
Architecture diagrams, ADRs, and sprint notes that say "the compliance gate is integrated" are not a substitute for a test that calls `orchestrator.process_event()` and asserts that `ComplianceGate.evaluate()` was invoked. Documentation can be wrong; a passing test is harder to lie about.

**Anti-pattern 3: Optional safety modules.**
Safety modules should not be optional. `if config.get("enable_compliance_gate", False): from compliance_gate import ComplianceGate` is a pattern that allows the safety module to be silently disabled by a missing or incorrect config value. Safety modules must be required; if they cannot run (misconfiguration, missing dependency), the application should refuse to start, not silently proceed without them.

**Anti-pattern 4: Safety modules as plugins discovered at runtime.**
Plugin discovery patterns (scanning a directory for `*.py` files, using `importlib.import_module` in a loop) are appropriate for optional extensions. They are inappropriate for safety modules because a missing file, a naming mismatch, or an import error silently removes the safety net. Safety modules must be explicitly imported.

**Anti-pattern 5: Separate test suites with no shared integration layer.**
When two teams each maintain their own test suite with no shared integration tests, this pattern is almost guaranteed to emerge eventually. The integration test suite — even a minimal one — must be owned jointly and must run in CI for both teams' changes.

**Anti-pattern 6: Measuring coverage by line coverage alone.**
A module that is never imported has 0% line coverage in the application's coverage report. However, if the module has its own tests, those tests execute its lines and the module may show 90%+ line coverage in the module-level report. Coverage aggregation must distinguish between coverage achieved by the module's own tests and coverage achieved by the application's integration tests.

---

## 8. Edge Cases and Variants

**Variant A: Module imported but entry point never called.**
The module is imported at the top of `orchestrator.py`, so the import coverage check passes. However, the import is a dead import: `ComplianceGate` is instantiated but `evaluate()` is never called in the processing path. Detection: trace-level integration tests that assert the entry point method was called with correct arguments (use `unittest.mock.patch` to spy on the method).

**Variant B: Module called during initialization but not in the hot path.**
`ComplianceGate.evaluate()` is called once during startup (e.g., as a health check or config validation), satisfying both the import check and a naive integration test. In the actual transaction processing path, the call was omitted. Detection: integration tests must call `process_event()` with representative inputs and assert compliance evaluation occurred.

**Variant C: Module active in development, removed during performance optimization.**
A developer profiling the hot path identifies `ComplianceGate.evaluate()` as a bottleneck and comments it out "temporarily." The comment is never restored. Detection: the CI import coverage gate catches this if the module is listed as required; startup registry assertions catch it at the next deployment.

**Variant D: Conditional import based on environment variable.**
`if os.getenv("ENABLE_COMPLIANCE") == "true": from compliance_gate import ComplianceGate`. The environment variable defaults to `false` in the production deployment manifest due to a copy-paste error from a staging config. The module is present but inactive in all environments except the developer's local machine. Detection: deployment verification scripts must assert the environment variable is set; the startup registry pattern catches it at runtime.

**Variant E: Module imported in a rarely executed code path.**
The import is present but gated: `if event["type"] == "high_risk": compliance_gate.evaluate(event)`. For the majority of event types, the gate is never called. This is a logical gap rather than an import gap. Detection: property-based tests that generate diverse event types and assert compliance evaluation coverage.

**Variant F: Rate limiter module present but applied to wrong layer.**
`rate_limiter.py` is imported and called — but only in the API handler layer. The internal agent-to-agent call path bypasses the API handler and calls the LLM directly. The rate limiter is active but does not protect the path where rate limits are actually exceeded. Detection: architectural diagram review cross-referenced with import analysis for each call path.

---

## 9. Audit Checklist

Use this checklist during code review of any pull request that introduces or modifies a safety module.

- [ ] **Import verified in non-test code**: `grep -r "import <module_name>" --include="*.py" . | grep -v test` returns at least one result.
- [ ] **Entry point called in application hot path**: the module's primary method (`.evaluate()`, `.check()`, `.call()`) appears in the application's main processing function, not only in tests or startup code.
- [ ] **CI import coverage gate is updated**: the module name is added to the `--required` list in the CI configuration.
- [ ] **Startup registry entry added**: the module calls `SafetyRegistry.register()` at import time.
- [ ] **`SafetyRegistry.require()` updated**: the application entry point declares the new module as required.
- [ ] **Startup log contract added**: the module logs a structured `MODULE_ACTIVE` event at instantiation time.
- [ ] **Graph structure test updated** (LangGraph/CrewAI): the test that asserts required nodes are present in the graph is updated to include this module's node name.
- [ ] **Integration test added**: at least one test in the main application's test suite invokes the full processing path and asserts the safety module's entry point was called.
- [ ] **Cross-team review**: if the module was written by a different team than the integrating application, a member of the integration team has reviewed and approved the wiring.
- [ ] **Deployment verification script updated**: the post-deployment check includes verification that the module's startup log line is present in the application logs.
- [ ] **Optional/conditional import reviewed**: if the import is conditional on an environment variable or config flag, the default value has been audited for each deployment environment.
- [ ] **Performance test confirms safety module is active under load**: a load test run confirms the module's metrics (calls counted, decisions logged) are non-zero after traffic is applied.

---

## 10. Further Reading

**Primary references:**

- Nygard, M. *Release It! Design and Deploy Production-Ready Software*, 2nd ed. (Pragmatic Programmers, 2018). Chapter 5: "Stability Patterns." The circuit breaker, bulkhead, and timeout patterns described there are all candidates for this anti-pattern if they are implemented but not imported.
- Fowler, M. "Integration Contract Test." martinfowler.com. The concept of a contract test — a test that verifies two modules interact correctly at their boundary — is the direct solution to the integration gap that enables this pattern.
- Google Engineering Practices documentation: "Code Review Best Practices" — the guidance on "completeness" in code review is directly relevant: reviewers must verify not just that new code is correct but that it is actually invoked.
- Python `importlib` documentation: Understanding how Python's import machinery works is essential for writing reliable import coverage tools.

**Framework-specific references:**

- LangGraph documentation: "Adding Nodes and Edges" — the graph compilation step is the correct place to assert node presence.
- CrewAI documentation: "Task and Agent Configuration" — a `Task` that references a non-existent agent silently completes without invoking the agent in some versions.
- LangChain documentation: "Callbacks" — `CallbackHandler` objects are silently ignored if not passed to the `callbacks=` parameter at invocation.

**Related patterns in this playbook:**

- Pattern #18 — Snapshot vs. Sustained Check: a monitoring module that is present and imported but misconfigured to read single-sample metrics.
- Pattern #19 — False Positive Crash Detection: a watchdog that is wired correctly but uses incorrect criteria, the mirror image of a safety module that is not wired at all.
- Pattern #16 — Missing Guard: a guard condition that is present in code but evaluates to a no-op due to a logic error — related in that protection appears present but is functionally absent.
