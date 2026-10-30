[English](ai.md) | Français

# Intelligence artificielle et rangement

Chaque nuit, un agent IA range l'inbox de chaque espace dans son wiki. Il utilise le
fournisseur d'IA de l'organisation.

## Choisir un fournisseur (page de l'organisation → Intelligence artificielle)

| Fournisseur | Identification | Remarques |
|---|---|---|
| Anthropic (Claude) | Clé d'API (console Anthropic) | Modèle par défaut : `claude-sonnet-5-5`, réflexion moyenne (bon rapport qualité-prix pour ranger ; `claude-opus-5-5` pour des espaces exigeants). Repli automatique en cas de refus par un filtre de sécurité. |
| OpenAI, Mistral | Clé d'API | Modèle à indiquer (`mistral-large-latest` par défaut pour Mistral). |
| Ollama | Aucune (adresse locale) | Le modèle doit savoir appeler des outils. |
| Compatible OpenAI | Adresse et, au besoin, clé | vLLM, LM Studio, passerelles… |
| **Abonnement Claude (Claude Code)** | Connexion du CLI sur le serveur | Réservé au super-admin. Voir plus bas. |
| **Abonnement ChatGPT (Codex)** | Connexion du CLI sur le serveur | Réservé au super-admin. Voir plus bas. |

- La clé d'API est chiffrée et n'est jamais réaffichée. Elle reste liée à son fournisseur
  et à son adresse : changer l'un ou l'autre oblige à la ressaisir (ou à la retirer).
- **Adresse de l'API** personnalisée (Ollama, compatible OpenAI, passerelle) : seul le
  super-admin peut la changer, car elle fait appeler le serveur vers l'adresse choisie.
- **Tester la connexion** fait un vrai appel d'outil. Pour une clé d'API, le test se fait
  aussitôt ; pour un abonnement, dans le worker (voir plus bas).
