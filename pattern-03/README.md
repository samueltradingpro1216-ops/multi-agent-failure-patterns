# Pattern 03 — Cascade de Pénalités (Mardi Noir)

## Le bug

Plusieurs agents modifient le même paramètre (ex: Risk_Percent) indépendamment dans le même cycle. Chaque modification est raisonnable seule, mais la cascade les multiplie.

## Symptome

- Risk tombe à 0.01% au lieu de 0.25% (14x trop petit)
- Lots microscopiques, gains en centimes
- Se produit quand plusieurs conditions négatives coïncident (ex: mardi + nuit + drawdown)

## Cause racine

5 modules font un read-modify-write indépendant : `0.8 × 0.5 × 0.7 × 0.5 × 0.5 = 0.07` du nominal. Chaque agent ignore les ajustements des autres.

## Quick fix

Pipeline accumulatif avec floor cumulatif à 30%. Voir `example.py`.

## Playbook complet

Le playbook payant contient : détection par audit trail, pipeline complet avec logs, alertes sur réduction excessive, et pattern d'architecture détaillé.

[Multi-Agent Debug Patterns — Playbook complet](https://example.com/playbook)
