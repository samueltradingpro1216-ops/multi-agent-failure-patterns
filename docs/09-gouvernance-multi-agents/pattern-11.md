# Pattern N°11 — Race Condition on Shared File

**Categorie :** Gouvernance Multi-Agents
**Severite :** High
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 3 a 20 jours (le bug est intermittent — il ne se produit que quand deux agents ecrivent "au meme moment", ce qui peut etre rare)

---

## 1. Symptome observable

Des donnees **disparaissent** du fichier de config ou du state store. Un compteur qui devrait valoir 10 vaut 7. Un JSON contient des donnees corrompues ou tronquees. Les logs d'un agent montrent qu'il a ecrit une valeur, mais le fichier contient une autre valeur — celle ecrite par un autre agent quelques millisecondes plus tard.

Le symptome le plus deroutant : le bug est **intermittent**. Il ne se produit que quand deux agents ecrivent au meme moment, ce qui depend du timing exact de l'ordonnancement. Le systeme peut tourner des heures sans probleme, puis perdre 3 updates en 5 minutes. Les tests en local (mono-thread) ne reproduisent jamais le bug. Il n'apparait qu'en production avec de la concurrence reelle.

Un autre symptome courant dans les systemes multi-agents : les **quotas ou compteurs de budget** sont faux. Deux agents consomment des tokens LLM en parallele, chacun incremente le compteur d'usage, mais un des deux increments est perdu. Le compteur dit "850/1000 tokens utilises" alors que la realite est "920/1000". Le budget explose sans alerte.

## 2. Histoire vecue (anonymisee)

Un systeme multi-agents avait un fichier `usage.json` qui trackait l'utilisation des APIs LLM. Chaque agent lisait le fichier, incrementait son compteur, et reecrivait le fichier. Quatre agents tournaient en parallele.

L'equipe a remarque que la facture LLM etait systematiquement 20-30% plus elevee que ce que le tracker affichait. En auditant les logs, ils ont decouvert le probleme :

```
Agent A: lit usage.json → {"total": 500}
Agent B: lit usage.json → {"total": 500}    (meme valeur, A n'a pas encore ecrit)
Agent A: ecrit usage.json → {"total": 510}  (500 + 10)
Agent B: ecrit usage.json → {"total": 505}  (500 + 5, ecrase le 510 de A)
→ 10 tokens de A sont perdus dans le compteur
```

Sur une journee avec des milliers d'operations, les pertes accumulees representaient 30% du total. Le tracker disait "7000 tokens utilises" alors que la facture reelle etait de 10,000.

## 3. Cause racine technique

Le bug est une **race condition classique** sur un read-modify-write non atomique. Deux agents (ou plus) executent la sequence suivante en parallele, sans mecanisme d'exclusion mutuelle :

```
1. Read:   data = json.load(open("state.json"))
2. Modify: data["count"] += 1
3. Write:  json.dump(data, open("state.json", "w"))
```

Si deux agents executent cette sequence en parallele, l'agent B peut lire la valeur **avant** que l'agent A n'ait ecrit sa modification. L'agent B calcule sa modification a partir de l'ancienne valeur et ecrase la modification de l'agent A.

Le probleme est aggrave dans les systemes multi-agents par trois facteurs :

**1. Agents en parallele.** Contrairement a un serveur web mono-processus, un systeme multi-agents a souvent N agents qui tournent en parallele (threads, processus, ou cron jobs independants). Chacun peut acceder au fichier partage a tout moment.

**2. Duree du "modify".** Dans un systeme multi-agents, l'etape "modify" peut inclure un appel LLM (3-30 secondes). Pendant ce temps, le fichier est "lu mais pas encore reecrit" — une fenetre de vulnerabilite tres large comparee a un simple `count += 1`.

**3. Fichiers JSON = pas de transactions.** Contrairement a une base de donnees, un fichier JSON ne supporte pas les ecritures atomiques ni les transactions. L'ecriture d'un fichier JSON n'est pas atomique : si le processus crashe au milieu, le fichier peut etre corrompu (tronque, JSON invalide).

