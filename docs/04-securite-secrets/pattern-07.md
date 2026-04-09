# Pattern #07 — Hardcoded Secret in Source

**Category:** Security & Secrets
**Severity:** Critical
**Frameworks affected:** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Average debug time if undetected:** 0 days to indefinite (the leak is not a "bug" to debug — it is invisible until exploited, which can happen in minutes or take years)

---

## 1. Observable Symptoms

A security scanner (truffleHog, gitleaks, GitHub Secret Scanning) raises alerts for plaintext tokens or API keys in the source code. Or worse: the LLM provider bill explodes without explanation — someone retrieved the key exposed in the git history and is using it for their own API calls.

In multi-agent systems, the most common symptom is the **proliferation of credentials**. Each agent has its own connections: LLM provider, database, notification service, external API. A 5-agent system may have 15–20 distinct credentials, each potentially hardcoded in a different file.

Another symptom: unauthorized messages or actions appear in the system's notification channels. An exposed bot token allows anyone to send messages through the bot — the messages appear to come from the system when they actually originate from an attacker.

## 2. Field Story (anonymized)

A developer hardcoded their notification bot token in a Python script for "quick testing." The script was committed to a private repository shared with 3 collaborators. Six months later, the repository was made public to share an open-source component with the community. No one thought to check the git history.

Within 24 hours, a bot scanner (hundreds of them continuously monitor public GitHub pushes in real time) detected the token. The attacker used the bot to post messages in the team's notification channel: phishing links disguised as system alerts. A team member clicked one, compromising a second credential.

Total cost of the incident: revocation of 4 credentials, 2 days of work to audit the git history, and a loss of confidence in the system's security that led to a complete overhaul of secret management.

## 3. Technical Root Cause

The pattern is almost always the same: a developer hardcodes a secret "temporarily" during development and forgets to remove it before committing:

```python
# "I'll move it to .env later" — a message from 6 months ago
API_KEY = "sk-proj-abc123def456ghi789jkl012mno345"
BOT_TOKEN = "8679765924:AAF3d5__KxB2nM7qR9zWpLkJh"
DB_PASSWORD = "SuperSecret123!"
```

The problem is compounded by three factors in multi-agent systems:

**1. Number of credentials.** A typical multi-agent system connects 3–5 external services. Each agent may have its own set of credentials. The total number of secrets to manage grows linearly with the number of agents.

**2. Code distributed across multiple files.** Credentials sometimes live in the main file (`main.py`), sometimes in configuration files (`config.py`), sometimes in utility scripts (`send_alert.py`). An audit must cover the entire codebase, not just one file.

**3. Git history retains everything.** Removing a secret from the current code is NOT enough. The secret persists in the git history indefinitely. `git log -p | grep "sk-"` will find it instantly. The only solution is to **revoke** the secret and generate a new one.

## 4. Detection

### 4.1 Manual code audit

Scan the codebase and history for common secret patterns:

```bash
# Search for API key patterns
grep -rn "sk-\|Bearer \|token.*=.*['\"][a-zA-Z0-9]" --include="*.py"

# Search for hardcoded passwords
grep -rn "password\s*=\s*['\"]" --include="*.py" -i

# Scan the full git history
git log -p --all | grep -i "api_key\|token\|password\|secret" | head -20

# Verify that .env has not been committed
git ls-files | grep -i "\.env$"
```

### 4.2 Automated CI/CD

Install a secret scanner as a pre-commit hook AND in CI:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks:
      - id: gitleaks

# Alternative: detect-secrets (Yelp)
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
```

`gitleaks` configuration for a multi-agent project:

```toml
# .gitleaks.toml
title = "Multi-Agent System Secret Scanner"

[[rules]]
id = "llm-api-key"
description = "LLM API Key"
regex = '''(?i)(sk-[a-zA-Z0-9]{20,}|gsk_[a-zA-Z0-9]{20,}|AIza[a-zA-Z0-9_-]{35})'''
tags = ["key", "llm"]

[[rules]]
id = "bot-token"
description = "Bot Token (Telegram, Slack, Discord)"
regex = '''\d{8,12}:[A-Za-z0-9_-]{30,}'''
tags = ["token", "bot"]
```

### 4.3 Runtime production

Verify at startup that secrets are loaded from the environment, not from code:

```python
import os
import sys

REQUIRED_SECRETS = [
    "LLM_API_KEY",
    "NOTIFICATION_BOT_TOKEN",
    "DATABASE_URL",
]

def validate_secrets_at_startup():
    """Fail fast if secrets are not set in environment."""
    missing = []
    for secret in REQUIRED_SECRETS:
        value = os.environ.get(secret)
        if not value:
            missing.append(secret)
        elif value.startswith("YOUR_") or value == "changeme":
            missing.append(f"{secret} (placeholder detected)")

    if missing:
        print(f"FATAL: Missing secrets: {missing}", file=sys.stderr)
        print("Set them in .env or as environment variables.", file=sys.stderr)
        sys.exit(1)
