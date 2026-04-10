# Pattern #49 — Cost Policy Violation

**Category:** Multi-Agent Governance
**Severity:** High
**Tags:** `cost-governance`, `model-selection`, `policy-enforcement`, `llm-routing`, `budget-overrun`

---

## 1. Observable Symptoms

These symptoms are characteristically delayed and often appear only at billing time, making the pattern one of the hardest to catch during normal operation:

- **Monthly invoice anomaly.** LLM API spend is 50x–200x higher than budgeted. The line item shows GPT-4 or Claude API calls at a volume that was planned for a free-tier or self-hosted model.
- **Correct functional output, wrong cost profile.** The affected agent produces high-quality results. No functional regression is observed. The violation is invisible to any test that evaluates output quality rather than resource consumption.
- **Missing cost attribution.** API usage dashboards aggregate spend by API key rather than by task type or agent identity. It is impossible to determine which agent or workflow triggered the expensive calls without additional instrumentation.
- **Configuration drift across environments.** The staging environment uses the correct free-tier model. Production was configured separately by a different team member who defaulted to the premium model. The discrepancy was never flagged because no comparison was made.
- **Retroactive discovery.** The violation is discovered weeks or months after it began. By the time it is found, the system has already accumulated significant overspend. Remediation requires both a code change and a financial escalation.

---

## 2. Field Story

A digital marketing agency built a content generation platform to produce first-draft copy for client campaigns: product descriptions, social media captions, email subject lines, and blog post outlines. The platform was composed of eight agents, each responsible for a specific content type.

The agency's cost policy was explicit: agents performing bulk generation tasks (high volume, low complexity, no client-facing real-time output) must use Llama 3 running on self-hosted infrastructure. Only agents performing client-review-facing tasks (low volume, high stakes, final polish) were authorised to use GPT-4.

During a sprint, a backend engineer configured the blog outline agent. The engineer was accustomed to using GPT-4 in prototyping and set `model="gpt-4"` in the agent's configuration file. The configuration file was not version-controlled in the same repository as the policy document. No automated check compared the model configuration to the policy. The agent passed all functional tests and was deployed.

Over the following six weeks, the blog outline agent ran 14,000 times, each call averaging 1,800 input tokens and 600 output tokens. At GPT-4 pricing, this produced an invoice line of approximately $2,800 for that agent alone — against a budgeted cost of $0 (self-hosted Llama). Total platform overspend for the period was $4,100.

The overspend was discovered during a quarterly finance review. The fix took 45 minutes. The financial impact was not recoverable.

---

## 3. Technical Root Cause

The root cause is the absence of a **centralised, enforced cost policy layer** between agent configuration and LLM API calls. Three structural gaps create the condition:

**3.1 Decentralised configuration.** Each agent owns its model configuration independently. There is no shared registry that maps task types to permitted models. An engineer configuring a new agent makes a local decision with no visibility into the cost policy.

**3.2 No enforcement point.** Even when a cost policy document exists, it is advisory. There is no runtime component that intercepts outgoing LLM API calls and validates the requested model against the policy. The agent calls the API directly; the API accepts the call; the cost is incurred.

**3.3 Delayed cost signal.** LLM API costs are reported on a monthly billing cycle. By the time the cost signal arrives, the violation has been running for weeks. In systems with high call volumes, even a few days of delay allows significant overspend to accumulate. There is no per-call or per-agent cost budget enforced at the time of the call.

The combination of these three gaps means that cost policy violations are both easy to introduce and slow to detect. The pattern is especially prevalent in organisations where the team that writes cost policy (finance, platform) is different from the team that configures agents (engineering).

---

## 4. Detection

### 4.1 Per-Call Model Policy Validator

A thin wrapper around the LLM client that checks the requested model against the policy before making the call. Raises an exception in strict mode; logs a warning in audit mode.

