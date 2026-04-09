# Pattern N°08 — Data Pipeline Freeze

**Categorie :** I/O & Persistence
**Severite :** High
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 5 a 30 jours (les donnees gelees produisent des resultats plausibles mais obsoletes — le probleme n'est pas cherche tant que les resultats semblent raisonnables)

---

## 1. Symptome observable

Un dashboard, un rapport, ou un module d'analyse affiche des donnees qui n'ont pas change depuis des jours ou des semaines. Les metriques derivees (moyennes, tendances, scores) sont toutes **stables** — ce qui semble positif mais est en realite le signe que les donnees sous-jacentes ne sont plus alimentees.

Le symptome trompeur : le consommateur ne crashe pas. Il lit un fichier ou une table qui **existe** et contient des donnees valides — elles sont juste anciennes. Les calculs s'executent normalement, les rapports sont generes a l'heure, et les metriques sont dans des plages normales. C'est la **date** des donnees qui est fausse, pas les donnees elles-memes.

Le producteur, de son cote, fonctionne aussi normalement. Il ecrit ses donnees — mais dans un **format ou un chemin different** de ce que le consommateur attend. Les deux composants tournent en parallele, sans erreur, sans se parler.

## 2. Histoire vecue (anonymisee)

Un systeme de monitoring multi-agents analysait les performances de ses agents toutes les heures. Le module d'analyse lisait les resultats depuis un fichier JSONL. Le module producteur, apres une refonte, est passe au format CSV sans modifier le consommateur.

Le fichier JSONL existait toujours — il contenait les donnees d'avant la refonte. Le consommateur le lisait normalement, calculait des metriques, et produisait des rapports. Pendant 11 jours, les rapports affichaient des metriques basees sur des donnees de 11 jours. L'equipe n'a rien remarque car les metriques etaient dans des plages normales (elles correspondaient a la realite d'il y a 11 jours).

Le bug a ete decouvert quand un membre de l'equipe a remarque que le nombre total de resultats n'avait pas augmente en 11 jours. En verifiant, le fichier JSONL n'avait pas ete modifie depuis la date de la refonte. Le producteur ecrivait dans un CSV que personne ne lisait.

## 3. Cause racine technique

Le bug se produit quand le **contrat implicite** entre un producteur et un consommateur est rompu sans que les deux parties le sachent :

```
AVANT la refonte:
    Producteur → data/results.jsonl → Consommateur
    (contrat: format JSONL, 1 ligne par resultat)

APRES la refonte:
    Producteur → data/results.csv    → (personne ne lit)
    Consommateur → data/results.jsonl → (fichier gele, derniere modification: 11 jours)
```

Le contrat est "implicite" parce qu'il n'est encode nulle part. Il n'y a pas de schema versionne, pas de verification de format, pas de test d'integration qui valide le flux complet. Le producteur assume que le consommateur lira le CSV. Le consommateur assume que le JSONL sera alimente.

Les causes courantes de rupture de contrat :
- Changement de format de fichier (JSONL → CSV, JSON → Parquet)
- Changement de chemin ou de nom de fichier
- Changement de schema (nouvelles colonnes, colonnes renommees)
- Changement de frequence d'ecriture (horaire → quotidien)
- Migration de stockage (fichier local → base de donnees → API)

## 4. Detection

### 4.1 Detection manuelle (audit code)

Pour chaque pipeline de donnees, verifier que producteur et consommateur pointent vers la meme source :

```bash
# Lister tous les chemins de fichiers ecrits
grep -rn "open.*'w'\|write_text\|to_csv\|to_json" --include="*.py" | grep -v test

# Lister tous les chemins de fichiers lus
grep -rn "open.*'r'\|read_text\|read_csv\|read_json\|load" --include="*.py" | grep -v test

# Croiser les deux listes — les chemins ecrits mais jamais lus sont suspects
# Les chemins lus mais jamais ecrits sont des pipelines gelees potentielles
```

### 4.2 Detection automatisee (CI/CD)

Documenter les pipelines et tester le flux complet a chaque deploiement :

