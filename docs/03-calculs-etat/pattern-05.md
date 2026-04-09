# Pattern N°05 — Unit Mismatch 100x

**Categorie :** Calculs & Etat
**Severite :** Critical
**Frameworks impactes :** LangChain / CrewAI / AutoGen / LangGraph / Custom
**Temps moyen de debogage si non detecte :** 1 a 5 jours (les consequences sont souvent visibles immediatement mais la cause racine est cherchee au mauvais endroit)

---

## 1. Symptome observable

Une action est executee avec une magnitude **10x a 100x superieure** (ou inferieure) a ce qui etait prevu. Un appel API qui devait consommer 40 tokens en consomme 4000. Un budget de $10 est depense en $1000. Un timeout de 30 secondes est interprete comme 30 millisecondes. La formule est correcte, les pourcentages sont corrects, mais la **valeur unitaire** est fausse.

Le symptome le plus piege : le systeme ne leve aucune erreur. Le calcul est mathematiquement correct — c'est un input qui est faux. Si `unit_cost = 0.01` alors que ca devrait etre `1.0`, le resultat sera 100x trop petit, mais Python ne sait pas que `0.01` est faux.

Les consequences varient selon la direction de l'erreur. Si la valeur est trop grande : overconsommation de ressources, budget explose, rate limits atteints. Si la valeur est trop petite : actions negligeables, resultats inutilisables, le systeme semble "ne rien faire".

## 2. Histoire vecue (anonymisee)

