[English](concepts.md) | Français

# Concepts

Cette page explique ce qu'est Synaptane, les mots employés dans toute la documentation, et
comment les éléments s'articulent. Pour les fichiers du dépôt d'un espace, voir
[Format du dépôt](reference/repository-format.fr.md).

## Vision

Synaptane est un **second cerveau d'équipe stocké dans Git**.

- Chaque **espace** est un dépôt Git distant (GitHub, GitLab, Gitea ou tout serveur Git). Son
  dossier `content/` se consulte comme un wiki.
- **Le Markdown d'abord, mais tous vos fichiers.** Les pages sont en Markdown ; PDF,
  documents Office, images, pages HTML et tout autre fichier les côtoient, affichés dans le
  wiki, cherchables et lisibles par les assistants (voir [Fichiers](#fichiers)).
- On s'en sert d'abord depuis son assistant IA (Claude, ChatGPT…) via **MCP** : on y cherche,
  on y lit, et on dépose ce qu'on apprend dans l'**inbox** d'un espace.
- Chaque nuit, un agent IA **range l'inbox** dans l'arborescence de `content/`, en créant des
  dossiers si besoin. Il commite le résultat directement : Git sert d'historique et
  d'annulation.
- La recherche est textuelle par défaut, façon grep. Elle devient hybride (texte et
  embeddings) sur les espaces où un modèle d'embedding est configuré.

Une journée type : un administrateur crée une organisation et un espace relié à un dépôt
GitLab. Un membre branche Synaptane dans Claude.ai et, depuis une conversation, dépose une
note dans l'inbox. La nuit suivante, la note est rangée dans `content/` par un commit poussé
sur GitLab. Le lendemain matin, la page s'affiche dans le wiki et l'assistant la retrouve par
la recherche.

## Vocabulaire

| Terme | Définition |
|---|---|
| Instance | Un déploiement de Synaptane, avec ses comptes. |
| Organisation | Regroupement d'espaces avec ses membres et ses administrateurs (une société, une équipe, un usage personnel…). |
| Espace | Un dépôt Git distant et l'une de ses branches. Il appartient à une seule organisation. Désigné par `organisation/espace`, par exemple `acme/infra`. |
| Contenu | Les fichiers sous `content/`, pages Markdown et tout autre fichier : ce que montrent le wiki, la recherche et le MCP. |
| Inbox | Les éléments en attente de rangement : les notes sous `.synaptane/inbox/`, et les pages modifiées directement. |
| Rangement | Le passage quotidien de l'agent IA qui vide l'inbox dans `content/`. |
| Vocation | À quoi sert un espace, ce qu'on y range et comment il est organisé (`.synaptane/vocation.md`). Lue par le rangement, les routines et les assistants. |
| Consignes de rangement | Des règles de rangement précises pour l'agent (`.synaptane/prompts/tidy.md`). |
| Routine | Une tâche d'agent planifiée sur un espace (contrôle de cohérence, résumé hebdomadaire…) qui écrit un rapport et peut déposer des éléments dans l'inbox. |
| Fournisseur d'IA | Les identifiants d'un modèle de langage ou d'embedding, définis par organisation : une clé d'API, ou un abonnement Claude ou ChatGPT. |

## Rôles et droits

| Rôle | Portée | Peut |
|---|---|---|
| Super-admin | Instance | Créer et désactiver comptes et organisations, nommer les administrateurs d'organisation, configurer la connexion unique (SSO). Administre toutes les organisations, et lit et écrit tout leur contenu dans l'interface web et l'API REST (par le MCP, seulement les espaces dont il est réellement membre). |
| Administrateur d'organisation | Organisation | Créer et configurer les espaces (dépôt, identifiants Git, heure de rangement, embeddings, vocation, routines), gérer membres et rôles, fournisseurs d'IA, webhooks. A les droits de contributeur sur tous les espaces de l'organisation. |
| Contributeur | Espace | Lire, chercher, déposer dans l'inbox, modifier `content/` directement (web, MCP, REST), lancer un rangement à la main. |
| Lecteur | Espace | Lire et chercher. Un lecteur ne dépose pas dans l'inbox. |

- Un compte peut avoir des rôles dans plusieurs espaces de plusieurs organisations.
- Un jeton personnel ou une connexion OAuth n'a jamais plus de droits que son propriétaire.
  Il peut être limité à la lecture et à certains espaces ; un jeton limité n'administre rien.
- Par le MCP, un super-admin n'atteint que les espaces dont il est réellement membre (ou dont
  il administre l'organisation) : son assistant ne voit pas toute l'instance.

**Connexion.** Les comptes locaux (mot de passe) et la connexion unique (GitLab, y compris
auto-hébergé, GitHub, Google ou tout fournisseur OIDC) coexistent. La connexion unique
n'accorde aucun droit à elle seule : les domaines d'e-mail autorisés restreignent qui peut
s'inscrire, mais n'accordent aucune adhésion, et un administrateur ajoute chaque membre. Voir
[SSO](admin/sso.fr.md).

## Git, source de vérité

- Le contenu vit dans les dépôts. La base PostgreSQL contient les comptes,
  organisations, espaces et droits, les réglages (dépôt, rangement, fournisseurs d'IA,
  routines, webhooks, connexion unique), les jetons et connexions, les secrets chiffrés, la
  file des tâches, l'historique des rangements et des routines, le journal d'audit et l'index
  sémantique (pgvector, reconstruit depuis Git). La recherche plein texte n'a pas d'index :
  ripgrep lit les dépôts et le texte extrait, gardé sous `data_dir`. `synaptane reindex`
  reconstruit le texte extrait et l'index sémantique depuis Git.
- Chaque espace a une copie locale. Chaque écriture est un commit lisible : l'auteur est la
  personne qui agit (ou l'agent de rangement, `Synaptane tidy-up`), et le message dit ce qui a
  été fait (`Inbox: …`, `Tidy-up: …`, `Synaptane: initialize space`…). Le commit est poussé
  aussitôt. Les historiques écrits par les versions antérieures gardent leurs noms d'auteur et
  sujets en français (`Synaptane – rangement`, `Rangement : …`), toujours reconnus.
- Les modifications faites ailleurs (un push, une édition sur la forge) sont récupérées par un
  webhook entrant, avec une synchronisation de secours toutes les 5 minutes, puis indexées.
- Quand les historiques divergent, Synaptane fait un rebase ou une fusion. En cas de conflit,
  l'espace est marqué comme tel et un administrateur choisit, fichier par fichier, entre la
  version distante et celle de Synaptane. Synaptane ne force jamais un push et ne laisse
  jamais une IA résoudre un conflit.

Détails : [Dépôts Git](admin/git.fr.md).

## Fichiers

Le Markdown est le format natif : seules les pages Markdown ont des liens, des rétroliens, un
sommaire, un état de vérification et des pages proches, et l'agent de rangement écrit du
Markdown. Mais un espace conserve tout fichier, dans le même historique Git, et Synaptane tire
parti de chacun :

| Format | Dans le wiki | Recherche | Assistants IA (MCP) |
|---|---|---|---|
| Markdown (`.md`, `.markdown`) | Page rendue | Mots et sens | Lecture et écriture |
| PDF | Lecteur intégré | Couche texte, ou OCR pour les pages scannées | Lecture du texte extrait |
| `.docx`, `.pptx`, `.xlsx`, `.odt`, `.odp`, `.ods`, et les anciens `.doc`, `.ppt`, `.xls` | Téléchargement, texte extrait dessous | Texte extrait | Lecture du texte extrait |
| Images (PNG, JPEG, GIF, WebP, AVIF, BMP, SVG) | Affichées | Texte de l'image, par OCR (sauf SVG et AVIF) | Lecture du texte OCR |
| HTML (`.html`, `.htm`) | Rendu dans un cadre isolé, source à un clic | Mots | Lecture et écriture |
| Autres fichiers texte (CSV, JSON, YAML, code, journaux…) | Coloration syntaxique | Mots | Lecture et écriture |
| Tout le reste | Conservé, versionné, téléchargeable | Nom du fichier | Listé, sans texte |

- Le texte des PDF, documents Office et images est **extrait** par le worker (pypdfium2,
  Tesseract pour l'OCR, catdoc pour les anciens formats Word et Excel) dans un processus séparé,
  limité en mémoire et en temps, et gardé dans un cache hors du dépôt. La recherche, les pages proches, la recherche par le sens, le MCP et
  l'agent de rangement s'en servent tous.
- Les fichiers arrivent par l'interface web (25 Mo par fichier), en pièces jointes d'une note
  déposée par un assistant ou par l'API REST (rangées par l'agent), ou par un `git push`.
- Les anciens formats binaires `.doc`, `.xls` et `.ppt` sont lus avec des limites : dans un
  `.xls`, les dates d'un format personnalisé apparaissent en nombres ; dans un `.ppt`, le texte
  des masques, des graphiques et des objets incorporés n'est pas lu ; un fichier protégé par un
  mot de passe n'a pas de texte.

Détails : [Fichiers autres que Markdown](guide/wiki.fr.md#fichiers-autres-que-markdown).

## Inbox

**Notes.** Par le MCP, l'API REST ou l'interface web, un contributeur dépose une note : un
titre, un contenu Markdown, des pièces jointes facultatives, sa provenance et une suggestion
de rangement facultative. Synaptane commite et pousse aussitôt la note sous
`.synaptane/inbox/`. Une note n'est refusée que pour sa taille, pour les droits, ou parce
qu'elle semble contenir un secret (mot de passe, clé d'API, clé privée, identifiants dans une
URL) ; la personne peut confirmer une fausse alerte.

**Modifications.** Un contributeur peut aussi modifier `content/` directement (web, REST,
MCP), et quiconque a accès au dépôt peut y pousser. Chaque page modifiée apparaît dans l'inbox
comme un élément de type **modification**, calculé depuis l'historique Git (aucun fichier
n'est écrit) : la page, ses auteurs, ses commits et le diff cumulé depuis le dernier
rangement. Les changements qui ne portent que sur des espaces blancs ou sur les champs de
vérification d'une page sont ignorés. Les commits de Synaptane lui-même (rangement, fusions)
n'en produisent jamais.

## Rangement

Chaque jour à l'heure choisie pour l'espace (dans son fuseau horaire), ou sur demande, s'il y
a des éléments dans l'inbox :

1. L'agent reçoit la description et la vocation de l'espace, les consignes par défaut et
   celles propres à l'espace, la liste des éléments et un aperçu de l'arborescence de
   `content/`.
2. Il explore le wiki (arborescence, lecture, recherche, rétroliens, pages proches,
   historique) et agit : créer, compléter, réécrire, déplacer, renommer, fusionner ou
   supprimer des pages et des dossiers, y compris des pages qui ne viennent pas de l'inbox.
   Un déplacement met à jour les liens qui menaient à la page.
3. Chaque élément est clos : une note rangée est retirée de l'inbox ; un élément que l'agent
   ne sait pas ranger y reste, avec sa raison, et est retenté au passage suivant. Pour une
   modification, l'agent vérifie les liens, l'emplacement, la cohérence avec les pages
   voisines et les doublons.
4. Le passage donne **un commit**, poussé aussitôt. Son rapport (éléments, pages touchées,
   coût) s'affiche dans l'interface web et peut déclencher un webhook. Un passage peut être
   annulé (revert de son commit).

**Langue.** Les consignes de l'agent sont en anglais. Il écrit dans la langue des pages
existantes de l'espace, ou dans celle que fixe sa vocation ; dans un espace encore sans page,
dans la langue de la note qu'il range. Une page qu'il complète garde sa langue. Les routines
suivent la même règle.

**Garde-fous.** Chaque passage plafonne les appels au modèle, les jetons, les appels d'outils
et les suppressions ; deux passages ne tournent jamais en même temps sur un espace. Une inbox
trop grosse pour un passage (25 éléments) est vidée par des passages enchaînés. Chaque passage
a un coût estimé, et un budget mensuel facultatif limite la dépense. Le contenu déposé dans
l'inbox reste une donnée, jamais une consigne pour l'agent.

**Fournisseurs.** Anthropic, OpenAI, Mistral, Ollama et tout endpoint compatible OpenAI (clé
d'API), plus deux abonnements : Claude (Claude Code) et ChatGPT (Codex). Le modèle est libre ;
il se choisit par organisation et peut être surchargé par espace.

Détails : [IA et rangement](admin/ai.fr.md).

## Vocation et consignes de rangement

On ne range pas de la même façon le wiki d'une équipe d'infrastructure, les notes d'une
équipe marketing et les papiers d'une famille. Chaque espace peut décrire, dans deux fichiers
Markdown versionnés dans son dépôt :

- sa **vocation** (`.synaptane/vocation.md`) : à quoi sert l'espace et pour qui, ce qu'on y
  range et ce qu'on n'y range pas, l'organisation des dossiers, les conventions, ce qui
  compte le plus, le vocabulaire. Le rangement, les routines et les assistants IA la lisent.
- ses **consignes de rangement** (`.synaptane/prompts/tidy.md`) : des règles précises pour
  l'agent seul, comme « une page par fournisseur dans `fournisseurs/` ».

Des modèles sont fournis (`infra`, `marketing`, `product`, `client`, `personal`, `family`,
`job-search`, `blank`), en anglais et en français : chacun se les voit proposer dans la langue
de son compte. Aucun des deux fichiers ne lève les garde-fous.

## Routines

Une routine, ce sont des consignes, un planning (quotidien, hebdomadaire ou mensuel, à une
heure locale de l'espace), un périmètre (certains dossiers, ou tout l'espace) et des sorties :
une page de rapport réécrite à chaque passage, des éléments déposés dans l'inbox, un e-mail,
un webhook `routine.done`. L'agent de la routine lit le wiki et n'écrit que dans ses sorties :
les vraies corrections passent par l'inbox et le rangement de la nuit, avec ses garde-fous.
Deux modèles sont fournis : un **contrôle de cohérence** hebdomadaire (contradictions,
informations périmées, doublons) et un **résumé hebdomadaire** de ce que l'équipe a appris et
décidé, en anglais et en français (la langue de la personne qui crée la routine).

Les administrateurs de l'organisation créent, modifient, lancent et suppriment les routines ;
les contributeurs de l'espace les voient, ainsi que leur journal. Le rangement nocturne n'est
pas une routine.

## Recherche

- **Texte** : ripgrep sur `content/` et sur le texte extrait des PDF, documents Office et
  images (gardé hors du dépôt). Insensible à la casse par défaut, expressions régulières en
  option. Tri par pertinence : correspondances dans le titre et le chemin, nombre
  d'occurrences, fraîcheur.
- **Par le sens (facultative, par espace)** : un administrateur d'organisation choisit un
  fournisseur et un modèle d'embedding. Les pages sont découpées en sections Markdown (les
  autres textes et le texte extrait, en passages) et indexées dans pgvector à chaque
  changement. Une requête sur un tel espace combine texte et
  vecteurs ; une recherche sur plusieurs espaces fusionne les résultats sans pénaliser les
  espaces sans embeddings.
- Les droits sont toujours appliqués avant la recherche.

Voir [Chercher](guide/wiki.fr.md#chercher).

## Interfaces

- **MCP** : un seul endpoint `/mcp` (Streamable HTTP), OAuth 2.1 avec PKCE et enregistrement
  dynamique des clients, ou jetons personnels. Une connexion atteint les espaces choisis au
  consentement : tous ceux de la personne, y compris les futurs, ou seulement certains (pour
  une personne membre de plusieurs organisations, seuls les espaces d'une organisation sont
  cochés par défaut). L'administration n'est pas exposée en MCP. Voir
  [Brancher son assistant IA](guide/mcp.fr.md).
- **Interface web** : wiki (arborescence, pages, liens, visionneuse PDF, aperçu HTML,
  historique et diff), recherche, édition Markdown, inbox, journal des rangements, routines,
  jetons et connexions, administration, état Git et résolution des conflits. Voir
  [Lire, écrire et chercher dans le wiki](guide/wiki.fr.md).
- **API REST** (`/api/v1`) : l'essentiel des capacités du MCP (sauf `backlinks`,
  `recent_changes`, `diff` et `append_page`), plus toute l'administration, avec un schéma
  OpenAPI généré. Voir [API REST](reference/api.fr.md).
- **Webhooks sortants** : par organisation, signés HMAC, avec relances. Voir
  [Webhooks](admin/webhooks.fr.md).

## Choix de conception

- **Git fait foi.** Le contenu vit dans des dépôts ordinaires, qu'on sait déjà héberger,
  relire et sauvegarder ; la base ne garde aucun contenu en propre.
- **Un espace, une branche de dépôt, une organisation.** Les droits et l'historique restent
  simples ; une organisation regroupe des espaces avec leurs administrateurs.
- **Déposer d'abord, ranger ensuite.** Les assistants déposent des notes dans une inbox
  plutôt que de modifier les pages ; un agent qui a la vue d'ensemble les range une fois par
  jour, en un seul commit qu'on peut relire.
- **Commit direct, sans file de validation.** Le rangement commite sans attendre d'accord.
  L'historique Git et l'annulation en un clic servent de filet, avec les plafonds et les
  alertes.
- **Pas d'IA dans la résolution des conflits.** Un conflit entre Synaptane et le dépôt
  distant est tranché par une personne, fichier par fichier ; Synaptane ne force jamais un
  push.
- **N'importe quel fournisseur.** L'agent tourne sur tout modèle pris en charge, avec une clé
  d'API ou un abonnement, sans liste blanche de modèles.
- **grep d'abord.** La recherche textuelle marche partout, sans réglage ; la recherche par le
  sens est une option par espace.
