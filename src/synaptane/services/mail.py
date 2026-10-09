"""Instance e-mails (SMTP): invitations, forgotten password, alerts, tidy-up notices to
depositors, test.

Settings in the database, editable by the super-admin; the SMTP password is encrypted and
never shown again. Without settings, Synaptane works: invitation links are shown to the
person who invites, and "forgotten password" points to an administrator.
"""

import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import Label, invalid
from synaptane.core.security import SecretBox
from synaptane.services import Actor, audit
from synaptane.storage.models import MailSettings

SECURITY = ("starttls", "ssl", "none")
TIMEOUT = 20
_CONTEXT = "smtp:password"


class MailError(Exception):
    """Sending failed (server unreachable, credentials refused…). `label`: the message to
    translate."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.label = message if isinstance(message, Label) else Label(message)


@dataclass(frozen=True)
class MailView:
    host: str
    port: int
    security: str
    username: str
    sender: str
    has_password: bool


def view(session: Session, actor: Actor) -> MailView | None:
    require(actor.access, Action.MANAGE_INSTANCE)
    row = session.get(MailSettings, 1)
    if row is None:
        return None
    return MailView(row.host, row.port, row.security, row.username, row.sender, bool(row.password))


def configured(session: Session) -> bool:
    return session.get(MailSettings, 1) is not None


def configure(
    session: Session,
    actor: Actor,
    box: SecretBox,
    *,
    host: str,
    port: int,
    security: str,
    sender: str,
    username: str = "",
    password: str | None = None,
) -> None:
    """`password=None` keeps the stored password; "" removes it."""
    require(actor.access, Action.MANAGE_INSTANCE)
    host, sender = host.strip(), sender.strip()
    if not host or not sender or "@" not in sender:
        raise invalid("Enter the SMTP server and a valid sender address.")
    if security not in SECURITY or not 0 < port < 65536:
        raise invalid("Invalid SMTP port or security.")
    row = session.get(MailSettings, 1)
    if row is None:
        row = MailSettings(id=1, host=host, sender=sender)
        session.add(row)
    row.host, row.port, row.security, row.sender = host, port, security, sender
    row.username = username.strip()
    if password is not None:
        row.password = box.encrypt(password, _CONTEXT) if password else None
    audit(session, actor, "instance.mail_configured", host=host)


def remove(session: Session, actor: Actor) -> None:
    require(actor.access, Action.MANAGE_INSTANCE)
    row = session.get(MailSettings, 1)
    if row is not None:
        session.delete(row)
        audit(session, actor, "instance.mail_removed")


def send(session: Session, box: SecretBox, to: str, subject: str, body: str) -> None:
    """Sends a plain-text e-mail. Raises MailError if sending fails."""
    row = session.get(MailSettings, 1)
    if row is None:
        raise MailError("Email sending is not configured on this instance.")
    message = EmailMessage()
    message["From"] = formataddr(("Synaptane", row.sender))
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=row.sender.rsplit("@", 1)[-1])
    message.set_content(body)
    password = box.decrypt(row.password, _CONTEXT) if row.password else ""
    try:
        if row.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                row.host, row.port, timeout=TIMEOUT, context=ssl.create_default_context()
            )
        else:
            client = smtplib.SMTP(row.host, row.port, timeout=TIMEOUT)
        with client:
            if row.security == "starttls":
                client.starttls(context=ssl.create_default_context())
            if row.username:
                client.login(row.username, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise MailError(
            Label("Cannot send: {detail}", detail=f"{type(error).__name__} {error}"[:300])
        ) from None


# --- Messages ---------------------------------------------------------------------------------

_TEXTS = {
    "invitation": {
        "fr": (
            "{inviter} vous invite sur Synaptane",
            "Bonjour,\n\n{inviter} vous invite à rejoindre l'organisation « {org} » sur "
            "Synaptane, le second cerveau de l'équipe.\n\nPour créer votre compte, ouvrez ce "
            "lien (valable {days} jours) :\n{link}\n\nSi vous ne vous attendiez pas à ce "
            "message, ignorez-le.\n",
        ),
        "en": (
            "{inviter} invites you to Synaptane",
            "Hello,\n\n{inviter} invites you to join the “{org}” organization on Synaptane, "
            "the team's second brain.\n\nTo create your account, open this link (valid for "
            "{days} days):\n{link}\n\nIf you were not expecting this message, ignore it.\n",
        ),
    },
    "added": {
        "fr": (
            "Vous avez accès à « {org} » sur Synaptane",
            "Bonjour,\n\n{inviter} vous a donné accès à l'organisation « {org} » sur "
            "Synaptane.\n\n{link}\n",
        ),
        "en": (
            "You now have access to “{org}” on Synaptane",
            "Hello,\n\n{inviter} gave you access to the “{org}” organization on "
            "Synaptane.\n\n{link}\n",
        ),
    },
    "reset": {
        "fr": (
            "Réinitialiser votre mot de passe Synaptane",
            "Bonjour,\n\nPour choisir un nouveau mot de passe, ouvrez ce lien (valable 30 "
            "minutes) :\n{link}\n\nSi vous n'avez rien demandé, ignorez ce message : votre "
            "mot de passe reste inchangé.\n",
        ),
        "en": (
            "Reset your Synaptane password",
            "Hello,\n\nTo choose a new password, open this link (valid for 30 minutes):\n"
            "{link}\n\nIf you did not ask for this, ignore this message: your password stays "
            "unchanged.\n",
        ),
    },
    "budget": {
        "fr": (
            "Budget d'IA de « {org} » : {level} % atteints",
            "Bonjour,\n\nLe rangement de l'organisation « {org} » a consommé {spent} € sur un "
            "budget mensuel de {budget} € ({level} %).\n\nÀ 100 %, le rangement est suspendu "
            "jusqu'au mois prochain. Pour relever le budget :\n{link}\n",
        ),
        "en": (
            "AI budget of “{org}”: {level}% reached",
            "Hello,\n\nTidying in the “{org}” organization has used {spent} € of a monthly "
            "budget of {budget} € ({level}%).\n\nAt 100%, tidying is paused until next month. "
            "To raise the budget:\n{link}\n",
        ),
    },
    "alert_deletion_cap": {
        "fr": (
            "Rangement de « {space} » : plafond de suppressions atteint",
            "Bonjour,\n\nL'agent de rangement de l'espace « {space} » ({org}) a voulu "
            "supprimer plus de {limit} pages en un passage. Les suppressions au-delà ont été "
            "refusées et les éléments concernés laissés dans l'inbox avec la raison.\n\n"
            "Vérifiez le journal du rangement :\n{link}/tidy\n",
        ),
        "en": (
            "Tidying of “{space}”: deletion cap reached",
            "Hello,\n\nThe tidying agent of the “{space}” space ({org}) tried to delete more "
            "than {limit} pages in one run. Further deletions were refused and the related "
            "items left in the inbox with the reason.\n\nCheck the tidying log:\n{link}/tidy\n",
        ),
    },
    "alert_big_changes": {
        "fr": (
            "Rangement de « {space} » : changements importants",
            "Bonjour,\n\nLe dernier rangement de l'espace « {space} » ({org}) a fait des "
            "changements importants :\n{details}\n\nRien n'a été bloqué. Si c'est une erreur, "
            "le journal du rangement permet d'annuler le passage (Git garde aussi l'historique "
            "de chaque page) :\n{link}/tidy\n",
        ),
        "en": (
            "Tidying of “{space}”: large changes",
            "Hello,\n\nThe last tidying run of the “{space}” space ({org}) made large "
            "changes:\n{details}\n\nNothing was blocked. If this is a mistake, the tidying "
            "log lets you undo the run (Git also keeps the history of every page):\n"
            "{link}/tidy\n",
        ),
    },
    "alert_quota_high": {
        "fr": (
            "Quota de l'abonnement presque épuisé (« {space} »)",
            "Bonjour,\n\nLe dernier rangement de l'espace « {space} » ({org}) a laissé le quota "
            "de l'abonnement d'IA presque épuisé :\n{usage}\n\nAu-delà de la limite, les "
            "rangements échouent jusqu'à la remise à zéro de la fenêtre. Journal du rangement :"
            "\n{link}/tidy\n",
        ),
        "en": (
            "AI subscription quota almost used up (“{space}”)",
            "Hello,\n\nThe last tidying of the “{space}” space ({org}) left the AI "
            "subscription quota almost used up:\n{usage}\n\nPast the limit, tidying fails "
            "until the window resets. Tidying log:\n{link}/tidy\n",
        ),
    },
    "alert_tidy_failed": {
        "fr": (
            "Rangement de « {space} » en échec",
            "Bonjour,\n\nLe rangement de l'espace « {space} » ({org}) a échoué :\n"
            "{error}\n\nJournal du rangement :\n{link}/tidy\n",
        ),
        "en": (
            "Tidying of “{space}” failed",
            "Hello,\n\nTidying of the “{space}” space ({org}) failed:\n{error}\n\n"
            "Tidying log:\n{link}/tidy\n",
        ),
    },
    "alert_tidy_interrupted": {
        "fr": (
            "Rangement de « {space} » interrompu",
            "Bonjour,\n\nLe rangement de l'espace « {space} » ({org}) s'est arrêté avant "
            "d'avoir traité tous les éléments :\n{reason}\n\nÉléments non traités, gardés "
            "dans l'inbox et repris au prochain passage : {count}.\nÉléments laissés après "
            "{attempts} passages interrompus (à reprendre à la main) : {given_up}.\n\n"
            "Le travail déjà fait est gardé. Journal du rangement :\n{link}/tidy\n",
        ),
        "en": (
            "Tidying of “{space}” interrupted",
            "Hello,\n\nTidying of the “{space}” space ({org}) stopped before handling every "
            "item:\n{reason}\n\nItems not handled, kept in the inbox and retried on the next "
            "run: {count}.\nItems left after {attempts} interrupted runs (to retry by hand): "
            "{given_up}.\n\nThe work already done is kept. Tidying log:\n{link}/tidy\n",
        ),
    },
    "alert_job_abandoned": {
        "fr": (
            "Tâche « {task} » de « {space} » abandonnée",
            "Bonjour,\n\nLa tâche de fond « {task} » de l'espace « {space} » ({org}) a échoué "
            "{attempts} fois de suite sur une erreur passagère et a été abandonnée :\n"
            "{error}\n\nElle repartira à la prochaine demande. Si l'erreur persiste, "
            "vérifiez l'espace :\n{link}\n",
        ),
        "en": (
            "Task “{task}” of “{space}” abandoned",
            "Hello,\n\nThe “{task}” background task of the “{space}” space ({org}) failed "
            "{attempts} times in a row on a transient error and was abandoned:\n{error}\n\nIt will "
            "start again on the next request. If the error persists, check the space:\n"
            "{link}\n",
        ),
    },
    "alert_routine_failed": {
        "fr": (
            "Routine « {routine} » de « {space} » en échec",
            "Bonjour,\n\nLa routine « {routine} » de l'espace « {space} » ({org}) a échoué :\n"
            "{error}\n\nRien n'a été publié par ce passage. Journal de la routine :\n"
            "{link}/-/routines\n",
        ),
        "en": (
            "Routine “{routine}” of “{space}” failed",
            "Hello,\n\nThe “{routine}” routine of the “{space}” space ({org}) failed:\n"
            "{error}\n\nNothing was published by this run. Routine log:\n{link}/-/routines\n",
        ),
    },
    "routine_report": {
        "fr": (
            "{routine} · {space}",
            "Bonjour,\n\nVoici la sortie de la routine « {routine} » de l'espace « {space} » "
            "({org}) :\n\n{text}\n\n—\nDans Synaptane : {link}\nPour ne plus recevoir ces "
            "e-mails : {account}\n",
        ),
        "en": (
            "{routine} · {space}",
            "Hello,\n\nHere is the output of the “{routine}” routine of the “{space}” space "
            "({org}):\n\n{text}\n\n—\nIn Synaptane: {link}\nTo stop receiving these e-mails: "
            "{account}\n",
        ),
    },
    "review_reminder": {
        "fr": (
            "Pages à revoir : {count}",
            "Bonjour,\n\nVous êtes propriétaire de pages dont la vérification est échue. "
            "Relisez-les, corrigez ce qui a changé, puis choisissez « Marquer comme vérifiée » "
            "dans le menu ⋮ de la page (ou confiez-les à quelqu'un d'autre) :\n\n{pages}\n\n"
            "Pour ne plus recevoir ces e-mails : {account}\n",
        ),
        "en": (
            "Pages to review: {count}",
            "Hello,\n\nYou own pages whose verification is overdue. Read them again, fix what "
            "changed, then choose “Mark as verified” in the page's ⋮ menu (or hand them over to "
            "someone else):\n\n{pages}\n\nTo stop receiving these e-mails: {account}\n",
        ),
    },
    "alert_sync_error": {
        "fr": (
            "Synchronisation de « {space} » en erreur",
            "Bonjour,\n\nL'espace « {space} » ({org}) ne se synchronise plus avec son dépôt "
            "Git :\n{error}\n\nÉtat du dépôt :\n{link}/-/git\n",
        ),
        "en": (
            "Sync of “{space}” failing",
            "Hello,\n\nThe “{space}” space ({org}) no longer syncs with its Git "
            "repository:\n{error}\n\nRepository status:\n{link}/-/git\n",
        ),
    },
    "alert_inbox_backlog": {
        "fr": (
            "Inbox de « {space} » en retard",
            "Bonjour,\n\nL'inbox de l'espace « {space} » ({org}) contient {count} éléments, "
            "le plus ancien depuis {days} jours. Vérifiez que le rangement est configuré et "
            "fonctionne :\n{link}/inbox\n",
        ),
        "en": (
            "Inbox of “{space}” falling behind",
            "Hello,\n\nThe inbox of the “{space}” space ({org}) holds {count} items, the "
            "oldest for {days} days. Check that tidying is configured and running:\n"
            "{link}/inbox\n",
        ),
    },
    "alert_webhook_disabled": {
        "fr": (
            "Webhook désactivé ({org})",
            "Bonjour,\n\nLe webhook {url} de l'organisation « {org} » a échoué {failures} fois "
            "de suite (dernier statut : {status}). Il a été désactivé pour ne plus ralentir "
            "Synaptane ; les événements suivants ne lui sont pas envoyés.\n\nUne fois le "
            "destinataire réparé, réactivez-le depuis la page des webhooks :\n"
            "{link}/-/webhooks\n",
        ),
        "en": (
            "Webhook disabled ({org})",
            "Hello,\n\nThe webhook {url} of the “{org}” organization failed {failures} times "
            "in a row (last status: {status}). It was disabled so it no longer slows Synaptane "
            "down; later events are not sent to it.\n\nOnce the receiver is fixed, re-enable "
            "it from the webhooks page:\n{link}/-/webhooks\n",
        ),
    },
    "tidy_notice": {
        "fr": (
            "Rangement de « {space} » : vos notes",
            "Bonjour,\n\nLe rangement de l'espace « {space} » ({org}) a traité {count} note(s) "
            "que vous avez déposée(s) :\n\n{items}\n\n{left}Pour ne plus recevoir ces messages "
            ": {account}\n",
        ),
        "en": (
            "Tidying of “{space}”: your notes",
            "Hello,\n\nTidying in the “{space}” space ({org}) handled {count} note(s) you "
            "deposited:\n\n{items}\n\n{left}To stop receiving these messages: {account}\n",
        ),
    },
    "tidy_notice_left": {  # paragraph added to `tidy_notice` when a note is left behind
        "fr": (
            "",
            "Une note laissée attend dans l'inbox : complétez-la (elle sera reprise au prochain "
            "passage) ou rangez-la à la main :\n{link}/inbox\n\n",
        ),
        "en": (
            "",
            "A note left behind waits in the inbox: complete it (it will be picked up by the "
            "next run) or file it by hand:\n{link}/inbox\n\n",
        ),
    },
    "verify_email": {
        "fr": (
            "Vérifiez votre adresse e-mail Synaptane",
            "Bonjour,\n\nPour confirmer que cette adresse est celle du compte Synaptane "
            "« {account} », ouvrez ce lien (valable 3 jours) et connectez-vous à ce compte :\n"
            "{link}\n\nSi ce compte n'est pas le vôtre, ignorez ce message : l'adresse ne lui "
            "sera pas attribuée.\n",
        ),
        "en": (
            "Verify your Synaptane e-mail address",
            "Hello,\n\nTo confirm this address belongs to the Synaptane account “{account}”, "
            "open this link (valid for 3 days) and sign in to that account:\n{link}\n\n"
            "If this account is not yours, ignore this message: the address will not be given "
            "to it.\n",
        ),
    },
    "email_changed": {
        "fr": (
            "L'adresse e-mail de votre compte Synaptane a changé",
            "Bonjour,\n\nL'adresse e-mail de votre compte Synaptane est désormais : {new}.\n\n"
            "Si vous n'êtes pas à l'origine de ce changement, prévenez un administrateur.\n",
        ),
        "en": (
            "The e-mail address of your Synaptane account changed",
            "Hello,\n\nThe e-mail address of your Synaptane account is now: {new}.\n\n"
            "If you did not make this change, tell an administrator.\n",
        ),
    },
    "test": {
        "fr": ("Test d'envoi Synaptane", "L'envoi d'e-mails de Synaptane fonctionne.\n"),
        "en": ("Synaptane test e-mail", "Synaptane can send e-mails.\n"),
    },
}


def compose(kind: str, locale: str, **values: object) -> tuple[str, str]:
    """(subject, body) in the requested language: the recipient account's, always supported
    (`accounts.create_user` validates it); English for an unknown value. A value can follow
    the language too: an interface text (`Label`), or a function of the language."""
    from synaptane.web.i18n import translator

    texts = _TEXTS[kind]
    locale = locale if locale in texts else "en"
    subject, body = texts[locale]
    values = {k: localized(v, locale, translator(locale)) for k, v in values.items()}
    return subject.format(**values), body.format(**values)


def localized(value: object, locale: str, translate: Callable[..., str] | None = None) -> object:
    """A value inserted into a text in `locale`: a `Label` is translated, a function of the
    language is called; anything else is kept as it is."""
    if isinstance(value, Label):
        if translate is None:
            from synaptane.web.i18n import translator

            translate = translator(locale)
        return translate(value)
    if callable(value):
        return value(locale)
    return value
