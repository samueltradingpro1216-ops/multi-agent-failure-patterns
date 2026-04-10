# Pattern #35 — Local Bypass of Write Function

**Category:** I/O & Persistence
**Severity:** High
**Tags:** `io`, `persistence`, `file-locking`, `audit-trail`, `bypass`, `desync`, `healthcare`

---

## 1. Observable Symptoms

**File content is correct but side effects are absent.** The data written to the file is valid and readable. However, the audit changelog contains no entry for the change, the file lock was never acquired, and downstream systems that rely on synchronization (replicas, caches, event buses) were never notified. The file looks correct to a human reading it directly; the system's higher-level invariants are silently violated.

**Replica divergence is discovered during failover.** The primary node's file and a replica's file contain different data. The divergence is not due to a transmission failure; it is because the bypass write never triggered the sync step that the official write function performs. The divergence is discovered only when the replica is promoted during a failover, at which point it serves stale or incorrect data.

**Audit log gaps cause compliance findings.** In regulated environments, every write to a sensitive file must produce an audit log entry. Bypass writes produce no such entry. During a compliance audit, the auditor observes that the file's modification timestamp has advanced but the changelog contains no corresponding entry. This is a compliance violation independent of whether the data written was correct.

**Concurrent write corruption.** Two processes both detect that a write is needed. Process A uses the official `write_config()` function, which acquires a file lock before writing. Process B uses the bypass `json.dump()`, which writes without checking the lock. The two writes interleave, producing a partially written file that is not valid JSON. The application reads the corrupted file on the next startup and fails.

**Silent operational drift in long-running systems.** Over weeks or months, the bypass is called in low-frequency operational paths (e.g., a maintenance script, a one-time migration). Each call produces a small divergence between the file and the system's authoritative state (changelog, replica, cache). The divergences accumulate. When the system is audited or a replica is consulted, the state is found to be inconsistent with expectations, and the source of each divergence is difficult to trace because no audit record exists.

---

## 2. Field Story

A healthcare records system stored clinical configuration — including dosage alert thresholds and formulary lists — in JSON files on the primary application server. The official write path was a function `write_config(key, value)` that:

1. Acquired a file lock using `fcntl.flock`.
2. Read the existing file.
3. Applied the update.
4. Wrote the updated file.
5. Released the lock.
6. Appended an entry to the audit changelog with the key, old value, new value, timestamp, and requesting user.
7. Triggered a sync to two replica servers.

A developer wrote a one-time migration script to bulk-update dosage thresholds for a new drug formulary. To avoid the perceived overhead of calling `write_config()` in a loop for 47 threshold values, the script used `json.dump()` directly:

```python
with open(CONFIG_PATH, "w") as f:
    json.dump(updated_config, f, indent=2)
```

The script ran successfully. All 47 threshold values were written correctly. However:

- No audit log entries were created for any of the 47 changes.
- The two replica servers were never notified. They continued serving the old thresholds for 72 hours until the next scheduled full sync.
- During those 72 hours, the primary server applied dosage alerts based on the new thresholds, while the two replicas applied alerts based on the old ones. Clinicians using different servers received different alert behavior for the same patient medication orders.

During a subsequent compliance audit, the auditor found 47 unexplained file modifications with no audit trail. This constituted a HIPAA audit control failure. The remediation required reconstructing the changelog from server logs and git history, a process that took three days of engineering time and produced a formal incident report filed with the compliance officer.

The root cause was not malice or negligence. The developer was unaware that `write_config()` did anything beyond writing the file, because the function's signature and the calling convention for the bypass were structurally identical — both produce a correctly formatted file.

---

## 3. Technical Root Cause

The root cause is that the official write function's side effects are invisible to callers who interact with the file directly. The function's value lies not in what it writes but in what it does before and after writing. When this value is undocumented or not enforced, callers bypass it without realizing they are forfeiting it.

```python
import fcntl
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)
CONFIG_PATH = Path("/etc/app/config.json")
CHANGELOG_PATH = Path("/var/log/app/config_changes.log")
REPLICA_HOSTS = ["replica1.internal", "replica2.internal"]


# The official write function — side effects are the point
def write_config(key: str, value: object, user: str) -> None:
    lock_fd = os.open(str(CONFIG_PATH) + ".lock", os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)           # 1. acquire lock
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        old_value = config.get(key)
        config[key] = value
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)             # 2. write
        _append_changelog(key, old_value, value, user) # 3. audit
        _sync_to_replicas(config)                      # 4. sync
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)            # 5. release lock
        os.close(lock_fd)


# The bypass — a "simpler" way that omits steps 1, 3, 4, 5
def _bad_bulk_update(updated_config: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(updated_config, f, indent=2)  # BUG: no lock, no audit, no sync
```

The secondary causes:

1. **No enforcement boundary.** The file path `CONFIG_PATH` is a module-level constant accessible to any code that imports the module. There is no mechanism that prevents direct file access.

2. **Convenience creates temptation.** The bypass is simpler to write and avoids the overhead of 47 individual `write_config()` calls. Developers under time pressure choose the simpler path.

3. **Documentation does not convey the contract.** The docstring of `write_config()` describes what the function does; it does not state that direct file writes are prohibited or explain the consequences of bypassing the function.

4. **No test covers the side effects.** The test suite verifies that `write_config()` updates the file correctly. It does not verify that the audit log is updated or that the sync is triggered — because these are treated as implementation details rather than behavioral contracts.

---

## 4. Detection

### 4.1 Manual Code Audit

Search for direct file writes to paths that are managed by an official write function. The indicator is `open(path, "w")` or `json.dump(..., f)` where `path` is a config, state, or record file that has a corresponding `write_*()` function elsewhere in the codebase.

Search patterns:

```
open(CONFIG_PATH, "w")
open(RECORDS_PATH, "w")
json.dump(
yaml.dump(
pickle.dump(
```

For each match, determine whether a named write function exists for that path. If yes, verify that the write function is the only caller of `open(..., "w")` for that path. Any other caller is a bypass.

Also search for scripts in `scripts/`, `migrations/`, `tools/`, or `bin/` that import the config path constant and write to it directly. Migration scripts are a common bypass location because they are written under time pressure and are not subject to the same review standards as application code.

### 4.2 Automated CI/CD

Use a `semgrep` rule to detect direct writes to paths that are known to be managed by official write functions. Maintain a list of protected paths in the rule's metadata:

```yaml
rules:
  - id: bypass-write-config
    patterns:
      - pattern: |
          open($PATH, "w")
      - pattern-not-inside: |
          def write_config(...):
              ...
      - pattern-not-inside: |
          def write_records(...):
              ...
    message: >
      Direct file write detected outside of official write functions.
      Use write_config() or write_records() to ensure locking, audit
      logging, and replica synchronization.
    languages: [python]
    severity: ERROR
    paths:
      include:
        - "*.py"
      exclude:
        - "tests/**"
```

Treat this rule as a blocking CI gate, not a warning. Any direct write to a protected path that does not originate from the official write function fails the build.

### 4.3 Runtime Production

Instrument file writes using a filesystem-level monitor. On Linux, use `inotify` to watch for `IN_CLOSE_WRITE` events on protected files. Compare the calling process and stack trace against the expected write path. Any write that does not originate from `write_config()` emits an alert:

```python
import inotify.adapters
import threading
import logging

logger = logging.getLogger(__name__)


def watch_config_file(config_path: str) -> None:
    """
    Background thread that monitors config_path for unexpected writes.
    Emits an ERROR log if a write is detected outside of the expected
    write window (i.e., not from write_config()).
    """
    notifier = inotify.adapters.Inotify()
    notifier.add_watch(config_path)
    for event in notifier.event_gen(yield_nones=False):
        (_, type_names, path, filename) = event
        if "IN_CLOSE_WRITE" in type_names:
            logger.error(
                "UNAUTHORIZED WRITE DETECTED: %s/%s was modified outside of write_config(). "
                "Audit log and replica sync may be missing.",
                path,
                filename,
            )


def start_file_watcher(config_path: str) -> threading.Thread:
    t = threading.Thread(target=watch_config_file, args=(config_path,), daemon=True)
    t.start()
    return t
```