```

## 5. Fix

### 5.1 Immediate fix

1. Revoke the compromised secret immediately (regenerate the key with the provider)
2. Move the secret into `.env`
3. Add `.env` to `.gitignore`

```python
# BEFORE (bug)
API_KEY = "sk-proj-abc123def456"

# AFTER (fix)
import os
API_KEY = os.environ["LLM_API_KEY"]
```

```bash
# .env (NEVER committed)
LLM_API_KEY=sk-proj-abc123def456

# .gitignore
.env
.env.*
```

### 5.2 Robust fix

Use a centralized secret management module with validation:

```python
"""secrets_manager.py — Centralized secret loading with validation."""
import os
import sys
from pathlib import Path

class Secrets:
    """Load and validate all secrets from environment."""

    def __init__(self):
        self._load_dotenv()
        self._validate()

    def _load_dotenv(self):
        """Load .env file if it exists (development mode)."""
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    def _validate(self):
        """Ensure all required secrets are present."""
        required = ["LLM_API_KEY", "NOTIFICATION_TOKEN", "DATABASE_URL"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            print(f"Missing secrets: {missing}. See .env.example.", file=sys.stderr)
            sys.exit(1)

    @property
    def llm_key(self) -> str:
        return os.environ["LLM_API_KEY"]

    @property
    def notification_token(self) -> str:
        return os.environ["NOTIFICATION_TOKEN"]

# Singleton — import from anywhere
secrets = Secrets()
```

## 6. Architectural Prevention

Prevention relies on **defense in depth** — multiple layers that make a leak progressively harder:

**Layer 1: .env + .gitignore.** Secrets live in `.env`, excluded from git. A `.env.example` with placeholders serves as documentation.

**Layer 2: Pre-commit hook.** gitleaks or detect-secrets blocks any commit containing a secret pattern. The developer cannot commit one "by accident."

**Layer 3: CI scan.** Even if the pre-commit hook is bypassed (`push --no-verify`), CI scans every PR and blocks the merge if a secret is detected.

**Layer 4: Rotation.** Every secret has an expiry date. A cron job alerts if a secret has not been rotated in more than 90 days.

**Layer 5: Least privilege.** Each agent has its own key with minimal permissions. If one agent is compromised, only its permissions are exposed.

## 7. Anti-patterns to Avoid

1. **"I'll move it to .env later."** "Later" never comes. Put it in `.env` immediately, even during prototyping.

2. **Removing the secret from the code and considering the problem solved.** Git history retains everything. If the secret was ever committed, it must be revoked.

3. **Using the same secret for all agents.** If the key is compromised, the entire system is exposed. One secret per agent.

4. **Storing secrets in a committed JSON file.** A `config.json` containing API keys, even in a private repository, is a leak waiting to happen.

5. **Logging secrets.** `print(f"Using API key: {API_KEY}")` in logs exposes the secret to anyone with log access.

## 8. Edge Cases and Variants

**Variant 1: Secrets in CI environment variables.** When properly configured, these are secure. However, if a CI step runs `env | sort` or `printenv` in a public log, the secrets are exposed.

**Variant 2: Secrets in Docker files.** A `COPY .env .` instruction in a Dockerfile bundles the file into the image. Anyone with access to the image can extract secrets with `docker inspect`.

**Variant 3: Secrets in Jupyter notebooks.** Notebook cells frequently contain API keys for demos. Notebooks are committed with their outputs — including the keys.

**Variant 4: Secrets in plaintext debug logs.** Code does `logger.debug(f"Request headers: {headers}")`. The headers contain `Authorization: Bearer sk-...`. The secret ends up in the log file.

## 9. Audit Checklist

- [ ] No secret appears in source code (verified by grep + gitleaks)
- [ ] `.env` is in `.gitignore` and has never been committed (verified by `git log`)
- [ ] A `.env.example` with placeholders exists and is up to date
- [ ] A pre-commit hook blocks commits containing secrets
- [ ] Every secret has been generated or rotated within the last 90 days
- [ ] Each agent has its own credentials (no key sharing)

## 10. Further Reading

- Corresponding short pattern: [Pattern 07 — Hardcoded Secret in Source](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-07)
- Related patterns: #04 (Multi-File State Desync — secrets can live across multiple unsynchronized files), #11 (Race Condition on Shared File — a `.env` file shared between agents can be subject to race conditions)
- Recommended reading:
  - "OWASP Top 10" (2021) — A07:2021 Identification and Authentication Failures
  - GitHub documentation on Secret Scanning — automatic detection of secrets in public and private repositories
