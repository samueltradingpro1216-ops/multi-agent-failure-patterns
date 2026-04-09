# Pattern #21 — LLM Pool Contention

**Category:** Multi-Agent Governance
**Severity:** High
**Affected Frameworks:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average Debugging Time if Undetected:** 2 to 14 days

---

## 1. Observable Symptoms

LLM pool contention is one of the most disorienting bugs in multi-agent systems because its symptoms look like multiple different problems depending on where you look. The true cause — rate limit exhaustion caused by uncoordinated agents — is rarely the first hypothesis.

**User-facing symptoms:**
- The system becomes progressively slower under load, not immediately. Initial requests succeed; degradation sets in after a ramp-up period.
- Some agent workflows complete normally while others hang indefinitely or return errors. The pattern of failures is not random — the same agents consistently fail while others consistently succeed, because retry competition favors agents that started earlier.
- Responses that normally take 2 seconds begin taking 30 to 90 seconds. Users see spinning indicators with no error message.
- Certain low-priority background agents (analytics, summarization) starve completely and never produce output.

**System/log symptoms:**
- HTTP 429 `Too Many Requests` errors appear in agent logs, but not uniformly across all agents. One or two agents dominate the 429 error count.
- Retry storms: log lines show the same agent retrying the same LLM call dozens of times within seconds, each attempt triggering another 429.
- Retry-after headers report increasing backoff windows (60s, 120s, 240s) because the pool never drains before the next wave hits.
- Total API cost spikes without a corresponding increase in successful completions — retries consume quota without producing value.
- `RateLimitError`, `APIStatusError`, `openai.error.RateLimitError`, or equivalent exception names appear in agent exception logs at high frequency.
- The number of in-flight requests across all agents simultaneously exceeds the API tier's requests-per-minute (RPM) or tokens-per-minute (TPM) limits.

**Misleading indicators:**
- Individual agents pass all unit tests because tests mock the LLM and never trigger rate limits.
- The bug only appears when agents run concurrently, which may not happen in staging if tests run sequentially.
- APM dashboards show high latency on LLM calls but may attribute it to model inference time rather than queue wait time.

---

## 2. Field Story (Anonymized)

A fintech startup built a crypto wallet analytics platform using six agents: a **Price Feed Agent** that polled exchange data and generated market summaries, an **Anomaly Detection Agent** that analyzed unusual transaction patterns, a **Portfolio Agent** that compiled per-user portfolio reports, a **Risk Agent** that scored addresses for compliance purposes, a **Notification Agent** that drafted alert messages, and a **Weekly Digest Agent** that summarized activity for a weekly email.

All six agents used the same OpenAI API key. The system was designed to run agents in parallel to minimize end-to-end latency — this was a documented architectural decision made explicitly to improve user experience.

During beta testing with five users and sequential workloads, everything worked. When the platform launched publicly and began processing real traffic, failures appeared within hours. The Price Feed Agent and Anomaly Detection Agent were the most active (they ran continuously) and consumed the majority of the rate limit budget. The Portfolio Agent, which ran on-demand when users viewed their portfolios, began failing with timeouts. Users reported that the portfolio page "never loads."

The team's first hypothesis was a database performance problem — the Portfolio Agent queried a PostgreSQL database before calling the LLM. They spent two days optimizing queries and adding indexes. Latency did not improve. A senior engineer then noticed that the Portfolio Agent's LLM calls were failing with 429 errors, not timing out at the database layer. This shifted the investigation to the LLM layer.

