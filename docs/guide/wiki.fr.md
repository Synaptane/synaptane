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

## Écrire (contributeurs)

- **Modifier** une page texte, **Nouvelle page**, **Ajouter** un fichier (25 Mo maximum, de
  tout type : voir [Fichiers autres que Markdown](#fichiers-autres-que-markdown)),
  **Déplacer** (ou renommer) ou **Supprimer ce fichier** (menu ⋮).
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
- **pages anciennes** : non modifiées depuis 3, 6, 12 ou 24 mois, d'après Git.

Chaque ligne mène à la page, et à « Modifier » pour les contributeurs. Le même contrôle est
servi par l'API REST (`GET /api/v1/organizations/{org}/spaces/{space}/health`, paramètres
`months` et `limit`). Le rangement le fait aussi en fin de passage, sur ce qu'il a écrit.

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

- **Inbox** (barre de gauche) : filtrez par texte ou par type (notes, modifications, éléments
  laissés par le rangement). Sur une note, un contributeur peut la **modifier** (si le
  rangement l'avait laissée, elle redevient à traiter), la **déplacer** vers l'inbox d'un
  autre espace où il contribue, ou la **supprimer** (elle reste dans l'historique Git). Une
  page modifiée qui ne demande rien au rangement peut être **marquée comme vue**.
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

