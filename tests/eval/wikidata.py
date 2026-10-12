"""Wiki du banc d'évaluation : l'équipe informatique fictive d'Acme, 50 pages.

Assez grand pour que l'agent ait à chercher (huit dossiers, index, liens croisés, liens wiki),
assez petit pour qu'un espace se crée en une fraction de seconde. Les faits sont précis et
uniques, pour que les correcteurs puissent vérifier qu'un fait a été intégré, ou qu'une page
n'a pas bougé. Ne pas modifier une page sans relire les scénarios et les requêtes qui en
dépendent (`scenarios.py`, `search_bench.py`)."""

PAGES: dict[str, str] = {
    "content/README.md": """# Wiki de l'équipe informatique

Bienvenue. Les pages sont rangées par dossier :

- [Infrastructure](infra/README.md)
- [Réseau](reseau/README.md)
- [Outils](outils/README.md)
- [Équipe](equipe/README.md)
- [Procédures](procedures/README.md)
- [Projets](projets/README.md)
- [Décisions](decisions/README.md)
- [Clients](clients/README.md)

Voir aussi le [glossaire](glossaire.md) et la [FAQ](faq.md).
""",
    "content/glossaire.md": """# Glossaire

- **PRA** : plan de reprise d'activité, voir [la restauration](procedures/restauration.md).
- **VLAN** : réseau virtuel, voir [les VLAN](reseau/vlan.md).
- **Astreinte** : permanence hors des heures ouvrées, voir [l'astreinte](equipe/astreinte.md).
- **Bastion** : serveur d'accès SSH unique, `bastion.acme.lan`.
""",
    "content/faq.md": """# Foire aux questions

**Comment accéder au réseau depuis chez moi ?** Avec le [VPN](outils/vpn.md).

**Où sont les mots de passe partagés ?** Dans le [coffre](outils/coffre-mots-de-passe.md).

**Qui appeler la nuit ?** La personne d'[astreinte](equipe/astreinte.md).
""",
    # Infrastructure --------------------------------------------------------------------
    "content/infra/README.md": """# Infrastructure

- [Serveurs](serveurs.md)
- [Proxmox](proxmox.md)
- [Stockage](stockage.md)
- [Sauvegardes](sauvegardes.md)
- [Certificats](certificats.md)
- [Supervision](supervision.md)
- [DNS](dns.md)
- [Bases de données](base-de-donnees.md)
- [Kubernetes](kubernetes.md)
""",
    "content/infra/serveurs.md": """# Serveurs

| Nom | Rôle | Emplacement |
|---|---|---|
| web1 | site public | baie A |
| web2 | site public (secours) | baie A |
| db1 | PostgreSQL principal | baie B |
| pve1 | hyperviseur [[proxmox]] | baie B |

Les serveurs physiques sont sous contrat de maintenance Dell jusqu'en 2027.
""",
    "content/infra/proxmox.md": """# Proxmox

Cluster de trois hyperviseurs (pve1, pve2, pve3), version 8.

La sauvegarde nocturne des machines virtuelles tourne à 2 h avec vzdump, vers le NAS
(voir [Sauvegardes](sauvegardes.md)).

Choix expliqué dans la [décision de 2025](../decisions/2025-01-choix-proxmox.md).
""",
    "content/infra/stockage.md": """# Stockage

Le NAS Synology de la baie B offre 8 To utiles en RAID 6.

Il reçoit les sauvegardes vzdump et les partages de fichiers de l'équipe.
""",
    "content/infra/sauvegardes.md": """# Sauvegardes

- Machines virtuelles : vzdump chaque nuit à 2 h, conservées 14 jours.
- Bases PostgreSQL : pg_dump toutes les 6 heures, copiées hors site chez OVH.
- Restauration : voir la [procédure](../procedures/restauration.md).
""",
    "content/infra/certificats.md": """# Certificats

Les certificats publics sont émis par Let's Encrypt et renouvelés par Caddy.

Le certificat interne `*.acme.lan` est signé par l'autorité interne et expire le 30 juin 2027.
""",
    "content/infra/supervision.md": """# Supervision

Zabbix surveille les serveurs ; les alertes partent sur le canal #alertes et, la nuit, vers
la personne d'[astreinte](../equipe/astreinte.md).

Seuil d'alerte disque : 85 %.
""",
    "content/infra/dns.md": """# DNS

Zone publique `acme.fr` chez le registraire Nordomaine ; zone interne `acme.lan` servie par deux
serveurs Unbound.

Durée de vie (TTL) par défaut : 3600 secondes.
""",
    "content/infra/base-de-donnees.md": """# Bases de données

PostgreSQL 16 sur db1, réplica en lecture sur db2.

Les sauvegardes sont décrites dans [Sauvegardes](sauvegardes.md).
""",
    "content/infra/kubernetes.md": """# Kubernetes

Pas de cluster en production pour l'instant : voir le projet
[migration vers le cloud](../projets/migration-cloud.md).
""",
    # Réseau ----------------------------------------------------------------------------
    "content/reseau/README.md": """# Réseau

- [Wifi](wifi.md)
- [Pare-feu](pare-feu.md)
- [VLAN](vlan.md)
- [Topologie](topologie.md)
""",
    "content/reseau/wifi.md": """# Wifi

Réseau du personnel : SSID « Acme-Pro », authentification WPA3 Entreprise.

Bornes Ubiquiti, une par étage.
""",
    "content/reseau/pare-feu.md": """# Pare-feu

Deux pare-feu OPNsense en haute disponibilité (CARP).

Toute ouverture de port passe par un ticket (voir [Tickets](../outils/tickets.md)).
""",
    "content/reseau/vlan.md": """# VLAN

| VLAN | Usage |
|---|---|
| 10 | serveurs |
| 20 | postes |
| 30 | téléphonie |
| 99 | administration |
""",
    "content/reseau/topologie.md": """# Topologie

Un cœur de réseau en fibre entre les baies A et B ; chaque étage est relié en 10 Gbit/s.

Le lien internet principal est une fibre dédiée de 1 Gbit/s, secourue par la 5G.
""",
    # Outils ----------------------------------------------------------------------------
    "content/outils/README.md": """# Outils

- [VPN](vpn.md)
- [GitLab](gitlab.md)
- [Messagerie](messagerie.md)
- [Coffre à mots de passe](coffre-mots-de-passe.md)
- [Tickets](tickets.md)
""",
    "content/outils/vpn.md": """# VPN

Accès distant par WireGuard ; le profil se télécharge depuis [GitLab](gitlab.md), dépôt
`infra/vpn`.

Chaque arrivée reçoit son profil le premier jour (voir l'[accueil](../equipe/onboarding.md)).
""",
    "content/outils/gitlab.md": """# GitLab

Instance interne `git.acme.lan`, intégration continue par GitLab CI
(voir la [décision](../decisions/2025-03-gitlab-ci.md)).
""",
    "content/outils/messagerie.md": """# Messagerie

Courrier sur Microsoft 365 ; discussions d'équipe sur Mattermost (`chat.acme.lan`).
""",
    "content/outils/coffre-mots-de-passe.md": """# Coffre à mots de passe

Vaultwarden, `coffre.acme.lan`. Une collection par équipe ; l'accès administrateur est réservé
à deux personnes.
""",
    "content/outils/tickets.md": """# Tickets

Les demandes passent par GLPI (`support.acme.lan`). Délai de prise en charge : 4 heures
ouvrées.
""",
    # Équipe ----------------------------------------------------------------------------
    "content/equipe/README.md": """# Équipe

- [Membres](membres.md)
- [Réunions](reunions.md)
- [Astreinte](astreinte.md)
- [Accueil d'un nouvel arrivant](onboarding.md)
- [Congés](conges.md)
""",
    "content/equipe/membres.md": """# Membres

- Claire Martin : responsable de l'équipe.
- Hugo Bernard : systèmes et virtualisation.
- Inès Petit : réseau et sécurité.
- Malik Robert : support et postes de travail.
""",
    "content/equipe/reunions.md": """# Réunions

La réunion d'équipe a lieu le lundi à 10 h, en salle Everest.

Revue des incidents : le premier jeudi du mois.
""",
    "content/equipe/astreinte.md": """# Astreinte

Une semaine sur quatre, du lundi 9 h au lundi suivant 9 h. Le planning est dans le calendrier
partagé « Astreinte ».

Les alertes de [supervision](../infra/supervision.md) arrivent par SMS.
""",
    "content/equipe/onboarding.md": """# Accueil d'un nouvel arrivant

1. Créer le compte (voir [l'arrivée](../procedures/arrivee-poste.md)).
2. Envoyer le profil [VPN](../outils/vpn.md).
3. Ajouter la personne à la collection du [coffre](../outils/coffre-mots-de-passe.md).
""",
    "content/equipe/conges.md": """# Congés

Les congés se posent dans Lucca, au moins deux semaines à l'avance. Deux personnes au plus
absentes en même temps.
""",
    # Procédures ------------------------------------------------------------------------
    "content/procedures/README.md": """# Procédures

- [Déploiement](deploiement.md)
- [Incident](incident.md)
- [Restauration](restauration.md)
- [Changement de mot de passe](changement-mot-de-passe.md)
- [Départ d'un collaborateur](depart-collaborateur.md)
- [Arrivée : préparer le poste](arrivee-poste.md)
""",
    "content/procedures/deploiement.md": """# Déploiement

Les mises en production passent par un pipeline [GitLab](../outils/gitlab.md), après revue de
code par une deuxième personne.
""",
    "content/procedures/incident.md": """# Incident

1. Ouvrir un ticket de priorité P1.
2. Prévenir la personne d'[astreinte](../equipe/astreinte.md).
3. Rédiger un compte rendu sous 48 heures.
""",
    "content/procedures/restauration.md": """# Restauration

Restaurer une machine virtuelle : depuis l'interface Proxmox, onglet « Sauvegarde », choisir
l'archive vzdump. Durée typique : 20 minutes pour 50 Go.

Un exercice de restauration a lieu chaque trimestre.
""",
    "content/procedures/changement-mot-de-passe.md": """# Changement de mot de passe

Les mots de passe des comptes de service changent tous les 12 mois ; la nouvelle valeur va
dans le [coffre](../outils/coffre-mots-de-passe.md).
""",
    "content/procedures/depart-collaborateur.md": """# Départ d'un collaborateur

Le jour du départ : désactiver le compte, révoquer le profil VPN, récupérer le portable.
""",
    "content/procedures/arrivee-poste.md": """# Arrivée : préparer le poste

Portable Dell Latitude préparé par Malik, avec chiffrement BitLocker et le client
[VPN](../outils/vpn.md).
""",
    # Projets ---------------------------------------------------------------------------
    "content/projets/README.md": """# Projets

- [Migration vers le cloud](migration-cloud.md)
- [Refonte du site](refonte-site.md)
- [Ceph](ceph.md)
- [Observabilité](observabilite.md)
""",
    "content/projets/migration-cloud.md": """# Migration vers le cloud

Étude en cours pour déplacer les services web chez Scaleway, avec un cluster Kubernetes
managé. Décision attendue au premier trimestre 2027.
""",
    "content/projets/refonte-site.md": """# Refonte du site

Nouveau site public sous Astro, mise en ligne prévue en décembre 2026, hébergé sur web1 et web2.
""",
    "content/projets/ceph.md": """# Ceph

Remplacement envisagé du NAS par un stockage Ceph réparti sur les trois hyperviseurs.
""",
    "content/projets/observabilite.md": """# Observabilité

Évaluation de Prometheus pour compléter Zabbix ([supervision](../infra/supervision.md)).
""",
    # Décisions -------------------------------------------------------------------------
    "content/decisions/README.md": """# Décisions

- [2025-01 : Proxmox pour la virtualisation](2025-01-choix-proxmox.md)
- [2025-03 : GitLab CI pour l'intégration continue](2025-03-gitlab-ci.md)
- [2025-06 : politique des mots de passe](2025-06-politique-mots-de-passe.md)
- [2026-02 : télétravail](2026-02-teletravail.md)
""",
    "content/decisions/2025-01-choix-proxmox.md": """# Choix de Proxmox

Janvier 2025 : Proxmox remplace VMware, pour le coût des licences.
""",
    "content/decisions/2025-03-gitlab-ci.md": """# GitLab CI

Mars 2025 : Jenkins est abandonné au profit de GitLab CI.
""",
    "content/decisions/2025-06-politique-mots-de-passe.md": """# Politique des mots de passe

Juin 2025 : 14 caractères au minimum, double authentification obligatoire pour les comptes
administrateurs.
""",
    "content/decisions/2026-02-teletravail.md": """# Télétravail

Février 2026 : jusqu'à trois jours de télétravail par semaine, présence obligatoire le lundi.
""",
    # Clients ---------------------------------------------------------------------------
    "content/clients/README.md": """# Clients

- [Contrats](contrats.md)
- [Support](support.md)
""",
    "content/clients/contrats.md": """# Contrats

Les contrats d'hébergement se renouvellent au 1er janvier ; préavis de trois mois.
""",
    "content/clients/support.md": """# Support aux clients

Le support répond de 9 h à 18 h en semaine, par le portail GLPI ([tickets](../outils/tickets.md)).
""",
}
