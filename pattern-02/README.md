# Pattern 02 — Rapid-Fire Loop (Refresh Zombie)

> Applicable a : tout systeme ou un orchestrateur re-tente une action sur un executeur (retry CrewAI, re-delegation LangChain, requeue AutoGen). Le pattern se produit des qu'un mecanisme de retry ne distingue pas "pas encore execute" de "execute puis termine".

## Symptome

- L'executeur repete la meme action en boucle (toutes les N secondes)
- Les resultats s'accumulent : des centaines d'executions identiques en une journee
- Chaque execution a un cout marginal (API call, I/O, frais) qui s'accumule

## Cause racine

Le mecanisme de refresh/retry ne suit pas le **cycle de vie** de la commande. Il voit "pas de resultat actif" et re-envoie, sans savoir que la commande a deja ete executee et terminee. `status == IDLE` a deux significations : "pas encore commence" et "termine et nettoyee".

## Detection

Compter les executions identiques dans une fenetre de temps. Si > N executions en < T secondes pour la meme action, c'est un rapid-fire. Voir `example.py`.

## Correction

Ajouter une state machine explicite a chaque commande : `PENDING -> EXECUTED -> CLOSED`. Le refresh ne re-envoie que les `PENDING`. Voir `example.py`.

## Prevention

- State machine obligatoire pour chaque commande/tache
- Cooldown minimum entre deux executions identiques
- Compteur d'executions par heure avec killswitch automatique si > seuil

## Playbook complet

Fiche detaillee avec state machine complete, guard avec cooldown, et analyse d'impact : [lien a venir]
