# Pattern N°07 — Hardcoded Secret in Source

**Categorie :** Securite & Secrets
**Severite :** Critical
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 0 jours a indefini (la fuite n'est pas un "bug" a debugger — elle est invisible jusqu'a exploitation, qui peut prendre des minutes ou des annees)

---

## 1. Symptome observable

Un scan de securite (truffleHog, gitleaks, GitHub Secret Scanning) remonte des alertes pour des tokens ou cles API en clair dans le code source. Ou pire : la facture du provider LLM explose sans explication — quelqu'un a recupere la cle exposee dans l'historique git et l'utilise pour ses propres appels.

Dans les systemes multi-agents, le symptome le plus courant est la **multiplication des credentials**. Chaque agent a ses propres connexions : LLM provider, base de donnees, service de notification, API externe. Un systeme a 5 agents peut avoir 15-20 credentials differents, chacun potentiellement hardcode dans un fichier different.

Un autre symptome : des messages ou actions non autorisees apparaissent dans les canaux de notification du systeme. Un token de bot expose permet a quiconque d'envoyer des messages via le bot — les messages apparaissent comme venant du systeme alors qu'ils viennent d'un attaquant.

## 2. Histoire vecue (anonymisee)

Un developpeur a hardcode le token de son bot de notification dans un script Python pour "tester rapidement". Le script a ete committe dans un repo prive partage avec 3 collaborateurs. Six mois plus tard, le repo a ete rendu public pour partager un composant open-source avec la communaute. Personne n'a pense a verifier l'historique git.

En 24 heures, un bot scanner (il en existe des centaines qui scrutent les push publics GitHub en temps reel) a detecte le token. L'attaquant a utilise le bot pour envoyer des messages dans le canal de notification de l'equipe : des liens de phishing deguises en alertes systeme. Un membre de l'equipe a clique, compromettant un second credential.

Le cout total de l'incident : revocation de 4 credentials, 2 jours de travail pour auditer l'historique git, et une perte de confiance dans la securite du systeme qui a conduit a une refonte complete de la gestion des secrets.

## 3. Cause racine technique

Le pattern est presque toujours le meme : un dev hardcode un secret "temporairement" pendant le developpement, et oublie de le retirer avant le commit :

```python
# "Je le mettrai dans .env plus tard" — message d'il y a 6 mois
API_KEY = "sk-proj-abc123def456ghi789jkl012mno345"
BOT_TOKEN = "8679765924:AAF3d5__KxB2nM7qR9zWpLkJh"
DB_PASSWORD = "SuperSecret123!"
```

Le probleme est aggrave par trois facteurs dans les systemes multi-agents :

**1. Nombre de credentials.** Un systeme multi-agents typique connecte 3-5 services externes. Chaque agent peut avoir son propre set de credentials. Le nombre total de secrets a gerer croit lineairement avec le nombre d'agents.

**2. Code distribue entre plusieurs fichiers.** Les credentials sont parfois dans le fichier principal (`main.py`), parfois dans des fichiers de configuration (`config.py`), parfois dans des scripts utilitaires (`send_alert.py`). Un audit doit couvrir toute la codebase, pas juste un fichier.

**3. L'historique git conserve tout.** Supprimer un secret du code actuel ne suffit PAS. Le secret reste dans l'historique git indefiniment. `git log -p | grep "sk-"` le retrouvera instantanement. La seule solution est de **revoquer** le secret et d'en generer un nouveau.

## 4. Detection

### 4.1 Detection manuelle (audit code)

Scanner la codebase et l'historique pour les patterns courants de secrets :

```bash
# Chercher les patterns de cles API
grep -rn "sk-\|Bearer \|token.*=.*['\"][a-zA-Z0-9]" --include="*.py"

# Chercher les passwords hardcodes
grep -rn "password\s*=\s*['\"]" --include="*.py" -i

# Scanner l'historique git complet
git log -p --all | grep -i "api_key\|token\|password\|secret" | head -20

# Verifier que .env n'est pas committe
git ls-files | grep -i "\.env$"
```

### 4.2 Detection automatisee (CI/CD)

Installer un scanner de secrets comme pre-commit hook ET dans la CI :

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

Configuration `gitleaks` pour un projet multi-agents :

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

### 4.3 Detection runtime (production)

Verifier au demarrage que les secrets sont charges depuis l'environnement, pas depuis le code :

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

