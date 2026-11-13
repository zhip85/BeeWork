"""T-008b: the platform resends an expired tenant provisioning invitation (PLT-022).

Covers the happy path (token rotation, mail, both audit chains, accepting the new link),
authorisation, tenant isolation, invalid states, rate limiting and the PostgreSQL
concurrency guarantee of the tenant row lock.
"""
import hashlib
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import sessionmaker

from beework.db import get_db
from beework.mail import hash_email, mask_email
from beework.main import app
from beework.model_secrets import decrypt_model_api_key
from beework.models import AuditEvent, MemberInvitation, Membership, Organization, Tenant, User
from beework.models.jobs import Job
from beework.models.mail import MailDelivery
from beework.security import hash_password
from isolation import disable_external_integrations
from test_assistants import PASSWORD, AssistantTests

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
ADMIN_EMAIL = "alex@invited.example"
ADMIN_NAME = "Alex Chen"
RESEND_ACTION = "tenant.admin_invitation_resent"


def token_of(sealed: dict) -> str:
    return sealed["context"]["invitation_url"].split("token=", 1)[1]


class AdminInvitationResendTests(AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            db.add(User(
                email="platform@example.com", display_name="Platform",
                password_hash=hash_password(PASSWORD), is_platform_admin=True,
            ))

    def platform_login(self) -> dict[str, str]:
        self.client.cookies.clear()
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "platform@example.com", "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf")}

    def provision(self, headers: dict[str, str], **overrides) -> dict:
        payload = {
            "name": "Invited Co", "slug": f"invited-{uuid4().hex[:8]}", "seat_limit": 10,
            "admin_name": ADMIN_NAME, "admin_email": ADMIN_EMAIL,
            "admin_password": None, "send_admin_invitation": True,
        }
        payload.update(overrides)
        response = self.client.post("/api/v1/platform/tenants", json=payload, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def invitation_id(self) -> UUID:
        with self.session_factory() as db:
            invitation = db.scalar(
                select(MemberInvitation).where(MemberInvitation.role == "tenant_admin")
            )
            assert invitation is not None
            return invitation.id

    def url(self, tenant_id: str, invitation_id: UUID) -> str:
        return f"/api/v1/platform/tenants/{tenant_id}/admin-invitations/{invitation_id}/resend"

    def expire(self, invitation_id: UUID) -> None:
        with self.session_factory.begin() as db:
            db.get(MemberInvitation, invitation_id).expires_at = datetime.now(UTC) - timedelta(minutes=1)

    def mails(self, db) -> list[tuple[MailDelivery, dict]]:
        deliveries = db.scalars(
            select(MailDelivery)
            .where(MailDelivery.template == "tenant_admin_invitation")
            .order_by(MailDelivery.created_at, MailDelivery.id)
        ).all()
        return [
            (delivery, json.loads(decrypt_model_api_key(db.get(Job, delivery.job_id).payload["sealed"])))
            for delivery in deliveries
        ]

    def tenant_row(self, headers: dict[str, str], tenant_id: str) -> dict:
        listing = self.client.get("/api/v1/platform/tenants", headers=headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        return next(item for item in listing.json() if item["id"] == tenant_id)

    # -- happy path ----------------------------------------------------------------------------

    def test_expired_invitation_is_resent_and_only_the_new_link_works(self) -> None:
        headers = self.platform_login()
        tenant = self.provision(headers)
        invitation_id = self.invitation_id()
        with self.session_factory() as db:
            invitation = db.get(MemberInvitation, invitation_id)
            old_hash = invitation.token_hash
            old_token = token_of(self.mails(db)[0][1])

        self.expire(invitation_id)
        listed = self.tenant_row(headers, tenant["id"])
        self.assertEqual(
            (listed["admin_invitation_id"], listed["admin_invitation_status"],
             listed["admin_invitation_name"], listed["admin_invitation_email"]),
            (str(invitation_id), "expired", ADMIN_NAME, ADMIN_EMAIL),
        )

        response = self.client.post(self.url(tenant["id"], invitation_id), headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["admin_invitation_status"], "pending")

        with self.session_factory() as db:
            invitation = db.get(MemberInvitation, invitation_id)
            self.assertEqual(invitation.status, "pending")
            self.assertNotEqual(invitation.token_hash, old_hash)
            expiry = invitation.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            self.assertGreater(expiry, datetime.now(UTC) + timedelta(days=6))
            mails = self.mails(db)
            self.assertEqual(len(mails), 2)
            sealed = mails[1][1]
            new_token = token_of(sealed)
            self.assertNotEqual(new_token, old_token)
            self.assertEqual(invitation.token_hash, hashlib.sha256(new_token.encode()).hexdigest())
            self.assertEqual((sealed["to"], sealed["context"]["admin_name"]), (ADMIN_EMAIL, ADMIN_NAME))
            self.assertEqual(mails[1][0].recipient_masked, "a***@invited.example")
            self.assertEqual(mails[1][0].recipient_hash, hash_email(ADMIN_EMAIL))
            audits = list(db.scalars(select(AuditEvent).where(AuditEvent.action == RESEND_ACTION)))
            self.assertEqual(len(audits), 2)
            self.assertEqual({entry.tenant_id for entry in audits}, {None, UUID(tenant["id"])})
            for entry in audits:
                self.assertEqual(entry.details["email"], mask_email(ADMIN_EMAIL))
                self.assertNotIn(new_token, json.dumps(entry.details, ensure_ascii=False))

        rejected = self.client.post(
            "/api/v1/members/invitations/accept",
            json={"token": old_token, "password": PASSWORD, "display_name": "Old Link"},
        )
        self.assertEqual(rejected.status_code, 410)
        accepted = self.client.post(
            "/api/v1/members/invitations/accept",
            json={"token": new_token, "password": PASSWORD, "display_name": ADMIN_NAME},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["email"], ADMIN_EMAIL)
        headers = self.platform_login()  # accepting the invitation signed this client in as the new admin
        settled = self.tenant_row(headers, tenant["id"])
        self.assertIsNone(settled["admin_invitation_id"])
        self.assertEqual(settled["admin_email"], ADMIN_EMAIL)

    def test_a_still_pending_invitation_can_also_be_resent(self) -> None:
        headers = self.platform_login()
        tenant = self.provision(headers)
        invitation_id = self.invitation_id()
        self.assertEqual(self.tenant_row(headers, tenant["id"])["admin_invitation_status"], "pending")
        response = self.client.post(self.url(tenant["id"], invitation_id), headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        with self.session_factory() as db:
            self.assertEqual(len(self.mails(db)), 2)

    def test_the_mail_locale_follows_the_platform_admin(self) -> None:
        with self.session_factory.begin() as db:
            db.scalar(select(User).where(User.email == "platform@example.com")).locale = "en"
        headers = self.platform_login()
        tenant = self.provision(headers)
        invitation_id = self.invitation_id()
        self.assertEqual(
            self.client.post(self.url(tenant["id"], invitation_id), headers=headers).status_code, 200
        )
        with self.session_factory() as db:
            self.assertEqual([delivery.locale for delivery, _ in self.mails(db)], ["en", "en"])

    # -- permissions and isolation --------------------------------------------------------------

    def test_only_platform_admins_can_resend(self) -> None:
        headers = self.platform_login()
        tenant = self.provision(headers)
        tenant_admin = self.login("admin@example.com")
        response = self.client.post(self.url(tenant["id"], self.invitation_id()), headers=tenant_admin)
        self.assertEqual(response.status_code, 403)

    def test_resend_requires_csrf(self) -> None:
        headers = self.platform_login()
        tenant = self.provision(headers)
        response = self.client.post(self.url(tenant["id"], self.invitation_id()))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(headers["X-CSRF-Token"], self.client.cookies.get("beework_csrf"))

    def test_another_tenants_invitation_or_an_unknown_id_is_a_404(self) -> None:
        headers = self.platform_login()
        tenant = self.provision(headers)
        invitation_id = self.invitation_id()
        urls = [
            self.url(self.ids["other"], invitation_id),
            self.url(str(uuid4()), invitation_id),
            self.url(tenant["id"], uuid4()),
        ]
        for url in urls:
            self.assertEqual(self.client.post(url, headers=headers).status_code, 404, url)
        with self.session_factory() as db:
            self.assertEqual(len(self.mails(db)), 1)

    # -- validation and state machine -----------------------------------------------------------

    def test_accepted_or_revoked_invitations_and_tenants_with_an_admin_are_rejected(self) -> None:
        headers = self.platform_login()
        tenant = self.provision(headers)
        invitation_id = self.invitation_id()
        url = self.url(tenant["id"], invitation_id)
        for state in ("accepted", "revoked"):
            with self.session_factory.begin() as db:
                db.get(MemberInvitation, invitation_id).status = state
            response = self.client.post(url, headers=headers)
            self.assertEqual(response.status_code, 409, state)
            self.assertEqual(response.json()["code"], "invitation.not_resendable")
        with self.session_factory.begin() as db:
            db.get(MemberInvitation, invitation_id).status = "pending"
            organization = db.scalar(
                select(Organization).where(Organization.tenant_id == UUID(tenant["id"]))
            )
            existing = db.scalar(select(User).where(User.email == "admin@example.com"))
            db.add(Membership(
                tenant_id=UUID(tenant["id"]), user_id=existing.id,
                organization_id=organization.id, role="tenant_admin",
            ))
        response = self.client.post(url, headers=headers)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "invitation.not_resendable")

    def test_a_deleted_tenant_cannot_be_resent_to(self) -> None:
        headers = self.platform_login()
        tenant = self.provision(headers)
        invitation_id = self.invitation_id()
        with self.session_factory.begin() as db:
            db.get(Tenant, UUID(tenant["id"])).status = "deleted"
        response = self.client.post(self.url(tenant["id"], invitation_id), headers=headers)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "tenant.deleted")

    def test_the_rate_limit_answers_429_without_rotating_the_token(self) -> None:
        headers = self.platform_login()
        tenant = self.provision(headers)
        invitation_id = self.invitation_id()
        url = self.url(tenant["id"], invitation_id)
        for _ in range(2):  # provisioning already used one of the three slots
            self.assertEqual(self.client.post(url, headers=headers).status_code, 200)
        with self.session_factory() as db:
            token_hash = db.get(MemberInvitation, invitation_id).token_hash
        response = self.client.post(url, headers=headers)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()["code"], "mail.rate_limited")
        with self.session_factory() as db:
            self.assertEqual(len(self.mails(db)), 3)
            self.assertEqual(db.get(MemberInvitation, invitation_id).token_hash, token_hash)
            self.assertEqual(
                len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == RESEND_ACTION)))), 4
            )  # two successful resends, each written to the tenant chain and the platform chain


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is not set")
class PostgresAdminInvitationResendTests(unittest.TestCase):
    """Two simultaneous resends of one invitation must serialise on the tenant lock."""

    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        cls.engine = create_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=5)
        cls.session_factory = sessionmaker(bind=cls.engine, expire_on_commit=False)

        def override_get_db():
            with cls.session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("TRUNCATE audit_events, audit_chain_checkpoints, jobs, mail_deliveries RESTART IDENTITY CASCADE")
            )
        with self.session_factory.begin() as db:
            for model in (MemberInvitation, Membership, Organization, Tenant, User):
                db.execute(delete(model))
            tenant = Tenant(name="Concurrency", slug="concurrency", seat_limit=10)
            platform = User(
                email="pg-platform@example.com", display_name="Platform",
                password_hash=hash_password(PASSWORD), is_platform_admin=True,
            )
            db.add_all([tenant, platform])
            db.flush()
            organization = Organization(tenant_id=tenant.id, name=tenant.name)
            db.add(organization)
            db.flush()
            db.add(MemberInvitation(
                tenant_id=tenant.id, email=ADMIN_EMAIL, invitee_name=ADMIN_NAME,
                role="tenant_admin", organization_id=organization.id,
                token_hash=hashlib.sha256(b"old-token").hexdigest(),
                status="pending", invited_by=platform.id,
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            ))
            self.tenant_id = str(tenant.id)

    def test_concurrent_resends_are_serialised_and_both_mails_are_queued(self) -> None:
        with self.session_factory() as db:
            invitation_id = db.scalar(select(MemberInvitation.id))
        url = f"/api/v1/platform/tenants/{self.tenant_id}/admin-invitations/{invitation_id}/resend"

        def resend() -> int:
            with TestClient(app) as client:
                login = client.post(
                    "/api/v1/auth/login",
                    json={"email": "pg-platform@example.com", "password": PASSWORD},
                )
                assert login.status_code == 200, login.text
                headers = {"X-CSRF-Token": client.cookies.get("beework_csrf")}
                return client.post(url, headers=headers).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _index: resend(), range(2)))
        self.assertEqual(statuses, [200, 200])

        with self.session_factory() as db:
            deliveries = list(db.scalars(select(MailDelivery)))
            self.assertEqual(len(deliveries), 2)
            tokens = [
                token_of(json.loads(decrypt_model_api_key(db.get(Job, item.job_id).payload["sealed"])))
                for item in deliveries
            ]
            self.assertEqual(len(set(tokens)), 2)
            current_hash = db.get(MemberInvitation, invitation_id).token_hash
            self.assertIn(current_hash, [hashlib.sha256(token.encode()).hexdigest() for token in tokens])
            self.assertEqual(
                len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == RESEND_ACTION)))), 4
            )


for _name in [name for name in dir(AssistantTests) if name.startswith("test_")]:
    setattr(AdminInvitationResendTests, _name, None)
