# Pattern 04 — Multi-File State Desync

> Applicable a : tout systeme ou le meme etat est stocke en plusieurs endroits (fichiers, DB, cache, variables d'environnement). Tres frequent dans les systemes LangChain avec Redis + fichier config + variable env, ou dans les pipelines AutoGen avec etat distribue.

## Symptome

- Un composant pense que le systeme est arrete, un autre pense qu'il tourne
- Le dashboard crash car un fichier d'etat n'existe pas
- 3 sources d'information donnent 3 reponses differentes pour la meme question

## Cause racine

L'etat a ete **duplique progressivement** au fil du developpement : V1 ecrit un fichier texte, V2 ajoute un JSON structure, V3 ajoute un champ dans la config. Les 3 sources ne sont jamais synchronisees. Pas de Single Source of Truth.

## Detection

Pour chaque etat critique, lister toutes les sources qui le stockent. Comparer les valeurs et les dates de modification. Si les valeurs divergent ou si une source est stale (> 24h), c'est une desync. Voir `example.py`.

## Correction

Un manager centralise qui ecrit dans TOUTES les sources a chaque modification et lit depuis UNE SEULE (la source primaire). Voir `example.py`.

## Prevention

- Single Source of Truth obligatoire pour chaque etat critique
- Sync periodique (toutes les 60s) qui re-aligne les copies
- Audit de coherence a chaque cycle : toutes les copies doivent etre identiques
- Chaque fichier d'etat contient `last_synced` — si > 5min, considerer comme stale

## Playbook complet

Fiche detaillee avec manager complet, audit automatise, et gestion des fichiers stale : [lien a venir]
