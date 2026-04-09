# Multi-Agent Debug Patterns

**Catalogue de bugs en architecture multi-agents IA**

66 patterns de bugs documentes, extraits de 1.5 an de construction et d'exploitation d'un systeme multi-agents en production. Chaque pattern inclut : symptome observable, cause racine, code de detection, correction, et prevention.

Ces patterns s'appliquent a toute architecture multi-agents : LangChain, CrewAI, AutoGen, custom Python, ou tout systeme ou des agents autonomes communiquent par fichiers, DB, API ou messages.

---

## 📖 Lire le playbook complet

Les 11 patterns sont disponibles en format long et structure sur le site dedie :

**➡️ https://samueltradingpro1216-ops.github.io/multi-agent-failure-patterns/**

Chaque fiche contient : symptome observable, histoire vecue, cause racine technique, methodes de detection (manuelle / CI / runtime), correction avec code executable, prevention architecturale, et checklist d'audit.

---

## ⭐ Tu trouves ce repo utile ?

Mets une etoile pour soutenir le projet et recevoir les notifications de mises a jour. Le catalogue est mis a jour chaque semaine.

---

## Statut

- **21 / 66 patterns** published with executable code
- 45 patterns in progress
- Full English playbook live on GitHub Pages

---

## Patterns disponibles

### Temps & Synchronisation
- [Pattern 01 — Timezone Mismatch](pattern-01/) — Un agent utilise l'heure locale au lieu de UTC pour ses filtres temporels
- [Pattern 02 — Rapid-Fire Loop](pattern-02/) — Un mecanisme de retry re-execute une action deja terminee

### Boucles & Orchestration
- [Pattern 03 — Cascade de Penalites](pattern-03/) — N agents ajustent le meme parametre independamment, la cascade le reduit a quasi-zero

### I/O & Persistence
- [Pattern 04 — Multi-File State Desync](pattern-04/) — Meme etat critique stocke en 3 endroits, jamais synchronise

### Calculs & Etat
- [Pattern 05 — Unit Mismatch 100x](pattern-05/) — Valeur unitaire incoherente entre modules = calcul 100x trop gros
- [Pattern 06 — Silent NameError](pattern-06/) — try/except generique masque un NameError, fonctionnalite silencieusement morte

### Securite & Secrets
- [Pattern 07 — Hardcoded Secret in Source](pattern-07/) — Token/cle API en clair dans le code source, expose dans git

### I/O & Persistence
- [Pattern 08 — Data Pipeline Freeze](pattern-08/) — Producteur change de format, consommateur lit des donnees gelees

### Boucles & Orchestration
- [Pattern 09 — Agent Infinite Loop](pattern-09/) — Agent recursif sans guard qui boucle indefiniment et explose les quotas

### Filtrage & Decisions
- [Pattern 10 — Survival Mode Deadlock](pattern-10/) — Seuil de confiance > score moyen = systeme bloque, aucune action executee

### Gouvernance Multi-Agents
- [Pattern 11 — Race Condition on Shared File](pattern-11/) — Pas de lock sur fichier partage entre agents, updates perdus

---

<details>
<summary>📋 Voir le catalogue complet des 66 patterns identifies (cliquer pour deplier)</summary>

### Categorie 1 — Temps & Synchronisation (10 patterns)
| # | Pattern |
|---|---------|
| 01 | **Timezone Mismatch** — Filtre temporel utilise la mauvaise timezone |
| 02 | **Command Timestamp Rejection** — Delta timestamp > timeout rejette des commandes valides |
| 03 | **Veto Timestamp Drift** — Fichier ecrit en local time, lu en server time |
| 04 | **Fake-UTC Timestamp** — datetime.now() + "Z" = faux UTC |
| 05 | **Mixed datetime.now() vs utcnow()** — Melange d'horloges dans le meme systeme |
| 06 | **Inconsistent Log Timezones** — Impossible de correler les logs entre composants |
| 07 | **Status Format Mismatch** — Formats de date differents selon les composants |
| 08 | **Deprecated utcnow()** — API Python depreciee, timezone-naive |
| 09 | **Stale Status Not Detected** — Donnees perimees non detectees si producteur crash |
| 10 | **Timing Comment Lie** — Commentaire dit X secondes, code fait Y |

