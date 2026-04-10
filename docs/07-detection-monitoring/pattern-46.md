# Pattern #46 — Adversarial Agent Too Conservative

**Category:** Detection & Monitoring
**Severity:** Medium
**Tags:** adversarial-agent, validation-agent, false-positive, throughput, llm-pipeline

---

## 1. Observable Symptoms

An adversarial agent that is too conservative produces a characteristic symptom profile that is distinct from a deadlock or a crash: throughput is reduced by a consistent, non-zero fraction, but the system continues to operate. The failure mode is economic, not operational.

**Symptom A — Unexpectedly low effective throughput.** The pipeline processes fewer items per unit time than capacity analysis predicts. Queues upstream of the validation stage accumulate. The agent stage reports high utilization but low pass-through rate. If the validator runs synchronously and blocks the pipeline, upstream latency rises. If it runs asynchronously, a growing discard pile accumulates items that were approved by the producer but blocked by the validator.

**Symptom B — High rejection rate with low downstream error rate.** The validator's reject counter climbs steadily, but downstream systems do not report a corresponding increase in errors from items that slipped through. This asymmetry is diagnostic: if the validator were blocking genuinely bad items, rejected items would correlate with downstream errors. If it is blocking good items, the downstream stays healthy but less productive.

**Symptom C — Inconsistent behavior on identical or similar inputs.** If the adversarial agent uses an LLM, stochastic output means that the same input may be approved on one call and rejected on another. Engineers attempting to reproduce a rejection fail intermittently, which delays root cause identification.

**Symptom D — Operator override rate rises.** Users or operators begin manually approving items that the validator rejected. If an override workflow exists, its usage rate is a direct measure of the false positive rate. An override rate above 5–10% is a strong signal of an over-conservative validator.

**Symptom E — Metrics look safe.** Error dashboards show no failures. The validator is "working correctly" by its own metrics. The only signal that something is wrong is a business-level metric: items processed per hour, revenue processed, tasks completed.

---

## 2. Field Story

A software company deployed an automated code review bot to act as an additional reviewer on all pull requests. The bot used an LLM to evaluate diffs and was configured to block merges if it detected any of a list of security, correctness, or style violations. The intent was to reduce the review burden on senior engineers for obvious violations while letting them focus on architectural concerns.

The bot was initially calibrated against a curated set of 200 historical PRs labeled by senior engineers as "acceptable" or "needs revision." On this calibration set, the bot's false positive rate (blocking acceptable PRs) was 8%, deemed acceptable for the pilot.

After deployment, three factors combined to increase the effective false positive rate to approximately 28%:

1. The calibration set was drawn from a period when the codebase had a relatively uniform style. After a major framework migration, legitimate PRs now contained patterns that superficially resembled the bot's "style violation" heuristics.
2. The LLM prompt had been written to err on the side of caution ("if in doubt, block"). Under distribution shift, "in doubt" applied to a much larger fraction of inputs.
3. The bot's rejection messages were not specific enough for engineers to understand why a PR was blocked, so engineers could not easily resolve false positives themselves and had to escalate to the bot's maintainer.

Within two weeks, engineering leads reported that PR cycle time had increased by 40%. Engineers began routing PRs through a manual approval bypass. The bot's maintainer, receiving 15–20 escalations per day, became a bottleneck. The bot was consuming more senior engineering time than it saved.

The fix involved recalibrating the prompt with domain-specific context, adding a confidence threshold below which the bot abstained rather than blocked, and surfacing the bot's reasoning in rejection messages so engineers could self-serve.

---

## 3. Technical Root Cause

The adversarial agent is too conservative when its decision boundary is set such that the false positive rate (legitimate actions blocked) significantly exceeds the acceptable operational cost of those false positives.

The root cause operates at two levels:

**Level 1 — Calibration mismatch.** The validator was calibrated on a distribution that no longer represents production inputs. Any classifier (rule-based or LLM-based) that is not periodically recalibrated against current inputs will drift. The calibration set acts as a fixed anchor; the production distribution moves.

