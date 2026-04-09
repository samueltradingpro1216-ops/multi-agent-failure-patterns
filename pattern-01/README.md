# Pattern 01 — Timezone Mismatch

## Le bug

Un composant utilise l'heure broker/locale au lieu de UTC pour ses filtres temporels. Le filtre bloque les **mauvaises heures**.

## Symptome

- Filtre "London/NY overlap 12h-18h UTC" autorise en réalité 9h-15h UTC (décalé de +3h broker)
- Le système rate la fenêtre de liquidité optimale
- Invisible en test local si le poste dev est en UTC

## Cause racine

`TimeCurrent()` (ou `datetime.now()`) retourne l'heure contextuelle, pas UTC. En production sur un serveur broker à Chypre (UTC+3), les heures sont décalées.

## Quick fix

Remplacer toutes les sources de temps par UTC explicite. Voir `example.py`.

## Playbook complet

Le playbook payant contient : détection automatisée, pattern de correction complet, tests avec timezone exotiques, et histoire réelle détaillée.

[Multi-Agent Debug Patterns — Playbook complet](https://example.com/playbook)
