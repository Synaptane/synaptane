[English](eval.md) | Français

# Banc d'évaluation du rangement et de la recherche

Sans mesure, chaque changement de consigne, de modèle ou de classement est un saut dans le
noir. Le banc de `tests/eval/` mesure le **résultat** d'un passage (l'état du wiki après
coup), pas le chemin suivi, avec des correcteurs écrits en code : gratuits et déterministes.

À lancer **à la main**, avant de changer :
- la consigne de l'agent (`INSTRUCTIONS` de `services/tidy_tools.py`) ou ses outils ;
- le modèle ou l'effort par défaut ;
- le classement de la recherche (`services/search.py`, `services/semantic.py`).

Il faut le PostgreSQL de développement (`docker compose -f compose.dev.yaml up -d`) : chaque
lancement crée une base jetable, puis la supprime.

## Lancer

```sh
./scripts/eval.sh                       # modèle simulé, puis recherche : gratuit, ~15 s
./scripts/eval.sh tidy --scenario deplacement --scenario doublon
./scripts/eval.sh search
uv run python -m tests.eval --help      # toutes les options
```

En CI, seul l'autocontrôle tourne (`tests/integration/test_eval_bench.py`) : modèle simulé
et recherche, jamais de vrai modèle.

### Avec un vrai modèle (payant, jamais en CI)

```sh
export ANTHROPIC_API_KEY=sk-ant-…        # lue dans l'environnement, jamais en argument
./scripts/eval.sh tidy --model claude-sonnet-5-5 --effort medium --budget 3 --trials 3 \
  --json resultats-sonnet-medium.json
```

- `--budget` (en €) est **obligatoire** : le banc arrête le modèle dès que le coût cumulé
  l'atteindrait (le dernier tour peut le dépasser un peu) et ne joue plus les scénarios
  suivants. Le coût suit les tarifs publics de `ai/pricing.py` (écritures en cache à 1,25 fois
  l'entrée) ; pour un autre modèle, `--price-input` et `--price-output` (€ par million).
- `--trials 3` : trois essais par scénario, pour voir la variance d'un modèle.
- `--provider` : tout fournisseur par clé d'API (`anthropic`, `openai`, `mistral`, `ollama`,
  `openai_compatible`, avec `--base-url` et `--api-key-env`). Les abonnements (Claude Code,
  Codex) ne sont pas proposés : le banc ne touche jamais à leurs identifiants.
- Ordre de grandeur : 13 scénarios d'un ou deux éléments, de quelques centimes à quelques
  dizaines de centimes chacun selon le modèle ; commencez par `--budget 1`.

**À faire : relancer en mode réel après la traduction des consignes.** Les consignes de
l'agent sont désormais en anglais, alors que le wiki et les notes du banc restent en
français : le banc doit être relancé avec un vrai modèle pour vérifier que l'agent range
toujours correctement et écrit en français (la langue des pages du wiki) :

```sh
./scripts/eval.sh tidy --model claude-sonnet-5-5 --effort medium --budget 1 --trials 1
```

Pour comparer deux réglages (Sonnet 5.5 en `medium` et en `high`, Opus 5.5 en `medium`…),
lancez-les avec le même `--trials` et comparez le **coût par passage réussi** et par élément
rangé, affichés à la fin : une requête moins chère qui rate son scénario ne fait pas
d'économie.

## Lire les résultats

Une ligne par passage : `ok` ou `ÉCHEC`, le scénario, le mode (`bon`, `mauvais` ou le modèle),
les jetons et le coût, puis chaque correcteur en échec avec son explication.

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
[Intelligence artificielle et rangement nocturne](../admin/ai.fr.md), « Bloc Qualité du
journal ».

Clés principales : `pages` (créés, modifiés, déplacés, supprimés, touchés), `broken_links`
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
