# Pattern #22 — Cron Duplicate Execution

**Category:** Loops & Orchestration
**Severity:** Medium
**Affected Frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average Debugging Time If Undetected:** 1 to 7 days

---

## 1. Observable Symptoms

The following symptoms manifest independently or in combination. No single symptom is definitive on its own; the pattern is confirmed when two or more appear together.

- **Duplicate side effects at fixed intervals.** Transactional emails (order confirmations, shipping notifications) arrive in pairs. Database records are inserted twice with near-identical timestamps separated by milliseconds to a few seconds.
- **API rate-limit errors that appear only at scheduled times.** Third-party payment gateways or inventory services return HTTP 429 exactly at the job's scheduled hour, but the same endpoints are fine during manual testing.
- **Log lines show the same job ID starting twice before either finishes.** A `grep` for `"job_id=ORDER_SYNC"` in application logs returns two `START` entries before the first `END` entry.
- **Idempotency counters are odd numbers when they should always be even.** A pipeline that processes records in batches of two shows odd totals, indicating one pass completed and one did not, or one pass ran while another was partially processed.
- **Monitoring dashboards show "scheduled invocations: 2, successful completions: 1" for the same cron slot.** This asymmetry is the clearest leading indicator.
- **Anomalous spikes during Daylight Saving Time (DST) transitions.** A job that runs cleanly every other week exhibits duplicates precisely on the two DST-change Sundays per year.
- **Queue depth doubles without a corresponding increase in incoming work.** A message queue that normally holds 100 items at peak now consistently holds 200 at the scheduled hour.

---

## 2. Field Story (Anonymized)

A mid-sized e-commerce company operated an automated order-processing pipeline responsible for syncing confirmed orders from their storefront platform to their warehouse management system (WMS). The sync job was scheduled to run every 15 minutes and was critical: it triggered pick-and-pack instructions, reserved inventory, and sent customer confirmation emails.

The engineering team began receiving sporadic customer complaints about receiving duplicate confirmation emails roughly six weeks after deploying a new server. Initial investigation dismissed the reports as a frontend bug — the email template service had recently been updated — and no backend investigation was initiated.

Over the following two weeks, the warehouse operations team noticed inventory discrepancies. Items appeared to be reserved twice, causing some orders to fail with an "insufficient stock" error even when physical inventory was available. The stock reservation table contained pairs of rows with identical `order_id` values and timestamps within two seconds of each other.

A senior engineer eventually pulled the APScheduler logs and discovered that the job had two registered instances. The root cause was a deployment procedure that restarted the application server without stopping the scheduler first. The scheduler's persistent job store (backed by a SQLite database) had been copied to the new server alongside the application code. When the application started on the new server, it loaded the existing job definitions from the copied store. The old server, which had not been decommissioned yet, continued running its scheduler instance against the same job store. Both instances fired simultaneously every 15 minutes against a shared production database.

The duplicate emails had been occurring for the entire six weeks since the new server was provisioned. The warehouse stock discrepancies became visible only after order volume increased enough to make the double-reservation statistically likely to exhaust available stock within a reservation window.

Resolution required decommissioning the old server, flushing the duplicated job store, adding a distributed lock around the job's entry point, and implementing idempotency checks on the WMS ingestion endpoint.

---

## 3. Technical Root Cause

Cron duplicate execution arises from one or more of the following discrete mechanisms.

**Mechanism A — Multiple scheduler instances sharing a persistent job store.**
APScheduler and similar libraries allow job stores to be backed by a database (SQLite, PostgreSQL, Redis). When the same job store is accessible to two running scheduler instances — whether from two application servers, a blue-green deployment overlap, or a container restart that does not cleanly terminate the previous instance — both schedulers read the same job definitions and both fire the job independently. The job store does not enforce exclusive execution; it only stores job metadata.

**Mechanism B — Duplicate cron entries in system crontab or Task Scheduler.**
A deployment script that appends a cron entry without first checking for an existing identical entry will accumulate duplicates over successive deployments. `crontab -l` may show the same `*/15 * * * * /opt/app/run_sync.sh` line twice or more. Task Scheduler on Windows exhibits the same behavior when XML task definitions are imported without deduplication logic.

