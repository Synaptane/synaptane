[English](cli.md) | Français

# Ligne de commande

`synaptane [--config FICHIER] <commande>` (configuration par défaut : `$SYNAPTANE_CONFIG`, sinon
`./synaptane.toml`). Avec Docker : `docker compose exec app synaptane …`.
`synaptane --version` affiche la version.

| Commande | Rôle |
|---|---|
| `init [--database-url …] [--data-dir …] [--public-url …]` | Écrit `synaptane.toml` s'il n'existe pas, crée les dossiers et la clé maître. Sans `--database-url`, utilise les variables `SYNAPTANE_*`. Ne remplace jamais une clé existante. |
| `db upgrade` | Applique les migrations de la base. |
| `admin create --username … [--display-name …] [--email …]` | Crée un super-admin. Mot de passe demandé, ou lu dans `SYNAPTANE_ADMIN_PASSWORD`. |
| `admin reset-2fa --username …` | Désactive la double authentification d'un compte, pour une personne qui a perdu son téléphone et ses codes de secours : elle se connecte avec son seul mot de passe, puis la réactive. Dans l'interface web, un super-admin fait de même depuis la page du compte (**Administration → Comptes**) ; cette commande sert au dernier super-admin bloqué. |
| `demo [--password …] [--source DOSSIER]` | Charge les données de démonstration : le wiki d'une équipe informatique fictive (`examples/acme-wiki/` par défaut, ou `--source`), dans un dépôt Git local sous `data_dir/demo/`. Crée ce qui manque (le super-admin `demo`, l'organisation `acme`, l'espace `acme/it`), puis synchronise et indexe l'espace ; relancée, elle ne recrée ni n'écrase rien. Le mot de passe de `demo` n'est fixé qu'à la création du compte : `--password`, sinon `SYNAPTANE_DEMO_PASSWORD`, sinon généré et affiché une fois. Exige `allow_local_repos = true` (`SYNAPTANE_ALLOW_LOCAL_REPOS=true`) pour cette commande, `serve` et `worker` : à réserver à une instance de démonstration jetable, car ce réglage permet à un admin d'organisation de relier un espace à n'importe quel dossier Git du serveur. Voir [Données de démonstration](../admin/installation.fr.md#données-de-démonstration). |
| `serve [--host 127.0.0.1] [--port 8000] [--forwarded-allow-ips 127.0.0.1]` | Web, API REST et MCP. |
| `worker` | Tâches de fond : synchronisation Git, indexation, rangement, webhooks, sauvegarde de la nuit (`backup_time`, dans `backup_dir`). |
| `reindex [--space org/espace]` | Vide le texte extrait et l'index sémantique, puis demande au worker de tout reconstruire. |
| `doctor` | Vérifie l'installation : clé, outils, base, migrations, HTTPS, sauvegarde automatique et copie hors serveur (en avertissement, `warning`, seulement). Code de sortie 1 en cas d'échec. |
| `check-clis [--installed-only]` | Vérifie, sans connexion ni appel au modèle, que les CLI d'abonnement (`claude`, `codex`) n'offrent à l'agent que les outils de Synaptane. Les deux sont exigés, comme dans une image construite avec eux (`AGENT_CLIS=true`, valeur par défaut du Dockerfile ; l'image publiée n'en contient aucun) ; avec `--installed-only`, un CLI absent n'est pas une erreur. Code de sortie 1 si le contrat est rompu ou non vérifié. Voir [Utiliser un abonnement Claude ou ChatGPT](../admin/ai.fr.md#utiliser-un-abonnement-claude-ou-chatgpt). |
| `backup [--output FICHIER\|DOSSIER\|-] [--without-repos] [--without-key] [--offsite]` | Sauvegarde, relue avant d'être annoncée. Sans `--output` : dans `$SYNAPTANE_BACKUP_DIR` (`/backups` avec Docker), sinon le dossier courant ; avec `--offsite` seul, aucune copie locale. `--offsite` chiffre l'archive (age) et la copie vers chaque destination rclone. Attend une sauvegarde déjà en cours. Codes : 0 tout va bien, 1 pas d'archive utilisable ou copie refusée, 3 archive bonne mais une destination en échec. Voir [Sauvegarde et restauration](../admin/backup.fr.md). |
| `backup check [--no-test]` | État des sauvegardes : sauvegarde automatique, dossier, espace libre, rétention, dernière archive, clé de chiffrement, chaque destination (un fichier témoin écrit, relu et effacé, sauf avec `--no-test`), dernière copie hors serveur. Pour ce qui manque, la commande à lancer. Code de sortie 1 si quelque chose échoue. |
| `backup setup` | Met en place la copie chiffrée hors serveur, question par question, dans un terminal (`docker compose exec -it app …`) : clé de chiffrement (une nouvelle paire dont la clé privée est affichée une fois et enregistrée nulle part, ou votre clé publique), stockage (assistant de rclone), destination (testée avant d'être enregistrée), première sauvegarde. Ce qui fonctionne déjà est gardé. |
| `offsite key [CLÉ_PUBLIQUE]` | Sans argument, fabrique une paire de clés age : la clé privée est affichée une fois sur la sortie standard, jamais enregistrée ; la clé publique est enregistrée dans le dossier de données. Avec une clé publique (`age1…`, `ssh-ed25519 …`), l'enregistre seulement. |
| `offsite add NOM:CHEMIN` | Teste une destination (écrit, relit et efface un fichier témoin), puis l'enregistre dans le dossier de données. |
| `offsite remove NOM:CHEMIN` | Retire une destination enregistrée par `offsite add` ou `backup setup`. |
| `offsite test` | Écrit, relit et efface un fichier témoin sur chaque destination hors serveur ; signale une clé age manquante. |
| `offsite list` | Archives chiffrées présentes sur chaque destination. |
| `restore ARCHIVE\|- [--force]` | Restaure une sauvegarde (fichier `.tar.gz`, ou `-` pour l'entrée standard), application et worker arrêtés. `--force` remplace une instance existante ou sa clé. Voir [Sauvegarde et restauration](../admin/backup.fr.md). |
