"""
Pattern 09 — Agent Infinite Loop
Demontre comment un agent recursif sans guard peut boucler indefiniment,
et 3 mecanismes pour l'empecher.

Usage: python example.py
"""
import time
from datetime import datetime, timedelta
from collections import defaultdict


# === LE BUG ===

class BuggyReflectionAgent:
    """
    BUG: l'agent re-evalue sa propre reponse en boucle.
    Si la reponse n'est "jamais assez bonne", il boucle indefiniment.
    """

    def __init__(self):
        self.call_count = 0

    def run(self, task: str) -> str:
        self.call_count += 1

        # Simuler: generer une reponse
        response = f"Response to: {task}"
        quality = self._evaluate(response)

        if quality < 0.9:  # Seuil jamais atteint
            # BUG: re-invocation sans limite de profondeur
            return self.run(task)

        return response

    def _evaluate(self, response: str) -> float:
        """Simule une evaluation qui ne donne jamais > 0.85."""
        return 0.7  # Toujours en dessous du seuil


# === LA CORRECTION ===

class FixedReflectionAgent:
    """
    FIX: 3 mecanismes anti-boucle.
    1. Max depth (compteur de profondeur)
    2. Timeout global
    3. Deduplication par hash d'input
    """

    def __init__(self, max_depth: int = 3, timeout_seconds: float = 5.0):
        self.max_depth = max_depth
        self.timeout_seconds = timeout_seconds
        self.call_count = 0
        self.start_time = None
        self.seen_inputs = set()

    def run(self, task: str, _depth: int = 0) -> str:
        self.call_count += 1

        # Guard 1: max depth
        if _depth >= self.max_depth:
            return f"[MAX_DEPTH={self.max_depth}] Best effort: {task}"

        # Guard 2: timeout global
        if self.start_time is None:
            self.start_time = time.monotonic()
        elapsed = time.monotonic() - self.start_time
        if elapsed > self.timeout_seconds:
            return f"[TIMEOUT={self.timeout_seconds}s] Best effort: {task}"

        # Guard 3: deduplication
        input_hash = hash(task)
        if input_hash in self.seen_inputs and _depth > 0:
            return f"[DEDUP] Same input, stopping: {task}"
        self.seen_inputs.add(input_hash)

        # Logique normale
        response = f"Response to: {task}"
        quality = self._evaluate(response)

        if quality < 0.9:
            # Re-invocation AVEC depth increment
            return self.run(task, _depth=_depth + 1)

        return response

    def _evaluate(self, response: str) -> float:
        return 0.7  # Simule une evaluation toujours insuffisante


# === CIRCUIT BREAKER ===

class CircuitBreaker:
    """
    Coupe un agent apres N echecs consecutifs.
    L'agent est desactive pendant cooldown_seconds.
    """

    def __init__(self, max_failures: int = 5, cooldown_seconds: float = 60.0):
        self.max_failures = max_failures
        self.cooldown_seconds = cooldown_seconds
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED = ok, OPEN = bloque

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.max_failures:
            self.state = "OPEN"

    def record_success(self):
        self.failure_count = 0
        self.state = "CLOSED"

    def can_execute(self) -> bool:
        if self.state == "CLOSED":
            return True

        # Verifier si le cooldown est passe
        if self.last_failure_time:
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed >= self.cooldown_seconds:
                self.state = "HALF_OPEN"
                return True  # Un essai

        return False

    def __repr__(self):
        return f"CircuitBreaker(state={self.state}, failures={self.failure_count}/{self.max_failures})"


# === INVOCATION MONITOR ===

class InvocationMonitor:
    """
    Compte les invocations par agent dans une fenetre glissante.
    Alerte si un agent depasse le seuil.
    """

    def __init__(self, window_seconds: int = 60, max_per_window: int = 10):
        self.window_seconds = window_seconds
        self.max_per_window = max_per_window
        self.history = defaultdict(list)

    def record(self, agent_id: str) -> bool:
        """
        Enregistre une invocation. Retourne False si le seuil est depasse.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Nettoyer l'historique
        self.history[agent_id] = [
            t for t in self.history[agent_id] if t > cutoff
        ]

        # Verifier le seuil
        if len(self.history[agent_id]) >= self.max_per_window:
            return False  # Seuil depasse

        self.history[agent_id].append(now)
        return True

    def get_count(self, agent_id: str) -> int:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        return sum(1 for t in self.history[agent_id] if t > cutoff)


# === DEMONSTRATION ===

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 09 -- Agent Infinite Loop")
    print("=" * 60)

    # --- Version buggee (limitee pour la demo) ---
    print("\n--- Version BUGGEE ---")
    buggy = BuggyReflectionAgent()

    # On ne peut pas vraiment lancer la boucle infinie,
    # donc on simule avec un compteur max
    import sys
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(50)  # Limiter pour la demo

    try:
        buggy.run("Analyse this data")
    except RecursionError:
        pass

    sys.setrecursionlimit(old_limit)
    print(f"  Appels avant RecursionError: {buggy.call_count}")
    print(f"  Sans limite Python, ca bouclerait indefiniment")
    print(f"  Avec des appels LLM: {buggy.call_count} appels API factures")

    # --- Version corrigee ---
    print("\n--- Version CORRIGEE ---")
    fixed = FixedReflectionAgent(max_depth=3, timeout_seconds=5.0)
    result = fixed.run("Analyse this data")
    print(f"  Resultat: {result}")
    print(f"  Appels: {fixed.call_count} (max_depth=3)")

    # --- Circuit breaker ---
    print("\n--- Circuit Breaker ---")
    cb = CircuitBreaker(max_failures=3, cooldown_seconds=2.0)

    for i in range(5):
        can_exec = cb.can_execute()
        if can_exec:
            cb.record_failure()  # Simuler un echec
        print(f"  Tentative {i+1}: can_execute={can_exec} {cb}")

    print(f"  Agent bloque apres {cb.max_failures} echecs")

    # --- Invocation monitor ---
    print("\n--- Invocation Monitor ---")
    monitor = InvocationMonitor(window_seconds=60, max_per_window=5)

    for i in range(8):
        allowed = monitor.record("agent-alpha")
        count = monitor.get_count("agent-alpha")
        status = "ALLOWED" if allowed else "BLOCKED"
        print(f"  Invocation {i+1}: {status} (count={count}/5)")

    # --- Comparaison ---
    print("\n--- Resume ---")
    print(f"  Bugge:  {buggy.call_count} appels (crash par RecursionError)")
    print(f"  Corrige: {fixed.call_count} appels (stop propre a max_depth)")
    print(f"  Circuit breaker: coupe apres {cb.max_failures} echecs")
    print(f"  Monitor: bloque apres {monitor.max_per_window} invocations/min")
