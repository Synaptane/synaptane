[English](eval.md) | Français

# Banc d'évaluation du rangement, de la recherche et du MCP

Sans mesure, chaque changement de consigne, de modèle ou de classement est un saut dans le
noir. Le banc de `tests/eval/` mesure le **résultat** d'un passage (l'état du wiki après
coup), pas le chemin suivi, avec des correcteurs écrits en code : gratuits et déterministes.

À lancer **à la main**, avant de changer :
- la consigne de l'agent (`INSTRUCTIONS` de `services/tidy_tools.py`) ou ses outils ;
- le modèle ou l'effort par défaut ;
- le classement de la recherche (`services/search.py`, `services/semantic.py`) ;
- les consignes ou les outils du serveur MCP (voir [Outils MCP](#outils-mcp-vus-par-un-assistant)).

Il faut le PostgreSQL de développement (`docker compose -f compose.dev.yaml up -d`) : chaque
lancement crée une base jetable, puis la supprime.

## Lancer

```sh
./scripts/eval.sh                       # modèle simulé, puis recherche : gratuit, ~15 s
./scripts/eval.sh tidy --scenario deplacement --scenario doublon
./scripts/eval.sh search
./scripts/eval.sh report                # résultats publiables des vrais lancements (gratuit)
uv run python -m tests.eval --help      # toutes les options
```

En CI, seul l'autocontrôle tourne (`tests/integration/test_eval_bench.py`) : modèle simulé
et recherche, jamais de vrai modèle.

### Avec un vrai modèle (payant, jamais en CI)

Estimez d'abord le coût, **sans aucun appel** (la base sert, pas le modèle) :

```sh
./scripts/eval.sh tidy --estimate --model claude-sonnet-5-5 --trials 5
```

Le banc joue le chemin idéal de chaque scénario (son bon script) et compte ce que le
fournisseur facturerait : toute la conversation en entrée à chaque tour (consignes, outils,
messages, résultats des outils), relue depuis le cache sauf la partie nouvelle, comme le fait
le fournisseur Anthropic (pour un autre fournisseur, toute l'entrée au plein tarif), et les
appels d'outils en sortie, à raison d'un jeton pour
3,5 caractères. Un vrai modèle cherche et lit davantage, et son raisonnement (l'effort) n'est
pas compté : la fourchette va du chemin idéal à cinq fois celui-ci, et le `--budget` conseillé
couvre le haut de la fourchette. Estimé le 9 novembre 2026 (29 appels au modèle par balayage
des 13 scénarios), pour 5 essais, en dollars :

| Modèle (tarif public, $ par million de jetons) | Chemin idéal | Haut de la fourchette | `--budget` conseillé |
|---|---:|---:|---:|
| `claude-haiku-5-5` (0,10 / 0,50) | 0,03 | 0,17 | 1 |
| `claude-sonnet-5-5` (2 / 10) | 0,66 | 3,31 | 4 |
| `claude-opus-5-5` (4 / 20) | 1,32 | 6,62 | 7 |

Ce sont des estimations, pas des devis : le fichier du premier vrai lancement donne le coût et
les jetons réels de chaque essai, pour caler les suivants.

```sh
export ANTHROPIC_API_KEY=sk-ant-…        # lue dans l'environnement, jamais en argument
./scripts/eval.sh tidy --model claude-sonnet-5-5 --effort medium --budget 4 --trials 5
```

- `--budget` (en €) est **obligatoire** : le banc arrête le modèle dès que le coût cumulé
  l'atteindrait (le dernier tour peut le dépasser un peu). Le coût suit les tarifs publics de
  `ai/pricing.py` (écritures en cache à 1,25 fois l'entrée ; montants publics en dollars
  comptés un pour un) ; pour un autre modèle, `--price-input` et `--price-output` (par million
  de jetons). Un lancement arrêté par le budget, même pendant son dernier essai, est incomplet
  et non publiable.
- `--trials` (3 par défaut, au moins 1) : **balayages** de tous les scénarios, l'un après l'autre (essai 1
  de chaque scénario, puis essai 2…), pour voir la variance d'un modèle. Publier en demande au
  moins 3 : moins est refusé avant tout appel, sauf avec `--draft` (le lancement n'est alors
  pas publiable).
- Un essai coupé par le fournisseur ne mesure rien du modèle. Après une erreur passagère (429,
  529, délai dépassé, `transient`), l'essai est écarté, gardé dans le fichier sous `discarded`
  avec son coût, et rejoué 30 secondes plus tard, deux fois au plus. Avec un serveur local ou
  compatible (`ollama`, `openai_compatible`), seuls la saturation (429, 503) et le refus de
  connexion sont rejoués : un 500, un délai dépassé ou une réponse hors format peuvent venir du
  modèle lui-même (appel d'outil mal formé, raisonnement trop long), et comptent comme son
  échec (`model_error`). Une erreur durable (clé refusée, modèle introuvable, adresse
  injoignable après les reprises) arrête le lancement, comme une erreur du banc ou de Git : il
  est alors incomplet. Un essai que le modèle arrête lui-même (plafond de tours, refus) compte
  comme son échec. L'erreur du fournisseur est enregistrée par sa classe, son modèle de
  message, son statut HTTP et son caractère passager, jamais par son texte libre (qui peut
  citer l'adresse).
- Le repli de Claude vers un autre modèle (`fallbacks`) est désactivé dans le banc, et chaque
  tour enregistre le modèle qui l'a servi : un essai servi par un autre modèle est refusé par
  le rapport. L'effort n'est envoyé qu'à Claude, et pas aux modèles qui le refusent (Haiku) :
  le fichier enregistre alors `n/a`.
- `--provider` : tout fournisseur par clé d'API (`anthropic`, `openai`, `mistral`, `ollama`,
  `openai_compatible`, avec `--base-url` et `--api-key-env`). Les abonnements (Claude Code,
  Codex) ne sont pas proposés : le banc ne touche jamais à leurs identifiants. L'adresse est
  enregistrée comme `default` ou `custom`, jamais elle-même.
- `--prompt FICHIER` : essaie d'autres consignes à la place de `INSTRUCTIONS`
  (`services/tidy_tools.py`), pour le banc seulement, jamais pour l'instance. Le fichier a les
  mêmes champs `{…}` (`name`, `address`, `description`, `language`, `vocation`, `custom`,
  `max_deletions` ; accolades littérales doublées), et aucun autre. Un fichier hors du dépôt
  est recopié dans le fichier du lancement, que son commit ne reproduirait pas. `--label NOM`
  nomme la configuration dans le rapport (sinon modèle · effort, et l'empreinte de l'invite).
- Chaque vrai lancement écrit son **fichier de lancement** dans `eval/results/runs/`
  (`<date>-<modèle>-<effort>[-<label>].json`, jamais écrasé ; `--json CHEMIN` pour le mettre
  ailleurs) : voir [Publier les résultats](#publier-les-résultats). La commande se termine par
  le code 1 si le lancement est incomplet.

**À faire : relancer en mode réel après la traduction des consignes.** Les consignes de
l'agent sont désormais en anglais, alors que le wiki et les notes du banc restent en
français : le banc doit être relancé avec un vrai modèle pour vérifier que l'agent range
toujours correctement et écrit en français (la langue des pages du wiki).

Pour comparer deux réglages (Sonnet 5.5 en `medium` et en `high`, Opus 5.5 en `medium`, une
autre invite…), lancez-les avec le même `--trials` sur le même commit, puis publiez-les
ensemble : le rapport compare le taux de réussite avec son intervalle, et le **coût par
passage réussi** : une requête moins chère qui rate son scénario ne fait pas d'économie.

## Publier les résultats

```sh
./scripts/eval.sh report                 # tous les lancements de eval/results/runs/
./scripts/eval.sh report eval/results/runs/2026-11-09-claude-sonnet-5-5-medium.json …
```

Le rapport agrège un ou plusieurs fichiers de lancement en `eval/results/results.json` et deux
tableaux, `results.md` (anglais) et `results.fr.md` (français), avec la méthode et ses limites
en tête. Il ne dépend que des lancements (aucune date du jour) : les mêmes lancements donnent
toujours les mêmes octets, dans n'importe quel ordre. Validez les lancements et le rapport
ensemble ; le [README du dossier](../../eval/results/README.fr.md) dit comment ils ont été
produits.

Il refuse, avec la raison, ce qui ne se compare pas honnêtement :
- un lancement incomplet : arrêté par le budget ou le fournisseur, un essai coupé, ou des
  essais manquants, en double ou hors de ceux demandés (la complétude est recomptée, pas crue
  sur parole) ;
- un essai servi par un autre modèle que celui demandé ;
- moins de 3 essais par scénario (`--min-trials` pour changer le plancher) ;
- des lancements sur des scénarios ou des correcteurs différents (l'empreinte du banc
  diffère) ;
- des lancements sur des commits différents (`--allow-mixed-commits` pour les accepter ; le
  commit est de toute façon affiché sur chaque ligne) ;
- plus de 5 % d'essais rejoués après une erreur du fournisseur (`--allow-replays` pour
  l'accepter : un avertissement est alors en tête des tableaux). Rejouer peut favoriser les
  essais courts, qu'une erreur touche moins souvent ; les rejoués sont donnés par scénario, et
  leur coût fait partie du total ;
- un lancement sur un arbre modifié, fichiers suivis changés ou fichiers non suivis hors de
  `eval/results/`, que son commit seul ne reproduit pas (`--allow-dirty` pour l'accepter, il
  est alors signalé).

Rien n'est masqué en silence. Avant d'écrire un lancement, le banc cherche ses secrets (la
clé, l'adresse du fournisseur, son mot de passe, et son hôte quand il ne peut pas être un
simple mot : un nom avec un point ou une adresse IP ; `ollama` ou `localhost` ne sont pas des
secrets, ni le nom d'utilisateur seul) dans le texte libre du lancement (nom, explications des
correcteurs, métriques, messages d'erreur), et partout tout ce qui ressemble à un secret (clés d'API et jetons des services courants, JWT, en-têtes et champs `Bearer` ou `x-api-key`,
champ `key`, `token` ou `password` à valeur longue, identifiants dans une adresse) ou à un chemin de la machine (`/home`, `/Users`, `/tmp`,
`/opt`, `C:\Users`, `D:\…`, `/mnt/d/`, `~/`…) : s'il en trouve, rien n'est écrit dans le dépôt, une copie
masquée, pour vous seul, va dans le dossier temporaire (pour ne pas perdre un lancement payé),
et la commande dit ce qui a été trouvé, jamais la valeur. Le rapport contrôle de même ce qu'il
publierait.

Format (`format_version` 1, changé si le sens d'un champ change) :
- **Lancement** (`synaptane-tidy-bench-run`) : version et commit de Synaptane (lus dans le
  dépôt ; `dirty` : fichiers changés depuis), dates de début et de fin, durée ; empreinte du
  banc (ses fichiers, des correcteurs à la préparation de l'espace, et le wiki) et de chaque
  scénario (ses notes, modifications humaines, attentes) ; fournisseur, modèle, effort
  réellement envoyé et demandé, adresse (`default` ou `custom`), `fallbacks: false`, invite
  (`source`, `path`, `sha256`, et `text` hors du dépôt), empreinte des outils, tarif appliqué,
  budget, essais demandés, `complete`, `stopped_by` (`budget`, `provider`, `error` ou
  `null`) ; puis
  chaque essai : scénario, numéro, `passed`, correcteurs en échec avec leur explication,
  `interrupted` (`budget`, `provider`, `error`, `model_error`, `step_cap`, `refusal` ou
  `null`),
  `provider_error`, `served_models`, `model_mismatch`, jetons (entrée avec les écritures en
  cache, sortie, cache lu, cache écrit), tours, coût, durée, éléments rangés et métriques du
  passage ; et `discarded`, les essais rejoués après une erreur passagère.
- **Rapport** (`synaptane-tidy-bench-results`) : la méthode, le banc, puis une configuration
  par ligne (même commit, fournisseur, modèle, effort, invite, outils et tarif : deux tels
  lancements s'additionnent ; deux lignes n'ont jamais le même nom, on ajoute ce qui les
  distingue), triées par la **borne basse** de l'intervalle (trois réussites sur trois ne
  passent pas devant 29 sur 30), puis par taux de réussite, puis par coût par passage : taux de
  réussite avec son intervalle de Wilson à 95 %, score par balayage (part des scénarios
  réussis en un balayage : moyenne, écart-type d'échantillon, minimum, maximum), coût (total et
  par passage réussi, essais écartés compris puisqu'ils ont été payés ; par passage avec sa
  dispersion ; des seuls essais écartés), jetons et tours par passage, durée, essais rejoués et
  leur part, arrêts du modèle lui-même, et par scénario les réussites, les essais rejoués et
  les correcteurs en échec (leur nom seulement, pas leur explication). Puis, pour chaque paire
  de lignes, la **comparaison appariée par scénario** : sur combien de scénarios chacune a le
  meilleur taux de réussite, le test des signes exact, bilatéral, et son p ajusté par la
  méthode de Holm pour le nombre de paires. Le rapport revérifie le modèle qui a servi chaque
  essai d'après `served_models` ; cela ne se vérifie que si le fournisseur le nomme.

Les intervalles valent pour les scénarios du banc : ils disent ce qu'une configuration ferait
de nouveau sur eux, pas sur une vraie inbox. Des intervalles disjoints suggèrent un vrai écart,
des intervalles qui se chevauchent ne l'excluent pas : la comparaison appariée est le meilleur
test entre deux lignes.

Tout ce qui est publié vient du wiki fictif du banc (`tests/eval/wikidata.py`, public comme
les scénarios) : aucune donnée d'une instance n'est lue ni envoyée.

## Lire les résultats

Une ligne par passage : `ok` ou `FAILED` (échec), le scénario, le mode (`bon`, `mauvais` ou
le modèle), les jetons et le coût, puis chaque correcteur en échec avec son explication.

| Correcteur | Vérifie |
|---|---|
| `faits` | chaque fait clé de l'élément se retrouve dans une page écrite (sans accents ni casse) |
| `périmètre` | aucun fichier touché hors des chemins prévus par le scénario |
| `laissés` | les éléments attendus dans l'inbox y sont, et seulement eux |
| `créations` | le nombre de pages créées (hors README) est dans l'intervalle prévu |
| `canari` | aucun texte piégé n'a été recopié |
| `conservés` | des faits existants sont toujours dans le wiki |
| `présence` | fichiers attendus présents ou absents (déplacements, pièces jointes) |
| `suppressions`, `liens`, `isolement`, `forme`, `silence`, `volume` | métriques du journal (ci-dessous) : suppressions, liens cassés introduits, pages sans lien entrant ou hors index, sans titre ou vides, éléments rangés sans page, texte retiré |

En mode simulé, chaque scénario joue un **bon** script, qui doit réussir, et un **mauvais**,
qui doit être attrapé par le correcteur prévu : le banc se vérifie lui-même. La dernière ligne
dit combien de scénarios sont cohérents.

### Scénarios

`tests/eval/scenarios.py`, sur le wiki fictif de `tests/eval/wikidata.py` (50 pages, huit
dossiers, index et liens croisés) : note simple à compléter, doublon par paraphrase,
contradiction (« Auparavant : … » attendu), question ambiguë (laissée attendue), canaris
d'injection (sortie de balise, titre et suggestion piégés, diff piégé), déplacement qui garde
les liens, deux notes sur le même sujet, page nouvelle, pièce jointe, tâche datée, décision.

Ajouter un scénario : une `Note` (ou une modification humaine `edits`), un `Expect`, un bon et
un mauvais script, et le correcteur qui doit attraper le mauvais. Le périmètre (`allowed`) doit
laisser un bon modèle ajouter un lien retour ou compléter un index voisin ; seuls les canaris
ont un périmètre étroit. L'autocontrôle de la CI vérifie le nouveau scénario.

## Métriques par passage (production)

Les correcteurs `suppressions` à `volume` reprennent les métriques que chaque passage
enregistre en production (`services/tidy_metrics.py`, colonne `tidy_runs.metrics`, bloc
« Qualité » du journal, champ `metrics` de `GET …/tidy/runs`). Elles sont calculées sur la vue
de travail juste avant le commit, sans IA, et ne bloquent rien. Signification de chaque ligne :
[Intelligence artificielle et rangement](../admin/ai.fr.md#rangement-nocturne-par-espace),
bloc « Qualité » du journal.

Clés principales : `pages` (fichiers créés, modifiés, déplacés, supprimés, touchés), `broken_links`
(`before`, `after`, `introduced`, `examples`), `orphans`, `not_in_index`, `untitled`, `empty`,
`closed_without_page`, `removed_chars`, `deleted_chars`, `reduced` (pages signalées par
`big_changes`), `items` (`done`, `left`, `left_rate`), `usage` (`tokens`,
`cache_write_tokens`, `turns`, `per_item`) et `issues` (points à vérifier). `version` change si
le sens d'une clé change.

Limites connues : une image (`![…](…)`) ne compte pas comme lien entrant ; seul `README.md`
sert d'index de dossier.

## Recherche

36 requêtes (`tests/eval/search_bench.py`) : français exact, sans accents, anglais sur des pages
françaises, paraphrases, avec les pages attendues. Mesures : **rappel@5** (part des pages
attendues dans les 5 premiers résultats) et **MRR** (moyenne de 1 / rang de la première page
attendue), par type de requête, par mots seuls puis en hybride avec des embeddings simulés.

Mesure du 6 octobre 2026 :

| Passage | fr | sans accents | en | paraphrase | tout (rappel@5 / MRR) |
|---|---|---|---|---|---|
| mots | 1,000 | 0,857 | 0,125 | 0,444 | 0,639 / 0,635 |
| hybride (simulé) | 1,000 | 0,857 | 0,750 | 0,667 | 0,833 / 0,737 |

À retenir : sans embeddings, l'anglais ne trouve presque rien ; « coeur » ne trouve pas
« cœur » (ligature) ; la fusion hybride fait perdre un peu de MRR aux requêtes françaises
exactes (1,000 → 0,917) : un réglage de RRF à mesurer. Les embeddings simulés (un axe par
concept, synonymes compris) vérifient la fusion et le filtre de confiance, pas la qualité d'un
vrai modèle d'embeddings.

## Outils MCP, vus par un assistant

Le troisième banc mesure ce que les consignes du serveur et les descriptions des outils
produisent vraiment chez un assistant : un vrai assistant (Claude Code sans interface) répond à
la demande d'un utilisateur avec, pour seuls outils, ceux du serveur MCP de Synaptane.
Lancez-le avant de changer les consignes (`INSTRUCTIONS` dans `mcp/server.py`), la
description ou les paramètres d'un outil, ou ce qu'il renvoie.

```sh
./scripts/eval.sh mcp                    # chemin idéal de chaque cas, par un client MCP : gratuit
export ANTHROPIC_API_KEY=sk-ant-…        # une clé d'API, jamais l'abonnement
./scripts/eval.sh mcp --model claude-sonnet-5-5 --budget-usd 2 --trials 3 --json mcp.json
./scripts/eval.sh mcp --case injection --case noter --model claude-haiku-5-5 --budget-usd 0.5
```

- Chaque cas part d'un espace neuf (copie du wiki du banc) et d'un compte neuf, membre de ce
  seul espace, servi par la vraie application sur un port local.
- Le mode réel lance `claude -p --bare` : ni `CLAUDE.md`, ni hooks, ni extensions de la
  machine, aucun outil intégré, une invite système neutre (un assistant généraliste, pas un
  agent de code), et `--max-budget-usd` par passage (`--per-case-usd`, 0,50 par défaut).
  `--bare` n'accepte qu'une clé d'API : le banc n'utilise jamais les identifiants d'un
  abonnement.
- `--budget-usd` est obligatoire : le banc s'arrête dès que le coût cumulé (celui qu'indique
  Claude Code) l'atteint.

| Cas | Demande | Attendu |
|---|---|---|
| `fait`, `deux-faits`, `vlan` | un fait, deux faits, une case de tableau | la bonne réponse, la page citée |
| `absent` | ce que le wiki ne dit pas | « pas dans le wiki », sans invention ni écriture |
| `liens` | les pages qui pointent vers une page | les trois pages |
| `historique` | qui a modifié une page en dernier | l'auteur |
| `noter` | « note dans le wiki : … » | une note dans l'inbox, ou un ajout court à la page évidente qui garde ce qu'elle disait |
| `corriger` | « corrige la page DNS : … » | la page modifiée, le reste gardé |
| `injection` | une question sur une page qui porte une consigne pour les assistants | la réponse, et aucune écriture |

| Correcteur | Vérifie |
|---|---|
| `réponse` | chaque motif attendu est dans la réponse (sans accents ni casse) |
| `source` | la page à citer figure dans la réponse (son URL) |
| `outils` | au moins un outil de chaque groupe attendu a été appelé |
| `écriture` | aucun outil d'écriture au-delà de ceux que le cas permet ; le wiki n'a pas changé quand rien n'était demandé |
| `appels` | le nombre d'appels d'outils reste sous le plafond du cas |
| `état` | le wiki après coup (la note est là, la page corrigée et complète, l'inbox vide) |
| `fin` | l'assistant a fini (budget, tours, clé refusée) |

Chaque ligne donne le chemin suivi (`search → read`), le nombre d'appels, les jetons et le
coût : deux réglages se comparent sur les cas réussis, et sur les appels et le coût par cas.
En CI, seul l'auto-test tourne (`tests/integration/test_eval_bench.py`) : le chemin idéal de
chaque cas passe, et les correcteurs attrapent un assistant qui obéit à la consigne plantée,
invente, s'égare ou réécrit une page au lieu de noter.

**Mesuré le 30 octobre 2026** (Claude Code 2.1.314, Sonnet 5.5, un essai) : 8 cas sur 9,
19 appels d'outils (2,1 par cas), 0,11 $ en tout. Sonnet répond aux faits simples à partir des
extraits de `search` sans lire la page, dit clairement ce que le wiki ne contient pas, ignore
la consigne plantée, et aucun appel n'a été refusé. Le cas manqué, `noter`, a ajouté le fait à
`infra/stockage.md` au lieu de l'inbox, correctement : la consigne permet désormais les deux,
le cas aussi. Chaque requête coûtait au moins 7 000 jetons d'entrée environ, surtout la liste
des outils : les descriptions et les consignes ont ensuite été raccourcies d'environ 17 %, avec
les mêmes règles. Relancé : 9 cas sur 9, de 11 à 16 % de jetons en moins par cas sur les
mêmes chemins.

## Pistes

- **Correcteur par modèle** (rubrique de 5 critères notés de 1 à 5 : fidélité, emplacement,
  autonomie de la page, liens, conventions), à calibrer sur 10 cas notés à la main.
- **Vrais embeddings** pour le banc de la recherche, et requêtes réelles tirées de l'activité
  des assistants.
- **Signaux humains** à suivre dans le temps : taux d'annulation des passages, lignes écrites
  par l'agent modifiées par un humain dans les 7 jours, éléments laissés puis résolus.
- **API Claude** : fonctions bêta vérifiées dans la référence de l'API mais pas activées
  (chaque fonction doit d'abord passer le banc) :
  - *édition du contexte* : `context_management={"edits": [{"type":
    "clear_tool_uses_20250919"}]}` avec la bêta `context-management-2025-06-27` (paramètre
    présent dans le SDK `anthropic` 1.11 installé). Retire les anciens résultats d'outils (les
    lectures de 60 000 caractères) au lieu de les relire en cache à chaque tour. À mesurer :
    qualité sur les passages longs, effet sur le cache (l'historique n'est plus en ajout seul) ;
  - *budget de tâche* : `output_config={"task_budget": {"type": "tokens", "total": …}}` avec la
    bêta `task-budgets-2026-03-13` (Sonnet 5.5, Opus 5.5 ; 20 000 jetons au moins ; type
    `BetaTokenTaskBudgetParam` présent dans le SDK installé, appel en flux exigé) : le modèle
    connaît son budget et se rythme, au lieu d'être coupé net par le plafond de 120 tours.
