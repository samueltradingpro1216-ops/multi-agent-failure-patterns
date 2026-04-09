"""
Pattern 01 — Timezone Mismatch
Démontre comment un filtre horaire peut bloquer les mauvaises heures
quand la timezone n'est pas explicitement UTC.

Usage: python example.py
"""
from datetime import datetime, timezone, timedelta


def is_london_ny_buggy(broker_offset_hours: int = 3) -> bool:
    """
    BUG: utilise l'heure broker directement pour le filtre.
    Sur un broker UTC+3, 12h-18h broker = 9h-15h UTC.
    """
    # Simuler l'heure broker
    utc_now = datetime.now(timezone.utc)
    broker_time = utc_now + timedelta(hours=broker_offset_hours)
    h = broker_time.hour
    return 12 <= h < 18  # Compare broker time à des bornes censées être UTC


def is_london_ny_fixed() -> bool:
    """
    FIX: utilise explicitement UTC pour le filtre.
    12h-18h UTC = toujours correct, quel que soit le broker.
    """
    h = datetime.now(timezone.utc).hour
    return 12 <= h < 18


# --- Détection de mismatch ---

def detect_timezone_mismatch(
    timestamp_a: datetime,
    timestamp_b: datetime,
    max_drift_seconds: int = 60
) -> dict:
    """
    Compare deux timestamps de composants différents.
    Si le delta est proche d'un multiple d'heure → probable mismatch timezone.
    """
    delta = abs((timestamp_a - timestamp_b).total_seconds())

    if delta < max_drift_seconds:
        return {"mismatch": False, "drift_seconds": delta}

    nearest_hour = round(delta / 3600)
    remainder = abs(delta - nearest_hour * 3600)

    if nearest_hour > 0 and remainder < 300:
        return {
            "mismatch": True,
            "probable_offset_hours": nearest_hour,
            "message": f"Delta ~{nearest_hour}h — probable mismatch timezone"
        }

    return {"mismatch": False, "drift_seconds": delta}


# --- Démonstration ---

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 01 — Timezone Mismatch")
    print("=" * 60)

    utc_now = datetime.now(timezone.utc)
    print(f"\nHeure UTC actuelle: {utc_now.strftime('%H:%M:%S')}")

    # Comparer buggy vs fixed
    print(f"\nFiltre London/NY (12h-18h UTC):")
    print(f"  Version buggée (broker UTC+3): {is_london_ny_buggy(3)}")
    print(f"  Version corrigée (UTC):        {is_london_ny_fixed()}")

    # Détecter le mismatch entre composants
    print(f"\n--- Détection de mismatch ---")
    broker_time = utc_now + timedelta(hours=3)
    # Simuler: composant A en UTC, composant B en broker time
    result = detect_timezone_mismatch(
        utc_now,
        broker_time.replace(tzinfo=timezone.utc)  # Faux UTC
    )
    if result["mismatch"]:
        print(f"  MISMATCH: {result['message']}")
    else:
        print(f"  OK — drift {result['drift_seconds']:.0f}s")

    # Montrer l'impact concret
    print(f"\n--- Impact ---")
    for offset in [0, 2, 3, 5]:
        buggy = is_london_ny_buggy(offset)
        fixed = is_london_ny_fixed()
        match = "OK" if buggy == fixed else "MISMATCH"
        print(f"  Broker UTC+{offset}: buggy={buggy}, fixed={fixed} [{match}]")
