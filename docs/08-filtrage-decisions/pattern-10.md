# Pattern N°10 — Survival Mode Deadlock

**Categorie :** Filtrage & Decisions
**Severite :** Critical
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 1 a 7 jours (le systeme est vivant mais inactif — la cause n'est pas cherchee tant que personne ne remarque l'absence d'actions)

---

## 1. Symptome observable

Le systeme est **vivant mais ne fait rien**. Les logs montrent que des evaluations ont lieu, des scores sont calcules, des decisions sont prises — mais la decision est toujours la meme : **rejete**. 100% des propositions sont vetoes. Aucune action n'est executee depuis des heures, des jours, voire des semaines.

Le monitoring systeme est au vert : CPU normal, memoire normale, pas de crash, pas d'erreur. Le systeme tourne, il evalue, il decide — il decide juste de ne rien faire. C'est un deadlock qui ne ressemble pas a un deadlock car il n'y a ni thread bloque ni ressource verrouille. C'est un **deadlock logique** : le systeme est piege dans un etat ou il ne peut pas progresser.

Le symptome le plus pervers : le cercle vicieux. Sans executer d'actions, le systeme ne genere pas de nouvelles donnees. Sans nouvelles donnees, les metriques ne s'ameliorent pas. Sans amelioration des metriques, le score reste en dessous du seuil. Le systeme est pris dans une boucle de retroaction negative dont il ne peut pas sortir seul.

## 2. Histoire vecue (anonymisee)

Un systeme de decisions automatisees avait un mecanisme de confiance : chaque proposition recevait un score de 0 a 100, et seules les propositions avec un score >= 50 etaient executees. Le seuil de 50 avait ete choisi arbitrairement ("la moitie, ca semble raisonnable").

Apres un changement de conditions externes (un parametre de marche avait change), le score moyen des propositions est tombe de 55 a 42. Soudainement, 100% des propositions etaient en dessous du seuil. Le systeme est entre en "mode survie" — un etat prevu pour les periodes difficiles ou il est plus prudent de ne rien faire.

Le probleme : le mode survie etait permanent. Sans executer d'actions, les metriques de performance stagnaient (pas de nouvelles donnees). Le module de scoring, base en partie sur les performances recentes, gardait un score bas. Le seuil, statique, ne bougeait pas. Le systeme est reste bloque pendant 4 jours avant que l'equipe ne remarque qu'aucune action n'avait ete executee.

## 3. Cause racine technique

Le bug se produit quand un seuil de qualite **statique** est plus eleve que le score moyen que le systeme peut produire dans les conditions actuelles :

```
Conditions normales:
    Score moyen:  55/100
    Seuil:        50/100
    Taux d'acceptation: ~70%     ← Systeme fonctionne

Conditions degradees:
    Score moyen:  42/100
    Seuil:        50/100
    Taux d'acceptation: ~2%      ← Quasi-bloque

Conditions adverses:
    Score moyen:  35/100
    Seuil:        50/100
    Taux d'acceptation: 0%       ← DEADLOCK TOTAL
```

Le mecanisme de deadlock est un **cercle vicieux a 4 etapes** :

```
1. Conditions adverses → score moyen baisse
2. Score < seuil → 100% des actions rejetees
3. 0 actions executees → 0 nouvelles donnees
4. 0 nouvelles donnees → metriques stagnent → score reste bas
→ Retour a l'etape 2 : DEADLOCK PERMANENT
```

Le probleme fondamental est que le seuil est **decouple des conditions reelles**. Un seuil de 50 a du sens quand les scores sont normalement entre 40 et 80. Il n'a plus de sens quand les scores sont entre 25 et 45 — un changement qui peut survenir a cause de facteurs completement exterieurs au systeme.

## 4. Detection

### 4.1 Detection manuelle (audit code)

Chercher les seuils statiques dans les mecanismes de decision :

```bash
# Chercher les comparaisons de score/confiance avec des constantes
grep -rn "confidence.*>=\|score.*>=\|threshold\|>= 50\|>= 0\.5" --include="*.py"

# Chercher les mecanismes de veto/rejet
grep -rn "VETO\|REJECT\|BLOCK\|DENIED\|rejected" --include="*.py"

# Chercher les compteurs de rejets (si le code en a)
grep -rn "reject.*count\|veto.*count\|blocked.*count" --include="*.py"
```

Pour chaque seuil statique trouve, verifier : existe-t-il un mecanisme de fallback si le taux d'acceptation tombe a 0% ?

### 4.2 Detection automatisee (CI/CD)

Tester que le systeme ne peut pas rester bloque indefiniment :

```python
def test_no_permanent_deadlock():
    """Simulate worst-case scores and verify the system eventually acts."""
    scorer = MockScorer(mean=35, std=5)  # Scores always below 50
    gate = ConfidenceGate(threshold=50)

    # Simulate 200 cycles
    actions_taken = 0
    for _ in range(200):
        score = scorer.generate()
        if gate.evaluate(score):
            actions_taken += 1

    assert actions_taken > 0, (
        "DEADLOCK: 0 actions in 200 cycles with scores mean=35. "
        "The gate has no fallback mechanism."
    )
```

### 4.3 Detection runtime (production)

Monitorer le taux d'acceptation en temps reel et alerter si 0% pendant > N cycles :

```python
import time
from collections import deque

class DeadlockDetector:
    """Detects when acceptance rate drops to 0% for too long."""

    def __init__(self, window_size: int = 100, alert_after_zero_pct: int = 50):
        self.window = deque(maxlen=window_size)
        self.alert_threshold = alert_after_zero_pct

    def record(self, accepted: bool):
        self.window.append(accepted)

    def check(self) -> dict:
        if len(self.window) < self.alert_threshold:
            return {"deadlock": False, "reason": "not enough data"}

        recent = list(self.window)[-self.alert_threshold:]
        acceptance_rate = sum(recent) / len(recent)

        if acceptance_rate == 0:
            return {
                "deadlock": True,
                "zero_streak": self.alert_threshold,
                "message": f"DEADLOCK: 0% acceptance over {self.alert_threshold} cycles",
            }

        return {"deadlock": False, "acceptance_rate": acceptance_rate}
```

## 5. Correction

### 5.1 Fix immediat

Ajouter un seuil minimum garanti : si le taux d'acceptation est 0% pendant N cycles, baisser temporairement le seuil :

```python
class EmergencyThresholdOverride:
    """Temporarily lowers threshold when deadlocked."""

    def __init__(self, normal_threshold: float, emergency_threshold: float, trigger_after: int = 50):
        self.normal = normal_threshold
        self.emergency = emergency_threshold
        self.trigger = trigger_after
        self.consecutive_rejects = 0

    def evaluate(self, score: float) -> bool:
        threshold = self.normal

        if self.consecutive_rejects >= self.trigger:
            threshold = self.emergency  # Emergency mode

        if score >= threshold:
            self.consecutive_rejects = 0
            return True
        else:
            self.consecutive_rejects += 1
            return False
```

### 5.2 Fix robuste

Implementer un seuil adaptatif avec fenetre d'exploration :

```python
class AdaptiveGate:
    """
    Three mechanisms to prevent deadlock:
    1. Adaptive threshold that decays when acceptance is 0%
    2. Exploration window: accept 1 in N regardless of score
    3. Percentile-based threshold instead of absolute value
    """

    def __init__(
        self,
        initial_threshold: float = 50.0,
        min_threshold: float = 20.0,
        decay_per_reject: float = 0.5,
        explore_every: int = 20,
    ):
        self.threshold = initial_threshold
        self.initial = initial_threshold
        self.minimum = min_threshold
        self.decay = decay_per_reject
        self.explore_every = explore_every
        self.total_evals = 0
        self.consecutive_rejects = 0
        self.scores_history = []

    def evaluate(self, score: float) -> dict:
        self.total_evals += 1
        self.scores_history.append(score)

        # Mechanism 1: adaptive decay
        if self.consecutive_rejects > 0 and self.consecutive_rejects % 10 == 0:
            self.threshold = max(self.minimum, self.threshold - self.decay)

        # Mechanism 2: exploration window
        if self.total_evals % self.explore_every == 0 and self.consecutive_rejects > 0:
            self.consecutive_rejects = 0
            return {"accepted": True, "reason": "exploration", "threshold": self.threshold}

        # Normal evaluation
        if score >= self.threshold:
            self.consecutive_rejects = 0
            # Recovery: slowly raise threshold back to initial
            self.threshold = min(self.initial, self.threshold + self.decay * 0.1)
            return {"accepted": True, "reason": "above_threshold", "threshold": self.threshold}
        else:
            self.consecutive_rejects += 1
            return {"accepted": False, "reason": "below_threshold", "threshold": self.threshold}
```

## 6. Prevention architecturale

Le principe fondamental : **un systeme ne doit jamais pouvoir s'arreter indefiniment a cause d'un seuil de qualite**. Meme dans le pire cas, il doit executer un minimum d'actions pour generer des donnees et pouvoir s'auto-corriger.

**1. Seuil adaptatif, jamais statique.** Le seuil doit s'ajuster aux conditions reelles. Si les scores moyens baissent, le seuil baisse aussi (avec un floor). Si les scores remontent, le seuil remonte.

**2. Fenetre d'exploration obligatoire.** Meme en mode ultra-conservateur, le systeme accepte au minimum 1 proposition sur N (ex: 1 sur 20). Cette "exploration" garantit que de nouvelles donnees sont generees, cassant le cercle vicieux.

**3. Seuil par percentile au lieu d'absolu.** Accepter le "top 10% des scores" au lieu de "score >= 50". Un percentile garantit un taux d'acceptation minimum quel que soit le niveau absolu des scores.

**4. Monitoring du taux d'acceptation comme KPI critique.** Le taux d'acceptation doit etre monitore au meme titre que la latence ou le taux d'erreur. Un taux de 0% pendant > 1h est une alerte P1.

## 7. Anti-patterns a eviter

1. **Seuil statique sans mecanisme de fallback.** `if score >= 50` sans aucune alternative quand le score moyen est 42 = deadlock garanti.

2. **"Mode survie" permanent.** Un mode conservateur sans condition de sortie automatique. Si le mode survie necessite une intervention humaine pour en sortir, il est permanent en dehors des heures de bureau.

3. **Score base uniquement sur les performances recentes.** Si le score depend des resultats des N dernieres actions et qu'il n'y a pas d'action, le score est base sur des donnees de plus en plus vieilles — il ne s'ameliorera jamais.

4. **Pas de separation entre scoring et gating.** Le module qui calcule le score ne devrait pas etre le meme que celui qui decide d'accepter ou non. Le gating peut avoir des regles d'exploration que le scoring ignore.

5. **Tester le gating uniquement avec des scores eleves.** Si les tests ne simulent jamais un score moyen en dessous du seuil, le deadlock n'est jamais detecte avant la production.

## 8. Cas limites et variantes

**Variante 1 : Deadlock partiel.** Le systeme accepte les actions de type A (score eleve) mais bloque 100% des actions de type B (score bas). Le monitoring global montre un taux d'acceptation > 0% mais le type B est completement mort.

**Variante 2 : Deadlock oscillant.** Le seuil adaptatif descend, quelques actions passent, les metriques s'ameliorent marginalement, le seuil remonte, tout est re-bloque. Le systeme oscille entre "bloque" et "quasi-bloque" sans jamais se stabiliser.

**Variante 3 : Deadlock par accumulation de vetos.** Le systeme a 5 gates independantes (confiance, regime, pattern, timing, adversarial). Chacune a un taux d'acceptation de 80%. Le taux cumule est `0.8^5 = 32%`. Si une gate tombe a 60%, le cumule tombe a 0.6 * 0.8^4 = 24%. Ajouter une 6eme gate a 90% fait tomber le cumule a 22%. L'accumulation de gates "raisonnables" cree un filtre ultra-restrictif.

**Variante 4 : Deadlock saisonnier.** Le score depend de facteurs externes (conditions de marche, charge utilisateur, disponibilite API). Pendant certaines periodes (weekends, vacances, maintenance), les scores sont systematiquement bas. Le deadlock apparait le vendredi soir et se resout le lundi matin — sans que personne ne le remarque.

## 9. Checklist d'audit

- [ ] Aucun seuil de decision n'est purement statique (chacun a un mecanisme adaptatif ou un fallback)
- [ ] Le taux d'acceptation est monitore et alerte si 0% pendant > 1h
- [ ] Une fenetre d'exploration garantit au minimum 1 action par N cycles
- [ ] Le worst case est teste : que se passe-t-il si le score moyen est 50% en dessous du seuil ?
- [ ] Le deadlock detector est actif en production et envoie des alertes

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 10 — Survival Mode Deadlock](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-10)
- Patterns connexes : #03 (Cascade de Penalites — une cascade qui baisse le score peut declencher le deadlock), #09 (Agent Infinite Loop — un agent qui boucle en cherchant un score impossible)
- Lectures recommandees :
  - "Reinforcement Learning: An Introduction" (Sutton & Barto), chapitre sur l'exploration vs exploitation — le dilemme fondamental que ce pattern illustre
  - Documentation AutoGen sur les "termination conditions" — comment definir quand un agent doit s'arreter meme sans resultat parfait
