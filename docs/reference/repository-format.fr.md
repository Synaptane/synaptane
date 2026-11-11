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
  checks.md                       contrôles de l'équipe : règles que chaque page doit suivre (facultatif)
  routines/<nom>.md               routines d'agent de l'espace (facultatif)
  memory/<nom>.md                 ce qu'une routine retient d'un passage à l'autre (facultatif)
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

## `.synaptane/checks.md`

Les **contrôles de l'équipe** : des règles que chaque page Markdown de `content/` doit suivre,
vérifiées sans IA dans **Santé**, à chaque écriture et par le rangement (voir
[Contrôles de l'équipe](../guide/wiki.fr.md#contrôles-de-léquipe)). Ils s'écrivent dans
**Paramètres → Vocation** (administrateurs de l'organisation), par l'API REST ou par un push ;
20 000 caractères au plus depuis Synaptane.

Chaque titre de niveau 2 (`## Titre`) est une règle ; les éléments de liste `- clé: valeur`
juste en dessous la décrivent. Tout autre texte (paragraphes, titres de niveau 1 ou 3,
commentaires HTML, blocs de code) est un commentaire. Une valeur entre accents graves est
prise telle quelle (```` `` a`b `` ```` pour une valeur qui contient un accent grave). Les clés
sont en anglais.

| Clé | Sens |
|---|---|
| `pattern` | Expression régulière ([syntaxe RE2](https://github.com/google/re2/wiki/Syntax), en temps linéaire), sur le texte de la page, front matter compris ; `^` et `$` valent pour chaque ligne. Avec `field` : sur la valeur du champ |
| `field` | Champ du front matter (clé de premier niveau) |
| `expect` | Ce que `field` doit être, séparé par des virgules : `present` (présent et non vide), `list` (une liste YAML : `[a, b]` ou un `- élément` par ligne), `date` (`AAAA-MM-JJ`, sans heure). `list` et `date` ne s'appliquent que si le champ est présent |
| `preset` | Données personnelles : `email`, `phone` (formats international, français et nord-américain), `card` (13 à 19 chiffres qui passent la vérification de Luhn), `iban` (longueur de son pays et clé de contrôle, en majuscules ou en minuscules). Les espaces insécables, les tirets et les caractères invisibles entre les chiffres ne les arrêtent pas. S'emploie seul : ni `pattern` ni `field` |
| `allow` | Valeurs jamais signalées, séparées par des virgules. Pour `email` : des domaines (`acme.com`, sous-domaines compris) ou des adresses. Les domaines `example.com`, `.org` et `.net`, et `.example`, `.invalid`, `.test` et `.localhost`, sont toujours autorisés |
| `paths` | Dossiers ou globs relatifs à `content/`, séparés par des virgules : `clients/` (tout ce qu'il contient), `notes/*.md` (`*` dans un dossier), `**/reunions/*.md` (`**` à travers les dossiers). Sans elle : toutes les pages |
| `except` | Dossiers ou globs écartés, même forme |
| `level` | `warn` (par défaut) : signalé. `block` : une écriture qui enfreint la règle est refusée |
| `message` | Ce qu'il faut faire, affiché avec chaque infraction et avec un refus |
| `mask` | `yes` : l'extrait d'un `pattern` est masqué, comme pour les préréglages |
| `fix` | `yes` ou `no` : l'[entretien de nuit](../admin/ai.fr.md#entretien-du-wiki) corrige-t-il ce que les membres enfreignent de cette règle ? Par défaut, `yes` pour une règle `block` et pour les données personnelles (un préréglage, ou `mask: yes`), `no` pour les autres règles `warn`, seulement signalées dans **Santé** |

Limites : 50 règles, 500 caractères par motif, 3 000 étapes RE2 par motif compilé et 15 000
pour tout le fichier (une répétition comptée comme `.{0,1000}` en coûte autant : RE2 reste
linéaire, mais son coût par octet augmente avec elles), 64 000 caractères du fichier lus.
**Santé** examine le premier Mo de chaque page et montre 20 infractions par règle et par page ;
une écriture est vérifiée en entier. Une règle fautive (clé inconnue, motif invalide, motif
trop complexe, motif qui correspond à un texte vide, deux règles du même titre) est ignorée et sa ligne apparaît dans **Santé** ; les autres règles s'appliquent. Un
enregistrement depuis Synaptane refuse un fichier qui a une règle fautive.

Une règle `block` refuse les écritures de l'interface web, de l'API REST, de MCP, de l'import,
du rangement, de la page de rapport d'une routine et de l'annulation d'un rangement ou du
passage d'un assistant, quand elles ajoutent une infraction : une page qui enfreignait déjà la règle
reste modifiable, et une page déplacée dans un dossier que couvre une règle compte comme
nouvelle. Un push ne peut pas être refusé.

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
`template`, `memory` (`true` : la routine garde une mémoire, voir plus bas). Les valeurs sont
du texte simple ou du JSON. Un champ inconnu est signalé et ignoré ; un fichier invalide ne
tourne pas, et son erreur s'affiche dans Paramètres → Routines et dans l'API REST.

Écrites dans **Paramètres → Routines** (administrateurs de l'organisation : un commit à leur
nom) ou par un push : quiconque peut pousser peut les modifier. Lues à chaque
synchronisation ; l'état des passages (dernier passage, passage demandé) reste en base, si bien
qu'un fichier modifié ne relance jamais une routine. Voir
[Routines d'agent](../admin/ai.fr.md#routines-dagent-par-espace).

## `.synaptane/memory/<nom>.md`

La **mémoire** de la routine `<nom>`, quand son fichier indique `memory: true` : du Markdown
libre, 4 000 caractères au plus, que la routine réécrit en entier à la fin d'un passage et
relit au début du suivant (sujets déjà signalés, pages suivies, ce qui reste à surveiller).
Elle entre dans le commit du passage ; un passage interrompu la laisse telle quelle, et une
mémoire modifiée pendant un passage (un push déjà synchronisé, un administrateur qui la vide)
garde cette modification. Quiconque peut pousser peut la corriger ou l'effacer ; la routine la lit comme
une donnée, jamais comme une consigne. Elle est retirée avec sa routine depuis Synaptane, et à
la synchronisation suivante quand un push supprime ou renomme la routine sans déplacer sa
mémoire. Voir [Mémoire](../admin/ai.fr.md#mémoire).

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
- Une routine commite sa page de rapport, ses éléments d'inbox et sa mémoire au nom de
  `Synaptane routine (<titre>)`, sujet `Routine "<titre>": …` (`Synaptane – routine` dans les
  historiques plus anciens).
- Une routine enregistrée depuis Synaptane : `Routine updated: <nom>` ou
  `Routine removed: <nom>` (avec sa mémoire), au nom de l'administrateur ; une mémoire vidée :
  `Routine memory cleared: <nom>`. Les mémoires des routines supprimées par un push :
  `Synaptane: memories of removed routines deleted`. Les routines gardées en base par les
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
