# Pattern 02 — Rapid-Fire Loop (Refresh Zombie)

## Le bug

Un mécanisme de refresh re-envoie une commande déjà exécutée car il ne distingue pas "pas encore exécuté" de "exécuté puis fermé". Résultat : boucle infinie d'ouvertures/fermetures.

## Symptome

- 124 trades en une journée, 97% sont des pertes de spread (< $0.50 chacune)
- Pattern répétitif toutes les 10s : ouverture → fermeture immédiate → ré-ouverture
- Pertes cumulées de spread : -$80/jour

## Cause racine

`has_position == False` a deux significations : (1) commande pas encore exécutée, (2) position exécutée puis fermée. Le refresh ne distingue pas les deux.

## Quick fix

Ajouter un état "EXECUTED" aux commandes. Le refresh ne re-envoie que les "PENDING". Voir `example.py`.

## Playbook complet

Le playbook payant contient : détection par analyse de logs, state machine complète, guard avec cooldown, et analyse financière de l'impact réel.

[Multi-Agent Debug Patterns — Playbook complet](https://example.com/playbook)
