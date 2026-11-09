[English](configuration.md) | Français

# Configuration

La configuration et l'administration d'une instance installée (voir
[Installer et exploiter Synaptane](installation.fr.md)).

`init` écrit `synaptane.toml` (droits 600) et génère la **clé maître** `master.key` dans le
dossier de données. Avec Docker, la configuration passe par les variables d'environnement
et la clé est générée au premier démarrage. Cette clé chiffre les secrets stockés en base,
comme les identifiants Git et les clés IA. **Sauvegardez-la hors de la base** : sans elle,
ces secrets sont perdus. Voir [Sauvegarde et restauration](backup.fr.md).

Chaque option de `synaptane.toml` peut être remplacée par une variable d'environnement
`SYNAPTANE_<OPTION>`, par exemple `SYNAPTANE_DATABASE_URL`. Le fichier se choisit avec
`--config` ou `SYNAPTANE_CONFIG`.

| Option | Rôle |
|---|---|
| `public_url` | URL publique. En `https://`, les cookies sont marqués `Secure`. C'est aussi l'émetteur OAuth et l'adresse du MCP (`<public_url>/mcp`) : pour Claude.ai, elle doit être en HTTPS et joignable depuis Internet. |
| `database_url` | Connexion PostgreSQL (`postgresql+psycopg://…`). |
| `data_dir` | Clé maître et copies locales des dépôts. |
| `default_locale` | Langue par défaut de l'interface : `en` (par défaut) ou `fr`. Elle sert quand le navigateur ne demande ni l'une ni l'autre, et pour les comptes créés par un administrateur ou la CLI ; chacun change la sienne dans **Mon compte**. Variable : `SYNAPTANE_DEFAULT_LOCALE`. |
| `session_days` | Durée d'une session web, 30 jours par défaut. |
| `superadmin_2fa` | Les super-admins qui se connectent par mot de passe doivent activer la double authentification (une application d'authentification) avant toute chose : `true` par défaut. L'authentification unique laisse le second facteur au fournisseur d'identité. `false` seulement pour une instance de démonstration ou de test (`examples/compose.demo.yaml` le règle). |
| `session_idle_days` | Une session web inutilisée pendant ce nombre de jours se termine : 7 par défaut, `0` pour aucune limite. Chacun voit ses sessions dans **Mon compte → Sécurité** et peut les fermer. |
| `poll_minutes` | Fréquence de la synchronisation de secours, 5 minutes par défaut. |
| `git_timeout` | Délai global d'un échange avec la forge (clone, récupération, envoi), en secondes, 300 par défaut. À augmenter seulement pour le premier clone d'un très gros dépôt. Voir [Git](git.fr.md). |
| `allow_local_repos` | Autorise les dépôts `file://`. **Développement uniquement.** |
| `ocr_languages` | Langues Tesseract, `fra+eng` par défaut. |
| `ocr_images` | Extraire le texte des images (OCR), `true` par défaut. |
| `allow_private_webhooks` | Autorise les webhooks en HTTP et vers le réseau local. **Développement uniquement.** |
| `allow_insecure_imap` | Autorise la [capture par e-mail](email-capture.fr.md) à relever sa boîte par une connexion IMAP non chiffrée. **Développement uniquement** (un serveur de test local). |
| `api_docs` | Publie la documentation interactive `/api/docs` et le schéma `/api/v1/openapi.json`, `false` par défaut. |
| `metrics_token` | Jeton porteur de `/metrics` pour Prometheus, 32 caractères au moins (voir [Supervision](#supervision)). Vide par défaut : `/metrics` n'existe pas. Avec Docker : `METRICS_TOKEN` dans `.env`. |
| `backup_dir` | Dossier de la sauvegarde de la nuit faite par le worker (`/backups` dans l'image, `SYNAPTANE_BACKUP_DIR`). Absent : pas de sauvegarde automatique. Voir [Sauvegarde](backup.fr.md). |
| `backup_time` | Heure de la sauvegarde de la nuit, heure du serveur : `04:00` par défaut (`4:00`, `04h00` sont lus aussi) ; `off` : aucune. Une valeur qui n'est pas une heure donne 04:00, avec un avertissement. |
| `backup_local_retention_days` | Jours de conservation des archives du worker dans `backup_dir`, 7 par défaut (les 3 plus récentes toujours) ; `0` : toutes. |
| `backup_timeout_minutes` | Durée maximale d'un dump de la base, ou de sa relecture : 120 minutes par défaut. |
| `backup_age_recipients` | Clés publiques age (`age1…`) pour lesquelles la copie hors serveur est chiffrée, en plus de celles enregistrées par `backup setup`. En variable d'environnement : séparées par des virgules. |
| `backup_age_recipients_file` | Ou un fichier de destinataires age. |
| `backup_remotes` | Destinations rclone de la copie hors serveur (`nom:chemin`, ou un dossier), en plus de celles enregistrées par `backup setup`. |
| `backup_retention_days` | Archives distantes plus anciennes effacées, 30 jours par défaut ; `0` : jamais. |
| `backup_rclone_config` | Configuration rclone. Par défaut `$RCLONE_CONFIG` (`/data/rclone/rclone.conf` avec Docker), sinon `<data_dir>/rclone/rclone.conf`. |

Avec Docker Compose, ces variables se règlent dans `.env` (voir les commentaires de
`.env.example`) :

| Variable | Rôle |
|---|---|
| `PUBLIC_URL`, `POSTGRES_PASSWORD` | Adresse publique et mot de passe de la base (obligatoires). |
| `COMPOSE_PROFILES` | Vide (derrière le nginx du serveur) ou `proxy` (tout-en-un). |
| `COMPOSE_FILE` | Fichiers Compose, séparés par `:`. Non défini : `compose.yaml`, plus `compose.override.yaml` s'il existe. |
| `SYNAPTANE_VERSION` | Étiquette des images publiées, la version de `compose.yaml` par défaut. |
| `SYNAPTANE_IMAGE`, `SYNAPTANE_NGINX_IMAGE` | Une autre image pour Synaptane, ou pour le nginx du mode proxy (`synaptane:local`, `synaptane-nginx:local` pour une construction). |
| `GIT_SHA`, `AGENT_CLIS` | Construction seulement (`compose.build.yaml`) : commit inscrit dans l'image, CLI d'abonnement (`true` par défaut). |
| `APP_PORT`, `APP_BIND`, `FORWARDED_ALLOW_IPS` | Mode par défaut : port local (8000), son adresse (`127.0.0.1`), réseaux des proxys de confiance. |
| `DOMAIN`, `LETSENCRYPT_EMAIL`, `LETSENCRYPT_STAGING`, `HTTP_PORT`, `HTTPS_PORT` | Mode proxy : domaine, compte Let's Encrypt et certificats de test, ports publiés (80 et 443). |
| `BACKUP_TIME`, `BACKUP_LOCAL_RETENTION_DAYS`, `BACKUP_TIMEOUT_MINUTES` | Sauvegarde de la nuit : heure (`04:00`, `off` : aucune), jours gardés (7), durée maximale d'un dump (120 minutes). Voir [Sauvegarde](backup.fr.md). |
| `BACKUP_AGE_RECIPIENTS`, `BACKUP_REMOTES`, `BACKUP_RETENTION_DAYS` | Copie hors serveur, sans `backup setup` (voir [Sauvegarde](backup.fr.md)). |
| `SYNAPTANE_DEFAULT_LOCALE`, `OCR_LANGUAGES`, `TZ` | Langue de l'interface, langues de l'OCR, fuseau horaire. |
| `SYNAPTANE_WORKER_MEMORY`, `SYNAPTANE_LOG_LEVEL` | Mémoire maximale du worker (`2g`), niveau des journaux. |

## Envoi d'e-mails, invitations et mot de passe oublié

**Administration → E-mail** : serveur SMTP, port, sécurité (STARTTLS ou SSL), identifiant,
mot de passe (chiffré, jamais réaffiché) et adresse d'expédition. « Envoyer un e-mail de
test » vérifie le réglage, vers l'adresse de votre compte.

Avec l'envoi d'e-mails :
- un admin d'organisation **invite** depuis l'onglet **Membres**. Il choisit l'adresse, le
  rôle dans l'organisation et, au choix, un rôle par espace et des **groupes** à rejoindre
  (voir [Rôles](#rôles) plus bas). La personne reçoit un lien valable 14 jours et crée son
  compte. Si l'adresse est celle, **vérifiée**, d'un compte existant, les accès sont donnés
  aussitôt ;
- **« Mot de passe oublié ? »** sur la page de connexion envoie un lien valable 30 minutes.

Sans envoi d'e-mails, les invitations marchent aussi : le lien est montré une fois à la
personne qui invite, qui le transmet. Le mot de passe oublié passe alors par un super-admin
(Administration → Comptes).

Un mot de passe choisi par un admin, à la création du compte ou par réinitialisation, doit
être changé à la première connexion. On se connecte avec son nom d'utilisateur ou son
e-mail.

**Adresses vérifiées.** Une adresse saisie dans « Mon compte » ou par un admin reste « à
vérifier » tant qu'elle n'est pas confirmée. Le lien envoyé à cette adresse (valable 3 jours)
doit être ouvert en étant connecté au compte qui l'a demandé, puis confirmé par un bouton : un
lien ouvert par quelqu'un d'autre, ou par l'antivirus d'une messagerie, ne vérifie rien. Seule
une adresse vérifiée sert aux invitations vers un compte existant, à la liaison SSO et au mot
de passe oublié. Changer son e-mail demande le mot de passe actuel, et l'ancienne adresse est
prévenue. Une adresse déclarée mais jamais vérifiée ne bloque pas son vrai titulaire : elle
lui revient quand il l'utilise (invitation, SSO).

**Limites d'envoi.** Chacun de ces e-mails part vers une adresse saisie par quelqu'un qui n'en
est pas forcément le titulaire, d'où un plafond : 3 liens de vérification par compte et par
heure, et 30 invitations par organisation et par heure (comptées que l'e-mail parte ou non). Au-delà, la
demande est refusée (HTTP 429) jusqu'à ce que l'heure soit passée.

**Connexions.** Après 10 échecs en 15 minutes depuis une même adresse IP, ce compte est
bloqué pour cette adresse ; après 50 échecs toutes adresses confondues, pour tous. Les
variantes d'écriture d'un identifiant de connexion (espaces, majuscules, e-mail) comptent pour
le même compte. Le journal désigne le compte par son nom d'utilisateur ; un identifiant qui ne
correspond à aucun compte y figure sous forme d'empreinte courte (`login:` suivi de 8
caractères hexadécimaux), jamais tel que saisi, car ce peut être un mot de passe tapé dans le
mauvais champ.

**Capture par e-mail.** **Administration → E-mail → Capture par e-mail** change les e-mails
envoyés à `brain+organisation.espace@example.com` en notes de l'inbox, relevés par le worker
dans une boîte IMAP dédiée : voir [Capture par e-mail](email-capture.fr.md).

## Index de recherche

Le texte extrait des PDF, des documents Office et des images est rangé hors des dépôts, dans
`data_dir/extract/`.

Chaque document est lu par un processus à part, borné, pour qu'un fichier piégé ou
démesuré (plan grand format, scan de centaines de pages) ne fasse pas tomber le worker :
- 1 Go de mémoire au plus (Tesseract et catdoc compris) et 10 minutes par document ;
- pages rendues pour l'OCR plafonnées à 40 millions de pixels (300 dpi pour un format
  courant, moins pour un grand format) ;
- 500 pages lues, dont 100 au plus passées à l'OCR, 60 secondes par page ; au-delà, le
  texte natif reste indexé et le journal du worker le signale ;
- images de plus de 250 millions de pixels ignorées.

Un fichier qui échoue (délai, mémoire, fichier invalide) est inscrit comme tel dans
`data_dir/extract/<espace>/manifest.json`, avec la raison, et n'est pas retenté tant qu'il
ne change pas : il est inscrit avant même d'être lu, si bien qu'un arrêt du worker au milieu
ne se répète pas indéfiniment (deux essais au plus). `synaptane reindex` retente tout,
échecs compris.

L'index se reconstruit à tout moment depuis Git :

```sh
uv run synaptane reindex                  # tous les espaces
uv run synaptane reindex --space acme/infra
docker compose exec app synaptane reindex   # avec Docker
```

## Alertes

Les administrateurs d'une organisation sont prévenus, par e-mail si l'envoi est configuré et
par le webhook `space.alert`, de ce qui demande leur attention, par exemple quand :
- le rangement d'un espace échoue durablement (pas une simple coupure réseau), ou s'arrête
  avant la fin ;
- un espace ne se synchronise plus avec son dépôt (identifiants refusés, dépôt introuvable) ;
- une inbox garde des éléments jamais examinés depuis plus de 3 jours ;
- l'agent de rangement atteint le plafond de suppressions, ou fait de gros changements ;
- le jeton d'accès Git d'un espace expire dans 30 jours, 7 jours, demain, ou a expiré (voir
  [Expiration des jetons](git.fr.md#expiration-des-jetons)).

La liste complète des types d'alerte est dans [Webhooks](webhooks.fr.md). Une même alerte
(type et espace) ne repart pas avant 20 heures ; l'expiration d'un jeton est signalée une fois
par palier. Le budget d'IA a ses propres alertes (voir
[Intelligence artificielle](ai.fr.md)).

Les super-administrateurs sont prévenus, par e-mail si l'envoi est configuré, quand la
sauvegarde de la nuit ou sa copie hors serveur échoue (voir [Sauvegarde](backup.fr.md)) ;
l'alerte s'affiche aussi dans **Administration > Santé**.

## Supervision

**Administration > Santé** (`/admin/health`, super-administrateurs) montre l'instance sur une
page : base de données, clé maîtresse, worker, tâches de fond, dernière sauvegarde, copie hors
site, disque, envoi d'e-mails, HTTPS et les jetons d'accès Git qui expirent d'ici 30 jours ou ont
expiré (avec un lien vers la page Git de chaque espace), puis la synchronisation et le rangement
de chaque espace, puis les alertes système des 30 derniers jours. Une sauvegarde compte une fois écrite et vérifiée,
où qu'elle soit allée ; une copie hors site, une fois que toutes les destinations l'ont. Les
copies faites avant la 1.11.0 ne sont pas vues.

`/healthz` répond dès que le processus tourne ; `/readyz` vérifie aussi la base de données (le
contrôle de santé de Docker s'en sert). Aucun des deux ne demande de connexion.

Pour Prometheus, ou tout outil qui lit son format (Grafana Alloy, VictoriaMetrics…),
`/metrics` donne l'état de l'instance. Il reste fermé tant que vous n'avez pas défini un jeton
de 32 caractères ou plus, puis redémarré :

```sh
openssl rand -hex 32   # le jeton
```

Avec Docker, mettez-le dans `.env` (`METRICS_TOKEN=…`), puis `docker compose up -d` ; depuis
les sources, `metrics_token` dans `synaptane.toml`. Le collecteur l'envoie comme jeton
porteur :

```yaml
scrape_configs:
  - job_name: synaptane
    scheme: https
    authorization:
      credentials_file: /etc/prometheus/synaptane.token
    static_configs:
      - targets: ["synaptane.example.com"]
```

`/metrics` passe par nginx comme le reste de l'application : c'est le jeton qui le protège,
gardez-le comme un mot de passe. Les espaces y figurent sous la forme `organisation/espace` ;
aucun contenu de page, aucune personne.

| Métrique | Ce qu'elle mesure |
|---|---|
| `synaptane_info{version,commit}` | Version et commit en service (toujours 1). |
| `synaptane_database_up` | 1 si la base de données répond. À 0, les métriques qui en viennent sont absentes. |
| `synaptane_database_connections_in_use` | Connexions à la base du serveur web en cours d'utilisation (20 au plus). |
| `synaptane_http_requests_total{area,code}` | Requêtes servies depuis le démarrage, par partie (`web`, `api`, `mcp`, `oauth`, `static`) et classe de statut (`2xx`, `4xx`…). |
| `synaptane_http_request_duration_seconds{area}` | Temps de réponse (histogramme). |
| `synaptane_users{state}` | Comptes, actifs (`active`) ou désactivés (`disabled`). |
| `synaptane_space_sync_status{space,status}` | 1 pour l'état de synchronisation actuel de chaque espace : `ok`, `error`, `conflict` ou `never`. |
| `synaptane_space_last_sync_timestamp_seconds{space}` | Dernière synchronisation réussie. |
| `synaptane_space_inbox_items{space}` | Éléments qui attendent le rangement dans l'inbox. |
| `synaptane_space_last_tidy_timestamp_seconds{space}` | Fin du dernier passage du rangement. |
| `synaptane_space_last_tidy_failed{space}` | 1 si ce passage a échoué. |
| `synaptane_jobs{kind,status}` | Tâches du worker en attente (`queued`) ou en cours (`running`). |
| `synaptane_jobs_waiting_seconds` | Attente de la plus ancienne tâche à lancer : elle grandit quand le worker est arrêté ou débordé. |
| `synaptane_jobs_abandoned_24h{kind}` | Tâches abandonnées dans les dernières 24 heures, essais épuisés. |
| `synaptane_ai_spend_month_euros{organization}` | Dépense d'IA estimée depuis le 1er du mois (UTC). |
| `synaptane_backup_last_timestamp_seconds` | Dernière archive vérifiée de `synaptane backup`, où qu'elle ait été écrite. |
| `process_*` | CPU, mémoire résidente, fichiers ouverts et heure de démarrage du serveur web. |

Exemples de règles d'alerte :

```yaml
groups:
  - name: synaptane
    rules:
      - alert: SynaptaneWorkerStuck
        expr: synaptane_jobs_waiting_seconds > 900
        for: 5m
      - alert: SynaptaneSyncError
        expr: synaptane_space_sync_status{status=~"error|conflict"} == 1
        for: 30m
      - alert: SynaptaneNoRecentBackup
        expr: time() - synaptane_backup_last_timestamp_seconds > 2 * 86400
```

## Journal d'audit

Qui a changé un rôle, un jeton ou un réglage, et quand. Synaptane note ces changements au fil de
l'eau ; deux pages les lisent, du plus récent au plus ancien, 50 par page (**Plus ancien** pour
la suite) :

- onglet **Journal** d'une organisation (`/o/<organisation>/-/journal`, ses administrateurs) :
  ses membres et rôles, les invitations, les réglages (organisation, IA, recherche sémantique,
  webhooks, espaces, accès Git, rangement, vocation, prompts d'assistant, routines) et les alertes envoyées à ses
  administrateurs. Rien d'une autre organisation ;
- **Administration > Journal** (`/admin/journal`, super-administrateurs) : toute l'instance,
  avec ce qui ne relève d'aucune organisation : les comptes (création, droits de super-admin,
  désactivation, mot de passe, double authentification, sessions), les jetons personnels, les
  applications connectées, la connexion SSO et l'envoi d'e-mails. Un filtre par organisation
  en garde une seule, ou ce qui ne relève d'aucune.

Chaque ligne donne la date, l'auteur (**Système** pour ce que Synaptane fait de lui-même, comme
une alerte), l'action, ce qu'elle concerne (espace, membre, jeton…) et ses détails utiles : rôle,
fournisseur et modèle, « Clé d'API remplacée ». Jamais de secret : aucune clé, aucun jeton, aucun
mot de passe, et d'un webhook seulement son hôte. Filtrez par type (membres et rôles, jetons et
applications, paramètres, comptes et sécurité, alertes) et par période (dates en UTC) ;
**Exporter (CSV)** télécharge la sélection, ses 10 000 lignes les plus récentes au plus (une
dernière ligne le dit quand il s'est arrêté là).

Les modifications de pages, les notes de l'inbox, les passages du rangement et les coûts d'IA
n'y sont pas : ils ont leurs propres pages (**Activité** et **Rangement** de chaque espace,
**Intelligence artificielle** de l'organisation). Les événements d'un espace supprimé restent
dans le journal de son organisation, sans son nom. Les changements notés avant cette page y sont
aussi, avec moins de détails (qui est devenu super-admin, quelle clé a changé).

## Rôles

| Rôle | Portée | Peut |
|---|---|---|
| Super-admin | Instance | Tout : comptes, toutes les organisations, et lecture et écriture de tous les contenus dans le web et l'API REST. Par un assistant (MCP), seulement ses appartenances réelles. |
| Admin d'organisation | Organisation | Créer et régler les espaces, gérer les membres. Contributeur sur tous les espaces de l'organisation. |
| Contributeur | Espace | Lire, chercher, déposer dans l'inbox, modifier le contenu. |
| Lecteur | Espace | Lire et chercher. |

Ajouter quelqu'un à un espace en fait automatiquement un membre simple de l'organisation.
Le retirer de l'organisation lui retire aussi ses espaces et ses groupes.

Par défaut, un membre n'accède qu'aux espaces où il est ajouté. Pour donner d'un coup à
chaque membre un rôle sur tous les espaces de l'organisation, choisissez un **rôle par défaut
dans les espaces** (lecteur ou contributeur) dans les **Paramètres** de l'organisation :
chaque membre a alors le plus élevé entre ce rôle et celui donné dans un espace. Marquez un
espace **restreint** (**Paramètres → Membres** de l'espace, ou dès sa création dans **Nouvel
espace**) pour le réserver à ses propres membres, pour les ressources humaines ou la
direction ; la page des membres d'un espace montre qui y accède par défaut et qui y a été
ajouté.

**Avec un rôle par défaut, ajouter quelqu'un à un espace lui ouvre les autres.** Une personne
qui n'est pas encore membre de l'organisation le devient quand vous l'ajoutez à un espace (ou
l'invitez) : elle reçoit alors aussi le rôle par défaut sur chaque espace non restreint de
l'organisation, et pas seulement sur l'espace choisi. Le formulaire d'ajout d'un espace et sa
confirmation le disent. Pour ne donner accès qu'à un espace, gardez les autres restreints, ou
laissez le rôle par défaut à « aucun accès ». Retirer quelqu'un d'un espace non restreint lui
laisse le rôle par défaut : la confirmation le dit aussi. Dans le formulaire d'invitation, un
espace non restreint propose le rôle par défaut comme plancher (« Lecteur (par défaut) ») et
seulement les rôles au-dessus ; la page d'invitation liste l'accès réellement donné, rôle par
défaut compris.

**Groupes : une équipe, plusieurs espaces.** Pour donner d'un coup à certains membres l'accès
à plusieurs espaces, au-delà du rôle par défaut, créez un **groupe** dans l'onglet **Groupes**
de l'organisation (ressources humaines, direction, équipe projet) : ajoutez ses membres
(membres de l'organisation seulement : invitez les autres en cochant le groupe dans le
formulaire d'invitation), puis donnez-lui un rôle, lecteur ou contributeur, sur chaque espace
qu'il ouvre. Chaque membre du groupe a alors le plus
élevé de ce rôle, de celui qui lui est donné dans l'espace et du rôle par défaut. **Un groupe
ouvre aussi un espace restreint** : restreignez l'espace des ressources humaines, puis donnez
l'accès au seul groupe « RH », et personne d'autre dans l'organisation ne le voit. Vous pouvez
aussi donner un rôle à un groupe depuis **Paramètres → Membres** d'un espace, section **Par un
groupe**. Sur un espace non restreint, un rôle de groupe inférieur ou égal au rôle par défaut
n'ajoute rien : la confirmation le signale. La page des membres d'un espace liste les groupes
qui y accèdent et dit, pour chaque personne, si son accès vient de l'espace lui-même, d'un
groupe (lequel), du rôle par défaut ou de l'administration de l'organisation. L'onglet
**Membres** de l'organisation montre les groupes de chaque membre ; retirer quelqu'un de
l'organisation le retire de ses groupes (la confirmation dit lesquels). Supprimer un groupe
retire l'accès qu'il donnait, sauf si ses membres l'ont autrement ; supprimer un espace retire
les rôles des groupes sur lui. Les groupes se gèrent aussi par l'API REST
(`/organizations/{org}/groups`), et chaque changement est inscrit au journal d'audit.

**Inviter quelqu'un directement dans des groupes.** Le formulaire d'invitation de l'onglet
**Membres** liste les groupes de l'organisation, chacun avec ce qu'il donne (« Contributeur sur
RH ») : cochez ceux que la personne rejoint. Quand elle accepte, elle devient membre de
l'organisation, puis des groupes cochés, d'un seul coup ; un compte existant les rejoint
aussitôt, et l'e-mail qui le prévient liste les rôles donnés dans les espaces et les groupes
rejoints. Avant de créer son compte, la personne voit son accès réel sur la page
d'invitation, groupes compris (par exemple RH : contributeur, par le groupe « Direction »), et la liste des
**Invitations en attente** montre les groupes de chaque invitation. Un groupe supprimé avant
l'acceptation n'est tout simplement pas rejoint, et un groupe renommé est rejoint sous son
nouveau nom. Annuler une invitation abandonne ses groupes ; inviter de nouveau la même adresse
les remplace par le nouveau choix. Seuls les groupes de l'organisation peuvent être choisis :
la base de données refuse tout autre, de même qu'un groupe supprimé pendant l'envoi de
l'invitation. Le journal d'audit garde les groupes, sous leurs noms d'alors, dans l'événement
**Invitation envoyée** et dans l'événement **Compte existant ajouté par invitation** (là,
seulement les groupes réellement rejoints, pas ceux dont le compte était déjà membre) ;
l'acceptation inscrit un événement **Ajout à un groupe** par groupe, par le système, comme les
autres accès qu'elle donne. Par l'API REST : `"groups": [<id>, …]` (200 au plus) dans
`POST /organizations/{org}/invitations`.

**Étape suivante :** les [premiers pas](first-steps.fr.md) : organisations, espaces,
membres, données de démonstration.