## 4. Detection

### 4.1 Detection manuelle (audit code)

Chercher les patterns read-modify-write sur des fichiers partages :

```bash
# Chercher les lectures de fichiers JSON
grep -rn "json\.load\|json\.loads.*read\|open.*json.*r" --include="*.py"

# Chercher les ecritures correspondantes
grep -rn "json\.dump\|open.*json.*w" --include="*.py"

# Chercher les fichiers accedes par plusieurs modules
for f in $(grep -roh "['\"].*\.json['\"]" --include="*.py" | sort -u); do
    count=$(grep -rl "$f" --include="*.py" | wc -l)
    if [ "$count" -gt 1 ]; then
        echo "SHARED: $f accessed by $count files"
    fi
done
```

### 4.2 Detection automatisee (CI/CD)

Test de concurrence qui verifie la coherence apres des ecritures paralleles :

```python
import threading
import json
import tempfile
import os

def test_concurrent_write_consistency():
    """Verify no updates are lost when multiple threads write to the same file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"count": 0}, f)
        filepath = f.name

    n_threads = 4
    increments_per_thread = 50
    expected_total = n_threads * increments_per_thread

    def increment():
        for _ in range(increments_per_thread):
            with open(filepath) as f:
                data = json.load(f)
            data["count"] += 1
            with open(filepath, "w") as f:
                json.dump(data, f)

    threads = [threading.Thread(target=increment) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(filepath) as f:
        actual = json.load(f)["count"]

    os.unlink(filepath)

    # This test will almost certainly FAIL — that's the point
    # It demonstrates the race condition exists
    if actual < expected_total:
        print(f"RACE CONDITION: expected {expected_total}, got {actual}, lost {expected_total - actual}")
    assert actual == expected_total, f"Lost {expected_total - actual} updates"
```

### 4.3 Detection runtime (production)

Compteur de verification qui compare les increments attendus aux increments reels :

```python
import threading

class WriteAuditor:
    """Tracks expected vs actual values to detect lost writes."""

    def __init__(self):
        self.expected_increments = 0
        self.lock = threading.Lock()

    def record_increment(self, amount: int = 1):
        """Call this BEFORE writing. Tracks how many increments should exist."""
        with self.lock:
            self.expected_increments += amount

    def audit(self, actual_total: int) -> dict:
        """Compare expected total with actual file value."""
        with self.lock:
            lost = self.expected_increments - actual_total
        if lost > 0:
            return {
                "race_condition": True,
                "expected": self.expected_increments,
                "actual": actual_total,
                "lost": lost,
                "loss_rate": lost / self.expected_increments if self.expected_increments > 0 else 0,
            }
        return {"race_condition": False}
```

## 5. Correction

### 5.1 Fix immediat

Ajouter un lock autour de chaque read-modify-write :

```python
import threading
import json

_file_lock = threading.Lock()

def safe_increment(filepath: str, key: str, amount: int = 1):
    """Thread-safe increment on a JSON file."""
    with _file_lock:
        with open(filepath) as f:
            data = json.load(f)
        data[key] = data.get(key, 0) + amount
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
```

Pour les processus multiples (pas juste des threads), utiliser un file lock :

```python
import fcntl  # Unix only; use msvcrt or portalocker on Windows

def safe_increment_multiprocess(filepath: str, key: str, amount: int = 1):
    """Process-safe increment using file locking."""
    with open(filepath, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # Exclusive lock
        try:
            data = json.load(f)
            data[key] = data.get(key, 0) + amount
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)  # Release lock
```

### 5.2 Fix robuste

Migrer vers SQLite avec transactions (elimine la classe entiere de race conditions sur les fichiers) :

```python
import sqlite3
from contextlib import contextmanager

class AtomicStateStore:
    """SQLite-based state store with atomic read-modify-write."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent reads
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def increment(self, key: str, amount: int = 1) -> int:
        """Atomic increment. Returns new value."""
        from datetime import datetime, timezone
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            current = int(row[0]) if row else 0
            new_value = current + amount
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
                (key, str(new_value), now)
            )
            return new_value

    def get(self, key: str, default: int = 0) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row else default
```

