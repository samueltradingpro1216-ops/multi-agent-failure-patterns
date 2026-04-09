# Pattern 05 — Lot Size 100x (Point Value Mismatch)

## Le bug

Deux modules utilisent des point values différentes pour le même asset. Le calcul de lot utilise `point=0.01` au lieu de `point=1.0` pour BTC → lot 100x trop gros.

## Symptome

- Lot = 0.50 au lieu de 0.005 (100x trop gros)
- Risk réel = 6.3% du compte au lieu de 0.25%
- Un seul trade peut tuer le compte

## Cause racine

Point value hardcodée dans chaque module au lieu d'être centralisée. Un copier-coller depuis un autre asset (XAUUSD point=0.01) a été utilisé pour BTC (point devrait être 1.0).

## Quick fix

Centraliser les point values dans une config unique + garde-fou LOT_TOO_BIG. Voir `example.py`.

## Playbook complet

Le playbook payant contient : audit croisé des point values au boot, garde-fou avec alerting, validation complète du lot sizing, et impact financier réel.

[Multi-Agent Debug Patterns — Playbook complet](https://example.com/playbook)
