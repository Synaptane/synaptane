# Contrôles de l'équipe

<!-- Des règles que chaque page de l'espace doit suivre, vérifiées sans IA : dans Santé, à
chaque écriture et par le rangement de la nuit. Chaque titre de niveau 2 est une règle ; la
liste juste en dessous la décrit (les clés restent en anglais) :
- pattern : une expression régulière (syntaxe RE2), entre accents graves ;
- field et expect : un champ du front matter, attendu `present`, en liste YAML (`list`) ou
  en date (`date`) ;
- preset : email, phone, card ou iban (données personnelles, masquées dans les rapports) ;
- allow : des valeurs (ou des domaines d'e-mail) jamais signalées ;
- paths et except : des dossiers ou des globs, relatifs à content/ (`clients/`,
  `notes/**/*.md`) ;
- level : warn (signalé) ou block (l'écriture est refusée) ;
- message : ce qu'il faut faire.
- fix : yes ou no, si l'entretien de nuit corrige ce que les membres enfreignent (par
  défaut, les règles block et les données personnelles).
Supprimez les règles inutiles, adaptez les autres. -->

## Pas de crochets imbriqués dans un libellé de lien
- pattern: `\[[^\]\n]*\[[^\]\n]*\][^\]\n]*\]\(`
- message: Retirez les crochets du texte du lien : ils cassent le lien.

## Les tags sont une liste YAML
- field: tags
- expect: list
- message: Écrivez `tags: [a, b]`, ou un `- tag` par ligne.

## Les sources sont une liste YAML
- field: sources
- expect: list
- message: Écrivez `sources: [a, b]`, ou une `- source` par ligne.

## Chaque page a un type
- field: type
- expect: present
- paths: notes/
- message: Ajoutez `type:` au front matter (réunion, procédure, décision…).

## Date de mise à jour sans heure
- field: updated
- expect: date
- message: Écrivez la date au format AAAA-MM-JJ, sans l'heure.

## Pas de chemin de travail local
- pattern: `/private/tmp|scratchpad`
- level: block
- message: Retirez le chemin local : mettez un lien vers la page ou le fichier.

## Pas d'adresse e-mail de personne
- preset: email
- allow: example.com
- level: block
- message: Écrivez le nom de la personne avec un lien vers sa page, pas son adresse.

## Pas de numéro de téléphone
- preset: phone
- message: Les numéros de téléphone vont dans l'annuaire, pas dans le wiki.

## Pas de numéro de carte
- preset: card
- level: block
- message: N'écrivez jamais un numéro de carte : retirez-le.

## Pas d'IBAN
- preset: iban
- paths: fournisseurs/
- message: Les coordonnées bancaires vont à la comptabilité, pas dans le wiki.
