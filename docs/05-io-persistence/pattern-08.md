# Pattern #08 — Data Pipeline Freeze

**Category:** I/O & Persistence
**Severity:** High
**Impacted frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 5 to 30 days (frozen data produces plausible but stale results — the problem is not investigated as long as results appear reasonable)

---

## 1. Observable Symptoms

A dashboard, report, or analytics module displays data that has not changed in days or weeks. Derived metrics (averages, trends, scores) are all **stable** — which appears positive but is in fact a sign that the underlying data is no longer being fed.

The deceptive symptom: the consumer does not crash. It reads a file or table that **exists** and contains valid data — it is simply old. Computations run normally, reports are generated on schedule, and metrics fall within normal ranges. It is the **date** of the data that is wrong, not the data itself.

The producer, on its side, also runs normally. It writes its data — but to a **different format or path** than what the consumer expects. Both components run in parallel, with no errors, without communicating with each other.

## 2. Field Story (anonymized)

A multi-agent monitoring system analyzed its agents' performance every hour. The analytics module read results from a JSONL file. After a refactor, the producer module switched to CSV format without updating the consumer.

The JSONL file still existed — it contained the data from before the refactor. The consumer read it normally, computed metrics, and produced reports. For 11 days, reports displayed metrics based on 11-day-old data. The team noticed nothing because the metrics were within normal ranges (they matched the reality of 11 days prior).

The bug was discovered when a team member noticed that the total number of results had not increased in 11 days. Upon investigation, the JSONL file had not been modified since the date of the refactor. The producer was writing to a CSV that nobody was reading.

## 3. Technical Root Cause

The bug occurs when the **implicit contract** between a producer and a consumer is broken without either party knowing:

```
BEFORE the refactor:
    Producer → data/results.jsonl → Consumer
    (contract: JSONL format, 1 line per result)

AFTER the refactor:
    Producer → data/results.csv    → (nobody reads this)
    Consumer → data/results.jsonl → (frozen file, last modified: 11 days ago)
```

The contract is "implicit" because it is not encoded anywhere. There is no versioned schema, no format validation, no integration test that validates the full pipeline. The producer assumes the consumer will read the CSV. The consumer assumes the JSONL will be kept up to date.

Common causes of contract breakage:
- File format change (JSONL → CSV, JSON → Parquet)
- File path or name change
- Schema change (new columns, renamed columns)
- Write frequency change (hourly → daily)
- Storage migration (local file → database → API)

## 4. Detection

### 4.1 Manual code audit

For each data pipeline, verify that producer and consumer point to the same source:

```bash
# List all file paths written
grep -rn "open.*'w'\|write_text\|to_csv\|to_json" --include="*.py" | grep -v test

# List all file paths read
grep -rn "open.*'r'\|read_text\|read_csv\|read_json\|load" --include="*.py" | grep -v test

# Cross-reference the two lists — paths written but never read are suspicious
# Paths read but never written are potential frozen pipelines
```

### 4.2 Automated CI/CD

Document pipelines and test the full flow on every deployment:

```python
# test_pipeline_contract.py
PIPELINES = [
    {
        "name": "agent_results",
        "producer_writes": "data/results.jsonl",
        "consumer_reads": "data/results.jsonl",
        "format": "jsonl",
    },
    {
        "name": "metrics_report",
        "producer_writes": "data/metrics.json",
        "consumer_reads": "data/metrics.json",
        "format": "json",
    },
]

def test_pipeline_contract_alignment():
    """Verify producer and consumer agree on file path and format."""
    for pipeline in PIPELINES:
        assert pipeline["producer_writes"] == pipeline["consumer_reads"], (
            f"Pipeline '{pipeline['name']}': producer writes to "
            f"'{pipeline['producer_writes']}' but consumer reads from "
            f"'{pipeline['consumer_reads']}'"
        )
```

### 4.3 Runtime production

Verify data freshness on every read:

```python
import os
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

class FreshnessChecker:
    """Refuses to read data older than max_age."""

    def __init__(self, max_age_hours: int = 24):
        self.max_age = timedelta(hours=max_age_hours)

    def check(self, filepath: str) -> dict:
        path = Path(filepath)

        if not path.exists():
            return {"fresh": False, "reason": "file does not exist"}

        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age = datetime.now(timezone.utc) - mtime

        if age > self.max_age:
            return {
                "fresh": False,
                "reason": f"file is {age.total_seconds()/3600:.1f}h old (max {self.max_age})",
                "last_modified": mtime.isoformat(),
            }

        return {"fresh": True, "age_hours": age.total_seconds() / 3600}

# Usage:
checker = FreshnessChecker(max_age_hours=2)
result = checker.check("data/results.jsonl")
if not result["fresh"]:
    logging.critical(f"PIPELINE FREEZE: {result['reason']}")
    send_alert(f"Data pipeline frozen: results.jsonl {result['reason']}")
```

## 5. Fix

### 5.1 Immediate fix

Realign producer and consumer on the same path and format:

