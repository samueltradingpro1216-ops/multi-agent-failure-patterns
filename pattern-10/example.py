"""
Pattern 10 — Survival Mode Deadlock
Demontre comment un seuil de confiance statique peut bloquer 100% des actions
quand le score moyen du systeme est inferieur au seuil.

Usage: python example.py
"""
import random
import time
from dataclasses import dataclass, field


# === LE BUG ===

class BuggyConfidenceGate:
    """
    BUG: seuil statique a 50. Score moyen ~42.
    100% des actions rejetees. Deadlock permanent.
    """

    def __init__(self, threshold: float = 50.0):
        self.threshold = threshold
        self.total = 0
        self.accepted = 0

    def evaluate(self, score: float) -> bool:
        """Retourne True si l'action est acceptee."""
        self.total += 1
        if score >= self.threshold:
            self.accepted += 1
            return True
        return False

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.total if self.total > 0 else 0


# === LA CORRECTION ===

class AdaptiveConfidenceGate:
    """
    FIX: 3 mecanismes anti-deadlock.
    1. Seuil adaptatif qui descend si acceptance_rate = 0%
    2. Fenetre d'exploration (1 action sur N meme si score bas)
    3. Seuil minimum garanti apres T cycles sans action
    """

    def __init__(
        self,
        initial_threshold: float = 50.0,
        min_threshold: float = 25.0,
        decay_rate: float = 2.0,
        explore_every_n: int = 20,
        deadlock_after_n: int = 100,
    ):
        self.threshold = initial_threshold
        self.initial_threshold = initial_threshold
        self.min_threshold = min_threshold
        self.decay_rate = decay_rate
        self.explore_every_n = explore_every_n
        self.deadlock_after_n = deadlock_after_n

        self.total = 0
        self.accepted = 0
        self.consecutive_rejects = 0
        self.scores_history = []

    def evaluate(self, score: float) -> dict:
        """Evalue un score et decide d'accepter ou non."""
        self.total += 1
        self.scores_history.append(score)

        reason = None

        # Mecanisme 1: seuil adaptatif
        # Si trop de rejets consecutifs, baisser le seuil
        if self.consecutive_rejects > 0 and self.consecutive_rejects % 10 == 0:
            old = self.threshold
            self.threshold = max(self.min_threshold, self.threshold - self.decay_rate)
            if self.threshold < old:
                reason = f"threshold_decay: {old:.1f} -> {self.threshold:.1f}"

        # Mecanisme 2: fenetre d'exploration
        if self.total % self.explore_every_n == 0 and self.consecutive_rejects > 0:
            self.accepted += 1
            self.consecutive_rejects = 0
            return {
                "accepted": True,
                "score": score,
                "threshold": self.threshold,
                "reason": "exploration_window",
            }

        # Mecanisme 3: deadlock breaker
        if self.consecutive_rejects >= self.deadlock_after_n:
            self.threshold = self.min_threshold
            reason = f"deadlock_breaker: threshold -> {self.min_threshold}"

        # Evaluation normale
        if score >= self.threshold:
            self.accepted += 1
            self.consecutive_rejects = 0
            return {
                "accepted": True,
                "score": score,
                "threshold": self.threshold,
                "reason": reason or "score_above_threshold",
            }
        else:
            self.consecutive_rejects += 1
            return {
                "accepted": False,
                "score": score,
                "threshold": self.threshold,
                "reason": reason or "score_below_threshold",
            }

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.total if self.total > 0 else 0

    @property
    def avg_score(self) -> float:
        return sum(self.scores_history) / len(self.scores_history) if self.scores_history else 0


# === DETECTION ===

def detect_deadlock(
    scores: list[float],
    threshold: float,
    window_size: int = 50,
    min_acceptance_rate: float = 0.05,
) -> dict:
    """
    Detecte un deadlock en comparant le score moyen au seuil
    sur les N derniers cycles.
    """
    if len(scores) < window_size:
        return {"deadlock": False, "reason": "not_enough_data"}

    recent = scores[-window_size:]
    avg = sum(recent) / len(recent)
    accepted = sum(1 for s in recent if s >= threshold)
    rate = accepted / len(recent)

    if rate < min_acceptance_rate:
        return {
            "deadlock": True,
            "avg_score": round(avg, 1),
            "threshold": threshold,
            "acceptance_rate": round(rate, 3),
            "message": (
                f"Deadlock: avg_score={avg:.1f} < threshold={threshold}, "
                f"acceptance={rate:.1%} over {window_size} cycles"
            ),
        }

    return {
        "deadlock": False,
        "avg_score": round(avg, 1),
        "acceptance_rate": round(rate, 3),
    }


