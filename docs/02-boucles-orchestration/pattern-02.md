# Pattern N°02 — Rapid-Fire Loop (Refresh Zombie)

**Categorie :** Boucles & Orchestration
**Severite :** High
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 1 a 3 jours (le symptome est visible rapidement dans les logs ou la facturation, mais la cause racine est subtile)

---

## 1. Symptome observable

L'executeur repete la meme action **en boucle rapide**, toutes les N secondes, sans interruption. Les logs montrent un pattern repetitif : la meme commande envoyee, executee, terminee, puis immediatement re-envoyee. Ce qui aurait du etre une action unique se transforme en centaines d'executions identiques en quelques heures.

Les consequences sont proportionnelles au cout marginal de chaque execution. Si chaque iteration coute un appel API LLM, la facture explose en quelques minutes. Si chaque iteration consomme du I/O disque, les logs gonflent a des centaines de Mo par jour. Si chaque iteration execute une action reelle (envoi de message, ecriture en base), le systeme pollue ses propres donnees avec des duplicatas.

Le symptome le plus trompeur : chaque execution individuelle **semble correcte**. Le log montre "action executee avec succes". C'est la repetition qui est le bug, pas l'action elle-meme. Un monitoring qui ne compte pas les executions par fenetre de temps ne detectera rien.

## 2. Histoire vecue (anonymisee)

Un systeme de monitoring multi-agents generait des rapports d'analyse toutes les 10 secondes. Quand un rapport etait genere et que le consommateur n'avait pas de tache active, le mecanisme de refresh re-envoyait la commande de generation.

Le probleme : quand le consommateur terminait son rapport rapidement (en 2-3 secondes), son statut repassait a "idle". Le refresh voyait "idle" et re-envoyait la commande, croyant qu'elle n'avait jamais ete executee. En une journee, le systeme a genere 124 rapports identiques au lieu d'un seul. Chaque rapport coutait un appel LLM. La facture quotidienne a ete multipliee par 100.

Le bug a ete decouvert en regardant la facture du provider LLM, pas les logs — les logs montraient 124 executions "normales".

## 3. Cause racine technique

Le bug naît quand un mecanisme de refresh/retry ne distingue pas deux etats qui partagent la meme representation externe :

```
Etat 1 : "La commande n'a PAS ENCORE ete executee"     → status = IDLE
Etat 2 : "La commande a ete executee et est TERMINEE"  → status = IDLE

Le refresh voit IDLE et re-envoie dans les deux cas.
```

Le cycle de vie implicite ressemble a ca :

```
Signal → Commande ecrite → Executeur lit → Execution → Fin
                                                        ↓
                                                   status = IDLE
                                                        ↓
                                              Refresh voit IDLE
                                                        ↓
                                              Re-ecrit la commande
                                                        ↓
                                              Executeur re-execute → BOUCLE
```

Le probleme fondamental est l'**absence de state machine explicite**. Le systeme utilise un booleen (`has_active_task`) au lieu d'un enum a 4 etats (`IDLE → PENDING → EXECUTING → COMPLETED`). Quand `has_active_task` repasse a `False` apres completion, c'est indiscernable de "jamais execute".