**Level 2 — Asymmetric cost function.** The validator was designed assuming that the cost of a false positive (blocking a good action) is low. If that assumption was ever true, it often becomes false at scale. One false positive in 100 actions is a minor annoyance; 28 false positives in 100 is an operational crisis.

```python
# Illustrative model of adversarial agent decision boundary
from dataclasses import dataclass
from typing import Callable


@dataclass
class ValidationDecision:
    approved: bool
    confidence: float   # 0.0 = certain reject, 1.0 = certain approve
    reasoning: str


class AdversarialAgent:
    """
    Wraps an LLM-based or rule-based validator with configurable thresholds
    and abstention logic.
    """

    def __init__(
        self,
        llm_caller: Callable[[str], tuple[bool, float, str]],
        approve_threshold: float = 0.7,
        reject_threshold: float = 0.3,
    ):
        """
        approve_threshold: confidence above which the agent approves.
        reject_threshold: confidence below which the agent rejects.
        Between the two thresholds, the agent abstains (defers to human review).
        """
        if reject_threshold >= approve_threshold:
            raise ValueError(
                "reject_threshold must be strictly less than approve_threshold"
            )
        self._llm = llm_caller
        self._approve_threshold = approve_threshold
        self._reject_threshold = reject_threshold

    def evaluate(self, action_description: str) -> ValidationDecision:
        approved, confidence, reasoning = self._llm(action_description)

        if confidence >= self._approve_threshold:
            return ValidationDecision(approved=True, confidence=confidence, reasoning=reasoning)
        elif confidence <= self._reject_threshold:
            return ValidationDecision(approved=False, confidence=confidence, reasoning=reasoning)
        else:
            # Abstain — do not block, but flag for human review
            return ValidationDecision(approved=True, confidence=confidence,
                                      reasoning=f"[ABSTAIN — low confidence, flagged for review] {reasoning}")
```

The critical design decision is the abstention band. An agent that must return binary approve/reject will, when uncertain, fall back to its calibration bias. If the calibration was conservative, uncertainty resolves to rejection. Adding an abstention band (approve but flag) converts uncertain rejections into flagged-but-unblocked items, which humans can review without being on the critical path.

---

## 4. Detection

### 4.1 False Positive Rate Measurement

The false positive rate is not directly observable without ground truth. Approximate it using the override rate or a human-labeled sample.

```python
# false_positive_estimator.py — estimate FPR from override logs
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Iterator


@dataclass
class ValidationEvent:
    item_id: str
    agent_decision: str        # "APPROVED" or "REJECTED"
    human_override: bool       # True if a human subsequently approved a rejected item
    downstream_error: bool     # True if item caused an error downstream


def load_events(log_path: Path) -> Iterator[ValidationEvent]:
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        yield ValidationEvent(
            item_id=d["item_id"],
            agent_decision=d["agent_decision"],
            human_override=d.get("human_override", False),
            downstream_error=d.get("downstream_error", False),
        )


def compute_metrics(events: list[ValidationEvent]) -> dict:
    total = len(events)
    rejected = [e for e in events if e.agent_decision == "REJECTED"]
    false_positives = [e for e in rejected if e.human_override]
    true_positives  = [e for e in rejected if not e.human_override and e.downstream_error]

    rejection_rate   = len(rejected) / total if total else 0.0
    # FPR approximation: rejected items that were subsequently approved by humans
    fpr_approx = len(false_positives) / len(rejected) if rejected else 0.0
    # True detection rate (lower bound): rejected items that caused downstream errors
    tdr_lower  = len(true_positives) / len(rejected) if rejected else 0.0

    return {
        "total_items": total,
        "rejected_count": len(rejected),
        "rejection_rate": round(rejection_rate, 4),
        "false_positive_count_approx": len(false_positives),
        "false_positive_rate_approx": round(fpr_approx, 4),
        "true_detection_rate_lower_bound": round(tdr_lower, 4),
        "override_rate": round(len(false_positives) / total, 4) if total else 0.0,
    }


if __name__ == "__main__":
    import sys
    events = list(load_events(Path(sys.argv[1])))
    metrics = compute_metrics(events)
    for k, v in metrics.items():
        print(f"{k}: {v}")
```

