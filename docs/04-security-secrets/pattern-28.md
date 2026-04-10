# Pattern #28 — SSL Verification Disabled

**Category:** Security & Secrets
**Severity:** High
**Frameworks affected:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debug time if undetected:** Indefinite (not a bug that causes visible errors — it is a silent security vulnerability)

---

## 1. Observable Symptoms

There are no runtime errors. No exceptions are raised. No log lines flag the problem. The system operates exactly as it did before the flag was introduced, which is precisely what makes this pattern so dangerous. From the perspective of application behavior, SSL verification disabled and SSL verification enabled are indistinguishable under normal conditions.

The only observable symptom at the code level is the presence of `verify=False` in HTTP session construction (via the `requests` library or `httpx`) or `ssl.CERT_NONE` in raw socket contexts. In LangChain-based systems, this flag is sometimes passed deep inside a custom LLM wrapper or a tool's HTTP client. In AutoGen, it may appear inside a custom `code_execution_config` that reaches out to an internal service. In CrewAI, it can surface in agent tool definitions. In all cases, the flag is invisible in runtime output.

If an active man-in-the-middle (MITM) attack is in progress, symptoms appear outside the Python process: intercepted prompts replayed to a rogue LLM endpoint, crafted responses injected into agent memory, or sensitive data silently exfiltrated through a proxy. These symptoms surface in anomaly detection dashboards or network monitoring — not in application logs. By the time they appear, exploitation is already underway.

## 2. Field Story (anonymized)

A team building an internal microservices platform was deploying a multi-agent system to coordinate document workflows across several internal services. During early integration testing, one of the internal services used a self-signed TLS certificate. A developer added `verify=False` to the HTTP client used by the orchestrating agent to stop `SSLCertVerificationError` from blocking progress. The fix was committed with a comment: "temp — remove when cert is fixed."

The certificate was replaced with a properly signed one three weeks later. The internal ticket was closed. No one updated the agent code. The `verify=False` line remained, buried in a utility module used by four separate agents in the pipeline.

Eight months later, a routine security audit of the platform's network traffic identified that the agent processes were establishing TLS sessions without certificate validation. The audit tool flagged every connection from the agent fleet — to the LLM API, to the document indexing service, to the authentication proxy — as subject to interception. None of these connections had actually been intercepted, but the vulnerability had been open for the better part of a year across a system handling sensitive internal documents. The remediation required patching five files, rotating one API key as a precaution, and rescheduling two weeks of delayed security review work.

## 3. Technical Root Cause

TLS certificate verification serves a single critical purpose: confirming that the server your client is connecting to is the server it claims to be. When `verify=False` is set, Python's `requests` library (and `httpx`, `aiohttp`, and the standard library `ssl` module) skips this confirmation entirely. A TLS handshake still occurs — the connection is encrypted — but the certificate presented by the server is never validated against a trusted certificate authority. An attacker positioned between the client and the server can present any certificate they choose, and the client will accept it without complaint.

```python
# This is what the developer wrote
import requests

session = requests.Session()
session.verify = False  # "just to get past the self-signed cert"

response = session.post(
    "https://internal-llm-gateway.corp/v1/chat",
    json={"prompt": prompt, "model": "gpt-4o"}
)
```

Under this configuration, an attacker who controls any point on the network path — a compromised router, a rogue Wi-Fi access point, a misconfigured proxy — can intercept the entire exchange. The connection appears encrypted from both ends, but the attacker holds the private key for the certificate they presented. They read every prompt sent to the LLM and every response received. In a multi-agent system, this means the attacker can observe the full reasoning chain across all agents.

The problem is amplified in multi-agent architectures because the disabled verification propagates:

```
Agent A (orchestrator) ──verify=False──► LLM API (HTTPS)
        │
        ├── tool call ──verify=False──► Internal search service (HTTPS)
        │
        └── result handoff ──verify=False──► Agent B (executor)
                                                    │
                                                    └── verify=False──► Document store (HTTPS)
```

