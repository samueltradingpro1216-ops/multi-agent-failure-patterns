# Pattern 05 — Unit Mismatch 100x

> Applicable a : tout systeme ou des agents manipulent des valeurs avec des unites (tokens, prix, scores, tailles). Frequent dans les pipelines LangChain qui melangent des couts en dollars et en centimes, ou dans les systemes AutoGen qui calculent des budgets de tokens avec des unites differentes selon les modules.

## Symptome

- Une action est executee avec une magnitude 10x-100x trop grande (ou trop petite)
- Un seul appel consomme tout le budget (tokens, API calls, ressources)
- Le calcul utilise la bonne formule, le bon pourcentage, mais la **valeur unitaire** est fausse

## Cause racine

Deux modules utilisent des valeurs unitaires differentes pour la meme entite. Un module dit "1 unite = 1.0", l'autre dit "1 unite = 0.01" (copie-colle depuis un autre contexte). La formule `quantite = budget / (distance * unit_value)` donne un resultat 100x trop grand.

## Detection

Lister toutes les valeurs unitaires par entite et par module. Si deux modules ont des valeurs differentes pour la meme entite, c'est un mismatch. Voir `example.py`.

## Correction

Centraliser les valeurs unitaires dans une config unique. Ajouter un garde-fou "TOO_BIG" : avant chaque action, verifier que la magnitude ne depasse pas un seuil absolu. Voir `example.py`.

## Prevention

- Source unique pour les valeurs unitaires (fichier de config ou classe)
- Garde-fou absolu avant chaque action critique (si magnitude > 10x attendu -> bloquer)
- Audit croise au demarrage : verifier que tous les modules lisent la meme config
- Ne jamais copier-coller les valeurs unitaires entre modules

## Playbook complet

Fiche detaillee avec audit croise au boot, garde-fou complet, et analyse d'impact financier : [lien a venir]