```python
from dataclasses import dataclass, field
from typing import Mapping, Set, Literal
import logging
import os

logger = logging.getLogger("cost_policy")


@dataclass
class CostPolicy:
    """Maps task categories to their permitted model sets."""
    permitted_models: Mapping[str, Set[str]] = field(default_factory=dict)
    enforcement_mode: Literal["strict", "audit", "off"] = "strict"

    def check(self, task_category: str, requested_model: str) -> dict:
        if self.enforcement_mode == "off":
            return {"status": "skipped", "reason": "enforcement_off"}
        permitted = self.permitted_models.get(task_category)
        if permitted is None:
            msg = f"Task category '{task_category}' is not registered in the cost policy."
            if self.enforcement_mode == "strict":
                raise ValueError(msg)
            logger.warning(msg)
            return {"status": "warning", "reason": "unregistered_category"}
        if requested_model not in permitted:
            msg = (
                f"Model '{requested_model}' is not permitted for task category "
                f"'{task_category}'. Permitted: {sorted(permitted)}."
            )
            if self.enforcement_mode == "strict":
                raise PermissionError(msg)
            logger.warning(msg)
            return {"status": "violation", "task_category": task_category,
                    "requested_model": requested_model, "permitted": sorted(permitted)}
        return {"status": "ok", "task_category": task_category, "model": requested_model}


# Policy definition — load from a central config in production
PLATFORM_POLICY = CostPolicy(
    permitted_models={
        "bulk_generation": {"llama3-8b", "mistral-7b"},
        "client_review_polish": {"gpt-4", "claude-3-5-sonnet"},
        "internal_summary": {"llama3-8b", "mistral-7b"},
        "real_time_chat": {"gpt-4", "claude-3-5-sonnet"},
    },
    enforcement_mode=os.getenv("COST_POLICY_MODE", "strict"),
)


def policy_checked_llm_call(task_category: str, model: str, prompt: str) -> str:
    """Wrapper that enforces cost policy before any LLM call."""
    PLATFORM_POLICY.check(task_category, model)
    # Actual LLM call goes here
    return f"[response from {model} for task {task_category}]"
```

### 4.2 Real-Time Per-Agent Cost Budget Enforcer

Track cumulative spend per agent per billing period and block calls once the budget is exhausted.

```python
import threading
import time
from dataclasses import dataclass, field
from typing import Dict


# Approximate cost per 1K tokens in USD — load from a pricing config in production
MODEL_COST_PER_1K_TOKENS: Dict[str, Dict[str, float]] = {
    "gpt-4":              {"input": 0.03,   "output": 0.06},
    "gpt-3.5-turbo":      {"input": 0.001,  "output": 0.002},
    "claude-3-5-sonnet":  {"input": 0.003,  "output": 0.015},
    "llama3-8b":          {"input": 0.0,    "output": 0.0},
    "mistral-7b":         {"input": 0.0,    "output": 0.0},
}


@dataclass
class AgentBudgetTracker:
    agent_id: str
    monthly_budget_usd: float
    billing_period_start: float = field(default_factory=time.time)
    _accumulated_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_call(self, model: str, input_tokens: int, output_tokens: int) -> dict:
        pricing = MODEL_COST_PER_1K_TOKENS.get(model)
        if pricing is None:
            raise ValueError(f"No pricing data for model '{model}'.")
        cost = (input_tokens / 1000.0) * pricing["input"] + \
               (output_tokens / 1000.0) * pricing["output"]
        with self._lock:
            if self._accumulated_usd + cost > self.monthly_budget_usd:
                raise PermissionError(
                    f"Agent '{self.agent_id}' would exceed monthly budget "
                    f"${self.monthly_budget_usd:.2f} (current: "
                    f"${self._accumulated_usd:.4f}, call cost: ${cost:.4f})."
                )
            self._accumulated_usd += cost
        return {
            "agent_id": self.agent_id,
            "call_cost_usd": round(cost, 6),
            "accumulated_usd": round(self._accumulated_usd, 6),
            "budget_remaining_usd": round(self.monthly_budget_usd - self._accumulated_usd, 6),
        }


# Usage
tracker = AgentBudgetTracker(agent_id="blog_outline_agent", monthly_budget_usd=5.00)
result = tracker.record_call(model="llama3-8b", input_tokens=1800, output_tokens=600)
print(result)
```

### 4.3 Configuration Audit Scanner

A CI/CD step that scans all agent configuration files and validates model assignments against the policy. Fails the build if any violation is found.

