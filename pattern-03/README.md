# Pattern 03 — Cascade de Penalites (effet multiplicatif)

> Applicable a : tout systeme multi-agents ou plusieurs agents ajustent le meme parametre (priorite de tache, budget de tokens, timeout, score de confiance). Frequent avec CrewAI quand plusieurs agents ont un droit de modification sur la meme config partagee.

## Symptome

- Un parametre tombe a une valeur absurdement basse (ex: 7% de sa valeur nominale)
- Le systeme fonctionne techniquement mais produit des resultats negligeables
- Se produit quand plusieurs conditions negatives coincident (ex: mardi + nuit + charge elevee)

## Cause racine

N modules font un read-modify-write independant sur le meme parametre dans le meme cycle. Chaque reduction est raisonnable seule (-20%, -30%, -50%), mais la cascade les multiplie : `0.8 x 0.5 x 0.7 x 0.5 x 0.5 = 0.07` du nominal.

## Detection

Analyser l'audit trail des ecritures. Si > 3 modifications du meme parametre en < 60s avec un ratio final/initial < 20%, c'est une cascade. Voir `example.py`.

## Correction

Pipeline accumulatif : les modules proposent des multiplicateurs, une seule fonction applique le resultat final avec un floor cumulatif (jamais reduire de plus de 70%). Voir `example.py`.

## Prevention

- Interdire les read-modify-write directs sur les parametres partages
- Floor cumulatif configurable (ex: 0.3 = reduction max 70%)
- Alerter si un parametre < 50% de sa valeur nominale pendant > 1h

## Playbook complet

Fiche detaillee avec pipeline complet, audit trail, et alerting : [lien a venir]
