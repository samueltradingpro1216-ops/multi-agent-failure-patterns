"""
Pattern 08 — Data Pipeline Freeze
Demontre comment un producteur qui change de format peut geler
un consommateur sans qu'aucune erreur ne soit levee.

Usage: python example.py
"""
import json
import csv
import io
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


# === LE BUG ===

class BuggyProducer:
    """Producteur qui ecrit en CSV (nouveau format apres une refonte)."""

    def __init__(self, data_dir: str):
        self.output = Path(data_dir) / "results.csv"

    def write_result(self, result: dict):
        """Ecrit un resultat en CSV."""
        file_exists = self.output.exists()
        with open(self.output, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=result.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(result)


class BuggyConsumer:
    """
    BUG: Consommateur qui lit le JSONL (ancien format).
    Le fichier JSONL existe mais n'est plus alimente.
    Le consommateur lit les anciennes donnees sans erreur.
    """

    def __init__(self, data_dir: str):
        self.input_file = Path(data_dir) / "results.jsonl"

    def read_results(self) -> list[dict]:
        """Lit les resultats depuis le JSONL — fichier gele."""
        results = []
        if self.input_file.exists():
            with open(self.input_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        results.append(json.loads(line))
        return results  # Retourne les anciennes donnees ou liste vide


# === LA CORRECTION ===

class FixedProducer:
    """Producteur avec metadata de fraicheur."""

    def __init__(self, data_dir: str, format_version: str = "2.0"):
        self.data_dir = Path(data_dir)
        self.output = self.data_dir / "results.jsonl"
        self.meta_file = self.data_dir / "results.meta.json"
        self.format_version = format_version

    def write_result(self, result: dict):
        """Ecrit un resultat avec mise a jour des metadata."""
        with open(self.output, "a") as f:
            f.write(json.dumps(result) + "\n")

        # Mettre a jour les metadata
        meta = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "format": "jsonl",
            "format_version": self.format_version,
            "file": str(self.output.name),
            "record_count": sum(1 for _ in open(self.output)),
        }
        with open(self.meta_file, "w") as f:
            json.dump(meta, f, indent=2)


class FixedConsumer:
    """
    FIX: Consommateur avec verification de fraicheur et de format.
    Refuse les donnees trop vieilles ou au mauvais format.
    """

    def __init__(self, data_dir: str, expected_version: str = "2.0"):
        self.data_dir = Path(data_dir)
        self.input_file = self.data_dir / "results.jsonl"
        self.meta_file = self.data_dir / "results.meta.json"
        self.expected_version = expected_version

    def check_pipeline_health(self, max_age_hours: int = 24) -> dict:
        """Verifie que la pipeline est saine avant de lire."""
        if not self.meta_file.exists():
            return {"healthy": False, "reason": "Metadata file missing"}

        with open(self.meta_file) as f:
            meta = json.load(f)

        # Verifier la fraicheur
        last_updated = datetime.fromisoformat(meta["last_updated"])
        age = datetime.now(timezone.utc) - last_updated
        age_hours = age.total_seconds() / 3600

        if age_hours > max_age_hours:
            return {
                "healthy": False,
                "reason": f"Data stale: {age_hours:.1f}h old (max {max_age_hours}h)",
                "last_updated": meta["last_updated"],
            }

        # Verifier le format
        if meta.get("format_version") != self.expected_version:
            return {
                "healthy": False,
                "reason": (
                    f"Format mismatch: producer={meta.get('format_version')} "
                    f"consumer={self.expected_version}"
                ),
            }

        return {
            "healthy": True,
            "age_hours": round(age_hours, 1),
            "records": meta.get("record_count", 0),
        }

    def read_results(self, max_age_hours: int = 24) -> list[dict]:
        """Lit les resultats apres verification de sante."""
        health = self.check_pipeline_health(max_age_hours)

        if not health["healthy"]:
            print(f"  PIPELINE UNHEALTHY: {health['reason']}")
            return []

        results = []
        if self.input_file.exists():
            with open(self.input_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        results.append(json.loads(line))

        return results


# === DETECTION ===

def detect_pipeline_freeze(
    data_dir: str,
    expected_files: list[str],
    max_age_hours: int = 24
) -> list[dict]:
    """
    Detecte les fichiers de donnees qui ne sont plus alimentes.
    """
    alerts = []
    now = datetime.now(timezone.utc)

    for filename in expected_files:
        filepath = Path(data_dir) / filename

        if not filepath.exists():
            alerts.append({
                "file": filename,
                "status": "MISSING",
                "message": f"{filename}: fichier absent",
            })
            continue

        # Verifier l'age du fichier
        mtime = datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
        age = now - mtime
        age_hours = age.total_seconds() / 3600

        if age_hours > max_age_hours:
            alerts.append({
                "file": filename,
                "status": "STALE",
                "age_hours": round(age_hours, 1),
                "message": f"{filename}: stale ({age_hours:.0f}h, max {max_age_hours}h)",
            })

        # Verifier si le fichier est vide
        if filepath.stat().st_size == 0:
            alerts.append({
                "file": filename,
                "status": "EMPTY",
                "message": f"{filename}: fichier vide (0 bytes)",
            })

    return alerts


# === DEMONSTRATION ===

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 08 -- Data Pipeline Freeze")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:

        # --- Version buggee ---
        print("\n--- Version BUGGEE ---")

        # Le producteur ecrit en CSV (nouveau format)
        producer = BuggyProducer(tmp)
        producer.write_result({"id": 1, "score": 85, "status": "ok"})
        producer.write_result({"id": 2, "score": 72, "status": "ok"})
        print(f"  Producteur: 2 resultats ecrits en CSV -> results.csv")

        # Creer un vieux JSONL (ancien format, gele)
        old_jsonl = Path(tmp) / "results.jsonl"
        old_jsonl.write_text('{"id": 0, "score": 50}\n')
        # Simuler un fichier vieux de 11 jours
        old_time = (datetime.now() - timedelta(days=11)).timestamp()
        os.utime(old_jsonl, (old_time, old_time))

        # Le consommateur lit le JSONL
        consumer = BuggyConsumer(tmp)
        results = consumer.read_results()
        print(f"  Consommateur: {len(results)} resultat(s) lus depuis JSONL")
        print(f"  -> Donnees de 11 jours, pas les 2 nouveaux resultats!")
        print(f"  -> Aucune erreur, aucun warning")

        # --- Version corrigee ---
        print("\n--- Version CORRIGEE ---")

        # Nouveau producteur avec metadata
        fixed_producer = FixedProducer(tmp, format_version="2.0")
        fixed_jsonl = Path(tmp) / "results.jsonl"
        # Reset le fichier
        fixed_jsonl.write_text("")
        fixed_producer.write_result({"id": 1, "score": 85, "status": "ok"})
        fixed_producer.write_result({"id": 2, "score": 72, "status": "ok"})
        print(f"  Producteur: 2 resultats ecrits en JSONL + metadata")

        # Consommateur avec verification
        fixed_consumer = FixedConsumer(tmp, expected_version="2.0")

        # Health check
        health = fixed_consumer.check_pipeline_health(max_age_hours=24)
        print(f"  Pipeline health: {health}")

        # Lire les donnees
        results = fixed_consumer.read_results(max_age_hours=24)
        print(f"  Resultats lus: {len(results)}")

        # --- Tester avec un format mismatch ---
        print("\n--- Test: format mismatch ---")
        mismatched_consumer = FixedConsumer(tmp, expected_version="3.0")
        health = mismatched_consumer.check_pipeline_health()
        print(f"  Health (version 3.0 attendue): {health}")

        # --- Detection de freeze ---
        print("\n--- Detection de pipeline freeze ---")
        # Creer un fichier stale
        stale_file = Path(tmp) / "metrics.json"
        stale_file.write_text('{"stale": true}')
        stale_time = (datetime.now() - timedelta(days=5)).timestamp()
        os.utime(stale_file, (stale_time, stale_time))

        alerts = detect_pipeline_freeze(
            tmp,
            expected_files=["results.jsonl", "metrics.json", "missing.db"],
            max_age_hours=24
        )
        for alert in alerts:
            print(f"  [{alert['status']}] {alert['message']}")
