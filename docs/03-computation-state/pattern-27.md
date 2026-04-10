# Pattern #27 — None Reinitialization Crash

**Category:** Computation & State
**Severity:** Medium
**Affected Frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average Debugging Time if Undetected:** 1 to 5 days

---

## 1. Observable Symptoms

The system runs correctly during normal operation and also during initial startup — both paths work without errors. The crash appears only after a specific lifecycle event: a cleanup cycle, an error recovery sequence, a configuration reload, or a resource teardown. After that event, a subsequent operation that calls a method or accesses an attribute on what used to be a valid object raises `AttributeError: 'NoneType' object has no attribute '...'`.

**Immediate signals:**

- `AttributeError: 'NoneType' object has no attribute 'predict'` — or `.get()`, `.run()`, `.embed()`, `.encode()`, `.config`, `.metadata`, or any attribute of the object that was reset to `None`.
- The traceback points to a code path that runs on every request — not to the cleanup code that caused the problem. Engineers initially interpret this as a new bug in the request-handling code, not as a consequence of a reset that happened earlier.
- The error appears in logs as a sudden spike after a period of normal operation. The spike begins at a specific timestamp that corresponds exactly to when the cleanup or reset ran.
- Restarting the process fixes the crash — because startup re-initializes the variable. The fix lasts until the cleanup cycle runs again, at which point the crash recurs. Engineers observe a cycle: deploy, works, crashes, restart, works, crashes. The interval between works and crashes matches the cleanup cycle period.

**Delayed signals:**

- An ML model serving endpoint returns 500 errors for all requests after a model reload or error recovery procedure. The requests that arrived before the reload worked; all requests after it fail.
- A pipeline agent begins failing with `NoneType` errors partway through a long run. Investigation reveals the errors started after a background health check reset a resource to `None` without reinitializing it.
- A retry mechanism successfully recovers from an upstream error but leaves the downstream processing object in a `None` state, causing the next item in the queue to crash.

**The distinguishing characteristic of this pattern** is the temporal gap between cause and symptom. The cleanup code that sets the variable to `None` runs successfully and silently. The crash happens later, in unrelated code, when the variable is next accessed. Without knowing the cleanup cycle's timing, the crash appears completely unprovoked. Engineers who examine only the crashing line — the `.predict()` call, the `.get()` call — will find correct code. The bug is not at the crash site; it is in the cleanup that ran before it.

---

## 2. Field Story (Anonymized)

**Domain:** ML model serving pipeline.

A machine learning team built a document classification service using a LangChain pipeline. The core component was a `ModelServer` class that held a loaded HuggingFace sentence transformer model in `self.model`. At startup, the model was loaded from a local file path, a warmup inference was run, and the server was marked ready. Each incoming document was classified by calling `self.model.encode(document_text)`.

The team added an error recovery mechanism after observing that the model occasionally became unresponsive under high load. The recovery logic was: if three consecutive inference calls raised an exception, run `self._reset_model()`, which set `self.model = None` and released GPU memory. The intent was to release the resource cleanly before reloading it. The method that was supposed to reload the model after the reset was `self._reload_model()`.

The reload call was in a `finally` block inside the error recovery function:

```python
def _handle_inference_error(self):
    self._reset_model()    # sets self.model = None
    try:
        self._reload_model()
    except Exception as reload_exc:
        logger.error("Reload failed: %s", reload_exc)
        # returns without reinitializing — self.model remains None
```

In a staging environment running on machines with reliable GPUs, the reload never failed. In production, the model was served from a network-attached storage volume. Occasionally, at high system load, the volume was briefly unavailable during the reload window. The reload raised an `OSError`. The `except` block logged it and returned. `self.model` was now `None`. The `finally` block was absent — the method simply returned after logging.

The next document that arrived for classification called `self.model.encode(document_text)`. Python raised `AttributeError: 'NoneType' object has no attribute 'encode'`. Every subsequent request failed identically. The service appeared completely broken.

