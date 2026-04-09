# Pattern 08 — Data Pipeline Freeze

> Applicable a : tout systeme multi-agents ou un agent produit des donnees consommees par un autre. Tres frequent dans les architectures LangChain avec des memory stores, les pipelines CrewAI ou un agent ecrit des resultats lus par un autre, et les workflows AutoGen avec des artefacts partages entre etapes.

## Symptome

- Un dashboard ou un rapport affiche des donnees **gelees** depuis des jours/semaines
- Les metriques derivees (moyennes, scores, tendances) sont fausses car basees sur des donnees obsoletes
- Le producteur de donnees fonctionne normalement (il ecrit), mais le consommateur ne voit rien de nouveau
- Aucune erreur dans les logs — le consommateur lit un fichier/table qui existe, il est juste vide ou perime

## Cause racine

Le producteur a **change de format** (ex: CSV au lieu de JSONL, nouveau schema, nouveau chemin) sans mettre a jour le consommateur. Les deux continuent de fonctionner sans erreur :

```
Producteur (Agent A):  ecrit data.csv        (nouveau format depuis la refonte)
Consommateur (Agent B): lit data.jsonl       (ancien format, fichier jamais mis a jour)
                        -> fichier existe mais 0 nouvelles lignes depuis 11 jours
```

Le consommateur ne crashe pas car le fichier existe et contient des donnees anciennes. Il les lit, calcule des metriques, et produit des rapports — tous bases sur des donnees gelees.

## Detection

Verifier l'age des donnees dans chaque pipeline. Si un fichier/table n'a pas ete mis a jour depuis plus de N heures alors que le producteur tourne, c'est une pipeline freeze. Voir `example.py`.

## Correction

1. **Aligner producteur et consommateur** sur le meme format/chemin
2. **Ajouter un timestamp `last_updated`** dans chaque fichier de donnees
3. **Verifier la fraicheur** cote consommateur avant de lire

Voir `example.py` pour le pattern complet.

## Prevention

- **Contract entre producteur et consommateur** : schema versionne, valide des deux cotes
- **Staleness check** : le consommateur refuse les donnees plus vieilles que N heures
- **Health check de pipeline** : verifier periodiquement que chaque maillon produit des donnees fraiches
- **Integration test** : tester le flux complet producteur -> consommateur a chaque deploiement
- **Schema registry** : versionner les formats de donnees, detecter les incompatibilites au build

## Playbook complet

Fiche detaillee avec schema registry simplifie, health check de pipeline, et cas reel ou une DB de resultats est restee gelee pendant 11 jours sans que personne ne le remarque : [lien a venir]