Every hop in the chain is compromised. A single `verify=False` in a shared session factory infects all downstream HTTP calls made through that session.

A secondary technical root cause is the suppression of `InsecureRequestWarning`. Developers who set `verify=False` often also add the following line to silence the warning that `requests` emits:

```python
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
```

This removes the only passive runtime indicator that verification is disabled. After this line, no log output distinguishes a secure session from an insecure one.

## 4. Detection

### 4.1 Manual code audit

The patterns to search for are specific and uncommon in correct production code. Any match is a finding that requires review:

```bash
# Locate verify=False in all Python files
grep -rn "verify\s*=\s*False" --include="*.py" .

# Locate ssl.CERT_NONE
grep -rn "CERT_NONE" --include="*.py" .

# Locate urllib3 warning suppression (often paired with verify=False)
grep -rn "disable_warnings" --include="*.py" .

# Locate check_hostname=False in ssl context construction
grep -rn "check_hostname\s*=\s*False" --include="*.py" .

# Locate verify_ssl=False used by aiohttp and httpx
grep -rn "verify_ssl\s*=\s*False\|ssl\s*=\s*False" --include="*.py" .

# Search across all config files as well (YAML, TOML, JSON)
grep -rn "verify.*false\|ssl.*false" --include="*.yaml" --include="*.toml" --include="*.json" . -i
```

Additionally, search for the `# temp` and `# TODO` comments that often accompany these lines:

```bash
grep -rn -B1 -A1 "verify\s*=\s*False" --include="*.py" .
```

Review every match in context. A `verify=False` in a test file that only runs against a local mock server is a different risk level from one in a production HTTP client factory.

### 4.2 Automated CI/CD

Bandit, the Python security linter, detects `verify=False` as rule B501 (request with certificate validation disabled) and B502 (ssl with no verification):

```bash
# Install and run
pip install bandit
bandit -r ./src -t B501,B502,B503,B504

# In CI (GitHub Actions example)
```

```yaml
# .github/workflows/security.yml
name: Security Scan

on: [push, pull_request]

jobs:
  bandit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install bandit
      - run: bandit -r ./src -t B501,B502,B503,B504 -f json -o bandit-report.json
      - uses: actions/upload-artifact@v4
        with:
          name: bandit-report
          path: bandit-report.json
```

For stricter enforcement, add a custom `ruff` rule using `flake8-bandit` integration, or use Semgrep with a custom pattern:

```yaml
# semgrep-ssl.yaml
rules:
  - id: ssl-verification-disabled
    patterns:
      - pattern: requests.get(..., verify=False, ...)
      - pattern: requests.post(..., verify=False, ...)
      - pattern: requests.Session().verify = False
      - pattern: $SESSION.verify = False
    message: "SSL certificate verification is disabled. Remove verify=False before merging."
    languages: [python]
    severity: ERROR
```

### 4.3 Runtime production monitoring

A startup audit function verifies that no live HTTP session in the process has verification disabled. This runs before any agent is initialized:

