# Pattern #47 — Threshold Mismatch Between Components

**Category:** Filtering & Decisions
**Severity:** Medium
**Tags:** threshold, pipeline, producer-consumer, configuration, silent-filter

---

## 1. Observable Symptoms

Threshold mismatch between pipeline components is a configuration divergence bug. Its defining characteristic is that both components operate without errors — no exceptions, no timeouts, no failed health checks — while the pipeline's effective throughput is significantly lower than expected.

**Symptom A — Low fill rate with no errors.** In a producer-consumer pipeline, the producer sends N items per unit time, but the consumer processes a fraction of N. The remainder are silently discarded at the consumer's filter. No error counter increments. The "lost" items leave no trace beyond the producer's sent counter and the consumer's processed counter diverging.

**Symptom B — Healthy-looking pipeline metrics.** Queue depth, consumer latency, and error rate all appear normal. The only anomalous metric is effective throughput or conversion rate at the downstream stage. Teams investigating infrastructure assume the producer is underperfoming; teams investigating the producer assume the consumer is underutilizing.

**Symptom C — Threshold values present in two separate configuration files (or code constants) with no cross-reference.** During a code review or configuration audit, the same logical parameter appears with different values in two places — with no comment, test, or contract enforcing that they must agree.

**Symptom D — Problem appears after a configuration change.** One component's threshold was updated (e.g., to improve precision on the producer side) without updating the consumer's threshold. The pipeline worked correctly before the change; the divergence is a regression introduced at a specific deployment.

**Symptom E — Discarded items are not logged.** The consumer's filter is implemented as a silent early return: `if spread >= self.max_spread: return`. No log line, no metric increment, no dead-letter queue. The items vanish without trace, making the filter invisible to operators who did not specifically instrument it.

---

## 2. Field Story

An advertising technology company operated a real-time bidding pipeline. The pipeline had two main components: a **bid selector** (producer) that evaluated ad opportunities and decided whether to submit a bid, and a **bid submitter** (consumer) that received selected bids and performed final validation before dispatching them to the exchange.

The bid selector filtered on bid spread: it submitted bids only when `spread < 250` basis points, where spread was the difference between the estimated clearing price and the bid floor. The bid submitter had its own spread check: it dispatched bids only when `spread < 60` basis points.

The two thresholds had been introduced at different times. The bid selector's threshold of 250 was set during initial development. The bid submitter's threshold of 60 was added eight months later, by a different team, as a risk control measure. The teams did not communicate the change. There was no shared configuration contract. Both thresholds were correct from the perspective of each component's individual design; they were simply incompatible.

The effective result was that the bid submitter discarded approximately 76% of the bids the selector sent it — bids with spread in [60, 250). The pipeline appeared healthy. The bid selector's "bids sent" counter looked normal. The bid submitter's "bids dispatched" counter was depressed, but this was attributed to "market conditions" (low inventory, high competition) by the business team for approximately six weeks.

The issue was identified when an engineer added a per-bid trace log to the bid submitter for an unrelated debugging task and noticed that the discard path was being hit on the majority of bids. A 20-minute investigation found the two threshold constants.

The financial impact was significant: the pipeline had been operating at approximately 24% of its intended capacity for six weeks.

---

## 3. Technical Root Cause

The root cause is the absence of a single source of truth for a shared threshold parameter, combined with the absence of a contract test that would detect divergence.

```python
# BUGGY — threshold defined independently in two components

# bid_selector.py
class BidSelector:
    MAX_SPREAD_BPS = 250   # basis points — set at initial development

    def select(self, opportunity: dict) -> dict | None:
        spread = opportunity["ask_price"] - opportunity["floor_price"]
        if spread >= self.MAX_SPREAD_BPS:
            return None   # filtered — no log, no metric
        return {"bid": opportunity["ask_price"] - 10, "spread": spread, **opportunity}


# bid_submitter.py
class BidSubmitter:
    MAX_SPREAD_BPS = 60    # basis points — added by risk team 8 months later

    def submit(self, bid: dict) -> bool:
        if bid["spread"] >= self.MAX_SPREAD_BPS:
            return False   # filtered — no log, no metric
        return self._dispatch_to_exchange(bid)

    def _dispatch_to_exchange(self, bid: dict) -> bool:
        # ... exchange API call ...
        return True
```

