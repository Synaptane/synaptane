[English](SECURITY.md) | Français

# Politique de sécurité

## Signaler une faille

**N'ouvrez jamais d'issue, de discussion ni de pull request publique pour un problème de
sécurité.**

Signalez-le en privé via le formulaire GitHub de signalement privé de vulnérabilité :

**<https://github.com/synaptane/synaptane/security/advisories/new>**

C'est le seul canal. Le signalement n'est visible que de vous et des mainteneurs ; la
discussion, le correctif et l'avis de sécurité s'y préparent. Vous pouvez écrire en anglais
ou en français.

Les signalements au titre du [code de conduite](CODE_OF_CONDUCT.md) passent par le même
formulaire privé : choisissez « Report a vulnerability » et décrivez le problème de conduite.
En repli, utilisez le signalement d'abus de GitHub : <https://github.com/contact/report-abuse>.

## Ce qu'il faut indiquer

- La version : la sortie de `synaptane --version` (elle comprend le commit).
- Le mode d'installation de Synaptane : Docker Compose derrière le proxy inverse du serveur,
  Docker Compose avec le profil `proxy` fourni, depuis les sources, ou autre.
- Les étapes pour reproduire, idéalement depuis une installation neuve.
- L'impact : ce qu'un attaquant peut lire, modifier ou exécuter, et avec quel niveau d'accès
  (anonyme, membre, administrateur d'espace ou d'organisation, administrateur de l'instance,
  client MCP, agent IA).
- Toute preuve de concept, journal ou capture d'écran. Retirez-en les secrets et les données
  personnelles réelles.

## Versions prises en charge

| Version | Prise en charge |
|---|---|
| Dernière version 1.x | Oui |
| Branche `main` | Oui |
| Toute version plus ancienne | Non |

Les correctifs sortent dans une nouvelle version corrective. Si vous le pouvez, mettez à jour
avant de signaler.

## Et ensuite

Synaptane est maintenu au mieux par une seule personne. Ces délais sont des objectifs, pas des
garanties :

- **Accusé de réception** sous 7 jours.
- **Première évaluation** (confirmée ou non, gravité) sous 14 jours.
- **Correctif** sous 90 jours après le signalement, plus tôt pour les problèmes graves.
- **Publication** par un avis de sécurité GitHub (GitHub Security Advisory), avec un
  identifiant CVE quand le problème le justifie, une fois une version corrigée disponible. Le
  calendrier se décide avec vous.
- **Crédit** : si vous le souhaitez, vous êtes cité dans l'avis, sous le nom de votre choix.

Merci de nous laisser un délai raisonnable pour corriger avant toute divulgation publique.

## Périmètre

Dans le périmètre :

- L'application Synaptane de ce dépôt : interface web, API REST, serveur MCP, tâches de fond,
  outil en ligne de commande, rangement par l'IA.
- L'image Docker construite depuis ce dépôt (`ghcr.io/synaptane/synaptane`) et les fichiers
  Compose fournis ici.
- L'**injection de prompt** qui amène les propres outils de Synaptane à une action que
  l'utilisateur ou l'administrateur n'a pas voulue : par exemple, un contenu dans une page ou
  dans l'inbox qui fait lire, écrire, déplacer ou supprimer à l'agent IA des données hors de
  ce qu'il devrait, ou qui fait fuiter des données vers un autre espace ou une autre
  organisation.

Hors périmètre :

- Les instances tierces mal configurées (par exemple exposées sans HTTPS, avec des mots de
  passe par défaut, ou avec des droits accordés trop largement par leurs administrateurs).
- Les failles des dépendances en amont (paquets Python, PostgreSQL, Git, fournisseurs d'IA,
  image de base) : signalez-les à leurs auteurs. Si Synaptane utilise une dépendance de façon
  dangereuse, c'est dans le périmètre.
- Un modèle d'IA qui donne une réponse médiocre ou fausse, sans impact sur la sécurité.
- Un déni de service qui exige d'envoyer un très grand volume de trafic.
