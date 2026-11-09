[English](repository-format.md) | Français

# Format du dépôt

Chaque espace est une branche d'un dépôt Git. Cette page décrit ce que Synaptane lit et écrit
dans ce dépôt. Pour relier un espace à son dépôt, voir [Dépôts Git](../admin/git.fr.md) ;
pour les idées sous-jacentes, voir [Concepts](../concepts.fr.md).

## Structure

```
content/                          visible : wiki, recherche, MCP
  README.md                       page d'accueil de l'espace (créée si content/ manque)
  …arborescence libre…            pages Markdown, PDF, documents Office, images, tout fichier
.synaptane/                       réservé à Synaptane
  vocation.md                     vocation de l'espace (facultatif)
  prompts/tidy.md                 consignes de rangement (facultatif)
  prompts/<nom>.md                prompts d'équipe pour les assistants (facultatif)
  routines/<nom>.md               routines d'agent de l'espace (facultatif)
  inbox/
    2026-10-03T14-12-05Z-note-politique-mots-de-passe-wifi-3f2a.md   une note
    2026-10-03T14-12-05Z-note-politique-mots-de-passe-wifi-3f2a/     ses pièces jointes éventuelles
README.md, .github/, …            tout le reste : ignoré par Synaptane
```

- Tout ce qui est hors de `content/` et de `.synaptane/` est ignoré : le dépôt peut avoir son
  propre README, ses fichiers de CI ou d'autres dossiers.