```python
# test_pipeline_contract.py
PIPELINES = [
    {
        "name": "agent_results",
        "producer_writes": "data/results.jsonl",
        "consumer_reads": "data/results.jsonl",
        "format": "jsonl",
    },
    {
        "name": "metrics_report",
        "producer_writes": "data/metrics.json",
        "consumer_reads": "data/metrics.json",
        "format": "json",
    },
]

def test_pipeline_contract_alignment():
    """Verify producer and consumer agree on file path and format."""
    for pipeline in PIPELINES:
        assert pipeline["producer_writes"] == pipeline["consumer_reads"], (
            f"Pipeline '{pipeline['name']}': producer writes to "
            f"'{pipeline['producer_writes']}' but consumer reads from "
            f"'{pipeline['consumer_reads']}'"
        )
```

### 4.3 Detection runtime (production)

Verifier la fraicheur des donnees a chaque lecture :

```python
import os
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

class FreshnessChecker:
    """Refuses to read data older than max_age."""

    def __init__(self, max_age_hours: int = 24):
        self.max_age = timedelta(hours=max_age_hours)

    def check(self, filepath: str) -> dict:
        path = Path(filepath)

        if not path.exists():
            return {"fresh": False, "reason": "file does not exist"}

        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age = datetime.now(timezone.utc) - mtime

        if age > self.max_age:
            return {
                "fresh": False,
                "reason": f"file is {age.total_seconds()/3600:.1f}h old (max {self.max_age})",
                "last_modified": mtime.isoformat(),
            }

        return {"fresh": True, "age_hours": age.total_seconds() / 3600}

# Usage:
checker = FreshnessChecker(max_age_hours=2)
result = checker.check("data/results.jsonl")
if not result["fresh"]:
    logging.critical(f"PIPELINE FREEZE: {result['reason']}")
    send_alert(f"Data pipeline frozen: results.jsonl {result['reason']}")
```

## 5. Correction

### 5.1 Fix immediat

Realigner producteur et consommateur sur le meme chemin et format :

```python
# Identifier le format actuel du producteur
# Si producteur ecrit en CSV, modifier le consommateur pour lire en CSV :
import csv

def read_results(filepath: str) -> list[dict]:
    """Read results — supports both JSONL and CSV for migration."""
    if filepath.endswith(".csv"):
        with open(filepath) as f:
            return list(csv.DictReader(f))
    elif filepath.endswith(".jsonl"):
        import json
        with open(filepath) as f:
            return [json.loads(line) for line in f if line.strip()]
    else:
        raise ValueError(f"Unknown format: {filepath}")
```

### 5.2 Fix robuste

Ajouter un contrat explicite entre producteur et consommateur avec versioning :

```python
"""pipeline_contract.py — Explicit contract between producer and consumer."""
import json
from pathlib import Path
from datetime import datetime, timezone

class PipelineContract:
    """Defines and validates the contract between a producer and a consumer."""

    def __init__(self, data_path: str, format: str, version: str):
        self.data_path = Path(data_path)
        self.meta_path = self.data_path.with_suffix(".meta.json")
        self.format = format
        self.version = version

    def write_meta(self, record_count: int):
        """Producer writes metadata after each data update."""
        meta = {
            "format": self.format,
            "version": self.version,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "record_count": record_count,
            "data_file": self.data_path.name,
        }
        with open(self.meta_path, "w") as f:
            json.dump(meta, f, indent=2)

    def validate(self, expected_version: str, max_age_hours: int = 24) -> dict:
        """Consumer validates the contract before reading."""
        if not self.meta_path.exists():
            return {"valid": False, "reason": "No metadata file — producer may have changed path"}

        with open(self.meta_path) as f:
            meta = json.load(f)

        if meta["version"] != expected_version:
            return {"valid": False, "reason": f"Version mismatch: {meta['version']} vs {expected_version}"}

        last_updated = datetime.fromisoformat(meta["last_updated"])
        age = datetime.now(timezone.utc) - last_updated
        if age.total_seconds() > max_age_hours * 3600:
            return {"valid": False, "reason": f"Data stale: {age.total_seconds()/3600:.1f}h old"}

        return {"valid": True, "records": meta["record_count"], "age_hours": age.total_seconds() / 3600}
```

