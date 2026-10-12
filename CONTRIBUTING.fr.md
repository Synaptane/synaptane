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
Une faille de sécurité ne se signale jamais en public : voir la
[politique de sécurité](SECURITY.fr.md).

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
| `catdoc` (`catdoc`, `xls2csv`) | tests des anciens fichiers Office `.doc` et `.xls` |
| `age` et `rclone` | tests de sauvegarde externalisée |
| `shellcheck` | vérification de `scripts/*.sh` et `deploy/*/*.sh` (en CI) |

Le job `check` de la CI installe `ripgrep`, `tesseract` avec le français, `catdoc` et
`postgresql-client-17`, et lance `shellcheck` (déjà présent sur la machine de CI) : les tests
qui en ont besoin y tournent même s'ils sont sautés sur votre machine. Il n'installe ni `age`
ni `rclone` : les tests de sauvegarde externalisée y sont donc sautés aussi. Ces deux outils
sont dans l'image Docker, et le job `image` fait une copie externalisée chiffrée pendant son
passage de bout en bout de la pile Compose.

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
  `"{n} left by the tidy-up": ["{n} laissé par le rangement", "{n} laissés par le rangement"]`.
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
contributeur (CLA). À la place, chaque commit doit être signé. En ajoutant une ligne
`Signed-off-by` à un commit, vous certifiez le Developer Certificate of Origin 1.1 ci-dessous,
reproduit mot pour mot depuis <https://developercertificate.org/> :

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

Traduction non contractuelle, seul le texte anglais fait foi.

> Certificat d'origine du développeur 1.1
>
> En contribuant à ce projet, je certifie que :
>
> (a) La contribution a été créée en tout ou partie par moi, et j'ai le droit de la soumettre
> sous la licence libre indiquée dans le fichier ; ou
>
> (b) La contribution s'appuie sur un travail antérieur qui, à ma connaissance, est couvert par
> une licence libre appropriée, et j'ai le droit, selon cette licence, de soumettre ce travail
> avec des modifications, créées en tout ou partie par moi, sous la même licence libre (sauf si
> je suis autorisé à le soumettre sous une autre licence), comme indiqué dans le fichier ; ou
>
> (c) La contribution m'a été transmise directement par une autre personne qui a certifié (a),
> (b) ou (c), et je ne l'ai pas modifiée.
>
> (d) Je comprends et j'accepte que ce projet et la contribution sont publics, et qu'une trace
> de la contribution (y compris toutes les informations personnelles que je soumets avec elle,
> dont ma signature) est conservée indéfiniment et peut être redistribuée conformément à ce
> projet ou à la ou aux licences libres concernées.

Pour signer, commitez avec `-s` :

```sh
git commit -s -m "Fix search ranking for exact title matches"
```

Cela ajoute une ligne comme `Signed-off-by: Jeanne Dupont <jeanne@example.com>`, avec le nom
et l'adresse de votre configuration Git. Utilisez votre vrai nom (ou celui sous lequel vous
êtes publiquement connu). Pour garder votre adresse privée, vous pouvez utiliser votre adresse
noreply GitHub, `ID+login@users.noreply.github.com` (affichée dans vos réglages d'e-mail
GitHub). Pour signer des commits déjà faits : `git commit --amend -s` pour le dernier, ou
`git rebase --signoff main` pour une branche, puis poussez avec `--force`.

Le [workflow DCO](.github/workflows/dco.yml) vérifie chaque commit d'une pull request (sauf
les commits de fusion) et nomme ceux qui n'ont pas de ligne `Signed-off-by`.

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
- Tous les commits sont signés ([DCO](#certificat-dorigine-du-développeur-dco), voir plus
  haut).
- Les pull requests sont fusionnées par **squash** : le titre de la pull request devient le
  titre du commit fusionné ; faites-en une phrase courte et claire, en anglais. Les messages
  de vos commits, avec leurs lignes `Signed-off-by`, en forment le corps.

Le modèle de pull request reprend cette liste. Merci !

## Pour les mainteneurs

### Fusionner les pull requests

Dans les réglages du dépôt (Settings → General → Pull Requests), gardez **Allow squash
merging** activé, avec comme message de commit par défaut **Pull request title and commit
details**. Le commit fusionné garde ainsi les lignes `Signed-off-by` des commits de la pull
request.

Le [workflow DCO](.github/workflows/dco.yml) vérifie chaque commit de chaque pull request, de
sa base à sa tête. Pour empêcher la fusion sans lui, rendez sa vérification (`Signed-off-by on
every commit`) obligatoire dans les règles de `main`.

### Publier une version

1. Dans une pull request, mettez la nouvelle version partout :
   - `pyproject.toml` : `version`, au format PEP 440 (`1.0.0rc1`, `1.0.0`). Lancez ensuite
     `uv lock` pour que `uv.lock` suive (sinon `uv lock --check` échoue).
   - `Dockerfile` : `ARG VERSION=`, même valeur.
   - `compose.yaml` : la valeur par défaut de `${SYNAPTANE_VERSION:-X.Y.Z}`, pour les deux
     images, au format semver de l'étiquette (`1.0.0-rc.1`, `1.0.0`).
   - `.env.example` : `SYNAPTANE_VERSION=`, au même format semver.
   - Les exemples de version (`--branch vX.Y.Z`, `SYNAPTANE_VERSION=`) de `README.md`,
     `README.fr.md`, `docs/admin/installation.md` et `docs/admin/installation.fr.md`.
   - `CHANGELOG.md` : la section `## [X.Y.Z] - AAAA-MM-JJ`, datée, et son lien en fin de
     fichier. Cette section devient les notes de version.

   `tests/unit/test_image_metadata.py` vérifie que le `Dockerfile`, `compose.yaml`,
   `.env.example` et les exemples `git clone --branch` suivent `pyproject.toml`.
2. Une fois la pull request fusionnée et la CI au vert, posez l'étiquette sur le commit
   fusionné de `main` et poussez-la :

   ```sh
   git switch main && git pull
   git tag -a v1.0.0 -m "v1.0.0"   # ou v1.0.0-rc.2 pour une préversion
   git push origin v1.0.0
   ```

3. L'étiquette lance [`release.yml`](.github/workflows/release.yml), qui :
   - vérifie que l'étiquette correspond à `pyproject.toml` (au sens de PEP 440 : `v1.0.0-rc.1`
     vaut `1.0.0rc1`) et que le commit est sur `main`, et ne publie rien sinon ;
   - relance la CI complète sur ce commit ;
   - publie les images `ghcr.io/synaptane/synaptane` et `ghcr.io/synaptane/synaptane-nginx`,
     étiquetées `X.Y.Z` et `X.Y`, et `latest` seulement pour une version finale (une
     préversion n'a que sa propre étiquette), avec SBOM, provenance et attestations signées ;
   - crée la GitHub Release (marquée comme préversion pour `-rc.N`), avec les notes tirées de
     `CHANGELOG.md` ;
   - si le secret `SITE_DISPATCH_TOKEN` existe, déclenche la synchronisation de la
     documentation du site (événement `repository_dispatch` de type `sync-docs` envoyé à
     `synaptane/website`). Sans ce secret, cette étape est sautée.
4. Suivez l'exécution dans l'onglet Actions, puis vérifiez la page de la version. Si la
   vérification de version ou de branche, ou la CI, échoue, rien n'est publié : corrigez la
   cause, supprimez l'étiquette (`git tag -d vX.Y.Z` et `git push --delete origin vX.Y.Z`),
   puis posez-la de nouveau.