### 4.2 Distribution Shift Detection

Detect when the production input distribution has drifted from the calibration distribution by comparing feature statistics.

```python
# distribution_shift_monitor.py — lightweight embedding-based drift detector
from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Sequence


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x ** 2 for x in a))
    norm_b = math.sqrt(sum(x ** 2 for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def centroid(embeddings: Sequence[list[float]]) -> list[float]:
    n = len(embeddings)
    if n == 0:
        return []
    dim = len(embeddings[0])
    return [sum(e[i] for e in embeddings) / n for i in range(dim)]


class DriftMonitor:
    """
    Compares the centroid of recent production embeddings against the
    calibration set centroid. Low cosine similarity indicates distribution shift.
    """

    def __init__(self, calibration_embeddings: list[list[float]], threshold: float = 0.85):
        self._calibration_centroid = centroid(calibration_embeddings)
        self._threshold = threshold

    def check_drift(self, recent_embeddings: list[list[float]]) -> dict:
        if not recent_embeddings:
            return {"drift_detected": False, "similarity": 1.0, "sample_size": 0}
        recent_centroid = centroid(recent_embeddings)
        similarity = cosine_similarity(self._calibration_centroid, recent_centroid)
        drift_detected = similarity < self._threshold
        return {
            "drift_detected": drift_detected,
            "similarity": round(similarity, 4),
            "sample_size": len(recent_embeddings),
            "threshold": self._threshold,
        }
```

### 4.3 A/B Shadow Mode

Run the validator in shadow mode (evaluate but do not block) and compare its decisions against actual downstream outcomes. This provides ground truth for calibration without operational risk.

```python
# shadow_validator.py — run adversarial agent in shadow mode for calibration
import logging
from typing import Any, Callable

log = logging.getLogger(__name__)


class ShadowValidator:
    """
    Wraps a validator. In shadow mode, evaluates but never blocks.
    Logs decisions for offline FPR analysis.
    """

    def __init__(
        self,
        validator: Callable[[Any], bool],
        shadow_mode: bool = True,
    ):
        self._validator = validator
        self._shadow_mode = shadow_mode

    def approve(self, item: Any, item_id: str) -> bool:
        decision = self._validator(item)
        if self._shadow_mode:
            log.info(
                "shadow_validator item_id=%s agent_decision=%s "
                "(not enforced — shadow mode)",
                item_id,
                "APPROVED" if decision else "REJECTED",
            )
            return True  # always allow through in shadow mode
        else:
            log.info(
                "shadow_validator item_id=%s agent_decision=%s (enforced)",
                item_id,
                "APPROVED" if decision else "REJECTED",
            )
            return decision
```

Shadow mode allows teams to measure the true false positive rate against actual outcomes before re-enabling enforcement after a recalibration.

---

## 5. Fix

### 5.1 Immediate Fix — Add Abstention Band and Lower Blocking Threshold

Convert the binary approve/reject validator to a three-state system: approve, flag-for-review, and reject. Only high-confidence rejections block. Low-confidence rejections become flags.

