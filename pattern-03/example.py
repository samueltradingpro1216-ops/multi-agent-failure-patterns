"""
Pattern 03 — Cascade de Pénalités (Mardi Noir)
Démontre comment N agents qui modifient le même paramètre indépendamment
créent une cascade qui réduit la valeur à quasi-zéro.

Usage: python example.py
"""


# --- Le bug ---

def cascade_buggy(base_risk: float) -> float:
    """
    BUG: chaque module fait un read-modify-write indépendant.
    Les réductions se multiplient.
    """
    risk = base_risk

    # Module 1: dynamic_risk (mardi = -20%)
    risk *= 0.8

    # Module 2: position_sizing (3 pertes consécutives = -50%)
    risk *= 0.5

    # Module 3: regime (haute volatilité = -30%)
    risk *= 0.7

    # Module 4: emergency (drawdown > seuil = -50%)
    risk *= 0.5

    # Module 5: night (session nuit = -50%)
    risk *= 0.5

    return risk


# --- La correction ---

class RiskPipeline:
    """
    FIX: accumule les ajustements, applique une seule fois avec floor.
    """

    def __init__(self, base_risk: float, min_risk: float = 0.05):
        self.base_risk = base_risk
        self.min_risk = min_risk
        self.adjustments = []

    def add(self, source: str, multiplier: float, reason: str):
        """Enregistre un ajustement SANS l'appliquer."""
        clamped = max(0.3, min(2.0, multiplier))
        self.adjustments.append((source, clamped, reason))

    def compute(self) -> dict:
        """Applique tous les ajustements avec floor cumulatif."""
        cumulative = 1.0
        for _, mult, _ in self.adjustments:
            cumulative *= mult

        # Floor: jamais réduire de plus de 70%
        cumulative = max(0.3, cumulative)

        final = max(self.min_risk, self.base_risk * cumulative)

        return {
            "base": self.base_risk,
            "cumulative": round(cumulative, 4),
            "final": round(final, 4),
            "adjustments": self.adjustments,
        }


# --- Détection ---

def detect_cascade(writes: list[tuple]) -> bool:
    """
    Détecte une cascade si le ratio final/initial < 20%
    en moins de 60 secondes.

    writes: [(timestamp_seconds, value), ...]
    """
    if len(writes) < 3:
        return False

    writes.sort()
    initial = writes[0][1]
    final = writes[-1][1]
    time_span = writes[-1][0] - writes[0][0]

    if initial <= 0:
        return False

    ratio = final / initial
    return ratio < 0.2 and time_span < 60


# --- Démonstration ---

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 03 — Cascade de Pénalités")
    print("=" * 60)

    base_risk = 0.25

    # Version buggée
    buggy_result = cascade_buggy(base_risk)
    print(f"\n--- Version BUGGÉE ---")
    print(f"  Base risk:  {base_risk}%")
    print(f"  Après cascade: {buggy_result:.4f}%")
    print(f"  Ratio: {buggy_result/base_risk:.1%} du nominal")
    print(f"  → Risk {base_risk/buggy_result:.0f}x trop petit")

    # Version corrigée
    print(f"\n--- Version CORRIGÉE (pipeline avec floor) ---")
    pipeline = RiskPipeline(base_risk=0.25, min_risk=0.05)
    pipeline.add("dynamic_risk", 0.8, "Mardi historiquement mauvais")
    pipeline.add("position_sizing", 0.5, "3 losses consécutives")
    pipeline.add("regime", 0.7, "High volatility")
    pipeline.add("emergency", 0.5, "Drawdown > 3%")
    pipeline.add("night", 0.5, "Session nuit")

    result = pipeline.compute()
    print(f"  Base risk:    {result['base']}%")
    print(f"  Multiplicateur: {result['cumulative']} (floored at 0.3)")
    print(f"  Final risk:   {result['final']}%")
    print(f"  Adjustments:")
    for source, mult, reason in result["adjustments"]:
        print(f"    {source}: x{mult} ({reason})")

    # Détection
    print(f"\n--- Détection de cascade ---")
    # Simuler 5 écritures en 10 secondes
    cascade_writes = [
        (0, 0.25), (2, 0.20), (4, 0.10), (6, 0.07), (8, 0.018)
    ]
    print(f"  5 writes en 8s: {cascade_writes}")
    print(f"  Cascade détectée: {detect_cascade(cascade_writes)}")

    normal_writes = [(0, 0.25), (3600, 0.20), (7200, 0.18)]
    print(f"  3 writes en 2h: cascade={detect_cascade(normal_writes)}")

    # Comparaison visuelle
    print(f"\n--- Comparaison ---")
    print(f"  Buggé:  {base_risk}% → {buggy_result:.4f}% ({buggy_result/base_risk:.1%})")
    print(f"  Corrigé: {base_risk}% → {result['final']}% ({result['final']/base_risk:.1%})")
    print(f"  Gain: risk {result['final']/buggy_result:.1f}x plus élevé avec le fix")
