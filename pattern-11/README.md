# Pattern 11 — Race Condition on Shared File

> Applicable a : tout systeme multi-agents ou plusieurs agents lisent et ecrivent le meme fichier ou la meme ressource. Extremement frequent dans les architectures LangChain avec un fichier de config partage, les agents CrewAI qui ecrivent dans un meme rapport, et les pipelines AutoGen avec un state store fichier ou un usage tracker partage.

## Symptome

- Des donnees disparaissent du fichier partage (ecrasees par un autre agent)
- Le fichier contient des donnees corrompues ou un JSON invalide
- Les compteurs (usage, quotas, budget) sont faux : deux agents incrementent en parallele et un increment est perdu
- Le probleme est **intermittent** : il ne se produit que quand deux agents ecrivent au meme moment

## Cause racine

Deux agents (ou plus) font un **read-modify-write** sur le meme fichier sans lock :

```
Agent A: lit fichier    -> {"count": 5}
Agent B: lit fichier    -> {"count": 5}
Agent A: ecrit fichier  -> {"count": 6}   (5 + 1)
Agent B: ecrit fichier  -> {"count": 6}   (5 + 1, ecrase le 6 de A)
                                           -> attendu: 7, reel: 6
```

C'est une race condition classique. Le probleme est aggrave dans les systemes multi-agents car :
- Les agents tournent souvent en parallele (threads, processes, cron jobs)
- Les fichiers JSON ne supportent pas les ecritures concurrentes
- L'intervalle entre read et write peut etre long (appel LLM entre les deux)

## Detection

Comparer les ecritures attendues vs reelles dans un fichier partage. Si le compteur final est inferieur a la somme des increments, il y a eu une race condition. Voir `example.py`.

## Correction

1. **File lock** : utiliser un lock fichier (`fcntl.flock` ou un `.lock` file) avant chaque read-modify-write
2. **Ecriture atomique** : ecrire dans un fichier temporaire puis renommer (atomique sur la plupart des OS)
3. **Base de donnees** : utiliser SQLite avec transactions au lieu de fichiers JSON

Voir `example.py` pour le pattern file lock.

## Prevention

- **Pas de fichier JSON comme state store concurrent** : utiliser SQLite ou un key-value store
- **Lock obligatoire** pour tout fichier partage entre agents
- **Ecriture atomique** : write-to-temp + rename au lieu de write-in-place
- **Un seul writer** : designer un agent comme "owner" du fichier, les autres lui envoient des messages
- **Test de concurrence** : lancer 10 agents en parallele sur le meme fichier et verifier la coherence

## Playbook complet

Fiche detaillee avec file lock cross-platform, ecriture atomique, migration vers SQLite, et cas reel ou un usage tracker LLM perdait 30% des increments : [lien a venir]
