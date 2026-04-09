# Pattern 01 — Timezone Mismatch

> Applicable a : tout systeme multi-agents ou des composants tournent sur des serveurs/timezones differents (orchestrateurs LangChain distribues, agents CrewAI cloud, pipelines AutoGen multi-region).

## Symptome

- Un filtre horaire bloque les **mauvaises heures** (decale de 2-3h)
- Le systeme rate sa fenetre d'activite optimale ou agit pendant les heures creuses
- Invisible en test local si le poste dev est en UTC

## Cause racine

Un composant utilise `datetime.now()` ou l'heure du serveur local au lieu de UTC explicite. En production, chaque noeud peut etre dans une timezone differente (UTC+2, UTC+3, heure locale du cloud provider).

## Detection

Comparer les timestamps de deux composants. Si le delta est proche d'un multiple d'heure (1h, 2h, 3h) au lieu d'un drift reseau normal (< 60s), c'est un mismatch timezone. Voir `example.py`.

## Correction

Remplacer toutes les sources de temps par `datetime.now(timezone.utc)`. Centraliser l'obtention du temps dans une seule fonction appelee partout. Voir `example.py`.

## Prevention

- Interdire `datetime.now()` sans `tz=` via un linter ou pre-commit hook
- Tester avec `TZ=Pacific/Chatham` (UTC+12:45) pour detecter les hypotheses implicites
- Alerter si le drift entre composants > 5min et proche d'un multiple d'heure

## Playbook complet

Fiche detaillee avec detection automatisee, classe TimeNormalizer complete, et histoire reelle : [lien a venir]
