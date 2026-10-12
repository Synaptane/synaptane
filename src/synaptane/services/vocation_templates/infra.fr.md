# Vocation de l'espace

## À quoi sert cet espace, et pour qui

La mémoire technique de l'équipe infrastructure et exploitation : ce qu'il faut savoir pour faire tourner, dépanner et faire évoluer nos serveurs, réseaux et services. Lu par l'équipe, les personnes d'astreinte et les assistants IA qui aident au diagnostic.

## Ce qu'on y range

- Une fiche par machine, service ou équipement réseau : rôle, emplacement, accès, dépendances.
- Les procédures (installation, mise à jour, sauvegarde, restauration, bascule).
- Les incidents et leurs comptes rendus : chronologie, cause, correction, actions de suivi.
- Les décisions d'architecture, avec leur date et leur raison.
- Les contrats, fournisseurs et échéances (certificats, domaines, licences).

## Ce qu'on n'y range pas

- Aucun mot de passe, jeton ou clé privée : seulement l'endroit où les trouver (coffre).
- Les tickets du quotidien sans enseignement durable : laisser dans l'inbox avec la raison.
- La documentation publique d'un logiciel : un lien suffit.

## Organisation des dossiers

- `serveurs/` : une fiche par machine (`nom-de-la-machine.md`).
- `services/` : une fiche par service (base de données, messagerie, supervision…).
- `reseau/` : plan d'adressage, pare-feu, VPN, DNS.
- `procedures/` : une page par procédure, titre à l'infinitif (« Restaurer une sauvegarde »).
- `incidents/` : un compte rendu par incident, `AAAA-MM-JJ-sujet.md`.
- `decisions/` : une page par décision, `AAAA-MM-JJ-sujet.md`.
- `fournisseurs/` : contrats, contacts, échéances.

## Conventions

- Langue : français ; termes techniques usuels en anglais (load balancer, failover).
- Ton : factuel et précis, phrases courtes.
- Noms de fichiers : minuscules et tirets, sans accents.
- Fiche : rôle, accès, configuration utile, supervision, sauvegarde, liens. Procédure : prérequis, étapes numérotées, commandes dans des blocs de code, vérification, retour arrière.

## Ce qui compte le plus

- L'exactitude : une commande ou une adresse fausse coûte cher pendant un incident. En cas de doute ou de contradiction, laisser l'élément dans l'inbox avec la question.
- La fraîcheur : dater les informations susceptibles de changer (versions, adresses IP).
- Pages vérifiées : les procédures et les fiches de service ont un propriétaire et sont vérifiées tous les 6 mois (« Marquer comme vérifiée ») ; une page à revoir se relit avant de s'y fier en incident.
- La confidentialité : jamais de secret dans une page.

## Vocabulaire

- PRA : plan de reprise d'activité.
- Astreinte : la personne joignable hors des heures ouvrées.
