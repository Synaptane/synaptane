[English](backup.md) | Français

# Sauvegarde et restauration

Avec Docker :

1. **Rien à faire : une sauvegarde vérifiée chaque nuit.** Le worker fait une archive à 4 h,
   la relit, et garde celles des 7 derniers jours dans le volume `synaptane-backups`. Elle
   protège d'une erreur de manipulation ou d'une mise à jour ratée.
   **Administration → Santé** montre la dernière.
2. **Une commande, une fois, pour la copie chiffrée hors du serveur**, qui protège d'une panne du disque,
   d'un vol ou d'un serveur perdu :

   ```sh
   docker compose exec -it app synaptane backup setup
   ```

   Elle fabrique la clé de chiffrement (ou prend la vôtre), déclare le stockage (S3 et
   compatibles, SFTP, WebDAV/Nextcloud, NAS SMB, Google Drive, OneDrive, Dropbox…), le teste,
   et fait la première copie. Les suivantes partent chaque nuit avec la sauvegarde.
3. **Puis une habitude : un exercice de restauration chaque mois**
   (`scripts/restore-drill.sh`), sur votre poste.
   Une sauvegarde qu'on n'a jamais restaurée n'est qu'un espoir.

À tout moment, `docker compose exec app synaptane backup check` dit ce qui fonctionne et, pour
ce qui manque, la commande exacte à lancer.

## Ce qui est sauvegardé

Chaque sauvegarde est une archive `.tar.gz` qui contient :

| Élément | Pourquoi |
|---|---|
| La base PostgreSQL (`pg_dump`) | Comptes, organisations, espaces, droits, jetons, réglages, secrets chiffrés, historique des rangements, index sémantique. |
| La **clé maître** | Sans elle, les secrets chiffrés de la base sont perdus : identifiants Git, clés IA, secrets SSO et webhooks. |
| Les copies locales des dépôts | Les contenus sont déjà sur les dépôts distants. Les copies gardent ce qui n'a pas encore pu être poussé, par exemple en cas de conflit ou de panne de la forge. |
| Un manifeste | Version de Synaptane, révision de la base, date, nombre de comptes, d'organisations et d'espaces. |

Ne sont **pas** sauvegardés :
- le texte extrait des PDF et des images, qui se reconstruit depuis Git ;
- les connexions des CLI Claude Code et Codex, à refaire après une restauration ;
- les réglages de la copie hors serveur gardés dans le volume de données : la configuration
  rclone des stockages (`/data/rclone/rclone.conf`), et les clés publiques et destinations
  enregistrées par `backup setup` (`/data/backup/`). Gardez les identifiants des stockages
  dans votre gestionnaire de mots de passe, et relancez `backup setup` après une restauration
  sur un nouveau serveur.

L'archive contient la clé maître en clair : **elle donne accès à tous les secrets**. C'est
pourquoi la copie hors serveur est toujours chiffrée, et refusée sans clé de chiffrement.
Avec `--without-key`, la clé reste hors de l'archive ; il faut alors la sauvegarder à part,
une fois pour toutes, car elle ne change pas.

## La sauvegarde de la nuit, sur le serveur

Chaque jour à `BACKUP_TIME` (4 h, heure du serveur `TZ`), le worker :

1. efface ses propres archives plus anciennes que `BACKUP_LOCAL_RETENTION_DAYS` (7 jours), en
   gardant toujours les 3 plus récentes : une sauvegarde qui échoue jour après jour ne finit
   pas avec un dossier vide. Seules les archives du worker,
   `synaptane-AAAA-MM-JJ-HHMMSS-auto.tar.gz`, sont concernées : un fichier déposé dans
   `/backups` à la main ou par `synaptane backup` n'est jamais effacé ;
2. vérifie l'espace libre : la nouvelle archive (la dernière plus 20 %) doit laisser 2 Go
   libres sur le disque, ou un dixième d'un disque de moins de 20 Go, pour PostgreSQL qui le
   partage. Sinon la sauvegarde échoue avec une alerte, et rien n'est écrit ;
