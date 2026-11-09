English | [Français](email-capture.fr.md)

# Capture by email

An email sent or forwarded to `brain+organization.space@example.com` becomes a note in the
inbox of that space, attachments included, and the nightly tidy-up files it like any other
note. The worker reads a dedicated mailbox over IMAP every few minutes: no webhook and no
incoming mail server to run.

Nothing is accepted on the word of the sender. An email is dropped only if:

1. its `From` is a single address, the **verified** address of an **active** account;
2. the receiving server of the mailbox **authenticated** it: DMARC passed, or a DKIM signature
   of the sender's domain is valid (see [How senders are authenticated](#how-senders-are-authenticated));
3. the address names a space (`+organization.space`) where this account **may drop notes**:
   the same rule as the web form and the `inbox_add` tool (contributors and organization
   admins; restricted spaces and groups included). A super admin only drops by email in the
   spaces they belong to;
4. it passes the checks of every note: at most 10 attachments of 25 MB each, 35 MB for the
   whole email, no likely secret (an API key, a password…), at most 120 notes per hour and per
   account.

The note is written in the name of the person, through the channel **email** (Activity shows
"by email", never "AI"), with the subject as its title and, as its origin, "Email from
*name*, *date*". Its text is the plain text version of the email when there is one,
otherwise its HTML turned into Markdown (scripts, styles and remote images removed; links
kept if they are `http`, `https` or `mailto`). An email forwarded "as an attachment" is kept
as an `.eml` file. Small images inside the HTML (signature logos, under 16 KB) are left out.

## Setting it up

### 1. Create the mailbox

Create a mailbox used **only** for this, for example `brain@example.com`, and check that its
provider delivers the `+` addresses: send an email to `brain+test@example.com`; it must
arrive in the mailbox. The worker moves every email it handles out of the folder it reads
(`INBOX` by default) into `Synaptane/Processed` or `Synaptane/Refused`, created if absent:
nobody should use this mailbox for anything else.

Use an **app password** when the provider offers one rather than the account's password.

| Provider | IMAP server | Trusted server (identifier) | Notes |
|---|---|---|---|
| Google Workspace, Gmail | `imap.gmail.com`, 993, SSL/TLS | `mx.google.com` | Turn on IMAP (Workspace: Admin console → Apps → Gmail → End user access). App password: Google account → Security → 2-Step Verification → App passwords. If your administrator turned app passwords off, Google requires OAuth for IMAP, which this version does not support. |
| Fastmail | `imap.fastmail.com`, 993, SSL/TLS | `*.messagingengine.com` | App password with IMAP access: Settings → Privacy & Security → Manage app passwords. |
| OVHcloud (MX Plan, Email Pro) | `ssl0.ovh.net` (MX Plan) or `pro1.mail.ovh.net` (Email Pro), 993, SSL/TLS | see **Test the connection** | Check that `+` addresses are delivered and that the latest emails carry an `Authentication-Results` header (**Test the connection** tells): without it, no sender can be authenticated with this provider. |
| Microsoft 365 | `outlook.office365.com`, 993 | — | Exchange Online no longer accepts an IMAP password (basic authentication is turned off): it needs OAuth, which this version does not support. Host the capture mailbox at another provider: people on Microsoft 365 can still send to it (see DKIM below). |
| Your own server (Dovecot…) | your server | the name your MTA writes (OpenDKIM, OpenDMARC, rspamd: `authserv_id`) | The MTA must add an `Authentication-Results` header with DMARC or DKIM results, and remove those that arrive with its name. |

### 2. Fill in the settings

**Administration → Email → Capture by email** (super admin):

- **IMAP mailbox**: server, port, security (SSL/TLS on 993, or STARTTLS on 143), username,
  password (encrypted with the master key, never shown again, never logged), folder read
  (`INBOX`).
- **Capture address**: the mailbox's address, without `+` (`brain@example.com`). Each space
  is then reached at `brain+organization.space@example.com`, shown on its **Inbox** page.
