[English](git.md) | Français

# Relier un espace à son dépôt Git

Chaque espace correspond à **une branche d'un dépôt Git** : GitHub, GitLab, Gitea ou tout
serveur Git. Ce dépôt fait foi : Synaptane y commite et y pousse ses modifications, et
récupère celles faites ailleurs.

## Structure du dépôt

```
content/                         visible : wiki, recherche, MCP
.synaptane/inbox/                éléments en attente de rangement
.synaptane/vocation.md           vocation de l'espace (facultatif)
.synaptane/prompts/tidy.md       consignes de rangement (facultatif)
```

Ancien nom des consignes, `.synaptane/prompts/rangement.md` : encore lu s'il est seul ; le
premier enregistrement depuis Synaptane le remplace par `tidy.md`.

La vocation et les consignes se modifient dans **Paramètres → Vocation** ou par un push
(voir [le rangement](ai.fr.md#vocation-et-consignes-de-rangement)).

À la première synchronisation, Synaptane crée ce qui manque :
- `content/README.md`, si `content/` n'existe pas ;
- `.synaptane/inbox/`.

Le reste du dépôt est ignoré.

## Ou laisser Synaptane créer le dépôt

À la création d'un espace (**Nouvel espace**, admin d'organisation), choisissez **Créer le dépôt
pour moi sur GitHub ou GitLab**. Synaptane crée un dépôt **privé et vide**, y relie l'espace et
le prépare à la première synchronisation : `content/README.md` et `.synaptane/inbox/` dans un
premier commit, sur la branche saisie (`main` par défaut), qui devient la branche par défaut du
dépôt. Les étapes 1 et 2 ci-dessous sont alors faites pour vous.

- **Fournisseur Git** : GitHub (github.com) ou GitLab : gitlab.com, ou un GitLab auto-hébergé
  (son adresse, en `https://`).
- **Propriétaire** : une organisation GitHub, ou un groupe ou sous-groupe GitLab
  (`acme/equipe`). Vide : le compte auquel appartient le jeton.
- **Nom du dépôt** : vide, l'identifiant de l'espace.
- **Jeton d'accès** : un jeton qui peut créer le dépôt (voir plus bas). Il ne sert qu'à cette
  création : il n'est jamais enregistré, et n'apparaît jamais dans les journaux, le journal
  d'audit ni une réponse de l'API.

Ce que Synaptane garde est limité au nouveau dépôt. Le jeton qui crée des dépôts peut bien plus
que ce dont la synchronisation a besoin : il n'est donc pas gardé.

- **GitHub** : une clé de déploiement SSH avec droit d'écriture, générée par Synaptane et
  enregistrée sur le dépôt (titre `Synaptane (<votre hôte>)`). Les clés SSH de github.com sont
  lues dans l'API de GitHub en HTTPS (`api.github.com/meta`) : il n'y a rien à comparer à la
  main. Si GitHub les change, la synchronisation refusée par la vérification de la clé d'hôte
  les relit dans la même API, les enregistre et réessaie une fois (journal d'audit : **Clés
  d'hôte SSH de GitHub mises à jour**) ; une clé vue en SSH n'est jamais acceptée telle quelle.
  Cela vaut pour **tout** accès SSH à github.com, y compris une clé de déploiement et des clés
  d'hôte saisies à la main : les clés d'hôte collées sont alors remplacées par celles que
  publie l'API de GitHub.
  Le serveur doit joindre github.com sur le port SSH 22.
- **GitLab** : un jeton d'accès de projet nommé `Synaptane`, avec les scopes `read_repository`
  et `write_repository` et le rôle **Maintainer** (GitLab protège la branche par défaut au
  premier push), valable le plus longtemps que l'instance le permet : 365 jours par défaut.
  Sa date d'expiration est gardée avec lui, et Synaptane prévient avant qu'elle arrive (voir
  [Expiration des jetons](#expiration-des-jetons)) : créez-en alors un nouveau sur GitLab
  (Settings → Access tokens) et saisissez-le dans **Accès au dépôt** (étape 2). Pas de clé de
  déploiement sur GitLab : elle ne peut pas pousser sur la branche par défaut protégée sans
  règle de branche supplémentaire.

### Jeton à créer

**GitHub** : un jeton d'accès personnel *fine-grained* (Settings → Developer settings →
Personal access tokens → Fine-grained tokens) :
- **Resource owner** : votre compte, ou l'organisation à qui appartiendra le dépôt.
  L'organisation doit autoriser les jetons *fine-grained* ; si elle exige une approbation, le
  jeton ne fonctionne qu'une fois approuvé par un propriétaire de l'organisation ;
- **Repository access** : **All repositories** (le dépôt n'existe pas encore, il ne peut donc
  pas être sélectionné) ;
- **Repository permissions** : **Administration**, en lecture et écriture : elle crée le dépôt
  et enregistre sa clé de déploiement. GitHub ajoute lui-même **Metadata** (lecture seule).

Un jeton classique avec le scope `repo` fonctionne aussi, mais il ouvre tous vos dépôts :
préférez un jeton *fine-grained* à durée courte, et supprimez-le une fois l'espace créé.

**GitLab** : un jeton d'accès personnel (User settings → Personal access tokens) avec le scope
**`api`**, qu'exige l'API des projets, d'un compte dont le rôle permet de créer des projets dans
le groupe (voir les paramètres du groupe, rubrique Permissions and group features). Synaptane
crée ensuite le jeton d'accès de projet avec ce même jeton : un jeton ne pouvant pas avoir un
rôle supérieur à celui de son créateur, le compte doit avoir au moins le rôle Maintainer sur le
nouveau projet. Une durée courte suffit, puisque Synaptane ne le garde pas.

Sur **GitLab.com, les jetons d'accès de projet demandent un abonnement Premium ou Ultimate**.
Synaptane lit d'abord l'offre de l'espace de noms : en offre gratuite, il refuse avant de créer
quoi que ce soit. Créez alors le dépôt vous-même sur GitLab, choisissez **Utiliser un dépôt
existant** et préparez son accès comme à l'étape 1. Sur un GitLab auto-hébergé, les jetons
d'accès de projet existent dans toutes les éditions, et l'offre n'est pas vérifiée.

### En cas de problème

Rien n'est créé si le jeton est refusé (invalide, expiré ou révoqué) ou n'a pas les droits, si
le propriétaire ou l'espace de noms n'existe pas, si le nom est déjà pris, si le fournisseur est
injoignable, répond trop lentement ou limite le nombre de requêtes. Chaque appel au fournisseur
est abandonné au bout de 20 secondes, résolution du nom, connexion et réponse entière
comprises, et la création entière au bout de 60 secondes. Un compte crée un dépôt à la fois
(une seconde demande pendant ce temps est refusée). Un certificat TLS refusé (expiré,
auto-signé, pour un autre nom) est signalé comme tel : pour un GitLab auto-hébergé avec une
autorité de certification privée, ajoutez-la aux autorités de confiance du serveur
(`SSL_CERT_FILE`). Le message dit lequel.

Si le dépôt a été créé mais que son accès n'a pas pu être configuré, l'espace est tout de même
créé dessus, et le message donne l'adresse du dépôt (si l'espace lui-même n'a pas pu être
créé, le message et le journal du serveur donnent aussi cette adresse) : saisissez ses identifiants dans
**Paramètres → Dépôt Git → Accès au dépôt** (étape 2). Synaptane ne supprime jamais un dépôt
chez le fournisseur : supprimez-le là-bas si vous n'en voulez plus.

Le journal d'audit enregistre **Dépôt Git créé par Synaptane** (`space.repository_created`) :
le fournisseur, l'hôte et le chemin du dépôt, et le type d'accès gardé. Jamais le jeton.

### GitLab auto-hébergé

Son adresse doit être en `https://`, sans identifiants. Pour un admin d'organisation, ce doit
être une adresse publique : comme pour les [webhooks](webhooks.fr.md), Synaptane résout le nom
une fois, refuse les adresses privées, de bouclage et lien-local, puis se connecte à l'adresse
vérifiée. Seul le super-administrateur peut relier un GitLab du réseau local (en
développement, l'option `allow_private_webhooks` lève aussi cette restriction). Les redirections
ne sont jamais suivies : le jeton reste à l'adresse saisie.

Par l'API REST : `create_repository` dans `POST /organizations/{org}/spaces` (voir la
[référence de l'API](../reference/api.fr.md#espaces)).

## 1. Préparer l'accès

**GitHub, en HTTPS** : créez un jeton *fine-grained* limité au dépôt, avec la permission
**Contents** en lecture et écriture.

**GitLab, en HTTPS** : créez un jeton d'accès de projet (Settings → Access tokens) avec les
scopes `read_repository` et `write_repository`, et le rôle :
- **Maintainer** si la branche est protégée, ce qui est le cas de `main` par défaut ;
- *Developer* suffit si la branche ne l'est pas, ou si les Developers sont autorisés à y
  pousser.

Le rôle *Guest*, parfois proposé par défaut, ne peut pas lire le code d'un projet privé : la
synchronisation échoue alors avec « You are not allowed to download code » (403). Le nom
d'utilisateur peut rester vide : GitLab accepte n'importe quel nom avec un jeton de projet.

**SSH (toutes forges)** : créez une clé de déploiement dédiée, avec droit d'écriture :

```sh
ssh-keygen -t ed25519 -N "" -C synaptane -f synaptane-deploy
# Déposez synaptane-deploy.pub sur la forge (Deploy keys, « Allow write access »),
# collez synaptane-deploy dans Synaptane.
```

Changer l'URL du dépôt retire les identifiants enregistrés : ils ne partent jamais vers un
autre dépôt que celui pour lequel ils ont été saisis.

## 2. Configurer l'espace

Dans **Paramètres → Dépôt Git** de l'espace (admin d'organisation) :

1. **Adresse du dépôt** : URL du dépôt et branche.
   - HTTPS : `https://github.com/acme/brain.git`.
   - SSH : `git@github.com:acme/brain.git` ou `ssh://git@hôte:2222/acme/brain.git`.
   - Ne mettez jamais d'identifiants dans l'URL.
2. **Accès au dépôt** : collez le jeton, ou la clé privée.
   - En SSH, **Lire les clés du serveur SSH** propose les clés d'hôte avec leurs empreintes.
     Comparez-les à celles publiées par la forge (GitHub les publie dans sa documentation
     « SSH key fingerprints ») avant d'enregistrer.

L'enregistrement des identifiants lance une synchronisation. Le bloc **État** affiche
ensuite l'état : à jour, en erreur (avec le message de Git) ou en conflit.

Les secrets sont chiffrés avec la clé maître et ne sont jamais réaffichés. Ils ne passent ni
dans la ligne de commande de Git, ni dans sa configuration.

## Expiration des jetons

Un jeton d'accès HTTPS cesse de fonctionner à sa date d'expiration, et la synchronisation
échoue alors. Synaptane connaît cette date :
- pour le jeton d'accès de projet GitLab qu'il a créé lui-même (365 jours par défaut) ;
- pour un jeton enregistré à la main dans **Accès au dépôt** (web, ou `PUT …/git/credentials`
  de l'API REST), quand le fournisseur la donne. Synaptane la demande une fois, au moment
  d'enregistrer le jeton :
  - **GitHub** (github.com) : l'en-tête `GitHub-Authentication-Token-Expiration` d'une réponse
    de son API (`GET api.github.com/rate_limit`, qui ne consomme aucun quota de requêtes),
    donné pour les jetons fine-grained et les jetons classiques qui expirent ;
  - **tout autre hôte HTTPS**, pris pour un GitLab : `GET /api/v4/personal_access_tokens/self`,
    qui donne `expires_at` pour les jetons d'accès personnels, de groupe et de projet. GitLab
    peut le refuser à un jeton sans la portée `read_api` ou `api` (un jeton de projet avec
    seulement `read_repository` et `write_repository`) : la date reste alors inconnue.

  L'appel ne part que vers l'hôte du dépôt lui-même (pour GitHub, son API), qui reçoit de toute
  façon le jeton pour la synchronisation, avec les protections de la création de dépôt :
  abandonné après 10 secondes, jamais redirigé, et pour un hôte auto-hébergé, adresse publique
  seulement (résolue une fois, puis connexion à l'adresse vérifiée), sauf pour le
  super-administrateur. Un dépôt à une adresse IP ou à un nom d'hôte qui n'est pas en ASCII
  simple n'est pas interrogé (deux versions d'IDNA peuvent faire de ce nom deux domaines
  différents), pas plus qu'avec un mot de passe qui n'est pas en ASCII simple. Rien d'autre n'est lu dans la
  réponse, et la date est gardée avec le jeton, chiffrée : pas de migration. Quand la date
  reste inconnue (pas d'expiration, refus, autre forge, injoignable), le jeton est enregistré
  quand même, sans avertissement. Une clé SSH n'expire pas.

  Un jeton qui contient un caractère invisible ou de contrôle (espace sans chasse, tabulation ou
  espace insécable collée avec lui) est refusé avec « Jeton d'accès invalide ». Espaces et
  lettres accentuées restent permises à l'intérieur : le champ sert aussi au mot de passe
  (Gitea, Bitbucket Server).

Une fois la date connue :
- **Paramètres → Dépôt Git → Accès au dépôt** l'affiche, en avertissement à moins de 30 jours,
  en problème une fois passée ;
- les administrateurs de l'organisation reçoivent une alerte (e-mail et **Alertes** dans le
  journal d'audit, webhook `space.alert`) **30 jours, 7 jours et 1 jour avant, puis le jour
  même** de l'expiration, une fois par palier : le worker vérifie toutes les heures, et un
  worker arrêté un moment n'envoie que le palier atteint. Un nouveau jeton, avec une autre
  date, repart de zéro. Si tous les e-mails d'un palier échouent (serveur d'envoi en panne),
  la vérification horaire suivante les renvoie, sans second événement d'audit ni webhook. Le
  journal d'audit et les alertes système montrent la date d'expiration ;
- **Administration > Santé** a une ligne **Jetons d'accès Git** : avertissement quand un jeton
  expire d'ici 30 jours, problème quand l'un a expiré, avec un lien vers la page Git de chaque
  espace ;
- une synchronisation refusée pour authentification après cette date dit que le jeton a
  expiré, et quand. Seulement aux administrateurs de l'organisation, comme le reste de l'accès
  au dépôt : les autres lecteurs de l'espace voient le message général d'authentification.

Pour le renouveler : créez un nouveau jeton chez le fournisseur (GitLab : Settings → Access
tokens, portées `read_repository` et `write_repository`, rôle Maintainer ; GitHub : jeton
fine-grained limité au dépôt, **Contents** en lecture et écriture), puis collez-le dans **Accès
au dépôt**. GitLab donne un jour, en UTC : son jeton cesse de fonctionner à cette date, ce
jour-là compte donc comme expiré. GitHub donne l'instant : le dernier jour, avant cette heure,
le jeton « expire aujourd'hui », puis il a expiré.

## 3. Webhook (recommandé)

Sans webhook, Synaptane vérifie le dépôt toutes les `poll_minutes`, 5 minutes par défaut.
Avec un webhook, chaque push déclenche une synchronisation immédiate.

1. Bloc **Webhook** → **Générer le secret**.
2. Sur la forge :
   - **GitHub** : Settings → Webhooks → Add webhook. Indiquez l'URL affichée, le type
     `application/json`, le secret, et l'événement *Just the push event*.
   - **GitLab** : Settings → Webhooks → Add new webhook. Indiquez l'URL, le *Secret token*,
     cochez seulement *Push events* et laissez la vérification SSL activée. **Test → Push
     events** doit répondre 202.
   - **Gitea** : même principe que GitHub.

L'URL du webhook repose sur `public_url`, qui doit être joignable par la forge.

## Ce que fait la synchronisation

- Elle récupère la branche, puis intègre les nouveautés : avance rapide, ou fusion si
  Synaptane a aussi des commits non envoyés.
- Elle pousse aussitôt les commits de Synaptane, sans jamais forcer. Si le dépôt a avancé
  entre-temps, elle récupère, réintègre et renvoie (trois tours au plus).
- Les pages modifiées par des commits faits **hors de Synaptane** apparaissent dans l'inbox
  (éléments `change`, calculés depuis l'historique : rien n'est écrit dans le dépôt). Le
  rangement nocturne les intégrera : liens, emplacement, cohérence. Au premier clone,
  l'historique existant n'est pas considéré comme nouveau.
- Elle enregistre des commits lisibles :
  - l'auteur est la personne qui agit, ou pour les actions automatiques `Synaptane` (mise en
    place de l'espace, fusions), `Synaptane tidy-up` (passages de rangement),
    `Synaptane routine (<nom>)` (routines) ou `Synaptane recovery` (modifications locales
    réappliquées après un changement d'historique) ;
  - le committer est `Synaptane <synaptane@hôte-public>` ;
  - les sujets sont en anglais : `Inbox: …` (notes), `Tidy-up: …`, `Routine "<nom>": …`,
    `Synaptane: initialize space`, `Synaptane: merge remote changes`… Les historiques écrits
    par les versions antérieures gardent leurs noms et sujets en français
    (`Synaptane – rangement`, `Rangement : …`, `Inbox : …`), toujours reconnus : voir
    [Format du dépôt](../reference/repository-format.fr.md#commits).

Rien de local n'est perdu quand l'historique change sous Synaptane :
- **URL ou branche modifiée** (migration de forge, dépôt corrigé) : la copie locale est
  refaite depuis le nouveau dépôt, puis les pages et notes modifiées localement y sont
  réappliquées et poussées.
- **Historique distant réécrit** (force-push, purge d'un secret, branche recréée) :
  Synaptane adopte le nouvel historique ; les commits retirés ne reviennent pas. Seules les
  modifications faites dans Synaptane depuis la dernière synchronisation sont réappliquées.

Dans les deux cas, l'ancienne branche locale est d'abord sauvegardée en entier dans un
bundle Git (`data/recuperation/<id de l'espace>/<date>.bundle`, restaurable par `git clone`). Un
fichier modifié des deux côtés n'est pas réappliqué : il reste dans le bundle, et le journal
d'audit (`space.local_changes_recovered`) liste les fichiers concernés.

Changer seulement l'adresse d'un **même** dépôt (passage de HTTPS à SSH, nouveau nom de
domaine de la forge) ne fait rien perdre au rangement : les pages modifiées en attente et
les pages laissées avec une raison restent dans l'inbox. Elles ne sont recalculées que si le
nouveau dépôt ne contient pas l'historique déjà examiné.

Une copie locale laissée en plan par un processus tué (verrou d'index, fusion interrompue)
est réparée automatiquement à l'opération suivante.

## Forge lente ou en panne

Une forge qui ne répond plus ne bloque jamais Synaptane : les écritures web, REST et MCP
et le rangement continuent sur la copie locale, et partiront à la synchronisation suivante.
Tous les échanges avec la forge (clone, récupération, envoi) se font sans tenir la copie
locale ; seules les étapes locales (fusion, commit) la réservent, quelques instants.

Les délais :
- **HTTPS** : un transfert sous 1 000 octets par seconde pendant 60 secondes est abandonné ;
- **SSH** : connexion abandonnée après 20 secondes, session coupée après une minute sans
  réponse du serveur ;
- **délai global** d'un échange : 5 minutes (`git_timeout`, en secondes, voir
  [Configuration](configuration.fr.md)). À augmenter seulement si le premier clone d'un très
  gros dépôt n'aboutit pas.

Un échange interrompu passe l'espace **en erreur**, avec le message de Git ; ce qui a déjà
été récupéré et intégré est gardé. La synchronisation est retentée automatiquement.

Le jeton HTTPS n'est donné qu'à l'hôte de l'adresse du dépôt : si la forge redirige vers un
autre serveur, celui-ci ne le reçoit pas.

## Conflits

Quand un même fichier a été modifié des deux côtés :
- l'espace passe **en conflit** et rien n'est poussé ;
- le bloc **Conflits à résoudre** liste les fichiers, et pour chacun on garde la version de
  Synaptane ou celle du dépôt distant ;
- la résolution produit un commit de fusion, au nom de la personne qui a tranché, qui
  indique pour chaque fichier la version gardée. L'autre version reste dans l'historique Git.

## Le worker

La synchronisation tourne dans un processus séparé :

```sh
uv run synaptane worker
```

Il doit tourner en permanence à côté de `synaptane serve`. Plusieurs workers peuvent tourner
en même temps sans risque : chaque tâche n'est prise qu'une fois, et chaque espace n'est
traité que par un seul worker à la fois. Une erreur réseau est retentée automatiquement,
5 fois au plus. Une erreur d'authentification est affichée telle quelle sur la page de
l'espace (avec la date d'expiration quand le jeton enregistré a expiré).
