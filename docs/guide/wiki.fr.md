[English](wiki.md) | Français

# Lire, écrire et chercher

Chaque espace affiche le dossier `content/` de son dépôt Git sous forme de wiki.
- L'arborescence est à gauche et la page à droite.
- Les **Paramètres** de l'espace (la roue dentée à côté de son nom) donnent l'état de
  synchronisation (**Dépôt Git**) et la liste des membres (**Membres**).

## Lire

- **Markdown** : rendu avec titres, tableaux, cases à cocher et code coloré.
  - Les liens relatifs (`[guide](../infra/proxmox.md)`) mènent à la page visée. Un lien qui
    commence par `/` part de la racine de `content/`.
  - Les images relatives s'affichent.
- **Dossier** : son `README.md` ou son `index.md` sert d'introduction, suivi de la liste des
  fichiers.
- **Autres fichiers** (voir [Fichiers autres que Markdown](#fichiers-autres-que-markdown)) :
  - PDF : visionneuse du navigateur (ou un lien **Ouvrir le PDF** sur téléphone ou tablette) ;
  - images : affichées ;
  - pages HTML : rendues dans un cadre isolé ;
  - documents Office : téléchargement, avec leur texte extrait ;
  - fichiers texte et code : coloration syntaxique ;
  - autres types : téléchargement.
- **Pages qui pointent ici** : les pages Markdown qui ont un lien vers la page affichée.
- **Voir aussi** : les pages proches, découvertes sans rien chercher (voir ci-dessous).
- **Historique** : chaque modification, avec son auteur et le détail des lignes changées. Un
  contributeur peut **restaurer** une version antérieure : **Comparer avec la version
  actuelle** montre ce qu'elle changerait, puis **Restaurer cette version** la remet en place
  dans un nouveau commit, à son nom. Rien n'est perdu : le texte remplacé reste dans
  l'historique. La page garde sa vérification actuelle (propriétaire, date) : revenir à un
  ancien texte ne la vérifie pas.
- **Page supprimée ou déplacée** : son ancienne adresse le dit. Une page déplacée donne sa
  nouvelle adresse ; une page supprimée renvoie vers son historique, où restaurer une version
  la fait revenir.

### Voir aussi : les pages proches

Sous « Pages qui pointent ici » (et, sur mobile, après le contenu), le panneau **Voir aussi**
montre jusqu'à 8 pages proches de la page affichée : son titre, son chemin et la raison.
- **même sujet** : le sens, quand l'espace a la recherche par le sens (embeddings) ; deux pages
  qui parlent de la même chose avec d'autres mots se retrouvent.
- **mots en commun** : des mots rares partagés (accents et formes des mots repliés, comme dans
  la recherche ; les mots courants ne comptent presque pas).
- **cite cette page** : elle a un lien vers la page affichée ; **liée par un lien** : la page
  affichée a un lien vers elle ; **liens en commun** : elles citent les mêmes pages, ou sont
  citées par les mêmes.
- **même dossier** : un léger bonus, jamais suffisant seul.

Le panneau se charge après la page, sans la ralentir, et reste masqué quand rien n'est assez
proche : mieux vaut rien que du bruit. Les PDF, documents Office et images y figurent par
leur texte extrait ; un fichier sans texte, jamais.

**Pas encore liée** signale une page proche vers laquelle la page affichée ne pointe pas.
Un contributeur peut cliquer sur **Proposer de relier** : une note « Relier X et Y » est
déposée dans l'inbox, et le rangement de la nuit ajoutera le lien (rien n'est écrit dans le
wiki tout de suite). La proposition est faite une seule fois ; tant qu'elle attend, le
panneau affiche « Proposition en attente ».

Les assistants ont la même liste (outil MCP `related`, REST
`GET …/files/{path}/related`), et l'agent de rangement la reçoit en lisant chaque note :
de quoi compléter une page existante plutôt que la doubler.

## Fichiers autres que Markdown

Le Markdown d'abord, mais tous vos fichiers. Les pages sont en Markdown, et elles seules ont
des liens, des rétroliens, un sommaire, une vérification et des pages proches. Mais un espace
accueille tout fichier, versionné dans Git comme une page (historique, déplacement,
suppression), et Synaptane tire parti de chacun :

| Format | Dans le wiki | Recherche | Assistants IA (MCP) |
|---|---|---|---|
| PDF | Lecteur intégré | Couche texte, ou OCR pour les pages scannées | Lecture du texte extrait |
| Word, PowerPoint, Excel (`.docx`, `.pptx`, `.xlsx`, et les anciens `.doc`, `.ppt`, `.xls`), OpenDocument (`.odt`, `.odp`, `.ods`) | Téléchargement, texte extrait dessous | Texte extrait | Lecture du texte extrait |
| Images (PNG, JPEG, GIF, WebP, BMP, AVIF, SVG) | Affichées | Texte de l'image, par OCR (sauf SVG et AVIF) | Lecture du texte OCR |
| HTML (`.html`, `.htm`) | Rendu dans un cadre isolé, source à un clic | Mots | Lecture et écriture |
| Autres fichiers texte (CSV, JSON, YAML, code, journaux…) | Coloration syntaxique | Mots | Lecture et écriture |
| Tout le reste (archives, vidéos…) | Téléchargement | Nom du fichier | Listé, sans texte |

Dans les espaces où la recherche par le sens est activée, tous les textes de ce tableau sont
aussi cherchables par le sens.

- **PDF** : affiché par la visionneuse PDF du navigateur, dans la page. Sous 700 px de large
  ou sur un écran tactile, où cette visionneuse reste souvent vide, un lien **Ouvrir le PDF**
  au-dessus ouvre le fichier dans le lecteur du navigateur. Son texte est extrait page par
  page ; une page sans texte (un scan) est lue par OCR avec Tesseract, dans les
  langues choisies par l'administrateur (français et anglais par défaut). Les très longs
  documents sont lus jusqu'à 500 pages, et jusqu'à 100 pages scannées.
- **Word, PowerPoint, Excel et OpenDocument** : pas d'aperçu dans le navigateur, un lien
  **Télécharger**. Leur texte est extrait et affiché sous le lien, dans **Texte extrait
  (utilisé par la recherche)** : le corps d'un document Word, le texte des diapositives (pas
  les notes du présentateur), le texte et les valeurs des cellules d'un tableur : pour une
  formule, son résultat tel qu'enregistré dans le fichier, pas la formule elle-même.
- **Images** : affichées dans la page, et dans les pages Markdown qui les incluent
  (`![baie](baie.png)`). Le texte qu'elles contiennent (un schéma, une capture d'écran, la
  photo d'une étiquette) est lu par OCR, sauf si l'administrateur l'a désactivé
  (`ocr_images`). Un SVG s'affiche comme une image, ses scripts ne s'exécutent jamais ; ouvert
  seul, il est téléchargé.
- **Pages HTML** : rendues dans un cadre isolé de Synaptane (sandbox) : aucun accès à votre
  session ni à Synaptane, pas de requête réseau depuis les scripts, pas d'envoi de formulaire.
  **Voir la source** affiche le code, coloré. Il n'est jamais rendu seul à l'adresse de
  Synaptane : ouvert hors de sa page du wiki, il est téléchargé. La recherche trouve les mots
  de la source.
- **Fichiers texte** (CSV, JSON, YAML, journaux, scripts, configuration…) : colorés selon leur
  extension, modifiables comme une page, cherchables.
- **Autres fichiers** : conservés et versionnés, proposés au téléchargement, trouvés par leur
  nom.

Le texte d'un nouveau document devient cherchable quelques secondes à quelques minutes après
son ajout, le temps que le worker l'extraie.

**Ajouter des fichiers** :
- interface web : **Ajouter** dans un dossier (contributeurs), 25 Mo par fichier ;
- depuis un assistant IA : en pièces jointes d'une note (`inbox_add`, base64, jusqu'à 10
  fichiers de 25 Mo), avec le dossier dans `hint` ; le rangement de la nuit les range près de
  la page qu'ils concernent et ajoute le lien ;
- API REST : `PUT …/files/{path}` avec `content_base64`, ou pièces jointes d'une note de
  l'inbox ;
- Git : commitez-les sous `content/` et poussez. Au-delà de 64 Mo, un fichier est conservé
  mais son texte n'est pas extrait.

