"""T-008 mail delivery: rendering, transport, queue handler, rate limit and API wiring."""
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework import mail as mail_service
from beework.config import settings
from beework.mail import (
    MailRateLimited,
    SmtpTransport,
    hash_email,
    mask_email,
    render_mail,
    send_mail,
)
from beework.models import Base, MemberInvitation, Membership, Organization, Tenant, User
from beework.models.jobs import Job
from beework.models.mail import MailDelivery
from beework.security import hash_password
from beework.worker.queue import claim_jobs, execute_job
from isolation import disable_external_integrations
from test_assistants import PASSWORD, AssistantTests


class RecordingTransport:
    """Stands in for SMTP: records what the worker handed over."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    def send(self, *, from_addr, to_addr, subject, body_text, body_html) -> None:
        if self.fail:
            raise ConnectionError("smtp unavailable")
        self.sent.append({
            "from_addr": from_addr, "to_addr": to_addr,
            "subject": subject, "body_text": body_text, "body_html": body_html,
        })


class RenderTests(unittest.TestCase):
    def test_every_template_renders_in_both_locales(self) -> None:
        context = {
            "tenant_name": "Acme", "inviter_name": "Olivia", "invitation_url": "https://x.test/invite?token=t",
            "expires_at": "2026-11-20 12:00 UTC", "admin_name": "Alex", "reset_url": "https://x.test/reset",
            "user_name": "Alex", "title": "标题", "message": "内容", "link": "https://x.test/more",
        }
        for template in sorted(mail_service.TEMPLATES):
            for locale in mail_service.LOCALES:
                subject, body_text, body_html = render_mail(template, locale, context)
                self.assertTrue(subject, template)
                self.assertTrue(body_text.strip(), template)
                self.assertTrue(body_html.strip(), template)

    def test_html_output_escapes_context_values(self) -> None:
        subject, _text, html = render_mail(
            "member_invitation", "zh-CN",
            {
                "tenant_name": "<script>alert(1)</script>", "inviter_name": "Olivia",
                "invitation_url": "https://x.test/i", "expires_at": "soon",
            },
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("https://x.test/i", html)
        self.assertIn("邀请", subject)

    def test_unknown_template_or_locale_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render_mail("nope", "zh-CN", {})
        with self.assertRaises(ValueError):
            render_mail("member_invitation", "fr-FR", {})

    def test_mask_and_hash(self) -> None:
        self.assertEqual(mask_email("alex.chen@example.com"), "a***@example.com")
        self.assertEqual(mask_email("a@example.com"), "a***@example.com")
        self.assertEqual(mask_email("@example.com"), "***@example.com")
        self.assertEqual(hash_email(" A@Example.com "), hash_email("a@example.com"))


class MailServiceTests(unittest.TestCase):
    """send_mail → queue → handler against an in-memory database and a fake transport."""

    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        cls.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(cls.engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        cls.session_factory = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.transport = RecordingTransport()
        mail_service.set_transport(self.transport)
        self.addCleanup(mail_service.set_transport, None)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme")
            db.add(tenant)
            db.flush()
            self.tenant_id = tenant.id

    def send(self, to: str = "new@example.com", template: str = "member_invitation", **kwargs):
        with self.session_factory.begin() as db:
            return send_mail(
                db, tenant_id=self.tenant_id, to=to, template=template,
                context={"invitation_url": "https://x.test/i", "tenant_name": "Acme", "inviter_name": "Olivia",
                         "expires_at": "2026-11-20 12:00 UTC"},
                **kwargs,
            )

    def run_jobs(self) -> None:
        """Claim queued jobs (execute_job only touches running ones) and run them; execute_job
        manages its own transactions, so it must not run inside a ``begin()`` block."""
        with self.session_factory.begin() as db:
            job_ids = [job.id for job in claim_jobs(db, "test-worker")]
        for job_id in job_ids:
            with self.session_factory() as db:
                execute_job(db, job_id)

    def test_send_mail_enqueues_and_the_worker_delivers(self) -> None:
        delivery = self.send()
        self.assertEqual(delivery.status, "queued")
        self.assertEqual(delivery.recipient_masked, "n***@example.com")
        self.assertNotEqual(delivery.job_id, None)
        self.run_jobs()
        with self.session_factory() as db:
            fresh = db.get(MailDelivery, delivery.id)
            self.assertEqual(fresh.status, "sent")
            self.assertIsNotNone(fresh.sent_at)
            self.assertEqual(fresh.attempts, 1)
            self.assertIsNone(fresh.error)
            job = db.get(Job, fresh.job_id)
            self.assertEqual(job.status, "succeeded")
        self.assertEqual(len(self.transport.sent), 1)
        sent = self.transport.sent[0]
        self.assertEqual(sent["to_addr"], "new@example.com")
        self.assertIn("Acme", sent["subject"])
        self.assertIn("https://x.test/i", sent["body_html"])

    def test_job_payload_carries_context_but_the_delivery_row_does_not(self) -> None:
        delivery = self.send()
        with self.session_factory() as db:
            job = db.get(Job, delivery.job_id)
            self.assertEqual(job.payload["to"], "new@example.com")
            self.assertIn("invitation_url", job.payload["context"])
            self.assertEqual(db.get(MailDelivery, delivery.id).error, None)

    def test_same_recipient_and_template_is_rate_limited(self) -> None:
        self.send()
        self.send()
        self.send()
        with self.assertRaises(MailRateLimited):
            self.send()
        # A different template for the same recipient is fine; so is another recipient.
        self.send(template="generic_notification")
        self.send(to="other@example.com")

    def test_failure_persists_the_error_and_the_queue_retries(self) -> None:
        self.transport.fail = True
        delivery = self.send()
        self.run_jobs()
        with self.session_factory() as db:
            job = db.get(Job, delivery.job_id)
            self.assertEqual(job.status, "queued")  # backed off for a retry, not dead yet
            fresh = db.get(MailDelivery, delivery.id)
            self.assertEqual(fresh.status, "failed")
            self.assertIn("smtp unavailable", fresh.error)
            self.assertEqual(fresh.attempts, 1)
        self.transport.fail = False
        with self.session_factory.begin() as db:
            db.get(Job, delivery.job_id).run_after = datetime.now(UTC)  # skip the backoff wait
        self.run_jobs()
        with self.session_factory() as db:
            fresh = db.get(MailDelivery, delivery.id)
            self.assertEqual(fresh.status, "sent")
            self.assertIsNone(fresh.error)

    def test_exhausted_retries_turn_the_job_dead_and_leave_the_delivery_failed(self) -> None:
        self.transport.fail = True
        with self.session_factory.begin() as db:
            delivery = send_mail(
                db, tenant_id=self.tenant_id, to="dead@example.com", template="member_invitation",
                context={"invitation_url": "u", "tenant_name": "t", "inviter_name": "i", "expires_at": "e"},
                max_attempts=1,
            )
            job_id = delivery.job_id
        self.run_jobs()
        with self.session_factory() as db:
            self.assertEqual(db.get(Job, job_id).status, "dead")
            self.assertEqual(db.get(MailDelivery, delivery.id).status, "failed")

    def test_console_transport_is_the_default_and_never_leaks_the_body(self) -> None:
        mail_service.set_transport(None)
        original_transport = settings.mail_transport
        try:
            settings.mail_transport = "console"
            self.assertIsInstance(mail_service.get_transport(), mail_service.ConsoleTransport)
            settings.mail_transport = "smtp"
            mail_service.set_transport(None)  # drop the cached transport: get_transport reads the setting once
            transport = mail_service.get_transport()
            self.assertEqual(transport.host, settings.smtp_host)
            self.assertIsInstance(transport, SmtpTransport)
            settings.mail_transport = "pigeon"
            mail_service.set_transport(None)
            with self.assertRaises(ValueError):
                mail_service.get_transport()
        finally:
            settings.mail_transport = original_transport
            mail_service.set_transport(self.transport)
        # The console transport logs a masked recipient and the subject only — never the body or links.
        with self.assertLogs(mail_service.logger, level="INFO") as captured:
            mail_service.ConsoleTransport().send(
                from_addr="BeeWork <no-reply@beework.local>", to_addr="alex@example.com",
                subject="Welcome", body_text="secret-token-and-link", body_html="secret-token-and-link",
            )
        joined = " ".join(captured.output)
        self.assertNotIn("secret-token-and-link", joined)
        self.assertIn("a***@example.com", joined)


class InvitationMailTests(AssistantTests):
    """Member invitations and resends really queue a mail (SET-010)."""

    def setUp(self) -> None:
        super().setUp()
        self.transport = RecordingTransport()
        mail_service.set_transport(self.transport)
        self.addCleanup(mail_service.set_transport, None)

    def deliveries(self) -> list[MailDelivery]:
        with self.session_factory() as db:
            return list(db.scalars(select(MailDelivery).order_by(MailDelivery.created_at)))

    def run_jobs(self) -> None:
        with self.session_factory.begin() as db:
            job_ids = [job.id for job in claim_jobs(db, "test-worker")]
        for job_id in job_ids:
            with self.session_factory() as db:
                execute_job(db, job_id)

    def test_inviting_a_member_queues_an_invitation_mail(self) -> None:
        headers = self.login("admin@example.com")
        response = self.client.post(
            "/api/v1/members/invitations",
            json={"emails": ["newbie@example.com"], "role": "builder", "organization_id": self.ids["root"]},
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        deliveries = self.deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual((deliveries[0].template, deliveries[0].status), ("member_invitation", "queued"))
        self.assertEqual(deliveries[0].recipient_masked, "n***@example.com")
        # The worker delivers it; the invitation link (with its token) reaches the mail only.
        with self.session_factory() as db:
            job = db.get(Job, deliveries[0].job_id)
            self.assertIn("/invite?token=", job.payload["context"]["invitation_url"])
        self.run_jobs()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0]["to_addr"], "newbie@example.com")

    def test_resending_queues_another_mail(self) -> None:
        headers = self.login("admin@example.com")
        created = self.client.post(
            "/api/v1/members/invitations",
            json={"emails": ["again@example.com"], "role": "viewer", "organization_id": self.ids["root"]},
            headers=headers,
        ).json()
        invitation_id = created["invitations"][0]["id"]
        resent = self.client.post(f"/api/v1/members/invitations/{invitation_id}/resend", headers=headers)
        self.assertEqual(resent.status_code, 200, resent.text)
        self.assertEqual(len(self.deliveries()), 2)

    def test_only_admins_can_send_invitation_mails(self) -> None:
        builder = self.login("builder@example.com")
        response = self.client.post(
            "/api/v1/members/invitations",
            json={"emails": ["x@example.com"], "role": "viewer", "organization_id": self.ids["root"]},
            headers=builder,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.deliveries(), [])

    def test_another_tenants_invitation_cannot_be_resent(self) -> None:
        with self.session_factory() as db:
            other_org = db.scalar(
                select(Organization).where(Organization.tenant_id == UUID(self.ids["other"]))
            )
            other_org_id = str(other_org.id)
        other_headers = self.login("other@example.com", tenant=self.ids["other"])
        response = self.client.post(
            "/api/v1/members/invitations",
            json={"emails": ["their@example.com"], "role": "viewer", "organization_id": other_org_id},
            headers=other_headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        invitation_id = response.json()["invitations"][0]["id"]
        headers = self.login("admin@example.com")
        response = self.client.post(f"/api/v1/members/invitations/{invitation_id}/resend", headers=headers)
        self.assertEqual(response.status_code, 404)

    def test_rate_limit_answers_429(self) -> None:
        # Three mails to this recipient within the window: the API must refuse instead of silently dropping.
        with self.session_factory.begin() as db:
            recipient_hash = hash_email("flood@example.com")
            for _ in range(3):
                db.add(MailDelivery(
                    tenant_id=UUID(self.ids["tenant"]), template="member_invitation", locale="zh-CN",
                    recipient_masked="f***@example.com", recipient_hash=recipient_hash, status="sent",
                ))
        headers = self.login("admin@example.com")
        response = self.client.post(
            "/api/v1/members/invitations",
            json={"emails": ["flood@example.com"], "role": "viewer", "organization_id": self.ids["root"]},
            headers=headers,
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "mail.rate_limited")


class PlatformTenantInvitationTests(AssistantTests):
    """Creating a tenant with ``send_admin_invitation`` mails an admin invite instead of a password (PLT-022)."""

    def setUp(self) -> None:
        super().setUp()
        self.transport = RecordingTransport()
        mail_service.set_transport(self.transport)
        self.addCleanup(mail_service.set_transport, None)
        with self.session_factory.begin() as db:
            db.add(User(email="platform@example.com", display_name="Platform",
                        password_hash=hash_password(PASSWORD), is_platform_admin=True))

    def platform_login(self) -> dict[str, str]:
        self.client.cookies.clear()
        response = self.client.post("/api/v1/auth/login", json={"email": "platform@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf")}

    def payload(self, **overrides) -> dict:
        body = {
            "name": "Invited Co", "slug": f"invited-{uuid4().hex[:8]}", "seat_limit": 10,
            "admin_name": "Alex Chen", "admin_email": "alex@invited.example",
            "admin_password": "an-initial-password-123",
        }
        body.update(overrides)
        return body

    def test_creating_with_an_invitation_mails_the_admin_and_sets_no_password(self) -> None:
        headers = self.platform_login()
        response = self.client.post(
            "/api/v1/platform/tenants",
            json=self.payload(admin_password=None, send_admin_invitation=True),
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertIsNone(body["admin_email"])  # no admin member exists yet
        with self.session_factory() as db:
            invitation = db.scalar(select(MemberInvitation).where(MemberInvitation.role == "tenant_admin"))
            self.assertIsNotNone(invitation)
            self.assertEqual(invitation.email, "alex@invited.example")
            memberships = db.scalars(
                select(Membership).where(Membership.tenant_id == invitation.tenant_id)
            ).all()
            self.assertEqual(memberships, [])  # the admin joins only after accepting
            delivery = db.scalar(select(MailDelivery).where(MailDelivery.template == "tenant_admin_invitation"))
            self.assertIsNotNone(delivery)
            self.assertEqual(delivery.status, "queued")
            job = db.get(Job, delivery.job_id)
            self.assertIn("/invite?token=", job.payload["context"]["invitation_url"])
            self.assertIn("Alex Chen", job.payload["context"]["admin_name"])
            tenant = db.get(Tenant, invitation.tenant_id)
            self.assertEqual(tenant.name, "Invited Co")

    def test_password_mode_still_works_and_is_the_default(self) -> None:
        headers = self.platform_login()
        response = self.client.post("/api/v1/platform/tenants", json=self.payload(), headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNotNone(response.json()["admin_email"])
        with self.session_factory() as db:
            admin_membership = db.scalar(
                select(Membership).where(Membership.role == "tenant_admin")
            )
            self.assertIsNotNone(admin_membership)

    def test_platform_delivery_list_masks_recipients_and_is_platform_only(self) -> None:
        headers = self.platform_login()
        created = self.client.post(
            "/api/v1/platform/tenants",
            json=self.payload(admin_password=None, send_admin_invitation=True),
            headers=headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        listing = self.client.get("/api/v1/platform/mail/deliveries?limit=10", headers=headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        items = listing.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "queued")
        self.assertEqual(items[0]["recipient_masked"], "a***@invited.example")
        self.assertEqual(items[0]["template"], "tenant_admin_invitation")
        filtered = self.client.get(
            "/api/v1/platform/mail/deliveries?status=sent", headers=headers
        )
        self.assertEqual(filtered.json()["items"], [])
        tenant_user = self.login("admin@example.com")
        self.assertEqual(
            self.client.get("/api/v1/platform/mail/deliveries", headers=tenant_user).status_code,
            403,
        )

    def test_invitation_and_password_are_mutually_exclusive(self) -> None:
        headers = self.platform_login()
        both = self.client.post(
            "/api/v1/platform/tenants",
            json=self.payload(send_admin_invitation=True),
            headers=headers,
        )
        self.assertEqual(both.status_code, 422)
        neither = self.client.post(
            "/api/v1/platform/tenants",
            json=self.payload(admin_password=None),
            headers=headers,
        )
        self.assertEqual(neither.status_code, 422)


for _name in [n for n in dir(AssistantTests) if n.startswith("test_")]:
    setattr(InvitationMailTests, _name, None)
    setattr(PlatformTenantInvitationTests, _name, None)

if __name__ == "__main__":
    unittest.main()
