[English](backup.md) | Français

# Sauvegarde et restauration

Trois niveaux, à mettre en place dans l'ordre :

1. **Une archive locale vérifiée chaque nuit** (`synaptane backup`) : elle protège d'une
   erreur de manipulation ou d'une mise à jour ratée.
2. **Une copie chiffrée hors du serveur** (`synaptane backup --offsite`), vers une ou
   plusieurs destinations au choix : S3 et compatibles, SFTP, WebDAV/Nextcloud, NAS SMB,
   Google Drive, OneDrive, Dropbox… Elle protège d'une panne du disque, d'un vol ou d'un
   serveur perdu.
3. **Un exercice de restauration chaque mois** (`scripts/restore-drill.sh`), sur votre poste.
   Une sauvegarde qu'on n'a jamais restaurée n'est qu'un espoir.

## Ce qui est sauvegardé

`synaptane backup` produit une archive `.tar.gz` qui contient :

| Élément | Pourquoi |
|---|---|
| La base PostgreSQL (`pg_dump`) | Comptes, organisations, espaces, droits, jetons, réglages, secrets chiffrés, historique des rangements, index sémantique. |
| La **clé maître** | Sans elle, les secrets chiffrés de la base sont perdus : identifiants Git, clés IA, secrets SSO et webhooks. |
| Les copies locales des dépôts | Les contenus sont déjà sur les dépôts distants. Les copies gardent ce qui n'a pas encore pu être poussé, par exemple en cas de conflit ou de panne de la forge. |
| Un manifeste | Version de Synaptane, révision de la base, date, nombre de comptes, d'organisations et d'espaces. |

Ne sont **pas** sauvegardés :
- le texte extrait des PDF et des images, qui se reconstruit depuis Git ;
- les connexions des CLI Claude Code et Codex, à refaire après une restauration ;
- la configuration rclone des destinations (`/data/rclone/rclone.conf`) : gardez une copie
  de ses identifiants dans votre gestionnaire de mots de passe.

L'archive contient la clé maître en clair : **elle donne accès à tous les secrets**. C'est
pourquoi la copie hors serveur est toujours chiffrée, et refusée sans clé de chiffrement.
Avec `--without-key`, la clé reste hors de l'archive ; il faut alors la sauvegarder à part,
une fois pour toutes, car elle ne change pas.

## Sauvegarde locale

Chaque archive est **relue avant d'être annoncée** : compression intègre (comme `gzip -t`),
tar complet, manifeste et clé présents, et dump entièrement lisible par `pg_restore`. Une
archive tronquée (disque plein, interruption) fait échouer la commande le jour même.

```sh
umask 077
docker compose exec -T app synaptane backup --output - > synaptane-$(date +%F).tar.gz
```

Avec `--output -`, rien n'est écrit sur la sortie standard si l'archive est mauvaise.

Options :
- `--output` : un fichier, un dossier, ou `-` pour la sortie standard. Sans `--output`,
  l'archive va dans `$SYNAPTANE_BACKUP_DIR` (`/backups` avec Docker), sinon dans le dossier
  courant ;
- `--without-repos` : sans les copies des dépôts ;
- `--without-key` : sans la clé maître ;
- `--offsite` : copie chiffrée hors serveur, voir plus bas.

Le volume `synaptane-backups` (`/backups`) n'a pas de rotation, il est sur le même disque
que les données, et **`docker compose down -v` l'efface** avec tous les autres volumes : ce
n'est pas un lieu de conservation. Préférez la sortie standard vers un dossier du serveur.
Pour en sortir une archive :

```sh
docker compose exec app ls -l /backups
docker compose cp app:/backups/synaptane-2026-10-03-030000.tar.gz .
```

## Copie hors serveur

### Principe

```text
serveur : synaptane backup --offsite
  archive vérifiée ──age──▶ synaptane-AAAA-MM-JJ-HHMMSS.tar.gz.age ──rclone──▶ destination 1
                    (clé publique)                                       ├──▶ destination 2
                                                                         └──▶ …
votre poste : clé privée age ──▶ déchiffre ──▶ restaure
```