The two classes define `MAX_SPREAD_BPS` independently. There is no mechanism to detect that they have diverged. The filter at each stage is a silent return, so discarded items produce no observable signal. The pipeline's "error rate" is zero because no error has occurred — the components are each behaving exactly as configured.

The technical root cause has two components:

**1. Duplicated threshold definition.** A parameter that is semantically shared between components (the maximum acceptable spread for the pipeline) is represented as two independent constants. Changes to one do not propagate to the other.

**2. Silent discard.** The filter operation does not emit a metric, log line, or dead-letter entry when it discards an item. A silent discard is undetectable without explicit instrumentation of the discard path.

---

## 4. Detection

### 4.1 Static Analysis — Find Duplicated Threshold Constants

Search the codebase for numeric constants or configuration keys that appear in multiple locations and could represent the same semantic parameter.

```python
# find_duplicated_thresholds.py — identify same-named constants with different values
import ast
import sys
from pathlib import Path
from collections import defaultdict


def extract_class_constants(path: Path) -> list[tuple[str, str, str, int]]:
    """
    Returns list of (class_name, constant_name, value, lineno) for all
    class-level numeric constant assignments.
    """
    results = []
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return results

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if (
                isinstance(item, ast.Assign)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, (int, float))
            ):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        results.append((
                            node.name,
                            target.id,
                            str(item.value.value),
                            item.lineno,
                        ))
    return results


def main(root: str) -> None:
    # constant_name -> list of (class_name, value, file, lineno)
    by_name: dict[str, list[tuple[str, str, str, int]]] = defaultdict(list)

    for path in Path(root).rglob("*.py"):
        for class_name, const_name, value, lineno in extract_class_constants(path):
            by_name[const_name].append((class_name, value, str(path), lineno))

    mismatches_found = False
    for const_name, occurrences in by_name.items():
        if len(occurrences) < 2:
            continue
        values = {v for _, v, _, _ in occurrences}
        if len(values) > 1:
            mismatches_found = True
            print(f"\nMISMATCH: '{const_name}' has {len(values)} different values:")
            for class_name, value, filepath, lineno in occurrences:
                print(f"  {filepath}:{lineno} — {class_name}.{const_name} = {value}")

    if mismatches_found:
        sys.exit(1)
    else:
        print("No threshold mismatches found.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
```

### 4.2 Runtime Discard Rate Monitoring

Instrument every filter operation to emit a counter. Track the discard rate at each stage. An unexpectedly high discard rate at the consumer compared to the producer is diagnostic.

```python
# pipeline_metrics.py — instrumented bid selector and submitter with discard counters
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class FilterMetrics:
    stage: str
    total_received: int = 0
    total_passed: int = 0
    total_discarded: int = 0

    @property
    def discard_rate(self) -> float:
        return self.total_discarded / self.total_received if self.total_received > 0 else 0.0

    def record_pass(self) -> None:
        self.total_received += 1
        self.total_passed += 1

    def record_discard(self, reason: str, item_repr: str = "") -> None:
        self.total_received += 1
        self.total_discarded += 1
        log.debug(
            "filter_discard stage=%s reason=%s item=%.80s discard_rate=%.4f",
            self.stage, reason, item_repr, self.discard_rate
        )

    def log_summary(self) -> None:
        log.info(
            "filter_metrics stage=%s total=%d passed=%d discarded=%d discard_rate=%.4f",
            self.stage, self.total_received, self.total_passed,
            self.total_discarded, self.discard_rate
        )


# Shared configuration (the fix — single source of truth, shown in Section 5)
MAX_SPREAD_BPS = 60  # canonical value — used by both components after fix


class InstrumentedBidSelector:
    def __init__(self, max_spread_bps: int):
        self._max_spread = max_spread_bps
        self.metrics = FilterMetrics(stage="bid_selector")

    def select(self, opportunity: dict) -> dict | None:
        spread = opportunity["ask_price"] - opportunity["floor_price"]
        if spread >= self._max_spread:
            self.metrics.record_discard(
                reason=f"spread_too_high spread={spread} threshold={self._max_spread}",
                item_repr=repr(opportunity)
            )
            return None
        self.metrics.record_pass()
        return {"bid": opportunity["ask_price"] - 10, "spread": spread, **opportunity}


class InstrumentedBidSubmitter:
    def __init__(self, max_spread_bps: int):
        self._max_spread = max_spread_bps
        self.metrics = FilterMetrics(stage="bid_submitter")

    def submit(self, bid: dict) -> bool:
        if bid["spread"] >= self._max_spread:
            self.metrics.record_discard(
                reason=f"spread_too_high spread={bid['spread']} threshold={self._max_spread}",
                item_repr=repr(bid)
            )
            return False
        self.metrics.record_pass()
        return self._dispatch_to_exchange(bid)

    def _dispatch_to_exchange(self, bid: dict) -> bool:
        return True
```