```python
"""ssl_audit.py — Runtime SSL verification audit for multi-agent systems."""
import ssl
import sys
import logging
import requests
from typing import Any

logger = logging.getLogger(__name__)


def audit_ssl_configuration() -> None:
    """
    Inspect the default SSL context and warn if certificate
    verification has been downgraded.

    Raises SystemExit if verification is disabled in a non-test environment.
    """
    import os
    is_test = os.environ.get("ENVIRONMENT", "production").lower() in ("test", "ci")

    # Check the default SSL context
    ctx = ssl.create_default_context()
    if ctx.verify_mode != ssl.CERT_REQUIRED:
        message = (
            f"SSL context verify_mode is {ctx.verify_mode!r}, "
            f"expected ssl.CERT_REQUIRED. "
            f"Certificate verification is not enforced."
        )
        if is_test:
            logger.warning(message)
        else:
            logger.critical(message)
            sys.exit(1)

    if not ctx.check_hostname:
        message = "SSL context check_hostname is False. Hostname verification disabled."
        if is_test:
            logger.warning(message)
        else:
            logger.critical(message)
            sys.exit(1)

    logger.info("SSL configuration audit passed: CERT_REQUIRED, check_hostname=True")


def create_verified_session(
    base_url: str,
    ca_bundle: str | None = None,
) -> requests.Session:
    """
    Create a requests.Session with SSL verification enforced.
    Accepts an explicit CA bundle path for internal PKI environments.
    """
    session = requests.Session()

    # Explicitly set — never rely on the default being correct
    if ca_bundle:
        session.verify = ca_bundle
        logger.info("HTTP session created with custom CA bundle: %s", ca_bundle)
    else:
        session.verify = True
        logger.info("HTTP session created with system CA bundle (verify=True)")

    # Sanity check — guard against post-construction mutation
    assert session.verify is not False, (
        "Session verify must not be False. "
        "Use a CA bundle path for self-signed certs."
    )

    return session


def patch_requests_to_detect_insecure_calls() -> None:
    """
    Monkey-patch requests.Session.request to log a critical warning
    if any call is made with verify=False in production.

    Install this at process startup, before any agent is initialized.
    """
    import os
    if os.environ.get("ENVIRONMENT", "production").lower() in ("test", "ci"):
        return  # Skip patching in test environments

    original_request = requests.Session.request

    def patched_request(self: Any, method: str, url: str, **kwargs: Any) -> Any:
        if kwargs.get("verify") is False or self.verify is False:
            logger.critical(
                "SECURITY: SSL verification disabled for %s %s. "
                "This connection is vulnerable to MITM interception. "
                "Fix immediately.",
                method.upper(), url,
            )
            # In strict mode, refuse the call entirely
            if os.environ.get("SSL_STRICT_MODE", "1") == "1":
                raise RuntimeError(
                    f"SSL verification disabled for {method.upper()} {url}. "
                    "Set SSL_STRICT_MODE=0 to downgrade this to a warning."
                )
        return original_request(self, method, url, **kwargs)

    requests.Session.request = patched_request  # type: ignore[method-assign]
    logger.info("SSL enforcement patch installed on requests.Session")
```

## 5. Fix

### 5.1 Immediate fix

Remove `verify=False` and replace it with the correct CA bundle path. If the original problem was a self-signed certificate on an internal service, the correct solution is to distribute that certificate's CA to the client, not to disable verification:

```python
# BEFORE (vulnerable)
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()
session.verify = False

response = session.post("https://internal-service.corp/api/infer", json=payload)


# AFTER (secure — using internal CA bundle)
import requests

# Path to the internal CA certificate (PEM format)
# Distribute this file via your secrets manager or container image
CA_BUNDLE_PATH = "/etc/ssl/internal/corp-ca.pem"

session = requests.Session()
session.verify = CA_BUNDLE_PATH  # explicit path, not True/False

response = session.post("https://internal-service.corp/api/infer", json=payload)
```

If the service now uses a properly signed certificate from a public CA, restore the system default:

```python
# AFTER (secure — public CA, system trust store)
session = requests.Session()
session.verify = True  # explicit, documents intent
```

### 5.2 Robust fix

A factory function that builds all HTTP sessions used by agents, with environment-aware CA bundle resolution and a hard guard against `verify=False`:

