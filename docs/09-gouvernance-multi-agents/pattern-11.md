# Pattern #11 — Race Condition on Shared File

**Category:** Multi-Agent Governance
**Severity:** High
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 3 to 20 days (the bug is intermittent — it only occurs when two agents write "at the same moment", which can be rare)

---

## 1. Observable Symptoms

Data **disappears** from the config file or the state store. A counter that should read 10 reads 7. A JSON file contains corrupted or truncated data. One agent's logs show it wrote a value, but the file contains a different value — the one written by another agent a few milliseconds later.

The most confusing symptom: the bug is **intermittent**. It only occurs when two agents write simultaneously, which depends on the exact scheduling timing. The system can run for hours without issue, then lose 3 updates in 5 minutes. Local tests (single-threaded) never reproduce the bug. It only appears in production under real concurrency.

Another common symptom in multi-agent systems: **quota or budget counters are wrong**. Two agents consume LLM tokens in parallel, each increments the usage counter, but one of the increments is lost. The counter reads "850/1000 tokens used" when the reality is "920/1000". The budget overruns without any alert.

## 2. Field Story (anonymized)

A multi-agent system had a `usage.json` file that tracked LLM API usage. Each agent read the file, incremented its counter, and rewrote the file. Four agents were running in parallel.

The team noticed that the LLM invoice was consistently 20–30% higher than what the tracker displayed. Auditing the logs revealed the problem:

```
Agent A: reads usage.json → {"total": 500}
Agent B: reads usage.json → {"total": 500}    (same value, A has not written yet)
Agent A: writes usage.json → {"total": 510}  (500 + 10)
Agent B: writes usage.json → {"total": 505}  (500 + 5, overwrites A's 510)
→ 10 tokens from A are lost in the counter
```

Over a day with thousands of operations, the accumulated losses represented 30% of the total. The tracker read "7000 tokens used" while the actual invoice was 10,000.

## 3. Technical Root Cause

The bug is a **classic race condition** on a non-atomic read-modify-write. Two or more agents execute the following sequence in parallel, without any mutual exclusion mechanism:

```
1. Read:   data = json.load(open("state.json"))
2. Modify: data["count"] += 1
3. Write:  json.dump(data, open("state.json", "w"))
```

If two agents execute this sequence in parallel, agent B may read the value **before** agent A has written its modification. Agent B then computes its modification from the stale value and overwrites agent A's modification.

The problem is compounded in multi-agent systems by three factors:

**1. Agents in parallel.** Unlike a single-process web server, a multi-agent system often has N agents running in parallel (threads, processes, or independent cron jobs). Each can access the shared file at any time.

**2. Duration of the "modify" step.** In a multi-agent system, the "modify" step may include an LLM call (3–30 seconds). During that time, the file is "read but not yet rewritten" — a very wide vulnerability window compared to a simple `count += 1`.

**3. JSON files = no transactions.** Unlike a database, a JSON file does not support atomic writes or transactions. Writing a JSON file is not atomic: if the process crashes mid-write, the file can be corrupted (truncated, invalid JSON).

## 4. Detection

### 4.1 Manual code audit

Search for read-modify-write patterns on shared files:

```bash
# Search for JSON file reads
grep -rn "json\.load\|json\.loads.*read\|open.*json.*r" --include="*.py"

# Search for corresponding writes
grep -rn "json\.dump\|open.*json.*w" --include="*.py"

# Search for files accessed by multiple modules
for f in $(grep -roh "['\"].*\.json['\"]" --include="*.py" | sort -u); do
    count=$(grep -rl "$f" --include="*.py" | wc -l)
    if [ "$count" -gt 1 ]; then
        echo "SHARED: $f accessed by $count files"
    fi
done
```

### 4.2 Automated CI/CD

Concurrency test that verifies consistency after parallel writes:

```python
import threading
import json
import tempfile
import os

def test_concurrent_write_consistency():
    """Verify no updates are lost when multiple threads write to the same file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"count": 0}, f)
        filepath = f.name

    n_threads = 4
    increments_per_thread = 50
    expected_total = n_threads * increments_per_thread

    def increment():
        for _ in range(increments_per_thread):
            with open(filepath) as f:
                data = json.load(f)
            data["count"] += 1
            with open(filepath, "w") as f:
                json.dump(data, f)

    threads = [threading.Thread(target=increment) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(filepath) as f:
        actual = json.load(f)["count"]

    os.unlink(filepath)

    # This test will almost certainly FAIL — that's the point
    # It demonstrates the race condition exists
    if actual < expected_total:
        print(f"RACE CONDITION: expected {expected_total}, got {actual}, lost {expected_total - actual}")
    assert actual == expected_total, f"Lost {expected_total - actual} updates"
```

### 4.3 Runtime production

Verification counter that compares expected increments to actual increments:

```python
import threading

class WriteAuditor:
    """Tracks expected vs actual values to detect lost writes."""

    def __init__(self):
        self.expected_increments = 0
        self.lock = threading.Lock()

    def record_increment(self, amount: int = 1):
        """Call this BEFORE writing. Tracks how many increments should exist."""
        with self.lock:
            self.expected_increments += amount

    def audit(self, actual_total: int) -> dict:
        """Compare expected total with actual file value."""
        with self.lock:
            lost = self.expected_increments - actual_total
        if lost > 0:
            return {
                "race_condition": True,
                "expected": self.expected_increments,
                "actual": actual_total,
                "lost": lost,
                "loss_rate": lost / self.expected_increments if self.expected_increments > 0 else 0,
            }
        return {"race_condition": False}
```

## 5. Fix

### 5.1 Immediate fix

Add a lock around each read-modify-write:

```python
import threading
import json

_file_lock = threading.Lock()

def safe_increment(filepath: str, key: str, amount: int = 1):
    """Thread-safe increment on a JSON file."""
    with _file_lock:
        with open(filepath) as f:
            data = json.load(f)
        data[key] = data.get(key, 0) + amount
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
```

For multiple processes (not just threads), use a file lock:

```python
import fcntl  # Unix only; use msvcrt or portalocker on Windows

def safe_increment_multiprocess(filepath: str, key: str, amount: int = 1):
    """Process-safe increment using file locking."""
    with open(filepath, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # Exclusive lock
        try:
            data = json.load(f)
            data[key] = data.get(key, 0) + amount
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)  # Release lock
```

### 5.2 Robust fix

Migrate to SQLite with transactions (eliminates the entire class of file-based race conditions):

```python
import sqlite3
from contextlib import contextmanager

class AtomicStateStore:
    """SQLite-based state store with atomic read-modify-write."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent reads
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def increment(self, key: str, amount: int = 1) -> int:
        """Atomic increment. Returns new value."""
        from datetime import datetime, timezone
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            current = int(row[0]) if row else 0
            new_value = current + amount
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
                (key, str(new_value), now)
            )
            return new_value

    def get(self, key: str, default: int = 0) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row else default
```

## 6. Architectural Prevention

Prevention rests on a paradigm shift: **never use JSON files as a concurrent state store**.

**1. SQLite for local state.** SQLite supports transactions, locks, and concurrent writes (with WAL mode). It is a drop-in replacement for JSON files with consistency guarantees.

**2. A single writer per resource.** Instead of 4 agents writing to the same file, a single "writer agent" centralizes writes. Other agents send it messages ("increment by 5"). The writer serializes the writes.

**3. Atomic writes for files.** If a JSON file is required (e.g., for compatibility), use the "write-to-temp + rename" pattern:

```python
import tempfile, os, json

def atomic_write_json(filepath: str, data: dict):
    """Write JSON atomically: temp file + rename."""
    dir_name = os.path.dirname(filepath) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, filepath)  # Atomic on most OS
    except Exception:
        os.unlink(tmp_path)
        raise
```

## 7. Anti-patterns to Avoid

1. **json.load() + json.dump() without a lock.** This is the recipe for a race condition. Always wrap in a lock or use SQLite.

2. **JSON file as a concurrent counter.** A JSON file is not a database. It does not support atomic writes or transactions.

3. **In-memory lock for multiple processes.** `threading.Lock()` only protects threads within the same process. If agents are separate processes or cron jobs, a file lock or a database is required.

4. **No concurrency test.** If the code is only tested single-threaded, the race condition is invisible. Always test with N threads/processes in parallel.

5. **Ignoring truncated writes.** If the process crashes during a `json.dump()`, the file may be truncated (invalid JSON). The next `json.load()` will crash. Solution: atomic write via temp file + rename.

## 8. Edge Cases and Variants

**Variant 1: Race condition on SQLite.** Even SQLite has limits: in journal mode (not WAL), concurrent writes are serialized and can cause timeouts. Use `PRAGMA journal_mode=WAL` and a sufficient timeout.

**Variant 2: Race condition on an in-memory cache.** Two threads access the same Python dictionary without a lock. Python's GIL protects some atomic operations, but not `dict[key] = compute(dict[key])`, which is a read-modify-write.

**Variant 3: Stale lock file.** A process acquires a file lock, then crashes without releasing it. The lock persists indefinitely. Solution: lock with a TTL (if the lock is older than N minutes, consider it stale and delete it).

**Variant 4: ABA problem.** Agent A reads `count=5` and is preempted. Agent B increments to 6 then decrements back to 5. Agent A resumes, sees `count=5` (unchanged), and writes `count=6`. The result is accidentally correct but the logic is wrong — agent A never observed B's two operations.

## 9. Audit Checklist

- [ ] No JSON file is used as a concurrent state store without a lock
- [ ] Every read-modify-write is protected by a lock (threading.Lock or file lock)
- [ ] Writes to critical files use the atomic write pattern (temp + rename)
- [ ] A concurrency test (N threads, M increments) verifies consistency
- [ ] Stale lock files are detected and cleaned up automatically

## 10. Further Reading

- Corresponding short pattern: [Pattern 11 — Race Condition on Shared File](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-11)
- Related patterns: #04 (Multi-File State Desync — a race condition can create a desync between copies), #03 (Penalty Cascade — concurrent read-modify-writes on the same parameter)
- Recommended reading:
  - "Designing Data-Intensive Applications" (Martin Kleppmann), chapter 7 on transactions and concurrency
  - Python `threading` documentation — section on locks and race conditions