### 4.3 Contract Test

A contract test asserts, at test time, that the two components use compatible thresholds. This test fails immediately if either component's threshold is changed without updating the other.

```python
# test_threshold_contract.py — ensure pipeline components agree on shared thresholds
import pytest
from bid_selector import BidSelector
from bid_submitter import BidSubmitter
import pipeline_config  # the shared config module (see Section 5.2)


class TestThresholdContract:
    """
    Contract tests for threshold compatibility between pipeline components.
    These tests fail if any component's threshold diverges from the shared config.
    """

    def test_bid_selector_uses_canonical_max_spread(self):
        selector = BidSelector(max_spread_bps=pipeline_config.MAX_SPREAD_BPS)
        assert selector._max_spread == pipeline_config.MAX_SPREAD_BPS, (
            f"BidSelector max_spread ({selector._max_spread}) does not match "
            f"canonical value ({pipeline_config.MAX_SPREAD_BPS})"
        )

    def test_bid_submitter_uses_canonical_max_spread(self):
        submitter = BidSubmitter(max_spread_bps=pipeline_config.MAX_SPREAD_BPS)
        assert submitter._max_spread == pipeline_config.MAX_SPREAD_BPS, (
            f"BidSubmitter max_spread ({submitter._max_spread}) does not match "
            f"canonical value ({pipeline_config.MAX_SPREAD_BPS})"
        )

    def test_selector_only_passes_bids_within_consumer_threshold(self):
        """
        Every bid that the selector passes must also pass the consumer's filter.
        If the selector's threshold is wider than the consumer's, this test fails.
        """
        selector  = BidSelector(max_spread_bps=pipeline_config.MAX_SPREAD_BPS)
        submitter = BidSubmitter(max_spread_bps=pipeline_config.MAX_SPREAD_BPS)

        test_spreads = [0, 10, 30, 59, 60, 61, 100, 249, 250, 300]
        for spread in test_spreads:
            opportunity = {"ask_price": 100 + spread, "floor_price": 100}
            selected = selector.select(opportunity)
            if selected is not None:
                # Any bid that passed the selector must also pass the submitter
                submitted = submitter.submit(selected)
                assert submitted, (
                    f"Selector passed bid with spread={spread} but submitter "
                    f"rejected it. Threshold mismatch detected."
                )
```

---

## 5. Fix

### 5.1 Immediate Fix — Align Thresholds

Identify which threshold is correct and update the other to match. This requires a business decision: the two thresholds represent different policies (maximum spread for bid selection vs. maximum spread for risk control). If they are the same policy, one must be the authoritative value. If they represent different policies, both should be documented as intentionally different, with explicit comments explaining the gap.

