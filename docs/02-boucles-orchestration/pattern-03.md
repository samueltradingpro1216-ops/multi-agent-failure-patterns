# Pattern N°03 — Cascade de Penalites (Effet Multiplicatif)

**Categorie :** Boucles & Orchestration
**Severite :** High
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 3 a 14 jours (le systeme tourne mais sous-performe ; la cause est rarement recherchee dans les ajustements de parametres)

---

## 1. Symptome observable

Un parametre critique du systeme (budget de tokens, timeout, score de confiance, taille de batch) tombe a une valeur **absurdement basse** sans qu'aucune erreur n'apparaisse. Le systeme continue de fonctionner — il ne crashe pas, ne leve aucune exception — mais ses resultats sont negligeables.

Un budget de tokens de 4000 tombe a 280. Un timeout de 30 secondes tombe a 2.1 secondes. Un seuil de confiance de 0.25 tombe a 0.018. Les actions sont techniquement executees mais avec des parametres si bas que le resultat est inutile : les reponses LLM sont tronquees, les requetes expirent avant d'aboutir, les scores sont tous en dessous du seuil.

Le symptome est intermittent. Il se manifeste quand **plusieurs conditions negatives coincident** : un jour specifique de la semaine, pendant les heures creuses, apres une serie d'echecs. Chaque condition declenche independamment un ajustement raisonnable. C'est leur combinaison simultanee qui cree la catastrophe.

## 2. Histoire vecue (anonymisee)

Un pipeline d'analyse de donnees multi-agents utilisait 5 modules qui ajustaient dynamiquement le budget de tokens LLM en fonction des conditions du moment. Un module reduisait de 20% le mardi (historiquement le jour le plus bruite). Un autre reduisait de 50% apres 3 erreurs consecutives. Un troisieme reduisait de 30% en dehors des heures de bureau. Un quatrieme reduisait de 50% si le taux d'erreur global depassait 5%. Un cinquieme reduisait de 50% pendant la maintenance nocturne.

Un mardi soir a 23h, apres une serie d'erreurs, les 5 modules ont triggue simultanement : `0.8 x 0.5 x 0.7 x 0.5 x 0.5 = 0.07`. Le budget est tombe a 7% de sa valeur nominale — de 4000 tokens a 280. Les reponses du LLM etaient tronquees en mid-sentence, rendant les analyses inutilisables. Le systeme a tourne dans cet etat pendant 6 heures avant que l'alerte de qualite ne se declenche le lendemain matin.

## 3. Cause racine technique

Le bug se produit quand **N modules ajustent le meme parametre independamment** dans le meme cycle de traitement. Chaque module fait un read-modify-write sans connaitre les ajustements des autres :

```python
# Module 1 : ajustement temporel
config = load_config()
config["token_budget"] *= 0.8  # Mardi = -20%
save_config(config)

# Module 2 : ajustement erreurs (meme cycle, 2 secondes plus tard)
config = load_config()  # Lit la valeur DEJA reduite par Module 1
config["token_budget"] *= 0.5  # 3 erreurs = -50%
save_config(config)

# Module 3, 4, 5 : idem...
# Resultat final : 0.8 * 0.5 * 0.7 * 0.5 * 0.5 = 0.07 du nominal
```

Le probleme fondamental est que chaque module pense ajuster depuis la valeur **nominale**, alors qu'il ajuste depuis la valeur **deja reduite** par les modules precedents. Les multiplicateurs sont appliques en serie au lieu d'etre accumules puis appliques une seule fois.

C'est un probleme de **commutativity brisee** : l'ordre d'execution des modules change le resultat. Si le module 1 s'execute avant le module 2, le resultat est different que si c'est l'inverse. Et comme l'ordre depend du scheduling, le bug peut apparaitre ou disparaitre de maniere apparemment aleatoire.

## 4. Detection

### 4.1 Detection manuelle (audit code)

Chercher les endroits ou un parametre est modifie par read-modify-write :

```bash
# Chercher les patterns de multiplication/division sur des parametres de config
grep -rn "\*=\s*0\.\|/=\s*[0-9]" --include="*.py" | grep -i "config\|param\|budget\|threshold"

# Chercher les ecritures multiples au meme fichier de config
grep -rn "save_config\|write_config\|dump.*json" --include="*.py"

# Compter combien de fichiers modifient le meme parametre
grep -rln "token_budget\|risk_percent\|timeout" --include="*.py" | wc -l
```

