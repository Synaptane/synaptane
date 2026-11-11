[English](installation.md) | Français

# Installer et exploiter Synaptane

> [!TIP]
> **Vous voulez juste essayer ?** Une instance jetable sur votre ordinateur, avec les
> données de démonstration :
> [Essayer en local en 2 minutes](../../README.fr.md#essayer-en-local-en-2-minutes), puis
> [Données de démonstration](first-steps.fr.md#données-de-démonstration). Cette page installe
> une vraie instance.

Deux façons d'installer :
- **Docker Compose** (recommandé), sur un serveur Linux, avec l'image publiée ou une image que
  vous construisez vous-même ;
- **depuis les sources**, pour développer ou pour un serveur sans Docker.

Synaptane tourne sous Linux, ou WSL2 sous Windows, sur x86-64 ou arm64 (serveurs ARM). macOS n'est
pas testé.

## Avec Docker Compose

Prérequis :
- Docker Engine avec le plugin Compose ;
- un nom de domaine qui pointe vers le serveur : HTTPS est indispensable pour les cookies,
  OAuth, le SSO et les connecteurs de Claude.ai et ChatGPT.

Les images publiées existent pour x86-64 et arm64 : Docker télécharge celle du serveur. L'image
`ghcr.io/synaptane/synaptane` contient tout le reste : Git, ripgrep,
Tesseract (anglais et français), catdoc (anciens fichiers Office), le client PostgreSQL pour
les sauvegardes, age et rclone. Elle ne contient **pas** les CLI Claude Code et Codex, qui ne
servent qu'à faire tourner l'agent IA sur un abonnement Claude ou ChatGPT : pour cela,
[construisez l'image vous-même](#construire-limage-vous-même).

**Récupérer les fichiers et démarrer.** Le dépôt fournit `compose.yaml`, `.env.example`, la
configuration nginx et les scripts du serveur. Clonez la version, puis lancez l'image
publiée :

```sh
git clone --branch v1.17.0 https://github.com/synaptane/synaptane.git ~/synaptane
cd ~/synaptane
cp .env.example .env
chmod 600 .env                # il contient le mot de passe de la base
openssl rand -hex 24          # à copier dans POSTGRES_PASSWORD
nano .env                     # PUBLIC_URL, POSTGRES_PASSWORD, mode (voir plus bas)
docker compose pull
docker compose up -d
docker compose exec app synaptane admin create --username admin --display-name "Admin"
docker compose exec app synaptane doctor
```

Git indique que la copie est en « detached HEAD » : c'est normal après le clone d'une
étiquette, et vous n'avez rien à y committer.

**Sauvegardes.** Le worker fait déjà une sauvegarde vérifiée chaque nuit, dans le volume
`synaptane-backups`. Pour la copie chiffrée hors du serveur, une commande, qui pose ses
questions une à une (voir [Sauvegarde](backup.fr.md)) :

```sh
docker compose exec -it app synaptane backup setup
```

`admin create` demande le mot de passe, de 12 caractères au moins.

`SYNAPTANE_VERSION` choisit l'étiquette de l'image. `.env.example` la règle sur la version
clonée, et `compose.yaml` prend cette même version si elle manque : l'image et le fichier
Compose vont toujours ensemble. Il n'y a pas de `latest` à suivre : une mise à jour est
toujours un choix (voir [Mettre à jour](#mettre-à-jour)).

`doctor`, `synaptane --version` et le pied de la page d'administration affichent la version
et le commit en service (par exemple `1.0.0rc1 (abc1234)`).

Les services :
- `postgres` : PostgreSQL 17 avec pgvector ;
- `migrate` : applique les migrations, puis s'arrête ;
- `app` : web, API REST et MCP ;
- `worker` : synchronisation Git, indexation, rangement, routines, webhooks et sauvegarde de
  la nuit ;
- `agents` : les CLI des abonnements Claude et ChatGPT, à l'écart du worker, sans les données
  ni la base (voir [Le conteneur des agents](ai.fr.md#le-conteneur-des-agents)). Avec
  l'image publiée, qui n'a aucun CLI, il tourne au repos.

Les données sont dans des volumes Docker :
- `postgres-data` ;
- `synaptane-data` : clé maître, copies des dépôts, texte extrait ;
- `synaptane-agents` : connexions des CLI pour les abonnements, monté dans le seul conteneur
  des agents (dans le worker jusqu'à la 1.14 : même volume, les connexions restent valables) ;
- `synaptane-agents-key` : la clé que partagent le worker et le conteneur des agents, créée
  par le worker ;
- `synaptane-backups` : les sauvegardes de la nuit (7 jours gardés), et `synaptane backup`
  sans `--output`. Il est sur le même disque et **`docker compose down -v` l'efface** avec
  les autres : la copie chiffrée hors serveur protège de cela (voir [Sauvegarde](backup.fr.md)).

### Construire l'image vous-même

Construisez depuis une étiquette de version si vous voulez les CLI Claude Code et Codex
(abonnements), ou si vous préférez ne pas tirer une image toute faite. `compose.yaml` seul ne
construit jamais : la construction est déclarée dans `compose.build.yaml`, que vous ajoutez
par `COMPOSE_FILE`.

```sh
git clone --branch v1.17.0 https://github.com/synaptane/synaptane.git ~/synaptane
cd ~/synaptane
cp .env.example .env && chmod 600 .env
nano .env                     # comme plus haut, plus ces deux lignes :
                              #   COMPOSE_FILE=compose.yaml:compose.build.yaml
                              #   SYNAPTANE_IMAGE=synaptane:local
GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build   # commit inscrit dans l'image
docker compose exec app synaptane admin create --username admin --display-name "Admin"
```

La construction prend une dizaine de minutes. `AGENT_CLIS=true` (par défaut) installe Claude
Code et Codex, logiciels tiers soumis à leurs propres conditions ; `AGENT_CLIS=false` dans
`.env` les laisse de côté.

Gardez un nom local comme `synaptane:local` (celui que prend `compose.build.yaml` quand
`SYNAPTANE_IMAGE` est vide), jamais le nom de l'image publiée : le prochain
`docker compose pull` remplacerait votre image. En mode proxy, l'image nginx est construite
aussi, sous le nom `synaptane-nginx:local`. Pour ne mettre à jour que les images téléchargées
(PostgreSQL, certbot) : `docker compose pull --ignore-buildable`.

### Choisir le mode : derrière le nginx du serveur, ou tout-en-un

**Mode par défaut : le serveur a déjà un nginx** (ou Caddy, Traefik…) qui sert d'autres
sites. Laissez `COMPOSE_PROFILES` vide. Synaptane écoute alors sur `127.0.0.1:8000`
(`APP_PORT`), joignable seulement depuis le serveur, et le nginx de l'hôte fait le HTTPS :

```sh
sudo LETSENCRYPT_EMAIL=vous@example.com deploy/host/install-nginx.sh brain.example.com
```

Le script installe le site `deploy/nginx/host-site.conf` avec votre domaine, après avoir
obtenu le certificat Let's Encrypt (certbot en *webroot*, sans toucher aux autres sites) :
HTTP redirigé vers HTTPS, HSTS, `server_tokens off`, journal sans jetons. Mettez
`APP_PORT=…` devant la commande si Synaptane n'écoute pas sur 8000. Sans le script : copiez
ce fichier dans `/etc/nginx/sites-available/`, remplacez `brain.example.com`, et obtenez le
certificat avant d'activer le bloc 443.

Le site désactive la mise en tampon (`proxy_buffering off`) pour le MCP et accepte des
envois de 100 Mo. Ne publiez pas le port 8000 sur Internet (laissez `APP_BIND`
à `127.0.0.1`) : l'application fait confiance aux en-têtes `X-Forwarded-*` venus des réseaux
Docker (`FORWARDED_ALLOW_IPS`, `172.16.0.0/12,192.168.0.0/16` par défaut). Si le journal
montre la même adresse pour tous les visiteurs, c'est que ce réglage ne correspond pas à
votre réseau : la limite des tentatives de connexion compterait alors tout le monde ensemble.

**Mode tout-en-un : rien d'autre ne tourne sur le serveur.** Dans `.env` :

```sh
COMPOSE_PROFILES=proxy
DOMAIN=brain.example.com
LETSENCRYPT_EMAIL=vous@example.com
```

Un nginx dans Docker écoute sur les ports 80 et 443 :
- certbot obtient le certificat Let's Encrypt, puis le renouvelle automatiquement ;
- nginx sert un certificat autosigné provisoire en attendant, puis recharge le vrai dès
  qu'il arrive (environ une minute) ;
- pour un premier essai, `LETSENCRYPT_STAGING=1` donne des certificats de test, sans
  risquer les limites de Let's Encrypt.

Le domaine doit déjà pointer vers le serveur, avec les ports 80 et 443 ouverts. En cas
d'échec, certbot réessaie toutes les heures (`docker compose logs certbot`).

On passe d'un mode à l'autre en changeant `COMPOSE_PROFILES`, puis
`docker compose up -d --remove-orphans`.

### Mettre à jour

Lisez d'abord le [journal des modifications](../../CHANGELOG.md) (en anglais) pour la version
visée. Les migrations s'appliquent toutes seules au démarrage (le service `migrate` journalise
chacune). Comptez une coupure de quelques dizaines de secondes.

**Avec l'image publiée :**

```sh
cd ~/synaptane
umask 077   # la sauvegarde contient la clé maître
docker compose exec -T app synaptane backup --output - > synaptane-avant-maj.tar.gz
git fetch --tags && git checkout v1.17.0      # fichier Compose et scripts de la nouvelle version
nano .env                                    # SYNAPTANE_VERSION=1.17.0
docker compose pull
docker compose up -d
docker compose exec app synaptane --version  # la version attendue ?
```

**Avec une image construite par vos soins** (`COMPOSE_FILE` et `SYNAPTANE_IMAGE` réglés
comme dans [Construire l'image vous-même](#construire-limage-vous-même)),
`deploy/host/deploy.sh` fait tout sur le serveur :

```sh
cd ~/synaptane
deploy/host/deploy.sh v1.17.0      # une étiquette, une branche ou un commit
```

Le script fait une sauvegarde, garde l'image en service, construit la nouvelle avec son
commit, redémarre, puis contrôle pendant 3 minutes au plus (`/readyz`, version, worker,
conteneur des agents, `doctor`). **En cas d'échec, il revient seul en arrière** : commit et image précédents, et
restauration de la sauvegarde si une migration a changé la base. Chaque déploiement laisse
une ligne `date commit résultat` dans `~/.local/state/synaptane/deploy.log`.
`DRY_RUN=1` montre ce qui serait fait ; `--check-ci` refuse un commit dont la CI n'est pas
verte. Détail : [`deploy/host/README.fr.md`](../../deploy/host/README.fr.md).

À la main, si le script ne convient pas :

```sh
umask 077
docker compose exec -T app synaptane backup --output - > synaptane-avant-maj.tar.gz
git fetch --tags && git checkout v1.17.0
docker compose pull --ignore-buildable      # images téléchargées : PostgreSQL, certbot
GIT_SHA=$(git rev-parse HEAD) docker compose build --pull --no-cache  # correctifs Debian compris
docker compose up -d
docker compose exec app synaptane --version
```

Mettez à jour au moins une fois par mois pour les correctifs de sécurité du système : avec
l'image publiée, passez à la dernière version (chacune est construite avec les correctifs
Debian du jour) ; avec une image construite par vos soins, reconstruisez avec
`--pull --no-cache`, même sans nouvelle version (`deploy.sh` reconstruit avec `--pull`). Les
images téléchargées (PostgreSQL, certbot) se mettent à jour
à part, après une sauvegarde : `docker compose pull --ignore-buildable && docker compose up -d`.

### Revenir en arrière

Pour revenir à une version antérieure **sans migration entre les deux** : avec l'image
publiée, remettez son numéro dans `SYNAPTANE_VERSION` (`.env`), puis `docker compose up -d` ;
avec une image construite par vos soins, déployez son étiquette ou son commit (il est dans le
journal) : `deploy/host/deploy.sh <commit>`. `deploy.sh` revient déjà seul en arrière quand
ses contrôles échouent. Revenir sous la 1.15 laisse tourner le conteneur `agents`, inutilisé
(les CLI tournent de nouveau dans le worker, avec les mêmes connexions) :
`docker compose up -d --remove-orphans` l'arrête (le retour arrière automatique de
`deploy.sh` le fait).

Si une migration a changé la base entre-temps, l'ancien code ne démarre pas sur la base
migrée : il faut aussi restaurer la sauvegarde faite juste avant la mise à jour fautive
(`synaptane-avant-maj.tar.gz`, ou avec `deploy.sh`
`~/synaptane-backups/synaptane-…-avant-<commit fautif>.tar.gz`) :

```sh
docker compose stop app worker
git checkout --detach v1.16.1                   # la version précédente
nano .env                                       # image publiée : SYNAPTANE_VERSION=1.16.1
# image construite par vos soins : GIT_SHA=$(git rev-parse HEAD) docker compose build
docker compose run --rm --no-deps -T app synaptane restore --force - < synaptane-avant-maj.tar.gz
docker compose up -d
```

La base est restaurée dans l'état de la sauvegarde faite avant la mise à jour (schéma
compris) ; les dépôts Git restent la référence pour le contenu.

### Journaux

`docker compose logs -f app worker agents` : une ligne par événement, avec l'heure. Le worker
écrit une ligne par tâche (synchronisation, indexation, rangement : durée, résultat).
`SYNAPTANE_LOG_LEVEL=DEBUG` dans `.env` donne plus de détail ; `WARNING` en donne moins.

### Abonnements Claude et ChatGPT

Il faut une [image construite par vos soins](#construire-limage-vous-même), avec les CLI.
Ils tournent dans le conteneur `agents`, à l'écart du worker, et s'y connectent une fois.
Leur connexion est rangée dans le volume `synaptane-agents`, donc elle survit aux mises à
jour (une connexion faite dans le worker jusqu'à la 1.14 reste valable : rien à refaire).

```sh
docker compose exec agents claude auth login         # abonnement Claude
docker compose exec agents codex login --device-auth # abonnement ChatGPT
```

Vérifiez d'abord les conditions de l'éditeur : voir [Intelligence artificielle](ai.fr.md).

### Autres réglages

Les options ci-dessous passent par des variables `SYNAPTANE_<OPTION>`. Pour en ajouter
une, créez un fichier `compose.override.yaml`. Compose ne le lit de lui-même que si
`COMPOSE_FILE` n'est pas défini ; si `.env` définit `COMPOSE_FILE` (image construite par vos
soins, démonstration), ajoutez-le à la liste, par exemple
`COMPOSE_FILE=compose.yaml:compose.build.yaml:compose.override.yaml`.

```yaml
services:
  app:
    environment:
      SYNAPTANE_SESSION_DAYS: "14"
  worker:
    environment:
      SYNAPTANE_SESSION_DAYS: "14"
```

## Depuis les sources

Prérequis : Python 3.12 avec [uv](https://docs.astral.sh/uv/), et PostgreSQL 17 avec
l'extension pgvector.

```sh
uv sync
uv run synaptane init --database-url postgresql+psycopg://synaptane:MOT_DE_PASSE@localhost:5432/synaptane \
                      --data-dir /var/lib/synaptane --public-url https://brain.example.com
uv run synaptane db upgrade
uv run synaptane admin create --username admin --display-name "Admin"
uv run synaptane doctor
uv run synaptane serve --host 127.0.0.1 --port 8000   # web, REST et MCP
uv run synaptane worker                                # tâches de fond
```

Outils système :
- Git 2.28 ou plus récent ;
- OpenSSH ;
- **ripgrep** (`rg`), pour la recherche ;
- **Tesseract**, facultatif mais recommandé : texte des PDF scannés et des images
  (`tesseract-ocr` avec `tesseract-ocr-eng` et `tesseract-ocr-fra`) ;
- **catdoc**, facultatif mais recommandé : texte des anciens fichiers Word et Excel `.doc` et
  `.xls` (paquet `catdoc`, qui fournit aussi `xls2csv` ; les anciens `.ppt` sont lus sans lui) ;
- le client PostgreSQL (`pg_dump`, `pg_restore`) de même version que le serveur, pour les
  sauvegardes ;
- `age` et `rclone`, pour la copie chiffrée hors serveur.

`synaptane doctor` vérifie leur présence.

## Annexe : fork privé

Si vous déployez depuis votre propre fork **privé**, un `git clone https://…` demande un
identifiant et échoue sur un serveur. Donnez plutôt au serveur une **clé de déploiement** :
une clé SSH à lui, en lecture seule, déclarée sur ce seul dépôt (GitHub → Settings → Deploy
keys, sans droit d'écriture). Clonez ensuite par un alias d'hôte SSH qui n'utilise que cette
clé :

```sh
git clone git@<alias-ssh>:<compte>/synaptane.git ~/synaptane
```

Création de la clé et de l'alias SSH dans `~/.ssh/config` : voir
[`deploy/host/README.fr.md`](../../deploy/host/README.fr.md). La suite de l'installation est
celle d'une [image construite par vos soins](#construire-limage-vous-même).

**Étape suivante :** la [configuration](configuration.fr.md) (options, e-mail, index de
recherche, alertes, supervision, journal d'audit, rôles), puis les
[premiers pas](first-steps.fr.md) (organisations, espaces, données de démonstration).
