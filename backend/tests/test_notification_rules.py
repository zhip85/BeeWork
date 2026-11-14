"""T-088: notification rules — the catalog, validation per channel, realtime and digest sending, recipients, and the events other modules emit."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from beework import events, notification_catalog, notify_rules, sms
from beework.config import settings
from beework.model_secrets import decrypt_model_api_key
from beework.models import Membership, User
from beework.models.channels import TenantApiKey
from beework.models.jobs import Job
from beework.models.mail import MailDelivery
from beework.models.notification_rules import NotificationQueueItem, NotificationRule
from beework.models.webhooks import DomainEvent, WebhookDelivery, WebhookEndpoint
from beework.security import hash_password
from test_webhooks import PASSWORD, Base_

PUBLIC = lambda _host: ["93.184.216.34"]  # noqa: E731
SLACK = "https://hooks.slack.com/services/T000/B000/abcdefgh1234"
NOW = datetime(2026, 11, 14, 1, 10, tzinfo=UTC)


class CatalogTests(unittest.TestCase):
    def test_there_are_twelve_events_with_texts_in_both_languages(self) -> None:
        self.assertEqual(len(notification_catalog.RULE_EVENTS), 12)
        samples = {
            "approval.requested": {"state": "submitted"}, "approval.result": {"state": "rejected"}, "guardrail.triggered": {"action": "block"}, "handoff.requested": {},
            "handoff.timeout": {}, "budget.threshold": {"threshold": 80, "used": 40000, "budget": 50000}, "quota.threshold": {"quota": "知识库存储", "percent": 90},
            "evaluation.completed": {}, "channel.unhealthy": {"name": "官网"}, "mcp.unhealthy": {"name": "CRM"}, "key.expiring": {"name": "ci", "days": 3}, "security.event": {"kind": "login_locked"},
        }
        for event, payload in samples.items():
            zh, en = notification_catalog.render(event, payload, "zh-CN"), notification_catalog.render(event, payload, "en")
            self.assertTrue(zh[1] and en[1] and zh[2].startswith("/"), event)
            self.assertNotEqual(zh[1], en[1], event)
            self.assertIn(notification_catalog.LABELS[event][0], zh[0])
            self.assertIn(notification_catalog.LABELS[event][1], en[0])
        self.assertEqual(set(samples), set(notification_catalog.RULE_EVENTS))

    def test_bus_events_map_onto_rule_events(self) -> None:
        rule_events = notification_catalog.rule_events_of
        self.assertEqual(rule_events("approval.updated", {"state": "submitted"}), ["approval.requested"])
        self.assertEqual(rule_events("approval.updated", {"state": "level_approved"}), ["approval.requested"])  # the next level has something to do
        for state in ("approved", "rejected", "withdrawn"):
            self.assertEqual(rule_events("approval.updated", {"state": state}), ["approval.result"])
        self.assertEqual(rule_events("usage.threshold", {}), ["budget.threshold"])
        self.assertEqual(rule_events("channel.unhealthy", {}), ["channel.unhealthy"])
        self.assertEqual(rule_events("conversation.completed", {}), [])  # webhook-only events are not notifications
        self.assertEqual(rule_events("assistant.published", {}), [])


class Setup(Base_):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            tenant = self.ids["mine"]
            org = db.scalar(select(Membership.organization_id).where(Membership.tenant_id == tenant))
            self.extra = {}
            for name in ("second-admin", "builder"):
                user = User(email=f"{name}@mine.example.com", display_name=name, password_hash=hash_password(PASSWORD), locale="en" if name == "builder" else None)
                db.add(user)
                db.flush()
                db.add(Membership(tenant_id=tenant, user_id=user.id, organization_id=org, role="tenant_admin" if name == "second-admin" else "builder"))
                self.extra[name] = user.id
        self.admin = self.login()
        self.addCleanup(sms.register_provider, None)

    def rule(self, headers=None, **fields):
        body = {"event": "approval.result", "channel": "email", "recipients": {"emails": ["ops@example.com"]}, "frequency": "realtime", **fields}
        return self.client.post("/api/v1/notification-rules", json=body, headers=headers or self.admin)

    def created(self, **fields) -> dict:
        response = self.rule(**fields)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def emit(self, kind: str = "approval.updated", payload: dict | None = None, code: str = "mine") -> UUID:
        with self.session_factory.begin() as db:
            return events.emit_event(db, self.ids[code], kind, payload or {"approval_id": "a1", "state": "approved"}).id

    def run_pending(self) -> None:
        """What the Worker does: dispatch the events, then send the realtime notifications that were queued."""
        with self.session_factory() as db:
            for job in list(db.scalars(select(Job).where(Job.kind == "events.dispatch", Job.status == "queued"))):
                events.dispatch_event(db, job.payload)
                job.status = "succeeded"
            db.commit()
        with self.session_factory() as db:
            for job in list(db.scalars(select(Job).where(Job.kind == "notifications.send", Job.status == "queued"))):
                notify_rules.send_job(db, job.payload)
                job.status = "succeeded"
            db.commit()

    def mails(self) -> list[tuple[str, dict]]:
        """(recipient, context) of every queued notification mail, read from the sealed job payloads."""
        out = []
        with self.session_factory() as db:
            for job in db.scalars(select(Job).where(Job.kind == "mail.send")):
                sealed = json.loads(decrypt_model_api_key(job.payload["sealed"]))
                out.append((sealed["to"], sealed["context"]))
        return sorted(out, key=lambda item: item[0])


class RuleApiTests(Setup):
    def test_the_catalog_lists_events_channels_roles_members_and_endpoints_of_this_tenant_only(self) -> None:
        with self.session_factory.begin() as db:
            db.add(WebhookEndpoint(tenant_id=self.ids["other"], url="https://x.example.com", events=[], secret_encrypted="x", secret_last4="x"))
        catalog = self.client.get("/api/v1/notification-rules/catalog", headers=self.admin).json()
        self.assertEqual([e["key"] for e in catalog["events"]], list(notification_catalog.RULE_EVENTS))
        self.assertEqual(next(c for c in catalog["channels"] if c["key"] == "sms"), {"key": "sms", "available": False, "reason": "unconfigured"})
        self.assertTrue(all(c["available"] for c in catalog["channels"] if c["key"] != "sms"))
        self.assertEqual({m["name"].split(" <")[0] for m in catalog["members"]}, {"admin-mine", "viewer-mine", "second-admin", "builder"})
        self.assertEqual(catalog["webhooks"], [])  # the other tenant's endpoint is not offered
        self.assertIn("tenant_admin", {r["id"] for r in catalog["roles"]})

    def test_email_recipients_are_checked(self) -> None:
        member = str(self.extra["builder"])
        self.assertEqual(self.created(recipients={"members": [member], "roles": ["tenant_admin"], "emails": ["A@Example.com", "a@example.com"]})["recipients"]["emails"], ["a@example.com"])
        for recipients in ({}, {"emails": ["not-an-email"]}, {"roles": ["superuser"]}, {"members": [str(uuid4())]}, {"members": [str(self.ids["other"])]}):
            refused = self.rule(recipients=recipients)
            self.assertIn(refused.status_code, (422,), recipients)
        with self.session_factory() as db:
            their_user = db.scalar(select(User.id).where(User.email == "admin@other.example.com"))
        crossing = self.rule(recipients={"members": [str(their_user)]})
        self.assertEqual((crossing.status_code, crossing.json()["code"]), (422, "notification.invalid_recipient"))  # not a member of this tenant

    def test_event_channel_and_frequency_must_be_known(self) -> None:
        self.assertEqual(self.rule(event="conversation.completed").status_code, 422)
        self.assertEqual(self.rule(channel="fax").status_code, 422)
        self.assertEqual(self.rule(frequency="weekly").status_code, 422)

    def test_a_slack_address_must_be_public_https_and_is_never_shown_again(self) -> None:
        for url in ("http://hooks.slack.com/x", "https://localhost/x", "https://10.0.0.1/x", ""):
            refused = self.rule(channel="slack", recipients={"url": url})
            self.assertEqual(refused.status_code, 400 if url else 400, url)
        rule = self.created(channel="slack", recipients={"url": SLACK})
        self.assertEqual(rule["recipients"], {"url_mask": "hooks.slack.com/••••1234"})
        self.assertNotIn("abcdefgh", json.dumps(self.client.get("/api/v1/notification-rules", headers=self.admin).json()))
        with self.session_factory() as db:
            stored = db.get(NotificationRule, UUID(rule["id"])).recipients
            self.assertNotIn("abcdefgh", json.dumps(stored))  # encrypted at rest
            self.assertEqual(decrypt_model_api_key(stored["url_sealed"]), SLACK)
        kept = self.client.put(f"/api/v1/notification-rules/{rule['id']}", json={"event": "key.expiring", "channel": "slack", "recipients": {}, "frequency": "daily", "row_version": rule["row_version"]}, headers=self.admin)
        self.assertEqual((kept.status_code, kept.json()["recipients"]["url_mask"], kept.json()["event"]), (200, "hooks.slack.com/••••1234", "key.expiring"))  # blank keeps the stored address

    def test_sms_is_unavailable_until_a_provider_is_configured(self) -> None:
        refused = self.rule(channel="sms", recipients={"phones": ["+8613800138000"]})
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "notification.sms_unconfigured"))

        class Fake:
            name = "fake"

            def send(self, phone, text):
                pass

        sms.register_provider(Fake())
        self.assertEqual(self.rule(channel="sms", recipients={"phones": ["+8613800138000"]}).status_code, 201)
        self.assertEqual(self.rule(channel="sms", recipients={"phones": ["12"]}).status_code, 422)
        self.assertTrue(next(c for c in self.client.get("/api/v1/notification-rules/catalog", headers=self.admin).json()["channels"] if c["key"] == "sms")["available"])

    def test_webhook_rules_can_only_use_the_tenants_own_endpoints(self) -> None:
        mine = self.client.post("/api/v1/webhooks", json={"url": "https://hooks.example.com/a", "events": ["approval.updated"]}, headers=self.admin).json()["id"]
        self.assertEqual(self.rule(channel="webhook", recipients={"webhook_ids": [mine]}).status_code, 201)
        self.assertEqual(self.rule(channel="webhook", recipients={"webhook_ids": [str(uuid4())]}).status_code, 422)
        self.assertEqual(self.rule(channel="webhook", recipients={}).status_code, 422)

    def test_permissions_isolation_search_and_deleting(self) -> None:
        rule = self.created()
        viewer = self.login("viewer")
        self.assertEqual(self.client.get("/api/v1/notification-rules", headers=viewer).status_code, 403)
        self.assertEqual(self.rule(headers=viewer).status_code, 403)
        other = self.login("admin", "other")
        self.assertEqual(self.client.get("/api/v1/notification-rules", headers=other).json()["items"], [])
        self.assertEqual(self.client.delete(f"/api/v1/notification-rules/{rule['id']}", headers=other).status_code, 404)
        self.assertEqual(self.client.put(f"/api/v1/notification-rules/{rule['id']}", json={"event": "approval.result", "channel": "email", "recipients": {"emails": ["a@b.co"]}}, headers=other).status_code, 404)
        admin = self.admin = self.login()
        self.created(event="budget.threshold", frequency="daily")
        self.assertEqual([r["event"] for r in self.client.get("/api/v1/notification-rules", params={"q": "预算"}, headers=admin).json()["items"]], ["budget.threshold"])
        self.assertEqual(self.client.delete(f"/api/v1/notification-rules/{rule['id']}", headers=admin).status_code, 204)
        stale = self.client.put(f"/api/v1/notification-rules/{self.created()['id']}", json={"event": "approval.result", "channel": "email", "recipients": {"emails": ["a@b.co"]}, "row_version": 99}, headers=admin)
        self.assertEqual(stale.status_code, 409)


class SendingTests(Setup):
    def test_a_realtime_rule_mails_members_the_current_holders_of_a_role_and_typed_addresses_of_its_own_tenant(self) -> None:
        self.created(recipients={"members": [str(self.extra["builder"])], "roles": ["tenant_admin"], "emails": ["ops@example.com"]})
        self.emit()
        self.run_pending()
        recipients = [to for to, _ in self.mails()]
        self.assertEqual(recipients, ["admin@mine.example.com", "builder@mine.example.com", "ops@example.com", "second-admin@mine.example.com"])
        self.assertNotIn("admin@other.example.com", recipients)  # nobody of another tenant
        title = self.mails()[0][1]["title"]
        self.assertIn("审批结果", title)

    def test_a_role_means_whoever_holds_it_when_the_message_is_sent(self) -> None:
        self.created(recipients={"roles": ["viewer"]})
        self.emit()
        with self.session_factory.begin() as db:  # a viewer joins after the rule was made, before the message goes out
            org = db.scalar(select(Membership.organization_id).where(Membership.tenant_id == self.ids["mine"]))
            user = User(email="late@mine.example.com", display_name="late", password_hash="x")
            db.add(user)
            db.flush()
            db.add(Membership(tenant_id=self.ids["mine"], user_id=user.id, organization_id=org, role="viewer"))
        self.run_pending()
        self.assertEqual([to for to, _ in self.mails()], ["late@mine.example.com", "viewer@mine.example.com"])

    def test_only_rules_of_the_matching_event_and_tenant_fire(self) -> None:
        self.created(event="approval.result")
        self.created(event="approval.requested", recipients={"emails": ["requests@example.com"]})
        other = self.login("admin", "other")
        self.rule(headers=other, event="approval.result", recipients={"emails": ["other-tenant@example.com"]})
        self.admin = self.login()
        self.emit(payload={"approval_id": "a1", "state": "submitted"})
        self.run_pending()
        self.assertEqual([to for to, _ in self.mails()], ["requests@example.com"])  # submitted is “发起审批”, not a result; the other tenant's rule did not hear it

    def test_a_disabled_rule_sends_nothing_and_a_rule_switched_off_after_queueing_sends_nothing(self) -> None:
        rule = self.created(enabled=False)
        self.emit()
        self.run_pending()
        self.assertEqual(self.mails(), [])
        on = self.created()
        self.emit()
        self.assertEqual(self.client.put(f"/api/v1/notification-rules/{on['id']}", json={"event": "approval.result", "channel": "email", "recipients": {"emails": ["ops@example.com"]}, "enabled": False}, headers=self.admin).status_code, 200)
        self.run_pending()
        self.assertEqual(self.mails(), [])
        self.assertIsNotNone(rule)

    def test_an_event_nobody_has_a_rule_for_costs_no_job(self) -> None:
        self.created(event="key.expiring")
        self.emit()  # an approval result: no rule listens
        with self.session_factory() as db:
            self.assertIsNone(db.scalar(select(Job.id).where(Job.kind == "events.dispatch")))

    def test_the_hourly_digest_merges_the_events_of_the_hour_into_one_mail(self) -> None:
        rule = self.created(frequency="hourly", recipients={"emails": ["ops@example.com"]})
        for _ in range(3):
            self.emit(payload={"approval_id": str(uuid4()), "state": "approved"})
        self.run_pending()
        self.assertEqual(self.mails(), [])  # nothing at once
        with self.session_factory() as db:
            self.assertEqual(len(db.scalars(select(NotificationQueueItem).where(NotificationQueueItem.rule_id == UUID(rule["id"]))).all()), 3)
            self.assertEqual(notify_rules.run_digests(db, NOW), 1)
        mails = self.mails()
        self.assertEqual(len(mails), 1)  # three events, one mail
        self.assertIn("3 条", mails[0][1]["title"])
        self.assertEqual(mails[0][1]["message"].count("\n- ") + 1, 3)
        with self.session_factory() as db:
            self.assertEqual(notify_rules.run_digests(db, NOW + timedelta(hours=1)), 0)  # already sent: nothing to repeat
        self.assertEqual(len(self.mails()), 1)

    def test_a_daily_digest_goes_out_only_at_the_configured_hour(self) -> None:
        self.created(frequency="daily", recipients={"emails": ["ops@example.com"]})
        self.emit()
        self.run_pending()
        with self.session_factory() as db:
            self.assertEqual(notify_rules.run_digests(db, NOW.replace(hour=settings.notification_daily_hour_utc + 3)), 0)
        self.assertEqual(self.mails(), [])
        with self.session_factory() as db:
            self.assertEqual(notify_rules.run_digests(db, NOW.replace(hour=settings.notification_daily_hour_utc)), 1)
        self.assertEqual(len(self.mails()), 1)

    def test_a_rule_switched_off_empties_its_queue_without_saying_anything(self) -> None:
        rule = self.created(frequency="hourly")
        self.emit()
        self.run_pending()
        self.client.put(f"/api/v1/notification-rules/{rule['id']}", json={"event": "approval.result", "channel": "email", "recipients": {"emails": ["ops@example.com"]}, "frequency": "hourly", "enabled": False}, headers=self.admin)
        with self.session_factory() as db:
            notify_rules.run_digests(db, NOW)
            self.assertEqual(db.scalar(select(NotificationQueueItem.sent_at)) is not None, True)
        self.assertEqual(self.mails(), [])

    def test_slack_gets_one_message_and_a_failure_keeps_the_digest_for_the_next_run(self) -> None:
        self.created(channel="slack", recipients={"url": SLACK}, frequency="hourly")
        self.emit()
        self.run_pending()
        sent: list[httpx.Request] = []

        def refuse(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(500)

        with self.session_factory() as db:
            self.assertEqual(notify_rules.run_digests(db, NOW, transport=httpx.MockTransport(refuse), resolver=PUBLIC), 0)
            self.assertIsNone(db.scalar(select(NotificationQueueItem.sent_at)))  # still queued
        with self.session_factory() as db:
            self.assertEqual(notify_rules.run_digests(db, NOW + timedelta(hours=1), transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200))[1]), resolver=PUBLIC), 1)
        self.assertEqual(len(sent), 2)
        text = json.loads(sent[1].content)["text"]
        self.assertIn("审批结果", text)
        self.assertEqual((sent[1].method, sent[1].headers["host"]), ("POST", "hooks.slack.com"))

    def test_sms_goes_to_the_provider_and_a_webhook_rule_becomes_a_delivery(self) -> None:
        texts: list[tuple[str, str]] = []

        class Fake:
            name = "fake"

            def send(self, phone, text):
                texts.append((phone, text))

        sms.register_provider(Fake())
        self.created(channel="sms", recipients={"phones": ["+8613800138000", "+8613900139000"]})
        hook = self.client.post("/api/v1/webhooks", json={"url": "https://hooks.example.com/a", "events": ["usage.threshold"]}, headers=self.admin).json()["id"]
        self.created(channel="webhook", recipients={"webhook_ids": [hook]})
        self.emit()
        self.run_pending()
        self.assertEqual([phone for phone, _ in texts], ["+8613800138000", "+8613900139000"])
        self.assertIn("审批", texts[0][1])
        with self.session_factory() as db:
            delivery = db.scalar(select(WebhookDelivery))
            self.assertEqual((delivery.event_type, delivery.payload["event"]), ("notification", "approval.result"))

    def test_mail_language_follows_the_recipient(self) -> None:
        self.created(recipients={"members": [str(self.extra["builder"])]})
        self.emit()
        self.run_pending()
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(MailDelivery.locale)), "en")
        self.assertIn("Approval result", self.mails()[0][1]["title"])


class EmitterTests(Setup):
    def listening(self, event: str) -> None:
        self.created(event=event, recipients={"emails": ["ops@example.com"]})

    def test_an_unhealthy_channel_emits_channel_unhealthy(self) -> None:
        from beework.channel_health import check_channel
        from beework.models.assistants import Assistant
        from beework.models.channels import ChannelInstance

        self.listening("channel.unhealthy")
        with self.session_factory.begin() as db:
            org = db.scalar(select(Membership.organization_id).where(Membership.tenant_id == self.ids["mine"]))
            assistant = Assistant(tenant_id=self.ids["mine"], organization_id=org, name="Bot", draft_config={})
            db.add(assistant)
            db.flush()
            channel = ChannelInstance(tenant_id=self.ids["mine"], name="官网", assistant_id=assistant.id, site_key="bw_site_t088000000000000001", status="active", published_snapshot={"allowed_origins": []})
            db.add(channel)
            db.flush()
            for _ in range(3):
                check_channel(db, channel)
        with self.session_factory() as db:
            event = db.scalar(select(DomainEvent).where(DomainEvent.type == "channel.unhealthy"))
            self.assertEqual((event.payload["name"], event.payload["type"]), ("官网", "website"))

    def test_a_key_that_expires_within_a_week_is_announced_once(self) -> None:
        from beework.api_keys import announce_expiring_keys

        self.listening("key.expiring")
        with self.session_factory.begin() as db:
            db.add_all([
                TenantApiKey(tenant_id=self.ids["mine"], name="soon", environment="production", scopes=["chat"], key_hash="h1", masked="m1", expires_at=NOW + timedelta(days=3)),
                TenantApiKey(tenant_id=self.ids["mine"], name="later", environment="production", scopes=["chat"], key_hash="h2", masked="m2", expires_at=NOW + timedelta(days=30)),
                TenantApiKey(tenant_id=self.ids["mine"], name="gone", environment="production", scopes=["chat"], key_hash="h3", masked="m3", expires_at=NOW + timedelta(days=2), revoked_at=NOW),
            ])
        with self.session_factory() as db:
            self.assertEqual(announce_expiring_keys(db, NOW), 1)
            self.assertEqual(announce_expiring_keys(db, NOW + timedelta(days=1)), 0)  # said once
        with self.session_factory() as db:
            payload = db.scalar(select(DomainEvent.payload).where(DomainEvent.type == "key.expiring"))
            self.assertEqual((payload["name"], payload["days"]), ("soon", 3))
            self.assertNotIn("h1", json.dumps(payload))

    def test_a_locked_account_emits_a_security_event_for_each_of_its_tenants(self) -> None:
        from beework import login_security

        self.listening("security.event")
        with self.session_factory.begin() as db:
            user = db.scalar(select(User).where(User.email == "admin@mine.example.com"))
            login_security._notify_admins(db, user)
        with self.session_factory() as db:
            event = db.scalar(select(DomainEvent).where(DomainEvent.type == "security.event"))
            self.assertEqual((event.tenant_id, event.payload["kind"]), (self.ids["mine"], "login_locked"))
            self.assertNotIn("admin@", json.dumps(event.payload))  # an id, never the address


if __name__ == "__main__":
    unittest.main()
