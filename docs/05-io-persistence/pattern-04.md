# Pattern N°04 — Multi-File State Desync

**Categorie :** I/O & Persistence
**Severite :** Critical
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 3 a 15 jours (le bug est masque par le fait que chaque composant fonctionne correctement en isolation)

---

## 1. Symptome observable

Trois composants du systeme donnent **trois reponses differentes** a la meme question. L'executeur pense que le systeme est en arret d'urgence. Le superviseur pense que tout fonctionne normalement. Le dashboard crash car un fichier d'etat n'existe pas.

Le symptome le plus deroutant : chaque composant a **raison selon sa propre source de donnees**. L'executeur lit un fichier qui dit "ARRET". Le superviseur lit une config qui dit "actif". Le dashboard cherche un fichier JSON qui n'a jamais ete cree. Aucun ne ment — ils lisent simplement des sources differentes qui ne sont pas synchronisees.

En consequences indirectes : des commandes sont envoyees mais jamais executees (le superviseur envoie, l'executeur bloque), des alertes se declenchent sans raison (le dashboard voit un etat incoherent), et le developpeur passe des heures a chercher un bug qui n'est pas dans le code mais dans la **topologie des donnees**.

## 2. Histoire vecue (anonymisee)

Un systeme multi-agents avait un mecanisme d'arret d'urgence ("killswitch") dont l'etat etait stocke en 3 endroits :
- Un fichier texte simple (`emergency.txt`) ecrit par l'executeur
- Un fichier JSON structure (`emergency_state.json`) pour le dashboard
- Un champ dans la config partagee (`config.json → emergency_active: false`) pour le superviseur

Lors d'un incident 10 jours plus tot, l'executeur avait active l'arret d'urgence en ecrivant "ACTIVE" dans le fichier texte. L'incident avait ete resolu manuellement en changeant `emergency_active: false` dans la config. Mais personne n'avait pense a mettre a jour le fichier texte.

Resultat : 10 jours plus tard, l'executeur lisait toujours "ACTIVE" dans son fichier et refusait silencieusement les commandes du superviseur. Le superviseur, lui, voyait `emergency_active: false` et continuait d'envoyer des commandes. Il a fallu 3 heures de debugging pour comprendre que le probleme n'etait pas dans le code mais dans un fichier texte stale de 10 jours.

## 3. Cause racine technique

L'etat a ete **duplique progressivement** au fil du developpement du systeme. Chaque nouvelle fonctionnalite a ajoute sa propre representation de l'etat sans synchroniser les precedentes :

```
V1 (mois 1) : executeur ecrit emergency.txt           ← format texte
V2 (mois 3) : superviseur utilise config.json          ← champ JSON
V3 (mois 6) : dashboard lit emergency_state.json       ← JSON structure
```

A aucun moment ces 3 fichiers ne sont synchronises. Chaque composant ecrit dans "son" fichier et ne lit que celui-la. Le resultat est 3 sources de verite pour un seul etat :

```
emergency.txt          → "ACTIVE"    (ecrit il y a 10 jours, jamais nettoye)
emergency_state.json   → absent      (jamais cree)
config.json            → false       (mis a jour manuellement)
```

Le probleme fondamental est l'absence de **Single Source of Truth** (SSOT). Quand un etat critique est stocke a N endroits, il faut N operations synchronisees pour le modifier. En pratique, seules 1 ou 2 operations sont faites, et les N-1 ou N-2 restantes divergent silencieusement.

## 4. Detection

### 4.1 Detection manuelle (audit code)

Pour chaque etat critique du systeme, lister toutes les sources qui le stockent :

```bash
# Chercher toutes les references a un etat specifique (ex: "emergency", "killswitch")
grep -rn "emergency\|killswitch\|shutdown" --include="*.py" --include="*.json"

# Compter les fichiers qui ECRIVENT cet etat
grep -rln "emergency.*=\|write.*emergency\|emergency.*write" --include="*.py"

# Compter les fichiers qui LISENT cet etat
grep -rln "read.*emergency\|emergency.*get\|load.*emergency" --include="*.py"
```

Si le nombre de writers > 1 ou le nombre de sources > 1, c'est un candidat pour la desync.

### 4.2 Detection automatisee (CI/CD)

Documenter les etats critiques dans un fichier de referentiel et verifier en CI que chaque etat n'a qu'une seule source d'ecriture :

```python
# test_single_source_of_truth.py
CRITICAL_STATES = {
    "emergency_active": {
        "primary_source": "data/emergency_state.json",
        "expected_writers": 1,
    },
    "system_config": {
        "primary_source": "data/config.json",
        "expected_writers": 1,
    },
}

def test_no_duplicate_writers():
    """Each critical state should have exactly one writer module."""
    import pathlib, re
    for state, spec in CRITICAL_STATES.items():
        writers = []
        for py_file in pathlib.Path("src").rglob("*.py"):
            content = py_file.read_text()
            if re.search(rf'write.*{state}|{state}.*=|save.*{state}', content):
                writers.append(str(py_file))
        assert len(writers) <= spec["expected_writers"], (
            f"State '{state}' has {len(writers)} writers: {writers}. "
            f"Expected max {spec['expected_writers']}."
        )
```

### 4.3 Detection runtime (production)

A chaque cycle, comparer les valeurs de toutes les copies d'un meme etat :

```python
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

def audit_state_consistency(state_name: str, sources: dict[str, callable]) -> list[str]:
    """
    Compare values from multiple sources for the same state.
    sources: {"source_name": callable_that_returns_value}
    """
    alerts = []
    values = {}

    for name, reader in sources.items():
        try:
            values[name] = reader()
        except Exception as e:
            alerts.append(f"{state_name}/{name}: read failed ({e})")

    # Check consistency
    unique_values = set(str(v) for v in values.values())
    if len(unique_values) > 1:
        alerts.append(
            f"DESYNC: {state_name} has {len(unique_values)} different values: "
            + ", ".join(f"{k}={v}" for k, v in values.items())
        )

    return alerts
```

## 5. Correction

### 5.1 Fix immediat

Synchroniser toutes les copies depuis la source primaire :

```python
def sync_emergency_state(primary_value: bool):
    """Force all copies to match the primary source."""
    # Primary
    with open("data/emergency_state.json", "w") as f:
        json.dump({"active": primary_value, "synced_at": datetime.now(timezone.utc).isoformat()}, f)

    # Legacy copies
    Path("data/emergency.txt").write_text("ACTIVE" if primary_value else "INACTIVE")

    config = json.loads(Path("data/config.json").read_text())
    config["emergency_active"] = primary_value
    with open("data/config.json", "w") as f:
        json.dump(config, f, indent=2)
```

### 5.2 Fix robuste

Implementer un **StateManager** qui est la seule facon de lire et modifier un etat. Il ecrit dans toutes les copies a chaque modification et lit depuis la source primaire uniquement :

```python
import json
from pathlib import Path
from datetime import datetime, timezone

class StateManager:
    """Single point of read/write for critical state. Syncs all copies."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.primary = self.data_dir / "state.json"
        self.legacy_copies = [
            self.data_dir / "emergency.txt",
        ]

    def get(self, key: str, default=None):
        """Read from primary source only."""
        if self.primary.exists():
            with open(self.primary) as f:
                return json.load(f).get(key, default)
        return default

    def set(self, key: str, value, source: str = "unknown"):
        """Write to primary AND all legacy copies."""
        # Read current state
        state = {}
        if self.primary.exists():
            with open(self.primary) as f:
                state = json.load(f)

        # Update
        state[key] = value
        state["_last_modified_by"] = source
        state["_last_modified_at"] = datetime.now(timezone.utc).isoformat()

        # Write primary
        with open(self.primary, "w") as f:
            json.dump(state, f, indent=2)

        # Sync legacy copies
        self._sync_legacy(state)

    def _sync_legacy(self, state: dict):
        for copy in self.legacy_copies:
            try:
                if copy.suffix == ".txt":
                    val = state.get("emergency_active", False)
                    copy.write_text("ACTIVE" if val else "INACTIVE")
                elif copy.suffix == ".json":
                    with open(copy, "w") as f:
                        json.dump(state, f, indent=2)
            except Exception:
                pass  # Log but don't fail on legacy sync

    def audit(self) -> list[str]:
        """Check all copies are consistent with primary."""
        issues = []
        primary_value = self.get("emergency_active")
        for copy in self.legacy_copies:
            if not copy.exists():
                issues.append(f"{copy.name}: missing")
                continue
            content = copy.read_text().strip()
            copy_value = content.upper() == "ACTIVE"
            if copy_value != primary_value:
                issues.append(f"{copy.name}: {copy_value} vs primary {primary_value}")
        return issues
```

## 6. Prevention architecturale

Le principe fondateur est le **Single Source of Truth** : chaque etat critique n'a qu'**une seule source d'ecriture**. Les autres representations sont des copies read-only synchronisees automatiquement.

En pratique, cela signifie migrer les etats critiques vers une base de donnees (SQLite pour un systeme local, PostgreSQL pour un systeme distribue) et eliminer les fichiers texte/JSON comme stockage d'etat. Les fichiers restent pour la compatibilite avec des composants legacy, mais ils sont generes par le StateManager et ne sont jamais lus comme source de verite.

Un sync periodique (toutes les 60 secondes) re-aligne les copies legacy avec la source primaire. Meme si un composant ecrit directement dans un fichier legacy (contournant le StateManager), le sync suivant ecrasera sa modification. C'est brutal mais efficace.

## 7. Anti-patterns a eviter

1. **Ajouter un nouveau fichier d'etat sans synchroniser les existants.** Chaque nouvelle representation d'un etat existant doit etre geree par le StateManager ou supprimee.

2. **Resoudre un incident en modifiant manuellement un fichier.** Si on change `config.json` a la main mais pas `emergency.txt`, on cree une desync. Toujours passer par un script ou le StateManager.

3. **Lire un fichier sans verifier sa fraicheur.** Un fichier qui n'a pas ete modifie depuis 10 jours est probablement stale. Chaque fichier d'etat devrait contenir un timestamp `last_synced`.

4. **Supposer que l'absence de fichier = etat par defaut.** Un fichier absent peut signifier "jamais cree" (etat initial) ou "supprime par accident" (corruption). Gerer explicitement les deux cas.

5. **Pas d'audit de coherence periodique.** Meme avec un StateManager, les fichiers peuvent diverger (ecriture directe, crash mid-sync). Un audit toutes les minutes qui compare les copies detecte les divergences en < 60 secondes.

## 8. Cas limites et variantes

**Variante 1 : Config partagee entre agents.** Un fichier `config.json` lu par 4 agents, chacun modifiant des sections differentes. Sans lock, l'agent A peut ecraser les modifications de l'agent B. C'est une desync intra-fichier, pas inter-fichier.

**Variante 2 : Cache applicatif desynchronise.** L'etat est correct dans la DB mais le cache en memoire d'un agent contient l'ancienne valeur. Le cache n'est pas invalide apres une modification. Solution : TTL court sur le cache ou invalidation explicite.

**Variante 3 : Etat distribue entre machines.** Deux instances du meme agent tournent sur deux VPS. Chacune a sa propre copie locale de l'etat. Apres une modification sur l'instance A, l'instance B garde l'ancienne valeur. Solution : stockage centralise (Redis, Consul, ou DB).

**Variante 4 : Fichier d'etat stale apres crash.** Le StateManager ecrit dans le fichier primaire, crashe avant de synchroniser les copies legacy. Au restart, les copies sont desynchronisees. Solution : le sync doit etre la premiere action au boot, avant toute lecture.

## 9. Checklist d'audit

- [ ] Chaque etat critique a une source primaire designee et documentee
- [ ] Aucun composant ne lit un fichier d'etat legacy directement (tous passent par le StateManager)
- [ ] Un sync periodique re-aligne les copies legacy avec la source primaire
- [ ] Chaque fichier d'etat contient un timestamp `last_synced` ou `last_modified`
- [ ] Un audit de coherence compare toutes les copies a chaque cycle et alerte si divergence

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 04 — Multi-File State Desync](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-04)
- Patterns connexes : #11 (Race Condition on Shared File — ecriture concurrente dans le meme fichier), #08 (Data Pipeline Freeze — un fichier stale peut etre un symptome de desync), #01 (Timezone Mismatch — les timestamps dans les fichiers desynchronises peuvent etre en timezones differentes)
- Lectures recommandees :
  - "Designing Data-Intensive Applications" (Martin Kleppmann), chapitre 5 sur la replication et la consistance
  - Documentation HashiCorp Consul sur le consensus distribue — les memes problemes a plus grande echelle