The support ticket from the operations team said: "The model server stopped working. All requests return 500. Nothing changed in the code." The engineer who investigated looked at the `encode()` call first and confirmed it was correct. Then they checked whether the model was loaded. Adding a log line at the start of the request handler revealed `self.model is None`. This prompted them to search for all `self.model = None` assignments — there was exactly one, in `_reset_model()`. Tracing the call chain to `_handle_inference_error()` and then to the reload failure revealed the root cause.

The fix took 20 minutes. The investigation took three days, because the engineers looked at the symptom (`.encode()` failing) rather than the cause (a failed reload leaving `self.model` in a reset state).

---

## 3. Technical Root Cause

The bug arises from a two-step pattern that is separated in time and, often, in code:

1. A cleanup or reset method explicitly assigns `None` to a variable: `self.model = None`.
2. A reload or reinitialization method is called next, but fails — raising an exception, returning early, or simply not being called at all.
3. The variable remains `None`. All subsequent code that assumes the variable is a valid object crashes with `AttributeError`.

```python
# The problematic pattern — reset without guaranteed reinit
def _handle_error(self):
    self.model = None       # step 1: resource released

    try:
        self._reload()      # step 2: should reinitialize
    except Exception as e:
        logger.error("Reload failed: %s", e)
        return              # BUG: returns with self.model still None
                            # No fallback. No sentinel. No guard.
```

The reason this pattern is subtle is that the code worked before. The initial load at startup always succeeds (because startup failures are caught immediately and block the service from becoming available). The reset-and-reload cycle in error recovery is a different code path that runs under different conditions — precisely the conditions (high load, partial resource unavailability) under which reloads are most likely to fail.

**Four specific conditions that create this pattern:**

1. **Cleanup without guaranteed reinitialization.** `self.x = None` is in a `reset()` method. The matching `self.x = SomeObject(...)` is in a separate `reinit()` method. If `reinit()` is not called, or fails, `self.x` is permanently `None`.

2. **Exception swallowing in the reload path.** The reload call is inside a `try/except` that catches the exception, logs it, and continues. The caller receives no signal that the reload failed and continues processing as if the variable is valid.

3. **Conditional initialization that misses the reset case.** `__init__` sets `self.x = SomeObject(...)`. A `reset()` method sets `self.x = None`. A subsequent call to `__init__` would re-initialize, but `__init__` is not called again — only `reset()` was called. The variable is now `None` without having gone through the init path.

4. **Late binding and deferred initialization.** The variable starts as `None` in `__init__` (lazy initialization pattern). `_initialize()` is called on first use. After a reset, the `_initialized` flag may be cleared, but the guard that calls `_initialize()` may not be present in every code path — only in the primary flow, not in error recovery paths.

The crash is a symptom of a deeper design flaw: the class has a valid-state invariant (`self.model is not None when serving requests`) but no mechanism to enforce it. The `None` assignment is an allowed transition that creates an invalid intermediate state, and the code does not guarantee that this state is transient.

---

## 4. Detection

### 4.1 Manual Code Audit

Find all explicit assignments of `None` to instance attributes (other than in `__init__`):

```bash
# Find self.x = None outside of __init__
grep -rn "self\.\w\+\s*=\s*None" --include="*.py" | grep -v "def __init__"

# Find methods named reset, cleanup, teardown, clear, destroy
grep -rn "def\s\+\(reset\|cleanup\|teardown\|clear\|destroy\|release\|unload\)" \
    --include="*.py" -A 20

# Find attribute access on variables that are assigned None elsewhere
grep -rn "self\.model\.\|self\.client\.\|self\.connection\.\|self\.session\." \
    --include="*.py" | grep -v "if self\."
```

For each `self.x = None` found outside `__init__`, trace the following questions:

- What method contains this assignment? Is it a reset, cleanup, or error handler?
- Is there a corresponding `self.x = <valid_object>` that always runs after the reset?
- Is the reinitalization inside a `try` block that can fail silently?
- Is there a guard (`if self.x is None: self._reinit()`) at every code path that subsequently uses `self.x`?

### 4.2 Automated CI/CD

Write a test that explicitly calls the reset/cleanup method and then exercises the normal operation path. This is the test that almost never exists and almost always would have caught the bug:

