"""
Pattern 02 — Rapid-Fire Loop (Refresh Zombie)
Démontre comment un refresh de commandes peut créer une boucle infinie
quand il ne distingue pas "pas encore exécuté" de "exécuté puis fermé".

Usage: python example.py
"""
from datetime import datetime, timedelta
from collections import defaultdict


# --- Le bug ---

class BuggyCommandRefresh:
    """Version buggée : refresh basé uniquement sur has_position."""

    def __init__(self):
        self.pending_command = None
        self.execution_count = 0

    def send_command(self, signal: str, symbol: str):
        self.pending_command = {"signal": signal, "symbol": symbol}

    def refresh(self, has_position: bool) -> bool:
        """
        BUG: si has_position=False et commande pendante → re-envoie.
        Ne sait pas si la commande a DEJA été exécutée.
        """
        if not has_position and self.pending_command:
            self.execution_count += 1
            return True  # Re-envoie la commande
        return False


# --- La correction ---

class FixedCommandRefresh:
    """Version corrigée : state machine PENDING → EXECUTED → CLOSED."""

    def __init__(self):
        self.command = None
        self.state = "IDLE"  # IDLE → PENDING → EXECUTED → CLOSED
        self.execution_count = 0

    def send_command(self, signal: str, symbol: str):
        self.command = {"signal": signal, "symbol": symbol}
        self.state = "PENDING"

    def on_position_opened(self):
        """L'exécuteur a ouvert la position."""
        if self.state == "PENDING":
            self.state = "EXECUTED"

    def on_position_closed(self):
        """La position a été fermée. État terminal."""
        if self.state == "EXECUTED":
            self.state = "CLOSED"

    def refresh(self, has_position: bool) -> bool:
        """
        FIX: ne re-envoie que si la commande est PENDING.
        EXECUTED et CLOSED ne sont jamais re-envoyés.
        """
        if self.state == "PENDING" and not has_position:
            self.execution_count += 1
            return True  # Re-envoie (commande pas encore lue)
        return False


# --- Détection de rapid-fire ---

def detect_rapid_fire(
    timestamps: list[datetime],
    window_seconds: int = 300,
    threshold: int = 5
) -> bool:
    """
    Détecte si trop d'exécutions ont lieu dans une fenêtre de temps.
    """
    if len(timestamps) < threshold:
        return False

    timestamps.sort()
    for i in range(len(timestamps) - threshold + 1):
        window = (timestamps[i + threshold - 1] - timestamps[i]).total_seconds()
        if window <= window_seconds:
            return True
    return False


# --- Démonstration ---

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 02 — Rapid-Fire Loop")
    print("=" * 60)

    # Simuler 20 cycles de la boucle principale (10s chacun)
    cycles = 20

    print(f"\n--- Version BUGGÉE ({cycles} cycles) ---")
    buggy = BuggyCommandRefresh()
    buggy.send_command("BUY", "XAUUSD")

    for i in range(cycles):
        has_pos = (i % 3 == 1)  # Position ouverte 1 cycle sur 3, puis fermée
        refreshed = buggy.refresh(has_pos)
        if refreshed and i < 10:  # Afficher les 10 premiers
            print(f"  Cycle {i:2d}: has_pos={has_pos} -> REFRESH (re-envoi #{buggy.execution_count})")

    print(f"  ... Total re-envois: {buggy.execution_count}")

    print(f"\n--- Version CORRIGÉE ({cycles} cycles) ---")
    fixed = FixedCommandRefresh()
    fixed.send_command("BUY", "XAUUSD")

    for i in range(cycles):
        if i == 1:
            fixed.on_position_opened()  # EA ouvre au cycle 1
        if i == 2:
            fixed.on_position_closed()  # Position fermée au cycle 2

        has_pos = (i == 1)  # Position ouverte seulement au cycle 1
        refreshed = fixed.refresh(has_pos)
        status = fixed.state
        if i < 10:
            print(f"  Cycle {i:2d}: state={status:8s} has_pos={has_pos} -> {'REFRESH' if refreshed else 'skip'}")

    print(f"  ... Total re-envois: {fixed.execution_count}")

    # Détection
    print(f"\n--- Détection rapid-fire ---")
    base = datetime(2026, 4, 7, 10, 0, 0)
    rapid_timestamps = [base + timedelta(seconds=i * 12) for i in range(50)]
    normal_timestamps = [base + timedelta(minutes=i * 30) for i in range(5)]

    print(f"  50 trades en 10min: rapid_fire={detect_rapid_fire(rapid_timestamps)}")
    print(f"  5 trades en 2.5h:  rapid_fire={detect_rapid_fire(normal_timestamps)}")

    # Impact financier
    print(f"\n--- Impact financier ---")
    spread_cost = 0.24  # $ par trade de spread
    buggy_trades = buggy.execution_count
    fixed_trades = fixed.execution_count
    print(f"  Buggé:  {buggy_trades} trades × ${spread_cost} = -${buggy_trades * spread_cost:.2f}/jour")
    print(f"  Corrigé: {fixed_trades} trade  × ${spread_cost} = -${fixed_trades * spread_cost:.2f}/jour")
