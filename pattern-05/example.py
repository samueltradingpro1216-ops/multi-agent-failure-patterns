"""
Pattern 05 — Lot Size 100x (Point Value Mismatch)
Démontre comment une point value incorrecte peut créer
un lot 100x trop gros et risquer le compte entier.

Usage: python example.py
"""


# --- Le bug ---

def compute_lot_buggy(
    balance: float,
    risk_pct: float,
    sl_points: float,
    asset: str
) -> float:
    """
    BUG: point value hardcodée par module.
    BTC utilise 0.01 (copié de XAUUSD) au lieu de 1.0.
    """
    # Chaque module a sa propre table de point values
    point_values = {
        "EURUSD": 0.0001,
        "XAUUSD": 0.01,
        "BTCUSD": 0.01,  # BUG — devrait être 1.0
    }

    point = point_values.get(asset, 0.01)
    risk_dollars = balance * (risk_pct / 100)
    lot = risk_dollars / (sl_points * point * 1)  # contract_size=1 pour simplifier

    return round(lot, 2)


# --- La correction ---

# Source UNIQUE de vérité pour les point values
ASSET_CONFIG = {
    "EURUSD": {"point": 0.0001, "contract_size": 100000, "min_lot": 0.01, "max_lot": 10.0},
    "XAUUSD": {"point": 0.01,   "contract_size": 100,    "min_lot": 0.01, "max_lot": 5.0},
    "BTCUSD": {"point": 1.0,    "contract_size": 1,      "min_lot": 0.01, "max_lot": 1.0},
    "GER40":  {"point": 0.1,    "contract_size": 1,      "min_lot": 0.01, "max_lot": 10.0},
}


def compute_lot_fixed(
    balance: float,
    risk_pct: float,
    sl_points: float,
    asset: str,
    max_risk_pct: float = 1.0
) -> dict:
    """
    FIX: point value centralisée + garde-fou LOT_TOO_BIG.
    """
    if asset not in ASSET_CONFIG:
        raise ValueError(f"Asset inconnu: {asset}")

    cfg = ASSET_CONFIG[asset]
    point = cfg["point"]
    contract = cfg["contract_size"]
    min_lot = cfg["min_lot"]
    max_lot = cfg["max_lot"]

    risk_dollars = balance * (risk_pct / 100)
    lot = risk_dollars / (sl_points * point * contract)
    lot = max(min_lot, min(max_lot, round(lot, 2)))

    # GARDE-FOU: vérifier le risk réel
    actual_risk = (lot * sl_points * point * contract) / balance * 100
    blocked = False

    if actual_risk > max_risk_pct:
        lot = min_lot
        actual_risk = (lot * sl_points * point * contract) / balance * 100
        blocked = True

    return {
        "lot": lot,
        "risk_dollars": round(lot * sl_points * point * contract, 2),
        "risk_pct": round(actual_risk, 4),
        "blocked": blocked,
        "point_value": point,
    }


# --- Détection ---

def detect_point_value_mismatch(asset: str, values_by_module: dict) -> list[str]:
    """
    Vérifie que tous les modules utilisent la même point value.
    """
    unique = set(values_by_module.values())
    if len(unique) > 1:
        ratio = max(unique) / min(unique)
        return [
            f"MISMATCH {asset}: {ratio:.0f}x difference — "
            + ", ".join(f"{m}={v}" for m, v in values_by_module.items())
        ]
    return []


# --- Démonstration ---

if __name__ == "__main__":
    print("=" * 60)
    print("Pattern 05 — Lot Size 100x")
    print("=" * 60)

    balance = 7500.0
    risk_pct = 0.25
    sl_points = 45.0

    # Version buggée
    print(f"\n--- Version BUGGÉE ---")
    print(f"  Balance: ${balance}  Risk: {risk_pct}%  SL: {sl_points} points")
    for asset in ["XAUUSD", "BTCUSD"]:
        lot = compute_lot_buggy(balance, risk_pct, sl_points, asset)
        print(f"  {asset}: lot = {lot}")

    btc_lot_buggy = compute_lot_buggy(balance, risk_pct, sl_points, "BTCUSD")
    btc_risk = btc_lot_buggy * sl_points * 1.0  # Avec la VRAIE point value
    print(f"\n  BTC risk RÉEL: ${btc_risk:.2f} ({btc_risk/balance*100:.1f}% du compte)")
    print(f"  → {btc_risk/balance*100/risk_pct:.0f}x le risk voulu!")

    # Version corrigée
    print(f"\n--- Version CORRIGÉE ---")
    for asset in ["XAUUSD", "BTCUSD"]:
        result = compute_lot_fixed(balance, risk_pct, sl_points, asset)
        status = "BLOCKED → min_lot" if result["blocked"] else "OK"
        print(
            f"  {asset}: lot={result['lot']} "
            f"risk=${result['risk_dollars']} ({result['risk_pct']}%) "
            f"[{status}]"
        )

    # Détection de mismatch
    print(f"\n--- Détection ---")
    alerts = detect_point_value_mismatch("BTCUSD", {
        "lot_calculator": 1.0,
        "trailing_stop": 0.01,
    })
    for alert in alerts:
        print(f"  {alert}")

    # Impact financier
    print(f"\n--- Impact ---")
    print(f"  Lot buggy BTC:  {btc_lot_buggy} → risk ${btc_risk:.2f} ({btc_risk/balance*100:.1f}%)")
    fixed_result = compute_lot_fixed(balance, risk_pct, sl_points, "BTCUSD")
    print(f"  Lot corrigé BTC: {fixed_result['lot']} → risk ${fixed_result['risk_dollars']} ({fixed_result['risk_pct']}%)")
    print(f"  Le bug multiplie le risk par {btc_risk / fixed_result['risk_dollars']:.0f}x")
