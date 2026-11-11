[English](api.md) | Français

# API REST

- Base : `/api/v1`.
- Documentation interactive : `/api/docs`, et schéma : `/api/v1/openapi.json`. Publiés
  seulement si `api_docs = true` (variable `SYNAPTANE_API_DOCS`) : ils listent toutes les
  routes, inutile de les exposer sur une instance en production.

## Authentification

Créez un jeton personnel dans **Mon compte → Jetons personnels**, puis envoyez-le dans un
en-tête :

```sh
curl -H "Authorization: Bearer syn_…" https://brain.example.com/api/v1/spaces
```

Un jeton a au plus les droits de son propriétaire. Il est révoqué automatiquement si le
mot de passe change ou si le compte est désactivé.

Un jeton peut être **restreint** à sa création (web, ou `POST /me/tokens` avec
`{"name": …, "days": …, "read_only": true, "spaces": ["acme/infra"]}`) :
- `read_only` : lecture et recherche seulement, aucune écriture ni dépôt ;
- `inbox_only` : lecture, recherche, et dépôt, correction ou retrait de notes dans l'inbox,
  mais aucune modification directe des pages (pas avec `read_only`) ;
- `spaces` : ces espaces seulement, parmi ceux dont le compte est membre (ou dont il
  administre l'organisation) ; absent : tous, y compris les futurs. Le formulaire web ne coche
  que les espaces de la première organisation (par ordre alphabétique) quand le compte est
  membre de plusieurs ; l'API n'applique pas ce défaut.

Un jeton restreint n'administre rien (instance, organisation) et ne gère pas le compte : il ne
peut ni créer de jeton, ni changer de mot de passe ou d'e-mail (403).

## Adressage

Les organisations et les espaces sont désignés par leurs identifiants lisibles :
`/organizations/acme/spaces/infra/members/alice`.

## Erreurs

Une erreur renvoie `{"error": "<code>", "message": "<texte>"}`, avec le statut HTTP
correspondant. Le code ne change jamais ; le message est en anglais, ou en français avec
`Accept-Language: fr`. Testez le code, pas le message.

| Code | Statut |
|---|---|
| `invalid` | 400 |
| `unauthenticated` | 401 |
| `forbidden` | 403 |
| `not_found` | 404 |
| `conflict` | 409 |
| `secret_detected` | 422 |
| `check_failed` | 422 (un contrôle de l'équipe refuse le contenu : voir plus bas) |
| `too_many_requests` | 429 (limite de débit d'un compte ou d'une organisation : réessayer plus tard) |
| `unavailable` | 503 (base, dépôt ou serveur momentanément indisponible ; en-tête `Retry-After`) |

Un corps de requête mal formé (champ manquant, mauvais type) renvoie 422 au format de
FastAPI : `{"detail": [{"loc": […], "msg": …, "type": …}]}`, sans les valeurs reçues (elles
peuvent contenir un secret envoyé dans le même corps). Trop de tentatives de connexion, ou
d'inscriptions OAuth, renvoient aussi 429.

Limites `too_many_requests`, sur une fenêtre glissante :
- dépôts dans l'inbox : 120 notes par heure et par compte, tous espaces et toutes interfaces
  confondus (web, REST, MCP), notes déplacées vers un autre espace comprises ;
- **Ranger maintenant** (`POST …/tidy`) : 5 passages manuels par personne et par espace sur
  24 heures (20 pour un admin de l'organisation) ;
- invitations : 30 par organisation et par heure ;
- liens de vérification d'e-mail : 3 par compte et par heure.

`secret_detected` : le contenu envoyé (dépôt, écriture de fichier) semble contenir un secret
(mot de passe, clé d'API, clé privée…). Rien n'est commité. Retirez-le, ou renvoyez la requête
avec `"allow_secrets": true` si ce n'en est pas un. Pour une page existante, seules les lignes
ajoutées sont examinées.

`check_failed` : le contenu enfreint un des [contrôles de l'équipe](../guide/wiki.fr.md#contrôles-de-léquipe)
de l'espace dont le niveau est `block` (un e-mail de personne, un chemin local…). Rien n'est
commité ; le `message` donne la règle, la page, la ligne et ce qu'il faut faire. Contrairement
à un secret, il ne se confirme pas : corrigez le contenu. Pour une page existante, seul ce que
la modification ajoute compte.

Une ressource invisible pour l'appelant renvoie 404, comme une ressource inexistante.

Droits : *super-admin* (instance), *admin* (de l'organisation), *contributeur* et
*lecteur* (de l'espace). Un admin d'organisation a tous les droits sur ses espaces.

## Compte

| Méthode et chemin | Rôle |
|---|---|
| `GET /me` | Compte appelant : `username`, `display_name`, `email`, `locale`, `is_superadmin`, `is_active` |
| `GET /me/tokens` | Jetons personnels : `id`, `name`, `hint` (4 derniers caractères), `created_at`, `expires_at`, `last_used_at`, `read_only`, `inbox_only`, `space_ids` |
| `POST /me/tokens` | `{"name": …, "days": 90, "read_only": false, "inbox_only": false, "spaces": ["acme/infra"]}` (201) ; `days: null` : sans expiration. Renvoie le `token`, une seule fois |
| `DELETE /me/tokens/{id}` | Révoque (204) |
| `GET /spaces` | Tous les espaces visibles : `organization`, `slug`, `name`, `description`, `repo_url`, `branch`, `role` (effectif : donné dans l'espace ou par un groupe, rôle par défaut de l'organisation, ou admin), `restricted` |

## Comptes (super-admin)

| Méthode et chemin | Rôle |
|---|---|
| `GET /users` | Tous les comptes |
| `POST /users` | `{"username": …, "display_name": …, "password": …, "email": …, "is_superadmin": false}` (201) |
| `PATCH /users/{username}` | `display_name`, `email`, `locale`, `is_active`, `is_superadmin`. Chacun peut modifier son nom et sa langue ; le reste est réservé au super-admin |

## Organisations

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET /organizations` | membre | Organisations visibles : `slug`, `name`, `description`, `role`, `default_space_role` (`none`, `reader` ou `contributor` : rôle de chaque membre sur chaque espace sans y être ajouté, espaces restreints exceptés) |
| `POST /organizations` | super-admin | `{"slug": …, "name": …, "description": …, "join": true}` (201) ; `join` : le créateur en devient admin |
| `GET /organizations/{org}` | membre | Détail |
| `PATCH /organizations/{org}` | admin | `name`, `description`, `default_space_role` (`none`, `reader`, `contributor` ; 422 sinon) |
| `DELETE /organizations/{org}` | super-admin | Supprime une organisation sans espace (204 ; 400 s'il en reste) |
| `GET /organizations/{org}/members` | membre | `username`, `display_name`, `role` (`admin` ou `member`) |
| `PUT /organizations/{org}/members/{username}` | admin | `{"role": "member"}` : ajoute ou change le rôle |
| `DELETE /organizations/{org}/members/{username}` | admin | Retire (204), des espaces et des groupes de l'organisation aussi |

## Espaces

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET /organizations/{org}/spaces` | membre | Espaces visibles de l'organisation |
| `POST /organizations/{org}/spaces` | admin | `{"slug": …, "name": …, "description": …, "repo_url": …, "branch": "main", "restricted": false}` (201). `restricted: true` : fermé au rôle par défaut de l'organisation dès sa création. Avec `create_repository` au lieu de `repo_url`, Synaptane crée le dépôt sur GitHub ou GitLab (voir plus bas) |
| `GET …/spaces/{space}` | lecteur | Détail, avec le `role` effectif de l'appelant, `review_every` (périodicité de vérification par défaut des pages, `""` : jamais) et `restricted` (le rôle par défaut de l'organisation ne s'applique pas) |
| `PATCH …/spaces/{space}` | admin | `name`, `description`, `repo_url`, `branch`, `review_every` (`90d`, `6m`, `1y`, `never`, `""` ; 400 si illisible), `restricted` (`true` : seuls les membres ajoutés à l'espace, les membres des groupes qui y ont un rôle, et les admins de l'organisation, y accèdent). Changer `repo_url` retire les identifiants Git |
| `DELETE …/spaces/{space}` | admin | Supprime l'espace dans Synaptane ; le dépôt distant n'est pas touché (204) |
| `GET …/spaces/{space}/members` | lecteur | Membres ajoutés à l'espace : `username`, `display_name`, `role` (`reader` ou `contributor`, le rôle qui y est donné). Ceux qui n'y accèdent que par un groupe ou par le rôle par défaut n'y figurent pas (`GET …/spaces/{space}/groups` et `GET /organizations/{org}/members` les listent) |
| `PUT …/spaces/{space}/members/{username}` | admin | `{"role": "contributor"}`. Un compte qui n'est pas membre de l'organisation le devient : avec un rôle par défaut (`default_space_role`), il reçoit alors aussi ce rôle sur chaque espace non restreint de l'organisation |
| `DELETE …/spaces/{space}/members/{username}` | admin | Retire (204) |
| `GET …/spaces/{space}/groups` | lecteur | Groupes qui ont un rôle sur l'espace : `id`, `name`, `role`, `members` (`username`, `display_name`) |
| `GET …/spaces/{space}/vocation` | lecteur | Vocation de l'espace (`.synaptane/vocation.md`) : `path`, `text` (`""` si elle n'existe pas), `sha`, `pending` (en attente de la première synchronisation), `history` (5 dernières modifications : `commit`, `author`, `date`, `subject`) |
| `PUT …/spaces/{space}/vocation` | admin | `{"text": …, "base_sha": …, "allow_secrets": false}` : un commit au nom de l'appelant, puis une synchronisation. `text` : 20 000 caractères au plus, `""` retire le fichier. `base_sha` : empreinte lue avant modification (`""` : le fichier ne doit pas exister) ; 409 s'il a changé entre-temps. 422 si un secret probable est détecté. Renvoie `commit` (`null` si rien n'a changé) et `pending` : avant la première synchronisation, le texte est mis en attente et écrit par elle |
| `GET /vocation-templates` | compte | Modèles de vocation : `key` (`infra`, `marketing`, `product`, `client`, `personal`, `family`, `job-search`, `blank`), `label`, `summary`, `text` (dans la langue du compte : anglais ou français) |

`…` vaut `/organizations/{org}`. Pour créer un espace avec une vocation, créez-le puis
envoyez `PUT …/vocation` (le texte attend la première synchronisation).

**Création du dépôt** (voir [Dépôts Git](../admin/git.fr.md#ou-laisser-synaptane-créer-le-dépôt)) :
`POST /organizations/{org}/spaces` avec, au lieu de `repo_url` :

```json
{"slug": "infra", "name": "Infra",
 "create_repository": {"provider": "github", "owner": "acme", "name": "", "token": "github_pat_…"}}
```

- `provider` : `github` ou `gitlab` ; `gitlab_url` : adresse d'un GitLab auto-hébergé, en
  `https://` (vide : gitlab.com) ;
- `owner` : organisation GitHub, ou chemin du groupe GitLab (`acme/equipe`) ; vide : le compte
  du jeton lui-même. `name` : vide, le `slug` de l'espace ;
- `token` : ne sert qu'à cette requête, jamais conservé ni renvoyé.

La réponse (201) ajoute `repository` : `provider`, `host`, `path` (`acme/infra`), `url` (page
chez le fournisseur), `access` (`ssh` : clé de déploiement, `https` : jeton d'accès de projet
GitLab, `null` : non configuré), `access_error` (pourquoi, dans la langue de l'appelant : le
dépôt existe, envoyez ses identifiants par `PUT …/git/credentials`) et `token_expires`
(expiration du jeton GitLab). Rien n'est créé si le jeton ou ses droits sont refusés, si le
propriétaire est inconnu ou, sur GitLab.com, en offre gratuite (400), si le nom est déjà pris
(409), si le fournisseur limite les requêtes (429), est injoignable ou trop lent (503). Donner
à la fois `repo_url` et `create_repository` est refusé (400).

## Groupes

Un groupe donne à certains membres d'une organisation un rôle (`reader` ou `contributor`) sur
plusieurs de ses espaces à la fois. Le rôle effectif d'un membre sur un espace est le plus élevé
du rôle donné dans l'espace, des rôles de ses groupes sur cet espace et du rôle par défaut de
l'organisation (qu'un espace restreint ignore). Le rôle d'un groupe s'applique aussi à un espace
restreint : c'est ainsi qu'on l'ouvre à une équipe. Un groupe est désigné par son `id` (son nom
peut changer) ; son nom est unique dans l'organisation, sans tenir compte de la casse (409
sinon).

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET /organizations/{org}/groups` | admin | Groupes de l'organisation, par nom : `id`, `name`, `description`, `members` (`username`, `display_name`), `spaces` (`space` : identifiant, `name`, `restricted`, `role`) |
| `POST /organizations/{org}/groups` | admin | `{"name": …, "description": ""}` (201) |
| `GET /organizations/{org}/groups/{id}` | admin | Un groupe, avec ses membres et ses espaces (404 pour un groupe d'une autre organisation) |
| `PATCH /organizations/{org}/groups/{id}` | admin | `name`, `description` |
| `DELETE /organizations/{org}/groups/{id}` | admin | Supprime le groupe (204) : ses membres perdent l'accès qu'il donnait, sauf s'ils l'ont autrement |
| `GET /organizations/{org}/groups/{id}/members` | admin | `username`, `display_name` |
| `PUT /organizations/{org}/groups/{id}/members/{username}` | admin | Ajoute un membre de l'organisation (200, même s'il est déjà dans le groupe) ; 400 pour un compte qui n'est pas membre de l'organisation |
| `DELETE /organizations/{org}/groups/{id}/members/{username}` | admin | Retire du groupe (204) |
| `GET /organizations/{org}/groups/{id}/spaces` | admin | `space`, `name`, `restricted`, `role` |
| `PUT /organizations/{org}/groups/{id}/spaces/{space}` | admin | `{"role": "reader"}` : donne ou change le rôle du groupe sur un espace de l'organisation |
| `DELETE /organizations/{org}/groups/{id}/spaces/{space}` | admin | Retire le rôle du groupe sur l'espace (204) |

Retirer quelqu'un de l'organisation le retire de ses groupes ; supprimer un espace retire les
rôles des groupes sur lui. Un jeton limité à certains espaces, à la lecture ou à l'inbox reste
limité, quels que soient les groupes de son titulaire. Les changements sont écrits au journal
d'audit (`group.created`, `group.updated`, `group.deleted`, `group.member_added`,
`group.member_removed`, `group.space_role_set`, `group.space_role_removed`).

## Git d'un espace

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET …/spaces/{space}/git` | lecteur | État : statut, dernière synchronisation, conflits, URL du webhook ; pour un admin d'organisation, `credentials` (`https`, `ssh` ou `null`) et `credentials_expire` (date d'expiration du jeton HTTPS quand elle est connue, sinon `null`) |
| `PUT …/spaces/{space}/git/credentials` | admin d'organisation | `{"kind": "https", "token": …}` ou `{"kind": "ssh", "private_key": …, "known_hosts": …}`. La date d'expiration d'un jeton HTTPS est lue chez GitHub ou GitLab quand ils la donnent (voir [Expiration des jetons](../admin/git.fr.md#expiration-des-jetons)) |
| `DELETE …/spaces/{space}/git/credentials` | admin d'organisation | Supprime les identifiants |
| `POST …/spaces/{space}/git/sync` | contributeur | Demande une synchronisation (202) |
| `POST …/spaces/{space}/git/webhook-secret` | admin d'organisation | Génère un secret, renvoie l'URL et le secret |
| `POST …/spaces/{space}/git/conflicts/resolve` | admin d'organisation | `{"choices": {"content/a.md": "local" ou "remote"}}` (202) |

Webhook entrant (forges) : `POST /hooks/git/{org}/{space}`. Il est authentifié par
`X-Hub-Signature-256` (GitHub), `X-Gitea-Signature` (Gitea) ou `X-Gitlab-Token` (GitLab).

## Contenu d'un espace

Les chemins sont relatifs à `content/` :
`…/spaces/infra/files/serveurs/proxmox.md`.

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET …/spaces/{space}/tree` | lecteur | Tous les fichiers : chemin, taille, empreinte |
| `GET …/spaces/{space}/files/{path}` | lecteur | `kind`, `sha`, et `text` : contenu, ou texte extrait d'un PDF, d'un document Office ou d'une image ; pour une page Markdown, `freshness` (voir plus bas) |
| `GET …/spaces/{space}/raw/{path}` | lecteur | Octets bruts |
| `PUT …/spaces/{space}/files/{path}` | contributeur | `{"content": "…"}` ou `{"content_base64": "…"}`, avec `message`. Créer un fichier ne demande rien de plus ; **remplacer** un fichier existant exige son `base_sha` (le `sha` lu par `GET …/files/{path}`), sinon 409 : on n'écrase pas une modification qu'on n'a pas lue. 409 aussi si le fichier a changé depuis ; `"base_sha": ""` exige une création ; `"force": true` remplace sciemment sans `base_sha` |
| `DELETE …/spaces/{space}/files/{path}` | contributeur | Supprime |
| `POST …/spaces/{space}/move` | contributeur | `{"from": "a.md", "to": "b/a.md"}` ; les liens vers le fichier et les siens sont réécrits dans le même commit |
| `GET …/spaces/{space}/files/{path}/related?limit=8` | lecteur | Pages proches (panneau « Voir aussi ») : `path`, `title`, `score` (0 à 1, rien sous 0,2), `reason` (signal principal : `meaning` même sujet, `words` mots en commun, `backlink` cite cette page, `link` liée par un lien, `shared` liens en commun, `folder` même dossier), `reasons` (tous, du plus fort au plus faible), `label` (la raison en clair) et `linked` (la page pointe déjà vers elle). Liste vide s'il n'y a rien d'assez proche ; `limit` de 1 à 20. Un fichier nommé exactement `related` se lit par `…/raw/{path}` |
| `POST …/spaces/{space}/verify/{path}` | contributeur | Marque une page Markdown comme vérifiée aujourd'hui : `{"owner": …, "review_every": "6m", "verify": true}`, tout facultatif (`owner` absent : inchangé, la personne si la page n'en a pas ; `review_every` `""` : celle de l'espace ; `"verify": false` : propriétaire et périodicité seulement). Renvoie `commit` (`null` si rien n'a changé), `path` et `freshness`. Le commit ne touche que le front matter et n'est pas signalé au rangement ; 400 pour un fichier qui n'est pas une page Markdown |
| `GET …/spaces/{space}/reports/{path}` | lecteur | Signalements ouverts indiquant que la page est obsolète : `reason`, `by`, `at` |
| `POST …/spaces/{space}/reports/{path}` | lecteur | `{"reason": "…"}` (500 caractères au plus) : signale une page Markdown comme obsolète ; de nouveau : met à jour votre raison. 201 |
| `DELETE …/spaces/{space}/reports/{path}` | lecteur | Retire votre signalement ; `?all=true` les écarte tous (contributeur). Une vérification de la page les clôt aussi |
| `GET …/spaces/{space}/history/{path}` | lecteur | Commits qui ont touché le fichier (même supprimé) : `sha`, `author`, `date`, `subject` |
| `POST …/spaces/{space}/restore` | contributeur | Remet un fichier tel qu'il était dans une version passée, un fichier supprimé aussi : `{"path": …, "commit": …}` (`commit` : un `sha` de l'historique), avec `message`, `base_sha` (le `sha` actuel, 409 si le fichier a changé depuis ; `""` pour un fichier supprimé) et `allow_secrets`, facultatifs. Renvoie `commit` (`null` si cette version est déjà l'actuelle). Une page garde ses champs de vérification actuels |
| `GET …/spaces/{space}/health?months=12&limit=500` | lecteur | Santé du wiki : `broken` (page, lien, suggestion), `orphans`, `empty`, `untitled`, `stale` (page, date), `due` (pages à revoir : `path`, `owner`, `verified`, `due`), `unowned` (vérifiées sans propriétaire), `drifts` (tableaux qui divergent : `key`, `column`, `first` et `second` avec `page`, `line`, `value`), `checks` (contrôles de l'équipe enfreints : `rule`, `level`, `page`, `line`, `excerpt` avec les données personnelles masquées, `message`), `check_rules` (règles de `.synaptane/checks.md`), `check_errors` (ses lignes en erreur : `line`, `message`) et leurs totaux `counts` (avec `verified` : pages vérifiées et à jour, et `checks`) |

`freshness` (lecture d'une page, résultats de recherche, vérification) : `state` (`verified`,
`stale` ou `never`), `verified` et `stale` (booléens), `verified_at`, `owner`, `review_due`
(échéance, `null` sans périodicité) et `review_every` (périodicité effective). Les champs
sont lus dans le front matter de la page (`owner`, `verified`, `review_every`), voir
[le guide](../guide/wiki.fr.md#vérifier-une-page).

Chaque écriture produit un commit au nom de l'appelant, poussé aussitôt. Le message de commit
se termine par le canal et le jeton (`Source: api`, `Via: API · <nom du jeton>`) ; la page
apparaît comme élément `change` dans l'inbox, calculé depuis l'historique Git.

## Recherche

`GET /api/v1/search?q=…&spaces=acme/infra&spaces=perso/notes&regex=false&limit=20&mode=auto`

La recherche porte sur tous les espaces lisibles si `spaces` est absent. Chaque résultat
donne `space`, `path`, `title`, `score`, `match` (`text` : par les mots, `meaning` : par le
sens, `both`), `extracted` (trouvé dans du texte extrait), `verified` et `stale` (page
vérifiée et à jour, ou à revoir), `freshness` (détail, `null` hors pages Markdown), la
dernière modification (`edited`, sa date ; `edited_by`, son auteur ; `edited_by_ai` : `tidy`,
`routine` ou `assistant` quand une IA l'a écrite, `null` pour une personne),
`reported_outdated` (signalements ouverts indiquant que la page est obsolète) et jusqu'à trois
`snippets` (`line`, `text`, `before`, `after`). `mode=text` : par les mots
seulement. Une page vérifiée et à jour est classée un peu plus haut (+10 %), une page à
revoir un peu plus bas (−7 %).

**Notes en attente.** Une note déposée n'est cherchable dans les pages qu'une fois rangée.
Pour ne pas casser les clients existants, la réponse reste une liste de pages : l'en-tête
`X-Pending-Count` compte les notes en attente qui contiennent tous les mots de `q` (20 au
plus), et `GET /api/v1/search/pending?q=…&spaces=…` les liste : `space`, `name` (pour
`GET …/inbox/{name}`), `title`, `deposited_by`, `deposited_at`, des plus récentes aux plus
anciennes.

## Inbox

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET …/spaces/{space}/inbox` | lecteur | Éléments en attente, paginés (`limit`, `offset` ; total dans `X-Total-Count`), filtrés par `kind` (`note`, `change` ou `left` : laissés par le rangement) : `name`, `type` (`note` ou `change`), `title`, `deposited_by`, `deposited_at`, `status`, `reason`, `hint`, `source` (canal), `via` (client ou jeton), `origin` (provenance), `attachments` |
| `POST …/spaces/{space}/inbox` | contributeur | `{"title": …, "content": …, "hint": …, "source": …, "attachments": [{"name": …, "content_base64": …}]}` (201). `source` : provenance de l'information (URL, réunion…), enregistrée dans `origin` |
| `GET …/spaces/{space}/inbox/{name}` | lecteur | Un élément, avec `body` : le texte d'une note, ou les commits et le diff d'une page modifiée |
| `PATCH …/spaces/{space}/inbox/{name}` | contributeur | Corrige une note : `title`, `content`, `hint`, `source`, `allow_secrets` ; un champ absent reste inchangé. Une note laissée par le rangement redevient à traiter. Renvoie la note |
| `DELETE …/spaces/{space}/inbox/{name}` | contributeur | Retire une note et ses pièces jointes (204). Pour un problème signalé par une routine d'agent, `?not_a_problem=…` (pourquoi, 500 caractères au plus) retient que ce n'en est pas un : la routine ne le signalera plus tant que ses pages restent les mêmes |
| `POST …/spaces/{space}/inbox/{name}/move` | contributeur des deux espaces | `{"space": "acme/autre"}` : déplace la note dans l'inbox d'un autre espace ; renvoie `space` et `path` |
| `POST …/spaces/{space}/inbox/{name}/seen` | contributeur | Marque une page modifiée (`change`) comme vue : elle ne demande rien au rangement, jusqu'à sa prochaine modification (204) |
| `GET …/spaces/{space}/inbox/{name}/files/{filename}` | lecteur | Pièce jointe d'une note, en octets bruts (téléchargée, jamais exécutée) |

## Rangement par l'IA

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET /organizations/{org}/ai` | admin | Réglage IA, ou `null` : `provider`, `model`, `base_url`, `effort`, `has_key`, `price_input`, `price_output` (par million de jetons), `monthly_budget`, `month_spent`, `metered` (fournisseur facturé à l'usage : sans `monthly_budget`, la dépense n'a pas de limite) |
| `PUT /organizations/{org}/ai` | admin | `{"provider": "anthropic", "model": …, "effort": "medium", "api_key": …, "monthly_budget": …}`. `api_key` absent : clé conservée, sauf changement de fournisseur ou d'adresse ; `base_url` réservé au super-admin. Un `monthly_budget` est refusé (400) si le modèle de l'organisation, ou celui choisi par l'un de ses espaces, n'a pas de tarif connu et qu'aucun tarif n'est saisi |
| `POST /organizations/{org}/ai/test` | admin | Appel d'essai : `{"message": …}` |
| `GET …/spaces/{space}/tidy-rules` | admin | Consignes de rangement de l'espace (`.synaptane/prompts/tidy.md`, ou son ancien nom `.synaptane/prompts/rangement.md` s'il est seul ; `path` dit lequel) : même forme que `GET …/vocation` |
| `PUT …/spaces/{space}/tidy-rules` | admin | Mêmes champs et mêmes règles que `PUT …/vocation` |
| `GET …/spaces/{space}/checks` | admin | [Contrôles de l'équipe](../guide/wiki.fr.md#contrôles-de-léquipe) de l'espace (`.synaptane/checks.md`) : même forme que `GET …/vocation`. Les pages qui les enfreignent sont dans `GET …/health` |
| `PUT …/spaces/{space}/checks` | admin | Mêmes champs et mêmes règles que `PUT …/vocation` ; 400 si une règle est en erreur (le `message` nomme chaque ligne fautive) |
| `GET …/spaces/{space}/tidy-settings` | admin | `enabled`, `time`, `timezone`, `model`, `delay`, `maintain`, et le [compte rendu de la nuit](../admin/ai.fr.md#la-nuit--routines-et-rangement-puis-un-seul-compte-rendu) : `report_email` (`""`, `admins`, `members`), `report_always`, `report_webhook` (`true` si un webhook Slack est configuré : son adresse n'est jamais renvoyée) |
| `PATCH …/spaces/{space}/tidy-settings` | admin | `enabled`, `time` (`"03:00"`, heure locale), `timezone` (`Europe/Paris`), `model` (`""` : celui de l'organisation), `delay` (minutes entre un dépôt et le rangement au fil de l'eau, `0` : éteint), `maintain` ([entretien du wiki](../admin/ai.fr.md#entretien-du-wiki) chaque nuit, `true` par défaut), `report_email`, `report_always`, `report_webhook_url` (webhook entrant Slack, gardé chiffré ; `null` : inchangé, `""` : supprimé) (204) |
| `POST …/spaces/{space}/tidy-settings/test-report` | admin | Le compte rendu de la dernière nuit, marqué comme essai : par e-mail à l'appelant seul, et au webhook Slack (202) ; 400 si personne ne reçoit le compte rendu ; 429 au-delà de 5 essais par heure pour l'espace |
| `GET …/spaces/{space}/nights` | lecteur | Les 7 dernières nuits : `day`, `status` (`queued`, `running`, `done`), `steps` dans l'ordre (`kind` : `routine` ou `tidy`, `when` : `before` ou `after`, `name`, `title`, `routine_id`, `run_id`, `status`, `reason` : pourquoi une étape a été sautée ou a échoué), `report` (`quiet`, `emails`, `slack`) |
| `POST …/spaces/{space}/tidy` | contributeur | Lance un rangement (202) ; 429 au-delà de la limite de passages manuels (voir [Erreurs](#erreurs)) |
| `GET …/spaces/{space}/tidy/runs` | lecteur | Passages : `status`, `trigger`, `model`, `items_done`, `items_left`, `summary`, `operations`, `commit`, jetons consommés, `cost`, `error`, `reverted_at` |
| `POST …/spaces/{space}/tidy/runs/{id}/revert` | admin | Annule un passage par un commit inverse |
| `GET …/spaces/{space}/tidy/runs/{id}/pages` | lecteur | Pages modifiées par le passage : `path`, `status` (`created`, `edited`, `deleted`, `moved`), `old` (ancien chemin d'une page déplacée), `state` (`undoable`, `changed` depuis le passage, `undone`) |
| `POST …/spaces/{space}/tidy/runs/{id}/undo-pages` | contributeur | `{"paths": […]}` : remet ces pages telles qu'avant le passage, en un commit. 409 pour une page modifiée depuis le passage (restaurez-la depuis son historique). Renvoie `commit`, `pages`, `notes_back` (notes revenues dans l'inbox : toutes leurs pages sont annulées) et `notes_kept` (toujours rangées : d'autres pages les gardent) |

## Routines d'agent

Une routine est un agent IA planifié qui relit le wiki d'un espace et n'écrit que dans ses
sorties, et dans ses dossiers tenus si elle en a (voir [Routines d'agent](../admin/ai.fr.md#routines-dagent-par-espace)). Chemins sous
`…/spaces/{space}/routines`. Chaque routine est un fichier du dépôt,
`.synaptane/routines/<nom>.md` : chaque écriture est un commit de ce fichier au nom de
l'appelant, et un push peut aussi le modifier.

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET …/routines` | contributeur | Routines de l'espace : `id`, `name` (nom du fichier), `title`, `description`, `path` (fichier dans le dépôt), `sha` (empreinte du fichier, pour `base_sha` ; `null` tant que la prochaine synchronisation ne l'a pas écrit, `pending` vaut alors `true`), `error` (fichier invalide : pourquoi la routine ne tourne pas ; `null` sinon), `warnings` (ce qui a été ignoré, comme un champ inconnu), `template` (`coherence`, `digest`, `archive` ou `""`), `instructions`, `frequency` (`daily`, `weekly`, `monthly`), `weekday` (0 = lundi), `monthday` (1 à 28), `at` (`"06:00"`, heure locale de l'espace), `night` (`before` ou `after` le rangement, dans la nuit ; `""` : à `at`), `scope` (dossiers ; vide : tout l'espace), `writes` (dossiers tenus ; vide : aucun), `output_page`, `output_inbox`, `output_email` (`""`, `admins`, `members`), `output_webhook`, `enabled`, `memory` (garde des notes d'un passage à l'autre), `next_run`, `last_run` |
| `POST …/routines` | admin | Crée une routine (201). `{"template": "coherence"}` part d'un modèle fourni (ses réglages, puis les champs donnés) ; vierge : `title`, `instructions` et au moins une sortie (`writes`, les dossiers tenus, en est une). `name` : nom du fichier (minuscules, chiffres, tirets), tiré du titre s'il manque ; un texte libre est pris pour le titre, comme avant que les routines soient des fichiers. 409 si le nom est pris, 422 pour un secret probable (`allow_secrets: true` pour confirmer) |
| `GET …/routines/{id}` | contributeur | Une routine |
| `PATCH …/routines/{id}` | admin | Modifie les champs donnés (`enabled: false` la désactive ; `writes` absent garde les dossiers, `[]` les retire) et réécrit le fichier. `base_sha` : le `sha` lu avant ; 409 si le fichier a changé entre-temps (push, autre admin). Le nom du fichier ne change pas (400) ; un fichier invalide se corrige dans le dépôt (400) |
| `DELETE …/routines/{id}` | admin | Retire le fichier de la routine et sa mémoire (204 ; `?base_sha=` comme pour `PATCH`) ; ses passages restent comptés dans le budget, sa page de rapport reste dans le wiki |
| `POST …/routines/{id}/run` | admin | Lance la routine dès que le worker est libre (202) ; refusé (400) si le budget du mois est atteint ou si le fichier est invalide |
| `GET …/routines/{id}/runs` | contributeur | Passages, du plus récent : `status` (`running`, `done`, `failed`), `trigger`, `model`, `summary`, `report` (texte de sortie), `outputs` (`page`, `inbox` : titres des éléments déposés, `written` : pages écrites dans ses dossiers, avec `path`, `change` et `from` pour un déplacement, `conflicts` : pages modifiées par quelqu'un pendant le passage, sa version gardée, `emails` : nombre envoyé, `webhook`, `memory` : `saved`, `cleared`, ou `changed` quand quelqu'un l'a modifiée pendant le passage, dont la version est gardée), `commit`, jetons, `cost`, `quota`, `error` |
| `GET …/routines/{id}/memory` | contributeur | Ce que la routine retient d'un passage à l'autre ([Mémoire](../admin/ai.fr.md#mémoire)) : `enabled` (`memory: true` dans son fichier), `path` (`.synaptane/memory/<nom>.md`), `text` (`""` : aucune), `sha` (`null` : pas encore de mémoire), `size` (octets), `too_large` (ni affichée, ni lue par la routine) |
| `DELETE …/routines/{id}/memory` | admin | Vide la mémoire (204) : un commit au nom de l'appelant ; un passage en cours la laisse vidée |

## Recherche par le sens

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET /organizations/{org}/embeddings` | admin | Fournisseur d'embeddings, ou `null` : `provider`, `model`, `base_url`, `has_key`, `meaning_min_similarity` |
| `PUT /organizations/{org}/embeddings` | admin | `{"provider": "openai", "model": …, "api_key": …, "meaning_min_similarity": 0.55}` (seuil de similarité, `null` par défaut ; effacé quand le modèle change) |
| `PUT …/spaces/{space}/semantic` | admin | `{"enabled": true}` : active l'index de l'espace ; renvoie `files` et `chunks` indexés |

## Connexion SSO (super-admin)

| Méthode et chemin | Rôle |
|---|---|
| `GET /sso-providers` | Fournisseurs : `slug`, `kind`, `name`, `issuer`, `client_id`, `auto_create`, `link_by_email`, `allowed_domains`, `enabled`, `callback_url`, `open_to_anyone` (création automatique sans domaine autorisé sur GitHub, Google ou gitlab.com : tout détenteur d'un compte chez eux en obtient un ici) |
| `PUT /sso-providers/{slug}` | Crée ou modifie : `{"kind": "gitlab", "name": …, "client_id": …, "client_secret": …, "issuer": …, "auto_create": false, "link_by_email": null, "allowed_domains": ["example.com"], "enabled": true}` (`client_secret: null` conserve le secret ; `auto_create` absent : `false` ; `link_by_email: null` : oui pour GitHub et Google, non sinon ; `allowed_domains` : domaines d'e-mail admis pour la liaison et la création automatiques, domaine exact, `[]` : aucune restriction) |
| `DELETE /sso-providers/{slug}` | Supprime (204) |

Voir [SSO](../admin/sso.fr.md).

## Webhooks (admin d'organisation)

| Méthode et chemin | Rôle |
|---|---|
| `GET /organizations/{org}/webhooks` | Liste : `id`, `url`, `events`, `active`, `last_delivery_at`, `last_status`, `failures` (échecs de livraison consécutifs) |
| `POST /organizations/{org}/webhooks` | `{"url": …, "events": [...]}` (tous les événements par défaut). Renvoie le `secret`, une seule fois (201) |
| `DELETE /organizations/{org}/webhooks/{id}` | Supprime (204) |
| `POST /organizations/{org}/webhooks/{id}/test` | Envoie un événement `ping` (202) |
| `POST /organizations/{org}/webhooks/{id}/reactivate` | Remet en service un webhook coupé après des échecs répétés (204) |

Voir [Webhooks](../admin/webhooks.fr.md) pour le format et la signature.

## Invitations (admin d'organisation)

| Méthode et chemin | Rôle |
|---|---|
| `GET /organizations/{org}/invitations` | Invitations en attente : `id`, `email`, `org_role`, `expires_at`, `groups` (`id` et `name` des groupes rejoints à l'acceptation ; un groupe supprimé depuis n'y figure plus) |
| `POST /organizations/{org}/invitations` | `{"email": …, "org_role": "member", "spaces": {"infra": "reader"}, "groups": ["<id>"]}` (201). `groups` : identifiants de groupes de l'organisation (`GET /organizations/{org}/groups`, 200 au plus, 422 au-delà), rejoints à l'acceptation, une fois membre ; un groupe inconnu, ou d'une autre organisation, est refusé (404). Pour un compte existant, `groups` dans la réponse liste les groupes réellement rejoints. Renvoie le `link` à transmettre, une seule fois ; `granted: true` si l'adresse avait déjà un compte (accès donnés et groupes rejoints aussitôt). 30 par organisation et par heure au plus (429 au-delà) |
| `DELETE /organizations/{org}/invitations/{id}` | Annule (204) |

## MCP

`/mcp` (Streamable HTTP), authentifié par OAuth 2.1 (inscription dynamique, PKCE) ou par
jeton personnel. Voir [le guide MCP](../guide/mcp.fr.md). Le serveur parle les protocoles avec
poignée de main (jusqu'au 25 novembre 2025) et le protocole sans état du 28 juillet 2026.
Les documents de métadonnées de client (CIMD), qui remplacent l'inscription dynamique dans
cette révision, ne sont pas encore acceptés : le SDK ne les gère pas côté serveur.

L'inscription dynamique (`POST /register`) est limitée à 20 par heure et par adresse IP
(429 au-delà). Un client inscrit mais jamais autorisé est effacé au bout de 24 heures.
