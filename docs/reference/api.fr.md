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
| `too_many_requests` | 429 (limite de débit d'un compte ou d'une organisation : réessayer plus tard) |
| `unavailable` | 503 (base, dépôt ou serveur momentanément indisponible ; en-tête `Retry-After`) |

Un corps de requête mal formé (champ manquant, mauvais type) renvoie 422 au format de
FastAPI : `{"detail": [{"loc": […], "msg": …, "type": …}]}`. Trop de tentatives de connexion,
ou d'inscriptions OAuth, renvoient aussi 429.

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

Une ressource invisible pour l'appelant renvoie 404, comme une ressource inexistante.

Droits : *super-admin* (instance), *admin* (de l'organisation), *contributeur* et
*lecteur* (de l'espace). Un admin d'organisation a tous les droits sur ses espaces.

## Compte

| Méthode et chemin | Rôle |
|---|---|
| `GET /me` | Compte appelant : `username`, `display_name`, `email`, `locale`, `is_superadmin`, `is_active` |
| `GET /me/tokens` | Jetons personnels : `id`, `name`, `hint` (4 derniers caractères), `created_at`, `expires_at`, `last_used_at`, `read_only`, `space_ids` |
| `POST /me/tokens` | `{"name": …, "days": 90, "read_only": false, "spaces": ["acme/infra"]}` (201) ; `days: null` : sans expiration. Renvoie le `token`, une seule fois |
| `DELETE /me/tokens/{id}` | Révoque (204) |
| `GET /spaces` | Tous les espaces visibles : `organization`, `slug`, `name`, `description`, `repo_url`, `branch`, `role` |

## Comptes (super-admin)

| Méthode et chemin | Rôle |
|---|---|
| `GET /users` | Tous les comptes |
| `POST /users` | `{"username": …, "display_name": …, "password": …, "email": …, "is_superadmin": false}` (201) |
| `PATCH /users/{username}` | `display_name`, `email`, `locale`, `is_active`, `is_superadmin`. Chacun peut modifier son nom et sa langue ; le reste est réservé au super-admin |

## Organisations

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET /organizations` | membre | Organisations visibles : `slug`, `name`, `description`, `role` |
| `POST /organizations` | super-admin | `{"slug": …, "name": …, "description": …, "join": true}` (201) ; `join` : le créateur en devient admin |
| `GET /organizations/{org}` | membre | Détail |
| `PATCH /organizations/{org}` | admin | `name`, `description` |
| `DELETE /organizations/{org}` | super-admin | Supprime une organisation sans espace (204 ; 400 s'il en reste) |
| `GET /organizations/{org}/members` | membre | `username`, `display_name`, `role` (`admin` ou `member`) |
| `PUT /organizations/{org}/members/{username}` | admin | `{"role": "member"}` : ajoute ou change le rôle |
| `DELETE /organizations/{org}/members/{username}` | admin | Retire (204) |

## Espaces

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET /organizations/{org}/spaces` | membre | Espaces visibles de l'organisation |
| `POST /organizations/{org}/spaces` | admin | `{"slug": …, "name": …, "description": …, "repo_url": …, "branch": "main"}` (201) |
| `GET …/spaces/{space}` | lecteur | Détail, avec le `role` de l'appelant et `review_every` (périodicité de vérification par défaut des pages, `""` : jamais) |
| `PATCH …/spaces/{space}` | admin | `name`, `description`, `repo_url`, `branch`, `review_every` (`90d`, `6m`, `1y`, `never`, `""` ; 400 si illisible). Changer `repo_url` retire les identifiants Git |
| `DELETE …/spaces/{space}` | admin | Supprime l'espace dans Synaptane ; le dépôt distant n'est pas touché (204) |
| `GET …/spaces/{space}/members` | lecteur | `username`, `display_name`, `role` (`reader` ou `contributor`) |
| `PUT …/spaces/{space}/members/{username}` | admin | `{"role": "contributor"}` |
| `DELETE …/spaces/{space}/members/{username}` | admin | Retire (204) |
| `GET …/spaces/{space}/vocation` | lecteur | Vocation de l'espace (`.synaptane/vocation.md`) : `path`, `text` (`""` si elle n'existe pas), `sha`, `pending` (en attente de la première synchronisation), `history` (5 dernières modifications : `commit`, `author`, `date`, `subject`) |
| `PUT …/spaces/{space}/vocation` | admin | `{"text": …, "base_sha": …, "allow_secrets": false}` : un commit au nom de l'appelant, puis une synchronisation. `text` : 20 000 caractères au plus, `""` retire le fichier. `base_sha` : empreinte lue avant modification (`""` : le fichier ne doit pas exister) ; 409 s'il a changé entre-temps. 422 si un secret probable est détecté. Renvoie `commit` (`null` si rien n'a changé) et `pending` : avant la première synchronisation, le texte est mis en attente et écrit par elle |
| `GET /vocation-templates` | compte | Modèles de vocation : `key` (`infra`, `marketing`, `product`, `client`, `personal`, `family`, `job-search`, `blank`), `label`, `summary`, `text` (dans la langue du compte : anglais ou français) |

`…` vaut `/organizations/{org}`. Pour créer un espace avec une vocation, créez-le puis
envoyez `PUT …/vocation` (le texte attend la première synchronisation).

## Git d'un espace

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET …/spaces/{space}/git` | lecteur | État : statut, dernière synchronisation, conflits, URL du webhook |
| `PUT …/spaces/{space}/git/credentials` | admin d'organisation | `{"kind": "https", "token": …}` ou `{"kind": "ssh", "private_key": …, "known_hosts": …}` |
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
| `GET …/spaces/{space}/history/{path}` | lecteur | Commits qui ont touché le fichier (même supprimé) : `sha`, `author`, `date`, `subject` |
| `POST …/spaces/{space}/restore` | contributeur | Remet un fichier tel qu'il était dans une version passée, un fichier supprimé aussi : `{"path": …, "commit": …}` (`commit` : un `sha` de l'historique), avec `message`, `base_sha` (le `sha` actuel, 409 si le fichier a changé depuis ; `""` pour un fichier supprimé) et `allow_secrets`, facultatifs. Renvoie `commit` (`null` si cette version est déjà l'actuelle). Une page garde ses champs de vérification actuels |
| `GET …/spaces/{space}/health?months=12&limit=500` | lecteur | Santé du wiki : `broken` (page, lien, suggestion), `orphans`, `empty`, `untitled`, `stale` (page, date), `due` (pages à revoir : `path`, `owner`, `verified`, `due`), `unowned` (vérifiées sans propriétaire) et leurs totaux `counts` (avec `verified` : pages vérifiées et à jour) |

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
`routine` ou `assistant` quand une IA l'a écrite, `null` pour une personne) et jusqu'à trois
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
| `DELETE …/spaces/{space}/inbox/{name}` | contributeur | Retire une note et ses pièces jointes (204) |
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
| `PATCH …/spaces/{space}/tidy-settings` | admin | `enabled`, `time` (`"03:00"`, heure locale), `timezone` (`Europe/Paris`), `model` (`""` : celui de l'organisation), `delay` (minutes entre un dépôt et le rangement au fil de l'eau, `0` : éteint) (204) |
| `POST …/spaces/{space}/tidy` | contributeur | Lance un rangement (202) ; 429 au-delà de la limite de passages manuels (voir [Erreurs](#erreurs)) |
| `GET …/spaces/{space}/tidy/runs` | lecteur | Passages : `status`, `trigger`, `model`, `items_done`, `items_left`, `summary`, `operations`, `commit`, jetons consommés, `cost`, `error`, `reverted_at` |
| `POST …/spaces/{space}/tidy/runs/{id}/revert` | admin | Annule un passage par un commit inverse |
| `GET …/spaces/{space}/tidy/runs/{id}/pages` | lecteur | Pages modifiées par le passage : `path`, `status` (`created`, `edited`, `deleted`, `moved`), `old` (ancien chemin d'une page déplacée), `state` (`undoable`, `changed` depuis le passage, `undone`) |
| `POST …/spaces/{space}/tidy/runs/{id}/undo-pages` | contributeur | `{"paths": […]}` : remet ces pages telles qu'avant le passage, en un commit. 409 pour une page modifiée depuis le passage (restaurez-la depuis son historique). Renvoie `commit`, `pages`, `notes_back` (notes revenues dans l'inbox : toutes leurs pages sont annulées) et `notes_kept` (toujours rangées : d'autres pages les gardent) |

## Routines d'agent

Une routine est un agent IA planifié qui relit le wiki d'un espace et n'écrit que dans ses
sorties (voir [Routines d'agent](../admin/ai.fr.md#routines-dagent-par-espace)). Chemins sous
`…/spaces/{space}/routines`.

| Méthode et chemin | Droit | Rôle |
|---|---|---|
| `GET …/routines` | contributeur | Routines de l'espace : `id`, `name`, `template` (`coherence`, `digest` ou `""`), `instructions`, `frequency` (`daily`, `weekly`, `monthly`), `weekday` (0 = lundi), `monthday` (1 à 28), `at` (`"06:00"`, heure locale de l'espace), `scope` (dossiers ; vide : tout l'espace), `output_page`, `output_inbox`, `output_email` (`""`, `admins`, `members`), `output_webhook`, `enabled`, `next_run`, `last_run` |
| `POST …/routines` | admin | Crée une routine (201). `{"template": "coherence"}` part d'un modèle fourni (ses réglages, puis les champs donnés) ; vierge : `name`, `instructions` et au moins une sortie |
| `GET …/routines/{id}` | contributeur | Une routine |
| `PATCH …/routines/{id}` | admin | Modifie les champs donnés (`enabled: false` la désactive) |
| `DELETE …/routines/{id}` | admin | Supprime la routine (204) ; ses passages restent comptés dans le budget, sa page de rapport reste dans le wiki |
| `POST …/routines/{id}/run` | admin | Lance la routine dès que le worker est libre (202) ; refusé (400) si le budget du mois est atteint |
| `GET …/routines/{id}/runs` | contributeur | Passages, du plus récent : `status` (`running`, `done`, `failed`), `trigger`, `model`, `summary`, `report` (texte de sortie), `outputs` (`page`, `inbox` : titres des éléments déposés, `emails` : nombre envoyé, `webhook`), `commit`, jetons, `cost`, `quota`, `error` |

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
| `GET /organizations/{org}/invitations` | Invitations en attente : `id`, `email`, `org_role`, `expires_at` |
| `POST /organizations/{org}/invitations` | `{"email": …, "org_role": "member", "spaces": {"infra": "reader"}}` (201). Renvoie le `link` à transmettre, une seule fois ; `granted: true` si l'adresse avait déjà un compte (accès donnés aussitôt). 30 par organisation et par heure au plus (429 au-delà) |
| `DELETE /organizations/{org}/invitations/{id}` | Annule (204) |

## MCP

`/mcp` (Streamable HTTP), authentifié par OAuth 2.1 (inscription dynamique, PKCE) ou par
jeton personnel. Voir [le guide MCP](../guide/mcp.fr.md). Le serveur parle les protocoles avec
poignée de main (jusqu'au 25 novembre 2025) et le protocole sans état du 28 juillet 2026.
Les documents de métadonnées de client (CIMD), qui remplacent l'inscription dynamique dans
cette révision, ne sont pas encore acceptés : le SDK ne les gère pas côté serveur.

L'inscription dynamique (`POST /register`) est limitée à 20 par heure et par adresse IP
(429 au-delà). Un client inscrit mais jamais autorisé est effacé au bout de 24 heures.