- **Chiffrement par [age](https://age-encryption.org)** vers une ou plusieurs clés
  publiques. Le serveur chiffre mais **ne peut pas déchiffrer** : un vol du serveur ou de la
  destination ne donne rien. La clé privée reste chez vous.
- **Copie par [rclone](https://rclone.org/overview/)**, qui connaît plus de 70 types de
  stockage. Les identifiants des destinations restent dans la configuration rclone, jamais
  dans la base ni en ligne de commande.
- **Plusieurs destinations** : chacune reçoit la copie. Une destination en panne n'empêche
  pas les autres ; le message dit laquelle a échoué, et le code de sortie le signale.
- **Rétention** : après une copie réussie, les archives `synaptane-*.age` plus anciennes
  que `BACKUP_RETENTION_DAYS` (30 jours par défaut) sont effacées de **cette** destination,
  et seulement à sa racine. Rien d'autre n'est touché, même dans un dossier partagé. Une
  destination qui n'a pas reçu la copie du jour garde toutes ses anciennes copies.

`age` et `rclone` sont dans l'image Docker. Depuis les sources, installez les paquets `age`
et `rclone`.

### 1. Créer la paire de clés age, sur votre poste

Pas sur le serveur : la clé privée ne doit jamais s'y trouver.

```sh
age-keygen -o synaptane-backup.key
# Public key: age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq
```

Rangez `synaptane-backup.key` **avec la clé maître**, une fois pour toutes, à deux endroits :
votre gestionnaire de mots de passe et une copie papier ou une clé USB dans un tiroir. Sans
elle, les copies hors serveur sont illisibles, y compris pour vous.

Plusieurs personnes peuvent restaurer : chacune crée sa paire, et toutes les clés publiques
sont indiquées (n'importe laquelle des clés privées ouvre l'archive). Une clé SSH publique
(`ssh-ed25519 …`) convient aussi.

### 2. Déclarer la clé publique

Dans `.env`, sur le serveur :

```sh
BACKUP_AGE_RECIPIENTS=age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq
```

Plusieurs clés : séparées par des virgules. Sans clé, `backup --offsite` **refuse** de
copier, avec un message clair.

### 3. Déclarer les destinations

L'assistant de rclone liste tous les types de stockage et pose les questions une à une :

```sh
docker compose exec -it app rclone config
```

La configuration est écrite dans `/data/rclone/rclone.conf` (volume `synaptane-data`,
droits 600). Ensuite, dans `.env`, la liste des destinations, sous la forme
`nom:chemin`, séparées par des virgules :

```sh
BACKUP_REMOTES=b2:synaptane-sauvegardes,nas:sauvegardes/synaptane
BACKUP_RETENTION_DAYS=30
```

Puis `docker compose up -d` pour que l'application lise ces réglages.

Pour chaque destination, créez un accès **réservé à cet usage** : un seau ou un dossier
dédié, un compte ou une clé d'API limités à ce seau. Ainsi, une fuite de la configuration
rclone n'expose rien d'autre.

Exemples de ce que l'assistant produit dans `rclone.conf` (valeurs à remplacer) :

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

### 4. Tester

```sh
docker compose exec app synaptane offsite test
# [ok] b2:synaptane-sauvegardes
# [ÉCHEC] nas:sauvegardes/synaptane — écriture : … connection refused
```

Pour chaque destination, un petit fichier témoin est écrit, relu puis effacé : les droits
d'écriture, de lecture et d'effacement (dont la rétention a besoin) sont vérifiés d'un
coup. La commande signale aussi une clé age manquante. `synaptane doctor` résume l'état
de la copie hors serveur, toujours en « attention », jamais en échec.

Première copie à la main, puis vérification :

```sh
docker compose exec app synaptane backup --offsite
docker compose exec app synaptane offsite list
```

Sans `--output`, `--offsite` ne garde **aucune** copie locale : l'archive en clair est
effacée du serveur une fois chiffrée.

### 5. Tous les jours, avec cron

Le script versionné `deploy/host/backup.sh` garde les archives locales vérifiées (14 jours
par défaut) et, avec `OFFSITE=1`, envoie la copie chiffrée hors serveur dans la même
sauvegarde. Il écrit d'abord dans un fichier `.part`, renommé seulement si l'archive est
bonne : une archive incomplète ne remplace jamais une bonne. Une destination hors serveur en
échec garde l'archive locale, se note au journal (`~/synaptane-backups/journal.log`) et rend
le code 3.

```sh
# crontab -e
0 3 * * * OFFSITE=1 $HOME/synaptane/deploy/host/backup.sh > /dev/null 2>> $HOME/synaptane-backups/cron.err
```

Réglages : `BACKUP_DIR`, `RETENTION_DAYS` (voir l'en-tête du script, et les
[scripts du serveur](../../deploy/host/README.fr.md)). Cron envoie par e-mail
ce que le script écrit sur l'erreur standard, si l'envoi de courrier est configuré sur le
serveur.

Codes de sortie de `synaptane backup` :

| Code | Signification |
|---|---|
| 0 | Archive vérifiée, et copiée vers toutes les destinations (avec `--offsite`). |
| 1 | Pas de sauvegarde utilisable, ou copie hors serveur refusée (réglage manquant) : rien n'a été écrit. |
| 3 | Archive vérifiée (et écrite si `--output`), mais une destination au moins a échoué (copie ou rétention). Le message dit laquelle. |

Pour être prévenu quand la sauvegarde ne tourne plus du tout (cron arrêté, serveur
éteint), ajoutez à la fin du script un appel à un moniteur de type « dead man's switch »
(Healthchecks.io, Uptime Kuma…) quand le code vaut 0.

## Restaurer

### Depuis une archive en clair

Par exemple sur un nouveau serveur :

```sh
docker compose up -d                  # instance neuve : base sans compte et nouvelle clé
docker compose stop app worker
docker compose run --rm -T app synaptane restore - < synaptane-2026-10-03.tar.gz
docker compose start app worker
```

### Depuis une copie hors serveur

On déchiffre **sur votre poste**, et l'archive déchiffrée part directement dans la
restauration : la clé privée ne touche jamais le serveur.

```sh
# Sur votre poste, avec rclone configuré pour lire la destination :
rclone lsf b2:synaptane-sauvegardes                     # choisir l'archive
rclone copyto b2:synaptane-sauvegardes/synaptane-2026-10-06-030000.tar.gz.age .
# Sur le serveur : docker compose up -d && docker compose stop app worker
age -d -i synaptane-backup.key synaptane-2026-10-06-030000.tar.gz.age \
  | ssh serveur 'cd ~/synaptane && docker compose run --rm -T app synaptane restore -'
# Sur le serveur : docker compose start app worker
```

Sans rclone sur votre poste, téléchargez le fichier depuis l'interface du stockage (console
S3, Nextcloud, Google Drive…).

### Ce que fait la restauration

- elle remplace la base, la clé maître et les copies des dépôts. Une archive pouvant venir
  d'ailleurs, la configuration Git de chaque copie est réduite à l'essentiel (adresse du
  distant, branche) et ses hooks sont supprimés : rien ne peut s'exécuter à la reprise ;
- elle met la base au niveau de la version installée ;
- elle demande au worker de resynchroniser et de réindexer tous les espaces. Les pages
  écrites avant la panne et pas encore poussées partent alors sur les dépôts.

Elle s'arrête sans rien toucher dans ces cas :
- **l'application ou le worker sont encore connectés** à la base ;
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
place.

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

Il finit par « Exercice réussi. » ou s'arrête en erreur. L'image utilisée est
`$SYNAPTANE_IMAGE` (sinon `synaptane:latest`, construite depuis le dépôt si elle manque) :
elle doit être au moins aussi récente que la version qui a fait la sauvegarde. Il faut
Docker Compose 2.24 ou plus récent.

Notez la date de chaque exercice réussi. Profitez-en pour vérifier que la clé privée age et
la clé maître sont toujours là où vous les avez rangées.

## Depuis les sources

```sh
uv run synaptane backup --output /var/backups/synaptane/
uv run synaptane backup --offsite        # avec backup_age_recipients et backup_remotes
# Arrêtez synaptane serve et synaptane worker, puis :
uv run synaptane restore /var/backups/synaptane/synaptane-2026-10-03-030000.tar.gz
```

Il faut `pg_dump` et `pg_restore` de la même version majeure que le serveur PostgreSQL, ou
d'une version plus récente. Dans `synaptane.toml` :

```toml
backup_age_recipients = ["age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"]
backup_remotes = ["b2:synaptane-sauvegardes", "nas:sauvegardes/synaptane"]
backup_retention_days = 30
# backup_rclone_config = "/etc/synaptane/rclone.conf"   # défaut : $RCLONE_CONFIG ou <data_dir>/rclone/rclone.conf
```