Un systeme multi-agents gerait des budgets de tokens LLM pour plusieurs modeles. Le module de calcul principal utilisait `cost_per_token = 0.001` (correct pour le modele A). Le module de monitoring, developpe separement, utilisait `cost_per_token = 0.00001` (copie-colle depuis la documentation d'un modele beaucoup moins cher).

Le resultat : le monitoring affichait des couts 100x inferieurs a la realite. L'equipe pensait depenser $50/jour alors que la facture reelle etait de $5000/jour. Le bug a ete decouvert a la reception de la premiere facture mensuelle — un choc de $150,000 au lieu des $1,500 estimes.

La cause : un copier-coller d'une valeur unitaire d'un modele a un autre, sans verification. Le code etait identique entre les deux modules, seule la constante differait.

## 3. Cause racine technique

Le bug se produit quand **deux modules du meme systeme utilisent des valeurs unitaires differentes** pour la meme entite. Chaque module hardcode sa propre constante, copiee depuis une documentation ou un autre module, sans source centralisee :

```python
# Module A — calcul de budget (correct)
COST_PER_TOKEN = 0.001  # $0.001 per token for Model X

# Module B — monitoring (BUG — copie depuis Model Y, 100x moins cher)
COST_PER_TOKEN = 0.00001  # Wrong! Copied from cheaper model docs

# La formule est identique, seule la constante differe :
total_cost = tokens_used * COST_PER_TOKEN
# Module A: 4000 * 0.001 = $4.00 (correct)
# Module B: 4000 * 0.00001 = $0.04 (100x trop bas)
```

Le probleme fondamental est l'**absence de source unique** pour les valeurs unitaires. Chaque module a sa propre copie de la constante, et rien ne garantit qu'elles sont identiques. Le copier-coller est le vecteur d'infection : la constante est correcte dans le module original, incorrecte dans la copie.

Les variantes incluent :
- Melanger dollars et centimes (`amount = 500` : est-ce 500$ ou 500 cents ?)
- Melanger secondes et millisecondes (`timeout = 30` : 30s ou 30ms ?)
- Melanger tokens et kiloTokens (`budget = 4` : 4 tokens ou 4000 ?)
- Utiliser des valeurs de test en production (`unit_cost = 0.0` ou `unit_cost = 1.0` au lieu de la vraie valeur)

## 4. Detection

### 4.1 Detection manuelle (audit code)

Lister toutes les constantes unitaires et les comparer entre modules :

```bash
# Chercher les definitions de constantes unitaires
grep -rn "COST_PER\|PRICE_PER\|UNIT_\|_PER_TOKEN\|_PER_UNIT" --include="*.py"

# Chercher les valeurs hardcodees dans les calculs
grep -rn "\* 0\.0\|\* 1\.0\|/ 0\.0\|/ 1\.0" --include="*.py"

# Chercher les copier-colles suspects (meme variable dans plusieurs fichiers)
for var in COST_PER_TOKEN UNIT_PRICE POINT_VALUE; do
    echo "=== $var ===" && grep -rn "$var" --include="*.py"
done
```

### 4.2 Detection automatisee (CI/CD)

Centraliser les valeurs unitaires et tester au demarrage que tous les modules utilisent la meme source :

```python
# test_unit_consistency.py
from config import UNIT_REGISTRY  # Single source of truth

def test_no_hardcoded_units():
    """Ensure no module hardcodes unit values that should come from the registry."""
    import pathlib, re

    # Known unit constants that must come from UNIT_REGISTRY
    forbidden_patterns = [
        r'COST_PER_TOKEN\s*=\s*[\d.]',
        r'PRICE_PER_UNIT\s*=\s*[\d.]',
        r'POINT_VALUE\s*=\s*[\d.]',
    ]

    violations = []
    for py_file in pathlib.Path("src").rglob("*.py"):
        if py_file.name == "config.py":
            continue  # Skip the registry itself
        content = py_file.read_text()
        for pattern in forbidden_patterns:
            for match in re.finditer(pattern, content):
                violations.append(f"{py_file}:{content[:match.start()].count(chr(10))+1}")

    assert not violations, f"Hardcoded unit values found:\n" + "\n".join(violations)
```

### 4.3 Detection runtime (production)

Garde-fou sur chaque action : verifier que la magnitude est dans une plage attendue avant d'executer :

```python
class MagnitudeGuard:
    """Blocks actions whose magnitude exceeds expected bounds."""

    def __init__(self, bounds: dict[str, tuple[float, float]]):
        self.bounds = bounds  # {"action_name": (min, max)}

    def check(self, action: str, value: float) -> bool:
        if action not in self.bounds:
            return True
        low, high = self.bounds[action]
        if value < low or value > high:
            raise ValueError(
                f"MAGNITUDE GUARD: {action}={value} outside bounds [{low}, {high}]. "
                f"Probable unit mismatch."
            )
        return True

# Usage:
guard = MagnitudeGuard({
    "token_budget": (100, 100_000),
    "api_cost_usd": (0.001, 100.0),
    "timeout_seconds": (1, 300),
})
guard.check("api_cost_usd", 5000.0)  # Raises: probable unit mismatch
```

## 5. Correction

### 5.1 Fix immediat

Identifier la bonne valeur unitaire et la corriger dans le module fautif :

```python
# AVANT (bug: copie depuis un autre modele)
COST_PER_TOKEN = 0.00001

# APRES (correct: valeur du bon modele)
COST_PER_TOKEN = 0.001
```

Puis ajouter un garde-fou qui bloque les valeurs aberrantes :

```python
def compute_cost(tokens: int, cost_per_token: float) -> float:
    cost = tokens * cost_per_token
    if cost > 1000:  # Hard cap: aucune action ne devrait couter > $1000
        raise ValueError(f"Cost too high: ${cost:.2f} for {tokens} tokens. Check unit value.")
    return cost
```

### 5.2 Fix robuste

Centraliser toutes les valeurs unitaires dans un registre unique :

```python
"""unit_registry.py — Single source of truth for all unit values."""

UNITS = {
    "gpt-4": {"cost_per_token": 0.00003, "max_tokens": 128000},
    "gpt-4o": {"cost_per_token": 0.0000025, "max_tokens": 128000},
    "claude-opus": {"cost_per_token": 0.000015, "max_tokens": 200000},
    "llama-70b": {"cost_per_token": 0.0, "max_tokens": 8192},  # Free tier
}

def get_unit(model: str, unit: str) -> float:
    """The ONLY way to get a unit value. Raises if model/unit unknown."""
    if model not in UNITS:
        raise ValueError(f"Unknown model: {model}. Add it to unit_registry.py")
    if unit not in UNITS[model]:
        raise ValueError(f"Unknown unit '{unit}' for model {model}")
    return UNITS[model][unit]

# Usage across all modules:
cost = tokens * get_unit("gpt-4", "cost_per_token")
```

## 6. Prevention architecturale

La prevention repose sur deux principes : **centralisation** et **garde-fous**.

**Centralisation** : toutes les valeurs unitaires vivent dans un seul fichier/module (`unit_registry.py` ou `config.yaml`). Aucun autre module n'a le droit de definir ses propres constantes unitaires. Un test CI verifie qu'aucun module ne hardcode une valeur qui devrait venir du registre.

**Garde-fous** : avant chaque action dont la magnitude depend d'une valeur unitaire, un `MagnitudeGuard` verifie que le resultat est dans une plage attendue. Si un calcul produit un budget de tokens de 4 millions ou un cout de $50,000, c'est presque certainement un bug unitaire — le guard le bloque et alerte.

En complement, les valeurs unitaires du registre doivent etre validees au demarrage du systeme par un health check qui compare avec une source externe (API du provider, documentation officielle).

## 7. Anti-patterns a eviter

1. **Copier-coller les valeurs unitaires entre modules.** C'est le vecteur d'infection principal. Toujours importer depuis le registre.

2. **Pas de guard sur les valeurs calculees.** Un `total = budget / unit_value` sans verification de borne est une bombe a retardement. Si `unit_value` est 100x trop petit, `total` est 100x trop grand.

3. **Melanger les unites dans le meme pipeline.** Un module qui passe un montant en centimes a un module qui attend des dollars cree un mismatch silencieux.

4. **Utiliser des valeurs de test en production.** `unit_cost = 0.0` pour "gratuit en dev" qui reste en prod = actions sans cout apparent = aucune alerte sur la surconsommation.

5. **Pas d'audit croise au demarrage.** Le systeme devrait verifier au boot que tous les modules ont les memes valeurs unitaires pour les memes entites.

## 8. Cas limites et variantes

**Variante 1 : Changement de pricing.** Le provider LLM change ses prix. Le registre est mis a jour mais un module secondaire a sa propre copie hardcodee qui reste a l'ancien prix. Le monitoring affiche des couts incorrects pendant des semaines.

**Variante 2 : Unites differentes par region.** Le meme service coute $0.01/appel en US et EUR0.01/appel en EU. Si le systeme ne convertit pas les devises, les couts sont compares sans conversion — 1 USD != 1 EUR.

**Variante 3 : Precision flottante.** `0.1 + 0.2 = 0.30000000000000004` en Python. Sur des millions d'operations, la derive de precision peut accumuler des erreurs significatives. Utiliser `decimal.Decimal` pour les calculs financiers.

**Variante 4 : Valeurs unitaires nulles.** `unit_cost = 0.0` (free tier) dans un calcul `budget / unit_cost` → `ZeroDivisionError`. Le guard doit aussi verifier les valeurs nulles.

## 9. Checklist d'audit

- [ ] Toutes les valeurs unitaires sont centralisees dans un registre unique
- [ ] Aucun module ne hardcode de valeur unitaire (verifie par test CI)
- [ ] Un garde-fou verifie la magnitude avant chaque action critique
- [ ] Le registre est valide au demarrage contre une source de reference
- [ ] Les unites sont documentees explicitement (dollars vs centimes, secondes vs millisecondes)

## 10. Pour aller plus loin

- Pattern court correspondant : [Pattern 05 — Unit Mismatch 100x](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns/tree/main/pattern-05)
- Patterns connexes : #03 (Cascade de Penalites — un mismatch unitaire peut amplifier une cascade), #04 (Multi-File State Desync — la valeur unitaire peut etre stockee a plusieurs endroits avec des valeurs differentes)
- Lectures recommandees :
  - "Mars Climate Orbiter" (NASA, 1999) — l'exemple historique le plus celebre de mismatch unitaire (imperial vs metrique), ayant cause la perte d'un satellite de $125 millions
  - Documentation OpenAI/Anthropic sur le pricing par token — les unites changent entre modeles et entre input/output