## 6. Prevention architecturale

La prevention repose sur un changement de paradigme : **ne jamais utiliser des fichiers JSON comme state store concurrent**.

**1. SQLite pour l'etat local.** SQLite supporte les transactions, les locks, et les ecritures concurrentes (avec WAL mode). C'est un remplacement drop-in pour les fichiers JSON avec des garanties de coherence.

**2. Un seul writer par ressource.** Au lieu de 4 agents qui ecrivent dans le meme fichier, un seul "agent writer" centralise les ecritures. Les autres agents lui envoient des messages ("incremente de 5"). Le writer serialise les ecritures.

**3. Ecriture atomique pour les fichiers.** Si un fichier JSON est necessaire (compatibilite), utiliser le pattern "write-to-temp + rename" :

```python
import tempfile, os, json

def atomic_write_json(filepath: str, data: dict):
    """Write JSON atomically: temp file + rename."""
    dir_name = os.path.dirname(filepath) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, filepath)  # Atomic on most OS
    except Exception:
        os.unlink(tmp_path)
        raise
```

## 7. Anti-patterns a eviter

1. **json.load() + json.dump() sans lock.** C'est la recette de la race condition. Toujours wrapper dans un lock ou utiliser SQLite.

2. **Fichier JSON comme compteur concurrent.** Un fichier JSON n'est pas une base de donnees. Il ne supporte pas les ecritures atomiques ni les transactions.

3. **Lock en memoire pour des processus multiples.** `threading.Lock()` ne protege que les threads du meme processus. Si les agents sont des processus separes ou des cron jobs, il faut un file lock ou une DB.

4. **Pas de test de concurrence.** Si le code n'est teste qu'en mono-thread, la race condition est invisible. Toujours tester avec N threads/processus en parallele.

5. **Ignorer les ecritures tronquees.** Si le processus crashe pendant un `json.dump()`, le fichier peut etre tronque (JSON invalide). Le prochain `json.load()` crash. Solution : ecriture atomique via temp file + rename.

## 8. Cas limites et variantes

**Variante 1 : Race condition sur SQLite.** Meme SQLite a des limites : en mode journal (pas WAL), les ecritures concurrentes sont serialisees et peuvent causer des timeouts. Utiliser `PRAGMA journal_mode=WAL` et un timeout suffisant.

**Variante 2 : Race condition sur un cache en memoire.** Deux threads accedent au meme dictionnaire Python sans lock. Python a le GIL qui protege certaines operations atomiques, mais pas `dict[key] = compute(dict[key])` qui est un read-modify-write.

**Variante 3 : Lock file stale.** Un processus acquiert un lock fichier, crashe sans le liberer. Le lock reste indefiniment. Solution : lock avec TTL (si le lock a > N minutes, le considerer comme stale et le supprimer).

**Variante 4 : ABA problem.** L'agent A lit `count=5`, est preempte. L'agent B incremente a 6 puis decremente a 5. L'agent A reprend, voit `count=5` (inchange), et ecrit `count=6`. Le resultat est correct par accident mais la logique est fausse — l'agent A n'a pas vu les deux operations de B.

## 9. Checklist d'audit

- [ ] Aucun fichier JSON n'est utilise comme state store concurrent sans lock
- [ ] Chaque read-modify-write est protege par un lock (threading.Lock ou file lock)
- [ ] Les ecritures de fichiers critiques utilisent le pattern atomic write (temp + rename)
- [ ] Un test de concurrence (N threads, M increments) verifie la coherence
- [ ] Les lock files stale sont detectes et nettoyes automatiquement

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 11 — Race Condition on Shared File](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-11)
- Patterns connexes : #04 (Multi-File State Desync — la race condition peut creer une desync entre copies), #03 (Cascade de Penalites — des read-modify-write concurrents sur le meme parametre)
- Lectures recommandees :
  - "Designing Data-Intensive Applications" (Martin Kleppmann), chapitre 7 sur les transactions et la concurrence
  - Documentation Python `threading` — section sur les locks et les conditions de course
