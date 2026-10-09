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

Sur la page de l'espace (admin d'organisation) :

1. **Réglages** : URL du dépôt et branche.
   - HTTPS : `https://github.com/acme/brain.git`.
   - SSH : `git@github.com:acme/brain.git` ou `ssh://git@hôte:2222/acme/brain.git`.
   - Ne mettez jamais d'identifiants dans l'URL.
2. **Accès au dépôt** : collez le jeton, ou la clé privée.
   - En SSH, **Lire les clés du serveur SSH** propose les clés d'hôte avec leurs empreintes.
     Comparez-les à celles publiées par la forge (GitHub les publie dans sa documentation
     « SSH key fingerprints ») avant d'enregistrer.

L'enregistrement des identifiants lance une synchronisation. Le bloc **Dépôt** affiche
ensuite l'état : à jour, en erreur (avec le message de Git) ou en conflit.

Les secrets sont chiffrés avec la clé maître et ne sont jamais réaffichés. Ils ne passent ni
dans la ligne de commande de Git, ni dans sa configuration.

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
  [l'installation](installation.fr.md)). À augmenter seulement si le premier clone d'un très
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
l'espace.
