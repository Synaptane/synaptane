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
| Compatible OpenAI | Adresse et, au besoin, clé | vLLM, LM Studio, passerelles…, ou Gemini (`https://generativelanguage.googleapis.com/v1beta/openai`) et DeepSeek (`https://api.deepseek.com`), dont les tarifs publics sont connus. |
| **Abonnement Claude (Claude Code)** | Connexion du CLI sur le serveur | Réservé au super-admin. Voir plus bas. |
| **Abonnement ChatGPT (Codex)** | Connexion du CLI sur le serveur | Réservé au super-admin. Voir plus bas. |

- La clé d'API est chiffrée et n'est jamais réaffichée. Elle reste liée à son fournisseur
  et à son adresse : changer l'un ou l'autre oblige à la ressaisir (ou à la retirer).
- **Adresse de l'API** personnalisée (Ollama, compatible OpenAI, passerelle) : seul le
  super-admin peut la changer, car elle fait appeler le serveur vers l'adresse choisie.
- **Tester la connexion** fait un vrai appel d'outil. Pour une clé d'API, le test se fait
  aussitôt ; pour un abonnement, le worker le fait passer par le conteneur des agents (voir
  plus bas).
- Chaque espace peut choisir un autre modèle (**Paramètres → Rangement** de l'espace).

## Utiliser un abonnement Claude ou ChatGPT

Au lieu d'une clé d'API facturée à l'usage, l'agent peut tourner sur un abonnement. C'est
alors le **CLI officiel** de l'éditeur (`claude` ou `codex`) qui fait tourner l'agent, et
Synaptane ne voit jamais les identifiants.

1. Installez le CLI là où il tourne (le conteneur des agents, voir plus bas). L'image publiée
   (`ghcr.io/synaptane/synaptane`) n'en contient aucun, car Claude Code ne peut pas être
   redistribué. Une image que vous construisez vous-même contient les deux
   (`AGENT_CLIS=true`, valeur par défaut du Dockerfile) : dans `.env`,
   `COMPOSE_FILE=compose.yaml:compose.build.yaml` et `SYNAPTANE_IMAGE=synaptane:local`, puis
   `GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build`. Voir
   [Construire l'image vous-même](installation.fr.md#construire-limage-vous-même).
2. Connectez-le une fois, **dans le conteneur des agents**, par le flux de l'éditeur :
   - avec Docker : `docker compose exec agents claude auth login`, ou
     `docker compose exec agents codex login --device-auth` ;
   - sans Docker : `claude auth login`, ou `codex login`, avec le compte système qui fait
     tourner le CLI (`synaptane agents-runner`, ou `synaptane worker` sans lui), précédé de
     `HOME=…` si `SYNAPTANE_AGENT_HOME` est réglée : la page Intelligence artificielle
     affiche la commande exacte.
3. En super-admin, choisissez « Abonnement Claude » ou « Abonnement ChatGPT » pour
   l'organisation, puis **Tester la connexion**.

`synaptane doctor` et la page de santé (**Administration → Santé**, « Agents des
abonnements ») indiquent où tournent les CLI, s'ils sont installés et s'ils respectent le
contrat d'isolation (voir « Sécurité »).

**Où vit la connexion.** Le CLI la garde dans son propre `HOME`, donné par
`SYNAPTANE_AGENT_HOME` :
- avec Docker, `/agents`, sur le volume `synaptane-agents`, monté **seulement** dans
  `agents` (dont c'est aussi le `HOME`, d'où la commande simple ci-dessus). Ni le conteneur
  `app`, exposé à Internet, ni le worker ne la voient ; elle n'est pas non plus dans le
  volume des données ni dans les sauvegardes : après une restauration sur un autre serveur,
  reconnectez le CLI. `docker compose down -v` l'efface ;
- sans Docker, sans cette variable, le `HOME` du compte qui fait tourner le CLI.

Synaptane ne lit, ne copie, ne déplace ni ne stocke jamais ces identifiants : seul le CLI
officiel les manipule.

**Tester la connexion** est lancé par le worker, **dans le conteneur des agents**, seul à
avoir le CLI connecté : la page affiche « Test en cours… » et se met à jour seule (ou
`POST …/organizations/{org}/ai/test` répond 202, et `GET …/ai` donne `test_pending`, puis
`test_ok` et `test_message`). Le test passe par la file rapide et dure au plus quelques
minutes. Si « Test en cours… » ne change pas, vérifiez que le worker et le conteneur des
agents tournent (`docker compose ps`). Pour Claude, c'est un vrai mini-appel au plus petit
modèle : une session expirée est ainsi détectée, même quand `claude auth status` se dit
connecté.

**Quand la connexion expire.** Chaque passage de rangement échoue alors aussitôt. Ce passage
est enregistré **en échec** (jamais comme terminé avec l'erreur cachée dans le journal) :
l'onglet **Rangement** de l'espace montre l'échec et sa pastille, les administrateurs
reçoivent l'alerte `tidy_failed`, et chaque page de Synaptane leur affiche un bandeau
rouge, « Rangement de nuit en panne dans Acme depuis le … : la connexion à Claude Code a
expiré », avec un lien vers cette page. Le bandeau ne s'affiche qu'aux administrateurs de
l'organisation, dès qu'un passage échoue pour une cause côté IA (connexion, clé, modèle,
adresse, conteneur des agents ; une limite de débit ou un fournisseur surchargé seulement
après 3 échecs de suite) ou que le dernier **Tester la connexion** échoue ; il disparaît dès
qu'un test ou un passage réussit. Reconnectez le CLI (commande ci-dessus), puis **Tester la
connexion**. Rien n'est perdu entre-temps : un passage qui n'a pas pu joindre l'IA ne compte
aucune tentative contre les notes et les pages modifiées en attente dans l'inbox, et
l'[entretien du wiki](#entretien-du-wiki) garde sa place.

**Mise à jour depuis une version où la connexion était dans `/data/home`.** Elle était
alors dans le volume des données, partagé avec le conteneur web. Synaptane ne la déplace pas
(seul le CLI manipule les identifiants) ; `synaptane doctor` et la page Intelligence
artificielle signalent l'ancien dossier tant qu'il existe (sans rien y lire). Une fois pour
toutes :
1. reconnectez le CLI dans le conteneur des agents :
   `docker compose exec agents claude auth login` (ou `codex login --device-auth`) ;
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
Synaptane, fournis par un serveur MCP éphémère du worker protégé par un jeton à usage
unique. Il n'a ni shell, ni accès aux fichiers, ni web : un contenu malveillant déposé dans
l'inbox ne peut pas lire le serveur. Et il tourne dans un conteneur à part, qui n'a de toute
façon rien à lire (voir « Le conteneur des agents » plus bas).

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
tête (« contrat d'isolation rompu »). Il tourne aussi là où tournent les CLI : le conteneur
des agents vérifie ses propres CLI à son démarrage
(`docker compose exec agents synaptane check-clis` en donne le détail), et
`synaptane doctor` et la page de santé en signalent le résultat (en avertissement).

### Le conteneur des agents

Depuis la 1.15, les CLI des abonnements ne tournent plus dans le worker mais dans un
conteneur à eux, `agents` dans `compose.yaml`. Même image, mais il ne reçoit rien de
l'instance :

![Worker et conteneur des agents](../assets/agents-container.svg)

- **Ni données, ni secret.** Il ne monte que les connexions des CLI (`synaptane-agents`, son
  `HOME`) et, en lecture seule, la clé qu'il partage avec le worker. Ni le volume des données
  (clé maître, dépôts, texte extrait), ni les sauvegardes, ni le socket Docker. Son
  environnement ne contient aucun secret de l'instance : ni adresse ni mot de passe de la
  base, ni jeton des métriques, ni réglage des sauvegardes.
- **Deux réseaux.** `agents`, interne (sans Internet), qu'il partage avec le seul worker ;
  `agents-internet`, à lui seul, pour son accès sortant à Anthropic et OpenAI. Ni la base ni
  le conteneur web ne sont sur un réseau où il se trouve : leurs noms n'y sont même pas
  résolus.
- **Durci.** Compte non root (celui de l'image, UID 1000), système de fichiers en lecture
  seule avec un `/tmp` de 512 Mo en mémoire, toutes les capacités Linux retirées,
  `no-new-privileges`, mémoire plafonnée (`SYNAPTANE_AGENTS_MEMORY`, 2 Go par défaut) et
  512 processus au plus.

**Un passage, pas à pas.**
1. Le worker lance le serveur d'outils MCP du passage sur sa propre adresse du réseau
   `agents`, et sur elle seule, avec un nouveau jeton aléatoire (32 octets) valable pour ce
   seul passage : le serveur s'arrête à la fin du passage.
2. Il envoie le passage à `http://agents:8700` (`SYNAPTANE_AGENTS_URL`) : fournisseur,
   modèle, instructions, prompt, port et jeton du serveur d'outils.
3. Le conteneur des agents (`synaptane agents-runner`) lance `claude -p` ou `codex exec`
   exactement comme le faisait le worker : mêmes options, même environnement minimal, dans
   un dossier de travail jetable. Le CLI joint les outils à l'adresse d'où vient la requête :
   un appelant ne peut pas le diriger ailleurs.
4. Il renvoie ce que lit le worker (résultat final, consommation, relevés du quota, erreurs,
   fin de la sortie d'erreur), gardé au fil de l'eau : 4 Mo au plus, au-delà desquels le CLI
   est arrêté ; le reste de la sortie du CLI (la conversation, les pages lues) n'est même pas
   gardé.

**Qui peut lancer un passage.** Le worker seul. Le conteneur des agents refuse toute requête
sans la clé qu'il partage avec le worker (`SYNAPTANE_AGENTS_KEY_FILE`) : 48 octets aléatoires,
créés par le worker à son premier appel dans le volume `synaptane-agents-key`, monté dans ces
deux conteneurs seulement (en lecture seule dans `agents`). La clé n'est jamais journalisée,
affichée ni envoyée ailleurs ; une requête refusée est journalisée avec sa seule adresse
d'origine. Le réseau s'y ajoute : le conteneur web, celui qui est exposé à Internet, n'est
pas sur le réseau `agents`. L'un ou l'autre suffirait ; ensemble, une erreur sur l'un (un
fichier Compose personnalisé qui met `app` sur ce réseau, un fichier de clé copié là où il
ne devrait pas) n'ouvre pas le conteneur des agents.

**Délais, annulation, concurrence.**
- Un passage dure au plus 45 minutes (3 pour un test de connexion) ; au-delà, le CLI et tout
  ce qu'il a lancé sont arrêtés (SIGTERM, puis SIGKILL 5 secondes plus tard).
- Si le worker abandonne (il s'arrête, ou son propre délai expire), la connexion se ferme et
  le conteneur des agents arrête le CLI en quelques secondes : aucun passage ne continue seul
  à consommer le quota.
- **Un CLI à la fois** par défaut (`SYNAPTANE_AGENTS_MAX_RUNS=1`). Les CLI du conteneur des
  agents partagent son compte : deux qui tourneraient ensemble pourraient lire l'environnement
  et la configuration MCP l'un de l'autre, donc le jeton de passage de l'autre (voir plus bas).
  Un passage qui arrive pendant un autre attend son tour dans le worker, en réessayant toutes
  les 10 à 15 secondes, aussi longtemps que peut durer un passage (3 minutes pour un test de
  connexion) ; ce n'est pas compté comme un échec. Passé ce délai, il est relancé plus tard
  comme toute erreur passagère : rien n'est perdu. Le prix : un test de connexion ou une
  routine demandés pendant un rangement de 30 minutes l'attendent. Monter la valeur supprime
  cette attente, au prix de la séparation entre passages (et chaque CLI prend quelques
  centaines de Mo). Sans conteneur des agents, le CLI tourne dans le worker, qui ne sépare de
  toute façon rien : aucune limite ne s'y applique.
- Instructions et prompt : 16 Mo au plus par requête.

**Mise à jour depuis la 1.14 ou avant.** Rien à faire : le nouveau `compose.yaml` ajoute le
service `agents` et y monte le volume `synaptane-agents` existant, au même endroit
(`/agents`, son `HOME`), avec le même compte (UID 1000). Les connexions faites dans le
worker restent valables : inutile de reconnecter les CLI. La clé est créée par le worker à
son premier appel. Avec `deploy/host/deploy.sh`, la mise à jour attend aussi que le
conteneur des agents soit en bonne santé avant de déclarer le déploiement réussi (et revient
en arrière sinon). Après la mise à jour, **Tester la connexion** sur la page Intelligence
artificielle, ou `docker compose exec worker synaptane doctor`, confirme que le worker le
joint.

**Sans Docker, ou avec un fichier Compose sans le service `agents`.** Sans
`SYNAPTANE_AGENTS_URL`, le CLI tourne dans le worker, comme avant la 1.15, avec les mêmes
options et le même contrat. Il est moins isolé : le CLI tourne alors sous le compte du
worker, qui peut lire le dossier des données. `synaptane doctor` (« Subscription CLIs apart
from the worker ») et la page de santé le signalent. Pour l'isoler sans Docker, lancez
`synaptane agents-runner --host 127.0.0.1 --port 8700` sous un autre compte système, qui ne
peut pas lire le dossier des données, avec `SYNAPTANE_AGENTS_KEY_FILE` pointant vers un
fichier d'un dossier où le worker peut écrire et que ce compte peut lire (la clé est créée
lisible par le groupe), et donnez au worker `SYNAPTANE_AGENTS_URL=http://127.0.0.1:8700` et
le même `SYNAPTANE_AGENTS_KEY_FILE`.

**Ce que le conteneur des agents protège, et ce qu'il ne protège pas.**
- Il protège le reste du serveur du CLI : une faille de Claude Code ou de Codex, ou une
  injection de prompt qui passerait leurs options, ne trouve ni la clé maître, ni les dépôts,
  ni la base, ni les secrets de l'instance, et ne peut joindre par le réseau ni le conteneur
  web ni la base.
- Il ne protège pas les connexions des CLI : elles sont dans le conteneur des agents, le CLI
  peut les lire, et qui contrôle le CLI peut se servir de l'abonnement.
- Il ne limite pas ce que fait l'agent **par les outils de Synaptane** pendant un passage :
  écrire et déplacer des pages de l'espace rangé, dans les garde-fous habituels (seuil de
  suppressions, un commit par passage qu'on peut annuler). Le serveur d'outils ne vit que le
  temps du passage, et seul le jeton de ce passage l'ouvre.
- Il ne sépare pas des passages qui tourneraient en même temps : ils partageraient le
  conteneur et son compte, si bien qu'un CLI compromis pourrait lire le jeton d'un autre
  passage en cours (un rangement d'une autre organisation, par exemple) et se servir de ses
  outils jusqu'à la fin de ce passage. C'est pourquoi `SYNAPTANE_AGENTS_MAX_RUNS` vaut 1 par
  défaut : les passages s'attendent. Ne le montez que si vos organisations peuvent
  s'atteindre ainsi.
- Ce qu'un passage laisse dans le `HOME` des CLI (`/agents`) reste pour les suivants. Ce qui
  pourrait les orienter est coupé : Claude Code tourne avec `--setting-sources ""` (ni les
  hooks, l'environnement et les permissions de `~/.claude/settings.json`, ni
  `~/.claude/CLAUDE.md`, les skills ou les agents), Codex avec `--ignore-user-config`, ses
  hooks coupés et sans la description des skills de son dossier (`~/.codex/skills`,
  `~/.agents/skills`) ; le contrat d'isolation plante un hook, des instructions et des skills
  dans le `HOME` de sa vérification et échoue s'ils ont un effet. Codex n'a pas d'option pour écarter
  `~/.codex/AGENTS.md` (ou `AGENTS.override.md`), qu'il ajoute à chaque passage : Synaptane
  n'en écrit jamais, donc un passage Codex est refusé tant qu'il y en a un. Reste qu'un CLI
  compromis pourrait remplacer les fichiers de connexion eux-mêmes (connecter le CLI à un
  autre compte, qui recevrait alors les prompts suivants) ou d'autres fichiers que le CLI lit
  pour lui-même. En cas de doute, déconnectez puis reconnectez les CLI
  (`docker compose exec agents claude auth logout`, puis `login`), ou videz le volume.
- Son accès sortant n'est pas filtré : il peut joindre Internet (il lui faut Anthropic et
  OpenAI), l'adresse de métadonnées de l'hébergeur (`169.254.169.254`) s'il y en a une, et
  tout ce qui écoute sur toutes les adresses du serveur : les ports publiés par Docker (qu'un
  pare-feu comme ufw ne filtre pas) et les services du serveur lui-même (une base de données,
  un Redis sans mot de passe…). Gardez le port de l'application lié à `127.0.0.1` (`APP_BIND`,
  la valeur par défaut), et liez ces services à `127.0.0.1` ou filtrez le trafic des réseaux
  Docker vers l'hôte.
- Le prompt de chaque passage (notes de l'inbox, pages) va bien au conteneur des agents, puis
  au fournisseur : c'est l'objet du passage.
- L'image est la même que celle des autres services : une faille du moteur Docker ou du
  noyau reste hors de sa portée.

**Dépannage.**
- « Le conteneur des agents est injoignable » : `docker compose ps agents`, puis
  `docker compose logs --tail 50 agents`. Une image publiée sans CLI n'est pas une erreur :
  le conteneur tourne, au repos.
- « a refusé la clé du worker » : les deux conteneurs doivent lire le même fichier ;
  vérifiez le volume `synaptane-agents-key` dans les deux (`docker compose config`). Supprimer
  le fichier de clé (`docker compose exec worker rm /run/synaptane-agents/key`) fait créer
  une nouvelle clé par le worker, que le conteneur des agents lit à sa requête suivante.
- « fait déjà tourner autant de CLI qu'il le peut » : un passage a attendu son tour plus
  longtemps que son propre délai ; il est relancé plus tard de lui-même. Si cela revient
  souvent, étalez les rangements dans la nuit, ou montez `SYNAPTANE_AGENTS_MAX_RUNS` en
  sachant ce que cela coûte (plus haut).
- « Codex ajouterait les instructions de … » : supprimez le fichier cité
  (`docker compose exec agents rm /agents/.codex/AGENTS.md`), après l'avoir lu : quelque chose
  l'a mis là.
- « atteint ce qu'il ne devrait pas » (page de santé, doctor) : le conteneur des agents voit
  un secret de l'instance, la clé maître, le socket Docker ou la base. Retirez ce que votre
  fichier Compose lui ajoute (`docker compose config`).

Conditions des éditeurs (octobre 2026) :
- **OpenAI** autorise l'usage de l'abonnement ChatGPT dans des outils tiers.
- **Anthropic** autorise la connexion d'un abonnement au **binaire Claude Code officiel**.
  Elle interdit à une application tierce de manipuler ces identifiants, et précise que les
  limites des abonnements Pro et Max supposent un usage individuel ordinaire.

Pour une organisation d'entreprise partagée par plusieurs personnes, préférez une clé d'API,
ou un plan Team ou Enterprise.

## Rangement nocturne (par espace)

Les réglages sont dans **Paramètres → Rangement** de l'espace ; le journal et **Ranger
maintenant**, dans son onglet **Rangement** (le bouton est aussi dans l'onglet Inbox, et en
haut de **Paramètres → Rangement**, d'où il mène au journal qui suit le passage).

- **Rangement quotidien** (heure et fuseau) : 02:00 à Paris par défaut. Le passage n'a lieu que s'il y a de
  nouveaux éléments dans l'inbox, ou si l'[entretien du wiki](#entretien-du-wiki) trouve des problèmes. **Ranger maintenant** le lance aussitôt (5 fois par personne et par
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
  sa raison. Il revient dans chaque passage qui a lieu, après les éléments nouveaux
  (25 éléments au plus par passage) : un rangement manuel, même pour des éléments laissés
  seuls, ou un passage planifié ou au fil de l'eau, qui n'a lieu que s'il y a du nouveau à
  ranger ou un problème du wiki à corriger. Modifié, un élément laissé redevient nouveau. Un
  élément présenté à un passage qui s'arrête avant de le clore (erreur du fournisseur, plafond,
  budget atteint) est *non traité* : le travail déjà fait est commité, l'élément reste
  nouveau et sera repris au passage suivant, avec un compteur (`tidy_attempts` dans
  l'en-tête d'une note, en base pour une page modifiée). Après 3 passages interrompus, il
  est laissé avec la raison. Dans les deux cas (arrêt, élément abandonné), les
  administrateurs reçoivent l'alerte `tidy_interrupted`. Un passage interrompu n'enchaîne
  pas aussitôt un autre passage. Un passage que le fournisseur arrête **avant que l'agent
  appelle le moindre outil** (connexion expirée, clé refusée, CLI qui ne démarre pas) est au
  contraire un passage **en échec** : rien n'est commité ni compté, et il est retenté plus
  tard si l'erreur est passagère (sinon, les administrateurs reçoivent l'alerte
  `tidy_failed`). Dès que l'agent a commencé à lire, une erreur du fournisseur est une
  interruption comme ci-dessus : la tentative est comptée, et un élément qui fait échouer
  chaque passage finit donc par être laissé.
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
  | Contrôles de l'équipe enfreints | infractions aux [contrôles de l'équipe](../guide/wiki.fr.md#contrôles-de-léquipe) de l'espace (niveau `warn`) introduites par le passage, déjà renvoyées une fois à l'agent pour correction ; une règle `block` refuse l'écriture elle-même | corriger les pages listées, ou ajuster la règle |
  | Éléments laissés | part des éléments laissés dans l'inbox | une part élevée et durable signale des consignes floues |
  | Par élément rangé | jetons, tours et coût par élément rangé | comparer deux modèles ou deux consignes |

  Rien n'est bloqué : les changements importants sont aussi signalés aux administrateurs
  (alerte `big_changes`, ci-dessus). Les mêmes valeurs sont dans le champ `metrics` de
  `GET …/tidy/runs` (REST), pour les suivre dans le temps. Pour changer les consignes ou le
  modèle sans saut dans le noir, voir le [banc d'évaluation](../dev/eval.fr.md).
- Sans fournisseur d'IA configuré, le rangement planifié ne tourne pas (pas d'échec inscrit
  chaque nuit).

### Entretien du wiki

On modifie aussi le wiki directement : une modification sur le web, un push. Ces
changements peuvent casser un lien ou laisser une nouvelle page vers laquelle rien ne mène.
L'onglet **Santé** voit ces problèmes, mais personne ne les corrigeait tant qu'aucune note
n'arrivait. Avec la case **Entretenir le wiki chaque nuit** (Paramètres → Rangement,
**cochée par défaut**), le passage planifié les corrige, même si l'inbox est vide.

- **Ce qui est vérifié**, sans IA, à l'heure planifiée et avec **Ranger maintenant** : les
  problèmes apparus **depuis le dernier passage** par les commits des membres (web, REST,
  MCP, push) :
  - les liens cassés introduits dans les pages qu'ils ont modifiées, ou dans les pages qui
    menaient à une page qu'ils ont supprimée ou déplacée ;
  - leurs nouvelles pages vers lesquelles aucune page ne mène, absentes de l'index de leur
    dossier (`README.md`, si le dossier en a un), sans titre ou vides.
  Les commits de Synaptane ne comptent pas : ce que casse le rangement est déjà repris par
  sa vérification de fin de passage. Les imports non plus, c'est-à-dire tout commit qui touche
  plus de 20 pages (un import Obsidian, Notion ou Confluence, une réorganisation) : le
  rangement les relit déjà d'un bloc, avec son élément « Import : N pages ». Seul compte ce
  que les membres changent ensuite dans ces pages. Un import plus petit est relu page par
  page, comme toute modification.
- **Ce que fait l'agent** : les problèmes arrivent comme un élément de plus, « Entretien du
  wiki (N) », avec la liste précise (page, lien, suggestion du contrôle de santé), présentée
  comme une donnée. L'agent ne corrige que ce qui est sûr : il fait pointer un lien vers la
  bonne page, relie une page isolée depuis la page de son sujet ou depuis l'index, complète
  l'index. Il n'ajoute aucun contenu, et ne supprime ni ne déplace jamais une page pour
  corriger un problème. Sinon, il laisse le problème, avec une raison. Ses pouvoirs ne
  changent pas : mêmes outils, même plafond de suppressions, mêmes alertes et même
  annulation que pour tout passage.
- **Ni déluge ni répétition** :
  - pas de rattrapage du passé : les problèmes sont comptés depuis la fin du dernier passage,
    ou depuis l'activation de l'entretien (ou la mise à jour) ;
  - **10 problèmes au plus par nuit** : assez pour une journée ordinaire de modifications,
    une petite part des limites d'un passage, et une liste facile à vérifier dans le journal.
    Les autres attendent les nuits suivantes ;
  - un problème laissé par l'agent n'est reproposé que s'il est **réintroduit** : il a
    disparu puis est revenu, ou quelqu'un l'écrit de nouveau (le même lien cassé ajouté une
    fois de plus, la page recréée). Modifier la page ne suffit pas : une page de journal
    modifiée chaque jour, avec un lien cassé exprès, ne coûte pas un passage chaque nuit ;
  - les passages lancés après un dépôt et les passages enchaînés n'entretiennent pas.
- **Coût** : rien n'est calculé ni lancé sans fournisseur d'IA, ni quand le budget du mois
  est épuisé. Une nuit sans problème ne coûte rien.
- **Journal** : chaque problème a sa ligne, **Problème du wiki corrigé** ou **Problème du
  wiki laissé** avec la raison de l'agent. Les déposants ne reçoivent pas d'avis pour lui. Le
  commit dit combien de problèmes ont été corrigés. Un passage arrêté avant la fin redonne
  les problèmes la nuit suivante ; après trois passages interrompus de suite, ils sont
  laissés, avec cette raison.
- **REST** : `maintain` dans `PATCH …/spaces/{space}/tidy-settings`.

## Vocation et consignes de rangement

On ne range pas de la même façon le second cerveau d'une équipe d'infrastructure, d'une
équipe marketing ou d'une famille. Chaque espace peut donc décrire, dans **Paramètres →
Vocation** (administrateurs de l'organisation), deux textes Markdown versionnés dans son
dépôt. Un troisième fichier, les [contrôles de l'équipe](../guide/wiki.fr.md#contrôles-de-léquipe)
(`.synaptane/checks.md`), est donné à l'agent comme donnée de configuration : ce que chaque
page doit suivre, vérifié sans IA.

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
  n'est enregistré avant **Enregistrer**. **Aperçu** montre le rendu sans enregistrer :
  au-dessus de chaque texte, comme pour une page du wiki (**Écrire**, **Aperçu**, **Côte à
  côte** sur grand écran), la vocation telle que les modèles la reçoivent (sans les
  commentaires des modèles), diagrammes Mermaid dessinés. Sans JavaScript, le bouton
  **Aperçu** sous le texte recharge la page avec le rendu. Administrateurs de l'organisation
  seulement, comme l'édition ; rien n'est commité ni journalisé.
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

Chaque routine est **un fichier du dépôt de l'espace**, `.synaptane/routines/<nom>.md`,
versionné comme la vocation et les consignes de rangement (voir
[Le fichier d'une routine](#le-fichier-dune-routine)). Paramètres → Routines et l'API REST
écrivent ce fichier : un commit au nom de l'administrateur, puis une synchronisation.

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
  - un **e-mail** aux administrateurs de l'organisation, ou à tous les membres de l'espace
    (y compris ceux qui y accèdent par un groupe ou par le rôle par défaut de l'organisation) :
    seulement vers une adresse vérifiée, aux comptes actifs qui lisent l'espace et qui n'ont
    pas décoché « Recevoir les e-mails des routines » dans **Mon compte** (préférence
    distincte des avis de rangement, active par défaut, rappelée dans chaque e-mail) ;
  - le **webhook** `routine.done`, avec le texte de sortie (webhooks de l'organisation).

### Le fichier d'une routine

Git fait foi. Le fichier d'une routine commence par un en-tête entre deux lignes `---`, suivi
des consignes :

```markdown
---
title: Contrôle de cohérence
description: Contradictions et pages périmées, chaque semaine
schedule: weekly monday 06:00
scope: infra, clients/acme
report: rapports/coherence.md
inbox: true
email: admins
webhook: false
enabled: true
template: coherence
memory: true
---
Relis les pages modifiées depuis le dernier passage…
```

| Champ | Valeur |
|---|---|
| `title` | Nom affiché dans Synaptane (par défaut : tiré du nom du fichier) |
| `description` | Facultative, sur une ligne |
| `schedule` | `daily 02:30`, `weekly monday 06:00` (ou `mon`), `monthly 1 06:00` (jour 1 à 28) ; heure locale de l'espace. Le français est lu aussi : `quotidien` ou `chaque jour`, `hebdo` ou `hebdomadaire` avec `lundi` à `dimanche` (ou `lun`, `mar`…), `mensuel`, par exemple `hebdo lundi 06:00` |
| `scope` | Dossiers du wiki, séparés par des virgules, en liste JSON, ou un par ligne `- dossier` ; absent : tout l'espace |
| `report` | Page de rapport réécrite à chaque passage, relative au wiki |
| `inbox` | `true` : un élément dans l'inbox par correction à faire |
| `email` | `admins`, `members` ou `none` |
| `webhook` | `true` : le webhook `routine.done` |
| `enabled` | `false` : la routine ne tourne pas selon son planning (`true` par défaut) |
| `template` | Modèle d'origine (`coherence`, `digest`), indiqué par Synaptane |
| `memory` | `true` : la routine garde des notes d'un passage à l'autre (voir [Mémoire](#mémoire)) ; `false` par défaut |

Le nom de la routine est celui du fichier : minuscules, chiffres et tirets, 40 caractères au
plus. Une routine créée depuis un modèle a toujours le même nom, quelle que soit la langue de
son titre : `consistency-check.md`, `weekly-digest.md` ; une autre prend un nom tiré de son
titre, sauf si vous en choisissez un. Au plus 20 routines par espace, des consignes de 20 000 caractères au plus, un périmètre
de 20 dossiers au plus, au moins une sortie, et un fichier de 64 000 octets au plus.

- **À chaque synchronisation**, Synaptane lit les fichiers au commit courant de l'espace. Un
  nouveau fichier crée la routine, qui attend son prochain créneau. Un fichier modifié change
  les réglages, jamais l'état des passages : une routine qui a déjà tourné aujourd'hui ne
  repart pas parce que son fichier a changé, et une nouvelle heure déjà passée attend le
  lendemain. Un fichier supprimé retire la routine ; son journal et son coût restent dans le
  budget du mois.
- **Un fichier invalide ne tourne pas.** L'erreur (ligne incomprise, planning inconnu, aucune
  sortie…) s'affiche dans Paramètres → Routines et dans l'API REST (`error`). Un champ que
  cette version ne connaît pas est signalé (`warnings`) et ignoré, sans empêcher la lecture ;
  une réécriture par Synaptane le garde.
- **Depuis Synaptane**, l'enregistrement réécrit l'en-tête dans ce format, en anglais : un
  planning écrit en français redevient `weekly monday 06:00`. Un fichier modifié
  depuis l'ouverture du formulaire (un push, un autre administrateur) est refusé, avec le choix
  de le remplacer, comme pour la vocation. Avant la première synchronisation de l'espace, la
  routine attend en base ; cette synchronisation l'écrit.

**Confiance.** Quiconque peut pousser sur le dépôt peut créer ou modifier une routine, comme il
peut modifier les consignes de rangement : accordez le droit de pousser en conséquence. Dans
Synaptane, seuls les administrateurs de l'organisation écrivent ces fichiers : pages, notes,
imports, e-mails capturés et assistants (MCP, REST) n'écrivent que dans `content/` et dans
l'inbox. Quel que soit son auteur, une routine garde ses garde-fous : elle n'écrit nulle part
ailleurs que dans ses sorties, dans la limite du budget.

### Mémoire

Sans mémoire, une routine repart de zéro à chaque passage : elle connaît l'activité depuis son
dernier passage et ses consignes, rien de plus. Avec `memory: true` dans son fichier (la case
**Garder des notes d'un passage à l'autre** du formulaire), elle garde aussi une **mémoire** :
ce qu'elle veut que ses prochains passages sachent, comme les sujets déjà signalés, les pages
qu'elle suit, les décisions notées, ce qui reste à surveiller.

- **Où** : `.synaptane/memory/<nom>.md`, à côté des autres fichiers de `.synaptane/`, sous le
  nom de la routine. C'est un fichier du dépôt, versionné et visible par l'équipe : son
  historique dit ce que la routine a noté et quand, et quiconque peut pousser sur le dépôt peut
  le corriger ou l'effacer. Un passage ne réécrit jamais le fichier de la routine elle-même.
- **Désactivée par défaut** : la mémoire est relue à chaque passage, elle coûte donc des
  jetons à chaque fois (quelques milliers de caractères au plus). Une routine existante ne
  change pas de comportement tant que vous ne l'activez pas. Le modèle **Contrôle de
  cohérence** garde une mémoire ; le **Résumé hebdomadaire** non (l'historique de la semaine
  lui suffit).
- **En lecture** : au début du passage, l'agent reçoit la mémoire telle qu'elle est au commit
  que lit le passage, 4 000 caractères au plus (un fichier plus long, modifié à la main, est
  coupé, et l'agent en est averti). Comme les pages, elle lui est donnée comme une donnée,
  jamais comme une consigne : elle ne change ni les consignes de la routine, ni ses outils, ni
  son périmètre.
- **En écriture** : l'agent la réécrit en entier avec l'outil `remember`, 4 000 caractères au
  plus : un résumé, pas un journal qui grossit sans fin. Un secret probable est refusé, de même
  qu'un texte qui enfreint une règle `block` des
  [contrôles de l'équipe](../guide/wiki.fr.md#contrôles-de-léquipe) portant sur le texte de
  toutes les pages (un motif, les adresses e-mail…) ; les règles limitées à des dossiers ou à
  un champ d'en-tête concernent les pages et ne s'appliquent pas. La mémoire entre dans le
  commit unique du passage, avec le rapport et les éléments d'inbox : **un passage interrompu
  la laisse telle quelle**.
- **Modifiée pendant le passage** (un push, un administrateur qui la vide) : cette
  modification l'emporte, et la version du passage est abandonnée (le journal indique
  « Mémoire conservée »). L'agent n'a pas vu cette modification, et une mémoire vidée parce
  qu'elle contenait une erreur ne doit pas revenir avec un passage commencé avant. Un push n'est
  vu que lorsque Synaptane l'a synchronisé : s'il arrive après la fin du passage, les deux
  versions de la mémoire sont en [conflit](git.fr.md#conflits), et un administrateur choisit
  celle à garder.
- **Les droits** : seuls les passages de la routine l'écrivent depuis Synaptane ; aucun autre
  chemin d'écriture n'atteint `.synaptane/`. Les contributeurs la lisent sur la page de la
  routine et dans l'API REST (`GET …/routines/{id}/memory`) ; les administrateurs de
  l'organisation peuvent aussi la **vider** (un commit à leur nom ; en REST :
  `DELETE …/routines/{id}/memory`).
- **Routine renommée ou supprimée** : supprimer une routine depuis Synaptane retire sa mémoire
  dans le même commit. Une routine supprimée ou renommée par un push perd sa mémoire à la
  synchronisation suivante (un commit `Synaptane: memories of removed routines deleted`) :
  ainsi, une nouvelle routine du même nom n'hérite jamais de notes écrites pour une autre.
  Pour la garder en renommant, déplacez-la aussi
  (`git mv .synaptane/memory/ancien.md .synaptane/memory/nouveau.md`). L'historique garde les
  notes dans tous les cas.

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
  rangé. Il garde une [mémoire](#mémoire) : les problèmes signalés (pour ne plus les présenter
  comme nouveaux tant que leurs pages ne changent pas), les doutes
  à revérifier et, une semaine calme, les pages anciennes déjà relues.

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
- Un secret probable (mot de passe, clé, jeton) dans le texte de sortie, un élément ou la
  mémoire est refusé à l'agent, qui doit le retirer.
- **Mémoire** : quiconque peut pousser sur le dépôt peut modifier la mémoire d'une routine ;
  l'agent la lit comme une donnée, comme les pages, et elle ne donne aucun droit (voir
  [Mémoire](#mémoire)).
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
  e-mails envoyés, webhook, mémoire mise à jour ou conservée), texte de sortie, modèle, jetons, coût estimé ou quota, et pour
  un échec, la cause traduite (le détail technique pour les administrateurs).
- Le commit d'une routine (page de rapport, éléments d'inbox et mémoire ensemble) est au nom de
  `Synaptane routine (<titre>)`, sujet `Routine "<titre>": …` : il n'apparaît pas comme une
  modification à examiner par le rangement (pas plus que ceux des versions antérieures, au nom
  de `Synaptane – routine`). Les passages d'une routine supprimée restent comptés dans le budget du mois.
- Sans fournisseur d'IA configuré, une routine planifiée ne tourne pas (pas d'échec inscrit) ;
  lancée à la main, elle échoue en le disant.

Le rangement nocturne reste à part : il n'est pas une routine et garde ses réglages.

## Coût et budget

Page de l'organisation → **Intelligence artificielle**, rubrique « Coût et budget » (ou
`PUT …/organizations/{org}/ai` avec `price_input`, `price_output`, `monthly_budget`).

- **Tarif** : en € par million de jetons, entrée et sortie. Vide : Synaptane applique le tarif
  public du modèle quand il le connaît, en dollars compté en euros ; le coût affiché est donc
  une estimation. Sans tarif ni modèle connu, aucun coût n'est calculé. Les tarifs saisis
  valent pour tous les modèles de l'organisation, y compris ceux choisis par ses espaces. La
  page affiche le tarif public appliqué au modèle de l'organisation.
- **Tarifs publics connus** : les principaux modèles d'Anthropic (Claude), d'OpenAI, de Google
  Gemini, de Mistral et de DeepSeek, au tarif standard à l'usage de l'API du fournisseur (sans
  supplément batch, prioritaire ou régional, sans gratuité). Un modèle est cherché d'abord parmi
  ceux de son fournisseur, puis parmi les autres sous son nom exact : un modèle Claude appelé
  par le fournisseur OpenAI à une adresse personnalisée (point d'accès compatible OpenAI
  d'Anthropic, passerelle) garde son tarif, de même que Gemini et DeepSeek derrière une API
  compatible OpenAI (ou OpenRouter, LiteLLM…). Ollama (local) et les abonnements n'ont pas de
  tarif public. Les modèles à accès restreint, ouverts sur accord du fournisseur
  (`gpt-6-cyber`…), ne sont pas dans la table : saisissez leur tarif. `mistral-large-latest`,
  le modèle Mistral par défaut, est compté au tarif de Mistral Large 4, le plus élevé des deux
  modèles vers lesquels l'alias peut pointer. Le nom du modèle est reconnu avec un préfixe de routeur (`openai/gpt-5`, `models/gemini-2.5-pro`), en version datée
  (`gpt-5-2025-08-07`, `claude-haiku-4-5-20251001`) ou avec les points d'OpenRouter
  (`anthropic/claude-sonnet-5.5`) ; tout autre modèle est inconnu, même si son nom commence
  comme celui d'un modèle connu (`gpt-5-pro` ne prend pas le tarif de `gpt-5`). Une passerelle
  qui facture son propre tarif (Azure, frais d'OpenRouter) : saisissez-le.
- **Cache**, tel que chaque fournisseur le facture : l'entrée relue depuis le cache compte au
  tarif d'entrée en cache du modèle (le plus souvent un dixième du tarif d'entrée ; un vingtième
  pour `claude-opus-5-5` et `claude-sonnet-5-5`, un quarantième pour `claude-fable-5-1` et
  `claude-mythos-5-1`). L'entrée écrite en cache coûte 1,25 fois le tarif d'entrée chez
  Anthropic (cache de 5 minutes, le seul que Synaptane demande) et pour les modèles GPT-5.6 et
  suivants d'OpenAI (30 minutes par défaut), sans supplément ailleurs. Avec Anthropic, Synaptane
  active le cache : chaque tour relit le précédent à moindre coût ; avec les autres
  fournisseurs, Synaptane compte les lectures et écritures en cache qu'ils indiquent. Avec un
  tarif saisi, les parts de cache du tarif public du modèle s'y appliquent.
- **Prompts longs** : certains modèles coûtent plus cher pour une requête dont le prompt est
  long (cache compris) : les modèles d'OpenAI à partir de GPT-5.4 (hors mini et nano) au-delà
  de 272 000 jetons, Gemini Pro au-delà de 200 000, `claude-haiku-5-5` au-delà de 100 000.
  Synaptane compte ces requêtes une à une au tarif le plus élevé.
- **Changements de tarif** annoncés par le fournisseur : appliqués à leur date (par exemple
  Gemini 4 Argon le 1er décembre 2026). Un tarif promotionnel dont la fin est annoncée sans le
  tarif suivant cesse d'être connu à cette date (`gpt-5.6-sol`, 1er décembre 2026) : voir
  plus bas. DeepSeek est compté à son tarif des heures pleines (un passage en heures creuses
  coûte moitié moins).
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
- **Un budget exige un tarif connu** pour le modèle de l'organisation **et** pour celui choisi
  par chaque espace (tarif public connu, ou tarifs saisis par l'admin). Sinon le budget est
  refusé, et une fois un budget fixé, un espace ne peut pas choisir un modèle sans tarif connu.
  Si un modèle perd son tarif plus tard (par exemple après une mise à jour), chaque passage avec
  ce modèle est compté au tarif public le plus élevé connu (10 $ / 50 $ par million de jetons en
  novembre 2026), jamais zéro, et le journal du passage le signale. C'est le tarif le plus élevé
  des modèles ouverts à tous de la table : les modèles que Synaptane ne peut pas utiliser (les
  modèles « pro » d'OpenAI, par l'API Responses seulement) et les modèles à accès restreint en
  sont écartés, pour ne pas relever ce tarif pour tous les modèles inconnus. Ce n'est donc pas
  un plafond : un modèle à accès restreint plus cher (`gpt-6-cyber`, 20 $ / 100 $) doit avoir
  son tarif saisi.
- **L'indexation sémantique** compte dans la dépense du mois : jetons relevés chez le
  fournisseur, ou estimés par excès (un jeton pour trois caractères) quand il ne les indique
  pas, aux tarifs publics des modèles d'OpenAI, Gemini, Mistral et Voyage AI (pour un modèle
  inconnu, le tarif le plus élevé de son fournisseur ; les jetons gratuits de Voyage AI ne
  sont pas déduits). Ollama et les API compatibles OpenAI ne sont pas
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