## 5. Correction

### 5.1 Fix immediat

1. Revoquer le secret compromis immediatement (regenerer la cle chez le provider)
2. Deplacer le secret dans `.env`
3. Ajouter `.env` au `.gitignore`

```python
# AVANT (bug)
API_KEY = "sk-proj-abc123def456"

# APRES (fix)
import os
API_KEY = os.environ["LLM_API_KEY"]
```

```bash
# .env (JAMAIS committe)
LLM_API_KEY=sk-proj-abc123def456

# .gitignore
.env
.env.*
```

### 5.2 Fix robuste

Utiliser un module centralise de gestion des secrets avec validation :

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

## 6. Prevention architecturale

La prevention repose sur la **defense en profondeur** — plusieurs couches qui rendent la fuite progressivement plus difficile :

**Couche 1 : .env + .gitignore.** Les secrets vivent dans `.env`, exclus du git. Un `.env.example` avec des placeholders sert de documentation.

**Couche 2 : Pre-commit hook.** gitleaks ou detect-secrets bloque tout commit contenant un pattern de secret. Le dev ne peut pas committer "par accident".

**Couche 3 : CI scan.** Meme si le pre-commit est contourne (push --no-verify), la CI scanne chaque PR et bloque le merge si un secret est detecte.

**Couche 4 : Rotation.** Chaque secret a une date d'expiration. Un cron job alerte si un secret n'a pas ete rotationnel depuis > 90 jours.

**Couche 5 : Moindre privilege.** Chaque agent a sa propre cle avec des permissions minimales. Si un agent est compromis, seules ses permissions sont exposees.

## 7. Anti-patterns a eviter

1. **"Je le mettrai dans .env plus tard."** Le "plus tard" n'arrive jamais. Mettre dans .env immediatement, meme en phase de prototypage.

2. **Supprimer le secret du code et considerer le probleme resolu.** L'historique git conserve tout. Si le secret a ete committe, il faut le revoquer.

3. **Utiliser le meme secret pour tous les agents.** Si la cle est compromise, tout le systeme est expose. Un secret par agent.

4. **Stocker les secrets dans un fichier JSON committe.** `config.json` avec les cles API, meme dans un repo prive, est une fuite en attente.

5. **Logger les secrets.** `print(f"Using API key: {API_KEY}")` dans les logs expose le secret a quiconque a acces aux logs.

## 8. Cas limites et variantes

**Variante 1 : Secrets dans les variables d'environnement du CI.** Correctement configurees, elles sont securisees. Mais si un step du CI fait `env | sort` ou `printenv` dans un log public, les secrets sont exposes.

**Variante 2 : Secrets dans les fichiers Docker.** Un `COPY .env .` dans un Dockerfile inclut le fichier dans l'image. Toute personne ayant l'image peut extraire les secrets avec `docker inspect`.

**Variante 3 : Secrets dans les notebooks Jupyter.** Les cellules de notebook contiennent souvent des cles API pour les demos. Les notebooks sont commites avec leurs outputs — incluant les cles.

**Variante 4 : Secrets en clair dans les logs de debug.** Le code fait `logger.debug(f"Request headers: {headers}")`. Les headers contiennent `Authorization: Bearer sk-...`. Le secret finit dans le fichier de log.

## 9. Checklist d'audit

- [ ] Aucun secret n'apparait dans le code source (verifie par grep + gitleaks)
- [ ] `.env` est dans `.gitignore` et n'a jamais ete committe (verifie par `git log`)
- [ ] Un `.env.example` avec des placeholders existe et est a jour
- [ ] Un pre-commit hook bloque les commits avec des secrets
- [ ] Chaque secret a ete genere/rotationne dans les 90 derniers jours
- [ ] Chaque agent a ses propres credentials (pas de partage de cles)

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 07 — Hardcoded Secret in Source](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-07)
- Patterns connexes : #04 (Multi-File State Desync — les secrets peuvent etre dans plusieurs fichiers non synchronises), #11 (Race Condition on Shared File — un fichier .env partage entre agents peut avoir des race conditions)
- Lectures recommandees :
  - "OWASP Top 10" (2021) — A07:2021 Identification and Authentication Failures
  - Documentation GitHub sur le Secret Scanning — la detection automatique de secrets dans les repos publics et prives