```python
# BEFORE — binary validator with no abstention
def validate_pr(diff: str, llm_evaluate: Callable) -> bool:
    result = llm_evaluate(diff)
    return result["approved"]  # True or False — no middle ground

# AFTER — three-state validator with configurable thresholds
from enum import Enum

class ValidationOutcome(Enum):
    APPROVED = "approved"
    FLAGGED  = "flagged"       # approved but routed to human review queue
    REJECTED = "rejected"      # blocked

def validate_pr(
    diff: str,
    llm_evaluate: Callable,
    approve_threshold: float = 0.80,
    reject_threshold: float = 0.35,
) -> tuple[ValidationOutcome, str]:
    """
    Returns (outcome, reasoning).
    Only rejects with confidence <= reject_threshold.
    Flags (does not block) with confidence between the two thresholds.
    """
    result = llm_evaluate(diff)
    confidence: float = result["confidence"]
    reasoning: str    = result["reasoning"]

    if result["approved"] and confidence >= approve_threshold:
        return ValidationOutcome.APPROVED, reasoning
    elif not result["approved"] and confidence <= reject_threshold:
        return ValidationOutcome.REJECTED, reasoning
    else:
        return ValidationOutcome.FLAGGED, reasoning
```

### 5.2 Recalibration Procedure

Recalibrate the validation agent against a current, representative sample using the following procedure.

```python
# recalibration.py — measure FPR on a labeled sample and adjust thresholds
from dataclasses import dataclass
from typing import Callable


@dataclass
class LabeledItem:
    item: object
    ground_truth_approved: bool   # human label: should this item be approved?


def measure_false_positive_rate(
    validator: Callable[[object], tuple[bool, float, str]],
    labeled_sample: list[LabeledItem],
    reject_threshold: float,
) -> dict:
    """
    Measures FPR at a given reject_threshold.
    FPR = (items incorrectly rejected) / (items that should be approved)
    """
    should_approve = [item for item in labeled_sample if item.ground_truth_approved]
    false_positives = 0

    for item in should_approve:
        approved, confidence, _ = validator(item.item)
        if not approved and confidence <= reject_threshold:
            false_positives += 1

    fpr = false_positives / len(should_approve) if should_approve else 0.0
    return {
        "reject_threshold": reject_threshold,
        "sample_size": len(labeled_sample),
        "eligible_approve_count": len(should_approve),
        "false_positive_count": false_positives,
        "false_positive_rate": round(fpr, 4),
    }


def find_threshold_for_target_fpr(
    validator: Callable[[object], tuple[bool, float, str]],
    labeled_sample: list[LabeledItem],
    target_fpr: float = 0.05,
    candidates: list[float] = None,
) -> float:
    """
    Binary search over reject_threshold candidates to find the highest threshold
    that keeps FPR below target_fpr.
    Returns the recommended reject_threshold.
    """
    if candidates is None:
        candidates = [round(0.05 * i, 2) for i in range(1, 10)]  # 0.05 to 0.45

    best_threshold = 0.05
    for threshold in candidates:
        result = measure_false_positive_rate(validator, labeled_sample, threshold)
        if result["false_positive_rate"] <= target_fpr:
            best_threshold = threshold
        else:
            break  # thresholds increase FPR as they rise; stop at first violation

    return best_threshold
```

---

## 6. Architectural Prevention

**1. Define acceptable FPR as a service-level objective.** The validator should have an explicit SLO: "False positive rate must remain below 5% over any rolling 7-day window." Measure it continuously. Alert when it is breached. This makes the problem detectable before it reaches 28%.

**2. Human-review queue as a pressure valve.** Any validation system that can block productive work needs a human-review queue. Items in the queue do not block the pipeline. Humans review them asynchronously. The queue depth is a leading indicator of over-conservatism.

**3. Separate detection from enforcement.** The validator detects potential problems. A separate enforcement policy decides whether to block based on the detection signal, its confidence, the item's context (e.g., author seniority, code path criticality), and the current queue depth. Decoupling detection from enforcement makes threshold adjustments deployable without changing the validator itself.

**4. Regular recalibration cadence.** Schedule recalibration on a fixed cadence (monthly or after any major input distribution change) using a labeled sample drawn from recent production data. Recalibration should be a routine operational task, not an incident response.

**5. Canary deployments for threshold changes.** When adjusting thresholds, deploy the new configuration to a subset of traffic first and measure the FPR change before full rollout.

---

## 7. Anti-patterns

