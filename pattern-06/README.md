# Pattern 06 — Silent NameError in try/except

> Applicable a : tout systeme multi-agents ou les agents communiquent via des objets partages, et ou les erreurs sont capturees par des blocs try/except larges. Frequent dans les pipelines LangChain avec gestion d'erreur generique, les agents CrewAI qui catchent Exception pour "resilience", et les workflows AutoGen ou un agent defaillant ne doit pas bloquer les autres.

## Symptome

- Une fonctionnalite critique **ne s'execute jamais** mais aucune erreur n'apparait dans les logs
- Le systeme semble fonctionner normalement — aucun crash, aucun warning
- En inspectant les donnees, on decouvre qu'une branche entiere du code est morte depuis des semaines
- Le bloc `except` silencieux masque un `NameError` qui se produit a chaque cycle

## Cause racine

Une variable est **utilisee avant son assignation**. Dans un code lineaire, Python leverait un `NameError` immediat. Mais quand le code est enveloppe dans un `try/except Exception`, l'erreur est capturee silencieusement et le flux continue comme si de rien n'etait.

```python
try:
    if status.get("has_position"):   # NameError: 'status' n'existe pas encore
        close_position()
    # ... 30 lignes plus bas ...
    status = read_status(agent_id)   # Assignation APRES utilisation
except Exception:
    pass  # Le NameError est avale ici
```

Le piege : le code **semble** correct a la lecture (status est bien defini dans la fonction). Mais l'ordre d'execution fait que l'utilisation precede l'assignation. Et le `except` generique rend le bug totalement invisible.

## Detection

Scanner le code pour les patterns `try/except Exception` ou `try/except:` (bare except) qui contiennent des variables potentiellement non definies. Voir `example.py` pour un detecteur statique simplifie.

## Correction

1. **Deplacer l'assignation avant l'utilisation** — la correction evidente
2. **Ne jamais utiliser `except Exception: pass`** — au minimum logger l'erreur
3. **Initialiser les variables en debut de scope** — `status = None` avant le try

Voir `example.py` pour une demonstration complete.

## Prevention

- **Linter strict** : `pylint` ou `ruff` detectent les variables utilisees avant assignation
- **Interdire les bare except** : regle linter `no-bare-except` + `broad-exception-caught`
- **Logger dans chaque except** : au minimum `logging.exception("...")` pour capturer la stack trace
- **Type hints + mypy** : `mypy --strict` detecte les variables potentiellement non definies
- **Code review cible** : verifier systematiquement l'ordre d'assignation dans les blocs try

## Playbook complet

Fiche detaillee avec detecteur statique complet, patterns de gestion d'erreur robustes, et cas reel ou un force-close d'urgence n'a jamais fonctionne pendant 6 mois : [lien a venir]