**Anciens formats Office** : le texte des fichiers `.doc`, `.xls` et `.ppt` (formats binaires
d'avant 2007) est extrait aussi : `.doc` et `.xls` par
[catdoc](https://www.wagner.pp.ru/~vitus/software/catdoc/), présent dans l'image Docker, `.ppt`
par Synaptane lui-même. Ils s'affichent comme les autres : un lien de téléchargement, le texte
extrait dessous. Ce qui est lu, et les limites :
- **Word** (`.doc`) : Word 97 à 2003, et aussi Word 6 et 95 ; un fichier RTF nommé `.doc` est
  lu aussi. Une ligne par paragraphe, cellules des tableaux séparées par des tabulations.
- **Excel** (`.xls`) : Excel 97 à 2003, et Excel 5 et 95. Une ligne par rangée, cellules
  séparées par des tabulations, une ligne vide entre les feuilles. Une date s'écrit en date
  (`2019-04-20`) seulement dans un des formats intégrés d'Excel ; dans un format personnalisé,
  et dans les fichiers enregistrés par LibreOffice, elle apparaît en nombre (jours depuis 1900).
- **PowerPoint** (`.ppt`) : PowerPoint 97 à 2003, et les fichiers enregistrés dans ce format par
  LibreOffice (les autres logiciels n’ont pas été testés). Titres, contenus, zones de texte libres, tableaux (une ligne par
  rangée, cellules séparées par des tabulations) et notes du présentateur, diapositive après
  diapositive (une ligne vide entre deux). Ne sont pas lus : les fichiers PowerPoint 95, le
  texte des masques (pieds de page qui y sont définis), des WordArt, des graphiques et des
  objets incorporés, les commentaires, et le texte qui n'est que dans une image.
- Un fichier protégé par un mot de passe, ou abîmé, n'a pas de texte : il reste téléchargeable
  et trouvé par son nom. Pour la meilleure recherche, enregistrez ces fichiers en `.docx`,
  `.xlsx` ou `.pptx`.

Sans catdoc (installation depuis les sources sans le paquet `catdoc`), les fichiers `.doc` et
`.xls` sont conservés et téléchargeables, sans texte, et `synaptane doctor` le signale ; une
fois catdoc installé, `synaptane reindex` lit les fichiers déjà présents. Les `.ppt` n'en ont
pas besoin.

## Premiers pas d'un espace

Jusqu'à son premier rangement, l'accueil d'un espace montre aux contributeurs ce qu'il reste à
faire : lier un dépôt Git, écrire la vocation, choisir le fournisseur d'IA, ajouter du contenu,
laisser passer le rangement. Chaque étape se coche d'elle-même quand la chose existe. La liste
s'en va après le premier rangement qui a fait quelque chose ; **Masquer** la retire plus tôt,
dans ce navigateur. Elle n'apparaît pas quand le rangement de l'espace est désactivé.

## Écrire (contributeurs)

- **Modifier** une page texte, **Nouvelle page**, **Ajouter** un fichier (25 Mo maximum, de
  tout type : voir [Fichiers autres que Markdown](#fichiers-autres-que-markdown)),
  **Déplacer** (ou renommer) ou **Supprimer ce fichier** (menu ⋮).
- **Aperçu**, au-dessus du texte d'une page Markdown (en la modifiant ou en la créant), montre
  la page telle qu'elle se lira, sans rien enregistrer : le même rendu qu'en lecture, syntaxe
  Obsidian (`![[image]]`, encadrés, `==surligné==`, commentaires `%%`), liens et
  `[[liens wiki]]` résolus dans l'espace, images de l'espace, code coloré et diagrammes
  Mermaid dessinés. **Écrire** revient au texte, qui reste tel quel. Sur grand écran, **Côte à
  côte** montre les deux : l'aperçu suit la frappe après une courte pause, et ce choix est
  retenu par votre navigateur. Dans l'aperçu, les liens s'ouvrent dans un nouvel onglet : on ne
  quitte jamais le texte en cours. L'aperçu demande JavaScript et n'existe que pour les pages
  Markdown ; il est refusé au-delà de 262 144 caractères (pas l'enregistrement), il est préparé
  un à la fois pour chaque compte, et rien n'est commité, indexé ni déposé dans l'inbox avant
  l'enregistrement.
- Chaque enregistrement produit un commit à votre nom, poussé aussitôt sur le dépôt.
- **Déplacer** garde les liens justes, dans le même commit : les liens relatifs de la page
  déplacée sont recalculés pour son nouvel emplacement, et les pages qui pointaient vers elle
  sont réécrites (liens relatifs, et `[[dossier/nom]]` qui ne mèneraient plus à elle). Un
  `[[nom]]` qui la trouve toujours n'est pas touché, et seul le lien change : le reste de la
  page reste tel quel. Le message affiché après le déplacement liste les pages mises à jour.
  Le MCP, l'API REST et l'agent de rangement déplacent de la même façon.
- Chaque page modifiée apparaît aussi dans l'inbox (élément « Modification », calculé depuis
  l'historique Git). Le rangement nocturne vérifiera l'emplacement, les liens et la cohérence
  avec les pages voisines. Une retouche d'espaces ou de lignes vides seulement est ignorée.
- Un texte qui semble contenir un secret (mot de passe, clé d'API, clé privée) est refusé
  avant d'être commité : il partirait sur la forge. Retirez-le (gardez-le dans un coffre de
  mots de passe), ou cochez « Ce n'est pas un secret » si c'est une fausse alerte.
- Si la page a changé entre-temps (autre personne, push externe), l'enregistrement est refusé
  et votre texte est conservé dans le formulaire. Rechargez, puis reportez vos modifications.

## Importer Obsidian, Markdown, Notion ou Confluence

Les contributeurs peuvent faire entrer tout un dossier : dans le menu **⋮** d'un dossier,
**Importer Obsidian, Notion, Confluence…**. Envoyez un zip (100 Mo au plus, 25 Mo par fichier,
10 000 fichiers, 400 Mo une fois décompressé), en choisissant un dossier de l'espace si besoin.
Synaptane reconnaît ce qu'est l'archive :

- un **coffre Obsidian**, ou n'importe quel dossier de **fichiers Markdown** zippé avec ses
  pièces jointes : les fichiers restent tels quels ;
- un **export Notion** ou un **export Confluence (HTML)** : les pages sont converties en
  Markdown, avec des noms nettoyés, et leurs liens mènent aux nouveaux chemins (voir plus bas).

Le formulaire permet de forcer le format, l'écran de contrôle aussi (**Lire ainsi**), si
l'archive n'est pas lue comme il faut. Rien n'est écrit à ce stade : Synaptane montre ce qui se
passerait.

- **Format reconnu**, le nombre de pages et de pièces jointes et, pour un export, le nombre de
  liens réécrits.
- **Fichiers à importer** : où va chaque fichier. Si tout est dans un seul dossier de tête (un
  coffre ou un espace zippé depuis l'extérieur), ce dossier n'est pas repris.
- **Secrets probables** : ces fichiers sont écartés, car un secret poussé dans Git est difficile
  à effacer. Les pages converties sont vérifiées aussi. N'en cochez un que s'il ne contient
  aucun vrai secret.
- **Déjà dans l'espace** : laissés tels quels, jamais écrasés.
- **Liens qui ne mènent nulle part** : liens d'un export vers une page qui n'y est pas (laissés
  tels quels, pour que **Santé** les liste ensuite).
- **Fichiers de l'export non importés tels quels** : styles et icônes de Confluence, second CSV
  d'une base Notion (voir plus bas).
- **Écartés** : les fichiers et dossiers cachés (`.obsidian`, `.trash`, `.git`), les fichiers
  système, et ce qui ne peut pas être écrit sans risque (liens, fichiers chiffrés, chemins qui
  sortent du dossier).

**Importer** écrit tout en un seul commit, à votre nom. S'il touche plus de 20 pages, le
rangement l'examine comme un seul élément, pas page par page. Les liens wiki et la
[syntaxe Obsidian](../reference/repository-format.fr.md) fonctionnent tels quels. Ensuite,
**Santé** liste les liens qui ne mènent nulle part. Une archive qui n'est pas importée est
supprimée au bout de 24 heures.

Quel que soit le format, un nom que Windows réserve à ses périphériques (`CON`, `PRN`, `AUX`,
`NUL`, `COM1` à `COM9`, `LPT1` à `LPT9`, avec ou sans extension) reçoit un `_` (`con.md` devient
`con_.md`), pour que le dépôt se clone sous Windows ; les liens vers lui dans un coffre sont à
corriger à la main (**Santé** les liste).

### Depuis Notion

1. Dans Notion, ouvrez le menu **⋯** de la page à exporter (ou, pour tout l'espace de travail,
   **Paramètres → Général → Exporter tout le contenu de l'espace de travail** ; réservé à ses
   propriétaires).
2. Choisissez **Exporter**, format **Markdown et CSV**, **Inclure le contenu : Tout**, et
   activez **Inclure les sous-pages** (et **Créer des dossiers pour les sous-pages** si Notion
   le propose).
3. Téléchargez le zip que Notion envoie (par e-mail pour un gros export) et envoyez-le tel quel
   à Synaptane. Un gros espace de travail arrive en zip de zips (`Export-…-Part-1.zip`,
   `-Part-2.zip`…) : envoyez le zip extérieur, Synaptane décompresse les parties, dans les mêmes
   limites. Si l'export dépasse 100 Mo, exportez quelques pages de premier niveau à la fois.

Ce qui est converti :

- **Noms** : l'identifiant de 32 caractères que Notion ajoute à chaque page, dossier et table
  (`Notes 0123…abcd.md`) est retiré ; un nom raccourci par Notion prend le titre complet de sa
  page (100 caractères au plus). Deux pages de même titre dans un dossier deviennent
  `Notes.md` et `Notes (2).md` (casse ignorée, pour que le dépôt se clone sous macOS et Windows).
  Une page qui a des sous-pages ou des fichiers devient la page d'accueil de son dossier :
  `Notes/README.md`, avec `Notes/Sous-page.md` et `Notes/image.png`.
- **Liens** : les liens vers une page, un fichier, une image ou une base de l'export sont
  réécrits vers les nouveaux chemins, ainsi que les adresses `https://www.notion.so/…` d'une page
  de l'export. Les liens vers des pages absentes de l'export restent tels quels.
- **Bases de données** : le CSV devient une page d'index (`Tâches/README.md`) : un tableau des
  lignes, chacune liée à sa page, les relations en liens. Quand Notion donne à la fois
  `Tâches.csv` et `Tâches_all.csv`, c'est le `_all`, complet, qui sert. Les propriétés de chaque
  ligne vont dans le front matter de sa page (`Status: "Done"`), déplacées depuis les lignes que
  Notion écrit sous le titre ; une propriété nommée comme un champ de vérification de Synaptane
  (`owner`, `verified`, `review_every`, `reconfirmed`) devient `owner (Notion)`, pour ne pas
  être prise pour lui. Une cellule de plus de 128 Kio est raccourcie dans la page d'index et le
  front matter, et le contrôle le signale.
- **Blocs** : les encadrés (`<aside>`) deviennent des encadrés, les blocs dépliables
  (`<details>`) des encadrés repliables (affichés ouverts), `<br>` un saut de ligne. Le reste du
  Markdown de Notion est gardé tel quel.

Ce qui est perdu : les vues, filtres, tris et formules des bases (seules les valeurs sont
exportées), les commentaires, les couleurs, les icônes et couvertures de page, les blocs
synchronisés (exportés en copies), les intégrations d'autres sites (gardées en liens), et ce que
Notion ne met pas lui-même dans l'export.

### Depuis Confluence

1. Dans Confluence, ouvrez l'espace, puis **Paramètres de l'espace → Exporter l'espace** (sur
   Confluence Data Center ou Server : **Outils de l'espace → Outils de contenu → Exporter**).
2. Choisissez **HTML**, puis **Export normal** (tout l'espace) ou **Export personnalisé**
   (quelques pages), et téléchargez le zip.
3. Envoyez-le tel quel à Synaptane.

L'export **XML** (destiné à un autre site Confluence) n'est pas lu : Synaptane le dit, et demande
l'export HTML.

Ce qui est converti :

- **Arborescence** : les pages placées sous une page vont dans son dossier, comme dans
  l'arborescence de Confluence (lue dans le `index.html` de l'export, sinon dans le fil d'Ariane
  de chaque page). Une page qui a des sous-pages ou des pièces jointes devient la page d'accueil
  de son dossier : `Procédures/README.md`. Les noms perdent l'identifiant de la page
  (`Procedures_65540.html` devient `Procédures`), avec `(2)` pour deux titres identiques.
- **Pages** : titres, paragraphes, gras, italique, barré, listes (de tâches aussi), tableaux,
  blocs de code avec leur langage, citations, liens et images deviennent du Markdown. Les
  panneaux info, astuce, remarque et avertissement deviennent des encadrés de même couleur
  (`> [!info]`, `> [!tip]`, `> [!warning]` pour une remarque jaune, `> [!danger]` pour un
  avertissement rouge), un bloc dépliable un encadré repliable (affiché ouvert), un panneau un
  encadré avec son titre, un statut un mot en gras, une émoticône son emoji. La ligne « Créé
  par … modifié le … » de la page est gardée en italique, et la liste de ses pièces jointes en
  bas. Une page dont le HTML dépasse 5 Mo n'est pas convertie : le contrôle la liste avec la
  raison.
- **Pièces jointes** : à côté de leur page, sous leur vrai nom (`attachments/65541/65601.png`
  devient `Procédures/Backup/schéma réseau.png`). Une pièce jointe dont la page n'est pas dans
  l'export reste où elle était, dans `attachments/<id de la page>/`.
- **Liens** : les liens vers une page ou une pièce jointe de l'export sont réécrits vers les
  nouveaux chemins, ainsi que les adresses du site Confluence portant l'identifiant d'une page de
  l'export (`…/pages/65540/…`, `viewpage.action?pageId=65540`). Les liens vers des pages absentes
  de l'export restent tels quels ; les ancres de Confluence et les liens qui sortent de l'export
  ne gardent que leur texte.

Ce qui est perdu : l'historique des pages, les commentaires en marge, les « j'aime », les
étiquettes, les restrictions de page, les mises en page et les couleurs, les macros qui calculent
leur contenu à l'affichage (tickets Jira, listes de pages, tables des matières : Synaptane
montre son propre plan) au-delà de ce que contient l'export, et les diagrammes d'applications
(draw.io, Gliffy) au-delà de leur image exportée. Les styles et icônes de l'export ne sont pas
importés.

Le HTML d'un export n'est pas digne de confiance : il est seulement lu, rien n'y est exécuté ni
chargé, scripts, cadres et formulaires sont retirés, les liens ne gardent que des schémas sûrs
(`http`, `https`, `mailto`…), et chaque chemin écrit vient de la disposition faite par
Synaptane, jamais de l'archive.

## Vérifier une page

Pour savoir, en lisant une page, si l'on peut s'y fier : qui en répond, quand elle a été
vérifiée, si elle est à revoir. Tout est dans le front matter de la page, donc dans Git :

```markdown
---
owner: ada            # identifiant d'un compte, ou nom libre
verified: 2026-10-03  # date de la dernière vérification
review_every: 6m      # 90d, 12w, 6m, 1y ; absent : celle de l'espace ; never : jamais
---
# Procédure de bascule
```

- **État** d'une page Markdown : **vérifiée** (une date, et l'échéance pas encore passée),
  **à revoir** (échéance passée) ou **jamais vérifiée**. Sans périodicité, ni la sienne ni
  celle de l'espace, une page vérifiée le reste. Les autres fichiers n'ont pas d'état.
- **Dans la page** : un badge discret dans la barre du haut, « Vérifiée le 3 oct. · Ada »
  en vert, ou « À revoir depuis le … » en ambre ; rien pour une page jamais vérifiée. Le
  détail (propriétaire, échéance, périodicité, « modifiée depuis la vérification ») est en tête
  du menu ⋮, et au survol du badge. Sur mobile, le badge se réduit à son pictogramme.
- **Marquer comme vérifiée** (contributeurs, menu ⋮) : relisez la page, puis le bouton
  inscrit la date du jour dans `verified` ; une page sans propriétaire devient la vôtre. Le
  même formulaire choisit le propriétaire (un compte de l'espace, ou un nom libre) et la
  périodicité ; « Enregistrer » les change sans dater. C'est un commit à votre nom
  (`Page verified: <chemin>`, ou `Page tracking updated: <chemin>` quand seuls le propriétaire
  ou la périodicité changent) qui ne touche que les lignes de ces trois champs : le reste du
  texte est gardé à l'octet près, et le bloc est créé en tête s'il n'existe pas. Un tel commit
  ne compte pas comme une modification pour « modifiée depuis la vérification » ; ceux écrits
  en français par les versions antérieures (sujets commençant par `Page vérifiée` ou
  `Suivi de la page mis à jour`) non plus : ils sont toujours reconnus.
- **Rangement** : un commit qui ne change que ces trois champs n'est pas une modification à
  examiner (le contenu est comparé, pas l'auteur ni le message : la même chose poussée
  depuis Git est reconnue aussi). Une page modifiée puis vérifiée reste à examiner.
- **Agents IA** : l'agent de rangement et les assistants (MCP) ne peuvent ni effacer ni
  changer ces champs en réécrivant une page : ils sont rétablis. Une page modifiée par l'agent
  reste vérifiée ; la page indique « modifiée depuis la vérification » (comparaison à la
  journée), à son propriétaire de la relire.
- **Listes et recherche** : une marque verte (vérifiée) ou ambre (à revoir) suit le nom de
  la page dans les dossiers et dans les résultats. La recherche classe un peu plus haut les
  pages vérifiées et à jour (+10 %), un peu plus bas les pages à revoir (−7 %) : le facteur
  est multiplicatif, la pertinence reste le premier critère.
- **Périodicité de l'espace** : *Paramètres → Général*, « Pages vérifiées : à revoir »
  (1, 3 ou 6 mois, 1 ou 2 ans, ou jamais). Une page qui porte `review_every` garde la sienne.
- **Signaler comme obsolète** (toute personne qui lit la page, menu ⋮) : dites ce qui n'est
  plus juste. La page affiche alors à tous un bandeau avec les signalements (qui, quand,
  pourquoi), la recherche la marque « signalée obsolète », l'onglet **Santé** de l'espace la
  liste, et les assistants le voient. Un signalement reste ouvert jusqu'à ce que quelqu'un
  **vérifie** la page (relue et jugée juste), qu'un contributeur l'**écarte** (la page est
  toujours juste), ou que son auteur le retire. Une page déplacée garde ses signalements.
- **Une page qui tient attend plus longtemps** : vérifiée de nouveau sans changement depuis sa
  vérification précédente, sa périodicité double (6 mois, puis 1 an, puis 2 ans au plus), et
  `reconfirmed: N` dans son front matter compte ces vérifications successives. Une
  modification de la page, par qui que ce soit, ou une périodicité choisie à la main repart
  de sa périodicité. « Sans changement » compare le texte à celui qu'avait la page lors de sa
  dernière vérification, pas les messages de commit.
- **Rappels** : l'accueil liste, dans « En attente », les pages à revoir dont vous êtes
  propriétaire (reconnu par votre identifiant ou votre adresse e-mail). Un e-mail groupé part
  au plus une fois par semaine, un jour ouvré, à chaque propriétaire de pages échues (adresse
  vérifiée ; décochez **Recevoir les e-mails des routines de mes espaces** dans *Mon compte*
  pour ne plus le recevoir).
  La routine **Contrôle de cohérence** reçoit la liste des pages à revoir.

La vocation de l'espace peut dire quelles pages doivent être vérifiées, et par qui (la
plupart des modèles le proposent dans leur section « Ce qui compte le plus », « What matters
most » en anglais).

## Santé du wiki

L'onglet **Santé** d'un espace (roue crantée, à côté d'Activité) fait un contrôle sans IA, au
dernier commit :
- **liens cassés**, avec une suggestion quand un fichier du même nom existe ailleurs ;
- **pages orphelines** : aucune autre page n'y mène (les `README.md` et `index.md` de dossier
  ne comptent pas) ;
- **pages vides** ou **sans titre** (ni `# Titre`, ni `title:` dans le front matter) ;
- **pages à revoir** : vérifiées, mais l'échéance de vérification est passée (propriétaire et
  date), et parmi les pages vérifiées, celles **sans propriétaire** ;
- **pages anciennes** : non modifiées depuis 3, 6, 12 ou 24 mois, d'après Git ;
- **tableaux qui divergent** : deux pages ont chacune un tableau sur les mêmes choses (même
  première colonne, au moins deux lignes en commun, une colonne de même nom) et donnent une
  valeur différente pour la même ligne, le NAS à 8 To ici et à 12 To là. L'une des deux est
  sans doute périmée. Des tableaux qui divergent plus qu'ils ne concordent décrivent des choses
  différentes (les fiches de deux sociétés), et deux notes datées (`veille-2026-11-02.md`)
  sont chacune vraies à leur date : ni les uns ni les autres ne sont signalés. Les routines
  d'agent reçoivent aussi ces valeurs ;
- **contrôles de l'équipe** : les pages qui enfreignent une des règles propres à l'espace (voir
  [Contrôles de l'équipe](#contrôles-de-léquipe)).

Chaque ligne mène à la page, et à « Modifier » pour les contributeurs. Le même contrôle est
servi par l'API REST (`GET /api/v1/organizations/{org}/spaces/{space}/health`, paramètres
`months` et `limit`). Le rangement le fait aussi en fin de passage, sur ce qu'il a écrit.

## Contrôles de l'équipe

Une équipe qui entretient son wiki finit par avoir ses propres règles, souvent écrites après
un incident : pas d'adresse e-mail de client dans une page, pas de chemin local comme
`/private/tmp`, un `type` dans le front matter de chaque page, des `tags` en liste YAML.
Synaptane les vérifie pour vous, sans IA. Elles sont dans le dépôt, dans
`.synaptane/checks.md`, lisibles et modifiables à la main comme une page.

Les administrateurs de l'organisation les écrivent dans **Paramètres → Vocation → Contrôles de
l'équipe** : **Partir de l'exemple** charge un exemple prêt à adapter (en français ou en
anglais), **Aperçu** liste les règles comprises, les lignes en erreur et le nombre de pages
actuelles qui enfreignent chaque règle ; **Enregistrer** fait un commit à votre nom, comme pour
la vocation (un conflit est détecté de la même façon). Elles se modifient aussi par un push, ou
par l'API REST (`GET` et `PUT …/checks`).

Chaque titre de niveau 2 est une règle. La liste juste en dessous la décrit ; tout autre texte
est un commentaire. Les clés restent en anglais :

```markdown
## Pas d'adresse e-mail de personne
- preset: email
- allow: acme.com
- level: block
- message: Écrivez le nom de la personne avec un lien vers sa page, pas son adresse.

## Les tags sont une liste YAML
- field: tags
- expect: list
- message: Écrivez `tags: [a, b]`, ou un `- tag` par ligne.

## Pas de chemin de travail local
- pattern: `/private/tmp|scratchpad`
- paths: notes/, projets/**/*.md
- message: Mettez un lien vers la page ou le fichier.
```

- **Ce qui est vérifié** : un motif (`pattern`) sur le texte de la page (une expression
  régulière, entre accents graves), un champ du front matter (`field`) avec ce qu'il doit être
  (`expect: present`, `list`, ou `date` pour une date sans heure) ou ce qu'il ne doit pas
  contenir (`pattern`), ou un préréglage (`preset`) de données personnelles : `email` (avec
  les domaines autorisés dans `allow`), `phone`, `card` (numéros de carte, avec la
  vérification de Luhn) et `iban` (avec sa clé de contrôle). Les préréglages lisent à
  travers les espaces insécables, les tirets et les caractères invisibles entre les chiffres.
- **Où** : toutes les pages Markdown par défaut ; `paths` limite la règle à des dossiers ou à
  des globs, relatifs à `content/` (`clients/`, `notes/**/*.md`), et `except` en retire.
- **Avec quelle sévérité** : `level: warn` (par défaut) signale ; `level: block` refuse
  l'écriture qui enfreint la règle, avec son `message`, depuis l'éditeur web, MCP, l'API REST,
  un import (le fichier est écarté, avec la raison), le rangement, la page de rapport d'une
  routine, et l'annulation d'un rangement ou du passage d'un assistant. Comme pour le
  détecteur de secrets, seul ce qu'une modification ajoute compte : une page qui enfreignait
  déjà une règle reste modifiable.
- **Ce qu'on en voit** : l'onglet **Santé** liste chaque page qui enfreint une règle, avec la
  ligne et un extrait. Les données personnelles (préréglages, ou règle avec `mask: yes`) sont
  masquées : `j•••@gmail.com`, `••• 1111`.

Un push Git ne peut pas être refusé : ce qu'il apporte apparaît dans **Santé**, et
l'[entretien de nuit](../admin/ai.fr.md#entretien-du-wiki), s'il est activé, reprend ce qui
compte le plus. Chaque nuit, l'agent de rangement reçoit ce que les membres ont enfreint depuis
le dernier passage : les règles `block` et les données personnelles, avec l'extrait masqué. Il
retire la donnée ou la remplace par une mention neutre. Il ne complète un champ qu'avec une
valeur que la page donne. Sinon, il laisse le problème, avec une raison. Les autres règles
`warn` sont seulement signalées ; `fix: yes` ou `fix: no` sur une règle en décide autrement.

L'agent de rangement reçoit aussi les règles avec ses consignes : une écriture qu'une règle
`block` refuse lui est renvoyée, et ce qu'il enfreint d'une règle `warn` revient dans son
contrôle de fin de passage, pour qu'il le corrige, puis dans le bloc **Qualité** de son
journal.

Les expressions suivent la [syntaxe RE2](https://github.com/google/re2/wiki/Syntax), qui
s'exécute en temps linéaire : aucun motif ne peut bloquer le serveur, quel que soit le texte.
Les assertions avant ou arrière et les références arrière ne sont donc pas disponibles. `^` et
`$` valent pour chaque ligne ; `(?i)` ignore la casse. Limites : 50 règles, 500 caractères
par motif, et un coût par motif : une longue répétition comptée comme `.{0,1000}` est refusée
(écrivez `.{0,200}`). **Santé** lit le premier Mo de chaque page ; une écriture est vérifiée en
entier. Un fichier en erreur n'est jamais bloquant : la règle
fautive est ignorée, les autres s'appliquent, et **Santé** nomme la ligne (un enregistrement
depuis Synaptane la refuse). Format complet :
[Format du dépôt](../reference/repository-format.fr.md#synaptanechecksmd).

## Chercher

- **Champ du menu** : cherche dans tous vos espaces.
- **Champ d'un espace** : cherche dans cet espace seulement.

Le comportement est celui de `grep` :
- recherche insensible à la casse et aux accents (« procedure » trouve « procédure »), au
  singulier comme au pluriel (« serveurs » trouve « serveur ») ; un mot de trois lettres ou
  moins ne correspond qu'à un mot entier ;
- chaque mot compte, sauf les mots vides (« de », « la », « the »…) ; les pages qui
  contiennent tous les mots, ou l'expression exacte, passent en premier ;
- **Expression régulière** pour des motifs, par exemple `sauv\w+ noct`.

Chaque résultat donne sa dernière modification : quand, par qui, avec un badge **IA** quand
une IA l'a écrite (le rangement nocturne, une routine, un assistant). Avec « vérifiée » ou
« à revoir », il dit quelle confiance accorder à la page.

Dans les espaces où la **recherche sémantique** est activée, elle trouve aussi les pages
proches par le sens (une question en anglais sur une page en français, une paraphrase…).
Ces résultats portent la mention « par le sens », ou « mots et sens ». Activer la
recherche sémantique sur un espace ne fait jamais reculer ses pages trouvées par les mots.

Les **notes déposées mais pas encore rangées** qui correspondent s'affichent au-dessus des
résultats, dans « En attente de rangement » : ce qui vient d'être déposé se retrouve
aussitôt.

La recherche porte sur :
- le Markdown et tous les fichiers texte ;
- le texte des PDF, y compris scannés grâce à l'OCR ;
- le texte des documents Word, Excel, PowerPoint et OpenDocument ;
- le texte des images (OCR) ;
- les noms de fichiers.

Le texte d'un nouveau PDF devient cherchable quelques secondes à quelques minutes après son
ajout, le temps de l'extraction. Vous ne voyez jamais de résultat d'un espace auquel vous
n'avez pas accès.

## Inbox et activité

- **Par e-mail** : quand le super-admin a mis en place la [capture par
  e-mail](../admin/email-capture.fr.md), la page **Inbox** d'un espace affiche son adresse à
  ceux dont les e-mails y seraient acceptés (adresse vérifiée, droit de déposer des notes ; un
  super-admin seulement dans ses espaces), par exemple `brain+acme.infra@example.com`, avec un
  bouton **Copier** (les premiers pas de l'espace aussi). Envoyez-y ou transférez-y un e-mail
  **depuis l'adresse vérifiée de votre compte** : il devient une note à votre nom, son objet
  pour titre, avec ses pièces jointes (10 au plus), et le rangement la classe. Le serveur de
  réception doit avoir authentifié votre e-mail (DKIM ou DMARC de votre domaine, voir le guide
  d'administration) : un expéditeur usurpé est refusé en silence. Un e-mail peut nommer
  plusieurs espaces, en À ou en Cc (jamais en Cci), et doit être daté de moins de 48 heures
  avant son arrivée (transférez un e-mail plus ancien : le transfert est daté du jour). Selon
  les réglages, vous recevez une réponse quand l'e-mail est refusé (pourquoi) ou à chaque fois
  (avec le lien vers la note).
- **Inbox** (barre de gauche) : filtrez par texte ou par type (notes, modifications, éléments
  laissés par le rangement). Sur une note, un contributeur peut la **modifier** (si le
  rangement l'avait laissée, elle redevient à traiter), la **déplacer** vers l'inbox d'un
  autre espace où il contribue, ou la **supprimer** (elle reste dans l'historique Git). Une
  page modifiée qui ne demande rien au rangement peut être **marquée comme vue**.
- **Aperçu** d'une note : au-dessus de son texte, en la déposant (dans l'inbox ou depuis
  **Déposer une information**, dans l'espace choisi) ou en la corrigeant, comme pour une page
  (voir [Écrire](#écrire-contributeurs)) : la note telle qu'elle se lira dans l'inbox, sans rien
  enregistrer (liens depuis la racine de l'espace, comme à la lecture de la note). Le côte à
  côte est proposé en corrigeant une note, pas dans le formulaire de dépôt, trop étroit. L'aperçu demande le même
  droit que déposer ou corriger la note (contributeur).
- **Activité** : tout ce qui change dans l'espace, d'après l'historique Git (pages, inbox,
  rangements), avec les commits poussés hors de Synaptane signalés. Chaque page touchée mène
  au détail de la modification.
  - **Personne ou IA** : une modification écrite par une IA porte un badge **IA** : le
    rangement nocturne, une routine, ou un assistant connecté en MCP, nommé (« Alice via
    Claude »). Une modification par l'API REST donne le nom du jeton. Le filtre **Écrit par
    l'IA** ne garde qu'elles.
  - En haut, la **part de l'IA** dans les modifications de pages des 30 derniers jours, avec le
    nombre pour chaque origine (rangement, routines, assistants, interface web, API REST,
    poussé avec Git).
  - Un commit poussé d'ailleurs compte toujours comme **poussé avec Git**, quoi que dise son
    message : seuls les commits de Synaptane lui-même sont lus pour leur origine.

