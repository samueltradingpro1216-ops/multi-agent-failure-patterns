# Pattern 10 — Survival Mode Deadlock

> Applicable a : tout systeme multi-agents avec un mecanisme de confiance/scoring qui gate les actions. Frequent dans les pipelines LangChain avec des "confidence gates", les agents CrewAI avec validation croisee, et les workflows AutoGen ou un agent evaluateur doit approuver les actions des autres. Le pattern apparait des qu'un seuil de qualite est plus eleve que ce que le systeme peut produire.

## Symptome

- Le systeme est **vivant mais ne fait rien** : aucune action executee depuis des heures/jours
- Les logs montrent des signaux generes, evalues, puis systematiquement rejetes
- Le score moyen de confiance est ~42, le seuil d'acceptation est 50 : 100% des actions sont vetoes
- Le systeme est entre dans un "mode survie" dont il ne peut pas sortir tout seul

## Cause racine

Un mecanisme de scoring (confiance, qualite, risque) a un **seuil statique** qui est plus eleve que le score moyen produit par le systeme dans les conditions actuelles. 

```
Score moyen produit:   42/100
Seuil d'acceptation:   50/100
-> 100% des actions rejetees
-> Le systeme ne fait rien
-> Les metriques ne s'ameliorent pas (pas de donnees recentes)
-> Le score reste bas
-> Deadlock permanent
```

Le cercle vicieux : sans executer d'actions, le systeme ne peut pas generer de nouvelles donnees pour ameliorer son score. C'est un **deadlock auto-renforce**.

## Detection

Comparer le score moyen sur les N derniers cycles avec le seuil d'acceptation. Si le score moyen < seuil pendant plus de M cycles, c'est un deadlock. Voir `example.py`.

## Correction

1. **Seuil adaptatif** : le seuil descend progressivement si le taux d'acceptation est 0%
2. **Fenetre d'exploration** : accepter 1 action sur N meme si le score est bas
3. **Fallback au seuil minimum** : apres T heures sans action, baisser le seuil a un minimum garanti

Voir `example.py` pour les 3 mecanismes.

## Prevention

- **Jamais de seuil statique** sans mecanisme de fallback
- **Monitoring du taux d'acceptation** : alerter si 0% sur > 1h
- **Exploration obligatoire** : meme en mode conservateur, executer au minimum 1 action par periode
- **Seuil base sur un percentile** : accepter le top 10% des scores au lieu d'un seuil absolu
- **Circuit breaker inverse** : si rien n'est accepte pendant N heures, baisser automatiquement le seuil

## Playbook complet

Fiche detaillee avec seuil adaptatif complet, fenetre d'exploration, et cas reel ou un systeme n'a execute aucune action pendant 4 jours a cause d'un seuil trop eleve : [lien a venir]
