[English](README.md) | Français

# Résultats du banc du rangement

Ce dossier contient les résultats publiés du banc qui mesure le rangement de nuit
(`tests/eval/`, décrit dans [docs/dev/eval.fr.md](../../docs/dev/eval.fr.md)), pour comparer
modèles, efforts et invites au grand jour.

- `runs/` : un fichier par vrai lancement, écrit par le banc lui-même
  (`synaptane-tidy-bench-run`, format 1) : commit de Synaptane, empreintes du banc, de chaque
  scénario, de l'invite et des outils, le modèle et ses réglages, le tarif appliqué, et chaque
  essai avec ses correcteurs, ses jetons, son coût et sa durée.
- `results.json`, `results.md`, `results.fr.md` : le rapport construit à partir de ces
  fichiers par `./scripts/eval.sh report` (`synaptane-tidy-bench-results`, format 1). Jamais
  modifié à la main : reconstruit depuis `runs/`, il donne les mêmes octets.

Aucun résultat n'est encore publié : le dossier se remplit avec les premiers vrais lancements.
Rien ici n'est inventé ni simulé ; le modèle simulé du banc ne sert qu'à vérifier le banc
lui-même et n'écrit jamais ici.

## Comment ils sont produits

1. Sur une copie propre du commit à mesurer (aucun fichier suivi modifié), avec le PostgreSQL
   de développement (`docker compose -f compose.dev.yaml up -d`) :

   ```sh
   ./scripts/eval.sh tidy --estimate --model claude-sonnet-5-5 --trials 5   # gratuit
   export ANTHROPIC_API_KEY=sk-ant-…
   ./scripts/eval.sh tidy --model claude-sonnet-5-5 --effort medium --trials 5 --budget 4
   ```

   Une commande par configuration (modèle, effort, `--prompt FICHIER`, `--label NOM`), sur le
   même commit et avec le même nombre d'essais. Chacune écrit son fichier dans `runs/`, sauf
   s'il contient ce qui ressemble à un secret : rien n'est alors écrit ici.
2. `./scripts/eval.sh report`, qui refuse les lancements incomplets (coupés par le budget ou
   le fournisseur, essais manquants ou en double), moins de 3 essais, un essai servi par un
   autre modèle, un arbre modifié, des scénarios ou des commits différents, et tout ce qui
   ressemble à un secret ou à un chemin de la machine.
3. Validez `runs/` et le rapport ensemble.

## Comment les reproduire

Prenez le commit indiqué pour une configuration (section Provenance de `results.fr.md`),
vérifiez l'empreinte du banc (`bench.sha256`) et celle de l'invite (`config.prompt.sha256`),
puis relancez la même commande avec votre propre clé d'API. Attendez-vous aux mêmes taux de
réussite à leurs intervalles près, pas aux mêmes chiffres : un modèle ne répond pas deux fois
de la même façon, et un nom de modèle peut désigner plus tard un autre modèle chez son
fournisseur.

## Ce qu'il mesure, et ses limites

Chaque scénario dépose des notes dans l'inbox d'un espace neuf, copie d'un wiki fictif de
50 pages en français (`tests/eval/wikidata.py`, public) ; l'agent fait un passage, puis des
correcteurs écrits en code vérifient le résultat (faits rangés, périmètre respecté, éléments
laissés, pages créées, canaris d'injection non recopiés, liens, index, suppressions, texte
retiré). L'échantillon est petit (13 scénarios courts sur un seul wiki), les correcteurs
vérifient des invariants du résultat et non la qualité de l'écriture, et le coût est estimé à
partir des jetons au tarif public, pas facturé. Aucune donnée d'une instance n'est lue ni
envoyée.
