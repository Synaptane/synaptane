[English](wiki.md) | Français

# Lire, écrire et chercher

Chaque espace affiche le dossier `content/` de son dépôt Git sous forme de wiki.
- L'arborescence est à gauche et la page à droite.
- L'onglet **Dépôt et membres** donne l'état de synchronisation et la liste des membres.

## Lire

- **Markdown** : rendu avec titres, tableaux, cases à cocher et code coloré.
  - Les liens relatifs (`[guide](../infra/proxmox.md)`) mènent à la page visée. Un lien qui
    commence par `/` part de la racine de `content/`.
  - Les images relatives s'affichent.
- **Dossier** : son `README.md` ou son `index.md` sert d'introduction, suivi de la liste des
  fichiers.
- **Autres fichiers** :
  - PDF : visionneuse du navigateur ;
  - images : affichées ;
  - fichiers texte et code : coloration syntaxique ;
  - autres types : téléchargement.
- **Pages qui pointent ici** : les pages Markdown qui ont un lien vers la page affichée.
- **Voir aussi** : les pages proches, découvertes sans rien chercher (voir ci-dessous).
- **Historique** : chaque modification, avec son auteur et le détail des lignes changées.

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

Le HTML et le SVG d'un dépôt ne s'exécutent jamais dans Synaptane : ils sont proposés au
téléchargement.

## Écrire (contributeurs)

- **Modifier** une page texte, **Nouvelle page**, **Ajouter** un fichier (25 Mo maximum),
  **Déplacer ou supprimer**.
- Chaque enregistrement produit un commit à votre nom, poussé aussitôt sur le dépôt.
- **Déplacer** garde les liens justes, dans le même commit : les liens relatifs de la page
  déplacée sont recalculés pour son nouvel emplacement, et les pages qui pointaient vers elle
  sont réécrites (liens relatifs, et `[[dossier/nom]]` qui ne mèneraient plus à elle). Un
  `[[nom]]` qui la trouve toujours n'est pas touché, et seul le lien change : le reste de la
  page reste tel quel. Le message affiché après le déplacement liste les pages mises à jour.
  Le MCP, la REST et l'agent de rangement déplacent de la même façon.
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
  périodicité ; « Enregistrer » les change sans dater. C'est un commit à votre nom (« Page
  vérifiée : … ») qui ne touche que les lignes de ces trois champs : le reste du texte est
  gardé à l'octet près, et le bloc est créé en tête s'il n'existe pas.
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
- **Rappels** : l'accueil liste, dans « En attente », les pages à revoir dont vous êtes
  propriétaire (reconnu par votre identifiant ou votre adresse e-mail). Un e-mail groupé part
  au plus une fois par semaine, un jour ouvré, à chaque propriétaire de pages échues (adresse
  vérifiée ; décochez « e-mails des routines » dans *Mon compte* pour ne plus le recevoir).
  La routine **Contrôle de cohérence** reçoit la liste des pages à revoir.

La vocation de l'espace peut dire quelles pages doivent être vérifiées, et par qui (les
modèles le proposent dans « Ce qui compte le plus »).

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
servi par la REST (`GET /api/v1/organizations/{org}/spaces/{space}/health`, paramètres
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