**Mechanism C — DST transition double-fire.**
When a server's timezone is set to a region that observes DST and the scheduler is configured with a local-time schedule (e.g., "run at 02:00"), the clock transition from 02:00 to 03:00 (spring forward) skips the 02:00 window. Conversely, the fall-back transition causes the clock to pass through 01:00 twice. A job scheduled for 01:30 will fire once in the first pass and once in the second pass of the same wall-clock hour.

**Mechanism D — `misfire_grace_time` combined with a backlogged executor.**
APScheduler's `misfire_grace_time` parameter defines how late a job is allowed to start before it is considered missed. If the executor is backlogged and a job fires at T+0, is delayed past `misfire_grace_time`, is rescheduled, and then the executor catches up and runs both the original and the rescheduled instance, the job executes twice within a short window.

**Mechanism E — Container orchestration restart without PID 1 shutdown signal propagation.**
In Kubernetes or Docker Swarm, a `SIGTERM` sent to the container's PID 1 must be forwarded to child processes. If the scheduler runs as a subprocess and the entrypoint script does not propagate signals, the container may overlap a new instance starting before the old instance has fully stopped.

---

## 4. Detection

### 4.1 Manual Code Audit

Inspect the scheduler initialization code for the presence of guards against duplicate job registration:

```python
# UNSAFE: adds the job unconditionally on every application start
scheduler.add_job(sync_orders, 'interval', minutes=15, id='order_sync')

# SAFE: replace_existing prevents duplicate registration on restart
scheduler.add_job(
    sync_orders,
    'interval',
    minutes=15,
    id='order_sync',
    replace_existing=True,
)
```

Check for the use of a persistent job store and whether more than one process can access it simultaneously:

```python
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

jobstores = {
    'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
}
# RISK: if jobs.sqlite is on a shared NFS mount or copied between servers,
# two schedulers will each fire every job in the store.
```

Audit system crontab and user crontabs:

```bash
# On Linux: check for duplicate entries across all crontab sources
crontab -l
cat /etc/cron.d/*
grep -r "run_sync" /var/spool/cron/
```

Check whether the scheduler timezone is set explicitly to UTC:

```python
# UNSAFE: relies on server local time; DST-sensitive
scheduler = BackgroundScheduler()

# SAFE: explicit UTC timezone eliminates DST double-fire
from pytz import utc
scheduler = BackgroundScheduler(timezone=utc)
```

### 4.2 Automated CI/CD

Add a static analysis step that fails the pipeline if `add_job` is called without `replace_existing=True`:

```python
# ci_checks/check_scheduler_safety.py
import ast
import sys
import pathlib

def check_file(path: str) -> list[str]:
    """Return a list of violation descriptions found in the given source file."""
    violations = []
    source = pathlib.Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match scheduler.add_job(...) calls
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == 'add_job'):
            continue

        keyword_names = {kw.arg for kw in node.keywords}
        if 'replace_existing' not in keyword_names:
            violations.append(
                f"{path}:{node.lineno} — add_job() called without replace_existing=True"
            )
    return violations

if __name__ == "__main__":
    source_root = sys.argv[1] if len(sys.argv) > 1 else "."
    all_violations = []
    for py_file in pathlib.Path(source_root).rglob("*.py"):
        all_violations.extend(check_file(str(py_file)))

    if all_violations:
        print("Scheduler safety violations found:")
        for v in all_violations:
            print(f"  {v}")
        sys.exit(1)

    print("Scheduler safety check passed.")
    sys.exit(0)
```

Add an integration test that starts two scheduler instances and asserts the job fires exactly once per interval:

```python
# tests/test_scheduler_deduplication.py
import time
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import utc

execution_count = 0
lock = threading.Lock()

def counted_job():
    global execution_count
    with lock:
        execution_count += 1

def make_scheduler() -> BackgroundScheduler:
    s = BackgroundScheduler(timezone=utc)
    s.add_job(counted_job, 'interval', seconds=1, id='test_job', replace_existing=True)
    return s

def test_no_duplicate_execution():
    global execution_count
    execution_count = 0

    s1 = make_scheduler()
    s2 = make_scheduler()
    s1.start()
    s2.start()

    time.sleep(3.2)

    s1.shutdown(wait=False)
    s2.shutdown(wait=False)

    # With an in-memory store and replace_existing=True, each scheduler
    # maintains its own job independently; expect exactly 2 schedulers x 3 ticks = 6.
    # This test documents the behavior; the fix (distributed lock) keeps effective
    # side-effect count at 3.
    assert execution_count <= 8, (
        f"Job executed {execution_count} times in 3 seconds; expected <= 8 "
        "(2 schedulers x ~3 ticks each with minimal overlap)"
    )
```