```python
# tests/test_model_server_reset_recovery.py
import pytest
from unittest.mock import patch, MagicMock
from myagents.model_server import ModelServer


class TestModelServerNoneReinitialization:
    """
    Test that ModelServer correctly recovers from reset/cleanup cycles.

    The key scenario: reset sets self.model = None. If reload fails,
    subsequent requests must not raise AttributeError — they must either
    succeed (with a fallback) or raise a clear, catchable domain exception.
    """

    def test_successful_reload_after_reset(self, tmp_path):
        """After a reset, a successful reload allows normal operation to resume."""
        server = ModelServer(model_path=str(tmp_path / "model"))
        server._load_model()  # initial load (mocked)

        server._reset_model()  # simulate cleanup
        assert server.model is None, "Post-reset: model should be None"

        server._reload_model()  # simulate recovery
        assert server.model is not None, "Post-reload: model must not be None"

        # Normal operation must work after reload
        result = server.classify("test document")
        assert result is not None

    def test_failed_reload_raises_clear_exception_not_attribute_error(self, tmp_path):
        """
        If reload fails, subsequent classify() calls must raise ModelUnavailableError,
        not AttributeError. AttributeError leaks the implementation detail that
        self.model is None; a domain exception communicates actionable information.
        """
        server = ModelServer(model_path=str(tmp_path / "model"))
        server._load_model()

        server._reset_model()

        # Simulate reload failure
        with patch.object(server, "_reload_model", side_effect=OSError("Volume unavailable")):
            server._handle_inference_error()

        # The next request must raise a domain exception, NOT AttributeError
        with pytest.raises(ModelUnavailableError):
            server.classify("test document")

        # Critically: AttributeError must NOT be raised
        try:
            server.classify("test document")
        except AttributeError as e:
            pytest.fail(
                f"NONE REINITIALIZATION: classify() raised AttributeError ({e}) "
                "after a failed reload. self.model was left as None. "
                "The cleanup path does not guarantee reinitialization."
            )
        except Exception:
            pass  # Any other exception (including ModelUnavailableError) is acceptable

    def test_model_is_never_none_during_serving(self, tmp_path):
        """
        Property-based check: no sequence of reset/reload/serve operations should
        leave self.model as None when serve() is called.
        """
        server = ModelServer(model_path=str(tmp_path / "model"))
        server._load_model()

        # Simulate 10 error-recovery cycles, some with reload failures
        for i in range(10):
            server._reset_model()
            if i % 3 == 0:
                # Every 3rd cycle, simulate a reload failure
                with patch.object(server, "_reload_model", side_effect=OSError("I/O error")):
                    try:
                        server._handle_inference_error()
                    except Exception:
                        pass
            else:
                server._handle_inference_error()  # successful recovery

            # After any recovery attempt, serving must not raise AttributeError
            try:
                server.classify("probe document")
            except AttributeError:
                pytest.fail(
                    f"self.model is None after recovery cycle {i}. "
                    "The server entered an unrecoverable None state."
                )
            except Exception:
                pass  # Domain exceptions are acceptable
```

### 4.3 Runtime Production

Add a guard at every entry point that accesses the potentially-`None` variable. Emit a structured warning when the guard triggers — this makes the `None` state visible before it causes a crash:

```python
import logging
import functools
from typing import Callable, TypeVar, Any

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def require_model(method: F) -> F:
    """
    Decorator that guards any ModelServer method requiring self.model to be loaded.

    If self.model is None, raises ModelUnavailableError with a diagnostic message
    instead of allowing AttributeError to propagate.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if self.model is None:
            logger.error(
                "ModelServer.%s called with self.model=None. "
                "The model was reset but not reinitialized. "
                "This indicates a failed reload or an incomplete recovery path. "
                "Server state: reset_count=%d last_reset_at=%s",
                method.__name__,
                getattr(self, "_reset_count", "unknown"),
                getattr(self, "_last_reset_at", "unknown"),
            )
            raise ModelUnavailableError(
                f"Model is not loaded. Cannot execute {method.__name__}(). "
                "The server is in an error recovery state. "
                "Retry after the recovery window or check server health endpoint."
            )
        return method(self, *args, **kwargs)
    return wrapper  # type: ignore[return-value]


class ModelUnavailableError(RuntimeError):
    """Raised when a ModelServer method is called while the model is not loaded."""
```

