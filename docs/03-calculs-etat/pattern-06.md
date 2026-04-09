# Pattern N°06 — Silent NameError in try/except

**Categorie :** Calculs & Etat
**Severite :** High
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 5 a 30 jours (la fonctionnalite affectee semble simplement "ne pas exister" — personne ne la cherche activement)

---

## 1. Symptome observable

Une fonctionnalite critique du systeme **ne s'execute jamais** mais aucune erreur n'apparait dans les logs, le monitoring, ou le dashboard. Le systeme tourne normalement, les autres fonctionnalites marchent, et le composant defaillant est simplement invisible.

En inspectant les donnees, on decouvre qu'une branche entiere du code est morte depuis des semaines ou des mois. Un mecanisme d'urgence n'a jamais fonctionne. Un module de validation n'a jamais valide. Un clean-up periodique n'a jamais nettoye. Le code est la, il semble correct a la lecture, mais il ne tourne pas.

Le symptome le plus perfide : le bug ne se manifeste que **par l'absence de quelque chose**. Pas d'erreur, pas de crash, pas de log suspect. Juste une fonctionnalite qui devrait exister et qui n'existe pas. C'est le bug invisible par excellence.

## 2. Histoire vecue (anonymisee)

Un systeme multi-agents avait un mecanisme d'arret d'urgence : quand un agent etait marque comme desactive, le superviseur devait forcer la fermeture de ses taches en cours. Le code existait depuis 6 mois.

Lors d'un incident, un agent desactive avait des taches actives qui continuaient de tourner. L'equipe a active le mecanisme d'urgence — rien ne s'est passe. En inspectant le code, ils ont trouve le bug : la variable `config` etait utilisee 30 lignes avant son assignation, dans un bloc `try/except Exception: pass`. Le `NameError` etait avale silencieusement a chaque cycle depuis 6 mois. Le mecanisme d'urgence n'avait **jamais fonctionne** — personne ne l'avait remarque parce qu'aucun incident ne l'avait necessairement sollicite avant.

L'equipe a realise avec horreur que si l'incident avait ete plus grave, ils n'auraient eu aucun filet de securite.

## 3. Cause racine technique

Le bug se produit quand une variable est **utilisee avant son assignation** dans un bloc `try/except` qui catch `Exception` ou est un bare `except:` :

```python
def process_agent(agent_id: str, is_disabled: bool):
    try:
        # Etape 1: check urgence
        if is_disabled:
            if config.get("has_active_tasks"):   # NameError! 'config' n'existe pas
                force_shutdown(agent_id)
                return

        # ... 30 lignes de code ...

        # Etape 2: lire la config (APRES l'utilisation)
        config = read_config(agent_id)

        # Etape 3: traitement normal
        process(config)

    except Exception:
        pass  # LE NAMEERROR EST AVALE ICI
```

Le mecanisme est le suivant :
1. Python entre dans le `try`
2. A la ligne `config.get(...)`, `config` n'est pas defini → `NameError`
3. Le `except Exception` catch le `NameError` (qui herite de `Exception`)
4. Le `pass` ne fait rien — pas de log, pas d'alerte
5. L'execution continue comme si la branche `if is_disabled` n'existait pas

Le code **semble** correct a la lecture : `config` est bien defini dans la fonction, juste plus bas. Mais l'ordre d'execution fait que l'utilisation precede l'assignation dans le chemin `is_disabled=True`. Et le `except` generique rend le bug totalement invisible.

Ce qui rend le bug particulierement dangereux : il peut vivre **des mois** sans etre detecte. La branche affectee ne s'execute que dans un cas specifique (ici, `is_disabled=True`), et ce cas est rare en fonctionnement normal. Le jour ou il se produit, la fonctionnalite critique est morte silencieusement.

## 4. Detection

### 4.1 Detection manuelle (audit code)

Chercher les blocs `except` dangereux :

```bash
# Bare except (catch tout, y compris SystemExit et KeyboardInterrupt)
grep -rn "except:" --include="*.py" | grep -v "except:\s*#"

# except Exception avec pass (avale silencieusement)
grep -B1 -A1 "except Exception" --include="*.py" -rn | grep -A1 "pass"

# except Exception sans logging
grep -A3 "except Exception" --include="*.py" -rn | grep -v "log\|print\|raise\|warning\|error"
```

Pour chaque `except Exception: pass` trouve, verifier si les variables dans le `try` sont toutes definies avant utilisation dans tous les chemins conditionnels.

### 4.2 Detection automatisee (CI/CD)

Configurer les linters pour intercepter les deux composantes du bug :

```toml
# pyproject.toml — ruff
[tool.ruff.lint]
select = [
    "E",     # pycodestyle errors
    "F821",  # undefined name
    "B",     # flake8-bugbear
    "BLE",   # flake8-blind-except (BLE001: bare except)
    "TRY",   # tryceratops
]

# Regles specifiques:
# F821: undefined name — detecte les variables pas encore definies
# BLE001: blind except — interdit les bare except
# TRY002: raise vanilla Exception — pousse a utiliser des exceptions specifiques
# TRY003: raise within except — interdit raise Exception(...) dans un except
```

Ajouter `mypy --strict` qui detecte les variables potentiellement non definies :

```bash
# mypy signale "possibly undefined" quand une variable est assignee
# dans un if/elif mais utilisee apres sans default
mypy --strict src/
# error: Name "config" may be undefined
```

### 4.3 Detection runtime (production)