- **Trusted servers**: the identifier at the start of the `Authentication-Results` header
  that the mailbox's provider writes (`mx.google.com`). Several are separated by commas;
  `*.example.com` stands for any server of that domain.
- **Internal domains accepted on SPF alone**: leave empty (see below).
- **Read the mailbox every**: 2 minutes by default (1 to 60).
- **Answer the sender**: when the email is refused (default), always (confirmation or
  refusal), or never. See [Answers](#answers).

Then **Test the connection**: it signs in, counts the emails waiting and names the servers
that authenticated the latest ones. Send yourself a test email first: the identifier shown is
the one to enter as trusted. **Read the mailbox now** asks the worker for a poll without
waiting.

An unencrypted connection is refused: the password and every email would travel in clear. For
a local test server only, `allow_insecure_imap = true` (`SYNAPTANE_ALLOW_INSECURE_IMAP`)
allows the **None** choice. Every change of these settings goes to the audit log
("Capture by email set up", "Capture by email removed").

### 3. Make DKIM and DMARC pass for your senders

The capture trusts what the receiving server says about the sender's domain, so the domains
your people send from must sign their emails:

- **Google Workspace**: Admin console → Apps → Gmail → Authenticate email → generate the DKIM
  key, publish the TXT record `google._domainkey`, then **Start authentication**.
- **Microsoft 365**: Microsoft Defender portal → Email authentication settings → DKIM → your
  domain: publish the two CNAME records `selector1._domainkey` and `selector2._domainkey`
  shown, then turn signing on (without it, Microsoft signs with `onmicrosoft.com`, which is
  not aligned with your domain).
- **OVHcloud**: in the Control Panel, turn DKIM on for the domain (DKIM tab of the email
  offer) and let OVHcloud publish the record, or publish it yourself if the DNS is elsewhere.
- **Fastmail** (custom domain): Settings → Domains → your domain: publish the DKIM records
  shown.

Then publish a DMARC record, even a lenient one, so that receivers report `dmarc=pass`:
`_dmarc.example.com TXT "v=DMARC1; p=none; rua=mailto:dmarc@example.com"`. Personal Gmail and
Outlook.com addresses are signed already.

## How senders are authenticated

The `From` header can be forged by anyone. The receiving server of the mailbox checks the
sender's DKIM signature, SPF and DMARC policy, and writes the results in an
`Authentication-Results` header (RFC 8601) **on top** of the email. The capture:

- reads **only the topmost** `Authentication-Results` header, the one added on arrival. A
  sender can write such a header in their own email, with `dmarc=pass`, but it ends up below
  the receiving server's;
- requires that header to carry a **trusted identifier**: a header written by any other
  server is ignored;
- accepts `dmarc=pass` for the `From` domain (the result must name it in `header.from`, as
  every common server does: a malformed `From` such as `alice@acme.test <eve@evil.test>` may be
  read as Eve's by the server and as Alice's elsewhere), or `dkim=pass` whose signing domain is
  aligned with it (the same domain, or a parent or child of it; never a bare top-level domain).
  An explicit `dmarc=fail` refuses the email;
- reads nothing from a header that could be read two ways (a double quote inside a comment,
  a comment left open: a server that copies the sender's address into a comment would
  otherwise let them write a result of their own), nor from one that holds two DMARC results;
- never answers an email that is not authenticated, and never one whose sender has no
  account: no "backscatter" to a forged address.

**Internal domains accepted on SPF alone (less safe).** For a domain that signs nothing
(neither DKIM nor DMARC), you may list it: an `spf=pass` whose envelope domain is aligned with
the `From` is then enough. SPF only proves which server sent the email: anyone allowed to send
through it (another application, another tenant of a shared server) can then write in a
colleague's name. Leave it empty if you can set up DKIM.

**Microsoft 365: accept its header without identifier.** Exchange Online writes its
`Authentication-Results` header without an identifier, which RFC 8601 requires. With this
option, such a header is accepted, and only if it is the topmost one. Only tick it if the
capture mailbox is hosted on Exchange Online (through an IMAP gateway such as DavMail, since
Exchange Online refuses IMAP passwords) and leave it unticked with any other provider: a
server that adds no header of its own would then let the sender's header through.

**Why not a secret address per person?** A secret token in the address (`brain+x7kq…@`) was
considered and not kept: the address travels in clear in every email, in the recipients'
address books and in replies, so it leaks; it would protect only domains that do not sign
their emails, which the SPF option covers more openly; and a personal address could not be
shared on the Inbox page of a space. Authentication by the receiving server, plus the account
and its rights, is checked for every email, without any secret to keep.

## The poll

- Every **interval** (2 minutes by default) the worker queues a poll, in its own lane: a slow
  mail server does not delay the syncs. A single poll runs at a time on the instance, even
  with several workers (PostgreSQL advisory lock).
- A poll handles at most 25 emails, oldest first, for at most 4 minutes; the rest waits for
  the next one. Every network operation is bounded (30 seconds). The parts of an email are
  only read once its sender is authenticated and has an account; an email of more than 50
  nested parts or 2,000 parts is refused as too complex (Python's parser would take minutes).
- Each email handled is **moved**: to `Synaptane/Processed` once dropped, to
  `Synaptane/Refused` otherwise. Without the IMAP `MOVE` extension, it is copied, flagged as
  deleted and expunged.
- An email that cannot be dropped **right now** (space busy, rate limit of the account,
  repository unavailable) stays in the folder and is tried again at the next poll, without
  holding back the others (they do not count among the 25). After 24 hours, it is refused
  ("Space unavailable for 24 hours") and its sender told. One that cannot be read does not
  stop the others: it goes to `Synaptane/Refused`.
- **Idempotent**: each email is remembered by a digest of its `Message-ID` and of the space
  aimed at, for 90 days. The same email received twice (two copies, or moved back into the
  folder after a poll was cut off) is dropped once. To retry a refused email (once the
  person got the right to drop notes, say), move it back from `Synaptane/Refused` to the
  folder read.
- **Routing**: the space is read from the recipient of this very copy (`Delivered-To`,
  `X-Original-To`…), otherwise from `To` and `Cc`: an email to two space addresses goes to
  both (5 at most). Case does not matter (`Brain+Acme.Infra@…`); slugs hold neither dots
  nor capitals, so `organization.space` cannot be read two ways. The bare address
  (`brain@…`) names no space: refused, since an account has no default space.

## Answers

With email sending set up (**Administration → Email**), the capture can answer the sender, in
their language, with an `Auto-Submitted: auto-replied` header (no robot answers it back):

- **When the email is refused** (default): the reason (no space in the address, no right to
  drop notes in this space or no such space, too large, too many attachments or parts, likely
  secret, space unavailable for 24 hours). A refused email is answered once, even if it is
  polled again;
- **Always**: also a confirmation with the link to the note;
- **Never**.

An account gets at most 20 answers per hour: beyond that, the capture stops answering it for
the hour, so that an auto-responder that ignores `Auto-Submitted` cannot start an endless
exchange. Only an **authenticated** sender who **has an account** is ever answered. An email that is
not authenticated, from an unknown address, or automatic (auto-reply, bounce, mailing list)
is refused in silence. The tidy-up notice (filed where, or left why) still goes to the person
as for every note they drop.

## Health and monitoring

- **Administration → Health**: the **Capture by email** line (last successful poll, the
  error of the last one if it failed, accepted and refused over 7 days) and **Emails refused
  by the capture**: the 10 latest, with the reason, the sender's domain and the space aimed
  at. Neither the subject, nor the content, nor the full sender address is kept.
- **`/metrics`**: `synaptane_email_capture_messages_24h{status, reason}`,
  `synaptane_email_capture_poll_failing`,
  `synaptane_email_capture_last_poll_timestamp_seconds`.

## Turning it off

**Turn off the capture** erases the settings and the password; the worker stops reading the
mailbox. Notes already dropped stay; emails sent meanwhile wait in the mailbox.