3. écrit l'archive dans un fichier caché `.part` de `/backups`, **la relit** (compression
   intègre comme avec `gzip -t`, tar complet, manifeste et clé présents, dump entièrement
   lisible par `pg_restore`), et seulement alors lui donne son nom (droits 600) : une archive
   tronquée (disque plein, interruption) ne ressemble jamais à une bonne ;
4. envoie la copie chiffrée hors serveur, si elle est en place (plus bas).

Elle n'a besoin de rien : ni tâche cron, ni réglage. Un worker démarré après l'heure
(installation neuve, redémarrage, mise à jour) fait aussitôt la sauvegarde du jour. Avec
7 jours gardés, comptez jusqu'à 10 archives de la taille de votre base et de vos dépôts dans
le volume `synaptane-backups`, sur le disque du serveur.

**Un échec alerte les super-administrateurs**, quelle qu'en soit la cause (disque plein, dump
bloqué plus de `BACKUP_TIMEOUT_MINUTES`, destination injoignable, worker tué plusieurs fois) :
par e-mail (si l'envoi est configuré) et dans les alertes système d'**Administration →
Santé** ; la même alerte n'est pas renvoyée avant 20 heures. Les archives précédentes sont
gardées.

**Administration → Santé** montre la dernière archive vérifiée, comment les sauvegardes sont
faites, le dernier passage du worker (en attente, en cours, fait, sauté parce qu'une autre
sauvegarde tournait, en échec et pourquoi) et, quand quelque chose manque, la commande à
lancer. **Sauvegarder maintenant** demande au worker une sauvegarde tout de suite (inscrite au
journal d'audit).

Réglages, dans `.env` (puis `docker compose up -d`) :

| Variable | Défaut | Rôle |
|---|---|---|
| `BACKUP_TIME` | `04:00` | Heure de la sauvegarde de la nuit, heure du serveur (`TZ`) : `04:00`, `4:00` ou `04h00`. `off` : pas de sauvegarde automatique (une tâche cron à vous s'en charge, voir plus bas). Une valeur qui n'est pas une heure n'empêche pas Synaptane de démarrer : 04:00 est utilisé, et `doctor`, `backup check` et la page de santé le signalent. |
| `BACKUP_LOCAL_RETENTION_DAYS` | `7` | Jours de conservation des archives du worker dans `/backups` (les 3 plus récentes toujours) ; `0` : toutes. |
| `BACKUP_TIMEOUT_MINUTES` | `120` | Durée maximale d'un dump de la base, ou de sa relecture : un dump bloqué est abandonné, avec une alerte. |

Pour désactiver la sauvegarde automatique : `BACKUP_TIME=off` dans `.env`, puis
`docker compose up -d`.

Une sauvegarde à la fois : celle du worker, celle d'une tâche cron, d'un déploiement ou à la
main s'attendent l'une l'autre (un verrou dans le volume de données), au plus deux fois
`BACKUP_TIMEOUT_MINUTES`, en le disant. La copie hors serveur part une fois le verrou libéré :
des heures de copie ne retiennent aucune autre sauvegarde. Une restauration refuse de démarrer
pendant une sauvegarde.

Le volume `synaptane-backups` est sur le même disque que les données, et **`docker compose
down -v` l'efface** avec tous les autres volumes : la copie hors serveur protège de cela. Pour
en sortir une archive :

```sh
docker compose exec app ls -l /backups
docker compose cp app:/backups/synaptane-2026-10-03-040000-auto.tar.gz .
```

## Copie chiffrée hors du serveur

### Principe

```text
serveur : sauvegarde de la nuit (ou synaptane backup --offsite)
  archive vérifiée ──age──▶ synaptane-AAAA-MM-JJ-HHMMSS.tar.gz.age ──rclone──▶ destination 1
                    (clé publique)                                       ├──▶ destination 2
                                                                         └──▶ …
votre poste : clé privée age ──▶ déchiffre ──▶ restaure
```

- **Chiffrement par [age](https://age-encryption.org)** vers une ou plusieurs clés
  publiques. Le serveur chiffre mais **ne peut pas déchiffrer** : un vol du serveur ou de la
  destination ne donne rien. La clé privée reste chez vous.
- **Copie par [rclone](https://rclone.org/overview/)**, qui connaît plus de 70 types de
  stockage. Les identifiants des stockages restent dans la configuration rclone, jamais dans
  la base ni en ligne de commande.
- **Plusieurs destinations** : chacune reçoit la copie. Une destination en panne n'empêche
  pas les autres ; l'alerte dit laquelle a échoué.
- **Rétention** : après une copie réussie, les archives `synaptane-*.age` plus anciennes
  que `BACKUP_RETENTION_DAYS` (30 jours par défaut) sont effacées de **cette** destination,
  et seulement à sa racine. Rien d'autre n'est touché, même dans un dossier partagé. Une
  destination qui n'a pas reçu la copie du jour garde toutes ses anciennes copies.

`age` et `rclone` sont dans l'image Docker. Depuis les sources, installez les paquets `age`
et `rclone`.

### La mettre en place : `backup setup`

```sh
docker compose exec -it app synaptane backup setup
```

La commande dit ce qui est déjà fait et ne demande que ce qui manque. Relancée, elle ne change
rien à ce qui fonctionne.

**1. Sauvegarde sur ce serveur** : elle rappelle l'heure et le dossier ; rien à répondre.

**2. Clé de chiffrement.** Deux choix :
- **appuyer sur Entrée** : une paire de clés est fabriquée dans le conteneur. La clé privée
  est **affichée une seule fois et enregistrée nulle part** ; la commande attend que vous
  tapiez `saved`, puis ne garde que la clé publique (dans `/data/backup/age-recipients.txt`) ;
- **coller la clé publique** (`age1…`) d'une paire faite sur votre poste, pour qui ne veut pas
  que la clé privée passe jamais par le serveur :

  ```sh
  age-keygen -o synaptane-backup.key
  # Public key: age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq
  ```

Rangez la clé privée **avec la clé maître**, une fois pour toutes, à deux endroits : votre
gestionnaire de mots de passe et une copie papier ou une clé USB dans un tiroir. **Jamais sur
le serveur** : qui détient le serveur et cette clé lit les copies. Sans elle, les copies sont
illisibles, y compris pour vous.

Plusieurs personnes peuvent restaurer : chacune ajoute sa clé publique
(`synaptane offsite key age1…`), et n'importe laquelle des clés privées ouvre l'archive. Une
clé SSH publique (`ssh-ed25519 …`) convient aussi.

**3. Destination.** Le stockage et son accès se déclarent une fois dans rclone : la commande
propose de lancer l'assistant de rclone (`rclone config`), qui liste tous les types de
stockage et pose les questions une à une (exemples plus bas). La configuration est écrite dans
`/data/rclone/rclone.conf` (volume `synaptane-data`, droits 600). Donnez ensuite la
destination, sous la forme `nom:dossier` (par exemple `b2:synaptane-sauvegardes`) : un petit
fichier témoin est écrit, relu puis effacé (droits d'écriture, de lecture et d'effacement,
dont la rétention a besoin), et la destination n'est enregistrée que si tout fonctionne (dans
`/data/backup/remotes.txt`).

Pour chaque destination, créez un accès **réservé à cet usage** : un seau ou un dossier
dédié, un compte ou une clé d'API limités à ce seau. Ainsi, une fuite de la configuration
rclone n'expose rien d'autre.

**4. Première sauvegarde** : faite tout de suite si vous le voulez, avec sa copie hors
serveur. Les suivantes partent chaque nuit.

Rien à redémarrer, rien à écrire dans `.env` : le worker lit les clés et destinations
enregistrées à chaque sauvegarde.

### Sans questions

Pour un script ou une gestion de configuration, les mêmes étapes une à une :

```sh
docker compose exec app synaptane offsite key            # nouvelle paire : clé privée affichée une fois
docker compose exec app synaptane offsite key age1…      # ou votre clé publique
docker compose exec -it app rclone config                # le stockage
docker compose exec app synaptane offsite add b2:synaptane-sauvegardes   # testée, puis enregistrée
docker compose exec app synaptane offsite remove b2:synaptane-sauvegardes
```

Ou dans `.env`, puis `docker compose up -d` ; ces variables s'ajoutent à ce que `setup` a
enregistré :

```sh
BACKUP_AGE_RECIPIENTS=age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq
BACKUP_REMOTES=b2:synaptane-sauvegardes,nas:sauvegardes/synaptane
BACKUP_RETENTION_DAYS=30
```

Plusieurs clés ou destinations : séparées par des virgules.

### Exemples de stockages

Ce que l'assistant de rclone écrit dans `rclone.conf` (valeurs à remplacer) :

**S3 et compatibles** (AWS, Scaleway, OVHcloud, Wasabi, MinIO…) :

```ini
[s3]
type = s3
provider = Scaleway
access_key_id = SCWXXXXXXXXXXXXXXXXX
secret_access_key = xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
region = fr-par
endpoint = s3.fr-par.scw.cloud
```

Destination : `s3:mon-seau/synaptane`. Pour OVHcloud, `provider = Other` et l'`endpoint`
de votre région (`s3.gra.io.cloud.ovh.net`…) ; pour AWS, `provider = AWS` et `region`.

**Backblaze B2** (clé d'application limitée à un seau) :

```ini
[b2]
type = b2
account = 0012345678900000000000001
key = K001xxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Destination : `b2:synaptane-sauvegardes`.

**SFTP** (un autre serveur, une box, un NAS) : déposez la clé SSH privée et la clé d'hôte
dans le volume, puis indiquez-les à rclone.

```sh
docker compose cp id_sauvegarde app:/data/rclone/id_sauvegarde
ssh-keyscan -t ed25519 nas.example.com > known_hosts
docker compose cp known_hosts app:/data/rclone/known_hosts
```

```ini
[nas]
type = sftp
host = nas.example.com
user = sauvegarde
key_file = /data/rclone/id_sauvegarde
known_hosts_file = /data/rclone/known_hosts
```

Destination : `nas:sauvegardes/synaptane`. `known_hosts_file` protège contre l'usurpation
du serveur.

**WebDAV / Nextcloud** (créez un mot de passe d'application dans Nextcloud ; l'assistant
le chiffre avec `rclone obscure`) :

```ini
[nextcloud]
type = webdav
url = https://cloud.example.com/remote.php/dav/files/alice/
vendor = nextcloud
user = alice
pass = <mot de passe obscurci par rclone>
```

Destination : `nextcloud:Sauvegardes/Synaptane`.

**NAS en SMB** (partage Windows, Synology, QNAP…) :

```ini
[smb]
type = smb
host = 192.168.1.20
user = sauvegarde
pass = <mot de passe obscurci par rclone>
```

Destination : `smb:partage/synaptane` (le premier élément est le nom du partage).

**Google Drive, OneDrive, Dropbox, pCloud** : la connexion passe par un navigateur, que le
serveur n'a pas. Dans l'assistant, répondez **non** à « Use auto config? » : il affiche une
commande `rclone authorize "drive" "…"`. Lancez-la sur votre poste (rclone installé), validez
dans le navigateur, puis collez la réponse dans l'assistant.

```ini
[gdrive]
type = drive
scope = drive.file
token = {"access_token":"…","refresh_token":"…"}
```

Destination : `gdrive:Sauvegardes Synaptane`. `scope = drive.file` limite rclone aux fichiers
qu'il a créés.

**Un dossier monté sur le serveur** (disque USB, montage NFS) : indiquez simplement son
chemin vu du conteneur, après l'avoir monté dans `compose.yaml`. Attention, un disque du
même serveur ne protège pas d'un vol ou d'un incendie.

**Contre un rançongiciel** qui effacerait aussi les copies : activez le versionnement et,
si le service le propose, le verrouillage des objets (*object lock*) sur le seau. Laissez
alors le stockage faire la rétention (règle de cycle de vie) et mettez
`BACKUP_RETENTION_DAYS=0`, ou donnez au serveur une clé sans droit d'effacement.

### Vérifier

```sh
docker compose exec app synaptane backup check
# [ok] Automatic backup: every day at 04:00, in /backups
# [ok] Free space: 41.2 GB in /backups
# [ok] Local retention: 7 days for the automatic backups (the 3 newest always kept)
# [ok] PostgreSQL client
# [ok] Last verified backup: 2026-11-08 04:00
# [ok] Encryption: 1 public key(s)
# [ok] Destination b2:synaptane-sauvegardes: writable
# [FAILED] Destination nas:sauvegardes/synaptane — write: … connection refused
# [ok] Off-site retention: 30 days
# [ok] Last off-site copy: 2026-11-08 04:02
```

Chaque ligne qui n'est pas `[ok]` dit quoi lancer. Chaque destination reçoit un fichier
témoin (écrit, relu, effacé) ; `--no-test` s'en passe. Code de sortie 1 si quelque chose
échoue. Les messages de la ligne de commande sont en anglais.

Autres commandes :
- `synaptane offsite test` : le fichier témoin seul, sur chaque destination ;
- `synaptane offsite list` : les archives chiffrées présentes sur chaque destination ;
- `synaptane doctor` : résume la sauvegarde automatique et la copie hors serveur, toujours en
  avertissement (`warning`), jamais en échec.

## Votre propre tâche cron (facultatif)

Avant la sauvegarde automatique, les sauvegardes étaient faites par une tâche cron du serveur,
avec le script versionné `deploy/host/backup.sh`. **Une installation qui en a une ne change
rien** : le script garde ses archives (14 jours par défaut dans `~/synaptane-backups`), envoie
la copie hors serveur avec `OFFSITE=1` et, une fois son archive vérifiée et en place, signale
à Synaptane qu'il a fait la sauvegarde du jour. La sauvegarde automatique du worker s'efface
alors (**Administration → Santé** le dit), et efface ses propres archives plus anciennes que la
rétention (la tâche cron garde les siennes). Elle regarde de nouveau chaque minute et
**reprend d'elle-même** dès que la dernière bonne sauvegarde de la tâche cron (avec sa copie
hors serveur, si des destinations sont en place) a plus de 26 heures : une nuit où la tâche
cron échoue, le worker fait la sauvegarde environ 26 heures après la précédente, le jour même.

- Avec `OFFSITE=1`, la tâche cron fait tout ; sans, tant que des destinations hors serveur
  sont en place, le worker fait quand même sa sauvegarde chaque nuit pour envoyer la copie.
  Ajoutez `OFFSITE=1`, ou retirez la tâche cron.
- `BACKUP_TIME=off` dans `.env` laisse les sauvegardes à la seule tâche cron.

Pour en mettre une en place malgré tout (un dossier hors des volumes Docker, par exemple) :

```sh
mkdir -m 700 -p ~/synaptane-backups   # avant le premier passage : cron y écrit ses erreurs
# crontab -e
0 3 * * * OFFSITE=1 $HOME/synaptane/deploy/host/backup.sh > /dev/null 2>> $HOME/synaptane-backups/cron.err
```

Le script écrit d'abord dans un fichier `.part`, renommé seulement si l'archive est bonne.
Une destination hors serveur en échec garde l'archive locale, se note au journal
(`~/synaptane-backups/journal.log`) et rend le code 3. Réglages : `BACKUP_DIR`,
`RETENTION_DAYS` (voir l'en-tête du script, et les
[scripts du serveur](../../deploy/host/README.fr.md)). Cron envoie par e-mail ce que le script
écrit sur l'erreur standard, si l'envoi de courrier est configuré sur le serveur.

Pour être prévenu quand plus aucune sauvegarde ne tourne (serveur éteint), ajoutez un moniteur
de type « dead man's switch » (Healthchecks.io, Uptime Kuma…), ou l'alerte Prometheus sur
`synaptane_backup_last_timestamp_seconds` (voir [Supervision](installation.fr.md#supervision)).

## Sauvegarde à la main : `synaptane backup`

```sh
umask 077
docker compose exec -T app synaptane backup --output - > synaptane-$(date +%F).tar.gz
```

L'archive est relue comme celle de la nuit avant d'être annoncée. Avec `--output -`, rien
n'est écrit sur la sortie standard si l'archive est mauvaise.

Options :
- `--output` : un fichier, un dossier, ou `-` pour la sortie standard. Sans `--output`,
  l'archive va dans `$SYNAPTANE_BACKUP_DIR` (`/backups` avec Docker), sinon dans le dossier
  courant ;
- `--without-repos` : sans les copies des dépôts ;
- `--without-key` : sans la clé maître ;
- `--offsite` : copie chiffrée hors serveur, vers les destinations en place. Sans `--output`,
  elle ne garde **aucune** copie locale : l'archive en clair est effacée du serveur une fois
  chiffrée.

L'archive est écrite dans un fichier caché `.part`, renommé une fois relu. Si une autre
sauvegarde est en cours, la commande attend qu'elle se termine (au plus deux fois
`BACKUP_TIMEOUT_MINUTES`), et le dit. Une sauvegarde complète (avec les dépôts et la clé)
gardée dans `/backups` ou envoyée sur la sortie standard, comme celle d'un déploiement, compte
comme la sauvegarde du jour si elle a moins de 6 heures quand vient l'heure du worker.

Codes de sortie :

| Code | Signification |
|---|---|
| 0 | Archive vérifiée, et copiée vers toutes les destinations (avec `--offsite`). |
| 1 | Pas de sauvegarde utilisable, ou copie hors serveur refusée (réglage manquant) : rien n'a été écrit. |
| 3 | Archive vérifiée (et écrite si `--output`), mais une destination au moins a échoué (copie ou rétention). Le message dit laquelle. |

## Restaurer

### Depuis une archive en clair

Par exemple sur un nouveau serveur :

```sh
docker compose up -d                  # instance neuve : base sans compte et nouvelle clé
docker compose stop app worker
docker compose run --rm --no-deps -T app synaptane restore - < synaptane-2026-10-03.tar.gz
docker compose start app worker
```

### Depuis une copie hors serveur

On déchiffre **sur votre poste**, et l'archive déchiffrée part directement dans la
restauration : la clé privée ne touche jamais le serveur.

```sh
# Sur votre poste, avec rclone configuré pour lire la destination :
rclone lsf b2:synaptane-sauvegardes                     # choisir l'archive
rclone copyto b2:synaptane-sauvegardes/synaptane-2026-10-06-040000.tar.gz.age .
# Sur le serveur : docker compose up -d && docker compose stop app worker
age -d -i synaptane-backup.key synaptane-2026-10-06-040000.tar.gz.age \
  | ssh serveur 'cd ~/synaptane && docker compose run --rm --no-deps -T app synaptane restore -'
# Sur le serveur : docker compose start app worker
```

Sans rclone sur votre poste, téléchargez le fichier depuis l'interface du stockage (console
S3, Nextcloud, Google Drive…). Si la paire de clés a été faite par `backup setup`, mettez la
clé privée qu'elle a affichée dans un fichier de votre poste (`synaptane-backup.key`), pour
`age -d -i`.

### Ce que fait la restauration

- elle remplace la base, la clé maître et les copies des dépôts. Une archive pouvant venir
  d'ailleurs, la configuration Git de chaque copie est réduite à l'essentiel (adresse du
  distant, branche) et ses hooks sont supprimés : rien ne peut s'exécuter à la reprise ;
- elle met la base au niveau de la version installée ;
- elle demande au worker de resynchroniser et de réindexer tous les espaces. Les pages
  écrites avant la panne et pas encore poussées partent alors sur les dépôts.

Elle s'arrête sans rien toucher dans ces cas :
- **l'application ou le worker sont encore connectés** à la base ;
- **une sauvegarde est en cours** : attendez qu'elle se termine ;
- **la sauvegarde vient d'une version plus récente** de Synaptane : installez d'abord cette
  version ;
- **l'instance a déjà des comptes** : ajoutez `--force` pour les remplacer ;
- **l'archive est abîmée** (copie interrompue, disque plein) : le dump est entièrement relu
  avant de toucher à la base ;
- **l'archive n'a pas la clé maître** (`--without-key`) et la clé en place n'est pas celle de
  la sauvegarde : déposez d'abord la clé sauvegardée, par exemple
  `docker compose cp master.key app:/data/master.key`.

La remise à zéro et la restauration de la base se font dans une seule transaction : en cas
d'erreur, la base reste telle qu'elle était. Par prudence, faites quand même un
`synaptane backup` juste avant un `restore --force`.

`synaptane doctor` vérifie ensuite que les secrets enregistrés se déchiffrent avec la clé en
place. Sur un nouveau serveur, relancez `backup setup` : les clés et destinations de la copie
hors serveur ne sont pas dans l'archive.

Une sauvegarde d'une version **antérieure** se restaure normalement : la base est vidée,
restaurée, puis mise à niveau. Revenir à la version précédente après une mise à jour
ratée, c'est donc : remettre l'ancien code, puis restaurer la sauvegarde faite avant la
mise à jour. Une instance neuve, sans compte, prend simplement la clé de l'archive à la
place de celle générée au démarrage.

## Exercice de restauration, chaque mois

Sur votre poste (pas sur le serveur), depuis une copie de ce dépôt, avec Docker :

```sh
./scripts/restore-drill.sh --identity ~/synaptane-backup.key --from b2:synaptane-sauvegardes
# ou avec un fichier déjà téléchargé :
./scripts/restore-drill.sh --identity ~/synaptane-backup.key --file synaptane-….tar.gz.age
```

Le script :
1. récupère la dernière archive de la destination (rclone) et la déchiffre ;
2. monte un projet Compose jetable, `synaptane-drill`, **sans port publié et sans worker** :
   rien n'est poussé vers vos dépôts, aucun rangement ne tourne, aucun e-mail ne part ;
3. restaure l'archive, attend que l'application réponde, lance `synaptane doctor` ;
4. compare le nombre de comptes, d'organisations et d'espaces au manifeste ;
5. démonte tout (`down -v`) et efface l'archive déchiffrée, même en cas d'échec.

Il finit par `Drill passed.`, ou par `Drill FAILED (n).` (`n` : le code de sortie) après
avoir affiché la fin des journaux de l'application. L'image utilisée est `$SYNAPTANE_IMAGE`,
sinon `synaptane:local` :
- `synaptane:local` absente : elle est construite depuis ce dépôt, sans les CLI
  d'abonnement ;
- une image publiée (`SYNAPTANE_IMAGE=ghcr.io/synaptane/synaptane:1.0.0-rc.1`) absente : elle
  est téléchargée.

Elle doit être au moins aussi récente que la version qui a fait la sauvegarde. Il faut
Docker Compose 2.24 ou plus récent.

Notez la date de chaque exercice réussi. Profitez-en pour vérifier que la clé privée age et
la clé maître sont toujours là où vous les avez rangées.

## Depuis les sources

Sans `backup_dir`, le worker ne fait pas de sauvegarde automatique : renseignez-le pour en
avoir une.

```sh
uv run synaptane backup --output /var/backups/synaptane/
uv run synaptane backup setup            # clé, destination, première copie
uv run synaptane backup --offsite        # copie vers les destinations en place
# Arrêtez synaptane serve et synaptane worker, puis :
uv run synaptane restore /var/backups/synaptane/synaptane-2026-10-03-040000.tar.gz
```

Il faut `pg_dump` et `pg_restore` de la même version majeure que le serveur PostgreSQL, ou
d'une version plus récente. Dans `synaptane.toml` :

```toml
backup_dir = "/var/backups/synaptane"   # sauvegarde automatique chaque nuit (aucune sans lui)
backup_time = "04:00"                   # heure du serveur ; "off" : aucune
backup_local_retention_days = 7         # 0 : toutes gardées
backup_age_recipients = ["age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"]
backup_remotes = ["b2:synaptane-sauvegardes", "nas:sauvegardes/synaptane"]
backup_retention_days = 30
# backup_rclone_config = "/etc/synaptane/rclone.conf"   # défaut : $RCLONE_CONFIG ou <data_dir>/rclone/rclone.conf
```