Add a health check endpoint that explicitly reports `model_loaded: false` when `self.model is None`, so the load balancer can route traffic away from unhealthy instances:

```python
def health(self) -> dict:
    return {
        "status": "ok" if self.model is not None else "degraded",
        "model_loaded": self.model is not None,
        "reset_count": self._reset_count,
        "last_reset_at": self._last_reset_at,
    }
```

---

## 5. Fix

### 5.1 Immediate Fix

Restructure the error recovery method to guarantee that either the model is successfully reloaded or a clear, persistent error state is set that blocks further requests with a domain exception — never with `AttributeError`:

```python
# model_server.py — corrected error recovery
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ModelUnavailableError(RuntimeError):
    """The model is not currently loaded. The server is recovering or degraded."""


class ModelServer:
    """
    Serves ML model inference. Handles load, reset, and reload lifecycle.

    Invariant: self.model is either a loaded SentenceTransformer (or equivalent)
    or None. When None, all public methods raise ModelUnavailableError, never
    AttributeError.
    """

    def __init__(self, model_path: str, max_reload_attempts: int = 3):
        self.model_path = model_path
        self.max_reload_attempts = max_reload_attempts
        self.model = None
        self._reset_count = 0
        self._last_reset_at: Optional[str] = None
        self._reload_failure_reason: Optional[str] = None

    def load(self) -> None:
        """Initial load at startup. Raises on failure (blocks service from starting)."""
        self.model = self._do_load()
        logger.info("ModelServer: model loaded from %s", self.model_path)

    def classify(self, text: str) -> str:
        """Classify a document. Raises ModelUnavailableError if model is not loaded."""
        if self.model is None:
            raise ModelUnavailableError(
                f"Model is not loaded (last reset: {self._last_reset_at}, "
                f"reason: {self._reload_failure_reason}). "
                "Retry after recovery or check /health."
            )
        return self.model.encode(text)

    def recover_from_error(self) -> bool:
        """
        Attempt to reset and reload the model after consecutive inference failures.

        Returns True if recovery succeeded (model is loaded again).
        Returns False if recovery failed (model remains None; serves ModelUnavailableError).

        Never leaves self.model in an undefined state. After this method returns,
        self.model is either a valid model object or None with a logged reason.
        """
        self._reset_count += 1
        self._last_reset_at = datetime.now(timezone.utc).isoformat()
        logger.warning(
            "ModelServer: initiating recovery (reset #%d).", self._reset_count
        )

        # Step 1: Release the existing model (may already be broken, release GPU memory).
        old_model = self.model
        self.model = None
        if old_model is not None:
            try:
                del old_model
            except Exception:
                pass  # best-effort release; do not block recovery on cleanup failure

        # Step 2: Attempt reload with retries.
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_reload_attempts + 1):
            try:
                self.model = self._do_load()
                self._reload_failure_reason = None
                logger.info(
                    "ModelServer: recovery succeeded on attempt %d/%d.",
                    attempt,
                    self.max_reload_attempts,
                )
                return True
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "ModelServer: reload attempt %d/%d failed: %s. "
                    "self.model remains None.",
                    attempt,
                    self.max_reload_attempts,
                    exc,
                )
                if attempt < self.max_reload_attempts:
                    time.sleep(2 ** attempt)  # exponential backoff between attempts

        # Step 3: All attempts failed. self.model is None. Record the reason.
        self._reload_failure_reason = str(last_exc)
        logger.error(
            "ModelServer: all %d reload attempts failed. "
            "Model is not loaded. Subsequent requests will raise ModelUnavailableError. "
            "Manual intervention required. Last error: %s",
            self.max_reload_attempts,
            last_exc,
        )
        return False

    def health(self) -> dict:
        return {
            "status": "ok" if self.model is not None else "degraded",
            "model_loaded": self.model is not None,
            "reset_count": self._reset_count,
            "last_reset_at": self._last_reset_at,
            "reload_failure_reason": self._reload_failure_reason,
        }

    def _do_load(self):
        """Load the model from disk. Raises on any failure."""
        # Replace with actual model loading logic:
        # from sentence_transformers import SentenceTransformer
        # return SentenceTransformer(self.model_path)
        path = Path(self.model_path)
        if not path.exists():
            raise FileNotFoundError(f"Model path does not exist: {path}")
        # Simulate model object
        return object()
```