```python
# FIXED — both components use the same value, intentional difference is documented

# If the thresholds represent the SAME policy:
MAX_SPREAD_BPS = 60  # risk-control threshold; set by risk team; applies to full pipeline

class BidSelector:
    def __init__(self):
        self._max_spread = MAX_SPREAD_BPS  # shared constant — do not duplicate

class BidSubmitter:
    def __init__(self):
        self._max_spread = MAX_SPREAD_BPS  # shared constant — do not duplicate

# If the thresholds are intentionally different (pre-filter vs. hard limit):
PRE_FILTER_MAX_SPREAD_BPS = 250  # selector pre-filter: wide net, fast evaluation
HARD_LIMIT_MAX_SPREAD_BPS = 60   # submitter hard limit: risk control, slow path
# NOTE: items with spread in [60, 250) are selected but then rejected at submission.
# This is INTENTIONAL: the pre-filter is deliberately wider to allow the
# risk model to make the final decision. Any tightening of HARD_LIMIT must be
# accompanied by a corresponding tightening of PRE_FILTER.
```

### 5.2 Preferred Fix — Shared Configuration Module with Validation

Centralize all pipeline thresholds in a single configuration module. Add a startup validation that asserts threshold compatibility across components.

```python
# pipeline_config.py — single source of truth for all pipeline thresholds
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineThresholds:
    """
    Canonical threshold values for the bidding pipeline.
    All components must read thresholds from this class.
    Do not define threshold constants in component files.
    """
    selector_max_spread_bps: int = 60
    submitter_max_spread_bps: int = 60
    min_bid_price: float = 0.01
    max_bid_price: float = 50.0

    def validate(self) -> None:
        """
        Assert that thresholds are internally consistent.
        Call at application startup.
        Raises ValueError on incompatibility.
        """
        if self.selector_max_spread_bps > self.submitter_max_spread_bps:
            raise ValueError(
                f"Threshold mismatch: selector_max_spread_bps "
                f"({self.selector_max_spread_bps}) > "
                f"submitter_max_spread_bps ({self.submitter_max_spread_bps}). "
                f"The selector would pass bids that the submitter rejects. "
                f"Reduce selector_max_spread_bps or increase "
                f"submitter_max_spread_bps."
            )
        if not (0 < self.min_bid_price < self.max_bid_price):
            raise ValueError(
                f"Invalid bid price range: [{self.min_bid_price}, {self.max_bid_price}]"
            )


# Singleton — import this in all components
THRESHOLDS = PipelineThresholds()


# application startup
def initialize_pipeline() -> None:
    THRESHOLDS.validate()  # raises immediately if thresholds are incompatible
    # ... rest of startup ...
```

---

## 6. Architectural Prevention

**1. Single source of truth for all shared thresholds.** Any threshold that governs a decision that affects another component is shared, regardless of whether the components are in the same process. Shared thresholds must live in a shared configuration location. This is the canonical fix and the only durable prevention.

**2. Startup-time compatibility assertion.** The `validate()` pattern (Section 5.2) detects mismatch at startup, not at runtime. The application refuses to start with incompatible thresholds. This converts a silent runtime failure into a loud startup failure.

**3. Instrument every filter.** No filter operation should execute without emitting at least a counter. "Items discarded at stage X" should be a first-class operational metric alongside "items processed." Alert on discard rate exceeding a baseline.

**4. Pipeline health check that verifies end-to-end throughput ratio.** A health check that verifies that `consumer_processed / producer_sent > 0.90` (or an appropriate ratio for the domain) will detect mismatch-induced throughput degradation. Integrate this into the service's readiness probe or monitoring dashboard.

**5. Change management for threshold adjustments.** Thresholds that affect pipeline behavior should require a documented change review that includes: what other components use this threshold, what is the expected impact on pipeline throughput, and has the contract test been updated. A checklist in the PR template enforces this.

---

## 7. Anti-patterns

**Anti-pattern A — Hardcoding thresholds as class-level constants.** Class-level constants (`MAX_SPREAD_BPS = 60`) are invisible to configuration management systems. They cannot be changed without a code deployment. They cannot be audited across components without reading source code. Prefer configuration injection via constructor arguments or a shared config object.

**Anti-pattern B — Silent discard.** Any filter that returns early without logging is a silent failure waiting to happen. The development ergonomic cost of adding `log.debug(...)` or `metrics.increment(...)` to a discard path is trivial. The operational cost of missing it is not.

