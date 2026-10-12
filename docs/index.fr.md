[English](index.md) | Français

# Documentation de Synaptane

Synaptane est un second cerveau d'équipe stocké dans Git : des espaces de pages Markdown et
de fichiers (PDF, documents Office, images…) adossés à des dépôts Git, utilisés depuis votre
assistant IA via MCP, et rangés chaque nuit par un agent IA. Commencez par les
[Concepts](concepts.fr.md) pour le vocabulaire et la vue d'ensemble.

## Utilisateurs

- [Brancher son assistant IA (MCP)](guide/mcp.fr.md) : Claude, ChatGPT, Claude Code ; les
  outils, les prompts et l'inbox.
- [Lire, écrire et chercher dans le wiki](guide/wiki.fr.md) : lecture, édition, vérification
  des pages, santé du wiki, recherche.

## Administration

- [Installer et exploiter Synaptane](admin/installation.fr.md) : Docker Compose ou depuis
  les sources, mises à jour, référence de la configuration.
- [Dépôts Git](admin/git.fr.md) : relier un espace à son dépôt, synchronisation, conflits.
- [IA et rangement](admin/ai.fr.md) : fournisseurs et abonnements, rangement nocturne,
  vocation et consignes, routines, coût et budget, recherche par le sens.
- [Connexion SSO](admin/sso.fr.md) : GitLab, GitHub, Google, OIDC.
- [Webhooks](admin/webhooks.fr.md) : événements sortants, format, livraison.
- [Sauvegarde et restauration](admin/backup.fr.md) : sauvegarde locale, copies hors serveur
  chiffrées, exercices de restauration.
- [Scripts du serveur](../deploy/host/README.fr.md) : mise à jour, sauvegarde nocturne et
  nginx pour un serveur Compose.

## Référence

- [Concepts](concepts.fr.md) : vision, vocabulaire, rôles, inbox, rangement, vocation,
  routines, choix de conception.
- [Format du dépôt](reference/repository-format.fr.md) : `content/`, `.synaptane/`, front
  matter, notes de l'inbox.
- [API REST](reference/api.fr.md) : toutes les routes de `/api/v1`.
- [Ligne de commande](reference/cli.fr.md) : la commande `synaptane`.

## Développement

- [Guide de développement](dev/development.fr.md) : installation, lancer l'application en
  local, tests, organisation du code.
- [Banc d'évaluation](dev/eval.fr.md) : mesurer le rangement et la recherche avant de changer
  une consigne, un modèle ou le classement.
- [Contribuer](../CONTRIBUTING.fr.md), [journal des modifications](../CHANGELOG.md) (en
  anglais), [politique de sécurité](../SECURITY.fr.md).
