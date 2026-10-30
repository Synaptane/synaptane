[English](README.md) | Français

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo.svg" alt="Synaptane" width="360">
  </picture>
</p>

# Synaptane

**Synaptane est un second cerveau d'équipe auto-hébergé, stocké dans Git.** Le Markdown
d'abord, mais tous vos fichiers : les pages côtoient PDF (scans compris), documents Office,
images et pages HTML, et tout est cherchable. Vos assistants IA y cherchent et déposent ce
qu'ils apprennent dans une inbox, via MCP. Chaque nuit, un agent IA range ces notes dans le
wiki et commite le résultat.

[![Licence : Apache-2.0](https://img.shields.io/github/license/synaptane/synaptane)](LICENSE)
[![CI](https://github.com/synaptane/synaptane/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/synaptane/synaptane/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)
[![Version](https://img.shields.io/github/v/release/synaptane/synaptane?sort=semver)](https://github.com/synaptane/synaptane/releases)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![Image : ghcr.io/synaptane/synaptane](https://img.shields.io/badge/image-ghcr.io%2Fsynaptane%2Fsynaptane-blue?logo=docker)](https://github.com/synaptane/synaptane/pkgs/container/synaptane)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshot-wiki-dark.png">
  <img src="docs/assets/screenshot-wiki-light.png" alt="Une page du wiki Synaptane : l'arborescence de l'espace, un tableau récapitulatif des sauvegardes, un badge de vérification, les pages qui y renvoient et les pages proches">
</picture>

## Pourquoi Synaptane ?

Votre équipe explique des choses à des assistants IA tous les jours, et ce savoir disparaît à
la fin de la conversation. Synaptane donne à vos assistants une **mémoire partagée et durable,
qui vous appartient** : des pages en Markdown, avec les documents qui vont avec, dans votre
propre dépôt Git. Les assistants ne se contentent pas de la lire, ils l'alimentent, et un agent
IA la tient en ordre chaque nuit.

- **Par rapport à Notion AI, Confluence ou Slite :** aucun enfermement. C'est auto-hébergé,
  cela marche avec le modèle de votre choix, et vos données sont un dépôt Git.
- **Par rapport à Outline, Wiki.js, BookStack ou Docmost :** c'est pensé pour les assistants
  IA, et le wiki est entretenu pour vous.
- **Par rapport à Obsidian, Logseq ou Basic Memory :** c'est multi-utilisateur et côté
  serveur : une seule mémoire pour les assistants de toute l'équipe.
- **Par rapport à Khoj ou AnythingLLM :** il ne s'agit pas seulement de répondre à partir de
  documents, mais d'entretenir une base de connaissances que des personnes lisent et modifient.

## Fonctionnalités

- **Git fait foi.** Chaque espace est un dépôt Git (GitHub, GitLab, Gitea ou tout autre
  hébergeur). Son dossier `content/` est votre wiki : des pages en Markdown et les fichiers
  qui vont avec, que vous pouvez lire, fouiller et cloner sans Synaptane. Chaque modification
  est un commit. La synchronisation marche dans les deux sens, avec résolution des conflits.
- **Le Markdown d'abord, mais tous vos fichiers.** Les pages sont en Markdown, avec liens,
  rétroliens et vérification. À côté, PDF (les scans sont lus par OCR), documents Word, Excel
  et PowerPoint (et OpenDocument), images, pages HTML, CSV, JSON ou code sont affichés,
  cherchables et lisibles par vos assistants. Voir
  [Vos fichiers, tous cherchables](#vos-fichiers-tous-cherchables).
- **Pensé pour les assistants IA (MCP).** Streamable HTTP, avec OAuth 2.1 (PKCE,
  enregistrement dynamique des clients) ou des jetons personnels. 20 outils et 3 prompts.
  Testé avec les connecteurs de Claude.ai, Claude Desktop, Claude Code et ChatGPT. Une
  connexion peut être limitée à la lecture seule ou à certains espaces.
- **Inbox et rangement nocturne.** Déposez des notes depuis votre assistant, l'interface web
  ou l'API REST. Un agent IA les range, met à jour les pages existantes, corrige les liens et
  commite. Chaque espace peut décrire sa *vocation* pour le guider. Garde-fous : 10
  suppressions au plus par passage, des plafonds de jetons, d'étapes et d'appels d'outils par
  passage, un budget mensuel facultatif, et l'annulation d'un passage en un clic.
- **Routines d'agent planifiées.** Par exemple un contrôle de cohérence chaque semaine ou un
  résumé le vendredi. La sortie va dans une page de rapport, l'inbox, un e-mail ou un webhook.
- **Une recherche qui trouve.** Recherche plein texte qui ignore les accents, avec une
  racinisation prudente (anglais et français), y compris dans le texte extrait des PDF,
  documents Office et images (OCR). Recherche hybride par le sens en option (pgvector, RRF).
  Pages proches pour chaque page.
- **Un savoir digne de confiance.** Propriétaire, date de vérification et périodicité de revue
  par page. Un onglet Santé du wiki (liens cassés, pages orphelines, pages anciennes). Les
  secrets probables sont refusés avant tout commit.
- **Le modèle de votre choix.** Anthropic, OpenAI, Mistral, Ollama ou toute API compatible
  OpenAI, avec une clé et un budget mensuel facultatif par organisation (20 € suggérés pour
  une clé facturée à l'usage). Un abonnement Claude ou ChatGPT
  peut aussi servir, par les CLI officiels (voir les [limites connues](#limites-connues)).
- **Fait pour les équipes.** Organisations, espaces, rôles (lecteur, contributeur, admin
  d'organisation, super-admin), invitations par e-mail, connexion SSO (OIDC, GitHub, GitLab,
  Google).
- **Des API ouvertes.** Une API REST complète et des webhooks signés par HMAC. Son schéma
  OpenAPI et sa documentation interactive sont désactivés par défaut (réglage `api_docs`).
- **Simple à exploiter.** Docker Compose, derrière votre propre proxy ou avec un nginx et
  Let's Encrypt fournis. `synaptane doctor` vérifie l'installation. Les sauvegardes sont
  vérifiées, et peuvent être chiffrées et copiées hors du serveur (age et rclone). Les admins
  reçoivent des alertes par e-mail.
- **Une interface sobre et bilingue.** Thèmes clair et sombre, anglais et français, recherche
  par Ctrl+K.

### Captures d'écran

Prises sur le jeu de démonstration (`synaptane demo`, en anglais) : Acme et ses membres sont fictifs.

<table>
  <tr>
    <td width="50%"><img src="docs/assets/screenshot-search.png" alt="La palette de recherche Ctrl+K"><br><sub><b>Recherche Ctrl+K</b>, dans tous vos espaces</sub></td>
    <td width="50%"><img src="docs/assets/screenshot-inbox.png" alt="L'inbox d'un espace"><br><sub><b>Inbox</b> : notes et modifications directes en attente de rangement</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/assets/screenshot-health.png" alt="L'onglet Santé du wiki"><br><sub><b>Santé du wiki</b> : liens cassés, pages à revoir</sub></td>
    <td width="50%"><img src="docs/assets/screenshot-history.png" alt="Le diff d'une modification de page"><br><sub><b>Historique</b> : chaque modification est un commit Git</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/assets/screenshot-assistants.png" alt="Brancher un assistant IA par MCP"><br><sub><b>Brancher un assistant IA</b> par MCP</sub></td>
    <td width="50%"><img src="docs/assets/screenshot-routines.png" alt="Les routines d'agent planifiées"><br><sub><b>Routines</b> : des agents planifiés, comme un contrôle de cohérence hebdomadaire</sub></td>
  </tr>
</table>

## Vos fichiers, tous cherchables

Le Markdown est le cœur du wiki, mais un espace accueille tout fichier : ajouté depuis
l'interface web (25 Mo par fichier), joint à une note déposée par un assistant, ou poussé avec
Git.

| Format | Dans le wiki | Recherche | Assistants IA (MCP) |
|---|---|---|---|
| Markdown (`.md`) | Page rendue : liens, rétroliens, sommaire, vérification, pages proches | Mots et sens | Lecture et écriture |
| PDF | Lecteur intégré | Couche texte, ou OCR (Tesseract) pour les pages scannées | Lecture du texte extrait |
| Word, PowerPoint, Excel (`.docx`, `.pptx`, `.xlsx`, et les anciens `.doc`, `.ppt`, `.xls`), OpenDocument (`.odt`, `.odp`, `.ods`) | Téléchargement, texte extrait affiché dessous | Texte extrait | Lecture du texte extrait |
| Images (PNG, JPEG, GIF, WebP, AVIF, BMP, SVG) | Affichées | Texte de l'image, par OCR (sauf SVG et AVIF) | Lecture du texte OCR |
| Pages HTML (`.html`, `.htm`) | Rendues dans un cadre isolé (sandbox), source à un clic | Mots | Lecture et écriture |
| Autres fichiers texte (CSV, JSON, YAML, code, journaux…) | Coloration syntaxique | Mots | Lecture et écriture |
| Tout le reste | Conservé, versionné, téléchargeable | Nom du fichier | Listé, sans texte |

« Sens » vaut pour tous les textes ci-dessus dans les espaces où la recherche par le sens est
activée. Le texte des anciens `.doc` et `.xls` est lu par catdoc (présent dans l'image Docker),
celui des anciens `.ppt` par Synaptane lui-même, avec des limites : détails dans
[Fichiers autres que Markdown](docs/guide/wiki.fr.md#fichiers-autres-que-markdown).

## État du projet

Synaptane est **stable** depuis la `v1.0.0`. Il est fourni tel quel et maintenu au mieux par
une seule personne.

Les versions suivent le [versionnage sémantique](https://semver.org/lang/fr/) : en 1.x, l'API
REST, les outils MCP et la configuration ne changent que de façon compatible. Chaque changement
est listé dans le [journal des modifications](CHANGELOG.md) (en anglais). Faites une sauvegarde
avant chaque mise à jour.

### Limites connues

- **Fonctionnalités récentes.** Les routines d'agent, les pages proches et la vérification
  des pages ont été ajoutées peu avant cette version. Attendez-vous à des aspérités.
- **Abonnements Claude et ChatGPT (Claude Code, Codex).** L'image publiée ne contient pas ces
  CLI : pour utiliser un abonnement, construisez l'image vous-même (voir
  [l'installation](docs/admin/installation.fr.md)). Le relevé du quota repose sur un événement
  non documenté de Claude Code et peut cesser de fonctionner après une mise à jour du CLI. Le
  CLI tourne sous le même compte système que le worker ; le faire tourner sous un compte à
  part est prévu.

  > [!WARNING]
  > **Vérifiez les conditions de votre fournisseur d'IA.** En octobre 2026, Anthropic
  > n'autorise l'usage d'un abonnement qu'avec le binaire officiel de Claude Code. Elle
  > interdit à une application tierce de manipuler ces identifiants, et les limites des
  > abonnements Pro et Max supposent un usage individuel ordinaire. Pour une organisation
  > partagée par plusieurs personnes, utilisez une clé d'API ou un plan Team ou Enterprise.
  > Détails : [IA et rangement](docs/admin/ai.fr.md).
- **Fonctions bêta d'Anthropic.** Avec les modèles Claude, Synaptane utilise une fonction bêta
  de l'API (repli côté serveur quand un filtre de sécurité refuse une requête). Une fonction
  bêta peut changer.
- **Coûts.** L'estimation automatique des coûts n'existe que pour les modèles Claude (et, pour
  la recherche sémantique, les modèles d'embeddings d'OpenAI, Gemini, Mistral et Voyage AI).
  Pour les autres fournisseurs, saisissez vous-même le tarif par million de jetons : sans
  tarif, aucun coût n'est calculé et aucun budget ne peut s'appliquer. La vectorisation des
  requêtes de recherche n'est pas comptée.
- **Pas encore là :** l'authentification à deux facteurs ; la restauration d'une version de
  page depuis l'interface (l'historique Git est là).
- **Supervision :** pas de point d'accès `/metrics`. `/healthz` et `/readyz` existent.
- **Plateformes :** Linux, ou WSL2 sous Windows. macOS n'est pas testé.
- **Langue :** l'interface web, les e-mails, les messages d'erreur de l'API REST et la
  documentation sont en anglais et en français. La ligne de commande, les journaux et les
  réponses aux assistants IA (MCP) sont en anglais.
- **La recherche approfondie de ChatGPT** avec le connecteur Synaptane n'a pas été testée.

## Prérequis

- Un **serveur Linux** (ou WSL2 pour essayer) avec **Docker Engine et le plugin Compose**.
- Un **nom de domaine servi en HTTPS**, pour les connecteurs de Claude.ai et ChatGPT, OAuth et
  le SSO. Inutile pour un essai en local.
- Un **dépôt Git** par espace, chez n'importe quel hébergeur, joignable en HTTPS ou en SSH.
- **Un fournisseur d'IA, facultatif.** Sans lui, le wiki, la recherche, le serveur MCP et
  l'inbox fonctionnent. Le rangement nocturne et les routines en ont besoin. La recherche par
  le sens demande un fournisseur d'embeddings (Ollama fonctionne en local).
- **Un serveur SMTP, facultatif**, pour les invitations par e-mail, le mot de passe oublié et
  les alertes.

## Démarrage rapide

Sur votre serveur, récupérez les fichiers de la version (fichier Compose, configuration nginx,
scripts), puis lancez l'image publiée :

```sh
git clone --branch v1.3.0 https://github.com/synaptane/synaptane.git ~/synaptane
cd ~/synaptane
cp .env.example .env && chmod 600 .env   # il contient le mot de passe de la base
openssl rand -hex 24                     # à copier dans POSTGRES_PASSWORD
nano .env                                # PUBLIC_URL, POSTGRES_PASSWORD, mode
docker compose pull
docker compose up -d
docker compose exec app synaptane admin create --username admin   # mot de passe : 12 caractères ou plus
docker compose exec app synaptane doctor
```

Git signale un « detached HEAD » après le clone d'une étiquette : c'est normal. `.env.example`
fixe déjà l'image sur la version clonée (`SYNAPTANE_VERSION`).

Choisissez ensuite, dans `.env`, qui sert le HTTPS :

- **Le nginx déjà installé sur le serveur** (par défaut) : Synaptane écoute sur
  `127.0.0.1:8000`. `deploy/host/install-nginx.sh` installe le site et son certificat
  Let's Encrypt.
- **Tout-en-un** (`COMPOSE_PROFILES=proxy`) : nginx et certbot tournent dans Docker, sur les
  ports 80 et 443.

Le [guide d'installation](docs/admin/installation.fr.md) détaille les deux modes, les mises à
jour, le retour arrière et la construction de l'image par vous-même.

## Essayer en local en 2 minutes

```sh
git clone --branch v1.3.0 https://github.com/synaptane/synaptane.git && cd synaptane
printf 'PUBLIC_URL=http://localhost:8000\nPOSTGRES_PASSWORD=%s\nSYNAPTANE_VERSION=1.3.0\n' \
  "$(openssl rand -hex 24)" > .env
chmod 600 .env                                                        # mot de passe de la base
echo 'COMPOSE_FILE=compose.yaml:examples/compose.demo.yaml' >> .env   # démonstration seulement
docker compose up -d
docker compose exec app synaptane demo
```

Git signale un « detached HEAD » après le clone d'une étiquette : c'est normal. Le port 8000
est déjà pris ? Ajoutez `APP_PORT=8001` dans `.env` et le même port dans `PUBLIC_URL`
(`http://localhost:8001`), puis dans les adresses ci-dessous.

`synaptane demo` charge le wiki d'une équipe informatique fictive, en anglais
([examples/acme-wiki](examples/acme-wiki/README.md)) : 50 pages, des pages vérifiées et une à
revoir, six notes d'inbox à ranger. La commande affiche l'adresse à ouvrir, le mot de passe du
compte `demo` (une seule fois) et comment brancher un assistant IA. Ouvrez
<http://localhost:8000/o/acme/it>, cherchez, parcourez l'inbox et l'onglet Santé. Le rangement
de l'inbox demande un fournisseur d'IA : voir [IA](docs/admin/ai.fr.md).

L'espace de démonstration utilise un dépôt Git local, que `examples/compose.demo.yaml`
autorise : gardez-le pour une instance jetable. Pour une vraie instance, sautez cette ligne et
créez le premier compte avec `docker compose exec app synaptane admin create --username admin`
([installation](docs/admin/installation.fr.md)).

Claude Code peut se brancher sur cette instance locale avec un jeton personnel (voir
ci-dessous). Claude.ai et ChatGPT demandent une adresse publique en HTTPS.

## Brancher votre assistant IA

Synaptane est un serveur MCP, à l'adresse `https://<votre-domaine>/mcp`. L'adresse exacte est
indiquée dans **Mon compte → Brancher un assistant IA (MCP)**.

- **Claude.ai, Claude Desktop :** Réglages → Connecteurs → Ajouter un connecteur
  personnalisé, collez l'adresse, puis connectez-vous et autorisez.
- **ChatGPT :** en mode développeur, créez un connecteur avec l'adresse et OAuth.
- **Claude Code et autres clients :** créez un jeton personnel, puis :

  ```sh
  claude mcp add --transport http synaptane https://brain.example.com/mcp \
    --header "Authorization: Bearer syn_…"
  ```

Guide complet, avec la liste des outils et des prompts :
[Brancher son assistant IA (MCP)](docs/guide/mcp.fr.md).

## Architecture

Une application FastAPI sert les trois interfaces : le MCP pour les assistants IA, l'interface
web et l'API REST. Un worker fait tourner le reste en arrière-plan : rangement nocturne,
routines, synchronisation Git, indexation et webhooks. Les deux partagent PostgreSQL (avec
pgvector) et les copies locales du dépôt Git de chaque espace. Le fournisseur d'IA est
facultatif : seuls le rangement, les routines et la recherche par le sens en ont besoin.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/architecture-dark.svg">
  <img src="docs/assets/architecture.svg" alt="Architecture : les assistants IA (MCP, OAuth 2.1), les navigateurs (interface web) et les scripts (API REST) passent par l'application FastAPI ; un worker gère le rangement nocturne, les routines, la synchronisation Git, l'indexation et les webhooks ; les deux partagent PostgreSQL avec pgvector et les copies de travail Git ; le worker synchronise un dépôt Git par espace et appelle un fournisseur d'IA facultatif.">
</picture>

## Documentation

Chaque page existe en anglais et en français. Commencez par le
[sommaire de la documentation](docs/index.fr.md) et les [concepts](docs/concepts.fr.md).

- **Utilisateurs :** [brancher son assistant IA (MCP)](docs/guide/mcp.fr.md),
  [lire, écrire et chercher dans le wiki](docs/guide/wiki.fr.md).
- **Administration :** [installation](docs/admin/installation.fr.md),
  [dépôts Git](docs/admin/git.fr.md), [IA et rangement](docs/admin/ai.fr.md),
  [SSO](docs/admin/sso.fr.md), [webhooks](docs/admin/webhooks.fr.md),
  [sauvegarde et restauration](docs/admin/backup.fr.md),
  [scripts du serveur](deploy/host/README.fr.md).
- **Référence :** [concepts](docs/concepts.fr.md),
  [format du dépôt](docs/reference/repository-format.fr.md),
  [API REST](docs/reference/api.fr.md), [ligne de commande](docs/reference/cli.fr.md).
- **Développement :** [travailler sur le code](docs/dev/development.fr.md),
  [banc d'évaluation](docs/dev/eval.fr.md).

## Contribuer

Les contributions sont les bienvenues. Lisez d'abord [CONTRIBUTING.fr.md](CONTRIBUTING.fr.md). Les
commits doivent être signés selon le
[Developer Certificate of Origin](https://developercertificate.org/) (`git commit -s`). Chaque
participant suit le [code de conduite](CODE_OF_CONDUCT.md). Pour les questions et l'aide, voir
[SUPPORT.fr.md](SUPPORT.fr.md).

## Sécurité

Ne signalez pas une faille dans une issue publique. Utilisez le signalement privé de failles
de GitHub, comme l'explique [SECURITY.fr.md](SECURITY.fr.md).

## Licence

Apache-2.0, voir [LICENSE](LICENSE) et [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

L'image publiée ne contient ni Claude Code ni Codex. Si vous construisez l'image avec eux
(`AGENT_CLIS=true`, la valeur par défaut du Dockerfile), vous installez des logiciels tiers
soumis à leurs propres conditions.

---

Synaptane est développé avec l'aide d'assistants de programmation IA (Claude) ; chaque
modification est relue par le mainteneur.