### 5.2 Robust Fix — State Machine with Explicit Transitions

For systems where the model goes through multiple lifecycle states (unloaded, loading, ready, resetting, reloading, failed), use an explicit state machine to make every transition visible and to prevent calling methods in invalid states:

```python
# model_server_state_machine.py
import logging
import threading
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger(__name__)


class ModelState(Enum):
    UNLOADED = auto()    # initial state; never served from
    LOADING = auto()     # load in progress; block requests
    READY = auto()       # model is loaded and healthy
    RESETTING = auto()   # cleanup in progress; block requests
    RELOADING = auto()   # reload in progress after reset; block requests
    FAILED = auto()      # reload failed; raise domain error until manual recovery


_VALID_TRANSITIONS: dict[ModelState, set[ModelState]] = {
    ModelState.UNLOADED:   {ModelState.LOADING},
    ModelState.LOADING:    {ModelState.READY, ModelState.FAILED},
    ModelState.READY:      {ModelState.RESETTING},
    ModelState.RESETTING:  {ModelState.RELOADING},
    ModelState.RELOADING:  {ModelState.READY, ModelState.FAILED},
    ModelState.FAILED:     {ModelState.LOADING},   # manual recovery only
}


class ModelUnavailableError(RuntimeError):
    pass


class ModelServer:
    """
    Model server with explicit state machine governing the model lifecycle.

    Calling classify() in any state other than READY raises ModelUnavailableError.
    No code path can accidentally leave the server in an undocumented None state.
    """

    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model = None
        self._state = ModelState.UNLOADED
        self._state_reason: str = "Initial state."
        self._lock = threading.Lock()

    @property
    def state(self) -> ModelState:
        return self._state

    def _transition(self, new_state: ModelState, reason: str = "") -> None:
        with self._lock:
            if new_state not in _VALID_TRANSITIONS[self._state]:
                raise RuntimeError(
                    f"Invalid state transition: {self._state.name} -> {new_state.name}. "
                    f"Valid transitions from {self._state.name}: "
                    f"{[s.name for s in _VALID_TRANSITIONS[self._state]]}. "
                    f"Reason for attempted transition: {reason}"
                )
            old_state = self._state
            self._state = new_state
            self._state_reason = reason
            logger.info(
                "ModelServer state: %s -> %s. Reason: %s",
                old_state.name,
                new_state.name,
                reason,
            )

    def load(self) -> None:
        self._transition(ModelState.LOADING, "Initial load.")
        try:
            self.model = self._do_load()
            self._transition(ModelState.READY, "Load succeeded.")
        except Exception as exc:
            self.model = None
            self._transition(ModelState.FAILED, f"Load failed: {exc}")
            raise

    def classify(self, text: str) -> str:
        if self._state != ModelState.READY:
            raise ModelUnavailableError(
                f"ModelServer is not ready (state={self._state.name}, "
                f"reason={self._state_reason}). Cannot classify."
            )
        # self.model is guaranteed non-None when state is READY
        return self.model.encode(text)

    def recover(self) -> bool:
        self._transition(ModelState.RESETTING, "Error recovery initiated.")
        old_model = self.model
        self.model = None
        try:
            del old_model
        except Exception:
            pass

        self._transition(ModelState.RELOADING, "Reset complete, attempting reload.")
        try:
            self.model = self._do_load()
            self._transition(ModelState.READY, "Reload succeeded.")
            return True
        except Exception as exc:
            self.model = None
            self._transition(ModelState.FAILED, f"Reload failed: {exc}")
            logger.error(
                "ModelServer recovery failed. State=FAILED. "
                "Manual intervention required (call load() to retry). Error: %s",
                exc,
            )
            return False

    def health(self) -> dict:
        return {
            "state": self._state.name,
            "model_loaded": self.model is not None,
            "state_reason": self._state_reason,
        }

    def _do_load(self):
        from pathlib import Path
        path = Path(self.model_path)
        if not path.exists():
            raise FileNotFoundError(f"Model not found: {path}")
        return object()  # replace with actual model loader
```