- À la première synchronisation, Synaptane crée ce qui manque, et seulement cela :
  `content/README.md` (avec le nom et la description de l'espace) si `content/` n'existe pas,
  et `.synaptane/inbox/`.
- Les réglages de l'espace (description, heure et fuseau du rangement, modèle, embeddings)
  sont en base, modifiables dans l'interface web ou l'API REST, pas dans le dépôt. Les routines
  sont des fichiers du dépôt ; seul l'état de leurs passages est en base.
- Git fait foi : chaque fichier décrit ici peut aussi changer par un push, et Synaptane le
  prend en compte à la synchronisation suivante.

## `content/`

Tous les types de fichiers sont acceptés. Ce que Synaptane en fait dépend du type :

| Type | Affichage | Recherche |
|---|---|---|
| Markdown (`.md`, `.markdown`) | Rendu en page de wiki | Texte intégral |
| Texte (`.txt`, `.csv`, `.json`, `.yaml`, code…) | Affiché comme texte | Texte intégral |
| PDF | Visionneuse | Texte natif, OCR pour les pages scannées |
| Office (`.docx`, `.xlsx`, `.pptx`, `.odt`, `.ods`, `.odp`, `.doc`, `.xls`, `.ppt`) | Téléchargement, avec un aperçu texte | Texte extrait (anciens `.doc` et `.xls` : avec catdoc) |
| HTML (`.html`, `.htm`) | Rendu dans un cadre isolé (sandbox), source à un clic | Texte intégral (source) |
| Images (`.png`, `.jpg`, `.gif`, `.webp`, `.bmp`, `.avif`, `.svg`) | Affichées | Texte lu par OCR, sauf SVG et AVIF (désactivable avec `ocr_images`) |
| Autres fichiers binaires | Téléchargement | Nom et chemin seulement |

Le texte extrait des PDF, documents Office et images est gardé hors du dépôt, dans le cache du
serveur, et reconstruit par `synaptane reindex`. Détails :
[Fichiers autres que Markdown](../guide/wiki.fr.md#fichiers-autres-que-markdown).

### Pages

- **Titre** : le premier titre de niveau 1 (`# Titre`) ; à défaut, le nom du fichier.
- **Page d'accueil d'un dossier** : `README.md` (ou `index.md`) s'affiche à l'ouverture du
  dossier.
- **Liens** : les liens Markdown relatifs (`[guide](../infra/proxmox.md)`) et les liens wiki
  (`[[nom]]`, `[[nom|libellé]]`, `[[nom#section]]`, `[[dossier/nom]]`) sont résolus dans le
  wiki. Déplacer une page depuis Synaptane met à jour les liens qui menaient à elle.
- **Syntaxe Obsidian** : les pages écrites dans Obsidian s'affichent comme là-bas, et leurs
  fichiers ne sont pas modifiés. `![[image.png]]` montre l'image (avec `|300` ou `|300x200`
  pour sa taille), `> [!warning] Titre` est un encadré (`> [!WARNING]` de GitHub aussi),
  `==texte==` est surligné, et les `%%commentaires%%` et les identifiants de bloc (`^id`) sont
  masqués à la lecture. Un `%%commentaire%%` n'a rien de privé : la recherche, les assistants
  et Git le voient toujours. `![[page]]` est un lien : la page n'est pas incluse, et un
  encadré ne se replie pas.
- **Diagrammes** : un bloc de code ` ```mermaid ` est dessiné à la lecture. S'il ne peut pas
  l'être, le code reste affiché. Le dessin se fait dans le navigateur, dans un cadre isolé.
- **Front matter** : facultatif. Synaptane lit trois champs de vérification ; les autres
  champs sont gardés tels quels (les propriétés d'une ligne de base Notion, par exemple, que
  l'[import](../guide/wiki.fr.md#depuis-notion) y écrit).

```markdown
---
owner: ada            # identifiant d'un compte, ou nom libre
verified: 2026-10-03  # date de la dernière vérification
review_every: 6m      # 90d, 12w, 6m, 1y ; absent : celle de l'espace ; never : jamais
reconfirmed: 1        # vérifiée de nouveau sans changement : périodicité doublée (écrit par Synaptane)
---
# Procédure de bascule
```

Une page est **vérifiée** (une date, et l'échéance pas encore atteinte), **à revoir**
(échéance passée) ou **jamais vérifiée**. Chaque vérification sans changement depuis la
précédente double la périodicité (`reconfirmed`, jusqu'à deux ans). Un commit qui ne change que ces champs n'est pas
signalé au rangement, et les agents IA ne peuvent ni les changer ni les effacer en réécrivant
une page : seuls **Marquer comme vérifiée** (web), `verify_page` (MCP) ou l'API REST le font.
Voir [Vérifier une page](../guide/wiki.fr.md#vérifier-une-page).

## `.synaptane/vocation.md`

La **vocation** de l'espace, en Markdown : à quoi il sert et pour qui, ce qu'on y range et ce
qu'on n'y range pas, l'organisation des dossiers, les conventions (langue, ton, noms de
fichiers, formats de pages), ce qui compte le plus, le vocabulaire. L'agent de rangement, les
routines et les assistants IA la lisent (MCP : `list_spaces` en donne les 600 premiers
caractères, `read` avec le chemin `.synaptane/vocation.md` la donne en entier). 20 000
caractères au plus.

Elle se modifie dans **Paramètres → Vocation** (administrateurs de l'organisation), par l'API
REST ou par un push. Des modèles sont fournis : `infra`, `marketing`, `product`, `client`,
`personal`, `family`, `job-search`, `blank`. Un texte vide retire le fichier.

## `.synaptane/prompts/tidy.md`

Les **consignes de rangement** de l'espace : des règles précises pour l'agent de rangement
seul, comme « une page par fournisseur dans `fournisseurs/` » ou « ne jamais renommer les
procédures ». Elles viennent après les consignes par défaut et la vocation, et priment sur la
vocation pour la manière de ranger. 20 000 caractères au plus. Ni la vocation ni les
consignes ne lèvent les garde-fous (plafond de suppressions, contenu traité comme une
donnée).

L'ancien nom de ce fichier, `.synaptane/prompts/rangement.md`, est encore lu quand il est
seul ; le premier enregistrement depuis Synaptane écrit `tidy.md` à la place.

Voir [IA et rangement](../admin/ai.fr.md).

## `.synaptane/prompts/<nom>.md`

Les **prompts d'équipe pour les assistants** : tout autre fichier Markdown du dossier, un par
prompt (30 au plus), nommé en lettres minuscules, chiffres et tirets. En-tête `title`,
`description`, `arguments` (des noms séparés par des virgules, `?` pour un argument
facultatif) ; le corps utilise `{{argument}}` et `{{include page.md}}`. Écrits dans
**Paramètres → Prompts d'assistant** (administrateurs de l'organisation) ou par un push. Voir
[Les prompts de votre équipe](../guide/mcp.fr.md#les-prompts-de-votre-équipe).

## `.synaptane/routines/<nom>.md`

Les **routines d'agent** de l'espace, un fichier Markdown chacune (20 au plus), nommé en
minuscules, chiffres et tirets (40 caractères au plus) : c'est le nom de la routine. Un
en-tête entre deux lignes `---`, puis les consignes :

```markdown
---
title: Résumé hebdomadaire
schedule: weekly friday 16:00
scope: projets, decisions
report: rapports/semaine.md
email: members
---
Rédige le résumé de la semaine…
```

Champs : `title`, `description`, `schedule` (`daily 02:30`, `weekly monday 06:00`,
`monthly 1 06:00` ; le français est lu aussi, comme `hebdo lundi 06:00`, et Synaptane réécrit
en anglais), `scope` (dossiers séparés par des virgules, liste JSON ou un par ligne
`- dossier`), `report`, `inbox`, `email` (`admins`, `members`, `none`), `webhook`, `enabled`,
`template`. Les valeurs sont du texte simple ou du JSON. Un champ inconnu est signalé et
ignoré ; un fichier invalide ne tourne pas, et son erreur s'affiche dans Paramètres → Routines
et dans l'API REST.

Écrites dans **Paramètres → Routines** (administrateurs de l'organisation : un commit à leur
nom) ou par un push : quiconque peut pousser peut les modifier. Lues à chaque
synchronisation ; l'état des passages (dernier passage, passage demandé) reste en base, si bien
qu'un fichier modifié ne relance jamais une routine. Voir
[Routines d'agent](../admin/ai.fr.md#routines-dagent-par-espace).

## `.synaptane/inbox/`

Une **note** est un fichier Markdown avec un front matter, nommé d'après sa date (UTC), son
type, une forme courte de son titre et un suffixe aléatoire : les noms sont uniques et se
trient par date. Les pièces jointes vont dans un dossier du même nom, sans `.md`.

```markdown
---
type: "note"
title: "Politique des mots de passe Wi-Fi"
source: "mcp"
deposited_at: "2026-10-03T14:12:05+00:00"
deposited_by: ["Ada Lovelace <ada@example.com>"]
via: "Claude"
origin: "Conversation sur le réseau invités"
hint: "reseau/wifi.md"
attachments: ["politique.pdf"]
---

Les mots de passe du Wi-Fi invités changent chaque trimestre…
```

| Champ | Sens |
|---|---|
| `type` | `note` |
| `title` | Titre de la note |
| `source` | Canal utilisé : `web`, `api`, `mcp` ou `email` (la [capture par e-mail](../admin/email-capture.fr.md)) |
| `deposited_at` | Date et heure du dépôt (UTC, ISO 8601) |
| `deposited_by` | Qui l'a déposée : `Nom <e-mail>` (une liste) |
| `via` | Client : nom de l'application OAuth ou du jeton personnel (facultatif) |
| `origin` | D'où vient l'information : URL, réunion, conversation (facultatif, 2 000 caractères au plus) |
| `hint` | Emplacement suggéré (facultatif, 500 caractères au plus) |
| `attachments` | Noms des pièces jointes (facultatif) |

Les valeurs sont écrites en JSON, qui est du YAML valide. Limites d'une note : 200 000
caractères de contenu, 10 pièces jointes, 25 Mo par pièce jointe.

Quand l'agent de rangement range une note, il copie dans `content/` les pièces jointes dont il
a besoin, puis retire de l'inbox la note et son dossier de pièces jointes. Quand il ne sait
pas la ranger, la note reste, avec trois champs de plus : `status: "left"`, `reason` et
`left_at`. Un passage interrompu avant de traiter une note l'inscrit dans `tidy_attempts` ;
après 3 passages interrompus, la note est laissée avec une raison.

Les **modifications** (pages modifiées directement dans `content/`) n'ont pas de fichier :
elles sont calculées depuis l'historique Git depuis le dernier rangement.

## Commits

- Les écritures depuis l'interface web, l'API REST ou le MCP sont commitées au nom de la
  personne qui agit, puis poussées aussitôt.
- Les passages de rangement sont commités par `Synaptane tidy-up` (`Synaptane – rangement`
  dans les historiques plus anciens), sujet `Tidy-up: …` : un commit par passage. Ses commits,
  comme les fusions de Synaptane et les fichiers initiaux, ne sont jamais signalés à l'inbox
  comme des modifications.
- Une routine commite sa page de rapport et ses éléments d'inbox au nom de
  `Synaptane routine (<titre>)`, sujet `Routine "<titre>": …` (`Synaptane – routine` dans les
  historiques plus anciens).
- Une routine enregistrée depuis Synaptane : `Routine updated: <nom>` ou
  `Routine removed: <nom>`, au nom de l'administrateur. Les routines gardées en base par les
  versions précédentes sont écrites par `Synaptane: routines written to the repository`, à la
  première synchronisation après la mise à jour.
- Les commits de Synaptane lui-même ont des sujets en anglais : `Inbox: <titre>` (une note
  déposée ; aussi `Inbox: edit “…”`, `Inbox: delete “…”`, `Inbox: “…” moved to <espace>`),
  `Synaptane: initialize space`, `Synaptane: merge remote changes`,
  `Synaptane: resolve conflicts`, `Synaptane: recover local changes (…)`. Ce dernier est au
  nom de `Synaptane recovery` (`Synaptane – reprise` dans les historiques plus anciens), pour
  que les pages qu'il réapplique soient encore signalées au rangement. Les historiques écrits
  par les versions antérieures gardent leurs sujets en français (`Inbox : …`,
  `Rangement : …`, `Synaptane : …`) ; l'historique web les libelle les uns comme les autres.
- Synaptane ne force jamais un push.
