# Pattern #14 — Stale Lock File

**Category:** I/O & Persistence
**Severity:** High
**Affected frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debugging time if undetected:** 1 to 5 days (the system appears "hung" on a specific resource with no error message; the lock file is found only by inspecting the filesystem directly)

---

## 1. Observable Symptoms

A critical file that should be updated regularly has **stopped being updated**. The system appears to hang on a specific operation — config writes fail silently, state updates never complete, and logs show either timeouts or complete silence around the locked resource.

The most confusing symptom: there is no running process holding the lock. The lock was created by a process that **crashed** mid-operation. The lock file persists on disk, and every subsequent process that tries to acquire the lock waits forever (or times out and skips the operation).

Manual inspection of the filesystem reveals a `.lock` file or a `*.lck` file with a modification timestamp from hours or days ago. Deleting the lock file manually resolves the issue — until the next crash recreates it.

## 2. Field Story (anonymized)

An API gateway management system used a file lock to serialize writes to a shared routing configuration. The lock was acquired with `open("config.lock", "x")` (exclusive create) and released with `os.remove("config.lock")` after the write completed.

One night, the config writer process was killed by the OS OOM killer mid-write. The `config.lock` file remained on disk. From that point, every routing update attempt saw the lock file, assumed another process was writing, and waited. After a 30-second timeout, it silently skipped the update.

For 3 days, the routing configuration was frozen. New routes weren't added, stale routes weren't removed, and load balancing weights weren't updated. The team discovered the issue when a customer reported routing errors. The fix was `rm config.lock` — a 2-second operation that took 3 days to discover.

## 3. Technical Root Cause

The bug occurs when a lock mechanism doesn't handle the case where the lock holder **crashes without releasing the lock**:

```python
# Dangerous pattern: manual lock with no crash protection
def update_config(new_data: dict):
    # Acquire lock
    lock_path = "config.lock"
    while os.path.exists(lock_path):
        time.sleep(0.1)  # Wait for lock to be released
    open(lock_path, "w").close()  # Create lock file

    try:
        # Write config
        with open("config.json", "w") as f:
            json.dump(new_data, f)
    finally:
        os.remove(lock_path)  # Release lock

    # PROBLEM: if the process is killed between creating the lock
    # and the finally block, the lock file persists forever
```

The fundamental problem: **the lock lifecycle is tied to the process lifecycle, but the lock file persists beyond the process**. When the process dies, the lock should die with it — but file-based locks don't have this property.

Common crash scenarios that leave stale locks:
- OOM killer terminates the process
- `kill -9` (SIGKILL) bypasses Python's `finally` blocks and `atexit` handlers
- Power failure or system reboot
- Unhandled exception outside the `try/finally` block
- Deadlock in the write operation itself (process hangs, gets killed by a watchdog)

## 4. Detection

### 4.1 Manual code audit

Search for lock file patterns without TTL or staleness detection:

```bash
# Find lock file creation
grep -rn "\.lock\|\.lck\|fcntl\.flock\|portalocker\|lockfile" --include="*.py"

# Check if locks have timeout/TTL logic
grep -A10 "\.lock" --include="*.py" -rn | grep -i "timeout\|ttl\|stale\|age\|expire"
```

If locks are created but no staleness/timeout logic exists, stale locks are possible.

### 4.2 Automated CI/CD

Test that a simulated crash doesn't leave a permanent lock:

```python
import os, signal, multiprocessing, time

def test_lock_survives_crash():
    """Verify lock is cleaned up even if the holder crashes."""

    def crashable_writer(lock_path):
        open(lock_path, "w").close()  # Acquire lock
        os.kill(os.getpid(), signal.SIGTERM)  # Simulate crash

    lock_path = "/tmp/test.lock"
    if os.path.exists(lock_path):
        os.remove(lock_path)

    p = multiprocessing.Process(target=crashable_writer, args=(lock_path,))
    p.start()
    p.join(timeout=5)

    # Lock should not persist after crash
    time.sleep(1)
    stale = os.path.exists(lock_path)
    if stale:
        os.remove(lock_path)
    assert not stale, "STALE LOCK: lock file persists after process crash"
```

### 4.3 Runtime production

Periodic lock staleness checker:

```python
import os, time, logging
from pathlib import Path

class LockStalenessChecker:
    """Detects and optionally cleans stale lock files."""

    def __init__(self, max_age_seconds: int = 300):
        self.max_age = max_age_seconds

    def check(self, lock_path: str) -> dict:
        path = Path(lock_path)
        if not path.exists():
            return {"stale": False, "exists": False}

        age = time.time() - path.stat().st_mtime
        if age > self.max_age:
            return {
                "stale": True,
                "age_seconds": round(age),
                "message": f"Lock {lock_path} is {age:.0f}s old (max {self.max_age}s)",
            }
        return {"stale": False, "age_seconds": round(age)}

    def clean_if_stale(self, lock_path: str) -> bool:
        result = self.check(lock_path)
        if result.get("stale"):
            os.remove(lock_path)
            logging.warning(f"STALE LOCK REMOVED: {result['message']}")
            return True
        return False
```

