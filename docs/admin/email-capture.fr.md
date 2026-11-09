[English](email-capture.md) | Français

# Capture par e-mail

Un e-mail envoyé ou transféré à `brain+organisation.espace@example.com` devient une note dans
l'inbox de cet espace, pièces jointes comprises, et le rangement de la nuit la classe comme
toute autre note. Le worker relève une boîte dédiée en IMAP toutes les quelques minutes : ni
webhook, ni serveur de messagerie entrant à faire tourner.

Rien n'est accepté sur la parole de l'expéditeur. Un e-mail n'est déposé que si :

1. son `From` est une seule adresse, l'adresse **vérifiée** d'un compte **actif** ;
2. le serveur de réception de la boîte l'a **authentifié** : DMARC est passé, ou une signature
   DKIM du domaine de l'expéditeur est valide (voir [Comment les expéditeurs sont
   authentifiés](#comment-les-expéditeurs-sont-authentifiés)) ;
3. l'adresse nomme un espace (`+organisation.espace`) où ce compte **peut déposer des notes** :
   la même règle que le formulaire web et l'outil `inbox_add` (contributeurs et administrateurs
   de l'organisation ; espaces restreints et groupes compris). Un super-admin ne dépose par
   e-mail que dans les espaces dont il est membre ;
4. il passe les contrôles de toute note : 10 pièces jointes au plus, de 25 Mo chacune, 35 Mo
   pour l'e-mail entier, pas de secret probable (clé d'API, mot de passe…), 120 notes par heure
   et par compte au plus.

La note est écrite au nom de la personne, par le canal **e-mail** (l'activité indique « par
e-mail », jamais « IA »), avec l'objet pour titre et, pour origine, « E-mail de *nom*,
*date* ». Son texte est la version texte de l'e-mail quand il en a une, sinon son HTML converti
en Markdown (scripts, styles et images distantes retirés ; liens gardés s'ils sont en `http`,
`https` ou `mailto`). Un e-mail transféré « en pièce jointe » est gardé en fichier `.eml`. Les
petites images du HTML (logos de signature, moins de 16 Ko) sont laissées de côté.

## Mise en place

### 1. Créer la boîte

Créez une boîte qui ne sert **qu'à cela**, par exemple `brain@example.com`, et vérifiez que son
fournisseur distribue les adresses avec `+` : envoyez un e-mail à `brain+test@example.com` ; il
doit arriver dans la boîte. Le worker déplace chaque e-mail traité hors du dossier relevé
(`INBOX` par défaut), dans `Synaptane/Processed` ou `Synaptane/Refused`, créés s'ils
n'existent pas : personne ne doit se servir de cette boîte pour autre chose.

Utilisez un **mot de passe d'application** quand le fournisseur en propose, plutôt que le mot
de passe du compte.

| Fournisseur | Serveur IMAP | Serveur de confiance (identifiant) | Remarques |
|---|---|---|---|
| Google Workspace, Gmail | `imap.gmail.com`, 993, SSL/TLS | `mx.google.com` | Activez IMAP (Workspace : console d'administration → Applications → Gmail → Accès des utilisateurs finaux). Mot de passe d'application : compte Google → Sécurité → Validation en deux étapes → Mots de passe des applications. Si votre administrateur les a désactivés, Google exige OAuth pour IMAP, que cette version ne prend pas en charge. |
| Fastmail | `imap.fastmail.com`, 993, SSL/TLS | `*.messagingengine.com` | Mot de passe d'application avec accès IMAP : Settings → Privacy & Security → Manage app passwords. |
| OVHcloud (MX Plan, E-mail Pro) | `ssl0.ovh.net` (MX Plan) ou `pro1.mail.ovh.net` (E-mail Pro), 993, SSL/TLS | voir **Tester la connexion** | Vérifiez que les adresses avec `+` sont distribuées et que les derniers e-mails portent un en-tête `Authentication-Results` (**Tester la connexion** l'indique) : sans lui, aucun expéditeur ne peut être authentifié chez ce fournisseur. |
| Microsoft 365 | `outlook.office365.com`, 993 | — | Exchange Online n'accepte plus de mot de passe en IMAP (l'authentification de base est coupée) : il faut OAuth, que cette version ne prend pas en charge. Hébergez la boîte de capture chez un autre fournisseur : les personnes sur Microsoft 365 peuvent toujours y écrire (voir DKIM plus bas). |
| Votre propre serveur (Dovecot…) | votre serveur | le nom qu'écrit votre MTA (OpenDKIM, OpenDMARC, rspamd : `authserv_id`) | Le MTA doit ajouter un en-tête `Authentication-Results` avec les résultats DMARC ou DKIM, et retirer ceux qui arrivent à son nom. |

### 2. Remplir les réglages

**Administration → E-mail → Capture par e-mail** (super-admin) :

- **Boîte IMAP** : serveur, port, sécurité (SSL/TLS sur 993, ou STARTTLS sur 143),
  identifiant, mot de passe (chiffré avec la clé maîtresse, jamais réaffiché, jamais
  journalisé), dossier relevé (`INBOX`).
- **Adresse de capture** : l'adresse de la boîte, sans `+` (`brain@example.com`). Chaque
  espace se joint alors à `brain+organisation.espace@example.com`, affichée sur sa page
  **Inbox**.
- **Serveurs de confiance** : l'identifiant au début de l'en-tête `Authentication-Results`
  qu'écrit le fournisseur de la boîte (`mx.google.com`). Plusieurs se séparent par des
  virgules ; `*.example.com` vaut pour tout serveur de ce domaine.
- **Domaines internes acceptés sur SPF seul** : laissez vide (voir plus bas).
- **Relever la boîte toutes les** : 2 minutes par défaut (de 1 à 60).
- **Répondre à l'expéditeur** : quand l'e-mail est refusé (par défaut), toujours
  (confirmation ou refus), ou jamais. Voir [Réponses](#réponses).

Puis **Tester la connexion** : le test se connecte, compte les e-mails en attente et nomme les
serveurs qui ont authentifié les derniers. Envoyez-vous d'abord un e-mail de test :
l'identifiant affiché est celui à indiquer comme fiable. **Relever maintenant** demande une
relève au worker sans attendre.

Une connexion non chiffrée est refusée : le mot de passe et chaque e-mail circuleraient en
clair. Pour un serveur de test local seulement, `allow_insecure_imap = true`
(`SYNAPTANE_ALLOW_INSECURE_IMAP`) permet le choix **Aucune**. Chaque changement de ces réglages
va au journal d'audit (« Capture par e-mail configurée », « Capture par e-mail supprimée »).

### 3. Faire passer DKIM et DMARC pour vos expéditeurs

La capture se fie à ce que le serveur de réception dit du domaine de l'expéditeur : les
domaines depuis lesquels vos équipes écrivent doivent donc signer leurs e-mails.

- **Google Workspace** : console d'administration → Applications → Gmail → Authentifier les
  e-mails → générez la clé DKIM, publiez l'enregistrement TXT `google._domainkey`, puis
  **Démarrer l'authentification**.
- **Microsoft 365** : portail Microsoft Defender → Paramètres d'authentification des e-mails →
  DKIM → votre domaine : publiez les deux enregistrements CNAME `selector1._domainkey` et
  `selector2._domainkey` indiqués, puis activez la signature (sans elle, Microsoft signe avec
  `onmicrosoft.com`, qui n'est pas aligné sur votre domaine).
- **OVHcloud** : dans l'espace client, activez DKIM pour le domaine (onglet DKIM de l'offre
  e-mail) et laissez OVHcloud publier l'enregistrement, ou publiez-le vous-même si le DNS est
  ailleurs.
- **Fastmail** (domaine personnalisé) : Settings → Domains → votre domaine : publiez les
  enregistrements DKIM indiqués.

Publiez ensuite un enregistrement DMARC, même permissif, pour que les destinataires indiquent
`dmarc=pass` : `_dmarc.example.com TXT "v=DMARC1; p=none; rua=mailto:dmarc@example.com"`. Les
adresses Gmail et Outlook.com personnelles sont déjà signées.

## Comment les expéditeurs sont authentifiés

N'importe qui peut falsifier l'en-tête `From`. Le serveur de réception de la boîte vérifie la
signature DKIM de l'expéditeur, SPF et la politique DMARC, et écrit les résultats dans un
en-tête `Authentication-Results` (RFC 8601) **en haut** de l'e-mail. La capture :

- ne lit **que l'en-tête `Authentication-Results` le plus haut**, celui ajouté à l'arrivée.
  Un expéditeur peut écrire un tel en-tête dans son propre e-mail, avec `dmarc=pass`, mais il
  se retrouve sous celui du serveur de réception ;
- exige que cet en-tête porte un **identifiant de confiance** : un en-tête écrit par tout
  autre serveur est ignoré ;
- accepte `dmarc=pass` pour le domaine du `From` (le résultat doit le nommer dans
  `header.from`, comme le fait tout serveur courant : un `From` mal formé comme
  `alice@acme.test <eve@evil.test>` peut être lu comme celui d'Ève par le serveur et comme celui
  d'Alice ailleurs), ou `dkim=pass` dont le domaine de signature est aligné sur lui (le même
  domaine, un parent ou un sous-domaine ; jamais un domaine de premier niveau seul). Un
  `dmarc=fail` explicite refuse l'e-mail ;
- ne lit rien d'un en-tête qui pourrait se lire de deux façons (un guillemet dans un
  commentaire, un commentaire non refermé : un serveur qui recopie l'adresse de l'expéditeur
  dans un commentaire le laisserait sinon écrire un résultat à lui), ni d'un en-tête qui porte
  deux résultats DMARC ;
- ne répond jamais à un e-mail non authentifié, ni à un expéditeur sans compte : pas de
  « backscatter » vers une adresse usurpée.

**Domaines internes acceptés sur SPF seul (moins sûr).** Pour un domaine qui ne signe rien (ni
DKIM ni DMARC), vous pouvez l'indiquer : un `spf=pass` dont le domaine d'enveloppe est aligné
sur le `From` suffit alors. SPF ne prouve que le serveur qui a envoyé l'e-mail : quiconque peut
envoyer par lui (une autre application, un autre client d'un serveur partagé) peut alors
écrire au nom d'un collègue. Laissez vide si vous pouvez mettre en place DKIM.

**Microsoft 365 : accepter son en-tête sans identifiant.** Exchange Online écrit son en-tête
`Authentication-Results` sans l'identifiant qu'exige la RFC 8601. Avec cette option, un tel
en-tête est accepté, et seulement s'il est le plus haut. Ne la cochez que si la boîte de
capture est hébergée sur Exchange Online (par une passerelle IMAP comme DavMail, puisque
Exchange Online refuse les mots de passe en IMAP) et laissez-la décochée avec tout autre
fournisseur : un serveur qui n'ajoute pas d'en-tête à lui laisserait passer celui de
l'expéditeur.

**Pourquoi pas une adresse secrète par personne ?** Un jeton secret dans l'adresse
(`brain+x7kq…@`) a été étudié et écarté : l'adresse circule en clair dans chaque e-mail, dans
les carnets d'adresses des destinataires et dans les réponses, donc elle fuit ; elle ne
protégerait que les domaines qui ne signent pas leurs e-mails, ce que l'option SPF couvre plus
franchement ; et une adresse personnelle ne pourrait pas être partagée sur la page Inbox d'un
espace. L'authentification par le serveur de réception, plus le compte et ses droits, est
vérifiée pour chaque e-mail, sans aucun secret à garder.

## La relève

- Toutes les **N minutes** (2 par défaut), le worker met une relève en file, dans sa propre
  voie : un serveur de messagerie lent ne retarde pas les synchronisations. Une seule relève à
  la fois sur l'instance, même avec plusieurs workers (verrou consultatif PostgreSQL).
- Une relève traite au plus 25 e-mails, du plus ancien au plus récent, pendant 4 minutes au
  plus ; le reste attend la suivante. Chaque opération réseau est bornée (30 secondes). Les
  parties d'un e-mail ne sont lues qu'une fois son expéditeur authentifié et doté d'un compte ;
  un e-mail de plus de 50 parties imbriquées ou de 2 000 parties est refusé comme trop complexe
  (l'analyseur de Python y passerait des minutes).
- Chaque e-mail traité est **déplacé** : dans `Synaptane/Processed` une fois déposé, dans
  `Synaptane/Refused` sinon. Sans l'extension IMAP `MOVE`, il est copié, marqué supprimé puis
  effacé.
- Un e-mail qui ne peut pas être déposé **tout de suite** (espace occupé, limite de débit du
  compte, dépôt indisponible) reste dans le dossier et est repris à la relève suivante, sans
  retenir les autres (il ne compte pas parmi les 25). Au bout de 24 heures, il est refusé
  (« Espace indisponible pendant 24 heures ») et son expéditeur prévenu. Un e-mail illisible
  n'arrête pas les autres : il va dans `Synaptane/Refused`.
- **Idempotente** : chaque e-mail est mémorisé par une empreinte de son `Message-ID` et de
  l'espace visé, pendant 90 jours. Le même e-mail reçu deux fois (deux copies, ou remis dans le
  dossier après une relève interrompue) n'est déposé qu'une fois. Pour réessayer un e-mail
  refusé (une fois que la personne a reçu le droit de déposer, par exemple), remettez-le de
  `Synaptane/Refused` dans le dossier relevé.
- **Routage** : l'espace est lu sur le destinataire de cette copie-là (`Delivered-To`,
  `X-Original-To`…), sinon sur `To` et `Cc` : un e-mail adressé à deux espaces va dans les deux
  (5 au plus). La casse ne compte pas (`Brain+Acme.Infra@…`) ; les identifiants ne contiennent
  ni point ni majuscule, donc `organisation.espace` ne peut pas se lire de deux façons.
  L'adresse nue (`brain@…`) ne nomme aucun espace : refusée, puisqu'un compte n'a pas d'espace
  par défaut.

## Réponses

Avec l'envoi d'e-mails configuré (**Administration → E-mail**), la capture peut répondre à
l'expéditeur, dans sa langue, avec un en-tête `Auto-Submitted: auto-replied` (aucun robot n'y
répond) :

- **Quand l'e-mail est refusé** (par défaut) : la raison (pas d'espace dans l'adresse, pas le
  droit de déposer dans cet espace ou espace inexistant, trop gros, trop de pièces jointes ou
  de parties, secret probable, espace indisponible pendant 24 heures). Un e-mail refusé ne
  reçoit qu'une réponse, même s'il est relevé à nouveau ;
- **Toujours** : aussi une confirmation avec le lien vers la note ;
- **Jamais**.

Un compte reçoit au plus 20 réponses par heure : au-delà, la capture cesse de lui répondre pour
l'heure, pour qu'un répondeur automatique qui ignore `Auto-Submitted` ne puisse pas lancer un
échange sans fin. Seul un expéditeur **authentifié** qui **a un compte** reçoit une réponse. Un e-mail non
authentifié, d'une adresse inconnue, ou automatique (réponse automatique, rebond, liste de
diffusion) est refusé en silence. L'avis du rangement (classée où, ou laissée pourquoi) part
toujours à la personne, comme pour toute note qu'elle dépose.

## Santé et supervision

- **Administration → Santé** : la ligne **Capture par e-mail** (dernière relève réussie,
  l'erreur de la dernière si elle a échoué, acceptés et refusés sur 7 jours) et **E-mails
  refusés par la capture** : les 10 plus récents, avec la raison, le domaine de l'expéditeur et
  l'espace visé. Ni l'objet, ni le contenu, ni l'adresse complète de l'expéditeur ne sont
  gardés.
- **`/metrics`** : `synaptane_email_capture_messages_24h{status, reason}`,
  `synaptane_email_capture_poll_failing`,
  `synaptane_email_capture_last_poll_timestamp_seconds`.

## Désactiver

**Désactiver la capture** efface les réglages et le mot de passe ; le worker cesse de relever
la boîte. Les notes déjà déposées restent ; les e-mails envoyés entre-temps attendent dans la
boîte.
