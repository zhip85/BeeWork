"""Outbound mail (T-008): a unified, retryable, auditable send capability.

``send_mail`` only enqueues a ``mail.send`` job (T-002 worker queue) plus a ``mail_deliveries``
row; the worker renders the Jinja2 template and hands it to a ``MailTransport``. Failures back
off and retry through the queue; once the job turns ``dead`` it shows up on the platform jobs
page and the delivery stays ``failed``. Template context (invitation links, tokens) only lives
in the job payload — never in logs, audit events or the delivery record.
"""
import hashlib
import logging
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Protocol
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.config import settings
from beework.models.mail import MailDelivery
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "mail_templates"
TEMPLATES = frozenset({
    "member_invitation",        # SET-010: a member is invited to a tenant
    "tenant_admin_invitation",  # PLT-022: the platform provisions a tenant and invites its first admin
    "password_reset",           # reserved for T-093; no caller yet
    "generic_notification",     # SET-101: a generic one-off notification mail
})
LOCALES = ("zh-CN", "en")
RATE_LIMIT_WINDOW = timedelta(minutes=1)  # anti-abuse: at most RATE_LIMIT_MAX mails per recipient per template
RATE_LIMIT_MAX = 3


class MailRateLimited(Exception):
    """The same recipient got too many mails of the same template inside the rate-limit window."""


class MailTransport(Protocol):
    """How a rendered mail physically leaves the system (``smtp`` in production, ``console`` in tests)."""

    def send(
        self, *, from_addr: str, to_addr: str, subject: str, body_text: str, body_html: str
    ) -> None: ...


class ConsoleTransport:
    """Development / test stand-in: logs a line without body, links or tokens."""

    def send(self, *, from_addr: str, to_addr: str, subject: str, body_text: str, body_html: str) -> None:
        logger.info("MAIL (console transport) to=%s subject=%s", mask_email(to_addr), subject)


class SmtpTransport:
    """Plain SMTP (auth and STARTTLS optional) — Mailpit in development, a provider in production."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        use_tls: bool = False,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.timeout_seconds = timeout_seconds

    def send(self, *, from_addr: str, to_addr: str, subject: str, body_text: str, body_html: str) -> None:
        message = EmailMessage()
        _name, address = parseaddr(from_addr)
        message["From"] = formataddr((parseaddr(from_addr)[0] or "BeeWork", address or from_addr))
        message["To"] = to_addr
        message["Subject"] = subject
        message.set_content(body_text)
        message.add_alternative(body_html, subtype="html")
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)


_transport: MailTransport | None = None


def get_transport() -> MailTransport:
    global _transport
    if _transport is None:
        if settings.mail_transport == "smtp":
            _transport = SmtpTransport(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user,
                password=settings.smtp_password,
                use_tls=settings.smtp_use_tls,
            )
        elif settings.mail_transport == "console":
            _transport = ConsoleTransport()
        else:
            raise ValueError(f"Unknown MAIL_TRANSPORT: {settings.mail_transport}")
    return _transport


def set_transport(transport: MailTransport | None) -> None:
    """Override the transport (tests inject a fake here; ``None`` restores the configured one)."""
    global _transport
    _transport = transport


_jinja_env: Environment | None = None


def _environment() -> Environment:
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR),
            autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
        )
    return _jinja_env


def render_mail(template: str, locale: str, context: dict) -> tuple[str, str, str]:
    """Render ``(subject, text, html)`` for a template in a locale; unknown names are a programming error."""
    if template not in TEMPLATES:
        raise ValueError(f"Unknown mail template: {template}")
    if locale not in LOCALES:
        raise ValueError(f"Unknown mail locale: {locale}")
    env = _environment()
    base = f"{locale}/{template}"
    subject = env.get_template(f"{base}/subject.txt").render(**context).strip()
    body_text = env.get_template(f"{base}/body.txt").render(**context)
    body_html = env.get_template(f"{base}/body.html").render(**context)
    return subject, body_text, body_html


def mask_email(address: str) -> str:
    """``alex.chen@example.com`` → ``a***@example.com``: enough to recognise, not enough to replay."""
    local, _, domain = address.strip().partition("@")
    return f"{local[:1]}***@{domain}" if local else f"***@{domain}"


def hash_email(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


def _recent_delivery_count(db: Session, recipient_hash: str, template: str) -> int:
    window_start = datetime.now(UTC) - RATE_LIMIT_WINDOW
    return int(
        db.scalar(
            select(func.count())
            .select_from(MailDelivery)
            .where(
                MailDelivery.recipient_hash == recipient_hash,
                MailDelivery.template == template,
                MailDelivery.created_at > window_start,
            )
        )
        or 0
    )


def send_mail(
    db: Session,
    *,
    tenant_id: UUID | None,
    to: str,
    template: str,
    context: dict,
    locale: str = "zh-CN",
    max_attempts: int = 5,
) -> MailDelivery:
    """Enqueue one mail inside the caller's transaction (no commit). Rate-limits per recipient+template.

    Raises :class:`MailRateLimited` when the recipient already reached the per-minute cap; the API
    layer turns it into a 429 so the caller can tell the user instead of silently dropping the mail.
    """
    recipient = to.strip().lower()
    if template not in TEMPLATES:
        raise ValueError(f"Unknown mail template: {template}")
    if locale not in LOCALES:
        raise ValueError(f"Unknown mail locale: {locale}")
    recipient_hash = hash_email(recipient)
    if _recent_delivery_count(db, recipient_hash, template) >= RATE_LIMIT_MAX:
        raise MailRateLimited(
            f"recipient {mask_email(recipient)} reached the {RATE_LIMIT_MAX}/minute limit for {template}"
        )
    delivery = MailDelivery(
        tenant_id=tenant_id,
        template=template,
        locale=locale,
        recipient_masked=mask_email(recipient),
        recipient_hash=recipient_hash,
        status="queued",
    )
    db.add(delivery)
    db.flush()
    # The context (invitation links, tokens) only travels in the job payload — never into logs, the
    # delivery record or audit events; bodies are rendered at delivery time and never persisted.
    job = enqueue(
        db, "mail.send",
        {"delivery_id": str(delivery.id), "to": recipient, "context": context},
        tenant_id=tenant_id,
        max_attempts=max_attempts,
    )
    db.flush()  # assigns the job its ID before the delivery row references it
    delivery.job_id = job.id
    db.flush()
    return delivery


@job_handler("mail.send")
def deliver_mail(db: Session, payload: dict) -> None:
    """Send one enqueued mail. A failure is persisted (``failed`` + error) before re-raising so the
    queue's rollback cannot erase it; the queue then retries with backoff until ``max_attempts``."""
    delivery = db.get(MailDelivery, UUID(str(payload["delivery_id"])))
    if delivery is None:
        raise ValueError("Mail delivery record is missing; cannot deliver")
    recipient = str(payload["to"])
    delivery.attempts += 1
    delivery.status = "queued"
    try:
        subject, body_text, body_html = render_mail(
            delivery.template, delivery.locale, payload.get("context") or {}
        )
        get_transport().send(
            from_addr=settings.mail_from,
            to_addr=recipient,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
    except Exception as exc:
        delivery.status = "failed"
        delivery.error = str(exc)[:2000]
        db.commit()  # survive the rollback in worker.queue.execute_job
        raise
    delivery.status = "sent"
    delivery.sent_at = datetime.now(UTC)
    delivery.error = None
