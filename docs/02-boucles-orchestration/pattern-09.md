# Pattern N°09 — Agent Infinite Loop

**Categorie :** Boucles & Orchestration
**Severite :** Critical
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 0 a 1 jour (les consequences financieres sont immediates — la facture LLM explose ou le systeme tombe par OOM/CPU saturation)

---

## 1. Symptome observable

Un agent consomme **100% du CPU** ou explose le quota d'appels API en quelques minutes. Les logs montrent le meme message repete des centaines de fois. La facture LLM passe de $5/jour a $500 en une heure. Le systeme entier ralentit car l'agent boucle monopolise les ressources partagees (pool de connexions HTTP, rate limits, memoire).

Contrairement au Rapid-Fire Loop (Pattern #02) qui est cause par un **refresh externe**, ici c'est l'agent lui-meme qui se re-invoque. L'agent a un mecanisme de reflexion, de retry, ou de re-delegation qui boucle indefiniment :

```
Agent → "Ma reponse n'est pas assez bonne" → re-invocation
     → "Ma reponse n'est pas assez bonne" → re-invocation
     → "Ma reponse n'est pas assez bonne" → re-invocation
     → ... (indefiniment)
```

Le symptome est souvent decouvert par un **quota exceeded** du provider LLM, un `RecursionError` Python, ou une alerte de monitoring systeme (CPU > 95% pendant > 5 minutes).

## 2. Histoire vecue (anonymisee)

Un systeme multi-agents avait un agent "reflexion" qui evaluait la qualite de ses propres reponses avant de les transmettre. Si le score de qualite etait inferieur a 90%, l'agent re-generait sa reponse. Le seuil etait ambitieux mais raisonnable pour des cas simples.

Un vendredi soir, un utilisateur a soumis une requete ambigue dont la reponse parfaite n'existait pas. L'agent a genere une reponse (score: 72%), l'a evaluee, re-genere (score: 68%), re-evaluee, re-genere (score: 74%)... Le score oscillait autour de 70%, jamais assez pour atteindre 90%. En 15 minutes, l'agent avait fait 2000 appels LLM, consomme le quota quotidien, et la facture du provider affichait $340.

Le systeme n'avait ni compteur de profondeur, ni timeout global, ni budget max par agent. L'agent etait "libre de recommencer autant qu'il le voulait" — une liberte qui s'est averee dangereuse.

## 3. Cause racine technique

L'agent a un mecanisme de re-invocation **non borne**. Trois variantes courantes :

**Variante 1 : Recursion directe.** L'agent s'appelle lui-meme :

```python
class ReflectionAgent:
    def run(self, task: str) -> str:
        response = self.llm.generate(task)
        quality = self.evaluate(response)

        if quality < 0.9:
            return self.run(task)  # Recursion sans limite

        return response
```

**Variante 2 : Boucle de delegation.** L'agent A delegue a l'agent B qui re-delegue a l'agent A :

```
Agent A → "Je ne sais pas, demandons a B" → Agent B
Agent B → "Je ne sais pas, demandons a A" → Agent A
→ Ping-pong infini entre A et B
```

**Variante 3 : Side-effect qui re-declenche.** Un cron job lance un agent. L'agent ecrit un fichier. Le cron job detecte le nouveau fichier et relance l'agent :

```
Cron → lance Agent → Agent ecrit output.json → Cron detecte le changement → relance Agent
```

Le probleme fondamental est l'absence de **condition d'arret garantie**. L'agent peut toujours trouver une raison de recommencer, et rien ne l'en empeche.

## 4. Detection

### 4.1 Detection manuelle (audit code)

Chercher les appels recursifs et les boucles sans borne :

```bash
# Recursion directe : methode qui s'appelle elle-meme
grep -rn "def \(\w\+\).*:" --include="*.py" | while read line; do
    func=$(echo "$line" | grep -oP "def \K\w+")
    file=$(echo "$line" | cut -d: -f1)
    if grep -q "self\.$func\|$func(" "$file" 2>/dev/null; then
        echo "RECURSION: $line"
    fi
done

# Boucles while True sans break conditionnel clair
grep -rn "while True\|while 1:" --include="*.py" -A5 | grep -v "break"

# Agents qui se re-invoquent via un dispatch
grep -rn "dispatch\|invoke\|delegate\|re.*run" --include="*.py"
```

### 4.2 Detection automatisee (CI/CD)

Tester que chaque agent a un max_depth et un timeout :

```python
def test_all_agents_have_depth_limit():
    """Every agent that can recurse must have a max_depth parameter."""
    from agents import ALL_AGENTS

    for agent_cls in ALL_AGENTS:
        sig = inspect.signature(agent_cls.run)
        has_depth = "max_depth" in sig.parameters or "depth" in sig.parameters
        has_timeout = "timeout" in sig.parameters

        assert has_depth or has_timeout, (
            f"{agent_cls.__name__}.run() has no depth/timeout parameter. "
            f"Add max_depth or timeout to prevent infinite loops."
        )
```

### 4.3 Detection runtime (production)

Circuit breaker + invocation monitor :

```python
import time
from collections import defaultdict

class AgentCircuitBreaker:
    """Cuts an agent after N invocations in T seconds."""

    def __init__(self, max_invocations: int = 10, window_seconds: int = 60, cooldown_seconds: int = 300):
        self.max = max_invocations
        self.window = window_seconds
        self.cooldown = cooldown_seconds
        self.history: dict[str, list[float]] = defaultdict(list)
        self.tripped_at: dict[str, float] = {}

    def can_invoke(self, agent_id: str) -> bool:
        now = time.monotonic()

        # Check cooldown
        if agent_id in self.tripped_at:
            if now - self.tripped_at[agent_id] < self.cooldown:
                return False
            del self.tripped_at[agent_id]

        # Clean old entries
        cutoff = now - self.window
        self.history[agent_id] = [t for t in self.history[agent_id] if t > cutoff]

        if len(self.history[agent_id]) >= self.max:
            self.tripped_at[agent_id] = now
            return False

        self.history[agent_id].append(now)
        return True
```

## 5. Correction

### 5.1 Fix immediat

Ajouter un compteur de profondeur :

```python
def run(self, task: str, _depth: int = 0, max_depth: int = 3) -> str:
    if _depth >= max_depth:
        return self.last_response or "Max depth reached"

    response = self.llm.generate(task)
    quality = self.evaluate(response)
    self.last_response = response

    if quality < 0.9:
        return self.run(task, _depth=_depth + 1, max_depth=max_depth)

    return response
```

### 5.2 Fix robuste

Combiner trois mecanismes : profondeur, timeout, et budget :

```python
import time

class SafeAgent:
    """Agent with triple anti-loop protection: depth, timeout, budget."""

    def __init__(self, max_depth: int = 5, timeout_seconds: float = 30.0, max_llm_calls: int = 20):
        self.max_depth = max_depth
        self.timeout = timeout_seconds
        self.max_calls = max_llm_calls
        self.call_count = 0
        self.start_time = None

    def run(self, task: str, _depth: int = 0) -> str:
        if self.start_time is None:
            self.start_time = time.monotonic()

        # Guard 1: depth
        if _depth >= self.max_depth:
            return f"[DEPTH_LIMIT] Best effort after {_depth} iterations"

        # Guard 2: timeout
        elapsed = time.monotonic() - self.start_time
        if elapsed > self.timeout:
            return f"[TIMEOUT] Stopped after {elapsed:.1f}s"

        # Guard 3: budget
        self.call_count += 1
        if self.call_count > self.max_calls:
            return f"[BUDGET_EXHAUSTED] Stopped after {self.call_count} LLM calls"

        response = self.generate(task)
        quality = self.evaluate(response)

        if quality < 0.9:
            return self.run(task, _depth=_depth + 1)

        return response
```

## 6. Prevention architecturale

La prevention repose sur le principe de **bounded execution** : aucun agent ne peut s'executer indefiniment, quels que soient les inputs ou les conditions.

**1. Budget par agent.** Chaque agent recoit un budget d'execution (tokens, appels API, secondes CPU) a la creation. Quand le budget est epuise, l'agent retourne son meilleur resultat et s'arrete. Le budget est gere par le framework, pas par l'agent lui-meme.

**2. Separation reflexion/execution.** L'agent qui genere une reponse n'est pas le meme que celui qui decide de re-generer. Un "juge" externe decide si la qualite est suffisante. Le juge a son propre budget (typiquement 2-3 evaluations max).

**3. Worst-case return.** Tout agent doit pouvoir retourner un resultat a tout moment, meme si ce resultat est imparfait. "Meilleur effort apres N iterations" est toujours preferable a "boucle infinie en cherchant la perfection".

## 7. Anti-patterns a eviter

1. **Recursion sans max_depth.** `self.run(task)` sans compteur est une bombe a retardement. Toujours passer `_depth + 1` et checker au debut.

2. **Seuil de qualite inatteignable.** Si le score moyen est 70% et le seuil est 95%, l'agent bouclera presque toujours. Le seuil doit etre calibre sur les scores reels du systeme.

3. **Agent qui catch ses propres erreurs et retry.** `except Exception: return self.run(task)` — chaque erreur declenche un retry, qui peut generer une nouvelle erreur, qui retry...

4. **Pas de monitoring des invocations.** Si personne ne compte combien de fois un agent s'execute par minute, les boucles passent inapercues jusqu'a ce que la facture arrive.

5. **Confiance dans le `sys.setrecursionlimit()` de Python.** Le default (1000) est eleve pour du code recursif. Un agent LLM qui boucle 1000 fois avant le `RecursionError` aura deja fait 1000 appels API.

## 8. Cas limites et variantes

**Variante 1 : Boucle multi-agents.** Trois agents forment un cycle : A → B → C → A. Chacun a un max_depth individuel mais le cycle complet n'a pas de limite. Necessite un **global depth** qui traverse les delegations.

**Variante 2 : Boucle asynchrone.** L'agent poste un message dans une queue, un worker le traite et poste le resultat dans une autre queue que l'agent ecoute. La boucle passe par une infrastructure externe (queue) et n'est pas visible comme recursion dans le code.

**Variante 3 : Boucle par fichier.** L'agent ecrit un fichier, un file watcher detecte le changement et relance l'agent. La boucle passe par le filesystem et n'est pas detectable par analyse statique du code.

**Variante 4 : Boucle lente.** L'agent ne boucle pas en millisecondes mais une fois par minute. En 24 heures, il a fait 1440 appels LLM. Le monitoring par fenetre de 5 minutes ne detecte rien (1 appel/minute = sous le seuil).

## 9. Checklist d'audit

- [ ] Chaque agent recursif a un parametre max_depth (defaut: 3-5)
- [ ] Chaque agent a un timeout global (defaut: 30-60 secondes)
- [ ] Un budget d'appels LLM par agent est defini et monitore
- [ ] Un circuit breaker coupe les agents qui depassent N invocations en T secondes
- [ ] Les cycles de delegation entre agents sont identifies et bornes

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 09 — Agent Infinite Loop](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-09)
- Patterns connexes : #02 (Rapid-Fire Loop — boucle declenchee par le refresh externe, pas par l'agent), #10 (Survival Mode Deadlock — un seuil inatteignable qui cause la boucle)
- Lectures recommandees :
  - Documentation LangGraph sur les "recursion limits" — le framework a un parametre `recursion_limit` exactement pour ce pattern
  - "Building LLM Powered Applications" (Valentino Gagliardi, 2024) — chapitre sur les guardrails d'agents recursifs