```python
# Identify the producer's current format
# If producer writes CSV, update the consumer to read CSV:
import csv

def read_results(filepath: str) -> list[dict]:
    """Read results — supports both JSONL and CSV for migration."""
    if filepath.endswith(".csv"):
        with open(filepath) as f:
            return list(csv.DictReader(f))
    elif filepath.endswith(".jsonl"):
        import json
        with open(filepath) as f:
            return [json.loads(line) for line in f if line.strip()]
    else:
        raise ValueError(f"Unknown format: {filepath}")
```

### 5.2 Robust fix

Add an explicit contract between producer and consumer with versioning:

```python
"""pipeline_contract.py — Explicit contract between producer and consumer."""
import json
from pathlib import Path
from datetime import datetime, timezone

class PipelineContract:
    """Defines and validates the contract between a producer and a consumer."""

    def __init__(self, data_path: str, format: str, version: str):
        self.data_path = Path(data_path)
        self.meta_path = self.data_path.with_suffix(".meta.json")
        self.format = format
        self.version = version

    def write_meta(self, record_count: int):
        """Producer writes metadata after each data update."""
        meta = {
            "format": self.format,
            "version": self.version,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "record_count": record_count,
            "data_file": self.data_path.name,
        }
        with open(self.meta_path, "w") as f:
            json.dump(meta, f, indent=2)

    def validate(self, expected_version: str, max_age_hours: int = 24) -> dict:
        """Consumer validates the contract before reading."""
        if not self.meta_path.exists():
            return {"valid": False, "reason": "No metadata file — producer may have changed path"}

        with open(self.meta_path) as f:
            meta = json.load(f)

        if meta["version"] != expected_version:
            return {"valid": False, "reason": f"Version mismatch: {meta['version']} vs {expected_version}"}

        last_updated = datetime.fromisoformat(meta["last_updated"])
        age = datetime.now(timezone.utc) - last_updated
        if age.total_seconds() > max_age_hours * 3600:
            return {"valid": False, "reason": f"Data stale: {age.total_seconds()/3600:.1f}h old"}

        return {"valid": True, "records": meta["record_count"], "age_hours": age.total_seconds() / 3600}
```

## 6. Architectural Prevention

Prevention rests on three principles:

**1. Explicit contract.** Every pipeline has a metadata file (`.meta.json`) that defines the format, version, and last-updated timestamp. The consumer validates the contract before each read. If the format or version does not match, it refuses to read and raises an alert.

**2. End-to-end integration test.** On every deployment, a test writes data through the producer, reads it through the consumer, and verifies the result is correct. This test immediately catches any change to format, path, or schema.

**3. Freshness monitoring.** A periodic health check verifies that each data file has been updated within the last N hours. If a file is stale, this triggers an immediate alert — not a daily report.

## 7. Anti-patterns to Avoid

1. **Changing the producer's format without touching the consumer.** Any modification to the producer must include verification that the consumer remains compatible.

2. **No timestamp in data files.** Without a timestamp, it is impossible to distinguish "fresh data" from "11-day-old frozen data".

3. **Silently reading an empty file as "no data".** An empty file can mean "nothing happened" (normal) or "the producer is writing elsewhere" (bug). Distinguish the two with a metadata file.

4. **Implicit pipeline contract.** If the format is documented nowhere, any change is an invisible breaking change. Document explicitly: format, schema, frequency, path.

5. **Testing producer and consumer separately.** Unit tests for the producer verify that it writes correctly. Tests for the consumer verify that it reads correctly. Neither verifies that both are mutually compatible.

## 8. Edge Cases and Variants

**Variant 1: Schema drift.** The producer adds a column to the CSV. The consumer silently ignores unknown columns but crashes if an expected column is renamed or removed. The file is being updated (not frozen) but the data is incompatible.

**Variant 2: Partial pipeline freeze.** The producer writes to the correct file but has a bug that causes new rows to be identical to old ones (a bug in the data generator). The file is technically "up to date" (recently modified) but the data does not change.

**Variant 3: Database storage change.** The producer migrates from file to SQLite. The old file is no longer updated but the consumer keeps reading it. This is the same pattern as a format change, applied to a storage migration.

**Variant 4: Pipeline frozen by a lock.** The producer attempts to write but a lock file (left by a crashed process) prevents it. It fails silently and the file is never updated. The consumer reads stale data.

## 9. Audit Checklist

- [ ] Every producer → consumer pipeline is documented (path, format, frequency)
- [ ] Every data file has a metadata file with format, version, and timestamp
- [ ] The consumer checks freshness before every read
- [ ] An integration test validates the full producer → consumer flow
- [ ] Monitoring alerts if a file has not been updated for more than N hours

## 10. Further Reading

- Corresponding short pattern: [Pattern 08 — Data Pipeline Freeze](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-08)
- Related patterns: #04 (Multi-File State Desync — a frozen file is a form of desync), #01 (Timezone Mismatch — a misinterpreted timestamp can mask a pipeline freeze)
- Recommended reading:
  - "Designing Data-Intensive Applications" (Martin Kleppmann), chapter 10 on batch processing and schema contracts
  - Confluent Schema Registry — the concept of a versioned contract between producers and consumers applied at scale
