# Pattern 04 — Killswitch Multi-File Desync

## Le bug

Le même état critique (killswitch/arrêt d'urgence) est stocké dans 3 fichiers différents, écrits par 3 composants différents, jamais synchronisés. Chaque composant lit "sa" version → états contradictoires.

## Symptome

- L'exécuteur refuse de trader (killswitch = ACTIVE dans son fichier)
- Le superviseur envoie des commandes (killswitch = false dans sa config)
- Le dashboard crash (fichier JSON absent)
- Personne ne sait si le système est vraiment arrêté ou non

## Cause racine

Pas de Single Source of Truth. Chaque version du système a ajouté sa propre source de vérité sans synchroniser les précédentes.

## Quick fix

Un KillswitchManager qui écrit dans TOUS les fichiers à chaque modification et lit depuis UN SEUL. Voir `example.py`.

## Playbook complet

Le playbook payant contient : audit de cohérence automatisé, pattern sync périodique, gestion des fichiers stale, et architecture complète.

[Multi-Agent Debug Patterns — Playbook complet](https://example.com/playbook)