### Categorie 2 — Boucles & Orchestration (9 patterns)
| # | Pattern |
|---|---------|
| 11 | **Rapid-Fire Loop** — Retry re-execute apres completion |
| 12 | **Cascade Write in Single Cycle** — N ecritures au meme fichier dans un cycle |
| 13 | **Cascade Penalty Accumulation** — Penalites cumulees reduisent un parametre a quasi-zero |
| 14 | **Duplicate Unreachable Gates** — Meme verification 2 fois, la 2e est code mort |
| 15 | **Double-Fire Same Cycle** — Deux mecanismes trigguent simultanement |
| 16 | **Iterate-Then-Skip** — Boucle traite des items disabled avant de les ignorer |
| 17 | **Cron Duplicate Execution** — Job planifie execute 2+ fois sans guard |
| 18 | **Agent Infinite Loop** — Agent recursif sans anti-loop guard |
| 19 | **Orphan Commands** — Commandes envoyees a des composants desactives |

### Categorie 3 — Calculs & Etat (10 patterns)
| # | Pattern |
|---|---------|
| 20 | **Unit Mismatch 100x** — Valeur unitaire incoherente entre modules |
| 21 | **Config Value Mismatch** — Meme parametre, valeur differente selon le contexte |
| 22 | **Variable Used Before Assignment** — NameError silencieux dans un try/except |
| 23 | **Missing Global Keyword** — Variable locale shadow le module-level |
| 24 | **Value Written to Wrong Dict** — Valeur ecrite dans le mauvais dictionnaire |
| 25 | **None Reinitialization Crash** — Variable remise a None, .get() crash en aval |
| 26 | **Baseline Reset** — Valeur de reference ecrasee, metriques derivees faussees |
| 27 | **Silent Config Override** — Parametre reecrit par un processus automatique |
| 28 | **Division Edge Case** — Division par zero silencieuse produit un resultat par defaut |
| 29 | **Counter Not Persisted** — Compteur journalier remis a 0 au restart |

### Categorie 4 — Securite & Secrets (3 patterns)
| # | Pattern |
|---|---------|
| 30 | **Hardcoded Secret in Source** — Token/cle API en clair dans le code |
| 31 | **SSL Verification Disabled** — MITM possible sur les communications |
| 32 | **Identifier Collision** — Meme identifiant pour tous les composants |

### Categorie 5 — I/O & Persistence (10 patterns)
| # | Pattern |
|---|---------|
| 33 | **Data Pipeline Freeze** — Producteur change de format, consommateur lit l'ancien |
| 34 | **Multi-File State Desync** — Meme etat stocke en 3 endroits, jamais synchronise |
| 35 | **File Handle Leak + Bare Except** — Fuite de ressource masquee par except generique |
| 36 | **json.load Without Context Manager** — Resource leak si exception pendant parsing |
| 37 | **I/O on Hot Path** — Ecriture disque a chaque evaluation dans un chemin critique |
| 38 | **Stale Lock File** — Lock fichier reste apres crash, bloque les ecritures |
| 39 | **Log Never Rotated** — Fichier log grossit sans limite |
| 40 | **Handle Recreated Every Call** — Resource lourde recreee a chaque appel |
| 41 | **Local Bypass of Write Function** — Fonction locale contourne la fonction officielle |
| 42 | **Wrong Directory Read** — Lecture dans le mauvais repertoire |

