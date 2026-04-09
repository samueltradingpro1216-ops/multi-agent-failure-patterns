"""
Pattern 04 — Killswitch Multi-File Desync
Démontre comment un état stocké en plusieurs endroits
peut diverger et créer de la confusion.

Usage: python example.py
"""
import json
import tempfile
import os
from datetime import datetime, timezone
from pathlib import Path


# --- Le bug ---

class BuggyKillswitch:
    """
    BUG: 3 fichiers, 3 composants, 0 synchronisation.
    Chaque composant écrit dans "son" fichier et ne lit que celui-là.
    """

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)

    def ea_activate(self):
        """L'EA écrit dans killswitch.txt (format texte simple)."""
        (self.data_dir / "killswitch.txt").write_text("ACTIVE")

    def supervisor_check(self) -> bool:
        """Le superviseur lit tuning.json (champ killswitch_global)."""
        tuning_file = self.data_dir / "tuning.json"
        if tuning_file.exists():
            with open(tuning_file) as f:
                return json.load(f).get("killswitch_global", False)
        return False

    def dashboard_check(self) -> dict:
        """Le dashboard lit killswitch_state.json."""
        state_file = self.data_dir / "killswitch_state.json"
        if state_file.exists():
            with open(state_file) as f:
                return json.load(f)
        return None  # Crash si le fichier n'existe pas


# --- La correction ---

class FixedKillswitch:
    """
    FIX: source unique de vérité + sync vers tous les fichiers.
    Tous les composants lisent la même source.
    """

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.primary = self.data_dir / "killswitch_state.json"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def activate(self, reason: str, activated_by: str):
        """Active et synchronise TOUS les fichiers."""
        state = {
            "active": True,
            "reason": reason,
            "activated_by": activated_by,
            "activated_at": self._now(),
        }
        self._write_all(state)

    def deactivate(self, deactivated_by: str):
        """Désactive et synchronise TOUS les fichiers."""
        state = {
            "active": False,
            "reason": f"Deactivated by {deactivated_by}",
            "activated_by": None,
            "activated_at": None,
        }
        self._write_all(state)

    def is_active(self) -> bool:
        """Source unique de lecture."""
        if self.primary.exists():
            with open(self.primary) as f:
                return json.load(f).get("active", False)
        return False

    def _write_all(self, state: dict):
        """Écrit dans la source primaire ET les fichiers legacy."""
        # Source primaire
        with open(self.primary, "w") as f:
            json.dump(state, f, indent=2)

        # Legacy: killswitch.txt (pour l'EA)
        (self.data_dir / "killswitch.txt").write_text(
            "ACTIVE" if state["active"] else "INACTIVE"
        )

        # Legacy: tuning.json (mise à jour du champ)
        tuning_file = self.data_dir / "tuning.json"
        tuning = {}
        if tuning_file.exists():
            with open(tuning_file) as f:
                tuning = json.load(f)
        tuning["killswitch_global"] = state["active"]
        with open(tuning_file, "w") as f:
            json.dump(tuning, f, indent=2)

    def audit(self) -> list[str]:
        """Vérifie que tous les fichiers sont cohérents."""
        expected = self.is_active()
        issues = []

        # Vérifier killswitch.txt
        txt_file = self.data_dir / "killswitch.txt"
        if txt_file.exists():
            actual = txt_file.read_text().strip().upper() == "ACTIVE"
            if actual != expected:
                issues.append(f"killswitch.txt={actual} vs primary={expected}")
        else:
            issues.append("killswitch.txt manquant")

        # Vérifier tuning.json
        tuning_file = self.data_dir / "tuning.json"
        if tuning_file.exists():
            with open(tuning_file) as f:
                actual = json.load(f).get("killswitch_global", False)
            if actual != expected:
                issues.append(f"tuning.json={actual} vs primary={expected}")

        return issues


# --- Démonstration ---

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 04 — Killswitch Multi-File Desync")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        # Initialiser tuning.json
        with open(os.path.join(tmp, "tuning.json"), "w") as f:
            json.dump({"killswitch_global": False, "risk": 0.25}, f)

        # --- Version buggée ---
        print(f"\n--- Version BUGGÉE ---")
        buggy = BuggyKillswitch(tmp)

        # L'EA active le killswitch
        buggy.ea_activate()
        print(f"  EA active le killswitch dans killswitch.txt")

        # Le superviseur ne le sait pas
        print(f"  Superviseur check (tuning.json): active={buggy.supervisor_check()}")

        # Le dashboard crash
        result = buggy.dashboard_check()
        print(f"  Dashboard check (killswitch_state.json): {result}")

        print(f"  → 3 réponses différentes pour la même question!")

        # --- Version corrigée ---
        print(f"\n--- Version CORRIGÉE ---")
        fixed = FixedKillswitch(tmp)

        # Activer
        fixed.activate(reason="Drawdown > 10%", activated_by="emergency_agent")
        print(f"  Killswitch activé")
        print(f"  is_active(): {fixed.is_active()}")

        # Vérifier que TOUS les fichiers sont cohérents
        print(f"  killswitch.txt: {(Path(tmp) / 'killswitch.txt').read_text()}")
        with open(os.path.join(tmp, "tuning.json")) as f:
            print(f"  tuning.json killswitch_global: {json.load(f)['killswitch_global']}")
        with open(os.path.join(tmp, "killswitch_state.json")) as f:
            state = json.load(f)
            print(f"  killswitch_state.json active: {state['active']}")

        # Audit
        issues = fixed.audit()
        print(f"\n  Audit: {'OK (0 issues)' if not issues else issues}")

        # Désactiver
        fixed.deactivate(deactivated_by="admin")
        print(f"\n  Killswitch désactivé")
        print(f"  is_active(): {fixed.is_active()}")
        issues = fixed.audit()
        print(f"  Audit: {'OK (0 issues)' if not issues else issues}")