- Chaque espace peut choisir un autre modèle (**Paramètres → Rangement** de l'espace).

## Utiliser un abonnement Claude ou ChatGPT

Au lieu d'une clé d'API facturée à l'usage, l'agent peut tourner sur un abonnement. C'est
alors le **CLI officiel** de l'éditeur (`claude` ou `codex`) qui fait tourner l'agent, et
Synaptane ne voit jamais les identifiants.

1. Installez le CLI sur la machine du worker. L'image publiée
   (`ghcr.io/synaptane/synaptane`) n'en contient aucun, car Claude Code ne peut pas être
   redistribué. Une image que vous construisez vous-même contient les deux
   (`AGENT_CLIS=true`, valeur par défaut du Dockerfile) : dans `.env`,
   `COMPOSE_FILE=compose.yaml:compose.build.yaml` et `SYNAPTANE_IMAGE=synaptane:local`, puis
   `GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build`. Voir
   [Construire l'image vous-même](installation.fr.md#construire-limage-vous-même).
2. Connectez-le une fois, **dans le worker**, par le flux de l'éditeur :
   - avec Docker : `docker compose exec worker claude auth login`, ou
     `docker compose exec worker codex login --device-auth` ;
   - sans Docker : `claude auth login`, ou `codex login`, avec le compte système qui lance
     `synaptane worker` (précédé de `HOME=…` si `SYNAPTANE_AGENT_HOME` est réglée : la
     page Intelligence artificielle affiche la commande exacte).
3. En super-admin, choisissez « Abonnement Claude » ou « Abonnement ChatGPT » pour
   l'organisation, puis **Tester la connexion**.

`synaptane doctor` indique si les CLI sont installés et s'ils respectent le contrat
d'isolation (voir « Sécurité »).

**Où vit la connexion.** Le CLI la garde dans son propre `HOME`, donné par
`SYNAPTANE_AGENT_HOME` :
- avec Docker, `/agents`, sur le volume `synaptane-agents`, monté **seulement** dans
  `worker` (dont c'est aussi le `HOME`, d'où la commande simple ci-dessus). Le conteneur
  `app`, exposé à Internet, ne la voit pas ; elle n'est pas non plus dans le volume des
  données ni dans les sauvegardes : après une restauration sur un autre serveur, reconnectez
  le CLI. `docker compose down -v` l'efface ;
- sans Docker, sans cette variable, le `HOME` du compte qui lance le worker.

Synaptane ne lit, ne copie, ne déplace ni ne stocke jamais ces identifiants : seul le CLI
officiel les manipule.

**Tester la connexion** se fait **dans le worker**, seul à avoir le CLI connecté : la page
affiche « Test en cours… » et se met à jour seule (ou `POST …/organizations/{org}/ai/test`
répond 202, et `GET …/ai` donne `test_pending`, puis `test_ok` et `test_message`). Le test
passe par la file rapide et dure au plus quelques minutes. Si « Test en cours… » ne change
pas, vérifiez que le worker tourne (`docker compose ps`). Pour Claude, c'est un vrai
mini-appel au plus petit modèle : une session expirée est ainsi détectée, même quand
`claude auth status` se dit connecté.

**Mise à jour depuis une version où la connexion était dans `/data/home`.** Elle était
alors dans le volume des données, partagé avec le conteneur web. Synaptane ne la déplace pas
(seul le CLI manipule les identifiants) ; `synaptane doctor` et la page Intelligence
artificielle signalent l'ancien dossier tant qu'il existe (sans rien y lire). Une fois pour
toutes :
1. reconnectez le CLI dans le worker : `docker compose exec worker claude auth login`
   (ou `codex login --device-auth`) ;
2. **Tester la connexion** sur la page Intelligence artificielle ;
3. supprimez vous-même l'ancienne connexion :
   `docker compose exec app rm -rf /data/home/.claude /data/home/.claude.json /data/home/.codex`.

Un abonnement n'a pas de coût à l'usage : la page ne demande ni tarif ni budget. À la place,
Synaptane relève le **quota** de l'abonnement, c'est-à-dire la part utilisée des fenêtres
de 5 heures et de 7 jours :
- avec Claude, à chaque passage de rangement, d'après les relevés que Claude Code émet
  (`rate_limit_event`, non documenté par Anthropic : le relevé peut disparaître avec une
  mise à jour du CLI, le rangement continue alors sans) ;
- le journal du rangement l'affiche (« 5 h : 3 % → 5 % · 7 jours : 29 % »), le test de
  connexion aussi ;
- au-delà de 90 % d'une fenêtre, les administrateurs de l'organisation sont prévenus.

Avec ChatGPT (Codex), le quota n'est pas encore relevé.

**Sécurité.** Pendant un passage, le CLI ne dispose que des outils de rangement de
Synaptane, fournis par un serveur MCP local éphémère protégé par un jeton à usage unique.
Il n'a ni shell, ni accès aux fichiers, ni web : un contenu malveillant déposé dans l'inbox
ne peut pas lire le serveur.

Cette garantie tient à des options de ligne de commande (`--tools ""` et
`--strict-mcp-config` pour Claude Code, avec le chargement différé des outils coupé par
`ENABLE_TOOL_SEARCH=false` ; `--disable …`, bac à sable en lecture seule et approbations
refusées pour Codex). Une montée de version peut les renommer ou en changer le
sens : `synaptane check-clis` le **vérifie automatiquement**, sans connexion ni appel au
modèle, et sans réseau :
- l'aide de Claude Code cite encore chaque option utilisée, avec le même sens ;
- chaque fonction coupée de Codex existe encore ; toute autre fonction active par défaut doit
  figurer dans une liste d'autorisation (`cli_contract.CODEX_ALLOWED`) : une fonction
  nouvelle fait échouer le contrat tant qu'elle n'est pas classée (dans le doute, coupée) ;
- surtout, la commande exacte d'un passage est lancée contre un **faux fournisseur local**
  (127.0.0.1, clé factice), qui note les outils que le CLI propose au modèle puis répond par
  une erreur. Seuls ceux de Synaptane sont admis ; pour Codex s'y ajoutent la recherche
  d'outils (limitée au serveur de Synaptane, vérifié), la lecture des ressources MCP
  (Synaptane n'en publie pas), `request_user_input` (sans réponse en mode non interactif) et
  `apply_patch` (refusé par le bac à sable en lecture seule ; aucune option ne le retire).

Ce contrat tourne dans la CI à chaque construction de l'image (avec les CLI), et chaque
lundi avant que le workflow de montée des CLI n'ouvre sa PR : s'il échoue, la PR le dit en
tête (« contrat d'isolation rompu »). `synaptane doctor` le signale aussi (en avertissement).

Limite connue : le CLI tourne sous le même compte système que le worker, qui peut lire
`/data` (clé maître, dépôts). Une faille dans le CLI lui-même pourrait donc suffire à lire
le serveur. Le lancer sous un compte distinct, sans ce droit de lecture, est prévu.

Conditions des éditeurs (octobre 2026) :
- **OpenAI** autorise l'usage de l'abonnement ChatGPT dans des outils tiers.
- **Anthropic** autorise la connexion d'un abonnement au **binaire Claude Code officiel**.
  Elle interdit à une application tierce de manipuler ces identifiants, et précise que les
  limites des abonnements Pro et Max supposent un usage individuel ordinaire.

Pour une organisation d'entreprise partagée par plusieurs personnes, préférez une clé d'API,
ou un plan Team ou Enterprise.

## Rangement nocturne (par espace)

Les réglages sont dans **Paramètres → Rangement** de l'espace ; le journal et **Ranger
maintenant**, dans son onglet **Rangement** (le bouton est aussi dans l'onglet Inbox).

- **Rangement quotidien** (heure et fuseau) : 02:00 à Paris par défaut. Le passage n'a lieu que s'il y a des
  éléments dans l'inbox. **Ranger maintenant** le lance aussitôt (5 fois par personne et par
  espace sur 24 heures, 20 pour un admin de l'organisation : voir
  [Coût et budget](#coût-et-budget)).
- **Rangement au fil de l'eau** (case « Ranger aussi au fil de l'eau, peu après chaque
  dépôt », décochée par défaut) : un passage part N minutes après un dépôt (**Délai après un
  dépôt**, 15 minutes par défaut, de 1 à 1 440), en plus de celui de l'heure planifiée. Les
  notes déposées pendant ce délai rejoignent le même passage. Rien n'est programmé quand le budget du mois est épuisé.
- **Avis aux déposants** : après chaque passage commité, la personne qui a déposé une note
  reçoit un e-mail (un seul par passage) qui dit où elle a été rangée, avec les liens des
  pages, ou pourquoi elle a été laissée. Actif par défaut, désactivable par chacun dans
  **Mon compte** ; seulement vers une adresse vérifiée, et si la personne lit encore
  l'espace. Une note laissée de nouveau au passage suivant ne renvoie pas d'avis.
- **Vocation et consignes propres à l'espace** : voir
  [ci-dessous](#vocation-et-consignes-de-rangement).
- **Ce que fait l'agent** :
  - il lit l'arborescence et les pages, ainsi que le texte des pièces jointes (extrait des
    PDF, documents Office et images dans le passage, et gardé pour les passages suivants) ;
  - il cherche par les mots et, si la recherche par le sens est activée pour l'espace, par le
    sens : une page qui dit la même chose avec d'autres mots est complétée plutôt que
    doublée ;
  - en lisant une note, il reçoit ses **pages proches** (même sujet, mots en commun : voir
    [Voir aussi](../guide/wiki.fr.md#voir-aussi--les-pages-proches)), et pour une page
    modifiée par quelqu'un, celles de la page, doublons possibles signalés ; l'outil
    `related` les donne pour n'importe quelle page. Les pages proches « pas encore liées »
    l'aident à relier. Une note « Relier X et Y », déposée depuis le panneau « Voir aussi »
    du wiki, lui demande seulement d'ajouter le lien ;
  - il juge la fraîcheur d'une page par ses trois dernières modifications (`history`), et
    écrit une page longue en plusieurs fois (`append_page`) ;
  - il crée, complète, déplace, fusionne ou supprime des pages, et range les pièces jointes ;
  - il relie les pages : liens vers les pages citées, index du dossier (`README.md`), au plus
    trois liens retour ; il garde les liens wiki `[[nom]]`, qui suivent une page déplacée ;
  - quand il déplace une page, ses liens et ceux qui pointent vers elle sont réécrits aussitôt
    (le même calcul que pour un déplacement depuis le web) ;
  - en fin de passage, un contrôle sans IA lui renvoie une fois les liens cassés qu'il a
    introduits et les pages créées sans lien entrant, pour un tour de correction ; ce qui
    reste est noté « À vérifier » dans le journal du passage, sans rien bloquer ;
  - il clôt chaque élément, ou le laisse dans l'inbox avec une raison (il sera retenté).
- **Garde-fous**, sans aucune validation humaine :
  - l'agent ne réécrit une page qu'après l'avoir lue en entier, et ne clôt un élément
    qu'après l'avoir lu ; une pièce jointe n'écrase jamais un fichier ;
  - **au plus 10 suppressions par passage** : au-delà, elles sont refusées, l'agent laisse
    les éléments concernés dans l'inbox, et les administrateurs sont prévenus (e-mail,
    webhook `space.alert`) ;
  - le contenu de l'inbox (texte, titre, suggestion, provenance), les modifications faites par
    d'autres et les pages lues sont présentés à l'agent comme des données, jamais comme des
    consignes, entre des balises qu'un texte déposé ne peut pas refermer. Les consignes
    propres à l'espace ne peuvent pas lever cette règle ;
  - l'agent ne doit rien inventer : en cas de doute, il laisse l'élément avec sa question ;
  - le texte extrait d'une pièce jointe passe au détecteur de secrets : s'il en contient un
    probable, il n'est pas transmis au modèle, la pièce jointe n'est pas rangée et l'élément
    est laissé avec la raison ;
  - avec Claude par clé d'API (sans passerelle), les outils sont stricts : le modèle ne peut
    pas leur passer d'argument inconnu ou mal typé ;
  - les outils d'écriture lui signalent les liens cassés, et `move` les pages qui pointent
    encore vers l'ancien chemin ;
  - chaque élément clos indique les pages où il a été intégré (journal : « → page », ou
    « aucune page indiquée ») ;
  - **changements importants** : jamais bloqués (Git garde l'historique, et le passage
    s'annule), mais signalés aux administrateurs (alerte `big_changes`) quand une page
    existante perd au moins 30 % de son contenu et au moins 300 octets, quand un passage
    déplace au moins 10 pages ou touche au moins 20 pages existantes. Le journal note chaque
    page fortement réduite ;
  - une réponse coupée par la limite de longueur n'est jamais exécutée.
- **Imports en masse** : un commit qui touche plus de 20 pages (import, réorganisation)
  devient un seul élément « Import : N pages » ; l'agent vérifie l'organisation d'ensemble
  au lieu de relire chaque page.
- **« Laissé » ou « non traité »** : un élément *laissé* l'est par décision de l'agent, avec
  sa raison ; il n'est repris que par un rangement manuel ou après modification. Un élément
  présenté à un passage qui s'arrête avant de le clore (erreur du fournisseur, plafond,
  budget atteint) est *non traité* : le travail déjà fait est commité, l'élément reste
  nouveau et sera repris au passage suivant, avec un compteur (`tidy_attempts` dans
  l'en-tête d'une note, en base pour une page modifiée). Après 3 passages interrompus, il
  est laissé avec la raison. Dans les deux cas (arrêt, élément abandonné), les
  administrateurs reçoivent l'alerte `tidy_interrupted`. Un passage interrompu n'enchaîne
  pas aussitôt un autre passage.
- **Langue** : les consignes de l'agent sont en anglais, quelle que soit la langue de
  l'espace. Il écrit les pages, les synthèses et les raisons dans la langue des pages
  existantes de l'espace (détectée sur sa page d'accueil et ses plus longues pages), ou dans
  celle que fixe sa vocation ; dans un espace encore sans page, dans la langue de la note
  qu'il range. Une page qu'il complète garde sa langue ; dans un espace qui mêle les langues,
  il garde celle de la page ou du dossier où il écrit.
- **Un seul commit par passage**, au nom de `Synaptane tidy-up` (sujet `Tidy-up: …`), poussé
  aussitôt ; les commits des versions antérieures, au nom de `Synaptane – rangement`, restent
  reconnus comme des passages de rangement. Si
  quelqu'un a modifié une page pendant le passage, les deux versions sont fusionnées, fichier
  par fichier ; en cas de conflit, la version humaine est gardée pour ce fichier (journal :
  « conflit »), le reste est commité, et les éléments rangés dans cette page redeviennent
  non traités. Rien n'est écrasé, et **le modèle n'est jamais relancé** pour un conflit
  d'écriture. Si la copie locale est occupée (synchronisation en cours), le commit attend
  jusqu'à 35 minutes.
- **Un seul passage à la fois par espace**, même avec plusieurs workers : une demande qui
  arrive pendant un passage est remise en file deux minutes plus tard. « Ranger maintenant »
  remplace un passage planifié en attente : il tourne bien en mode manuel.
- **Limites par passage** : 25 éléments, 120 étapes, 2 millions de jetons, ou 400 appels
  d'outils avec un abonnement. S'il reste des éléments nouveaux, un **passage enchaîné**
  suit aussitôt (8 au plus), jusqu'à vider l'inbox ou atteindre le budget du mois.
- **Journal** (onglet **Rangement** de l'espace) : opérations, résumé, jetons et coût
  estimé de chaque passage, échecs et nouveaux essais compris.
- **Bloc « Qualité » du journal** : mesuré sans IA, juste avant le commit, pour chaque
  passage terminé (rien pour un passage échoué ou antérieur à la mesure). Le titre dit
  « rien à signaler » ou le nombre de points à vérifier ; le bloc déplié donne :

  | Ligne | Ce qu'elle mesure | Que faire si elle s'allume |
  |---|---|---|
  | Fichiers créés, modifiés, déplacés, supprimés | ampleur du passage | comparer au nombre d'éléments rangés |
  | Liens cassés avant / après, **introduits** | liens qui ne mènent plus nulle part, dans les pages touchées et celles qui visaient une page déplacée ou supprimée | corriger les liens cités, ou annuler le passage |
  | Créés sans lien entrant | page ou pièce jointe que rien ne relie au reste du wiki | ajouter un lien depuis la page du sujet |
  | Absents de l'index du dossier | page absente du `README.md` de son dossier (pour un README, de celui du dossier parent) | compléter l'index |
  | Pages créées sans titre de niveau 1, vides | forme | compléter la page |
  | Éléments rangés sans page indiquée | élément clos sans dire où il est intégré : doublon légitime, ou information perdue | vérifier l'élément dans l'historique de l'inbox |
  | Pages fortement réduites, texte retiré | contenu supprimé des pages gardées (un déplacement compare à l'ancien chemin) et des pages supprimées | relire le diff du commit |
  | Éléments laissés | part des éléments laissés dans l'inbox | une part élevée et durable signale des consignes floues |
  | Par élément rangé | jetons, tours et coût par élément rangé | comparer deux modèles ou deux consignes |

  Rien n'est bloqué : les changements importants sont aussi signalés aux administrateurs
  (alerte `big_changes`, ci-dessus). Les mêmes valeurs sont dans le champ `metrics` de
  `GET …/tidy/runs` (REST), pour les suivre dans le temps. Pour changer les consignes ou le
  modèle sans saut dans le noir, voir le [banc d'évaluation](../dev/eval.fr.md).
- Sans fournisseur d'IA configuré, le rangement planifié ne tourne pas (pas d'échec inscrit
  chaque nuit).

## Vocation et consignes de rangement

On ne range pas de la même façon le second cerveau d'une équipe d'infrastructure, d'une
équipe marketing ou d'une famille. Chaque espace peut donc décrire, dans **Paramètres →
Vocation** (administrateurs de l'organisation), deux textes Markdown versionnés dans son
dépôt :

| | Vocation | Consignes de rangement |
|---|---|---|
| Fichier | `.synaptane/vocation.md` | `.synaptane/prompts/tidy.md` (ancien nom `rangement.md` encore lu s'il est seul) |
| Dit | **ce qu'est** l'espace | **comment ranger** |
| Contenu | à quoi il sert et pour qui ; ce qu'on y range et ce qu'on n'y range pas ; l'organisation des dossiers ; les conventions (langue, ton, noms de fichiers, format des pages : fiches, procédures, comptes rendus…) ; ce qui compte le plus (exactitude, fraîcheur, confidentialité…) ; le vocabulaire | des règles précises pour l'agent : « une page par fournisseur dans `fournisseurs/` », « ne jamais renommer les procédures »… |
| Lue par | le rangement, les routines, les assistants IA (MCP, REST) | l'agent de rangement seul |

- **Modèles** prêts à adapter : équipe infrastructure / ops, équipe marketing, équipe
  produit, projet client, second cerveau personnel, famille, recherche d'emploi, vierge.
  Chacun existe en anglais et en français, et il est proposé dans la langue de la personne
  qui l'utilise (langue du compte ; de même pour `GET /vocation-templates` en REST). Un
  modèle pré-remplit l'éditeur (avec confirmation s'il contient déjà un autre texte) ; rien
  n'est enregistré avant **Enregistrer**. **Aperçu** montre le rendu sans enregistrer.
- **Enregistrer** fait un commit au nom de la personne (`Space purpose updated` ou `Tidy-up
  rules updated` ; `… removed` quand le texte est vidé), sous le verrou de l'espace, puis une
  synchronisation ; la page montre les cinq dernières modifications (date, auteur). Git reste
  la source : les deux fichiers se modifient aussi par un push. Si le fichier a changé depuis
  l'ouverture de la page (push, autre administrateur), l'enregistrement est refusé et le
  choix de remplacer est demandé. 20 000 caractères au plus ; un secret probable est refusé
  (comme pour les pages) ; un texte vide retire le fichier.
- **À la création d'un espace**, un modèle de vocation peut être choisi. Le dépôt n'est pas
  encore cloné : le texte est mis en attente (fichier `<id>.attente.json` à côté de la copie
  locale, dans `repos/`) et la **première synchronisation** l'écrit dans le dépôt, au nom de
  la personne, juste après la préparation de l'espace. Un dépôt qui a déjà sa propre vocation
  la garde. Tant que l'espace n'est pas synchronisé, la page Vocation (et `PUT …/vocation`)
  modifie ce texte en attente.
- **Dans le rangement**, la vocation est donnée à l'agent dans ses consignes système, dans un
  bloc « Vocation de l'espace, définie par ses administrateurs », après les consignes par
  défaut et **avant** les consignes propres à l'espace, qui priment sur elle pour la manière
  de ranger. Elle est lue au commit de départ du passage, sans les commentaires HTML des
  modèles. L'agent range selon l'organisation des dossiers qu'elle décrit, suit ses
  conventions et laisse dans l'inbox, avec la raison, ce qu'elle exclut. Ni la vocation ni
  les consignes ne lèvent les garde-fous : le contenu déposé reste une donnée, jamais une
  consigne, et le plafond de suppressions s'applique toujours.
- **Pour les assistants** : `list_spaces` (MCP) donne le début de la vocation de chaque
  espace, et `read` avec le chemin `.synaptane/vocation.md` la donne en entier. Les
  consignes du serveur MCP demandent de la lire et de la suivre avant d'écrire dans un
  espace.

## Routines d'agent (par espace)

Une **routine** est un agent IA planifié qui relit le wiki d'un espace et produit une sortie :
un rapport, des corrections proposées, un résumé envoyé à l'équipe. **Paramètres →
Routines** de l'espace (`GET …/spaces/{space}/routines` en REST). Les administrateurs de
l'organisation créent, modifient, lancent et suppriment ; les contributeurs voient les
routines et leur journal ; les lecteurs ne les voient pas (ils lisent les pages de rapport
comme toute page).

Une routine, c'est :

- des **consignes** : ce que l'agent doit faire et ce que doit contenir sa sortie ;
- un **planning** : chaque jour, chaque semaine (un jour) ou chaque mois (jour 1 à 28), à une
  heure locale de l'espace (le fuseau du rangement). Une routine créée, réactivée ou
  replanifiée après son heure du jour attend le prochain créneau ; **Lancer maintenant** la
  fait tourner aussitôt ;
- un **périmètre** : des dossiers du wiki, ou tout l'espace. L'agent ne lit que là
  (arborescence, pages, recherche, rétroliens, pages proches, historique) ;
- des **sorties**, au moins une :
  - une **page de rapport**, réécrite à chaque passage (`rapports/coherence.md` par exemple).
    Une modification faite à la main y sera remplacée : la page le dit en pied ;
  - des **éléments dans l'inbox** (10 au plus par passage), un par correction à faire, avec
    les pages concernées : le rangement de la nuit les traite avec ses garde-fous (plafond
    de suppressions, fusion, annulation) ;
  - un **e-mail** aux administrateurs de l'organisation, ou à tous les membres de l'espace :
    seulement vers une adresse vérifiée, aux comptes actifs qui lisent l'espace et qui n'ont
    pas décoché « Recevoir les e-mails des routines » dans **Mon compte** (préférence
    distincte des avis de rangement, active par défaut, rappelée dans chaque e-mail) ;
  - le **webhook** `routine.done`, avec le texte de sortie (webhooks de l'organisation).

### Modèles fournis

Ajoutés en un clic (**Activer**), ou après les avoir ajustés (**Personnaliser d'abord**) :

- **Contrôle de cohérence**, chaque lundi à 06:00 : relit les pages modifiées depuis le
  dernier passage, puis les pages qui s'y rattachent (40 au plus ; une semaine calme, les
  plus anciennes du périmètre), et repère les contradictions entre pages, les informations
  probablement périmées (dates passées, « en cours », versions) et les doublons de sujet. Il
  reçoit aussi le contrôle de santé du wiki (liens cassés, orphelines, pages anciennes : voir
  l'onglet **Santé**) et, calculées sans IA, les paires de pages au contenu très proche parmi
  les 20 premières pages à relire (doublons possibles, qu'il vérifie en les lisant). Sortie :
  la page `rapports/coherence.md` et un élément d'inbox par problème à corriger, avec les
  pages et extraits concernés. Il ne redépose pas un problème déjà déposé et pas encore
  rangé.

  **Une fausse alerte ne revient pas.** Quand un élément déposé par une routine quitte
  l'inbox, ce qu'il est devenu est retenu, lié aux pages concernées telles qu'elles étaient
  alors :
  - **Pas un problème** (bouton sur l'élément, avec la raison : contributeurs ; REST :
    `DELETE …/inbox/{name}?not_a_problem=…`) ;
  - supprimé de l'inbox ;
  - traité par le rangement (rangé, avec son résumé).

  À ses passages suivants, la routine reçoit ces verdicts et ne redépose pas le problème, tant
  qu'aucune de ses pages n'a changé ; une modification de l'une d'elles rend le verdict
  caduc. Un élément de même titre est refusé d'office. Les verdicts sont gardés un an (90
  jours pour un problème lié à aucune page).
- **Résumé hebdomadaire**, chaque vendredi à 16:00 : ce que l'équipe a appris et décidé dans
  la semaine, d'après l'historique Git du wiki (pages créées, très modifiées) et les passages
  du rangement (éléments rangés, laissés). Sortie : la page `rapports/semaine.md` et un
  e-mail aux membres de l'espace.

Les deux modèles existent en anglais et en français : le formulaire est pré-rempli avec les
consignes et la page de rapport dans la langue de la personne qui crée la routine
(`reports/consistency.md` et `reports/weekly-digest.md` en anglais), et une routine activée en
un clic prend aussi cette langue. Une **routine vierge** prend vos propres consignes (revue
d'un dossier, briefing du lundi, FAQ d'accueil régénérée…).

### Sécurité

- **Une routine n'écrit nulle part ailleurs que dans ses sorties.** Elle n'a aucun outil
  d'écriture du wiki : la page de rapport a un chemin fixé par la routine (l'agent ne le
  choisit pas), les éléments vont dans l'inbox, et les vraies corrections passent par le
  rangement. C'est aussi ce qui limite l'injection : un texte malveillant lu dans une page ne
  peut, au pire, que proposer un élément d'inbox, que le rangement examine.
- Tout ce que l'agent lit (pages, résultats de recherche, historique, activité, contrôle de
  santé, vocation de l'espace) lui est présenté comme une donnée, jamais une consigne, entre
  des balises qu'un texte ne peut pas refermer. Les consignes de la routine viennent d'un
  administrateur et restent soumises à ces règles.
- Un secret probable (mot de passe, clé, jeton) dans le texte de sortie ou un élément est
  refusé à l'agent, qui doit le retirer.
- **Vocation de l'espace** : si le dépôt contient `.synaptane/vocation.md`, l'agent la reçoit
  en contexte, pour juger de ce qui est dans le sujet.
- **Langue** : comme pour le rangement, les consignes de l'agent sont en anglais ; il écrit le
  texte de sortie, les éléments d'inbox et sa synthèse dans la langue des pages de l'espace,
  ou dans celle que fixe sa vocation ; dans un espace encore sans page, dans la langue des
  consignes de la routine.

### Exécution, coût et journal

- Le worker regarde chaque minute ce qui est dû. Les routines passent par la même file que le
  rangement (tâches lentes) et **jamais en même temps qu'un rangement du même espace** (même
  verrou) : si l'espace est occupé, la demande est reprise deux minutes plus tard.
- Mêmes fournisseurs que le rangement (clé d'API ou abonnement Claude/ChatGPT par le CLI
  officiel ; Synaptane ne lit jamais les identifiants d'un abonnement).
- **Limites par passage** : 60 étapes, un million de jetons, 200 appels d'outils avec un
  abonnement.
- **Budget** : le coût des routines compte dans le budget mensuel de l'organisation (avec le
  rangement). Budget atteint : la routine n'est pas lancée ; atteint pendant le passage, elle
  s'arrête. Dans les deux cas l'échec est journalisé et rien n'est publié.
- **Un passage interrompu ne publie rien** (fournisseur en erreur, plafond, budget) : pas de
  rapport à moitié écrit. Son coût est compté, et les administrateurs reçoivent l'alerte
  `routine_failed` (e-mail, webhook `space.alert`). Une erreur passagère du fournisseur avant
  tout travail est retentée par la file, sans alerte ; le quota d'un abonnement est relevé
  comme pour le rangement (alerte `quota_high` au-delà de 90 %).
- **Journal** (page de la routine) : résumé, sorties (page réécrite, éléments déposés,
  e-mails envoyés, webhook), texte de sortie, modèle, jetons, coût estimé ou quota, et pour
  un échec, la cause traduite (le détail technique pour les administrateurs).
- Le commit d'une routine (page de rapport et éléments d'inbox ensemble) est au nom de
  `Synaptane routine (<nom>)`, sujet `Routine "<nom>": …` : il n'apparaît pas comme une
  modification à examiner par le rangement (pas plus que ceux des versions antérieures, au nom
  de `Synaptane – routine`). Les passages d'une routine supprimée restent comptés dans le budget du mois.
- Sans fournisseur d'IA configuré, une routine planifiée ne tourne pas (pas d'échec inscrit) ;
  lancée à la main, elle échoue en le disant.

Le rangement nocturne reste à part : il n'est pas une routine et garde ses réglages.

## Coût et budget

Page de l'organisation → **Intelligence artificielle**, rubrique « Coût et budget » (ou
`PUT …/organizations/{org}/ai` avec `price_input`, `price_output`, `monthly_budget`).

- **Tarif** : en € par million de jetons, entrée et sortie. Vide : Synaptane applique le tarif
  public du modèle quand il le connaît (modèles Claude), en dollars compté en euros ; le coût
  affiché est donc une estimation. Sans tarif ni modèle connu, aucun coût n'est calculé. Les
  tarifs saisis valent pour tous les modèles de l'organisation, y compris ceux choisis par
  ses espaces.
- L'entrée relue depuis le cache du fournisseur compte pour un dixième du tarif d'entrée
  (un vingtième pour `claude-opus-5-5`, un quarantième pour `claude-fable-5-1`), l'entrée
  écrite en cache pour 1,25 fois le tarif d'entrée (tarif d'Anthropic pour le cache de
  5 minutes, le seul que Synaptane demande). Avec Anthropic, Synaptane active le cache :
  chaque tour relit le précédent à moindre coût.
- **Abonnements** (Claude Code, Codex) : pas de coût à l'usage, ni de budget à fixer.
- **Budget mensuel** (facultatif) : somme des coûts estimés du mois (UTC) dans tous les
  espaces de l'organisation : passages de rangement, passages de routines et indexation
  sémantique (voir plus bas). Les administrateurs de l'organisation reçoivent un e-mail à
  80 % puis à 100 % (si l'envoi d'e-mails est configuré), et le webhook `tidy.budget` est
  émis. À 100 %, les passages sont refusés jusqu'au mois suivant ; relever le budget les
  relance. Un passage en cours s'arrête dès que son coût épuiserait le budget (travail fait
  gardé), et les passages enchaînés s'arrêtent dès que le budget est atteint.
- **Fournisseurs facturés à l'usage** (Anthropic, OpenAI, Mistral, ou API compatible OpenAI
  avec une clé) : le formulaire propose un budget de 20 € par mois à la première
  configuration, et tant qu'aucun budget n'est fixé, la page IA affiche un avertissement : la
  dépense n'a pas de limite. Ni les abonnements, ni Ollama, ni une API compatible sans clé ne
  sont concernés.
- **Un budget exige un tarif connu** pour le modèle de l'organisation **et** pour celui
  choisi par chaque espace (tarif public pour les modèles Claude, ou tarifs saisis par
  l'admin). Sinon le budget est refusé, et une fois un budget fixé, un espace ne peut pas
  choisir un modèle sans tarif connu. Si un modèle perd son tarif plus tard (par exemple après
  une mise à jour), chaque passage avec ce modèle est compté au tarif public le plus élevé
  connu, jamais zéro, et le journal du passage le signale.
- **L'indexation sémantique** compte dans la dépense du mois : jetons relevés chez le
  fournisseur, ou estimés par excès (un jeton pour trois caractères) quand il ne les indique
  pas, aux tarifs publics des modèles d'OpenAI, Gemini, Mistral et Voyage AI (le plus élevé
  d'entre eux pour un modèle inconnu). Ollama et les API compatibles OpenAI ne sont pas
  comptés, ni la vectorisation des requêtes de recherche. Le budget n'arrête pas
  l'indexation.
- **Limites d'usage** : **Ranger maintenant** permet 5 passages manuels par personne et par
  espace sur 24 heures (20 pour un admin de l'organisation) ; les passages planifiés et au fil
  de l'eau ne sont pas concernés. Un compte dépose au plus 120 notes par heure dans les
  inbox, tous espaces et toutes interfaces confondus (web, REST, MCP), notes déplacées vers un
  autre espace comprises. Au-delà, la demande est refusée (HTTP 429, `too_many_requests`).
- **Annuler ce passage** (journal du rangement, administrateurs) crée un commit inverse ; les
  éléments reviennent dans l'inbox. Il échoue dès qu'une page du passage a été modifiée depuis.
- **Annuler certaines pages…** (contributeurs) liste les pages modifiées par le passage, chacune
  avec sa différence, et remet celles que vous cochez telles qu'avant le passage, en un commit
  à votre nom. Une page modifiée depuis le passage est refusée : restaurez-la depuis son
  historique. Une note rangée revient dans l'inbox une fois annulées toutes les pages où elle a
  été intégrée ; avant, le passage suivant la réécrirait dans les pages gardées.

## Recherche sémantique (embeddings, facultative)

Par défaut, la recherche se fait par les mots (façon grep). Dans les espaces où elle est
activée, la recherche sémantique trouve aussi les pages **proches par le sens** : une
question en anglais sur une page en français, ou une paraphrase sans mot commun.

1. **Page de l'organisation → Recherche sémantique** : choisissez un fournisseur.

   | Fournisseur | Modèle par défaut | Remarques |
   |---|---|---|
   | Ollama (local, sans clé) | `bge-m3` | Multilingue ; le contenu ne quitte pas le serveur. `ollama pull bge-m3`. |
   | OpenAI | `text-embedding-3-small` | Clé d'API. |
   | Google Gemini | `gemini-embedding-001` | Clé d'API Google AI Studio (multilingue, 3 072 dimensions). Sans facturation activée, l'offre gratuite s'applique et Google peut utiliser les contenus envoyés. |
   | Mistral | `mistral-embed` | Clé d'API. |
   | Voyage AI | `voyage-3.5` | Clé d'API. |
   | Compatible OpenAI | à indiquer | Toute API `/v1/embeddings`. |

   La page explique, pour chaque fournisseur, où et comment créer la clé.

   Pour le français, préférez un modèle **multilingue**. `nomic-embed-text` (Ollama), par
   exemple, distingue mal les pages pertinentes des autres.
2. **Paramètres → Recherche sémantique** de l'espace : activez-la. L'index se construit en
   tâche de fond ; la page indique le nombre de fichiers et de morceaux indexés.

Fonctionnement :
- **Découpage** : les pages sont découpées par section, avec le titre et les intertitres. Le
  texte extrait des PDF et des documents Office est indexé aussi.
- **Mise à jour** : l'index se met à jour à chaque modification. Changer de modèle le refait
  entièrement ; `synaptane reindex` le reconstruit depuis Git.
- **Fusion** : les résultats par mots et par sens sont fusionnés (RRF). Un espace sans
  embeddings n'est pas désavantagé dans une recherche sur plusieurs espaces.
- **Filtrage du sens** : une page trouvée *seulement* par le sens n'est proposée que si elle se
  détache nettement des autres voisins. Une question sans rapport avec l'espace ne remonte
  donc rien. Dans un espace de pages semblables (transcriptions d'appels, tickets), même une
  question pertinente se détache rarement : réglez une **similarité minimale** sur la page
  Recherche par le sens de l'organisation. La meilleure page doit alors l'atteindre à la
  place (entre 0 et 1, par ex. 0,55 avec `voyage-4-large`). Son échelle dépend du modèle :
  mesurez-la sur des questions liées à l'espace et sur des questions sans rapport. Elle est
  effacée quand le modèle change.
- **Panne du fournisseur** : la recherche par mots reste disponible.
- **Contrôle par l'appelant** : `mode=text` (REST, MCP) force la recherche par mots seuls.
- **Pages proches** (panneau « Voir aussi », outil MCP `related`) : avec les embeddings, le
  vecteur d'une page est la moyenne de ses morceaux, et chaque autre page est jugée par son
  morceau le plus proche, la similarité étant ramenée à la médiane de l'espace (le seuil ne
  dépend pas du modèle). Faute d'index ANN, ce calcul parcourt tous les morceaux de l'espace
  (0,4 s pour 80 000 morceaux de 1 024 dimensions), sous un délai de 4 s, puis il est gardé
  une heure tant que la page ne change pas ; au-delà du délai, les pages proches se
  calculent par les mots et les liens seuls. Sans embeddings, elles se calculent sans appel
  au fournisseur. Calcul et caches : `src/synaptane/services/related.py`.