### Categorie 6 — Dead Code & Architecture (5 patterns)
| # | Pattern |
|---|---------|
| 43 | **Critical Module Never Imported** — Module entier jamais appele |
| 44 | **Dead Functions** — Fonctions definies mais jamais appelees |
| 45 | **Stub Never Implemented** — Pattern/classe avec body = pass |
| 46 | **Orphan Module No Callers** — Module sans aucun import externe |
| 47 | **Fragile Bare Import** — Import sans chemin explicite, depend de sys.path |

### Categorie 7 — Detection & Monitoring (7 patterns)
| # | Pattern |
|---|---------|
| 48 | **Snapshot vs Sustained Check** — Mesure instantanee au lieu de soutenue |
| 49 | **False Correlation in Audit** — Audit valide une action sur une correlation fausse |
| 50 | **Error Returns Success** — Fonction retourne ok=True meme sur erreur |
| 51 | **Connection Leak on Error Path** — Connexion DB non fermee dans le except |
| 52 | **Non-Standard Existence Check** — Check qui retourne toujours True |
| 53 | **Adversarial Agent Too Conservative** — Agent de verification bloque des actions valides |
| 54 | **False Positive Crash Detection** — Restart normal detecte comme crash loop |

### Categorie 8 — Filtrage & Decisions (6 patterns)
| # | Pattern |
|---|---------|
| 55 | **Missing Guard on Critical Operation** — Operation a risque sans verification de borne |
| 56 | **Threshold Mismatch Between Components** — Seuil different entre producteur et consommateur |
| 57 | **Missing Decision Conditions** — Conditions du composant source absentes dans le superviseur |
| 58 | **Overzealous Pattern Detector** — Detecteur de patterns bloque 100% des actions valides |
| 59 | **Survival Mode Deadlock** — Seuil de confiance > score moyen = systeme bloque |
| 60 | **Malformed Input Accepted as Valid** — Donnees malformees passent la validation |

### Categorie 9 — Gouvernance Multi-Agents (6 patterns)
| # | Pattern |
|---|---------|
| 61 | **LLM Pool Contention** — Agents partagent le meme pool, s'affament mutuellement |
| 62 | **Race Condition on Shared Tracker** — Pas de lock sur fichier partage entre agents |
| 63 | **Recursive Retry** — Retry recursif au lieu d'iteratif |
| 64 | **Missing File Encoding** — Encoding non specifie dans les I/O fichier |
| 65 | **Cost Policy Violation** — Agent utilise un LLM paye malgre politique free-tier |
| 66 | **Agent State Lost on Restart** — Agent perd son etat en memoire au restart |

</details>

---

## Playbook complet (en cours de redaction)

Les 5 patterns ci-dessus sont des extraits gratuits. Le **playbook complet** contiendra :
- 66 fiches detaillees (symptome, cause, detection, correction, prevention)
- Code Python executable pour chaque pattern
- Architecture recommandee pour systemes multi-agents robustes
- Checklist d'audit complete

**📬 Sois notifie du lancement** (prix early-bird -50%) : https://tally.so/r/vGODE8

---

## Structure du repo

```
pattern-XX/
  README.md    -- Description du pattern (symptome, cause, detection, correction, prevention)
  example.py   -- Code minimal reproductible (Python stdlib, zero dependance)
```

```bash
# Tester un pattern
python pattern-01/example.py
```

---

## Contexte

Ces patterns viennent d'un systeme multi-agents en production depuis 1.5 an :
- 4+ agents autonomes (superviseur, analyseur, urgence, strategie)
- Communication par fichiers JSON, SQLite et messages inter-agents
- Boucle de decision toutes les 10 secondes
- 66 bugs identifies lors d'un audit complet, dont 8 critiques

Les patterns sont generiques. Les exemples utilisent un domaine specifique (trading) pour illustrer des concepts qui s'appliquent a tout systeme multi-agents : orchestration de taches, synchronisation d'etat, gestion de configuration partagee, monitoring de sante, et gouvernance d'agents LLM.

---

## License

MIT License. Voir [LICENSE](LICENSE).

---

*English version coming soon.*
