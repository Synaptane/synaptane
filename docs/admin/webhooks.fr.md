[English](webhooks.md) | Français

# Webhooks sortants

Un admin d'organisation peut prévenir d'autres outils (Slack, n8n, une CI…) de ce qui se
passe dans les espaces de l'organisation. La configuration se fait sur la page de
l'organisation, section **Webhooks**, ou par l'API (`/api/v1/organizations/{org}/webhooks`).

## Événements

| Événement | Quand | `data` |
|---|---|---|
| `inbox.item_added` | Une information est déposée dans l'inbox (web, REST, MCP) | `path`, `title`, `source`, `via`, `origin`, `by` |
| `content.changed` | Le contenu change : web, REST, MCP, rangement ou push sur le dépôt | `paths`, `action` (`written`, `deleted`, `moved`, `tidied`, `pushed`), `by`, `source`, `via` (écritures web, REST, MCP) |
| `tidy.completed` | Un passage de rangement se termine, réussi ou non | `status`, `items_done`, `items_left`, `summary`, `commit`, `error` |
| `routine.done` | Un passage de routine se termine, réussi ou non (seulement pour les routines qui ont le webhook parmi leurs sorties) | `routine`, `template`, `status`, `summary`, `text` (texte de sortie), `page`, `inbox` (titres des éléments déposés), `commit`, `error` |
| `tidy.budget` | Le budget mensuel d'IA de l'organisation franchit 80 % ou 100 % | `level`, `spent`, `budget` |
| `space.alert` | Alerte aux administrateurs : `deletion_cap` (plafond de suppressions du rangement), `tidy_failed` (rangement en échec durable), `sync_error` (synchronisation en échec durable, ou en erreur passagère depuis plus de 6 h), `tidy_interrupted` (rangement arrêté avant la fin : erreur du fournisseur, plafond, budget ; ou éléments laissés après 3 passages interrompus), `job_abandoned` (tâche de fond abandonnée après 5 essais sur une erreur passagère : `task`, `attempts`, `error`), `inbox_backlog` (éléments en attente depuis plus de 3 jours), `quota_high` (quota d'un abonnement d'IA au-delà de 90 %), `big_changes` (rangement qui a fait de gros changements : pages fortement réduites, nombreux déplacements), `routine_failed` (routine d'agent en échec : `routine`, `error`) | `kind`, détails selon le type |
| `sync.conflict` | Un espace entre en conflit avec son dépôt distant | `paths` |
| `ping` | Bouton « Envoyer un test » | `message` |

## Format

Une requête `POST` en JSON :

```json
{
  "id": "5f0c…",
  "event": "content.changed",
  "occurred_at": "2026-10-03T21:04:11+00:00",
  "organization": "acme",
  "space": "acme/infra",
  "data": {"paths": ["stockage/nas.md"], "action": "written", "by": "alice", "source": "mcp"}
}
```

En-têtes :
- `X-Synaptane-Event` : le nom de l'événement ;
- `X-Synaptane-Delivery` : l'identifiant de l'envoi (le même à chaque nouvel essai) ;
- `X-Synaptane-Signature` : `sha256=` suivi du HMAC-SHA256 du corps brut, avec le secret du
  webhook.

Le secret (`whsec_…`) n'est affiché qu'une fois, à la création. Pour en changer, supprimez le
webhook et recréez-le.

Vérification en Python :

```python
import hashlib, hmac


def valid(secret: str, body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

## Livraison

Les envois partent du worker.
- Une réponse 2xx est un succès.
- Une réponse 5xx ou 429, ou une erreur réseau, donne lieu à un nouvel essai : jusqu'à
  5 tentatives, après 30 s, 2 min, 10 min puis 1 h.
- Les autres réponses 4xx ne sont pas retentées.
- Les redirections ne sont pas suivies.

Le dernier statut est affiché sur la page de l'organisation.

L'adresse doit être en HTTPS. Pour éviter qu'un webhook serve à sonder le réseau interne du
serveur, les adresses privées, de boucle locale ou de lien local sont refusées au moment de
l'envoi. En développement, l'option `allow_private_webhooks` lève ces deux restrictions.
