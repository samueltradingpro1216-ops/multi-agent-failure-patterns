# Pattern 09 — Agent Infinite Loop

> Applicable a : tout systeme ou un agent peut se re-invoquer (chaines LangChain recursives, agents CrewAI qui delegent a eux-memes, workflows AutoGen avec boucles de reflexion non bornees). Le pattern apparait des qu'un agent a la capacite de declencher sa propre re-execution.

## Symptome

- Un agent consomme 100% du CPU ou explose le quota d'appels API en quelques minutes
- Les logs montrent le meme message repete des centaines de fois
- La facture LLM explose : l'agent s'appelle lui-meme en boucle
- Le systeme entier ralentit car un agent monopolise les ressources partagees

## Cause racine

L'agent a un mecanisme de **retry ou de re-delegation** qui peut se declencher indefiniment. Exemples courants :

- Un agent "reflexion" qui re-evalue sa propre reponse et decide de recommencer, sans limite
- Un orchestrateur qui re-dispatche une tache echouee au meme agent, qui echoue a nouveau
- Un cron job qui declenche un agent, qui declenche le meme cron job via un side-effect

L'absence de **compteur de profondeur**, de **timeout global**, ou de **guard anti-boucle** permet a la recursion de continuer indefiniment.

## Detection

Compter les invocations d'un meme agent dans une fenetre de temps. Si un agent s'execute > N fois en < T secondes, c'est une boucle. Voir `example.py`.

## Correction

1. **Compteur de profondeur** : chaque invocation incremente un compteur, stop a max_depth
2. **Timeout global** : l'agent ne peut pas tourner plus de N secondes au total
3. **Deduplication par hash** : si l'input est identique a la derniere execution, stop

Voir `example.py` pour les 3 mecanismes.

## Prevention

- **Max depth obligatoire** dans tout agent recursif (defaut: 3-5)
- **Circuit breaker** : apres N echecs consecutifs, l'agent se desactive pour une periode
- **Budget d'execution** : chaque agent a un budget max de tokens/appels/secondes par cycle
- **Monitoring en temps reel** : alerter si un agent depasse 10 invocations par minute
- **Separation des concerns** : un agent ne devrait jamais pouvoir se re-declencher directement

## Playbook complet

Fiche detaillee avec circuit breaker complet, budget manager, et cas reel ou un agent orchestrateur a consomme 2000 appels LLM en 15 minutes : [lien a venir]