Wrapper les blocs `except` critiques avec un logger qui capture la stack trace :

```python
import logging
import traceback

logger = logging.getLogger(__name__)

def safe_except_handler(func):
    """Decorator that logs exceptions instead of silently catching them."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(
                f"Exception in {func.__name__}: {type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            # Re-raise or return a safe default based on policy
            return None
    return wrapper
```

## 5. Correction

### 5.1 Fix immediat

Deplacer l'assignation avant l'utilisation :

```python
def process_agent(agent_id: str, is_disabled: bool):
    # FIX: lire la config EN PREMIER
    config = read_config(agent_id)

    try:
        if is_disabled:
            if config.get("has_active_tasks"):
                force_shutdown(agent_id)
                return
        process(config)
    except Exception as e:
        logging.error(f"Error processing {agent_id}: {e}")
```

### 5.2 Fix robuste

Initialiser toutes les variables en debut de scope ET remplacer le bare except par des exceptions specifiques :

```python
def process_agent(agent_id: str, is_disabled: bool):
    config = None  # Initialisation explicite

    try:
        config = read_config(agent_id)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.error(f"Cannot read config for {agent_id}: {e}")
        return

    # Le code qui depend de config est HORS du try
    # Si config est None ici, c'est explicite et verifiable
    if config is None:
        logging.error(f"No config available for {agent_id}")
        return

    if is_disabled and config.get("has_active_tasks"):
        force_shutdown(agent_id)
        return

    try:
        process(config)
    except ProcessingError as e:
        logging.error(f"Processing failed for {agent_id}: {e}")
```

## 6. Prevention architecturale

La prevention repose sur trois regles strictes :

**1. Interdire `except Exception: pass` au niveau du projet.** Configurer ruff/pylint pour bloquer les bare except et les except trop larges sans logging. Un pre-commit hook empeche de committer du code qui avale les exceptions silencieusement.

**2. Initialiser les variables en debut de scope.** Toute variable utilisee dans un bloc try doit etre initialisee avant le try : `config = None`, `result = []`, `handlers = []`. Cela elimine la classe entiere des bugs "undefined variable in conditional path".

**3. Separer les blocs try par responsabilite.** Au lieu d'un seul gros `try` qui englobe tout, utiliser des blocs specifiques pour chaque operation risquee. Chaque bloc catch uniquement les exceptions attendues pour cette operation.

## 7. Anti-patterns a eviter

1. **`except Exception: pass`** — Le silenceur de bugs par excellence. Il ne gere pas les erreurs, il les cache. Au minimum : `except Exception: logging.exception("...")`.

2. **Un seul `try` qui englobe 50 lignes.** Plus le bloc est grand, plus il y a de chances qu'une exception inattendue soit avalee. Decouper en petits blocs specifiques.

3. **Catch `Exception` au lieu de l'exception specifique.** `except FileNotFoundError` est precis. `except Exception` catch aussi `NameError`, `TypeError`, `AttributeError` — des bugs, pas des erreurs attendues.

4. **Variable assignee dans un `if` sans `else`.** Si `config` n'est assigne que dans `if condition:`, il est indefini quand `condition` est faux. Toujours initialiser avant le `if` ou ajouter un `else`.

5. **Tester la fonctionnalite seulement dans le happy path.** Si le mecanisme d'urgence n'est teste que "quand il n'y a pas d'urgence", le bug dans le chemin d'urgence reste invisible.

## 8. Cas limites et variantes

**Variante 1 : AttributeError silencieux.** `self.module.process()` quand `self.module` est `None` → `AttributeError` avale par `except Exception`. Le module n'a jamais ete initialise mais le code fait comme s'il existait.

**Variante 2 : TypeError dans un generateur.** Un generateur qui yield des valeurs est appele avec le mauvais type d'argument. Le `TypeError` est catch par l'appelant, le generateur ne produit rien, et le pipeline continue avec une liste vide.

**Variante 3 : ImportError masque.** `from optional_module import feature` dans un try/except. Le module n'est pas installe, le `ImportError` est catch, et `feature` n'est jamais defini. Toutes les references a `feature` plus bas levent `NameError` — catch a nouveau.

**Variante 4 : Dead code par ordre de conditions.** Deux `if` testent la meme condition. Le premier catch tout, le second est du dead code. Pas un NameError mais le meme effet : du code qui ne s'execute jamais, invisiblement.

## 9. Checklist d'audit

- [ ] Aucun `except Exception: pass` ni `except: pass` dans la codebase
- [ ] Chaque `except` log au minimum le type et le message de l'exception
- [ ] Toutes les variables utilisees dans un `try` sont initialisees avant le `try`
- [ ] `ruff` est configure avec les regles BLE001, F821, TRY002
- [ ] `mypy --strict` ne signale aucun "possibly undefined"
- [ ] Les chemins d'urgence/erreur sont testes explicitement (pas juste le happy path)

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 06 — Silent NameError in try/except](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-06)
- Patterns connexes : #08 (Data Pipeline Freeze — un consommateur qui ne crash pas mais ne recoit plus de donnees a souvent un except silencieux quelque part), #10 (Survival Mode Deadlock — un except silencieux dans le calcul de confiance peut masquer un score toujours a zero)
- Lectures recommandees :
  - "The Pragmatic Programmer" (Hunt & Thomas), section sur les exceptions : "Crash early, don't hide errors"
  - PEP 8 section sur les exceptions — le guide officiel Python recommande de ne jamais utiliser bare except