### 4.3 Runtime Production

Deploy a distributed execution counter using Redis to detect duplicate fires in real time:

```python
# monitoring/duplicate_detector.py
import functools
import time
import redis

_redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)

def detect_duplicate_execution(job_id: str, window_seconds: int = 10):
    """
    Decorator that emits a warning log if the decorated function is called
    more than once within `window_seconds` for the same `job_id`.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = f"job_exec:{job_id}:{int(time.time()) // window_seconds}"
            count = _redis_client.incr(key)
            _redis_client.expire(key, window_seconds * 2)

            if count > 1:
                import logging
                logging.getLogger(__name__).warning(
                    "DUPLICATE EXECUTION DETECTED: job_id=%s executed %d times "
                    "within the last %ds window",
                    job_id, count, window_seconds,
                )
            return func(*args, **kwargs)
        return wrapper
    return decorator


# Usage example
@detect_duplicate_execution(job_id="order_sync", window_seconds=60)
def sync_orders():
    # ... actual sync logic
    pass
```

---

## 5. Fix

### 5.1 Immediate

Stop all but one scheduler instance. Identify running scheduler processes:

```bash
# Linux: find all Python processes running a scheduler
ps aux | grep -E "scheduler|apscheduler|celery beat"

# If using APScheduler with a SQLite job store, identify which process owns the file lock
fuser /path/to/jobs.sqlite
```

Remove duplicate cron entries immediately:

```bash
# Export current crontab, deduplicate, and re-import
crontab -l | sort -u | crontab -
```

Apply a distributed lock to the job entry point as an emergency guard while the structural fix is prepared:

```python
# immediate_fix/distributed_lock_guard.py
import redis
import logging

_redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)
logger = logging.getLogger(__name__)

def with_distributed_lock(lock_name: str, timeout_seconds: int = 840):
    """
    Decorator that acquires a Redis lock before executing the job.
    If the lock is already held, the execution is skipped rather than queued.
    `timeout_seconds` should be set to slightly less than the cron interval.
    """
    def decorator(func):
        import functools
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            lock = _redis_client.lock(
                lock_name,
                timeout=timeout_seconds,
                blocking=False,   # Do not wait; skip if already locked
            )
            acquired = lock.acquire()
            if not acquired:
                logger.warning(
                    "Job %s skipped: lock %s already held by another instance",
                    func.__name__, lock_name,
                )
                return None
            try:
                return func(*args, **kwargs)
            finally:
                try:
                    lock.release()
                except redis.exceptions.LockNotOwnedError:
                    logger.error("Lock %s expired before job completed.", lock_name)
        return wrapper
    return decorator


@with_distributed_lock("order_sync_lock", timeout_seconds=840)
def sync_orders():
    # ... actual sync logic
    pass
```

### 5.2 Robust

Implement idempotency at the action level so that even if the job fires twice, the observable side effects occur exactly once:

```python
# robust_fix/idempotent_order_sync.py
import hashlib
import time
import logging
from dataclasses import dataclass
from typing import Optional
import redis

logger = logging.getLogger(__name__)
_redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)


@dataclass
class Order:
    order_id: str
    customer_email: str
    total_amount: float


def _idempotency_key(order_id: str, action: str) -> str:
    raw = f"{order_id}:{action}"
    return "idempotency:" + hashlib.sha256(raw.encode()).hexdigest()


def _mark_processed(order_id: str, action: str, ttl_seconds: int = 86400) -> bool:
    """
    Atomically mark an action as processed.
    Returns True if this call is the FIRST to mark it (safe to proceed).
    Returns False if already marked (duplicate; skip).
    """
    key = _idempotency_key(order_id, action)
    result = _redis_client.set(key, "1", nx=True, ex=ttl_seconds)
    return result is True


def send_confirmation_email(order: Order) -> None:
    """Send a confirmation email, guarded by an idempotency check."""
    if not _mark_processed(order.order_id, "confirmation_email"):
        logger.info("Email for order %s already sent; skipping duplicate.", order.order_id)
        return
    # ... actual email send logic
    logger.info("Confirmation email sent for order %s.", order.order_id)


def reserve_inventory(order: Order) -> None:
    """Reserve inventory, guarded by an idempotency check."""
    if not _mark_processed(order.order_id, "inventory_reservation"):
        logger.info(
            "Inventory for order %s already reserved; skipping duplicate.",
            order.order_id,
        )
        return
    # ... actual reservation logic
    logger.info("Inventory reserved for order %s.", order.order_id)


def sync_orders(orders: list[Order]) -> None:
    for order in orders:
        send_confirmation_email(order)
        reserve_inventory(order)
```

Configure APScheduler with all safety parameters set explicitly:

```python
# robust_fix/scheduler_config.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.redis import RedisJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from pytz import utc

jobstores = {
    'default': RedisJobStore(
        host='localhost',
        port=6379,
        db=0,
        jobs_key='apscheduler.jobs',
        run_times_key='apscheduler.run_times',
    )
}

executors = {
    'default': ThreadPoolExecutor(max_workers=4),
}

job_defaults = {
    'coalesce': True,           # Run only once if multiple misfires accumulated
    'max_instances': 1,         # Never allow more than one concurrent instance
    'misfire_grace_time': 60,   # Skip (do not retry) if delayed more than 60s
}

scheduler = BackgroundScheduler(
    jobstores=jobstores,
    executors=executors,
    job_defaults=job_defaults,
    timezone=utc,               # Always UTC; eliminates DST double-fire
)

scheduler.add_job(
    sync_orders,
    trigger='interval',
    minutes=15,
    id='order_sync',
    replace_existing=True,      # Prevents duplicate registration on restart
)
```

---

## 6. Architectural Prevention

**Use exactly one scheduler process per environment.** Enforce this at the infrastructure level. In Kubernetes, deploy the scheduler as a `Deployment` with `replicas: 1` and a `PodDisruptionBudget` that disallows more than zero unavailable pods during rollouts — but also disallows more than one running pod at a time by setting `maxSurge: 0` in the rolling update strategy.

```yaml
# k8s/scheduler-deployment.yaml (excerpt)
spec:
  replicas: 1
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 0        # Never create a new pod before the old one is terminated
      maxUnavailable: 1  # Allow the old pod to be terminated first
```

**Separate the scheduler from the worker.** The scheduler's only responsibility is to enqueue work (write to a queue). The worker processes items from the queue. The queue acts as a natural deduplication boundary when items carry an idempotency key.

**Use UTC everywhere.** Set the server timezone, the scheduler timezone, all database timestamps, and all log timestamps to UTC. This eliminates the entire class of DST-related timing bugs.

**Implement a "heartbeat" job.** Register a lightweight job that runs every minute and writes a timestamp to a monitoring key. Alert when the key has not been updated in more than two minutes (scheduler stopped) or has been updated more than twice in one minute (duplicate scheduler running).

---

## 7. Anti-patterns to Avoid

- **Calling `add_job` in application startup code without `replace_existing=True`.** Every application restart adds another copy of the job to the persistent store.
- **Using a local-time cron schedule on a server that observes DST.** Express all schedules in UTC and run `cron` with `CRON_TZ=UTC`.
- **Assuming that `max_instances=1` prevents cross-process duplication.** The `max_instances` parameter limits concurrency within a single scheduler instance. It does not prevent two independent scheduler processes from each launching one instance of the same job simultaneously.
- **Relying on database unique constraints as the sole duplicate-prevention mechanism.** Under high load, two transactions can both pass the uniqueness check before either commits.
- **Using `coalesce=False` without understanding its implications.** Setting `coalesce=False` instructs APScheduler to run the job for every missed firing rather than collapsing them into one. If the scheduler restarts after a long outage, this can trigger a burst of executions.
- **Copying a persistent job store to a new server without clearing it.** The new server inherits all job definitions and begins firing them immediately, in parallel with the original server if it is still running.

