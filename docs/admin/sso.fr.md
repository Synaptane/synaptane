[English](sso.md) | Français

# Connexion SSO

Les comptes locaux (mot de passe) et la connexion par un fournisseur externe coexistent. Le
super-admin configure les fournisseurs dans **Administration → Connexion SSO**, ou par l'API
(`PUT /api/v1/sso-providers/{identifiant}`).

## Fournisseurs

| Type | Émetteur | Remarques |
|---|---|---|
| GitLab | `https://gitlab.com` par défaut ; l'URL de l'instance pour un GitLab auto-hébergé | Application OAuth avec les portées `openid`, `email`, `profile` |
| Google | `https://accounts.google.com` (par défaut) | Identifiants OAuth « Application Web » |
| GitHub | aucun | OAuth App (pas de GitHub App) ; l'e-mail principal vérifié est lu par l'API |
| OIDC générique | URL de l'émetteur (Keycloak, Authentik, Entra ID…) | Découverte par `/.well-known/openid-configuration` |

Chez le fournisseur, déclarez l'**adresse de retour** affichée par Synaptane :
`<public_url>/sso/<identifiant>/callback`. `public_url` doit donc être l'adresse publique
réelle de l'instance.

Le secret du client est chiffré par la clé maître et n'est jamais réaffiché. Le laisser vide
lors d'une modification conserve celui qui est enregistré.

## Rattachement des comptes

À la connexion, Synaptane cherche dans l'ordre :
1. une identité déjà rattachée (même fournisseur, même identifiant chez le fournisseur) ;
2. si l'option « Lier à un compte existant de même adresse vérifiée » est cochée, un compte
   existant avec la même adresse e-mail, **vérifiée** par le fournisseur **et** vérifiée chez
   Synaptane (lien reçu à cette adresse). Une adresse seulement déclarée sur un compte ne
   capte jamais l'identité de quelqu'un : elle est retirée de ce compte. L'option est
   conseillée pour GitHub et Google seulement (et activée par défaut pour eux par l'API) :
   un serveur GitLab ou OIDC auto-hébergé peut « vérifier » n'importe quelle adresse. Option
   décochée, la connexion est refusée et la personne est invitée à se connecter avec son mot
   de passe, puis à lier le fournisseur depuis **Mon compte → Sécurité** ;
3. sinon, si l'option « Créer automatiquement le compte à la première connexion » est
   cochée, un nouveau compte sans mot de passe. Sinon, la connexion est refusée.

**Créer automatiquement le compte à la première connexion** est décoché par défaut. Les
fournisseurs enregistrés avant ce changement de défaut gardent leur réglage. **Modifier** sur
un fournisseur remplit le formulaire avec tous ses réglages (type, libellé, émetteur,
identifiant du client, domaines, options), sauf le secret du client, qui reste vide :
réenregistrer sans rien toucher garde tout. Par l'API, `PUT /api/v1/sso-providers/{identifiant}` remplace tout le fournisseur (sauf
un secret `null`, conservé) : sans `auto_create`, la création automatique est désactivée ;
sans `allowed_domains`, les domaines sont retirés.

**Domaines d'e-mail autorisés** (par fournisseur, par exemple `example.com, example.org`) :
les étapes 2 et 3 n'acceptent qu'une adresse de l'un de ces domaines. La correspondance est
exacte : les sous-domaines ne sont pas compris (`mail.example.com` n'est pas couvert par
`example.com`). Pour Google, quand le profil indique le domaine Workspace du compte (`hd`), ce
domaine doit aussi figurer dans la liste. Liste vide : aucune restriction. Les identités déjà
rattachées (étape 1) et les liaisons faites depuis **Mon compte → Sécurité** ne sont pas
concernées.

> [!WARNING]
> Avec GitHub, Google ou `gitlab.com`, la création automatique sans domaine autorisé permet à
> **n'importe qui** possédant un compte chez ce fournisseur d'obtenir un compte sur votre
> instance. La page d'administration signale un tel fournisseur comme **Ouvert à tous**, et
> l'API REST renvoie `open_to_anyone: true`. Indiquez des domaines autorisés, ou désactivez la
> création automatique et invitez plutôt les personnes.

**Le SSO ne donne aucun droit.** Un nouveau compte ne voit rien tant qu'un admin ne l'a pas
ajouté à une organisation ou à un espace. Les domaines autorisés restreignent qui peut
s'inscrire ; ils n'accordent toujours aucune adhésion.

Une adresse e-mail non vérifiée chez le fournisseur est toujours refusée, pour éviter qu'on
s'approprie un compte en déclarant l'adresse de quelqu'un d'autre.

## Côté utilisateur

- La page de connexion affiche un bouton par fournisseur actif.
- **Mon compte → Sécurité → Connexions SSO** liste les identités rattachées et propose de lier
  un autre fournisseur actif (un passage chez le fournisseur, le retour doit arriver dans la
  même session). On ne peut pas délier la dernière identité d'un compte sans mot de passe.
- Un compte créé par SSO peut définir un premier mot de passe sans en donner un ancien.

## Sécurité

Le flux est celui du code d'autorisation, avec PKCE (S256), un `state` à usage unique valable
10 minutes et un `nonce`. Le profil est lu côté serveur (`userinfo`, ou l'API GitHub) avec le
jeton obtenu grâce au secret du client.