This does not prevent the bypass but detects it within seconds of occurrence, enabling rapid remediation before replicas serve stale data for 72 hours.

---

## 5. Fix

### 5.1 Immediate

Replace all direct file writes with calls to the official write function. For bulk updates, extend the official function to accept a batch:

```python
def write_config_bulk(updates: dict[str, object], user: str) -> None:
    """
    Applies multiple config updates atomically under a single lock acquisition.
    Produces one audit log entry per key-value pair.
    Triggers a single replica sync after all updates are applied.
    """
    lock_fd = os.open(str(CONFIG_PATH) + ".lock", os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        old_values = {k: config.get(k) for k in updates}
        config.update(updates)
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        for key, value in updates.items():
            _append_changelog(key, old_values[key], value, user)
        _sync_to_replicas(config)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
```

The `write_config_bulk()` function eliminates the performance concern (single lock acquisition for 47 updates) while preserving all side effects.

### 5.2 Robust

Enforce the write boundary at the file system level using a `ProtectedFile` abstraction that intercepts all writes and routes them through the official function. Make direct file access structurally impossible for callers outside the module:

```python
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ChangeRecord:
    timestamp: float
    key: str
    old_value: Any
    new_value: Any
    user: str


class ProtectedConfigStore:
    """
    Manages read and write access to a JSON config file with mandatory
    locking, audit logging, and replica synchronization on every write.

    The file path is encapsulated: callers cannot access it directly.
    All writes MUST go through write() or write_bulk().
    There is no public attribute that exposes the underlying path.
    """

    def __init__(
        self,
        config_path: Path,
        changelog_path: Path,
        replica_sync_fn: Optional[callable] = None,
    ) -> None:
        self._config_path = config_path
        self._changelog_path = changelog_path
        self._lock_path = Path(str(config_path) + ".lock")
        self._replica_sync_fn = replica_sync_fn
        self._lock_fd: Optional[int] = None

    # --- public API ---

    def read(self, key: str, default: Any = None) -> Any:
        with open(self._config_path) as f:
            config = json.load(f)
        return config.get(key, default)

    def write(self, key: str, value: Any, user: str) -> None:
        self.write_bulk({key: value}, user)

    def write_bulk(self, updates: dict[str, Any], user: str) -> None:
        """
        Apply multiple updates atomically.
        Acquires lock -> reads -> updates -> writes -> audits -> syncs -> releases.
        Every step is mandatory. There is no code path that skips any step.
        """
        self._acquire_lock()
        try:
            config = self._read_raw()
            old_values = {k: config.get(k) for k in updates}
            config.update(updates)
            self._write_raw(config)
            for key, value in updates.items():
                self._audit(key, old_values[key], value, user)
            if self._replica_sync_fn is not None:
                self._replica_sync_fn(config)
        finally:
            self._release_lock()

    # --- private implementation ---

    def _acquire_lock(self) -> None:
        self._lock_fd = os.open(str(self._lock_path), os.O_CREAT | os.O_WRONLY)
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def _read_raw(self) -> dict:
        if not self._config_path.exists():
            return {}
        with open(self._config_path) as f:
            return json.load(f)

    def _write_raw(self, config: dict) -> None:
        tmp_path = self._config_path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp_path, self._config_path)  # atomic rename

    def _audit(self, key: str, old_value: Any, new_value: Any, user: str) -> None:
        record = ChangeRecord(
            timestamp=time.time(),
            key=key,
            old_value=old_value,
            new_value=new_value,
            user=user,
        )
        with open(self._changelog_path, "a") as f:
            f.write(json.dumps(record.__dict__) + "\n")
        logger.info(
            "config change: key=%s user=%s old=%r new=%r",
            key, user, old_value, new_value,
        )


def _sync_to_replicas(config: dict) -> None:
    """Placeholder for replica synchronization logic."""
    logger.info("syncing config to replicas: %d keys", len(config))


# --- demonstration ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.json"
        changelog_path = Path(tmpdir) / "changelog.log"
        config_path.write_text(json.dumps({"max_dose_mg": 500, "alert_threshold": 0.8}))

        store = ProtectedConfigStore(
            config_path=config_path,
            changelog_path=changelog_path,
            replica_sync_fn=_sync_to_replicas,
        )

        # Bulk update: 47 threshold values via a single lock acquisition
        formulary_updates = {f"threshold_{i}": float(i) * 0.1 for i in range(47)}
        store.write_bulk(formulary_updates, user="migration_script_v2")

        # Verify audit log was written
        changelog = changelog_path.read_text().strip().split("\n")
        assert len(changelog) == 47, f"Expected 47 audit entries, got {len(changelog)}"
        print(f"Audit log contains {len(changelog)} entries.")

        # Verify value is readable
        val = store.read("threshold_5")
        assert val == 0.5, f"Expected 0.5, got {val}"
        print("All assertions passed.")
```