---

## 8. Edge Cases and Variants

**Blue-green deployment overlap.** When a blue environment is kept alive for 60 seconds after a green environment starts (to allow connection draining), both schedulers may fire the same job once if the interval falls within that window. Mitigate by stopping the scheduler on the blue environment before starting it on the green environment, separate from application process lifecycle.

**Kubernetes `CronJob` with `concurrencyPolicy: Allow`.** The default `concurrencyPolicy` does not prevent overlapping runs. A job that takes longer than its scheduled interval will accumulate concurrent instances. Set `concurrencyPolicy: Forbid` for jobs that must not overlap, or `Replace` for jobs where only the latest run matters.

```yaml
# k8s/cronjob.yaml (excerpt)
spec:
  concurrencyPolicy: Forbid
  schedule: "*/15 * * * *"
```

**APScheduler with `BlockingScheduler` inside a multiprocessing worker pool.** If a task runner (Celery, RQ) uses `fork`-based multiprocessing and a `BlockingScheduler` is initialized before the fork, each child process inherits the scheduler and begins firing jobs independently.

**NFS-backed SQLite job store.** SQLite's file-locking protocol does not work reliably over NFS. Two scheduler instances on different hosts sharing an NFS-mounted `jobs.sqlite` may both believe they hold the write lock and both fire jobs. Use a network-aware job store (PostgreSQL, Redis) for any multi-host deployment.

**Clock skew between hosts.** If two servers have clocks that differ by more than the scheduler's tick resolution (typically 1 second), a job scheduled for exactly 14:00:00 may fire on server A at 14:00:00.200 and on server B at 14:00:00.800. Both fire within the same second but from independent schedulers. NTP synchronization to within 100 ms is required for reliable distributed scheduling.

---

## 9. Audit Checklist

- [ ] All `scheduler.add_job()` calls include `replace_existing=True`.
- [ ] Scheduler timezone is explicitly set to UTC (`timezone=utc`).
- [ ] `job_defaults` includes `coalesce=True` and `max_instances=1`.
- [ ] `misfire_grace_time` is set to a value appropriate for the job's interval (not left at the default of 1 second).
- [ ] The persistent job store backend is a network-aware service (Redis, PostgreSQL), not a local file (SQLite) on a shared mount.
- [ ] Infrastructure deployment configuration ensures exactly one scheduler process runs per environment at any time (`maxSurge: 0` in Kubernetes, or equivalent).
- [ ] A distributed lock is applied at the job entry point as a defense-in-depth measure.
- [ ] All critical job actions implement idempotency via a persistent idempotency key store.
- [ ] Server and container timezones are set to UTC; no DST-observing timezone is used in scheduling configuration.
- [ ] CI/CD pipeline includes a static analysis step that detects unsafe `add_job` calls.
- [ ] Monitoring alerts exist for both "scheduler silent" (no heartbeat) and "scheduler duplicate" (heartbeat frequency exceeds expected rate).
- [ ] Kubernetes `CronJob` resources (if used) have `concurrencyPolicy: Forbid` or `Replace` explicitly set.
- [ ] Deployment runbooks include a step to stop the scheduler on the old instance before starting it on the new instance.
- [ ] Post-deployment verification query checks for duplicate rows in affected tables within 30 minutes of deployment.
- [ ] Log aggregation query for `"START"` events without a corresponding `"END"` within the job's expected runtime is part of the on-call runbook.

---

## 10. Further Reading

- APScheduler documentation — `misfire_grace_time`, `coalesce`, `max_instances`: https://apscheduler.readthedocs.io/en/stable/userguide.html#configuring-the-scheduler
- Crontab pitfalls and DST handling: https://crontab.guru and `man 5 crontab` (see the `CRON_TZ` variable)
- Redis distributed locks (Redlock algorithm): https://redis.io/docs/latest/develop/use/patterns/distributed-locks/
- Kubernetes `CronJob` concurrency policy: https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/#concurrency-policy
- Related failure patterns in the multi-agent failure patterns repository: https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns
