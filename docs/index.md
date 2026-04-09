# Introduction — Pourquoi ce playbook existe

## Le probleme

Les systemes multi-agents IA sont en train de devenir l'architecture dominante pour les applications LLM complexes. LangChain, CrewAI, AutoGen, LangGraph — chaque mois apporte un nouveau framework, de nouveaux patterns d'orchestration, de nouvelles facons de faire collaborer des agents autonomes.

Et chaque mois, les memes bugs apparaissent.

Pas des bugs de logique ou d'algorithmique. Des bugs **d'architecture distribuee** : des agents qui se marchent dessus sur un fichier partage, des timezones qui derivent silencieusement, des mecanismes de retry qui boucle indefiniment, des seuils de confiance qui bloquent le systeme entier. Des bugs qui ne crashent pas — ils degradent. Ils rendent un systeme fonctionnel en apparence mais defaillant en realite.

Ces bugs sont particulierement pernicieux parce qu'ils n'apparaissent pas dans les tests unitaires. Ils emergent de l'**interaction entre composants** : l'agent A ecrit un fichier, l'agent B le lit avec un format different, le superviseur ne detecte rien, et les metriques derivees sont fausses depuis 11 jours sans que personne ne le remarque.

## L'origine de ce playbook

Je m'appelle Samuel. Je construis et maintiens un systeme multi-agents en production depuis plus de 18 mois. Ce systeme fait tourner 4+ agents autonomes (superviseur, analyseur, urgence, strategie) qui communiquent par fichiers JSON, SQLite et messages inter-agents, avec une boucle de decision toutes les 10 secondes.

En avril 2026, j'ai realise un audit complet ligne par ligne de ce systeme. Le resultat : **66 bugs identifies**, dont 8 critiques et 16 a impact eleve. 18 d'entre eux ont ete corriges en urgence en une semaine.

Ce qui m'a frappe, c'est que la plupart de ces bugs n'etaient pas specifiques a mon systeme. Ce sont des **patterns recurrents** que tout developpeur de systeme multi-agents rencontre ou rencontrera. Un timezone mismatch dans mon superviseur est le meme timezone mismatch qu'un dev LangChain va avoir entre son orchestrateur et son executeur. Une race condition sur un fichier de config partage dans mon systeme est la meme race condition qu'un dev CrewAI va subir quand deux agents ecrivent dans le meme state store.

J'ai decide de documenter ces patterns. Pas comme une liste de bugs, mais comme un **catalogue de patterns de defaillance** — avec pour chacun : le symptome observable, la cause racine technique, le code de detection, la correction, et surtout la prevention architecturale qui empeche le bug de jamais apparaitre.

## A qui s'adresse ce playbook

Ce playbook est ecrit pour les developpeurs qui construisent des systemes multi-agents en Python. Le niveau technique assume une maitrise de Python, une comprehension basique des systemes distribues, et une experience (meme debutante) avec au moins un framework multi-agents (LangChain, CrewAI, AutoGen, ou du code custom).

Les exemples utilisent un domaine specifique (trading automatise) pour illustrer des concepts generiques. Si tu construis un chatbot multi-agents, un pipeline RAG distribue, un systeme d'automatisation avec des agents LLM, ou tout autre systeme ou des composants autonomes interagissent — ces patterns s'appliquent a toi.

## Structure du playbook

Chaque pattern suit la meme structure en 10 sections :

1. **Symptome observable** — ce que tu vois quand le bug est present
2. **Histoire vecue** — un cas reel anonymise pour creer le contexte
3. **Cause racine technique** — le mecanisme precis du bug
4. **Detection** — manuelle (audit), automatisee (CI/CD), runtime (production)
5. **Correction** — fix immediat + fix robuste
6. **Prevention architecturale** — comment empecher le bug a la racine
7. **Anti-patterns a eviter** — les mauvaises habitudes qui menent au bug
8. **Cas limites et variantes** — le meme bug sous d'autres formes
9. **Checklist d'audit** — 5 items verifiables en 2 minutes
10. **Pour aller plus loin** — patterns connexes et ressources

Les patterns sont organises en 9 categories, de "Temps & Synchronisation" a "Gouvernance Multi-Agents". Chaque categorie a une introduction qui explique pourquoi cette classe de bugs est frequente et comment elle se manifeste dans les differents frameworks.

## Cinq enseignements transverses

Apres avoir catalogue 66 bugs et corrige les 18 plus critiques en urgence, voici les 5 lecons que j'aurais voulu connaitre 18 mois plus tot :

1. **Les bugs les plus couteux ne crashent pas.** Ils degradent silencieusement. Un filtre horaire decale de 3 heures, un compteur qui ne s'incremente plus, un module de risk management qui n'est jamais appele — le systeme tourne, les logs semblent normaux, mais les resultats sont faux. Le monitoring de **fraicheur** (les donnees sont-elles recentes ?) est plus important que le monitoring d'erreur.

2. **Chaque point d'ecriture partagee est un bug en puissance.** Des que deux agents ecrivent dans le meme fichier, la meme DB, ou le meme objet en memoire — sans lock, sans schema versionne, sans source unique de verite — c'est une question de temps avant qu'une race condition ou un desync apparaisse. La regle : **un seul writer par ressource**.

3. **Les cascades sont invisibles.** Quand 5 agents ajustent le meme parametre independamment, chaque ajustement est raisonnable. Le produit de 5 reductions raisonnables est une valeur absurde. La regle : **pipeline accumulatif avec floor**, pas de read-modify-write independants.

4. **Le try/except Exception: pass est un silenceur de bugs.** Il ne gere pas les erreurs, il les cache. Chaque bug silencieux que j'ai trouve etait protege par un except trop large. La regle : **logger dans chaque except**, ne jamais catch Exception sans re-raise ou log.

5. **Les tests unitaires ne detectent pas les bugs d'integration.** Les 66 bugs de mon audit passaient tous les tests unitaires. Ils n'apparaissaient que dans l'interaction entre composants en production. La regle : **tester le flux complet** (producteur → consommateur → decision → action), pas juste les fonctions isolees.

---

*Ce playbook est un document vivant. Les nouvelles fiches sont ajoutees chaque semaine. Si tu trouves un pattern qui manque, tu peux le signaler sur le repo public associe.*