The root cause: all six agents called `openai.ChatCompletion.create()` independently, each with its own retry logic (exponential backoff with jitter, implemented differently in each agent — three agents used LangChain's built-in retry, two used a custom decorator, one had no retry at all). When the Price Feed and Anomaly agents hit the rate limit together, they both started retrying. Their retries re-saturated the limit the moment it recovered, preventing other agents from ever getting a successful call through.

The Portfolio Agent, which had no retry logic and short timeouts, gave up quickly and returned errors to users. The Weekly Digest Agent had simply stopped producing output entirely — its failures were silent because no user was waiting synchronously for it.

The fix introduced a centralized token-bucket rate limiter shared across all agents, with a priority queue that assigned weights: Risk Agent (highest, for compliance), Portfolio Agent (high, user-facing), Notification Agent (medium), Anomaly Detection Agent (medium), Price Feed Agent (low, can tolerate delay), Weekly Digest Agent (lowest, batch). After the fix, user-facing portfolio loads completed within 3 seconds and no agent starved. Total API cost decreased by 22% because retry waste was eliminated.

Debugging total: eleven days, of which nine were spent on the wrong hypotheses (database, network, model temperature settings).

---

## 3. Technical Root Cause

OpenAI and other LLM providers enforce rate limits at two levels: **requests per minute (RPM)** and **tokens per minute (TPM)**. Both limits apply to the API key, not to the individual process or thread making the request. When multiple agents share a key, their usage aggregates.

The thundering herd failure mode proceeds as follows:

1. Agents A, B, C, D, E, F all call the API concurrently.
2. The aggregate request rate exceeds the RPM or TPM limit.
3. The provider returns HTTP 429 with a `Retry-After` header (e.g., 60 seconds).
4. Each agent independently interprets this 429. Each one schedules its own retry after approximately 60 seconds (with jitter).
5. After 60 seconds, all agents retry simultaneously. The aggregate rate again exceeds the limit.
6. Step 3 repeats. The rate limit never recovers because the retry wave is synchronized (all agents got their 429 at roughly the same time and all backoff by roughly the same amount).
7. Agents with shorter timeouts or fewer retry attempts give up first. Higher-priority user-facing agents may be the first to give up if they were designed with strict SLAs.
8. Agents with longer retry loops continue hammering the limit, preventing recovery for the entire pool.

This is the classic **thundering herd problem**, applied to API rate limiting. The standard solution is global coordination: a single authority manages the available rate budget and allocates it to requestors according to a priority policy.

The **token bucket algorithm** is the standard mechanism. A bucket holds a maximum of `capacity` tokens and is refilled at rate `r` tokens per second. Each LLM request consumes `cost` tokens (proportional to the estimated token count of the request). A requestor that finds insufficient tokens waits until the bucket refills. Because all agents wait on the same bucket, the aggregate request rate is bounded by `r`, which is set to a fraction of the provider's limit to leave headroom.

Without coordination, independent exponential backoff does not solve the problem. It reduces the retry collision probability per retry but does not eliminate it, and it does not prevent the initial burst that causes the first rate limit hit.

---

## 4. Detection

### 4.1 Manual Code Audit

Identify every place in the codebase where an LLM client is instantiated or the API key is accessed:

```bash
# Search for API key references (replace with your actual env var names)
grep -rn "OPENAI_API_KEY\|openai\.api_key\|AsyncOpenAI\|OpenAI(" ./src --include="*.py"

# Search for LLM client instantiation in LangChain
grep -rn "ChatOpenAI\|AzureChatOpenAI\|ChatAnthropic\|ChatGoogleGenerativeAI" \
    ./src --include="*.py"

# Search for independent retry decorators
grep -rn "@retry\|tenacity\|backoff\|RateLimitError" ./src --include="*.py"
```

If LLM clients are instantiated in more than one module without referencing a shared singleton, that is a signal that rate coordination is absent. If retry decorators appear in multiple agent files independently, that is a direct indicator of the uncoordinated retry pattern.

### 4.2 Automated CI/CD

Write an integration test that runs all agents concurrently against a mock LLM server that enforces a rate limit. Assert that no agent receives more than its proportional share of the budget and that high-priority agents are not starved:

```python
# tests/test_rate_coordination.py
import asyncio
import time
from collections import defaultdict
from unittest.mock import AsyncMock, patch

import pytest

from myagents.rate_limiter import SharedRateLimiter
from myagents.agents import (
    PriceFeedAgent,
    AnomalyAgent,
    PortfolioAgent,
    RiskAgent,
    NotificationAgent,
    DigestAgent,
)

CALL_LOG: dict[str, list[float]] = defaultdict(list)

async def mock_llm_call(agent_name: str, tokens: int = 500) -> str:
    CALL_LOG[agent_name].append(time.monotonic())
    return f"Mock response from {agent_name}"


@pytest.mark.asyncio
async def test_no_agent_starves_under_load():
    limiter = SharedRateLimiter(
        requests_per_minute=20,
        tokens_per_minute=40_000,
    )

    agents = [
        PriceFeedAgent(rate_limiter=limiter, priority=1),
        AnomalyAgent(rate_limiter=limiter, priority=3),
        PortfolioAgent(rate_limiter=limiter, priority=5),
        RiskAgent(rate_limiter=limiter, priority=6),
        NotificationAgent(rate_limiter=limiter, priority=4),
        DigestAgent(rate_limiter=limiter, priority=2),
    ]

    # Run all agents for 10 seconds, each trying to call LLM every 0.5s
    async def run_agent(agent, duration: float = 10.0):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            await agent.run_once()
            await asyncio.sleep(0.1)

    await asyncio.gather(*[run_agent(a) for a in agents])

    # Every agent should have completed at least one call
    for agent in agents:
        assert len(CALL_LOG[agent.name]) > 0, (
            f"Agent {agent.name} was completely starved. "
            "Rate limiter priority policy may be incorrect."
        )

    # High-priority agents should have more completions than low-priority ones
    risk_calls = len(CALL_LOG["risk"])
    digest_calls = len(CALL_LOG["digest"])
    assert risk_calls >= digest_calls, (
        f"Risk agent ({risk_calls} calls) should not be outrun by "
        f"Digest agent ({digest_calls} calls)."
    )
```

### 4.3 Runtime Production

Track two counters per agent — `llm_success_total` and `llm_429_total` — and expose them to Prometheus or your APM. Compute the 429 rate as `rate(llm_429_total[5m]) / (rate(llm_success_total[5m]) + rate(llm_429_total[5m]))` and alert when it exceeds 10% over a two-minute window. A nonzero 429 rate on any agent is the primary runtime signal for this pattern.

```python
# In the shared LLM gateway — increment these counters around every call
try:
    response = await openai_client.chat.completions.create(**params)
    metrics[agent_name]["success"] += 1
except openai.RateLimitError:
    metrics[agent_name]["rate_limit_429"] += 1
    raise  # never swallow — let the rate limiter handle retry
```

Monitor total API cost per agent alongside success counts. A disproportionate cost-to-success ratio (many tokens consumed, few successful completions) is a secondary indicator of retry waste caused by pool contention.

---

## 5. Fix

### 5.1 Immediate

Implement a centralized token-bucket rate limiter and route all agent LLM calls through it. This is the minimum viable fix:

```python
# rate_limiter.py
import asyncio
import time
import logging
from dataclasses import dataclass, field

_log = logging.getLogger("rate_limiter")


@dataclass
class SharedRateLimiter:
    """
    Token-bucket rate limiter shared across all agents.
    Thread-safe via asyncio lock.

    requests_per_minute: provider RPM limit (set to 80% of actual limit for headroom)
    tokens_per_minute: provider TPM limit (set to 80% of actual limit)
    """
    requests_per_minute: float
    tokens_per_minute: float

    _request_tokens: float = field(init=False)
    _token_tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self):
        self._request_tokens = self.requests_per_minute
        self._token_tokens = self.tokens_per_minute
        self._last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now

        self._request_tokens = min(
            self.requests_per_minute,
            self._request_tokens + elapsed * (self.requests_per_minute / 60.0),
        )
        self._token_tokens = min(
            self.tokens_per_minute,
            self._token_tokens + elapsed * (self.tokens_per_minute / 60.0),
        )

    async def acquire(self, estimated_tokens: int = 500, agent_name: str = "unknown"):
        """
        Block until the bucket has enough capacity for this request.
        Call this before every LLM API call.
        """
        async with self._lock:
            while True:
                self._refill()
                if self._request_tokens >= 1 and self._token_tokens >= estimated_tokens:
                    self._request_tokens -= 1
                    self._token_tokens -= estimated_tokens
                    return
                # Calculate wait time until enough tokens are available
                wait_requests = max(0, (1 - self._request_tokens) / (self.requests_per_minute / 60.0))
                wait_tokens = max(0, (estimated_tokens - self._token_tokens) / (self.tokens_per_minute / 60.0))
                wait = max(wait_requests, wait_tokens) + 0.05  # small buffer
                _log.debug(
                    "Agent %s waiting %.2fs for rate limit capacity "
                    "(request_tokens=%.1f, token_tokens=%.0f)",
                    agent_name, wait, self._request_tokens, self._token_tokens,
                )
                await asyncio.sleep(wait)
```

Usage in an agent:

```python
# Before every LLM call
await self.rate_limiter.acquire(
    estimated_tokens=len(prompt.split()) * 1.3,  # rough estimate
    agent_name=self.name,
)
response = await self.llm_client.chat(prompt)
```

### 5.2 Robust

Add a **priority queue** so that user-facing agents are served before background agents when the pool is under pressure. Extend `SharedRateLimiter` with a min-heap of pending requests ordered by priority score, a 50 ms dispatcher loop that grants tokens to the highest-priority waiter each tick, and an aging mechanism that gradually increases the effective priority of waiting requests to prevent permanent starvation of low-priority agents:

```python
# priority_rate_limiter.py
import asyncio
import heapq
import time
import logging
from dataclasses import dataclass, field

_log = logging.getLogger("priority_rate_limiter")


@dataclass(order=True)
class _Request:
    priority: float           # lower = higher priority (min-heap)
    tokens: int = field(compare=False)
    agent: str = field(compare=False)
    event: asyncio.Event = field(compare=False, default_factory=asyncio.Event)
    queued: float = field(compare=False, default_factory=time.monotonic)


class PriorityRateLimiter:
    """Token-bucket rate limiter with priority queue and starvation aging."""

    def __init__(self, rpm: float, tpm: float, aging_rate: float = 0.1):
        self.rpm, self.tpm, self.aging_rate = rpm, tpm, aging_rate
        self._req_tokens, self._tok_tokens = rpm, tpm
        self._last = time.monotonic()
        self._heap: list[_Request] = []
        self._lock = asyncio.Lock()

    async def start(self):
        asyncio.create_task(self._loop())

    async def acquire(self, priority: int, tokens: int = 500, agent: str = "?"):
        r = _Request(priority=float(priority), tokens=tokens, agent=agent)
        async with self._lock:
            heapq.heappush(self._heap, r)
        await r.event.wait()

    def _refill(self):
        now, self._last = time.monotonic(), time.monotonic()
        elapsed = now - self._last
        self._req_tokens = min(self.rpm, self._req_tokens + elapsed * self.rpm / 60)
        self._tok_tokens = min(self.tpm, self._tok_tokens + elapsed * self.tpm / 60)

    async def _loop(self):
        while True:
            await asyncio.sleep(0.05)
            async with self._lock:
                self._refill()
                # Apply aging: waiting requests gain priority over time
                for r in self._heap:
                    r.priority = max(0.0, r.priority - (time.monotonic() - r.queued) * self.aging_rate)
                heapq.heapify(self._heap)
                while self._heap:
                    top = self._heap[0]
                    if self._req_tokens >= 1 and self._tok_tokens >= top.tokens:
                        heapq.heappop(self._heap)
                        self._req_tokens -= 1
                        self._tok_tokens -= top.tokens
                        top.event.set()
                    else:
                        break
```

Assign priority levels at system startup (lower number = served first): Risk Agent = 1, Portfolio Agent = 2, Notification Agent = 3, Anomaly Agent = 4, Price Feed Agent = 5, Digest Agent = 6. Pass the same `PriorityRateLimiter` instance to every agent constructor.

---

## 6. Architectural Prevention

**Establish a single LLM gateway module.** No agent should import the LLM client directly. All agents call an `llm_gateway.chat()` function that internally applies the rate limiter. This makes it impossible for a new agent to bypass the coordination layer.

**Set per-agent rate budgets.** Rather than a single shared pool, allocate a fraction of the total budget to each agent category. User-facing agents get a guaranteed minimum; background agents get what remains. This prevents any single agent from monopolizing the pool.

**Use separate API keys per agent category when possible.** Some LLM providers allow multiple API keys under one account, each with its own rate limit. Assign one key to user-facing agents and another to batch/background agents. This creates hard isolation at the provider level.

**Model the budget before deployment.** Given the average token count per LLM call per agent, the expected call frequency, and the API tier's TPM limit, calculate whether the planned concurrency is feasible. This is a simple spreadsheet exercise that can prevent the problem entirely.

**Implement circuit breakers per agent.** Track consecutive 429 errors per agent in the gateway. After a configurable threshold (e.g., 5 consecutive failures), open the circuit for that agent for a fixed recovery window (e.g., 60 seconds) and reject its requests immediately without touching the API. This isolates a misbehaving or over-demanding agent and protects the pool for other agents. After the recovery window, allow a single probe request through; if it succeeds, close the circuit. The `tenacity` library's `stop_after_attempt` combined with a custom `retry_error_callback` can implement this without writing a circuit breaker from scratch.

---

## 7. Anti-patterns to Avoid

**Shared API key without rate coordination.** Each agent instantiates its own LLM client and makes calls without any awareness of what other agents are doing. This is the root cause of the pattern. It feels simple and it is — until concurrency begins.

**Independent retry logic per agent.** Each agent implements its own backoff strategy. Without coordination, all agents back off by approximately the same amount and resume at approximately the same time, re-creating the thundering herd on every retry wave.

**Catching `RateLimitError` and returning a fallback value.** Similar to catching `UnicodeError` and returning an empty string (Pattern #20), this suppresses the signal and produces silent failures. The agent that catches the error appears to succeed; the output is quietly wrong.

**Over-aggressive retries with no jitter.** A fixed retry interval (e.g., `time.sleep(60)`) means all agents retry at exactly 60 seconds, guaranteed to collide. Jitter is necessary but not sufficient without global coordination.

**Ignoring the token-per-minute limit and only tracking request rate.** Large prompts (long documents, full conversation histories) can exhaust the TPM limit with few requests. An agent that sends 3 requests per minute but each request contains 10,000 tokens can saturate a 30,000 TPM limit alone.

**Assuming staging load tests represent production concurrency.** Rate limit bugs are concurrency bugs. Sequential integration tests and light staging traffic do not trigger them.

---

## 8. Edge Cases and Variants

**Burst-then-quiet workloads.** If agents only run concurrently in short bursts (e.g., all agents triggered by an hourly cron), the thundering herd may be intermittent and time-of-day-correlated. The bug appears to "fix itself" between cron runs.

**Provider-side tier upgrades.** Upgrading an API tier increases the rate limit, which may appear to fix the problem without addressing the root cause. As the number of agents grows or traffic increases, the problem re-emerges at higher scale.

**Multiple API keys, same account.** Some providers aggregate usage across all keys on an account for billing and may also aggregate for rate limiting. Verify with the provider whether per-key limits are truly independent.

**Azure OpenAI vs. OpenAI direct.** Azure OpenAI deployments have per-deployment TPM limits, separate from the underlying model's global limit. An agent that uses multiple Azure deployments may need separate rate limiters per deployment.

**Streaming responses.** When agents use streaming mode (`stream=True`), the request occupies a connection for an extended period. Some providers count a streaming request as occupying a request slot until the stream closes. Agents using streaming with long generations can block the pool even with few concurrent requests.

**Embeddings and completions share the same key.** If agents make both chat completion and embedding calls, both consume from the same key's quota. The rate limiter must account for all LLM call types, not only chat completions.

**Exponential backoff with full jitter.** The correct form of independent backoff (when a global limiter is not yet in place as an immediate stopgap) is `sleep(random.uniform(0, min(cap, base * 2**attempt)))`. This does not solve thundering herd but reduces collision probability significantly.

---

## 9. Audit Checklist

Use this checklist when reviewing any multi-agent system that shares an LLM API key:

- [ ] All LLM calls route through a single gateway function or class that applies a shared rate limiter.
- [ ] No agent module instantiates an LLM client directly without going through the gateway.
- [ ] The rate limiter uses a token-bucket algorithm with both RPM and TPM buckets.
- [ ] The rate limiter's budget is set to 80% or less of the provider's stated limit to leave headroom.
- [ ] A priority policy is defined: user-facing agents have higher priority than background agents.
- [ ] An aging mechanism prevents low-priority agents from starving indefinitely.
- [ ] No agent has its own independent retry decorator that catches `RateLimitError` — retry logic lives in the gateway.
- [ ] A circuit breaker is in place per agent to isolate misbehaving agents.
- [ ] Production monitoring includes a per-agent 429 rate metric with an alert threshold.
- [ ] Integration tests exercise all agents concurrently against a rate-limited mock.
- [ ] The peak token budget (requests * average tokens per request * number of agents) is less than the TPM limit.
- [ ] Separate API keys are considered for user-facing vs. batch agent categories.
- [ ] Streaming agents are accounted for in connection-limit calculations if the provider enforces them.
- [ ] The architecture decision record documents the LLM budget allocation policy.

---

## 10. Further Reading

- Brendan Burns, "Designing Distributed Systems" (O'Reilly, 2018) — Chapter on rate limiting and token buckets
- OpenAI rate limits documentation: [https://platform.openai.com/docs/guides/rate-limits](https://platform.openai.com/docs/guides/rate-limits)
- OpenAI cookbook — How to avoid rate limits: [https://cookbook.openai.com/examples/how_to_handle_rate_limits](https://cookbook.openai.com/examples/how_to_handle_rate_limits)
- AWS Architecture Blog — Implementing token bucket rate limiting: [https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
- "Exponential Backoff and Jitter" (AWS blog): [https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)
- Prometheus alerting best practices: [https://prometheus.io/docs/practices/alerting/](https://prometheus.io/docs/practices/alerting/)

**Related patterns in this playbook:**
- [Pattern #20 — Missing File Encoding](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/09-multi-agent-governance/pattern-20-missing-file-encoding.md)
- [Pattern #22 — Agent Context Window Overflow](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/09-multi-agent-governance/pattern-22-context-window-overflow.md)
- [Pattern #07 — Missing Timeout on External Calls](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/07-detection-monitoring/pattern-07-missing-timeout-external-calls.md)
- [Pattern #14 — Uncoordinated Shared Resource Access](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/blob/main/playbook/v1/01-categories/09-multi-agent-governance/pattern-14-uncoordinated-shared-resource.md)
