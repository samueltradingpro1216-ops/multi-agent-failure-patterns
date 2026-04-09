"""
Pattern 07 — Hardcoded Secret in Source
Demontre comment detecter et corriger les secrets hardcodes dans le code,
et pourquoi les supprimer du code ne suffit pas (historique git).

Usage: python example.py
"""
import os
import re
import tempfile
from pathlib import Path


# === LE BUG ===

# NE JAMAIS FAIRE CECI — secrets en dur dans le code source
BUGGY_CODE_EXAMPLE = '''
# notification_service.py — VERSION BUGGEE
BOT_TOKEN = "8679765924:AAF3d5__KxB2nM7qR9zWpLkJh"
API_KEY = "sk-proj-abc123def456ghi789jkl012mno345"
DB_PASSWORD = "SuperSecret123!"
WEBHOOK_URL = "https://hooks.example.com/services/T0123/B4567/xyzabc"

def send_notification(message):
    # Utilise BOT_TOKEN directement
    import urllib.request
    url = f"https://api.messaging.com/send?token={BOT_TOKEN}&text={message}"
    urllib.request.urlopen(url)
'''


# === LA CORRECTION ===

FIXED_CODE_EXAMPLE = '''
# notification_service.py — VERSION CORRIGEE
import os

# Secrets lus depuis les variables d'environnement
BOT_TOKEN = os.environ["NOTIFICATION_BOT_TOKEN"]
API_KEY = os.environ["LLM_API_KEY"]
DB_PASSWORD = os.environ["DATABASE_PASSWORD"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

def send_notification(message):
    import urllib.request
    url = f"https://api.messaging.com/send?token={BOT_TOKEN}&text={message}"
    urllib.request.urlopen(url)
'''

ENV_EXAMPLE = '''
# .env.example — Template a copier en .env (jamais committe)
NOTIFICATION_BOT_TOKEN=your_bot_token_here
LLM_API_KEY=sk-your-key-here
DATABASE_PASSWORD=your_password_here
WEBHOOK_URL=https://hooks.example.com/your/webhook
'''

GITIGNORE_ADDITION = '''
# Secrets — JAMAIS committer
.env
.env.local
.env.production
*.key
*.pem
credentials.json
'''


# === DETECTION ===

# Patterns de secrets courants (regex simplifiees)
SECRET_PATTERNS = [
    # Cles API generiques
    (r'(?:api[_-]?key|apikey)\s*=\s*["\'][a-zA-Z0-9_\-]{20,}["\']', "API key"),
    # Tokens de bot
    (r'\d{8,12}:[A-Za-z0-9_\-]{30,}', "Bot token"),
    # Prefixes courants de cles LLM
    (r'sk-[a-zA-Z0-9]{20,}', "LLM API key (sk-...)"),
    # Mots de passe en dur
    (r'(?:password|passwd|pwd)\s*=\s*["\'][^"\']{8,}["\']', "Hardcoded password"),
    # Webhooks
    (r'hooks\.[a-z]+\.com/services/[A-Z0-9/]+', "Webhook URL"),
    # AWS keys
    (r'AKIA[0-9A-Z]{16}', "AWS Access Key"),
    # Connection strings avec password
    (r'(?:mongodb|postgres|mysql)://[^:]+:[^@]+@', "Database connection string"),
]


def scan_for_secrets(code: str, filename: str = "<input>") -> list[dict]:
    """
    Scanne du code source pour trouver des secrets hardcodes.
    Retourne une liste d'alertes avec le type de secret et la ligne.
    """
    alerts = []
    lines = code.split("\n")

    for line_num, line in enumerate(lines, 1):
        # Ignorer les commentaires et les lignes vides
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue

        # Ignorer les lignes qui lisent depuis l'environnement
        if "os.environ" in line or "os.getenv" in line:
            continue

        for pattern, secret_type in SECRET_PATTERNS:
            matches = re.findall(pattern, line, re.IGNORECASE)
            if matches:
                # Masquer le secret dans l'alerte
                masked_line = line.strip()
                for match in matches:
                    if len(match) > 10:
                        masked = match[:6] + "..." + match[-4:]
                        masked_line = masked_line.replace(match, masked)

                alerts.append({
                    "file": filename,
                    "line": line_num,
                    "type": secret_type,
                    "masked": masked_line,
                })

    return alerts