Si > 2 fichiers modifient le meme parametre, c'est un candidat pour la cascade.

### 4.2 Detection automatisee (CI/CD)

Ajouter un test qui simule l'execution de tous les modules d'ajustement et verifie que le parametre ne descend pas en dessous d'un floor :

```python
def test_parameter_floor_after_all_adjustments():
    """Ensure no parameter drops below 20% of nominal after all adjustments."""
    nominal = {"token_budget": 4000, "timeout": 30, "confidence": 0.5}

    # Simulate worst case: all adjusters trigger simultaneously
    adjusted = apply_all_adjustments(nominal, worst_case=True)

    for param, value in adjusted.items():
        ratio = value / nominal[param]
        assert ratio >= 0.2, (
            f"CASCADE DETECTED: {param} dropped to {ratio:.1%} of nominal "
            f"({value} vs {nominal[param]})"
        )
```

### 4.3 Detection runtime (production)

Logger chaque ajustement avec sa source et detecter les cascades en temps reel :

```python
import time
from collections import defaultdict

class CascadeDetector:
    """Detects cascading modifications to the same parameter."""

    def __init__(self, window_seconds: int = 60, max_ratio: float = 0.2):
        self.window = window_seconds
        self.max_ratio = max_ratio
        self.writes: dict[str, list[tuple]] = defaultdict(list)

    def record_write(self, param: str, old_value: float, new_value: float, source: str):
        now = time.monotonic()
        self.writes[param].append((now, old_value, new_value, source))
        # Clean old entries
        cutoff = now - self.window
        self.writes[param] = [w for w in self.writes[param] if w[0] > cutoff]

        # Check for cascade
        entries = self.writes[param]
        if len(entries) >= 3:
            first_old = entries[0][1]
            last_new = entries[-1][2]
            if first_old > 0:
                ratio = last_new / first_old
                if ratio < self.max_ratio:
                    sources = [e[3] for e in entries]
                    return {
                        "cascade": True,
                        "param": param,
                        "ratio": ratio,
                        "sources": sources,
                        "message": f"CASCADE: {param} at {ratio:.1%} of nominal via {sources}"
                    }
        return {"cascade": False}
```

## 5. Correction

### 5.1 Fix immediat

Ajouter un floor absolu a chaque parametre. Quelles que soient les reductions, le parametre ne descend jamais en dessous de X% du nominal :

```python
FLOORS = {
    "token_budget": 1000,    # Minimum 1000 tokens
    "timeout": 5.0,          # Minimum 5 secondes
    "confidence": 0.1,       # Minimum 10%
}

def safe_adjust(param: str, current: float, multiplier: float) -> float:
    """Apply an adjustment with a hard floor."""
    adjusted = current * multiplier
    floor = FLOORS.get(param, current * 0.2)  # Default floor: 20% of current
    return max(adjusted, floor)
```

### 5.2 Fix robuste

Remplacer les read-modify-write independants par un **pipeline accumulatif** :

```python
from dataclasses import dataclass, field

@dataclass
class AdjustmentPipeline:
    """Accumulates adjustments, applies once with a cumulative floor."""

    base_value: float
    min_ratio: float = 0.3   # Cumulative floor: never below 30% of base
    adjustments: list = field(default_factory=list)

    def propose(self, source: str, multiplier: float, reason: str):
        """Register an adjustment WITHOUT applying it."""
        clamped = max(0.3, min(2.0, multiplier))  # Individual clamp
        self.adjustments.append({"source": source, "mult": clamped, "reason": reason})

    def compute(self) -> dict:
        """Apply all adjustments with cumulative floor."""
        cumulative = 1.0
        for adj in self.adjustments:
            cumulative *= adj["mult"]

        # Cumulative floor
        cumulative = max(self.min_ratio, cumulative)
        final = self.base_value * cumulative

        return {
            "base": self.base_value,
            "cumulative_multiplier": round(cumulative, 4),
            "final": round(final, 2),
            "adjustments": self.adjustments,
        }

# Usage: modules propose, pipeline applies once
pipeline = AdjustmentPipeline(base_value=4000, min_ratio=0.3)
pipeline.propose("time_module", 0.8, "Tuesday penalty")
pipeline.propose("error_module", 0.5, "3 consecutive errors")
pipeline.propose("night_module", 0.7, "Off-hours")
result = pipeline.compute()  # final = 4000 * 0.3 = 1200 (floored), not 4000 * 0.28 = 1120
```