Usage in a LangGraph node:

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict, Literal
from model_server_state_machine import ModelServer, ModelUnavailableError

model_server = ModelServer(model_path="/models/classifier_v3")
model_server.load()


class ClassificationState(TypedDict):
    document: str
    label: str | None
    error: str | None


def classify_node(state: ClassificationState) -> ClassificationState:
    try:
        label = model_server.classify(state["document"])
        return {**state, "label": label, "error": None}
    except ModelUnavailableError as exc:
        # Trigger recovery and return a retryable error signal
        logger.warning("Model unavailable: %s. Triggering recovery.", exc)
        model_server.recover()
        return {**state, "label": None, "error": "MODEL_UNAVAILABLE_RETRY"}


def route_after_classify(
    state: ClassificationState,
) -> Literal["output_node", "retry_node", "error_node"]:
    if state["label"] is not None:
        return "output_node"
    if state["error"] == "MODEL_UNAVAILABLE_RETRY":
        return "retry_node"
    return "error_node"
```

---

## 6. Architectural Prevention

**Principle:** a class that holds a resource (model, connection, client, session) must enforce its own validity invariant. The invariant is: if any public method requires the resource to be non-`None`, then that method must check for `None` and raise a clear domain exception, not allow Python to raise `AttributeError`.

This principle has three concrete implementation rules:

1. **No public method should ever raise `AttributeError` due to an internal attribute being `None`.** `AttributeError` is an implementation leak. Replace it with a domain exception that communicates why the resource is unavailable and what to do.

2. **Every `self.x = None` outside `__init__` must be paired with a guaranteed reinit or a state transition that blocks further use.** The reset is not atomic in real time, but it must be atomic in terms of the class invariant: the object must never be in a state where `self.x is None` and public methods that require `self.x` can be called without raising a domain exception.

3. **Cleanup and reinit must be in the same method, or the method that calls cleanup must be the only entry point that can reach reinit.** If cleanup is in `_reset()` and reinit is in `_reload()`, they can get separated. Prefer a single `_recover()` method that performs both steps atomically.

For LangChain and LangGraph pipelines, these patterns are especially important because nodes are functions that access shared objects via closures or module-level singletons. A `None`-reset singleton will crash every subsequent node invocation until the process is restarted:

```
# Architecture that avoids None propagation in LangGraph

┌─────────────────────────────────────────────┐
│  ModelServer (module-level singleton)        │
│                                              │
│  State machine: UNLOADED → LOADING → READY  │
│               ↑                    ↓        │
│           FAILED ← RELOADING ← RESETTING    │
│                                             │
│  classify() checks state before accessing   │
│  self.model — never raw attribute access    │
└─────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────┐    ┌───────────────────────┐
│  classify_node          │    │  recovery_node         │
│  calls model.classify() │ →  │  calls model.recover() │
│  catches ModelUnavail.  │    │  reports health state  │
└─────────────────────────┘    └───────────────────────┘
```

---

## 7. Anti-patterns to Avoid

**Anti-pattern 1: Setting `self.x = None` in a cleanup method with no paired reinit guarantee.**

```python
# WRONG
def cleanup(self):
    self.model = None    # released

def serve(self, text):
    return self.model.encode(text)  # crashes if cleanup() was called without reload()
```

Any code path that calls `cleanup()` without subsequently calling `reload()` before the next `serve()` will crash. The caller is responsible for sequencing — which is too much responsibility to rely on.

**Anti-pattern 2: Swallowing the reload exception and continuing.**

```python
# WRONG — the except block does not re-raise and does not block subsequent use
def _recover(self):
    self.model = None
    try:
        self.model = self._load_model()
    except Exception as e:
        logger.error("Reload failed: %s", e)
        # self.model is still None; no barrier prevents the next .encode() call
```

After this returns, callers have no way to know the reload failed. They proceed to call `self.model.encode()` and get `AttributeError`.

**Anti-pattern 3: Using `hasattr` or `is not None` checks scattered across call sites.**

```python
# WRONG — defensive checks at every call site
def classify(self, text):
    if hasattr(self, "model") and self.model is not None:
        return self.model.encode(text)
    return "unknown"  # silent fallback — hides the real error