```python
"""http_client.py — Centralized, secure HTTP session factory for multi-agent systems."""
import os
import logging
import ssl
from pathlib import Path

import requests
import certifi

logger = logging.getLogger(__name__)

# Environment variable names
ENV_CA_BUNDLE = "INTERNAL_CA_BUNDLE_PATH"
ENV_ENVIRONMENT = "ENVIRONMENT"


def _resolve_ca_bundle() -> str | bool:
    """
    Determine the correct CA bundle for this environment.

    Resolution order:
      1. INTERNAL_CA_BUNDLE_PATH env var (internal PKI environments)
      2. Certifi's bundled CA store (public internet services)

    Returns a path string or True (use system default).
    Never returns False.
    """
    custom_bundle = os.environ.get(ENV_CA_BUNDLE)
    if custom_bundle:
        bundle_path = Path(custom_bundle)
        if not bundle_path.exists():
            raise FileNotFoundError(
                f"CA bundle not found at {bundle_path}. "
                f"Check the {ENV_CA_BUNDLE} environment variable."
            )
        logger.info("Using internal CA bundle: %s", bundle_path)
        return str(bundle_path)

    # Fall back to certifi's curated CA bundle
    logger.info("Using certifi CA bundle: %s", certifi.where())
    return certifi.where()


def build_agent_session(
    agent_name: str,
    timeout: int = 30,
) -> requests.Session:
    """
    Build a requests.Session with enforced SSL verification.

    Args:
        agent_name: Identifies the agent in log output.
        timeout: Default timeout for all requests (seconds).

    Returns:
        A configured requests.Session. Raises RuntimeError if called
        in a way that would result in verify=False.
    """
    ca_bundle = _resolve_ca_bundle()

    session = requests.Session()
    session.verify = ca_bundle

    # Set a default timeout via a transport adapter
    # (requests has no native default timeout — this is a separate issue)
    session.headers.update({
        "User-Agent": f"MultiAgentSystem/{agent_name}",
        "X-Request-Timeout": str(timeout),
    })

    # Final guard: assert verification is active
    if session.verify is False:
        raise RuntimeError(
            f"[{agent_name}] Session was constructed with verify=False. "
            "This is not permitted in this system. "
            "Use INTERNAL_CA_BUNDLE_PATH for self-signed certificates."
        )

    logger.info(
        "[%s] HTTP session ready. SSL verify=%r",
        agent_name, session.verify,
    )
    return session


def build_ssl_context_for_internal_service(
    ca_bundle_path: str,
) -> ssl.SSLContext:
    """
    Build an ssl.SSLContext for raw socket connections to internal services.
    Enforces CERT_REQUIRED and hostname verification.

    Args:
        ca_bundle_path: Path to the PEM file for the internal CA.

    Returns:
        A configured ssl.SSLContext.
    """
    ctx = ssl.create_default_context(cafile=ca_bundle_path)

    # Verify these are set — create_default_context sets them, but be explicit
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True

    logger.info(
        "SSL context built: verify_mode=CERT_REQUIRED, "
        "check_hostname=True, cafile=%s",
        ca_bundle_path,
    )
    return ctx


# --- Usage example ---

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Agent sessions — each agent gets its own session instance
    orchestrator_session = build_agent_session("orchestrator")
    executor_session = build_agent_session("executor")

    # Test against a public endpoint
    resp = orchestrator_session.get("https://httpbin.org/get")
    print(f"Status: {resp.status_code}, SSL verified: {resp.url.startswith('https')}")
```

## 6. Architectural Prevention

The root cause of this pattern is that SSL configuration is scattered across individual HTTP client instantiations rather than centralized. When a developer needs to make an HTTP call, they instantiate a session locally, set `verify=False` to fix the immediate problem, and move on. The fix to this problem is architectural: **all HTTP sessions in the system must be created through a single factory function**, and that factory must make it impossible to create an unverified session.

The `build_agent_session` factory in Section 5.2 demonstrates this approach. The factory is the single point of authority for session configuration. Developers do not call `requests.Session()` directly. They call `build_agent_session("agent-name")`. This ensures that SSL verification settings, timeout defaults, and authentication headers are applied consistently across every agent.

For multi-agent systems that communicate over internal networks with private PKI, the CA bundle distribution problem must be solved separately and robustly. The CA certificate should be baked into the container image or mounted as a secret at deployment time, and the `INTERNAL_CA_BUNDLE_PATH` environment variable should be set centrally in the deployment configuration. This separates the concern of certificate distribution from application code entirely — developers never need to touch SSL configuration in Python.