# === PERCENTILE THRESHOLD ===

class PercentileGate:
    """
    Alternative: accepter le top X% des scores au lieu d'un seuil absolu.
    Garantit toujours un taux d'acceptation minimum.
    """

    def __init__(self, accept_top_pct: float = 10.0, min_history: int = 20):
        self.accept_top_pct = accept_top_pct
        self.min_history = min_history
        self.history = []
        self.accepted = 0
        self.total = 0

    def evaluate(self, score: float) -> bool:
        self.history.append(score)
        self.total += 1

        if len(self.history) < self.min_history:
            # Pas assez d'historique, accepter tout
            self.accepted += 1
            return True

        # Calculer le seuil dynamique (percentile)
        sorted_scores = sorted(self.history[-100:])  # Derniers 100
        idx = int(len(sorted_scores) * (1 - self.accept_top_pct / 100))
        dynamic_threshold = sorted_scores[min(idx, len(sorted_scores) - 1)]

        if score >= dynamic_threshold:
            self.accepted += 1
            return True
        return False

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.total if self.total > 0 else 0


# === DEMONSTRATION ===

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 10 -- Survival Mode Deadlock")
    print("=" * 60)

    # Generateur de scores: moyenne ~42, ecart-type ~8
    random.seed(42)
    def generate_score():
        return max(0, min(100, random.gauss(42, 8)))

    n_cycles = 200

    # --- Version buggee ---
    print(f"\n--- Version BUGGEE ({n_cycles} cycles, seuil=50) ---")
    buggy = BuggyConfidenceGate(threshold=50.0)
    scores = []

    for _ in range(n_cycles):
        score = generate_score()
        scores.append(score)
        buggy.evaluate(score)

    avg = sum(scores) / len(scores)
    print(f"  Score moyen: {avg:.1f}")
    print(f"  Seuil: {buggy.threshold}")
    print(f"  Actions acceptees: {buggy.accepted}/{buggy.total}")
    print(f"  Taux d'acceptation: {buggy.acceptance_rate:.1%}")
    if buggy.acceptance_rate < 0.05:
        print(f"  -> DEADLOCK: le systeme ne fait rien")

    # Detection
    result = detect_deadlock(scores, threshold=50.0)
    if result["deadlock"]:
        print(f"  Detection: {result['message']}")

    # --- Version corrigee (adaptative) ---
    print(f"\n--- Version CORRIGEE: seuil adaptatif ---")
    random.seed(42)
    adaptive = AdaptiveConfidenceGate(
        initial_threshold=50.0,
        min_threshold=25.0,
        decay_rate=2.0,
        explore_every_n=20,
        deadlock_after_n=100,
    )

    for _ in range(n_cycles):
        score = generate_score()
        adaptive.evaluate(score)

    print(f"  Score moyen: {adaptive.avg_score:.1f}")
    print(f"  Seuil final: {adaptive.threshold:.1f} (initial: {adaptive.initial_threshold})")
    print(f"  Actions acceptees: {adaptive.accepted}/{adaptive.total}")
    print(f"  Taux d'acceptation: {adaptive.acceptance_rate:.1%}")

    # --- Version corrigee (percentile) ---
    print(f"\n--- Version CORRIGEE: percentile top 10% ---")
    random.seed(42)
    percentile = PercentileGate(accept_top_pct=10.0, min_history=20)

    for _ in range(n_cycles):
        score = generate_score()
        percentile.evaluate(score)

    print(f"  Actions acceptees: {percentile.accepted}/{percentile.total}")
    print(f"  Taux d'acceptation: {percentile.acceptance_rate:.1%}")

    # --- Comparaison ---
    print(f"\n--- Comparaison ---")
    print(f"  Seuil statique (50):  {buggy.acceptance_rate:.1%} acceptes")
    print(f"  Seuil adaptatif:      {adaptive.acceptance_rate:.1%} acceptes")
    print(f"  Percentile top 10%:   {percentile.acceptance_rate:.1%} acceptes")
    print(f"  Le seuil statique cree un deadlock, les 2 autres le resolvent.")