```python
import json
import pathlib
import sys
from typing import List


POLICY = {
    "bulk_generation":        {"llama3-8b", "mistral-7b"},
    "client_review_polish":   {"gpt-4", "claude-3-5-sonnet"},
    "internal_summary":       {"llama3-8b", "mistral-7b"},
    "real_time_chat":         {"gpt-4", "claude-3-5-sonnet"},
}


def scan_agent_configs(config_dir: str) -> List[dict]:
    violations = []
    for config_file in pathlib.Path(config_dir).rglob("agent_config.json"):
        with open(config_file) as f:
            config = json.load(f)
        agent_id = config.get("agent_id", str(config_file))
        task_category = config.get("task_category")
        model = config.get("model")
        if task_category is None or model is None:
            violations.append({
                "file": str(config_file),
                "agent_id": agent_id,
                "issue": "missing task_category or model field",
            })
            continue
        permitted = POLICY.get(task_category)
        if permitted is None:
            violations.append({
                "file": str(config_file),
                "agent_id": agent_id,
                "issue": f"unknown task_category '{task_category}'",
            })
        elif model not in permitted:
            violations.append({
                "file": str(config_file),
                "agent_id": agent_id,
                "issue": f"model '{model}' not permitted for '{task_category}'; "
                         f"permitted: {sorted(permitted)}",
            })
    return violations


if __name__ == "__main__":
    config_dir = sys.argv[1] if len(sys.argv) > 1 else "./agents"
    violations = scan_agent_configs(config_dir)
    if violations:
        for v in violations:
            print(f"VIOLATION [{v['agent_id']}] {v['issue']} ({v['file']})")
        sys.exit(1)
    print("All agent configurations comply with cost policy.")
    sys.exit(0)
```

---

## 5. Fix

### 5.1 Centralised Model Router

Replace direct model configuration in each agent with a call to a central router that resolves the correct model for the task category. Agents declare their task category; the router selects the model.

```python
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass
class ModelRouter:
    """
    Agents declare their task category. The router selects the permitted model.
    Agents never specify a model name directly.
    """
    routing_table: Mapping[str, str]  # task_category -> model_name
    fallback_model: Optional[str] = None

    def resolve(self, task_category: str) -> str:
        model = self.routing_table.get(task_category)
        if model is None:
            if self.fallback_model:
                return self.fallback_model
            raise KeyError(
                f"No model configured for task category '{task_category}'. "
                "Register it in the routing table or set a fallback_model."
            )
        return model


# Single source of truth — loaded from a versioned config file in production
ROUTER = ModelRouter(
    routing_table={
        "bulk_generation":      "llama3-8b",
        "client_review_polish": "gpt-4",
        "internal_summary":     "llama3-8b",
        "real_time_chat":       "claude-3-5-sonnet",
    }
)


class BlogOutlineAgent:
    TASK_CATEGORY = "bulk_generation"

    def __init__(self, router: ModelRouter = ROUTER):
        self.model = router.resolve(self.TASK_CATEGORY)

    def generate(self, topic: str) -> str:
        # self.model is always "llama3-8b" — the agent cannot override this
        return f"[outline generated by {self.model} for topic: {topic}]"
```

### 5.2 Policy-as-Code in CI/CD Pipeline

Enforce the policy at the point where it can prevent deployment, not after the cost has been incurred. The scanner from Section 4.3 runs as a required CI step. Any pull request that introduces a policy violation is blocked from merging.

```yaml
# .github/workflows/cost-policy.yml  (representative structure)
# name: Cost Policy Audit
# on: [pull_request]
# jobs:
#   audit:
#     runs-on: ubuntu-latest
#     steps:
#       - uses: actions/checkout@v4
#       - name: Run cost policy scanner
#         run: python scripts/cost_policy_scan.py ./agents
```

```python
# scripts/cost_policy_scan.py — full executable version (extend Section 4.3 scan)
import json
import pathlib
import sys

POLICY = {
    "bulk_generation":      {"llama3-8b", "mistral-7b"},
    "client_review_polish": {"gpt-4", "claude-3-5-sonnet"},
    "internal_summary":     {"llama3-8b", "mistral-7b"},
    "real_time_chat":       {"gpt-4", "claude-3-5-sonnet"},
}

PREMIUM_MODELS = {"gpt-4", "claude-3-5-sonnet", "gpt-4-turbo"}


def scan(config_dir: str) -> int:
    violation_count = 0
    for config_file in pathlib.Path(config_dir).rglob("agent_config.json"):
        try:
            config = json.loads(config_file.read_text())
        except json.JSONDecodeError as exc:
            print(f"ERROR: Cannot parse {config_file}: {exc}")
            violation_count += 1
            continue
        agent_id = config.get("agent_id", str(config_file))
        task_category = config.get("task_category", "")
        model = config.get("model", "")
        permitted = POLICY.get(task_category, set())
        if model not in permitted:
            severity = "HIGH" if model in PREMIUM_MODELS else "MEDIUM"
            print(
                f"[{severity}] Agent '{agent_id}': model '{model}' violates policy "
                f"for task '{task_category}'. Permitted: {sorted(permitted) or 'none (unknown category)'}. "
                f"File: {config_file}"
            )
            violation_count += 1
    return violation_count


if __name__ == "__main__":
    config_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    count = scan(config_dir)
    if count:
        print(f"\n{count} cost policy violation(s) found. Failing build.")
        sys.exit(1)
    print("Cost policy audit passed.")
    sys.exit(0)
```

