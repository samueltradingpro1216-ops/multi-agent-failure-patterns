"""
Pattern 11 — Race Condition on Shared File
Demontre comment deux agents qui lisent-modifient-ecrivent le meme fichier
sans lock perdent des mises a jour.

Usage: python example.py
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path


# === LE BUG ===

class BuggySharedTracker:
    """
    BUG: read-modify-write sans lock.
    Deux agents qui incrementent en parallele perdent des updates.
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        # Initialiser
        with open(filepath, "w") as f:
            json.dump({"count": 0, "agents": {}}, f)

    def increment(self, agent_id: str):
        """Incrementer le compteur — PAS thread-safe."""
        # Read
        with open(self.filepath) as f:
            data = json.load(f)

        # Modify
        data["count"] += 1
        data["agents"][agent_id] = data["agents"].get(agent_id, 0) + 1

        # Simuler un delai (appel LLM, traitement, etc.)
        time.sleep(0.001)

        # Write
        with open(self.filepath, "w") as f:
            json.dump(data, f)

    def get_count(self) -> int:
        with open(self.filepath) as f:
            return json.load(f)["count"]


# === LA CORRECTION ===

class FixedSharedTracker:
    """
    FIX: file lock autour du read-modify-write.
    Utilise un threading.Lock pour la demo (en prod: fcntl.flock ou portalocker).
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.lock = threading.Lock()
        # Initialiser
        with open(filepath, "w") as f:
            json.dump({"count": 0, "agents": {}}, f)

    def increment(self, agent_id: str):
        """Incrementer le compteur — thread-safe avec lock."""
        with self.lock:
            # Read
            with open(self.filepath) as f:
                data = json.load(f)

            # Modify
            data["count"] += 1
            data["agents"][agent_id] = data["agents"].get(agent_id, 0) + 1

            # Simuler un delai
            time.sleep(0.001)

            # Write
            with open(self.filepath, "w") as f:
                json.dump(data, f)

    def get_count(self) -> int:
        with self.lock:
            with open(self.filepath) as f:
                return json.load(f)["count"]


# === ECRITURE ATOMIQUE ===

class AtomicSharedTracker:
    """
    Alternative: ecriture atomique (write-to-temp + rename).
    Plus robuste que le file lock contre les crashes.
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.lock = threading.Lock()
        self._atomic_write({"count": 0, "agents": {}})

    def _atomic_write(self, data: dict):
        """Ecriture atomique: temp file + rename."""
        dir_name = os.path.dirname(self.filepath) or "."
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, self.filepath)  # Atomique sur la plupart des OS
        except Exception:
            os.unlink(tmp_path)
            raise

    def increment(self, agent_id: str):
        with self.lock:
            with open(self.filepath) as f:
                data = json.load(f)

            data["count"] += 1
            data["agents"][agent_id] = data["agents"].get(agent_id, 0) + 1

            time.sleep(0.001)

            self._atomic_write(data)

    def get_count(self) -> int:
        with open(self.filepath) as f:
            return json.load(f)["count"]


# === DETECTION ===

def detect_race_condition(
    expected_total: int,
    actual_total: int,
    per_agent: dict[str, int],
) -> dict:
    """
    Detecte une race condition en comparant les totaux.
    """
    agent_sum = sum(per_agent.values())
    lost = expected_total - actual_total

    if lost > 0:
        return {
            "race_condition": True,
            "expected": expected_total,
            "actual": actual_total,
            "lost_updates": lost,
            "loss_rate": round(lost / expected_total * 100, 1),
            "message": (
                f"Race condition: {lost} updates perdus "
                f"({lost/expected_total:.1%}). "
                f"Expected={expected_total}, actual={actual_total}"
            ),
        }

    return {"race_condition": False, "expected": expected_total, "actual": actual_total}


# === DEMONSTRATION ===

def run_agents(tracker, n_agents: int, increments_per_agent: int) -> int:
    """Lance n_agents en parallele, chacun incrementant le compteur."""
    threads = []
    for i in range(n_agents):
        agent_id = f"agent-{i}"
        t = threading.Thread(
            target=lambda aid: [tracker.increment(aid) for _ in range(increments_per_agent)],
            args=(agent_id,),
        )
        threads.append(t)

    # Demarrer tous les threads
    for t in threads:
        t.start()

    # Attendre la fin
    for t in threads:
        t.join()

    return tracker.get_count()


if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 11 -- Race Condition on Shared File")
    print("=" * 60)

    n_agents = 4
    increments = 25
    expected = n_agents * increments

    with tempfile.TemporaryDirectory() as tmp:

        # --- Version buggee ---
        print(f"\n--- Version BUGGEE ({n_agents} agents x {increments} increments) ---")
        buggy_file = os.path.join(tmp, "buggy.json")
        buggy = BuggySharedTracker(buggy_file)

        actual = run_agents(buggy, n_agents, increments)
        lost = expected - actual

        print(f"  Attendu: {expected}")
        print(f"  Reel:    {actual}")
        print(f"  Perdu:   {lost} updates ({lost/expected:.1%})")

        result = detect_race_condition(expected, actual, {})
        if result["race_condition"]:
            print(f"  -> {result['message']}")

        # --- Version corrigee (lock) ---
        print(f"\n--- Version CORRIGEE: file lock ---")
        fixed_file = os.path.join(tmp, "fixed.json")
        fixed = FixedSharedTracker(fixed_file)

        actual = run_agents(fixed, n_agents, increments)
        lost = expected - actual

        print(f"  Attendu: {expected}")
        print(f"  Reel:    {actual}")
        print(f"  Perdu:   {lost} updates ({lost/expected:.1%})")

        # --- Version atomique ---
        print(f"\n--- Version CORRIGEE: ecriture atomique ---")
        atomic_file = os.path.join(tmp, "atomic.json")
        atomic = AtomicSharedTracker(atomic_file)

        actual = run_agents(atomic, n_agents, increments)
        lost = expected - actual

        print(f"  Attendu: {expected}")
        print(f"  Reel:    {actual}")
        print(f"  Perdu:   {lost} updates ({lost/expected:.1%})")

    # --- Comparaison ---
    print(f"\n--- Resume ---")
    print(f"  Sans lock:          updates perdus (race condition)")
    print(f"  Avec lock:          0 updates perdus")
    print(f"  Ecriture atomique:  0 updates perdus + resilient aux crashes")
