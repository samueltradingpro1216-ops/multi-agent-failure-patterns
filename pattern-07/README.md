# Pattern 07 — Hardcoded Secret in Source

> Applicable a : tout systeme multi-agents qui communique avec des services externes (LLM APIs, bases de donnees, messagerie). Extremement frequent dans les prototypes LangChain qui hardcodent OPENAI_API_KEY, les agents CrewAI avec des tokens de notification, et les pipelines AutoGen qui stockent des credentials de connexion directement dans le code.

## Symptome

- Un `git log` ou `git show` revele des tokens, cles API ou mots de passe en clair dans le code source
- Un scan de securite (truffleHog, gitleaks, GitHub secret scanning) remonte des alertes
- Un bot malveillant utilise votre cle API quelques minutes apres un push public
- Facture LLM anormalement elevee : quelqu'un exploite votre cle exposee

## Cause racine

Pendant le developpement, le dev hardcode une cle API "temporairement" pour tester. La cle reste dans le code, est committee, et eventuellement pushee sur un repo (meme prive — les collaborateurs y ont acces).

```python
# "temporaire" depuis 6 mois
BOT_TOKEN = "8679765924:AAF3d5__KxB2nM..."
API_KEY = "sk-proj-abc123def456..."
```

Le probleme s'aggrave dans les systemes multi-agents car il y a **plus de services** connectes (LLM provider, notification, DB, monitoring) et donc plus de credentials a gerer.

## Detection

Scanner le code source et l'historique git pour les patterns de secrets. Voir `example.py` pour un scanner simplifie. En production, utiliser truffleHog, gitleaks, ou GitHub Advanced Security.

## Correction

1. Deplacer tous les secrets dans un fichier `.env` (jamais committe)
2. Lire via `os.environ` ou `python-dotenv`
3. Ajouter `.env` au `.gitignore`
4. Si le secret a ete committe : **le revoquer immediatement** (le supprimer du code ne suffit pas, il reste dans l'historique git)

Voir `example.py` pour le pattern complet.

## Prevention

- **Pre-commit hook** : installer `detect-secrets` ou `gitleaks` comme hook pre-commit
- **`.env.example`** : fournir un template avec des placeholders (`API_KEY=your_key_here`)
- **CI/CD scan** : scanner chaque PR avec truffleHog/gitleaks avant merge
- **Rotation reguliere** : changer les cles tous les 90 jours minimum
- **Principe du moindre privilege** : chaque agent a sa propre cle avec permissions limitees

## Playbook complet

Fiche detaillee avec scanner d'historique git, workflow de rotation de cles, et cas reel ou un token de notification expose a permis l'envoi de messages non autorises : [lien a venir]