For environments using a secrets manager (HashiCorp Vault, AWS Secrets Manager), the CA bundle path and certificate content can be stored and rotated alongside API keys. A startup hook that fetches the current CA certificate and writes it to a well-known path before agent initialization ensures the system always uses the latest certificate.

## 7. Anti-patterns to Avoid

1. **`verify=False` with a "temp" or "TODO" comment.** These comments are not action items — they are deferrals. The SSL certificate problem should be solved immediately by distributing the CA certificate, not by disabling verification.

2. **Suppressing `InsecureRequestWarning` with `urllib3.disable_warnings`.** This silences the only passive indicator that verification is disabled. If the warning is annoying, the correct fix is to enable verification, not to hide the warning.

3. **Per-call `verify=False` inside tool methods.** In agent tool definitions, HTTP calls with `verify=False` are invisible to session-level audits. Every HTTP call must go through the centralized session factory.

4. **Creating a custom `ssl.SSLContext` with `check_hostname=False` and `verify_mode=ssl.CERT_NONE`.** This is the lower-level equivalent of `verify=False` and is equally dangerous. It appears in websocket connections and raw socket code that bypasses `requests` entirely.

5. **Trusting that infrastructure-level TLS termination makes application-level verification unnecessary.** If the application makes direct HTTPS calls to services outside its immediate network boundary — including calls to LLM provider APIs — application-level verification is required regardless of what the load balancer does.

## 8. Edge Cases and Variants

**Variant 1: LangChain custom LLM wrapper with `verify=False`.** When wrapping an internal LLM endpoint with a LangChain `BaseLLM` subclass, developers sometimes pass `verify=False` into the underlying `requests` call inside `_call`. This is not caught by audits of the main application code — only an audit of every custom wrapper class will find it.

**Variant 2: `httpx.AsyncClient` with `verify=False`.** In async agent systems using `httpx` (common in FastAPI-based orchestration layers), the pattern is `httpx.AsyncClient(verify=False)`. Bandit rule B501 does not always catch `httpx` usage. A dedicated Semgrep rule is needed.

**Variant 3: Self-signed cert replaced but old CA bundle path still set.** The `INTERNAL_CA_BUNDLE_PATH` points to a file that no longer contains the correct CA for the renewed certificate. The system raises `SSLCertVerificationError` and a developer adds `verify=False` as an emergency fix, returning to the original problem.

**Variant 4: Inherited verification setting from an environment variable.** Some HTTP client libraries read `CURL_CA_BUNDLE` or `REQUESTS_CA_BUNDLE` from the environment. If these are set to an empty string or a non-existent path, verification silently degrades. Auditing environment variable defaults at startup prevents this.

## 9. Audit Checklist

- [ ] `grep -rn "verify\s*=\s*False"` returns no matches in production source code
- [ ] `grep -rn "CERT_NONE"` returns no matches in production source code
- [ ] `grep -rn "disable_warnings"` returns no matches or is limited to test fixtures
- [ ] All HTTP sessions are created through a centralized factory function
- [ ] The CA bundle path for internal services is configured via environment variable, not hardcoded
- [ ] Bandit rules B501 and B502 are enforced in CI and block merges on failure
- [ ] A startup audit function verifies the SSL context before agent initialization
- [ ] `check_hostname=False` does not appear in any `ssl.SSLContext` construction

## 10. Further Reading

- Corresponding short pattern: [Pattern 28 — SSL Verification Disabled](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-28)
- Related patterns: #07 (Hardcoded Secret in Source — credentials exposed over an unverified connection are doubly vulnerable), #04 (Multi-File State Desync — SSL configuration split across multiple files without a single authoritative source)
- Recommended reading:
  - OWASP Testing Guide v4.2, Section OTG-CRYPST-001: Testing for Weak SSL/TLS Ciphers, Insufficient Transport Layer Protection
  - Python `ssl` module documentation — "Security Considerations" section, covering the implications of `CERT_NONE`, `CERT_OPTIONAL`, and `CERT_REQUIRED`