**Anti-pattern A — "Err on the side of caution" without a cost model.** Prompt instructions that tell the LLM to block when uncertain are appropriate when the cost of a missed detection far exceeds the cost of a false positive. For many code review and content moderation use cases, the cost ratio inverts at scale. Make the cost model explicit.

**Anti-pattern B — Binary block/pass with no override mechanism.** Any automated blocking system without an override path creates an unconditional dependency on the validator's correctness. When the validator is wrong, the only remediation is to turn it off entirely.

**Anti-pattern C — Calibrating against historical data only once.** Initial calibration gives a false sense of permanent accuracy. Input distributions shift. Adversarial actors adapt. Calibrate continuously.

**Anti-pattern D — Using rejection rate as a proxy for value.** Teams sometimes argue that a high rejection rate proves the validator is catching many problems. This is only true if the false positive rate is low. A validator that rejects everything has a 100% rejection rate and zero value.

**Anti-pattern E — Treating LLM temperature as a tuneable safety knob.** Lowering temperature reduces stochasticity but does not address calibration bias. A biased validator at temperature 0.0 consistently rejects the same (wrong) items. Temperature is not a substitute for threshold calibration.

---

## 8. Edge Cases

**Adversarial gaming.** If actors know the validator's decision criteria, they can craft inputs that superficially satisfy the criteria while still being harmful. An over-conservative validator that is then relaxed because of its FPR may provide weaker coverage than intended. Maintain a held-out test set of adversarial examples and verify that FPR reduction does not degrade true positive rate.

**Compound validators.** Some pipelines chain multiple validators (rule-based pre-filter → LLM deep check). The false positive rates compound: if each stage has a 10% FPR, a two-stage pipeline rejects up to 19% of legitimate items. Audit compound pipelines for cumulative FPR, not just per-stage FPR.

**Latency interaction.** A validator that is slow (2–5 seconds per item) and has a 25% FPR imposes both a latency cost and a throughput cost. In latency-sensitive pipelines, the validator may time out and default to rejection, artificially inflating the FPR under load.

**Domain-specific base rates.** In domains where most items are legitimate (e.g., internal code review where only 1–2% of PRs contain genuine violations), even a 95% accurate classifier will have a false positive rate that exceeds the true positive rate. Apply Bayes' theorem to verify that the expected precision is acceptable given the domain's base rate before deploying.

---

## 9. Audit Checklist

- [ ] Define the acceptable false positive rate for the validator (e.g., < 5%).
- [ ] Instrument the validator to emit per-item decisions with confidence scores.
- [ ] Instrument the human override workflow and compute the override rate daily.
- [ ] Compute FPR over a rolling window; alert when it exceeds the SLO.
- [ ] Confirm that a human-review queue exists and does not block the main pipeline.
- [ ] Confirm that the validator has an abstention band (not binary block/pass).
- [ ] Run the shadow mode validator (Section 4.3) for 7 days after any recalibration before re-enabling enforcement.
- [ ] Review the calibration sample: is it drawn from data less than 3 months old? Does it reflect the current input distribution?
- [ ] Check for compounding FPR in multi-stage validation pipelines.
- [ ] Verify that rejection messages are specific enough for the submitter to self-resolve without escalation.
- [ ] Schedule the next recalibration date and assign ownership.

---

## 10. Further Reading

- "Responsible AI Practices: Fairness" — Google AI — `https://ai.google/responsibilities/responsible-ai-practices/`
- Sculley et al., "Hidden Technical Debt in Machine Learning Systems" — NeurIPS 2015.
- "Calibration of Modern Neural Networks" by Guo et al., ICML 2017.
- NIST AI Risk Management Framework — `https://airc.nist.gov/`
- "The Base Rate Fallacy" — Kahneman & Tversky; application to classifier precision at low prevalence.
- OpenAI cookbook: "How to work with large language models" — prompt calibration section — `https://github.com/openai/openai-cookbook`
- Karpathy, A., "Software 2.0" — conceptual framing of ML components as software with testable contracts.
