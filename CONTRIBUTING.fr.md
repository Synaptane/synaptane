[English](CONTRIBUTING.md) | Français

# Contribuer à Synaptane

Merci de l'intérêt que vous portez à Synaptane. Signalements de bugs, corrections de la
documentation, traductions et code : tout est bienvenu.

Synaptane est en **alpha (préversion)**. Il est fourni tel quel et maintenu au mieux par une
seule personne. Les relectures peuvent prendre du temps, et certaines propositions seront
refusées pour garder le projet simple et cohérent. Ne le prenez pas mal.

Toute personne qui participe au projet respecte le [code de conduite](CODE_OF_CONDUCT.md)
(texte officiel en anglais ;
[traduction française officielle](https://www.contributor-covenant.org/fr/version/2/1/code_of_conduct/)).
Une faille de sécurité ne se signale jamais en public : voir [SECURITY.md](SECURITY.md).

## Avant de commencer

- **Ouvrez une issue avant tout changement non trivial** (nouvelle fonctionnalité, changement
  de comportement, refonte, nouvelle dépendance). S'accorder d'abord sur l'approche fait gagner
  du temps à tout le monde. Les coquilles et les petites corrections évidentes peuvent aller
  directement en pull request.
- Cherchez d'abord une issue ou une discussion existante.
- **Langue** : les issues et les pull requests peuvent être écrites en anglais ou en français.
  Pour les nouvelles contributions, le code, les commentaires et les messages de commit sont en
  anglais. Une partie du code et des commentaires existants est encore en français : inutile de
  les traduire dans un changement qui n'a rien à voir.

## Installer l'environnement de développement

### Prérequis

- [uv](https://docs.astral.sh/uv/), qui installe Python 3.12 si besoin.
- Docker avec Docker Compose, pour PostgreSQL et pgvector.
- [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) : **obligatoire**, la recherche
  en dépend.

Outils facultatifs. Sans eux, les tests qui en ont besoin sont **sautés**, et c'est normal :

| Outil | Utilisé par |
|---|---|
| `pg_dump` et `pg_restore` en version 17 (`postgresql-client-17`) | tests de sauvegarde et de restauration |
| `tesseract` avec la langue française (`tesseract-ocr-fra`) | test d'OCR |
| `age` et `rclone` | tests de sauvegarde externalisée |
| `shellcheck` | vérification de `scripts/*.sh` et `deploy/*/*.sh` (en CI) |

La CI les installe tous : un test sauté sur votre machine y tourne quand même.

### Installation

```sh
uv sync                                    # environnement virtuel et dépendances
docker compose -f compose.dev.yaml up -d   # PostgreSQL sur 127.0.0.1:5433
uv run synaptane --help
```

Plus de détails dans le [guide de développement](docs/dev/development.fr.md).

## Vérifier ses changements

```sh
./scripts/check.sh              # vérification complète, la même qu'en CI
./scripts/check.sh tests/unit   # les arguments sont transmis à pytest (sans seuil de couverture)
```

`./scripts/check.sh` lance les mêmes vérifications que la CI, dans cet ordre :

1. `uv lock --check` : le fichier de verrouillage est à jour ;
2. `ruff check` : lint ;
3. `ruff format --check` : mise en forme (`uv run ruff format .` pour corriger) ;
4. `mypy` en mode strict, sur `src/` et `tests/` ;
5. `pytest` en parallèle, avec la couverture des lignes et des branches. La suite complète
   échoue sous **87 %**.

Les tests :

- `tests/unit` : aucune dépendance externe.
- `tests/integration` : un vrai PostgreSQL, par défaut celui de `compose.dev.yaml`. Chaque
  session crée une base jetable puis la supprime. Pour viser une autre instance :
  `SYNAPTANE_TEST_DATABASE_URL`.
- `tests/eval` : voir le [banc d'évaluation](#banc-dévaluation) plus bas.

La CI construit aussi l'image Docker et fait tourner la pile Compose complète de bout en bout
(`scripts/e2e-compose.sh`). Inutile de la lancer en local, mais c'est possible : voir le guide
de développement.

## Organisation du code

Tout se trouve dans `src/synaptane/` :

| Paquet | Rôle |
|---|---|
| `core/` | Règles pures : droits (`access.can` est la seule autorité), identifiants, sécurité, erreurs. |
| `services/` | Cas d'usage : vérification des droits, écriture en base, journal d'audit. |
| `storage/` | PostgreSQL : modèles, connexion, migrations Alembic (`storage/migrations`). |
| `git/` | Dépôts des espaces : clone, commits, push, récupération, conflits. |
| `web/`, `api/`, `mcp/` | Interfaces web, REST et MCP. |
| `worker/` | Tâches de fond : synchronisation, indexation, rangement nocturne. |
| `ai/` | Fournisseurs d'IA, derrière une seule interface de conversation à outils. |
| `server.py` | Assemblage FastAPI : routes, fichiers statiques, erreurs, en-têtes de sécurité. |

Règles :

- **Les services sont la seule porte d'entrée.** Les interfaces (`web/`, `api/`, `mcp/`)
  appellent les services ; elles ne touchent jamais directement à la base ni à Git.
- **Les interfaces ne contiennent aucune logique métier.** Les droits et les règles vivent
  dans `core/` et `services/`.
- **REST et MCP restent à parité.** Une action possible par l'un doit l'être par l'autre, avec
  les mêmes règles, sauf raison écrite.

## Migrations de la base

Les migrations utilisent Alembic. Pour en générer une (avec le PostgreSQL de développement
démarré) :

```sh
ls src/synaptane/storage/migrations/versions | tail -1   # dernier numéro
uv run python -c "from alembic import command; from synaptane.storage.db import alembic_config; \
  command.revision(alembic_config('postgresql+psycopg://synaptane:synaptane@127.0.0.1:5433/synaptane'), \
  message='…', autogenerate=True, rev_id='NNNN')"
```

- `rev_id` est **séquentiel** : le numéro qui suit la dernière migration.
- **Relisez toujours** le fichier généré : l'autogénération est un point de départ, pas un
  résultat.
- Nommez les contraintes avec `op.f(…)`, sinon la convention de nommage s'applique deux fois.
- `tests/integration/test_migrations.py` défait toutes les migrations, les refait, puis
  vérifie que le schéma correspond aux modèles.

## Textes de l'interface

Les textes de l'interface s'écrivent **en anglais** : entourés de `_("…")` dans les gabarits,
et passés tels quels aux fonctions qui les traduisent dans le code (`page.translate`,
`redirect(…, "…")`, erreurs métier comme `invalid("…")`, `Label("…")`). Leur traduction
française va dans le catalogue `src/synaptane/web/locales/fr.json`, sous la forme
`"English text": "texte français"`. Des tests vérifient que chaque texte des gabarits et de ces
appels figure dans `fr.json`, avec les mêmes paramètres : **tout texte nouveau ou modifié doit
avoir son entrée dans `fr.json`**. Si vous n'êtes pas à l'aise en français, dites-le dans la
pull request et le mainteneur l'ajoutera.

- Les valeurs passent par des paramètres, jamais par concaténation :
  `_("Page moved: {path}", path=path)`.
- Même texte anglais, français différent (genre, nom ou verbe) : un contexte,
  `_("Enabled", context="routine")`, traduit par l'entrée `"Enabled|routine": "Activée"`.
- Texte anglais invariable selon le nombre alors que le français varie : une paire,
  `"{n} left by tidying": ["{n} laissé par le rangement", "{n} laissés par le rangement"]`.
- Une valeur qui est elle-même un texte d'interface, ou un message calculé par les services et
  affiché plus tard (enregistré en base, envoyé par e-mail) : un `Label`, traduit à l'affichage.
- Les erreurs REST gardent un code `error` stable ; leur `message` suit `Accept-Language`
  (anglais par défaut). Les textes destinés aux modèles d'IA (consignes, descriptions d'outils,
  réponses MCP), les journaux et la ligne de commande sont en anglais et ne se traduisent pas.

Pour ajouter une langue : son code dans `LOCALES` (`web/i18n.py` et `services/accounts.py`)
et dans les menus de langue (`login.html`, `account-profile.html`), un catalogue
`locales/<code>.json` avec les clés de `fr.json`, sa règle de pluriel dans `i18n.plural`, et
ses textes d'e-mails dans `services/mail.py`. Les modèles de vocation et de routines peuvent
suivre (en anglais sinon).

## Banc d'évaluation

`tests/eval` mesure la qualité du rangement par l'IA et du classement de la recherche. Il ne
fait pas partie de la suite de tests normale. Lancez-le à la main avant de changer la consigne
ou les outils de l'agent, le modèle par défaut, ou le classement de la recherche :

```sh
./scripts/eval.sh          # modèle simulé, puis recherche : gratuit, environ 15 secondes
```

Avec un vrai modèle, il est **payant** (appels facturés sur votre clé) et exige un budget
explicite (`--budget`). La CI ne lance que l'autocontrôle gratuit. Voir
[docs/dev/eval.fr.md](docs/dev/eval.fr.md).

## Certificat d'origine du développeur (DCO)

Synaptane est sous [licence Apache 2.0](LICENSE). Il n'y a pas d'accord de licence de
contributeur (CLA). À la place, chaque commit doit être signé selon le
[Developer Certificate of Origin 1.1](https://developercertificate.org/).

En ajoutant une ligne `Signed-off-by`, vous certifiez que :

- (a) vous avez écrit la contribution, en tout ou partie, et avez le droit de la soumettre sous
  la licence libre du projet ; ou
- (b) elle s'appuie sur un travail antérieur sous une licence libre compatible, et vous avez le
  droit de la soumettre, avec vos modifications, sous la même licence ; ou
- (c) elle vous a été transmise par une personne qui a certifié (a), (b) ou (c), et vous ne
  l'avez pas modifiée ;
- (d) vous comprenez que la contribution et votre signature (nom et adresse e-mail compris)
  sont publiques et conservées indéfiniment.

Le texte de référence, en anglais, est sur <https://developercertificate.org/>. Pour signer,
commitez avec `-s` :

```sh
git commit -s -m "Fix search ranking for exact title matches"
```

Cela ajoute une ligne comme `Signed-off-by: Jeanne Dupont <jeanne@example.com>`, avec le nom
et l'adresse de votre configuration Git. Utilisez votre vrai nom (ou celui sous lequel vous
êtes publiquement connu). Pour signer des commits déjà faits : `git commit --amend -s` pour le
dernier, ou `git rebase --signoff main` pour une branche.

## Contributions assistées par IA

Les contributions écrites avec l'aide d'un assistant IA sont acceptées, à ces conditions :

- vous avez **relu** chaque ligne et **comprenez** ce qu'elle fait ;
- vous en **assumez la responsabilité**, comme si vous l'aviez écrite vous-même (y compris la
  signature DCO : assurez-vous d'avoir le droit de la soumettre) ;
- vous **le signalez dans la pull request** (le modèle a une case à cocher), en disant en
  quelques mots comment l'assistant a servi.

Les pull requests qui semblent générées sans relecture seront fermées.

## Pull requests

- **Une pull request, un sujet.** Séparez les changements sans rapport.
- `./scripts/check.sh` passe.
- Des tests sont ajoutés ou adaptés pour tout changement de comportement.
- La documentation est mise à jour **en anglais et en français** : chaque page existe en
  `page.md` (anglais) et `page.fr.md` (français), dans le même dossier. Si vous ne pouvez pas
  écrire l'une des deux langues, dites-le et le mainteneur vous aidera.
- Ajoutez une entrée au [CHANGELOG.md](CHANGELOG.md), dans la section de la version à paraître,
  pour tout changement visible d'un utilisateur ou d'un administrateur.
- Aucun secret, jeton ni donnée personnelle réelle, ni dans le code, ni dans les tests, les
  données de test ou les captures d'écran.
- Tous les commits sont signés (DCO, voir plus haut).
- Les pull requests sont fusionnées par **squash** : le titre de la pull request devient le
  message de commit ; faites-en une phrase courte et claire, en anglais.

Le modèle de pull request reprend cette liste. Merci !