## 6. Prevention architecturale

La prevention repose sur un principe : **les modules ne modifient jamais un parametre directement**. Ils soumettent des propositions d'ajustement a un pipeline centralise qui les agrege et les applique une seule fois par cycle.

Ce pipeline agit comme un **middleware de config** : il recoit les propositions de N modules, calcule le multiplicateur cumule, applique un floor configurable, et ecrit le resultat final. Les modules n'ont aucun acces en ecriture directe au parametre.

En complement, l'audit trail du pipeline (quel module a propose quel ajustement, et quel a ete le resultat final) permet de diagnostiquer immediatement pourquoi un parametre est bas. Au lieu de chercher dans 5 fichiers differents, tout est centralise dans un log unique.

## 7. Anti-patterns a eviter

1. **Read-modify-write independant sur un parametre partage.** C'est la cause directe de la cascade. Chaque module doit proposer un ajustement relatif, pas ecrire une valeur absolue.

2. **Pas de floor sur les parametres critiques.** Un token budget de 0 ou un timeout de 0.1s sont des valeurs qui cassent le systeme. Chaque parametre doit avoir un minimum defini.

3. **Multiplicateurs non bornes.** Un module qui applique `*= 0.01` peut a lui seul reduire un parametre a 1% du nominal. Chaque multiplicateur individuel doit etre clampe (ex: entre 0.3 et 2.0).

4. **Pas d'audit trail des ajustements.** Sans log de qui a modifie quoi et quand, le diagnostic d'une cascade est un cauchemar. Logger chaque ajustement avec source, ancien valeur, nouvelle valeur, et timestamp.

5. **Tester les modules d'ajustement en isolation.** Chaque module teste separement fonctionne parfaitement. C'est la combinaison qui cree le bug. Le test de cascade doit simuler l'execution simultanee de tous les modules.

## 8. Cas limites et variantes

**Variante 1 : Cascade ascendante.** Au lieu de reduire, les modules augmentent un parametre (ex: timeout, budget). Le timeout passe de 30s a 300s, les requetes prennent 5 minutes chacune, le systeme est techniquement fonctionnel mais extremement lent.

**Variante 2 : Cascade sur des parametres interdependants.** Le module A reduit le budget de tokens. Le module B augmente le nombre de retries parce que les reponses sont tronquees (budget trop bas). Le module C reduit encore le budget parce que le nombre de retries est trop eleve. Boucle de retroaction qui amplifie la cascade.

**Variante 3 : Cascade temporelle.** Les modules ne trigguent pas dans le meme cycle mais sur des cycles successifs. Le parametre descend de 5% par cycle, trop lentement pour declencher une alerte instantanee, mais apres 50 cycles il est a 7% du nominal. La "cascade lente" est la plus difficile a detecter.

## 9. Checklist d'audit

- [ ] Chaque parametre critique a un floor absolu defini et documente
- [ ] Les modules d'ajustement proposent des multiplicateurs au lieu d'ecrire des valeurs absolues
- [ ] Un pipeline centralise agrege les ajustements et applique un floor cumulatif
- [ ] L'audit trail enregistre chaque ajustement (source, ancien, nouveau, timestamp)
- [ ] Un test d'integration simule le worst case (tous les modules trigguent) et verifie le floor

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 03 — Cascade de Penalites](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-03)
- Patterns connexes : #11 (Race Condition on Shared File — les read-modify-write concurrents sont le meme mecanisme), #10 (Survival Mode Deadlock — une cascade qui reduit un seuil de confiance peut mener au deadlock)
- Lectures recommandees :
  - "Release It!" (Michael T. Nygard, 2018), chapitre sur les cascading failures et les stability patterns
  - Documentation CrewAI sur la gestion de config partagee entre agents — la section "shared state pitfalls"