---

## 6. Architectural Prevention

- **Task category as a first-class agent attribute.** Every agent must declare a `task_category` at initialisation. The category is not optional and cannot be changed at runtime. The model is resolved from the category; agents never hold a model name as configuration.
- **Immutable routing table per deployment.** The model routing table is versioned, reviewed, and deployed as a separate artifact. It is not embedded in agent configuration files. Any change to the routing table requires explicit review and approval.
- **Budget alerts at 25%, 50%, and 80% of monthly limit.** Configure alerts at sub-monthly intervals so that violations surface in days, not weeks. At 80%, freeze non-critical agent calls automatically.
- **API key scoping by model tier.** Use separate API keys for premium and free-tier model access. Premium-tier API keys are not included in agent deployment configurations; they are injected only into agents whose task categories permit premium models. An agent that should not use GPT-4 simply does not have the GPT-4 key available.

---

## 7. Anti-patterns

- **Trusting engineers to self-enforce the policy.** Cost policy documented in a wiki or README is not enforced policy. Engineers working under time pressure will use the model they are most familiar with. Enforcement must be automated.
- **Per-agent API keys with unlimited budgets.** Unlimited API keys make it impossible to detect per-agent overruns until billing consolidation. Always set per-key spending limits at the API provider level as a last-resort backstop.
- **Model names embedded in prompt templates.** Storing model names inside prompt template strings (e.g., `f"Use {model} to...`) creates invisible configuration that is not caught by configuration file scanners.
- **Relying on output quality as an indirect cost signal.** Premium models produce better output. If a free-tier model is substituted, output quality may degrade and trigger a reversion to the premium model. This cycle only ends when cost is a first-class, monitored metric alongside quality.

---

## 8. Edge Cases

- **Fallback on free-tier model failure.** If the permitted free-tier model is unavailable (self-hosted infrastructure outage), the agent may be designed to fall back to a premium model. This fallback must be a deliberate policy decision, time-limited, and logged as a cost exception — not a silent automatic escalation.
- **Model version drift.** The policy permits "gpt-3.5-turbo" but the API provider has updated the alias to point to a more expensive model version. Policy definitions must use explicit versioned model identifiers (e.g., `gpt-3.5-turbo-0125`) rather than aliases that the provider can reroute.
- **Batch vs. real-time pricing.** Some providers offer batch processing endpoints at 50% of the real-time price. A policy that permits a model name without specifying the endpoint type may allow agents to use the more expensive real-time endpoint when the batch endpoint would suffice.
- **Multi-tenant platforms.** If the platform serves multiple clients, cost policy may differ by client tier (enterprise clients may have allocated premium model budgets). The routing table must be parameterised by tenant context, not just task category.

---

## 9. Audit Checklist

```text
[ ] Every agent declares a task_category at initialisation.
[ ] No agent configuration file contains a model name directly; all model resolution goes through the central router.
[ ] The model routing table is version-controlled, reviewed, and deployed as a separate artifact.
[ ] The CI/CD pipeline runs the cost policy scanner on every pull request and blocks merge on violation.
[ ] Per-agent cumulative cost is tracked in real time and compared against a monthly budget.
[ ] Budget alerts fire at 25%, 50%, and 80% of the monthly limit per agent.
[ ] API keys for premium models are not present in the environment of agents assigned to free-tier task categories.
[ ] Model identifiers in the policy use explicit version strings, not provider aliases.
[ ] Fallback escalations to premium models are logged as cost exceptions and require a time limit.
[ ] Monthly cost-per-agent report is reviewed by both engineering and finance.
```

---

## 10. Further Reading

- OpenAI Usage Policies and Rate Limit Documentation. https://platform.openai.com/docs/guides/rate-limits — On API key scoping, spending limits, and usage tiers.
- Anthropic API Documentation: Model comparison and pricing. https://docs.anthropic.com/en/docs/about-claude/models — On model versioning and explicit identifier usage.
- FinOps Foundation. (2023). *FinOps Framework*. https://www.finops.org/framework/ — Governance model for cloud and API cost management; directly applicable to LLM spend governance.
- Kleppmann, M. (2017). *Designing Data-Intensive Applications*. O'Reilly. — Chapter 1 on reliability and maintainability; cost predictability as a reliability property.
- Kreuzberger, D., Kühl, N., & Hirschl, S. (2022). "MLOps: Overview, Definition, and Architecture." *arXiv:2205.02302*. — MLOps governance frameworks that include model selection policy as a controlled, auditable decision.