## 5. Fix

### 5.1 Immediate fix

Add a TTL to the lock: if the lock file is older than N seconds, consider it stale and remove it:

```python
import os, time, json

LOCK_TTL_SECONDS = 120

def acquire_lock(lock_path: str, timeout: int = 30) -> bool:
    """Acquire a lock with automatic stale detection."""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        # Check for stale lock
        if os.path.exists(lock_path):
            age = time.time() - os.path.getmtime(lock_path)
            if age > LOCK_TTL_SECONDS:
                os.remove(lock_path)  # Stale — remove it
                continue

        # Try to create lock
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            time.sleep(0.1)

    return False  # Timeout

def release_lock(lock_path: str):
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass
```

### 5.2 Robust fix

Use a context manager that writes PID + timestamp and auto-cleans on stale detection:

```python
import os, time, json
from contextlib import contextmanager

@contextmanager
def file_lock(lock_path: str, ttl: int = 120, timeout: int = 30):
    """Context manager with PID tracking and stale detection."""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        # Check stale
        if os.path.exists(lock_path):
            try:
                with open(lock_path) as f:
                    info = json.load(f)
                pid = info.get("pid", -1)
                created = info.get("created", 0)

                # Check if holder is still alive
                if pid > 0:
                    try:
                        os.kill(pid, 0)  # Check if process exists
                    except OSError:
                        os.remove(lock_path)  # Process dead, remove stale lock
                        continue

                # Check TTL
                if time.time() - created > ttl:
                    os.remove(lock_path)
                    continue
            except (json.JSONDecodeError, OSError):
                os.remove(lock_path)
                continue

        # Acquire
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                json.dump({"pid": os.getpid(), "created": time.time()}, f)
            break
        except FileExistsError:
            time.sleep(0.1)
    else:
        raise TimeoutError(f"Could not acquire lock {lock_path} in {timeout}s")

    try:
        yield
    finally:
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass

# Usage:
with file_lock("config.lock"):
    update_config(new_data)
```

## 6. Architectural Prevention

The safest approach: **don't use file locks at all**. Use SQLite with WAL mode (built-in locking), or a database transaction. File locks are inherently fragile because the lock's lifecycle isn't tied to the process lifecycle.

If file locks are required (legacy compatibility), every lock must have:
1. **TTL**: maximum age before auto-removal (default: 2 minutes)
2. **PID tracking**: the holder's PID is written in the lock file, enabling stale detection via `os.kill(pid, 0)`
3. **Watchdog**: a separate process checks for stale locks every 60 seconds
4. **Startup cleanup**: on system boot, remove all lock files unconditionally (no process can be holding a lock if the system just started)

## 7. Anti-patterns to Avoid

1. **Lock without TTL.** A lock that can live forever is a system halt waiting to happen. Always set a maximum age.

2. **Lock without PID tracking.** Without the holder's PID, there's no way to distinguish "held by a live process" from "held by a dead process."

3. **Relying on `finally` for lock release.** `finally` doesn't execute on SIGKILL, OOM kill, or power failure. The lock must be self-healing via TTL.

4. **Blocking forever on lock acquisition.** `while os.path.exists(lock_path): sleep(0.1)` without timeout will hang the process indefinitely on a stale lock.

5. **Manual lock cleanup as a standard procedure.** If the ops team regularly runs `rm *.lock`, the system needs a better lock mechanism.

## 8. Edge Cases and Variants

**Variant 1: NFS lock files.** File locking on NFS is unreliable. `fcntl.flock` may not work across NFS mounts. Use a database or a coordination service (Consul, etcd) for distributed locking.

**Variant 2: Windows lock files.** On Windows, file locking semantics differ from Unix. `os.open(path, O_CREAT | O_EXCL)` works but `fcntl` doesn't exist. Use `msvcrt.locking` or `portalocker` for cross-platform compatibility.

**Variant 3: Lock directory instead of lock file.** `os.mkdir("config.lockdir")` is atomic on most filesystems. Cleaner than file-based locks but same stale problem applies.

**Variant 4: Double crash.** The lock holder crashes. A watchdog detects the stale lock and removes it. A new process acquires the lock. The new process also crashes. Now the lock is stale again, and the watchdog's interval determines how long the system is blocked.

## 9. Audit Checklist

- [ ] Every file lock has a TTL (max age before considered stale)
- [ ] Lock files contain the holder's PID for liveness checking
- [ ] A watchdog or startup script cleans stale locks automatically
- [ ] Lock acquisition has a timeout (never blocks forever)
- [ ] Tests simulate process crash and verify lock cleanup

## 10. Further Reading

- Related patterns: #11 (Race Condition on Shared File — locks are the fix for race conditions, stale locks are a bug in the fix), #04 (Multi-File State Desync — a stale lock can freeze a state file)
- Recommended reading:
  - Python `portalocker` library documentation — cross-platform file locking with timeout
  - "The Art of Multiprocessor Programming" (Herlihy & Shavit), chapter on lock-free algorithms — why file locks are a last resort