**Anti-pattern C — Threshold documentation only in comments.** A comment above a constant saying "must match BidSubmitter.MAX_SPREAD_BPS" will be read exactly once, on the day it is written. It will not be checked during code review. It will not prevent a future change that breaks the invariant. Tests enforce invariants; comments do not.

**Anti-pattern D — Testing components in isolation only.** Unit tests that test `BidSelector` and `BidSubmitter` independently will not detect the mismatch. Integration tests that run the full pipeline against representative data and assert a minimum pass-through rate are required.

**Anti-pattern E — Investigating infrastructure first.** Low throughput in a pipeline naturally suggests infrastructure causes (network saturation, database contention, CPU pressure). A rapid check for discard rates at each stage takes five minutes and should be the first diagnostic step, not the last.

---

## 8. Edge Cases

**One-sided threshold updates.** A threshold change that makes one component more permissive while the other remains strict increases the percentage of items silently discarded. A change that makes one component stricter while the other remains permissive is wasteful (the producer does work the consumer will reject) but not harmful in terms of data quality. Both directions of mismatch must be detected.

**Dynamic thresholds.** If thresholds are loaded from a configuration store (e.g., feature flags, database) and can change at runtime, both components must subscribe to updates. A rolling deployment where one pod has received a configuration update and another has not creates a transient threshold mismatch. Use atomic configuration reload or coordinate updates via a shared external store.

**Threshold negotiation via protocol.** In some architectures, the producer can query the consumer's current threshold before sending. This is robust but adds latency and coupling. It is appropriate for high-stakes pipelines; for high-throughput pipelines, shared configuration is preferred.

**Multiple consumers with different thresholds.** If the same producer feeds multiple consumers with intentionally different thresholds (e.g., a conservative risk-control path and a permissive experimental path), the architecture must explicitly represent this fan-out. A producer that sends to both must not assume a single canonical threshold.

**Threshold units.** Threshold mismatches sometimes involve unit differences rather than value differences: one component uses basis points, another uses percentage points. `spread < 250` basis points and `spread < 2.5` percent are equivalent, but they look like a mismatch in a code review. Document units in constant names and in the shared configuration schema.

---

## 9. Audit Checklist

- [ ] Run the static analysis scanner (Section 4.1) to find same-named constants with different values across components.
- [ ] Verify that all shared thresholds are defined in a single shared configuration module, not duplicated in component files.
- [ ] Confirm that the shared configuration module has a `validate()` method called at application startup.
- [ ] Confirm that every filter operation (early return based on threshold) emits a log line or metric counter.
- [ ] Add "items discarded at each pipeline stage" to the operational dashboard.
- [ ] Verify that the contract test (Section 4.3) exists and runs in CI.
- [ ] Compute `consumer_processed / producer_sent` ratio over the past 30 days; investigate any ratio below expected.
- [ ] Review the change history of all threshold constants for the past 6 months; identify any unilateral changes.
- [ ] Confirm that PR template includes a threshold change checklist.
- [ ] For dynamic thresholds (feature flags, DB config), verify that both producer and consumer subscribe to the same configuration key and update atomically.

---

## 10. Further Reading

- "Designing Data-Intensive Applications" by Martin Kleppmann, Chapter 11: Stream Processing — pipeline topology and consumer semantics.
- "Release It!" by Michael Nygard, Chapter 5: Stability Patterns — bulkhead and timeout configuration alignment.
- Google SRE Book, Chapter 6: "Monitoring Distributed Systems" — the importance of measuring what is not happening (silent discards).
- "The Twelve-Factor App" — Config factor: `https://12factor.net/config` — single source of truth for configuration.
- Fowler, M., "Microservices" — Consumer-Driven Contract Testing: `https://martinfowler.com/articles/consumerDrivenContracts.html`
- Pact documentation (consumer-driven contract testing): `https://docs.pact.io/`
- Prometheus documentation: Counters for monotonically increasing event counts — `https://prometheus.io/docs/concepts/metric_types/#counter`