def scan_directory(directory: str, extensions: tuple = (".py", ".js", ".yaml", ".yml", ".json", ".toml")) -> list[dict]:
    """
    Scanne un repertoire entier pour les secrets hardcodes.
    """
    all_alerts = []
    for path in Path(directory).rglob("*"):
        if path.suffix in extensions and path.is_file():
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                alerts = scan_for_secrets(content, str(path))
                all_alerts.extend(alerts)
            except (PermissionError, OSError):
                continue
    return all_alerts


def generate_env_from_code(code: str) -> str:
    """
    Extrait les secrets hardcodes et genere un fichier .env template.
    """
    env_lines = ["# Auto-generated .env template", "# Replace values with real secrets", ""]

    # Chercher les assignations de secrets
    pattern = r'^([A-Z][A-Z0-9_]*)\s*=\s*["\']([^"\']+)["\']'
    for line in code.split("\n"):
        match = re.match(pattern, line.strip())
        if match:
            var_name = match.group(1)
            env_lines.append(f"{var_name}=your_{var_name.lower()}_here")

    return "\n".join(env_lines)


# === DEMONSTRATION ===

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 07 -- Hardcoded Secret in Source")
    print("=" * 60)

    # --- Scanner le code bugge ---
    print("\n--- Scan du code BUGGE ---")
    alerts = scan_for_secrets(BUGGY_CODE_EXAMPLE, "notification_service.py")
    if alerts:
        print(f"  {len(alerts)} secret(s) trouve(s):")
        for alert in alerts:
            print(f"    L{alert['line']} [{alert['type']}]: {alert['masked']}")
    else:
        print("  Aucun secret trouve")

    # --- Scanner le code corrige ---
    print("\n--- Scan du code CORRIGE ---")
    alerts = scan_for_secrets(FIXED_CODE_EXAMPLE, "notification_service.py")
    if alerts:
        print(f"  {len(alerts)} secret(s) trouve(s)")
    else:
        print("  Aucun secret trouve (os.environ utilise)")

    # --- Generer le .env template ---
    print("\n--- .env template genere ---")
    env_template = generate_env_from_code(BUGGY_CODE_EXAMPLE)
    print(env_template)

    # --- Demo: lire depuis l'environnement ---
    print("\n--- Lecture depuis l'environnement ---")

    # Simuler un .env charge
    os.environ["DEMO_API_KEY"] = "sk-test-key-for-demo"
    os.environ["DEMO_BOT_TOKEN"] = "1234567890:AAFtest"

    api_key = os.environ.get("DEMO_API_KEY", "NOT_SET")
    bot_token = os.environ.get("DEMO_BOT_TOKEN", "NOT_SET")
    missing = os.environ.get("DEMO_MISSING_KEY", "NOT_SET")

    print(f"  DEMO_API_KEY: {'***' + api_key[-4:] if api_key != 'NOT_SET' else 'NOT_SET'}")
    print(f"  DEMO_BOT_TOKEN: {'***' + bot_token[-4:] if bot_token != 'NOT_SET' else 'NOT_SET'}")
    print(f"  DEMO_MISSING_KEY: {missing}")

    # Nettoyage
    del os.environ["DEMO_API_KEY"]
    del os.environ["DEMO_BOT_TOKEN"]

    # --- Scan d'un repertoire (demo avec fichier temp) ---
    print("\n--- Scan de repertoire ---")
    with tempfile.TemporaryDirectory() as tmp:
        # Creer un fichier avec un secret
        bad_file = Path(tmp) / "config.py"
        bad_file.write_text('API_KEY = "sk-proj-realkey123456789abcdef"\n')

        # Creer un fichier propre
        good_file = Path(tmp) / "service.py"
        good_file.write_text('API_KEY = os.environ["API_KEY"]\n')

        alerts = scan_directory(tmp)
        print(f"  Fichiers scannes: 2")
        print(f"  Secrets trouves: {len(alerts)}")
        for alert in alerts:
            print(f"    {Path(alert['file']).name}:L{alert['line']} [{alert['type']}]")

    # --- Rappel important ---
    print("\n--- RAPPEL ---")
    print("  Supprimer un secret du code ne suffit PAS.")
    print("  Il reste dans l'historique git (git log -p).")
    print("  Il faut REVOQUER le secret et en generer un nouveau.")