Les variantes incluent :
- Un orchestrateur LangChain qui re-dispatche une tache parce que le resultat a ete consomme (le callback l'a lu et supprime)
- Un agent CrewAI qui re-delegue une tache a un sous-agent parce que le `task_output` a ete nettoye par le garbage collector
- Un cron job qui relance un agent "si pas de resultat recent", sans verifier si un resultat a deja ete produit et archive

## 4. Detection

### 4.1 Detection manuelle (audit code)

Chercher les patterns de refresh/retry qui ne trackent pas l'historique d'execution :

```bash
# Chercher les boucles de retry basees sur un statut booleen
grep -rn "while.*not.*done\|if.*status.*idle\|if not.*active" --include="*.py"

# Chercher les mecanismes de re-envoi de commandes
grep -rn "refresh.*command\|retry.*send\|re.*dispatch" --include="*.py"

# Chercher les fichiers/flags effaces apres execution (perte de memoire)
grep -rn "os\.remove\|\.unlink\|delete.*after" --include="*.py"
```

Verifier pour chaque occurrence : est-ce que le code distingue "jamais execute" de "execute et termine" ?

### 4.2 Detection automatisee (CI/CD)

Ajouter un test d'integration qui simule le cycle complet commande → execution → fin → verification que le refresh ne re-envoie pas :

```python
def test_no_rapid_fire_after_completion():
    """Verify that a completed task is not re-dispatched by the refresh mechanism."""
    orchestrator = Orchestrator()
    executor = MockExecutor()

    # Send a task
    orchestrator.dispatch("task-1", executor)
    assert executor.execution_count == 1

    # Simulate task completion
    executor.complete("task-1")

    # Run 10 refresh cycles
    for _ in range(10):
        orchestrator.refresh()

    # Should still be 1, not 11
    assert executor.execution_count == 1, (
        f"Rapid-fire detected: task executed {executor.execution_count} times"
    )
```

### 4.3 Detection runtime (production)

Compter les executions par identifiant de tache dans une fenetre glissante :

```python
import time
from collections import defaultdict

class RapidFireDetector:
    """Detects when the same task is executed too many times in a window."""

    def __init__(self, window_seconds: int = 300, max_executions: int = 5):
        self.window = window_seconds
        self.max = max_executions
        self.history: dict[str, list[float]] = defaultdict(list)

    def record(self, task_id: str) -> bool:
        """Record an execution. Returns False if rapid-fire detected."""
        now = time.monotonic()
        cutoff = now - self.window

        # Clean old entries
        self.history[task_id] = [t for t in self.history[task_id] if t > cutoff]

        if len(self.history[task_id]) >= self.max:
            return False  # RAPID-FIRE

        self.history[task_id].append(now)
        return True

# Integration in the execution pipeline:
detector = RapidFireDetector(window_seconds=300, max_executions=5)

def execute_task(task_id: str, payload: dict):
    if not detector.record(task_id):
        logging.critical(f"RAPID-FIRE blocked: {task_id} executed {detector.max}+ times in {detector.window}s")
        return
    # ... actual execution
```

## 5. Correction

### 5.1 Fix immediat

Ajouter un cooldown entre deux executions de la meme tache :

```python
import time

_last_execution: dict[str, float] = {}
COOLDOWN_SECONDS = 60

def should_execute(task_id: str) -> bool:
    """Block re-execution if the same task ran recently."""
    now = time.monotonic()
    last = _last_execution.get(task_id, 0)
    if now - last < COOLDOWN_SECONDS:
        return False
    _last_execution[task_id] = now
    return True
```

### 5.2 Fix robuste

Implementer une state machine explicite pour chaque commande :

```python
from enum import Enum
from datetime import datetime, timezone

class TaskState(Enum):
    PENDING = "pending"       # Commande creee, pas encore lue par l'executeur
    EXECUTING = "executing"   # Executeur a commence le traitement
    COMPLETED = "completed"   # Traitement termine avec succes
    FAILED = "failed"         # Traitement echoue
    EXPIRED = "expired"       # Timeout depasse sans execution

class TaskTracker:
    """Tracks the lifecycle of each task. Refresh only re-sends PENDING tasks."""

    def __init__(self, max_age_seconds: int = 120):
        self.tasks: dict[str, dict] = {}
        self.max_age = max_age_seconds

    def create(self, task_id: str, payload: dict):
        self.tasks[task_id] = {
            "state": TaskState.PENDING,
            "payload": payload,
            "created_at": datetime.now(timezone.utc),
            "executed_at": None,
            "completed_at": None,
        }

    def mark_executing(self, task_id: str):
        if task_id in self.tasks:
            self.tasks[task_id]["state"] = TaskState.EXECUTING
            self.tasks[task_id]["executed_at"] = datetime.now(timezone.utc)

    def mark_completed(self, task_id: str):
        if task_id in self.tasks:
            self.tasks[task_id]["state"] = TaskState.COMPLETED
            self.tasks[task_id]["completed_at"] = datetime.now(timezone.utc)

    def should_refresh(self, task_id: str) -> bool:
        """Only PENDING tasks should be refreshed. Never COMPLETED or EXECUTING."""
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task["state"] != TaskState.PENDING:
            return False
        # Expire old pending tasks
        age = (datetime.now(timezone.utc) - task["created_at"]).total_seconds()
        if age > self.max_age:
            task["state"] = TaskState.EXPIRED
            return False
        return True
```

## 6. Prevention architecturale

Le pattern de prevention est la **state machine explicite avec identifiant unique par commande**. Chaque commande recoit un UUID a la creation. L'executeur confirme l'execution en renvoyant cet UUID. Le refresh ne re-envoie que les commandes dont l'UUID n'a pas ete confirme.

Cette architecture elimine le probleme a la racine : il n'y a plus d'ambiguite entre "pas encore execute" et "execute et termine". L'UUID sert de preuve d'execution.

En complement, un **budget d'execution par fenetre de temps** agit comme filet de securite. Meme si la state machine a un bug, le budget empeche plus de N executions de la meme tache par heure. C'est le circuit breaker de dernier recours.

Enfin, le mecanisme de refresh ne devrait jamais etre le seul moyen de declencher une execution. Si l'executeur a besoin d'une nouvelle tache, il la **demande** (pull) au lieu de la recevoir passivement (push). Le pull elimine les refresh zombies car c'est l'executeur qui controle le rythme.

## 7. Anti-patterns a eviter

1. **Utiliser un booleen pour tracker l'etat d'une commande.** `is_active = True/False` ne peut pas distinguer 4 etats (pending, executing, completed, failed). Utiliser un enum ou un string avec des valeurs explicites.

2. **Supprimer le fichier de commande apres execution.** L'absence de fichier est ambigue : "jamais cree" ou "cree et supprime apres execution" ? Toujours garder une trace de completion.

3. **Refresh base sur un timer sans memoire.** "Toutes les 10 secondes, verifier si une tache est active" sans tracker si la tache a deja ete executee est la recette du rapid-fire.

4. **Pas de limite d'executions par tache.** Meme avec une state machine correcte, un hard cap (ex: max 3 executions par tache) previent les boucles infinies en cas de bug.

5. **Logger "execution reussie" sans compter.** Si le log dit "task executed successfully" 200 fois en une heure pour la meme tache, c'est un bug — mais personne ne le voit sans compteur.

## 8. Cas limites et variantes

**Variante 1 : Retry apres echec qui ne detecte pas le succes partiel.** L'agent execute 80% d'une tache, echoue sur les 20% restants. Le retry relance la tache entiere, incluant les 80% deja faits. Resultat : actions dupliquees (emails envoyes deux fois, donnees inserees en double).

**Variante 2 : Callback perdu.** L'executeur termine la tache et envoie un callback "done". Le callback est perdu (reseau, queue pleine, crash du recepteur). L'orchestrateur ne recoit jamais la confirmation et re-envoie. Cela necessite un mecanisme d'idempotence cote executeur.

**Variante 3 : Race condition temporelle.** L'executeur termine a T=10.001s. Le refresh se declenche a T=10.000s, juste avant la completion. Il re-envoie la commande parce qu'au moment du check, la tache etait encore "en cours". Cela necessite un lock entre le refresh et le callback de completion.

**Variante 4 : Cascade d'agents.** L'agent A declenche l'agent B. L'agent B echoue et retourne un signal a l'agent A. L'agent A re-declenche l'agent B. L'agent B echoue a nouveau. Sans limite de profondeur, c'est une boucle a deux acteurs.

## 9. Checklist d'audit

- [ ] Chaque commande/tache a une state machine explicite (pas un booleen)
- [ ] Le refresh ne re-envoie que les taches en etat PENDING (jamais COMPLETED ou EXECUTING)
- [ ] Un cooldown ou un compteur limite les executions de la meme tache
- [ ] Le monitoring compte les executions par tache et alerte si > N en T secondes
- [ ] Les tests d'integration verifient que completion → refresh ne re-execute pas

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 02 — Rapid-Fire Loop](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-02)
- Patterns connexes : #09 (Agent Infinite Loop — boucle au niveau de l'agent lui-meme, pas du refresh), #03 (Cascade de Penalites — les re-executions repetees peuvent cumuler des penalites)
- Lectures recommandees :
  - "Designing Data-Intensive Applications" (Martin Kleppmann, 2017), chapitre 11 sur l'idempotence et le exactly-once processing
  - Documentation LangChain sur les retry policies et les RunnableWithFallbacks — ou ce pattern exact est documente comme un piege connu