## 6. Prevention architecturale

La prevention repose sur trois principes :

**1. Contrat explicite.** Chaque pipeline a un fichier de metadata (`.meta.json`) qui definit le format, la version, et le timestamp de derniere mise a jour. Le consommateur valide le contrat avant chaque lecture. Si le format ou la version ne correspond pas, il refuse de lire et alerte.

**2. Test d'integration end-to-end.** A chaque deploiement, un test ecrit des donnees via le producteur, les lit via le consommateur, et verifie que le resultat est correct. Ce test detecte immediatement tout changement de format, chemin, ou schema.

**3. Monitoring de fraicheur.** Un health check periodique verifie que chaque fichier de donnees a ete mis a jour dans les N dernieres heures. Si un fichier est stale, c'est une alerte immediate — pas un rapport quotidien.

## 7. Anti-patterns a eviter

1. **Changer le format du producteur sans toucher le consommateur.** Toute modification du producteur doit inclure la verification que le consommateur est compatible.

2. **Pas de timestamp dans les fichiers de donnees.** Sans timestamp, il est impossible de distinguer "donnees fraiches" de "donnees gelees de 11 jours".

3. **Lire silencieusement un fichier vide comme "pas de donnees".** Un fichier vide peut signifier "rien ne s'est passe" (normal) ou "le producteur ecrit ailleurs" (bug). Distinguer les deux avec un metadata file.

4. **Contrat de pipeline implicite.** Si le format n'est documente nulle part, tout changement est un breaking change invisible. Documenter explicitement : format, schema, frequence, chemin.

5. **Tester producteur et consommateur separement.** Les tests unitaires du producteur verifient qu'il ecrit correctement. Les tests du consommateur verifient qu'il lit correctement. Aucun ne verifie qu'ils sont compatibles ensemble.

## 8. Cas limites et variantes

**Variante 1 : Schema drift.** Le producteur ajoute une colonne au CSV. Le consommateur ignore les colonnes inconnues mais crashe si une colonne attendue est renommee ou supprimee. Le fichier est mis a jour (pas gele) mais les donnees sont incompatibles.

**Variante 2 : Pipeline freeze partielle.** Le producteur ecrit dans le bon fichier mais a un bug qui fait que les nouvelles lignes sont identiques aux anciennes (bug dans le generateur de donnees). Le fichier est techniquement "a jour" (modifie recemment) mais les donnees ne changent pas.

**Variante 3 : Changement de base de donnees.** Le producteur migre de fichier vers SQLite. L'ancien fichier n'est plus alimente mais le consommateur continue de le lire. C'est le meme pattern que le changement de format, applique a un changement de stockage.

**Variante 4 : Pipeline gelée par un lock.** Le producteur essaie d'ecrire mais un lock file (d'un processus crashe) l'en empeche. Il echoue silencieusement et le fichier n'est jamais mis a jour. Le consommateur lit les anciennes donnees.

## 9. Checklist d'audit

- [ ] Chaque pipeline producteur → consommateur est documentee (chemin, format, frequence)
- [ ] Chaque fichier de donnees a un metadata file avec format, version, et timestamp
- [ ] Le consommateur verifie la fraicheur avant chaque lecture
- [ ] Un test d'integration valide le flux complet producteur → consommateur
- [ ] Le monitoring alerte si un fichier n'a pas ete mis a jour depuis > N heures

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 08 — Data Pipeline Freeze](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-08)
- Patterns connexes : #04 (Multi-File State Desync — un fichier gele est une forme de desync), #01 (Timezone Mismatch — un timestamp mal interprete peut masquer une pipeline freeze)
- Lectures recommandees :
  - "Designing Data-Intensive Applications" (Martin Kleppmann), chapitre 10 sur le batch processing et les contrats de schema
  - Schema Registry de Confluent — le concept de contrat versionne entre producteurs et consommateurs applique a l'echelle
