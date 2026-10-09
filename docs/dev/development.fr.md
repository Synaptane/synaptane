[English](development.md) | Français

# Développer Synaptane

## Prérequis

- [uv](https://docs.astral.sh/uv/), qui installe Python 3.12 si besoin.
- Docker, pour PostgreSQL et pgvector en développement.
- Git 2.28 ou plus récent.

## Démarrer

```sh
uv sync                                    # environnement et dépendances
docker compose -f compose.dev.yaml up -d   # PostgreSQL sur 127.0.0.1:5433
uv run synaptane --help
```

## Lancer l'application en local

Contre le PostgreSQL de `compose.dev.yaml` (utilisateur, mot de passe et base : `synaptane`),
depuis la racine du dépôt. `synaptane.toml`, `data/` et `local/` sont ignorés par Git.

```sh
uv run synaptane init --database-url postgresql+psycopg://synaptane:synaptane@127.0.0.1:5433/synaptane
echo 'allow_local_repos = true' >> synaptane.toml   # dépôts file:// : développement seulement
uv run synaptane db upgrade
uv run synaptane admin create --username admin --display-name "Admin"
uv run synaptane doctor
```

- `init` écrit `./synaptane.toml` (droits 600) et crée le dossier des données `./data/`, avec
  la clé maître `data/master.key` et les copies des dépôts `data/repos/`. Valeurs par défaut :
  `--data-dir ./data` (relatif au dossier du fichier de configuration) et
  `--public-url http://127.0.0.1:8000`.
- Pour un autre fichier : `--config` avant la commande (`uv run synaptane --config
  local/dev.toml init …`), ou `SYNAPTANE_CONFIG`, à redonner à chaque commande.
- Chaque clé du fichier peut venir d'une variable `SYNAPTANE_<CLÉ>`, qui l'emporte : par
  exemple `SYNAPTANE_DATABASE_URL`, ou `SYNAPTANE_ALLOW_LOCAL_REPOS=true` au lieu de la ligne
  `allow_local_repos`.
- `admin create` demande le mot de passe (12 caractères au moins), ou le lit dans
  `SYNAPTANE_ADMIN_PASSWORD`.
- En local, `doctor` signale en « attention » l'URL publique en HTTP et les outils facultatifs
  absents (Tesseract, client PostgreSQL, age, rclone) : sans conséquence pour développer.

Puis, dans deux terminaux :

```sh
uv run synaptane serve     # web, REST et MCP sur http://127.0.0.1:8000
uv run synaptane worker    # tâches de fond : synchronisation Git, indexation, rangement
```

`serve` et `worker` refusent de démarrer sur une base en retard : relancez `db upgrade` après
une mise à jour qui apporte une migration. Pour un autre port, `serve --port 8001`, et
`public_url = "http://127.0.0.1:8001"` dans `synaptane.toml`.

### Relier un espace à un dépôt local

Pas besoin de forge : un dépôt nu sur le disque suffit.

```sh
git init --bare -b main local/notes.git
echo "file://$PWD/local/notes.git"   # l'adresse à donner à l'espace
```

1. Connectez-vous sur http://127.0.0.1:8000 avec le compte `admin`.
2. **Administration** → **Nouvelle organisation**, par exemple `acme` → **Créer
   l'organisation**.
3. Dans l'organisation : **Nouvel espace**, **Adresse du dépôt Git** : l'adresse `file://…`
   ci-dessus, branche `main` → **Créer l'espace**.

Un dépôt `file://` ne demande aucun accès à renseigner. Le worker prépare le dépôt vide
(premier commit avec `content/README.md` et `.synaptane/inbox/`) : l'espace est prêt. Un
`git clone local/notes.git` permet ensuite d'y pousser des pages comme le ferait un humain.

`allow_local_repos` est réservé au développement et aux tests : sur un serveur, il laisserait
un administrateur d'organisation faire lire au worker n'importe quel dépôt Git de la machine.
Sans lui, la synchronisation d'un dépôt `file://` échoue.

## Vérifier

```sh
./scripts/check.sh            # lockfile, ruff, format, mypy strict, pytest
./scripts/check.sh tests/unit # les arguments sont transmis à pytest
```

- `tests/unit` : aucune dépendance.
- `tests/integration` : un vrai PostgreSQL, celui de `compose.dev.yaml` par défaut. Chaque
  session crée une base jetable puis la supprime. Pour viser une autre instance :
  `SYNAPTANE_TEST_DATABASE_URL`.
- `tests/eval` : banc d'évaluation du rangement et de la recherche, hors de la suite
  normale, lancé à la main avant de changer une consigne, un modèle ou le classement
  (`./scripts/eval.sh`, voir [eval.fr.md](eval.fr.md)). Son autocontrôle avec le modèle simulé
  tourne en CI.

La CI (`.github/workflows/ci.yml`) exécute le même script, puis construit l'image et lance
`scripts/e2e-compose.sh` : la pile Compose complète, de l'installation à la restauration
après un `down -v`. En local, sans les CLI pour aller plus vite :

```sh
docker build --build-arg AGENT_CLIS=false -t synaptane:e2e .
SYNAPTANE_IMAGE=synaptane:e2e ./scripts/e2e-compose.sh
```

## Nouvelle migration

```sh
uv run python -c "from alembic import command; from synaptane.storage.db import alembic_config; \
  command.revision(alembic_config('postgresql+psycopg://synaptane:synaptane@127.0.0.1:5433/synaptane'), \
  message='…', autogenerate=True, rev_id='NNNN')"
```

La base `synaptane` doit être à jour (`uv run synaptane db upgrade`, voir
[Lancer l'application en local](#lancer-lapplication-en-local)) : Alembic compare les modèles à
son schéma.

`rev_id` : le numéro qui suit la dernière migration de `storage/migrations/versions/`
(`ls src/synaptane/storage/migrations/versions | tail -1`).
Relisez toujours le fichier généré, et nommez les contraintes avec `op.f(…)` : la
convention de nommage s'appliquerait deux fois. `tests/integration/test_migrations.py` défait
toutes les migrations, les refait, puis vérifie que le schéma correspond aux modèles.

## Traductions

Les textes de l'interface sont écrits en anglais, dans les gabarits (`_("…")`) et dans le code
(erreurs métier, messages de `redirect`, `Label`). `web/locales/fr.json` les traduit en
français (`"English text": "texte français"`) ; l'anglais n'a pas de catalogue, le texte est
sa propre traduction. `web/i18n.py` gère aussi les contextes (`_("Enabled", context="routine")`,
entrée `"Enabled|routine"`), les pluriels français de textes anglais invariables (une paire de
formes) et la typographie française (espaces insécables avant `:` `;` `?` `!`). Des tests
vérifient que chaque texte des gabarits et des appels qui traduisent figure dans `fr.json`,
avec les mêmes paramètres.

Les textes calculés par les services et affichés plus tard sont des `Label` (gabarit et
valeurs) : enregistrés (`Label.dumps`, résultat du test de connexion) ou envoyés par e-mail
(`mail.compose` les traduit pour chaque destinataire), ils sont traduits à l'affichage. Les
textes enregistrés en français avant cette bascule restent reconnus (erreurs Git, journal du
rangement, sujets de commits). Les erreurs REST gardent leur code `error` et traduisent leur
`message` selon `Accept-Language` (anglais par défaut) ; les réponses MCP restent en anglais.
Voir [CONTRIBUTING.fr.md](../../CONTRIBUTING.fr.md#textes-de-linterface) pour ajouter un texte
ou une langue.

## Organisation du code

`src/synaptane/` :

| Paquet | Rôle |
|---|---|
| `core/` | Règles pures : droits (`access.can`, seule autorité), identifiants, sécurité, erreurs. |
| `services/` | Cas d'usage : vérification des droits, écriture en base, audit. Seule porte d'entrée des interfaces. |
| `storage/` | PostgreSQL : modèles, connexion, migrations Alembic (`storage/migrations`). |
| `git/` | Dépôts des espaces : clone, commits, push, récupération, conflits. |
| `ai/` | Fournisseurs d'IA, derrière une même conversation à outils : API Anthropic, API compatibles OpenAI (OpenAI, Mistral, Ollama…), CLI d'abonnement (Claude Code, Codex) et le contrat qui vérifie leur isolation ; embeddings, tarifs publics et coût estimé, quota des abonnements. |
| `web/`, `api/`, `mcp/` | Interfaces web, REST et MCP. Elles ne contiennent aucune règle métier. |
| `server.py` | Assemblage FastAPI : routes, fichiers statiques, erreurs, en-têtes de sécurité. |
| `worker/` | Tâches de fond : synchronisation, indexation, rangement nocturne. |
| `cli.py` | Ligne de commande `synaptane` : `init`, `db upgrade`, `admin create`, `serve`, `worker`, `doctor`, `backup`, `restore`… |
| `config.py` | Réglages : fichier TOML (`synaptane.toml`), surchargé par les variables `SYNAPTANE_*`. |
| `extract.py` | Texte des PDF, documents Office et images (OCR par Tesseract), lu dans un processus à part aux ressources bornées. |

Dans `web/`, `pages.py` porte les fondations (contexte de requête, gabarits, redirections,
erreurs) ; chaque groupe de pages a son module et son routeur (`login_pages`, `org_pages`,
`space_pages`, `admin_pages`, `account_pages`, `wiki`, `inbox_pages`…). Le rangement est
partagé entre `services/tidy.py` (passage, planification) et `services/tidy_tools.py` (vue de
travail, outils et consignes de l'agent).

## Outils système pour les tests

- `rg` (ripgrep) est obligatoire.
- `pg_dump` et `pg_restore` en version 17 (paquet `postgresql-client-17`) : sans eux, les
  tests de `tests/integration/test_backup.py` qui sauvegardent sont **sautés**.
- `age` (avec `age-keygen`) et `rclone` : sans eux, les tests de la copie hors serveur qui
  chiffrent ou copient (`tests/unit/test_offsite.py`, `tests/integration/test_offsite.py`) sont
  **sautés**. La CI ne les installe pas : `scripts/e2e-compose.sh` exerce la copie hors
  serveur dans l'image, qui les contient.
- `tesseract`, avec `fra`, est nécessaire au test d'OCR (`tests/fixtures/scan-fr.pdf`), qui
  est sauté s'il manque. La CI l'installe.
- Sans paquet système, on peut emprunter le Tesseract d'une image Docker qui le contient, avec
  un petit script `tesseract` placé en tête du `PATH` :

  ```sh
  exec docker run --rm -i --network none -u "$(id -u):$(id -g)" -v /tmp:/tmp \
    --entrypoint tesseract <image> "$@"
  ```
