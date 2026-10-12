[English](README.md) | Français

# Scripts du serveur

Scripts lancés **sur le serveur** qui fait tourner Synaptane avec Docker Compose, derrière
le nginx de l'hôte (mode par défaut de `compose.yaml`), paramétrés pour servir sur
n'importe quel serveur.

| Script | Rôle | Lancé par |
|---|---|---|
| `deploy.sh [<ref>]` | Mise à jour : sauvegarde, construction, contrôles, retour arrière automatique | vous, à chaque mise à jour |
| `backup.sh [<étiquette>]` | Sauvegarde dans `~/synaptane-backups`, 14 jours gardés | cron, chaque nuit ; `deploy.sh` |
| `install-nginx.sh <domaine>` | Site nginx (HTTP → HTTPS, HSTS) et certificat Let's Encrypt | `sudo`, une fois (puis à chaque changement de `deploy/nginx/host-site.conf`) |

On suppose le dépôt cloné dans `~/synaptane`, avec son `.env` (voir
[l'installation](../../docs/admin/installation.fr.md)) :

```sh
git clone --branch v1.0.0-rc.1 https://github.com/synaptane/synaptane.git ~/synaptane
```

`deploy.sh` **construit l'image depuis les sources** (utile pour les CLI d'abonnement), avec
`compose.build.yaml`, sous un nom local. Dans `.env` :

```sh
COMPOSE_FILE=compose.yaml:compose.build.yaml
SYNAPTANE_IMAGE=synaptane:local
```

Sans `COMPOSE_FILE`, `deploy.sh` prend de lui-même `compose.yaml:compose.build.yaml` (plus
`compose.override.yaml` s'il existe) ; un `COMPOSE_FILE` qui omet `compose.build.yaml` est
refusé. Il refuse aussi de construire sous le nom de l'image publiée (`ghcr.io/synaptane/…`) :
un `docker compose pull` la remplacerait, et le retour arrière ne la retrouverait plus. Une
installation qui utilise déjà un autre nom local (`synaptane:latest`, par exemple) le garde :
il faut seulement que le nom ne change pas entre deux déploiements.

Pour suivre plutôt l'image publiée, `deploy.sh` est inutile : `docker compose pull && docker
compose up -d` (voir [l'installation](../../docs/admin/installation.fr.md)).

## Fork privé : clé de déploiement (facultatif)

Si vous déployez depuis votre propre fork **privé**, le serveur lit le dépôt GitHub avec sa
propre clé SSH, **en lecture seule** et limitée à ce
dépôt : elle ne peut rien pousser, et la révoquer ne touche à rien d'autre.

1. Sur le serveur, créer la clé (sans phrase de passe : `deploy.sh` doit pouvoir s'en
   servir seul) :

   ```sh
   ssh-keygen -t ed25519 -N "" -C "synaptane deploy $(hostname)" -f ~/.ssh/synaptane_deploy
   cat ~/.ssh/synaptane_deploy.pub
   ```

2. Sur GitHub, dans le dépôt : **Settings → Deploy keys → Add deploy key**. Titre : le nom
   du serveur ; clé : la ligne affichée ; **ne cochez pas** « Allow write access ».

3. Sur le serveur, un alias d'hôte qui n'utilise que cette clé, dans `~/.ssh/config` :

   ```
   Host github-synaptane
       HostName github.com
       User git
       IdentityFile ~/.ssh/synaptane_deploy
       IdentitiesOnly yes
   ```

   Puis `chmod 600 ~/.ssh/config`, et accepter la clé d'hôte de GitHub une fois :
   `ssh -T git@github-synaptane` (réponse attendue : « successfully authenticated… does not
   provide shell access »).

4. Brancher le dépôt sur cet alias :

   ```sh
   # Serveur neuf :
   git clone git@github-synaptane:<compte>/synaptane.git ~/synaptane
   # Copie existante (arrivée par git bundle, par exemple) :
   cd ~/synaptane
   git remote set-url origin git@github-synaptane:<compte>/synaptane.git   # ou : git remote add origin …
   git fetch origin
   ```

Avec plusieurs serveurs : une clé par serveur (GitHub refuse la même clé sur deux dépôts,
et une clé par serveur se révoque seule).

## Mettre à jour : `deploy.sh`

```sh
cd ~/synaptane
deploy/host/deploy.sh                 # dernière version publiée (étiquette v*)
deploy/host/deploy.sh v1.0.1          # une étiquette, une branche ou un commit
deploy/host/deploy.sh origin/main     # dernier commit de main (en développement)
DRY_RUN=1 deploy/host/deploy.sh       # à blanc : ce qui serait fait, sans rien changer
```

Lisez le [journal des modifications](../../CHANGELOG.md) avant une nouvelle version. Le script :

1. récupère le code (`git fetch`) et se place sur le commit visé (`git checkout --detach`) ;
2. fait une sauvegarde (`backup.sh avant-<commit>`), et garde l'image en service sous le
   nom `synaptane:precedent` ;
3. construit l'image avec le commit inscrit (`synaptane --version` l'affiche), aussi
   nommée `synaptane:<commit>`, puis redémarre (`docker compose up -d --no-build`) ;
4. contrôle pendant 3 minutes au plus : `/readyz` à 200, `synaptane --version` au bon
   commit, worker `healthy`, `synaptane doctor` ;
5. **en cas d'échec, revient seul en arrière** : commit et image précédents, et
   restauration de la sauvegarde **seulement si** une migration a changé la base ;
6. note une ligne `date commit résultat` dans `~/.local/state/synaptane/deploy.log`.

Code de sortie 0 : déployé ; 1 : échec (le journal dit si le retour arrière a réussi) ;
2 : rien n'a été fait (usage, modifications locales, CI pas verte…).

Options et réglages (détail dans l'en-tête du script) :
- `--check-ci` : refuse un commit dont la CI GitHub n'est pas verte. Pour un dépôt public,
  rien à fournir. Pour un fork privé, il faut un jeton dans `GITHUB_TOKEN` : jeton à droits
  fins (*fine-grained*), limité à ce dépôt, permission **Actions : lecture**. Par exemple
  dans `~/.config/synaptane/github-token` (droits 600), puis
  `GITHUB_TOKEN=$(cat ~/.config/synaptane/github-token) deploy/host/deploy.sh --check-ci` ;
- `--no-backup` : sans sauvegarde, seulement quand l'application ne tourne pas encore ;
- `DEPLOY_TIMEOUT` (180 s), `STATE_DIR`, `BACKUP_DIR`, `SYNAPTANE_DIR`, `GITHUB_REPO`.

Les images téléchargées (PostgreSQL, certbot) ne changent pas avec `deploy.sh` : pour les
mettre à jour, `docker compose pull --ignore-buildable && docker compose up -d`, après une
sauvegarde.

Nettoyage : les trois dernières étiquettes `synaptane:<commit>` sont gardées, les plus
anciennes retirées. Rien d'autre n'est effacé : pas de `docker image prune`, car le serveur
peut faire tourner d'autres projets. Pour récupérer la place des anciennes couches de
construction, lancez vous-même `docker image prune` et `docker builder prune`.

Revenir à une version antérieure, c'est déployer son commit :
`deploy/host/deploy.sh <commit>` (le commit de chaque déploiement est dans le journal). Si
la base a été migrée entre-temps, restaurez la sauvegarde faite avant (procédure à la main
dans [l'installation](../../docs/admin/installation.fr.md#revenir-en-arrière)).

## Sauvegarde chaque nuit : `backup.sh`

`crontab -e`, sous le compte qui fait tourner Docker :

```cron
0 3 * * * $HOME/synaptane/deploy/host/backup.sh > /dev/null 2>> $HOME/synaptane-backups/cron.err
```

- archive `~/synaptane-backups/synaptane-AAAA-MM-JJ.tar.gz`, droits 600 (elle contient la
  clé maître), dossier en 700 ;
- écrite dans un `.part`, lue en entier, puis renommée : jamais d'archive tronquée qui
  ressemble à une vraie ;
- archives de plus de `RETENTION_DAYS` jours (14) effacées ; une ligne par sauvegarde dans
  `~/synaptane-backups/journal.log` ;
- `OFFSITE=1` ajoute la copie hors serveur (`synaptane backup --offsite`, voir
  [Sauvegarde](../../docs/admin/backup.fr.md)) : `0 3 * * * OFFSITE=1 $HOME/synaptane/…`.

Ces sauvegardes sont sur le même disque que le serveur : elles protègent d'une erreur,
pas de la perte du serveur.

## Site nginx : `install-nginx.sh`

```sh
sudo LETSENCRYPT_EMAIL=vous@example.com deploy/host/install-nginx.sh brain.example.com
```

Il installe `deploy/nginx/host-site.conf` avec votre domaine : HTTP redirigé vers HTTPS
(sauf les défis ACME), HSTS, `server_tokens off`, journal sans jetons, MCP en flux. Le
certificat est propre au domaine (certbot en *webroot*, renouvelé par le minuteur de
certbot), sans toucher aux autres sites du serveur. Relançable : c'est aussi la façon
d'appliquer une nouvelle version du site. `APP_PORT` si Synaptane n'écoute pas sur 8000.