```

This silences the `AttributeError` but substitutes a wrong answer (`"unknown"`) for every request while the model is `None`. Silent wrong answers are worse than loud errors in a classification pipeline. Defensive checks are not a substitute for invariant enforcement.

**Anti-pattern 4: Lazy initialization without a guard in every code path.**

```python
# WRONG — _initialize() is only called in the primary path
def __init__(self):
    self.model = None  # lazy

def classify(self, text):
    if self.model is None:
        self._initialize()  # called here
    return self.model.encode(text)

def batch_classify(self, texts):
    # BUG: no lazy init guard here
    return [self.model.encode(t) for t in texts]  # crashes if model is None
```

Lazy initialization requires the guard at every method that uses the resource, not only at the primary method. This is error-prone and easy to miss when adding new methods.

**Anti-pattern 5: Treating cleanup as optional in error paths.**

```python
# WRONG — cleanup only happens on success, leaving resources allocated on error
def process(self, item):
    result = self.model.encode(item)
    if result is None:
        self.model = None  # cleanup on error
        # ... but then no reload, and subsequent calls crash
    return result
```

Cleanup must be paired with reinit. Cleanup-without-reinit is only valid if the object is being permanently destroyed (i.e., going out of scope). If the object will be used again, the cleanup must be matched by a guaranteed reinit or a state transition that prevents use.

---

## 8. Edge Cases and Variants

**Variant A — Config reload sets a client to `None`.**
A `ConfigManager` holds a database client in `self.db_client`. A config reload method does `self.db_client = None` while reinitializing, intending to replace it. If the new connection fails, `self.db_client` is `None` and all subsequent queries crash. Fix: build the new client first, then atomically swap: `new_client = build_client(new_config); self.db_client = new_client`. Never set to `None` before the replacement is confirmed.

**Variant B — Concurrent reset and use.**
`self.model = None` is set by a background cleanup thread. Simultaneously, a request thread calls `self.model.encode()`. The race condition produces `AttributeError` even if a single-threaded recovery would have succeeded. Fix: protect the model reference with a `threading.RLock`. Acquire the lock in both the reset method and in every method that accesses `self.model`.

**Variant C — LangGraph checkpointer state vs. object state.**
A LangGraph graph stores a `model_loaded` boolean in its `TypedDict` state. The actual model object is held in a module-level singleton. The state says `model_loaded: True` (from a previous run, recovered from the checkpointer) but the module-level singleton was reset to `None` by a background health check after the checkpoint was saved. The graph believes the model is loaded; the object is `None`. Fix: never use checkpointed state as the ground truth for the liveness of a runtime object. Always check the object itself.

**Variant D — `__del__` calling `self.x = None`.**
A `__del__` method is added for cleanup and sets `self.x = None`. Python's garbage collector may call `__del__` before the object is truly out of scope in edge cases involving reference cycles. This can reset `self.x` to `None` while another reference to the object still exists and is being used. Fix: do not use `__del__` for state management. Use context managers (`__enter__`/`__exit__`) or explicit `close()` / `dispose()` methods instead.

**Variant E — CrewAI tool reset between tasks.**
A CrewAI `Tool` object holds a loaded embedding model. The CrewAI framework calls a `reset()` hook between tasks to release resources. The tool's `reset()` sets `self.embedder = None`. The framework then calls the tool again on the next task without re-calling `__init__`. The tool crashes on the first embedding call of the new task. Fix: implement `reset()` to call `self._load_embedder()` at the end, or implement a lazy-init guard in the embedding method that checks and reloads if `None`.

**Variant F — Error recovery that sets to `None` inside a `with` block.**
A context manager wraps a resource. On exception, `__exit__` sets `self.resource = None`. The `with` block catches the exception and continues, calling `self.resource.method()` again within the same `with` block body. Fix: never access a resource after its context manager has exited (or been told to exit via exception). Use separate `with` blocks for the initial use and for recovery.

---

## 9. Audit Checklist

Use this checklist during code review for any class that holds a mutable resource (model, client, connection, session, handle) in an instance attribute.

- [ ] Every assignment `self.x = None` outside of `__init__` is immediately followed (in the same method or a guaranteed call chain) by `self.x = <valid_reinit>` or a state transition that blocks further use.
- [ ] No public method raises `AttributeError` when a resource attribute is `None` — domain exceptions (`ModelUnavailableError`, `ClientNotReadyError`) replace it.
- [ ] The `except` clause in any reload/recovery path does not silently swallow the exception and return normally — it either re-raises, sets a failure state, or raises a domain exception.
- [ ] If lazy initialization is used (`self.x = None` in `__init__`, initialized on first use), the guard `if self.x is None: self._init_x()` is present in every method that accesses `self.x`, not only in the primary method.
- [ ] Concurrent access to mutable resource attributes is protected by a lock; reset and read operations cannot interleave.
- [ ] A health check or `status()` method reports whether the resource is loaded, so operators and load balancers can detect the degraded state without waiting for a crash.
- [ ] A CI test exists that explicitly calls the cleanup/reset method and then calls the normal serving method, asserting no `AttributeError` is raised.
- [ ] A CI test exists that simulates a reload failure (mocking the load function to raise) and verifies that subsequent serving calls raise a domain exception, not `AttributeError`.
- [ ] The class's docstring or type annotation documents the invariant: "self.x is None only during cleanup; all public methods require self.x to be non-None."
- [ ] `__del__` is not used to set resource attributes to `None`. Context managers or explicit `close()` methods are used instead.

---

## 10. Further Reading

**Internal cross-references:**

- [Pattern #13 — Silent Config Override](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/03-computation-state/pattern-13-silent-config-override.md): a config reload that resets a parameter is a structural cousin of this pattern; the difference is that a config value reset to its default produces wrong output, while a resource reset to `None` produces an immediate crash.
- [Pattern #17 — Division Edge Case](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/03-computation-state/pattern-17-division-edge-case.md): both patterns involve a function receiving an operand that was valid during testing but is invalid in specific production conditions. The fix in both cases is to replace silent incorrect behavior (returning 0, raising `AttributeError`) with an explicit domain signal.
- [Pattern #06 — Silent NameError](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/03-calculs-etat/pattern-06-silent-nameerror.md): the symptom surface is similar (`AttributeError` vs. `NameError`); the root cause is different (reinitialization to `None` vs. missing variable). When triaging, check both: is the variable missing entirely, or was it reset?
- [Pattern #11 — Race Condition on Shared File](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/09-gouvernance-multi-agents/pattern-11-race-condition-shared-file.md): Variant B of this pattern (concurrent reset and use) is a race condition. The locking fix described in Section 8 corresponds to the coordination patterns described in Pattern #11.

**External references:**

- Python documentation — `object.__del__`: https://docs.python.org/3/reference/datamodel.html#object.__del__ — the official documentation explicitly warns against relying on `__del__` for deterministic cleanup, directly addressing Variant D of this pattern.
- Python documentation — `contextlib.contextmanager` and context manager protocol: https://docs.python.org/3/library/contextlib.html — the standard Python mechanism for paired setup/teardown that guarantees cleanup does not outlive validity.
- Gamma et al., "Design Patterns: Elements of Reusable Object-Oriented Software" (Addison-Wesley, 1994) — the State pattern (Chapter 5). The state machine fix described in Section 5.2 is a direct application of the State pattern. The pattern is specifically designed to prevent objects from being used in invalid states.
- "Working Effectively with Legacy Code" (Michael Feathers, Prentice Hall, 2004) — Chapter 20: "This Class Is Too Big and I Don't Want It to Get Any Bigger." Classes that hold mutable resources with complex lifecycle states are a primary example of the "too big" class smell. The refactoring strategies in this chapter (extract class, replace conditional with polymorphism) apply directly to the state machine fix.
- Python `attrs` library — validators and `__attrs_post_init__`: https://www.attrs.org/en/stable/init.html#validators — `attrs` validators can enforce class invariants (including `self.x is not None`) at object construction time, providing a lightweight alternative to full state machines for simpler cases.
- All patterns in this playbook: https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns
