English | [Français](sso.fr.md)

# Single sign-on (SSO)

Local accounts (password) and sign-in through an external provider coexist. The super admin
configures the providers in **Administration → Single sign-on**, or through the API
(`PUT /api/v1/sso-providers/{identifier}`).

## Providers

| Type | Issuer | Notes |
|---|---|---|
| GitLab | `https://gitlab.com` by default; the instance URL for a self-hosted GitLab | OAuth application with the `openid`, `email`, `profile` scopes |
| Google | `https://accounts.google.com` (default) | OAuth credentials of type "Web application" |
| GitHub | none | OAuth App (not a GitHub App); the verified primary email is read through the API |
| Generic OIDC | Issuer URL (Keycloak, Authentik, Entra ID…) | Discovery through `/.well-known/openid-configuration` |

At the provider, register the **callback URL** shown by Synaptane:
`<public_url>/sso/<identifier>/callback`. `public_url` must therefore be the real public
address of the instance.

The client secret is encrypted with the master key and never shown again. Leaving it empty
when editing keeps the saved one.

## Account matching

On sign-in, Synaptane looks, in this order, for:
1. an identity already linked (same provider, same identifier at the provider);
2. if the option **Link to an existing account with the same verified email** is ticked, an
   existing account with the same email address, **verified** by the provider **and**
   verified in Synaptane (link received at that address). An address that is only declared
   on an account never captures someone's identity: it is removed from that account. The
   option is recommended for GitHub and Google only (and turned on by default for them
   through the API): a self-hosted GitLab or OIDC server can "verify" any address. With the
   option off, the sign-in is refused and the person is asked to sign in with their password,
   then link the provider from **My account → Security**;
3. otherwise, if the option **Create the account automatically on first sign-in** is ticked,
   a new account without a password. Otherwise, the sign-in is refused.

**Create the account automatically** is off by default. Providers saved before this default
changed keep their setting. **Edit** on a provider fills the form with all its settings
(type, label, issuer, client ID, domains, options), except the client secret, which stays
empty: saving again without touching anything keeps everything. Through the API,
`PUT /api/v1/sso-providers/{identifier}` replaces the whole provider (except a `null`
secret, which is kept): an absent `auto_create` turns automatic creation off, absent
`allowed_domains` removes the domains.

**Allowed email domains** (per provider, for example `example.com, example.org`): steps 2
and 3 only accept an address in one of these domains. The match is exact: subdomains are not
included (`mail.example.com` is not covered by `example.com`). For Google, when the profile
gives the Workspace domain of the account (`hd`), that domain must be in the list too. Empty
list: no restriction. Identities already linked (step 1) and links made from
**My account → Security** are not affected.

> [!WARNING]
> With GitHub, Google or `gitlab.com`, automatic creation without allowed domains lets
> **anyone** with an account at that provider get an account on your instance. The
> administration page flags such a provider as **Open to everyone**, and the REST API returns
> `open_to_anyone: true`. Enter allowed domains, or turn automatic creation off and invite
> people instead.

**SSO grants no rights.** A new account sees nothing until an admin adds it to an
organization or a space. Allowed domains restrict who can sign up; they still grant no
membership.

An email address that is not verified at the provider is always refused, so that nobody can
take over an account by declaring someone else's address.

## For users

- The sign-in page shows one button per active provider.
- **My account → Security → Single sign-on identities** lists the linked identities and
  offers to link another active provider (one round trip to the provider; the return must
  arrive in the same session). The last identity of an account without a password cannot be
  unlinked.
- An account created through SSO can set a first password without giving an old one.

## Security

The flow is the authorization code flow, with PKCE (S256), a single-use `state` valid for
10 minutes, and a `nonce`. The profile is read server-side (`userinfo`, or the GitHub API)
with the token obtained using the client secret.
