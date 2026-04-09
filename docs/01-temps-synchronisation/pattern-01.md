# Pattern N°01 — Timezone Mismatch

**Categorie :** Temps & Synchronisation
**Severite :** Critical
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 2 a 10 jours (souvent decouvert par accident lors d'un audit, pas par un symptome immediat)

---

## 1. Symptome observable

Le systeme agit aux **mauvaises heures**. Un filtre temporel cense activer les agents entre 12h et 18h UTC les active en realite entre 9h et 15h UTC — un decalage de 3 heures que personne ne remarque parce que le systeme semble fonctionner normalement.

Les logs sont trompeurs : le filtre fonctionne, il bloque et autorise des requetes. Mais les heures sont decalees. Le systeme rate sa fenetre d'activite optimale (par exemple le pic de trafic utilisateur, la fenetre de batch processing, ou la plage horaire ou les APIs partenaires sont disponibles) et agit pendant les heures creuses.

Le symptome le plus perfide : **le bug est invisible en test local** si le poste du developpeur est en UTC. Le decalage n'apparait qu'en production, sur un serveur dont la timezone systeme est differente (UTC+2 sur un VPS en Europe, UTC+8 sur un cloud provider asiatique, ou l'heure du service tiers auquel on se connecte). Les tests passent, le code review passe, et le bug vit en production pendant des semaines avant qu'un audit ne le revele.

Un autre symptome courant : les **timestamps entre composants ne correspondent pas**. L'agent A ecrit un log a "14:32:05", l'agent B ecrit un log a "17:32:07" pour le meme evenement. Le delta est proche d'un multiple d'heure (3h), pas d'un drift reseau normal (< 1s). C'est le signal d'un mismatch timezone, pas d'un probleme de latence.

## 2. Histoire vecue (anonymisee)

Un systeme multi-agents en production utilisait un filtre horaire pour limiter l'activite de ses agents a la fenetre 12h-18h UTC — la plage ou les donnees en temps reel etaient les plus fiables et les plus volumineuses. Le filtre fonctionnait depuis 3 semaines sans alerte.

Lors d'un audit de performance, l'equipe a remarque que le systeme etait anormalement actif entre 9h et 15h UTC et silencieux entre 15h et 18h UTC. En investiguant, ils ont decouvert que le composant executeur utilisait `TimeCurrent()` (l'equivalent local de `datetime.now()`) au lieu de `TimeGMT()` pour evaluer le filtre. Le serveur etait en UTC+3 (data center en Europe de l'Est), donc les bornes "12h-18h" etaient evaluees en heure locale (12h-18h serveur = 9h-15h UTC). Le systeme ratait les 3 meilleures heures de la journee et operait pendant les 3 heures les moins fiables.

Le bug etait invisible car les agents produisaient des resultats acceptables meme sur la mauvaise fenetre — juste ~15-20% moins bons. Il a fallu un audit ligne par ligne pour le trouver.

## 3. Cause racine technique

Le bug naît quand un composant utilise l'**heure locale du serveur** au lieu d'UTC explicite pour ses calculs temporels. Dans un systeme mono-serveur, ca peut fonctionner. Dans un systeme multi-agents, chaque composant peut tourner dans un contexte timezone different :

- L'orchestrateur Python utilise `datetime.now()` (heure locale du VPS)
- L'executeur utilise l'heure de son service tiers (API, base de donnees, cloud provider)
- Les cron jobs utilisent leur propre timezone configuree
- Les logs de chaque composant sont dans la timezone de leur processus

Le code typique du bug :

```python
# BUG: datetime.now() retourne l'heure locale, pas UTC
current_hour = datetime.now().hour

# Ce filtre est cense bloquer hors de 12h-18h UTC
# Mais sur un serveur en UTC+3, il bloque hors de 12h-18h LOCAL = 9h-15h UTC
if not (12 <= current_hour < 18):
    return "BLOCKED — outside active window"
```

Le probleme fondamental est l'**hypothese implicite de timezone**. Le developpeur ecrit `datetime.now().hour` en pensant "heure UTC" parce que son poste de dev est en UTC. En production, `datetime.now()` retourne l'heure du serveur, qui peut etre n'importe quoi.

Le mismatch se propage dans tout le systeme :

```
Composant A (UTC)      : "Il est 14h, fenetre active"
Composant B (UTC+3)    : "Il est 17h, fenetre active"
Composant C (UTC-5)    : "Il est 9h, hors fenetre"
                          → 3 composants, 3 heures differentes, 3 decisions differentes
```

Les variantes du meme bug incluent :
- `time.time()` qui retourne un timestamp POSIX (toujours UTC, correct) mais converti en heure locale via `time.localtime()` au lieu de `time.gmtime()`
- `datetime.utcnow()` qui retourne un objet **timezone-naive** (pas d'info timezone attachee), ce qui le rend impossible a comparer correctement avec un datetime timezone-aware
- Des timestamps stockes en "local time + suffixe Z" : `datetime.now().isoformat() + "Z"` produit un timestamp qui **pretend** etre UTC mais qui est en heure locale

## 4. Detection

### 4.1 Detection manuelle (audit code)

Chercher toutes les sources de temps dans la codebase :

```bash
# Chercher les appels datetime.now() sans timezone
grep -rn "datetime\.now()" --include="*.py" | grep -v "timezone"

# Chercher utcnow() deprecie (Python 3.12+)
grep -rn "\.utcnow()" --include="*.py"

# Chercher les conversions localtime potentiellement dangereuses
grep -rn "localtime\|mktime\|strftime" --include="*.py"

# Chercher les suffixes "Z" ajoutes manuellement (faux UTC)
grep -rn '+ "Z"\|+"Z"\|+ "z"' --include="*.py"
```

Chaque occurrence trouvee est suspecte. Verifier si le contexte d'execution (serveur, container, cron) a la meme timezone que ce que le code attend.

### 4.2 Detection automatisee (CI/CD)

Configurer `ruff` pour interdire les appels a `datetime.now()` sans timezone :

```toml
# ruff.toml ou pyproject.toml
[tool.ruff.lint]
select = ["DTZ"]  # datetime-timezone rules
# DTZ001: datetime.now() without tz
# DTZ002: datetime.utcnow() (deprecated)
# DTZ003: datetime.now(timezone.utc) preferred
# DTZ005: datetime.now().isoformat() without tz
```

Ajouter un test custom qui echoue si un module utilise `datetime.now()` sans argument :

```python
import ast
import pathlib

def test_no_naive_datetime_now():
    """Fail if any Python file calls datetime.now() without a timezone argument."""
    violations = []
    for py_file in pathlib.Path("src").rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "now"
                and not node.args and not node.keywords):
                violations.append(f"{py_file}:{node.lineno}")

    assert not violations, f"datetime.now() without tz found:\n" + "\n".join(violations)
```

### 4.3 Detection runtime (production)

Comparer les timestamps entre composants a chaque cycle. Si le delta est proche d'un multiple d'heure, c'est un mismatch timezone :

```python
from datetime import datetime, timezone, timedelta

def check_component_clock_drift(
    timestamps: dict[str, datetime],
    max_drift_seconds: int = 60
) -> list[str]:
    """
    Compare timestamps from different components.
    If delta is close to a multiple of 1 hour, flag as timezone mismatch.

    Args:
        timestamps: {"component_name": datetime_with_tz, ...}
        max_drift_seconds: normal drift tolerance (network lag)

    Returns:
        List of alert messages.
    """
    alerts = []
    components = list(timestamps.keys())

    for i in range(len(components)):
        for j in range(i + 1, len(components)):
            a, b = components[i], components[j]
            delta = abs((timestamps[a] - timestamps[b]).total_seconds())

            if delta < max_drift_seconds:
                continue

            nearest_hour = round(delta / 3600)
            remainder = abs(delta - nearest_hour * 3600)

            if nearest_hour > 0 and remainder < 300:
                alerts.append(
                    f"TIMEZONE MISMATCH: {a} vs {b} — "
                    f"delta ~{nearest_hour}h (probably {a} or {b} not in UTC)"
                )

    return alerts

# Usage in a health check loop
def periodic_clock_audit(agent_timestamps: dict[str, datetime]):
    alerts = check_component_clock_drift(agent_timestamps)
    for alert in alerts:
        logging.critical(alert)
        # Send to monitoring (Telegram, Slack, PagerDuty, etc.)
```

En complement, ajouter un champ `_tz_info` dans chaque message inter-agents qui indique la timezone du composant emetteur. Si un recepteur voit un `_tz_info` different du sien, il peut alerter immediatement.

## 5. Correction

### 5.1 Fix immediat

Remplacer chaque appel `datetime.now()` par `datetime.now(timezone.utc)` :

```python
from datetime import datetime, timezone

# AVANT (bug)
current_hour = datetime.now().hour

# APRES (fix)
current_hour = datetime.now(timezone.utc).hour
```

Pour les composants non-Python (executeurs externes, services tiers), convertir explicitement leur timestamp en UTC avant toute comparaison :

```python
from datetime import datetime, timezone, timedelta

def external_time_to_utc(external_time: datetime, known_offset_hours: int) -> datetime:
    """Convert a time from an external system to UTC."""
    return external_time.replace(tzinfo=None) - timedelta(hours=known_offset_hours)
```

### 5.2 Fix robuste

Centraliser toute obtention de temps dans un module unique. Aucun composant n'appelle `datetime.now()` directement — tous passent par ce module :

```python
"""time_utils.py — Single source of truth for all time operations."""
from datetime import datetime, timezone, timedelta

class Clock:
    """Centralized time provider. All components use this, never datetime.now() directly."""

    # Known external system offsets (configure per deployment)
    OFFSETS = {
        "server_eu": 3,     # UTC+3
        "server_us": -5,    # UTC-5 (EST)
        "api_partner": 0,   # UTC
    }

    @staticmethod
    def now() -> datetime:
        """The ONLY way to get current time. Always UTC, always timezone-aware."""
        return datetime.now(timezone.utc)

    @classmethod
    def from_external(cls, external_time: datetime, system: str) -> datetime:
        """Convert an external system's time to UTC."""
        offset = cls.OFFSETS.get(system, 0)
        naive = external_time.replace(tzinfo=None)
        return (naive - timedelta(hours=offset)).replace(tzinfo=timezone.utc)

    @staticmethod
    def is_in_window(start_hour_utc: int, end_hour_utc: int) -> bool:
        """Check if current UTC hour is within a time window."""
        h = Clock.now().hour
        if start_hour_utc <= end_hour_utc:
            return start_hour_utc <= h < end_hour_utc
        else:  # Window crosses midnight
            return h >= start_hour_utc or h < end_hour_utc

# Usage across all components:
from time_utils import Clock

if Clock.is_in_window(12, 18):
    agent.execute()
```

## 6. Prevention architecturale

La regle fondamentale : **un seul type de temps dans tout le systeme, et c'est UTC**. Pas d'exceptions, pas de "juste ici c'est en local pour la lisibilite", pas de "on convertit au dernier moment".

Concretement, cela implique trois decisions architecturales :

**1. Un module `Clock` centralise.** Aucun composant n'importe `datetime` directement. Tout passe par un module qui garantit UTC + timezone-aware. Ce module est le seul a connaitre les offsets des systemes externes.

**2. Les timestamps stockes sont toujours en ISO-8601 avec timezone.** Pas de `"2026-04-09 14:30:00"` sans timezone. Toujours `"2026-04-09T14:30:00+00:00"` ou `"2026-04-09T14:30:00Z"`. La presence explicite du `+00:00` rend le bug impossible a ignorer.

**3. Les tests d'integration s'executent dans une timezone non-UTC.** Ajouter `TZ=Pacific/Chatham` (UTC+12:45, la timezone la plus exotique au monde) dans le CI pour forcer les hypotheses implicites a exploser immediatement. Si un test passe en UTC et echoue en UTC+12:45, il y a un bug de timezone.

## 7. Anti-patterns a eviter

1. **`datetime.now()` sans argument timezone.** C'est le bug sous sa forme la plus courante. Remplacer systematiquement par `datetime.now(timezone.utc)`.

2. **`datetime.utcnow()`** (deprecie depuis Python 3.12). Il retourne un datetime timezone-naive qui **pretend** etre UTC mais ne porte pas l'information. Il ne peut pas etre compare de maniere fiable avec un datetime timezone-aware.

3. **Ajouter `"Z"` manuellement a un timestamp.** `datetime.now().isoformat() + "Z"` produit un timestamp qui dit "UTC" mais qui est en heure locale. C'est un mensonge dans les metadonnees.

4. **Comparer des timestamps sans verifier leur timezone.** `time_a > time_b` quand l'un est en UTC et l'autre en local donne un resultat faux sans lever d'erreur.

5. **Assumer que le serveur est en UTC.** Meme si c'est vrai aujourd'hui, une migration de serveur, un changement de cloud provider, ou un passage a l'heure d'ete peut changer la timezone silencieusement.

## 8. Cas limites et variantes

**Variante 1 : DST (heure d'ete/hiver).** Certaines timezones changent de +2 a +3 selon la saison (Europe). Le bug peut apparaitre ou disparaitre deux fois par an, rendant le diagnostic extremement difficile. Un serveur en Europe centrale est en UTC+1 l'hiver et UTC+2 l'ete — meme avec un offset connu, il change.

**Variante 2 : Microservices multi-cloud.** L'agent A tourne sur AWS (UTC par defaut), l'agent B sur GCP (UTC par defaut aussi, mais le container peut avoir une timezone custom), l'agent C tourne sur un VPS on-premise (timezone locale du pays). Trois sources de temps differentes pour le meme systeme.

**Variante 3 : Timestamps dans les fichiers de communication.** Un agent ecrit un fichier JSON avec `"created_at": "2026-04-09 14:30"` en heure locale. Un autre agent lit ce fichier et interprete le timestamp comme UTC. Le fichier ne contient aucune information de timezone — impossible de savoir qui a raison sans connaitre le contexte de creation.

**Variante 4 : Bases de donnees avec timezone implicite.** SQLite stocke les datetimes comme du texte sans timezone. PostgreSQL a un type `TIMESTAMP WITH TIME ZONE` mais aussi `TIMESTAMP WITHOUT TIME ZONE`. Si un composant ecrit sans timezone et un autre lit en assumant UTC, le decalage s'installe silencieusement.

## 9. Checklist d'audit

- [ ] Aucun appel `datetime.now()` sans argument `tz=timezone.utc` dans la codebase
- [ ] Aucun appel `datetime.utcnow()` (deprecie Python 3.12+)
- [ ] Tous les timestamps stockes (fichiers, DB, messages) contiennent une timezone explicite
- [ ] Les tests CI s'executent dans une timezone non-UTC (ex: `TZ=Pacific/Chatham`)
- [ ] Un module `Clock` centralise ou une convention documentee impose UTC partout
- [ ] Les timestamps des composants externes sont convertis en UTC avant comparaison
- [ ] Un health check compare les horloges entre composants et alerte si drift > 5min

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 01 — Timezone Mismatch](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-01)
- Patterns connexes : #04 (Multi-File State Desync — les timestamps dans les fichiers partages peuvent etre en timezones differentes), #08 (Data Pipeline Freeze — un timestamp mal interprete peut faire croire que des donnees sont fraiches alors qu'elles sont stales)
- Lectures recommandees :
  - "Falsehoods programmers believe about time" (Zach Holman, 2015) — la reference sur les hypotheses implicites en gestion du temps
  - Documentation Python `datetime` (section timezone-aware vs timezone-naive) — la distinction fondamentale que tout dev Python doit maitriser