---

## 6. Architectural Prevention

**Encapsulate the file path.** The single most effective prevention is to ensure that the path to a protected file is never a public constant. If `CONFIG_PATH` is importable, any code can open it directly. Make it a private attribute of the `ProtectedConfigStore` class. Callers interact only with the store's API; they have no path to bypass.

**Atomic writes using rename.** The `_write_raw` method above uses `os.replace(tmp_path, config_path)`, which is atomic on POSIX systems. This eliminates the partial-write corruption scenario that affects non-atomic direct writes. The official function must use this pattern; a bypass write typically uses `open(path, "w")` directly, which is not atomic.

**Audit log as a mandatory side effect, not optional instrumentation.** The audit log must not be implemented as an optional parameter or a flag that can be set to `False`. It must be an unconditional step in the write path. If the audit log write fails, the entire write operation must fail — the file must not be updated without an audit record in a regulated environment.

**Code review policy for protected paths.** Add a list of protected file paths to the repository's CODEOWNERS or CI policy. Any pull request that adds a direct `open(protected_path, "w")` outside the official write function must be approved by a security or compliance reviewer. Pair this with the semgrep CI rule for automated enforcement.

**Migration scripts must use the official write function.** Establish a policy that migration scripts are not exempt from the write path requirement. If the official function is too slow for bulk operations, extend it with a `write_bulk()` method (as shown above). Do not accept "this is a one-time script" as justification for bypassing safety mechanisms.

---

## 7. Anti-patterns to Avoid

**Do not export the config file path as a public module constant.** A public constant like `CONFIG_PATH = "/etc/app/config.json"` is an implicit invitation to open the file directly. Make it private or eliminate it from the public API entirely.

**Do not make the audit log step optional.** An `audit=True` parameter on the write function is a bypass waiting to happen. The first developer under time pressure will pass `audit=False` for "just this one maintenance task." Remove the parameter.

**Do not use `with open(path, "w") as f: json.dump(...)` in any code that manages shared state.** This pattern is appropriate for writing application output files. It is not appropriate for config files, record files, or any file that has locking, auditing, or sync requirements.

**Do not treat migration scripts as exempt from safety requirements.** Migrations modify production data. They are subject to the same correctness requirements as the application code they support.

**Do not rely on file permissions alone to enforce the write boundary.** File permissions (e.g., making the config file owner `app_user` and the migration script running as `root`) are coarse-grained and do not enforce the application-level invariants (locking, auditing, sync). Structural enforcement via the `ProtectedConfigStore` API is the correct mechanism.

**Do not skip the `os.replace()` (atomic rename) pattern.** Writing directly to the target path with `open(path, "w")` creates a window during which the file is partially written. Any reader that opens the file during this window will read corrupt data. The write-to-temp-then-rename pattern eliminates this window.

---

## 8. Edge Cases and Variants

