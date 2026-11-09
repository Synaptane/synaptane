[English](first-steps.md) | Français

# Premiers pas

Au premier démarrage, la page d'accueil du super-admin rappelle ces étapes.

1. **Administration** : onglet **Organisations** pour les créer (case cochée par défaut :
   vous en devenez administrateur, sinon vos assistants n'y verraient rien), onglet
   **Comptes** pour créer les comptes (chaque compte a sa page : droits, mot de passe).
2. Page de l'organisation, une page par sujet :
   - **Espaces**, puis « Nouvel espace » : nom, identifiant, adresse du dépôt Git ;
   - **Membres** : ajouter, nommer les administrateurs ;
   - **Groupes** : donner à une équipe l'accès à plusieurs espaces d'un coup ;
   - **Intelligence artificielle** : le fournisseur du rangement nocturne ;
   - **Recherche sémantique** et **Webhooks**, facultatifs ;
   - **Paramètres** : nom, description, rôle par défaut dans les espaces, suppression.
3. Espace, onglet **Paramètres** : **Dépôt Git** (adresse, accès, webhook, état et conflits),
   **Membres** (lecteur ou contributeur, groupes, espace restreint), **Rangement** (heure, fuseau,
   modèle),
   **Recherche sémantique** et **Général**.

Une suppression d'espace ou d'organisation demande de retaper son identifiant.

Pour relier un espace à son dépôt, voir [Git](git.fr.md). Pour la connexion par GitLab, GitHub,
Google ou OIDC, voir [SSO](sso.fr.md) ; pour prévenir d'autres outils, voir
[Webhooks](webhooks.fr.md). Tout cela est aussi possible par
l'[API REST](../reference/api.fr.md).

## Données de démonstration

Pour essayer Synaptane avant de relier vos propres dépôts, `synaptane demo` charge le wiki
d'une équipe informatique fictive ([examples/acme-wiki](../../examples/acme-wiki/README.md), en
anglais) : 50 pages, une vocation, six notes d'inbox qui couvrent les cas types du rangement,
des pages vérifiées, une page à revoir et un lien cassé. Huit fichiers autres que Markdown
sont reliés depuis les pages, pour voir ce que Synaptane en fait : un contrat en PDF, un bon
de livraison scanné (OCR), une checklist Word, une présentation, deux tableurs (dont un à
l'ancien format `.xls`), un schéma de baie (OCR) et un rapport de statut en HTML. Cherchez « Kestrel », un mot qui n'apparaît que dans le
scan (Tesseract requis, présent dans l'image Docker). Sur une instance initialisée et
migrée, elle crée ce qui manque, puis synchronise et indexe l'espace :
- un super-admin `demo`, dont le mot de passe est affiché une fois (ou donné par
  `SYNAPTANE_DEMO_PASSWORD`, ou `--password`) ;
- l'organisation `acme`, dont `demo` est administrateur ;
- l'espace `acme/it`, adossé à un dépôt Git local, `<data_dir>/demo/acme-it.git`.

Relancée, elle ne change rien : un compte existant garde son mot de passe, et ce que vous avez
modifié dans l'espace reste.

Le dépôt est local (`file://`) : Synaptane le refuse sans `SYNAPTANE_ALLOW_LOCAL_REPOS=true`
pour la commande, le serveur et le worker. **Démonstration et développement seulement** : ce
réglage permet à un administrateur d'organisation de relier un espace à n'importe quel dossier
Git du serveur. Faites la démonstration sur une instance jetable, pas sur celle de production.

Avec Docker Compose, `examples/compose.demo.yaml` le règle pour `app` et `worker` :

```sh
echo 'COMPOSE_FILE=compose.yaml:examples/compose.demo.yaml' >> .env
docker compose up -d
docker compose exec app synaptane demo
```

Avec une image construite par vos soins :
`COMPOSE_FILE=compose.yaml:compose.build.yaml:examples/compose.demo.yaml`.

Depuis les sources :

```sh
export SYNAPTANE_ALLOW_LOCAL_REPOS=true   # aussi dans les terminaux de serve et du worker
uv run synaptane demo
```

La commande affiche ce qu'elle a créé (`super admin demo`, `organization acme`…), l'adresse
à ouvrir, le mot de passe de `demo` et comment brancher un assistant IA. Elle rappelle aussi ce
qui garde le dépôt de démonstration synchronisé : dans un conteneur, le service Compose
`worker`, à laisser tourner ; depuis les sources, un `synaptane worker` à garder lancé, avec
`SYNAPTANE_ALLOW_LOCAL_REPOS=true`. Le rangement de l'inbox demande un fournisseur d'IA (page
de l'organisation, **Intelligence artificielle**). `docker compose down -v` efface toute
l'instance de démonstration.

**Étape suivante :** [brancher son assistant IA (MCP)](../guide/mcp.fr.md), puis relier
les espaces à leurs dépôts ([Dépôts Git](git.fr.md)).