**Bypass in a subprocess.** If a subprocess is spawned that writes directly to the config file (e.g., a shell command executed via `subprocess.run`), the Python-level `ProtectedConfigStore` cannot intercept it. The `inotify`-based runtime monitor (Section 4.3) is the appropriate detection mechanism for subprocess bypasses.

**Concurrent bypass and official write.** If the bypass and the official function run concurrently, the file lock acquired by the official function does not prevent the bypass write, because the bypass does not attempt to acquire the lock. The result is a corrupted file. The `inotify` monitor detects this after the fact; prevention requires encapsulating the path (Section 6).

**Replica sync failure during official write.** If the replica sync step fails (network error), the primary file has been updated and the audit log has been written, but the replicas are stale. The official write function must handle this by logging the sync failure, queuing a retry, and emitting a metric. The replicas must implement a reconciliation mechanism that detects divergence and requests a full sync.

**Write function itself contains a bypass.** If the `_write_raw` method inside `ProtectedConfigStore` calls `open(path, "w")` instead of using the atomic rename pattern, it is a bypass of its own atomic write guarantee. Review the implementation of the official write function for internal bypasses.

**The `json.dump` bypass in a `finally` block.** In error-handling code, developers sometimes write a "safe fallback" that dumps the config directly if the official write function raises. This fallback is a bypass. The correct behavior when the official write function fails is to raise the exception, not to silently write without side effects.

**Multiple official write functions.** If the codebase grows to have `write_config()`, `write_config_v2()`, and `write_config_legacy()`, each of which has slightly different side effects, callers will choose the one with the fewest side effects. Consolidate to a single official write function with a well-defined set of mandatory side effects.

---

## 9. Audit Checklist

- [ ] The config file path is not a public module constant; it is encapsulated inside a `ProtectedConfigStore` or equivalent class.
- [ ] A semgrep or AST-based CI rule blocks any direct `open(config_path, "w")` outside of the official write function, and the rule is a blocking gate (not a warning).
- [ ] The official write function uses `os.replace()` (atomic rename via temp file) for the actual file write step.
- [ ] The audit log step inside the official write function has no bypass path (no `audit=False` parameter, no conditional skip).
- [ ] If the audit log write fails, the entire write operation fails — the config file is not updated without an audit record.
- [ ] The replica sync step is mandatory and its failure is detected, logged, and queued for retry.
- [ ] All migration scripts and maintenance tools use the official write function or its `write_bulk()` variant; no direct file writes are present in `scripts/`, `migrations/`, or `tools/` directories.
- [ ] A `write_bulk()` method exists on the official write interface so that bulk operations have a compliant, performant path that does not incentivize bypass.
- [ ] An `inotify` or equivalent filesystem-level monitor alerts within 60 seconds of any unauthorized write to the protected file.
- [ ] The test suite for the official write function verifies all mandatory side effects: file is updated, audit log contains the correct entry, replica sync function was called. Mocking the sync function and asserting it was called is acceptable.

---

## 10. Further Reading

- Repository with annotated code examples for all patterns in this series: [github.com/samueltradingpro1216-ops/multi-agent-failure-patterns](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns)
- Python `fcntl` module documentation for file locking: [docs.python.org/3/library/fcntl.html](https://docs.python.org/3/library/fcntl.html)
- `os.replace()` for atomic file writes (POSIX rename semantics): [docs.python.org/3/library/os.html#os.replace](https://docs.python.org/3/library/os.html#os.replace)
- `inotify` Python bindings for filesystem event monitoring: [github.com/dsoprea/PyInotify](https://github.com/dsoprea/PyInotify)
- HIPAA Security Rule technical safeguard requirements for audit controls: [hhs.gov/hipaa/for-professionals/security/laws-regulations](https://www.hhs.gov/hipaa/for-professionals/security/laws-regulations/index.html)
- Semgrep documentation on blocking CI rules: [semgrep.dev/docs/semgrep-ci/running-semgrep-ci-with-semgrep-cloud-platform](https://semgrep.dev/docs/semgrep-ci/running-semgrep-ci-with-semgrep-cloud-platform/)
