import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework import entitlements, usage
from beework.api.conventions import ApiError
from beework.db import get_db
from beework.gateway_sync import should_block
from beework.integrations.litellm import LiteLLMClient
from beework.main import app
from beework.models import AuditEvent, Base, Membership, Organization, PlatformModel, Tenant, User
from beework.models.assistants import Assistant
from beework.models.jobs import Job
from beework.models.notifications import InAppNotification
from beework.models.usage import UNKNOWN_ID, UsageAlert, UsageRecord, UsageSeen
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"


class FakeSpendLogs:
    """Stands in for LiteLLM's ``/spend/logs/v2``: filters by start_date, pages and sorts like the real one."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.requests: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/spend/logs/v2"
        params = dict(request.url.params)
        self.requests.append(params)
        start = datetime.strptime(params["start_date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        rows = sorted(
            (row for row in self.rows if datetime.fromisoformat(row["startTime"]) >= start),
            key=lambda row: row["startTime"],
        )
        size, page = int(params["page_size"]), int(params["page"])
        total_pages = max(1, -(-len(rows) // size))
        return httpx.Response(
            200, json={"data": rows[(page - 1) * size : page * size], "total": len(rows), "page": page, "total_pages": total_pages}
        )

    def client(self) -> LiteLLMClient:
        return LiteLLMClient(base_url="http://litellm.test", master_key="k", transport=httpx.MockTransport(self.handler))


def spend_row(
    request_id: str, started: datetime, tenant_id: UUID | None, *, tokens_in: int = 1000, tokens_out: int = 500,
    environment: str = "production", assistant_id: UUID | None = None, source: str = "live",
    model_id: UUID | None = None, status: str = "success",
) -> dict:
    return {
        "request_id": request_id,
        "startTime": started.isoformat(),
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "spend": 0.001,
        "model_id": str(model_id) if model_id else "",
        "status": status,
        "metadata": {
            "user_api_key_alias": f"beework:{tenant_id}:{environment}" if tenant_id else None,
            "spend_logs_metadata": {"assistant_id": str(assistant_id), "source": source} if assistant_id else None,
        },
    }


class UsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(cls.engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

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
        raw = self.engine.raw_connection()
        raw.cursor().execute("PRAGMA foreign_keys=OFF")
        raw.close()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.client = TestClient(app)
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.gateway = FakeSpendLogs()
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=9)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=9)
            platform = User(email="platform@example.com", display_name="Platform", password_hash=hash_password(PASSWORD), is_platform_admin=True)
            admin = User(email="admin@example.com", display_name="Admin", password_hash=hash_password(PASSWORD))
            viewer = User(email="viewer@example.com", display_name="Viewer", password_hash=hash_password(PASSWORD))
            other_admin = User(email="other@example.com", display_name="OtherAdmin", password_hash=hash_password(PASSWORD))
            model = PlatformModel(
                display_name="Chat A", provider="private", model_type="chat", availability="all_plans",
                input_price_per_million="1", output_price_per_million="2",
                input_points_per_thousand="1", output_points_per_thousand="2", status="healthy",
                provider_model_id="chat-a", endpoint_url="https://models.example.test/v1",
                encrypted_api_key="encrypted", api_key_last_four="cret", region="ap_singapore",
            )
            db.add_all([tenant, other, platform, admin, viewer, other_admin, model])
            db.flush()
            org = Organization(tenant_id=tenant.id, name=tenant.name)
            other_org = Organization(tenant_id=other.id, name=other.name)
            db.add_all([org, other_org])
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=admin.id, organization_id=org.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=viewer.id, organization_id=org.id, role="viewer"),
                Membership(tenant_id=other.id, user_id=other_admin.id, organization_id=other_org.id, role="tenant_admin"),
            ])
            assistant = Assistant(tenant_id=tenant.id, organization_id=org.id, name="=cmd|' /C calc'!A0")
            db.add(assistant)
            db.flush()
            self.tenant_id, self.other_id, self.model_id = tenant.id, other.id, model.id
            self.assistant_id = assistant.id

    # -- helpers ----------------------------------------------------------------------------------------------

    def login(self, email: str) -> dict[str, str]:
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(self.tenant_id)}

    def pull(self, now: datetime | None = None) -> usage.PullResult:
        with self.session_factory() as db, self.gateway.client() as client:
            return usage.pull_usage(db, client, now=now or self.now)

    def total_points(self, tenant_id: UUID | None = None) -> Decimal:
        with self.session_factory() as db:
            return usage.month_points_decimal(db, tenant_id or self.tenant_id, self.now.date())

    def add_row(self, request_id: str, minutes_ago: int = 10, **kwargs) -> None:
        kwargs.setdefault("model_id", self.model_id)
        self.gateway.rows.append(
            spend_row(request_id, self.now - timedelta(minutes=minutes_ago), kwargs.pop("tenant_id", self.tenant_id), **kwargs)
        )

    # -- pulling ----------------------------------------------------------------------------------------------

    def test_points_follow_the_catalog_prices_and_pulling_twice_counts_once(self) -> None:
        self.add_row("r1", tokens_in=1000, tokens_out=500, assistant_id=self.assistant_id, source="debug")
        self.add_row("r2", tokens_in=2000, tokens_out=0, assistant_id=self.assistant_id, source="live")
        first = self.pull()
        self.assertEqual((first.counted, first.duplicates), (2, 0))
        self.assertEqual(self.total_points(), Decimal("4"))  # (1000/1000*1 + 500/1000*2) + 2000/1000*1
        second = self.pull()
        self.assertEqual((second.counted, second.duplicates), (0, 2))
        self.assertEqual(self.total_points(), Decimal("4"))
        with self.session_factory() as db:
            records = list(db.scalars(select(UsageRecord).order_by(UsageRecord.source)))
            self.assertEqual([(r.source, r.requests, r.tokens_in, r.tokens_out) for r in records], [("debug", 1, 1000, 500), ("live", 1, 2000, 0)])

    def test_rows_arriving_late_inside_the_reread_window_are_counted_once(self) -> None:
        self.add_row("early", minutes_ago=20)
        self.pull()
        self.add_row("late-but-older", minutes_ago=15)  # written to the gateway after the first pull
        self.pull()
        self.pull()
        self.assertEqual(self.total_points(), Decimal("4"))  # 2 × (1 + 1)
        cursor_start = datetime.strptime(self.gateway.requests[1]["start_date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        self.assertEqual(cursor_start, self.now - timedelta(minutes=20) - usage.REREAD_WINDOW)

    def test_pagination_is_followed(self) -> None:
        for index in range(5):
            self.add_row(f"p{index}", minutes_ago=30 - index)
        original = usage.PAGE_SIZE
        usage.PAGE_SIZE = 2
        try:
            result = self.pull()
        finally:
            usage.PAGE_SIZE = original
        self.assertEqual(result.counted, 5)
        self.assertEqual(self.total_points(), Decimal("10"))

    def test_attribution_comes_from_the_key_alias_and_failed_or_foreign_calls_are_skipped(self) -> None:
        self.add_row("mine", environment="development")
        self.add_row("theirs", tenant_id=self.other_id)
        self.add_row("failed", status="failure", tokens_in=0, tokens_out=0)
        self.add_row("master-key", tenant_id=None)
        result = self.pull()
        self.assertEqual((result.counted, result.unattributed), (2, 1))
        self.assertEqual(self.total_points(), Decimal("2"))
        self.assertEqual(self.total_points(self.other_id), Decimal("2"))
        with self.session_factory() as db:
            record = db.scalar(select(UsageRecord).where(UsageRecord.tenant_id == self.tenant_id))
            self.assertEqual((record.environment, record.assistant_id, record.source), ("development", UNKNOWN_ID, "unknown"))
            self.assertEqual(db.scalar(select(UsageSeen).where(UsageSeen.request_id == "failed")).request_id, "failed")

    def test_unknown_model_is_counted_with_zero_points_and_reported(self) -> None:
        self.add_row("ghost", model_id=uuid4())
        result = self.pull()
        self.assertEqual((result.counted, result.unpriced), (1, 1))
        self.assertEqual(self.total_points(), Decimal("0"))

    def test_gateway_error_is_recorded_and_raised(self) -> None:
        from beework.integrations.litellm import GatewayError

        def broken(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "boom"}})

        client = LiteLLMClient(base_url="http://litellm.test", master_key="k", transport=httpx.MockTransport(broken))
        from beework.config import settings

        settings.litellm_base_url, settings.litellm_master_key = "http://litellm.test", "k"
        original = usage.LiteLLMClient
        usage.LiteLLMClient = lambda: client
        try:
            with self.session_factory() as db, self.assertRaises(GatewayError):
                usage.sync_usage(db)
        finally:
            usage.LiteLLMClient = original
            settings.litellm_base_url = settings.litellm_master_key = ""
        with self.session_factory() as db:
            self.assertIn("boom", db.get(usage.UsageSyncState, usage.SYNC_NAME).last_error)

    # -- quotas -----------------------------------------------------------------------------------------------

    def test_monthly_points_quota_is_hard_and_counted_from_usage(self) -> None:
        with self.session_factory.begin() as db:
            subscription = entitlements.get_subscription(db, self.tenant_id)
            subscription.quotas = {**subscription.quotas, "monthly_points": 4}
        self.add_row("q1", tokens_in=1000, tokens_out=500)  # 2 points
        self.pull()
        with self.session_factory() as db:
            self.assertTrue(entitlements.check_quota(db, self.tenant_id, "monthly_points").allowed)
        self.add_row("q2", minutes_ago=9, tokens_in=1000, tokens_out=500)
        self.pull()
        with self.session_factory() as db:
            decision = entitlements.check_quota(db, self.tenant_id, "monthly_points")
            self.assertEqual((decision.allowed, decision.used, decision.limit), (False, 4, 4))
            with self.assertRaises(ApiError) as caught:
                entitlements.enforce_quota(db, self.tenant_id, "monthly_points")
            self.assertEqual(caught.exception.code, "plan.quota_exceeded")

    def test_conversation_quota_counts_real_conversations_not_console_tests(self) -> None:
        from beework.conversations import console_end_user, start_conversation

        with self.session_factory.begin() as db:
            subscription = entitlements.get_subscription(db, self.tenant_id)
            subscription.quotas = {**subscription.quotas, "monthly_conversations": 1}
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, self.assistant_id)
            end_user = console_end_user(db, self.tenant_id, db.scalar(select(User).where(User.email == "admin@example.com")))
            for _ in range(3):  # debugging in the console is not metered
                start_conversation(db, assistant=assistant, end_user=end_user, source="debug", environment="production", created_by=None)
            start_conversation(db, assistant=assistant, end_user=end_user, source="widget", environment="production", created_by=None)
        with self.session_factory() as db:
            self.assertEqual(usage.month_conversations(db, self.tenant_id), 1)
            with self.assertRaises(ApiError) as caught:
                start_conversation(db, assistant=db.get(Assistant, self.assistant_id), end_user=end_user, source="widget", environment="production", created_by=None)
            self.assertEqual(caught.exception.code, "plan.quota_exceeded")

    # -- budget -----------------------------------------------------------------------------------------------

    def set_budget(self, **values) -> None:
        with self.session_factory.begin() as db:
            budget = usage.get_budget(db, self.tenant_id)
            for key, value in values.items():
                setattr(budget, key, value)

    def alerts(self) -> list[int]:
        with self.session_factory() as db:
            return sorted(db.scalars(select(UsageAlert.threshold).where(UsageAlert.tenant_id == self.tenant_id)))

    def test_each_threshold_fires_once_per_month_and_notifies_admins_only(self) -> None:
        self.set_budget(monthly_points=10, thresholds=[50, 80, 100])
        self.add_row("b1", minutes_ago=30, tokens_in=2000, tokens_out=1500)  # 2 + 3 = 5 → 50%
        self.pull()
        self.assertEqual(self.alerts(), [50])
        self.pull()
        self.assertEqual(self.alerts(), [50])
        self.add_row("b2", minutes_ago=20, tokens_in=3000, tokens_out=0)  # 8 → 80%
        self.pull()
        self.assertEqual(self.alerts(), [50, 80])
        self.add_row("b3", minutes_ago=10, tokens_in=1000, tokens_out=0)  # 9: nothing new
        self.pull()
        self.assertEqual(self.alerts(), [50, 80])
        with self.session_factory() as db:
            notes = list(db.scalars(select(InAppNotification).where(InAppNotification.type == "usage.threshold")))
            self.assertEqual(len(notes), 2)
            admin_id = db.scalar(select(User.id).where(User.email == "admin@example.com"))
            self.assertEqual({note.recipient_id for note in notes}, {admin_id})
            self.assertEqual(notes[0].link, "/settings/usage")
            # Next month the same thresholds can fire again.
            fired = usage.evaluate_budget(db, self.tenant_id, now=self.now + timedelta(days=32))
            self.assertEqual(fired, [])  # new month starts at zero usage

    def test_a_tenant_that_never_opened_the_page_still_gets_the_default_alerts(self) -> None:
        with self.session_factory() as db:
            self.assertIsNone(db.get(usage.TenantBudget, self.tenant_id))  # no budget row yet
        self.add_row("big", tokens_in=40_000, tokens_out=0, minutes_ago=5)  # 40 points: tiny vs the 50,000 default
        with self.session_factory.begin() as db:
            db.add(UsageRecord(
                tenant_id=self.tenant_id, day=self.now.date(), environment="production", model_id=self.model_id,
                assistant_id=UNKNOWN_ID, source="live", requests=1, tokens_in=0, tokens_out=0, points=Decimal(41_000),
            ))
        self.pull()
        self.assertEqual(self.alerts(), [80])  # 41,040 / 50,000 = 82%

    def test_crossing_100_percent_pauses_non_production_until_budget_is_raised(self) -> None:
        self.set_budget(monthly_points=4, thresholds=[100], pause_non_production=True)
        self.add_row("pause", tokens_in=2000, tokens_out=1000)  # 4 points
        self.pull()
        with self.session_factory() as db:
            self.assertTrue(usage.usage_paused(db, self.tenant_id, now=self.now))
            usage.ensure_environment_active(db, self.tenant_id, "production")
            with self.assertRaises(ApiError) as caught:
                usage.ensure_environment_active(db, self.tenant_id, "development")
            self.assertEqual(caught.exception.code, "usage.environment_paused")
            jobs = list(db.scalars(select(Job).where(Job.kind == "gateway.sync_tenant_access")))
            self.assertEqual(len(jobs), 1)
            self.assertEqual(db.scalar(select(AuditEvent.action).where(AuditEvent.action == "usage.non_production_paused")), "usage.non_production_paused")
            tenant = db.get(Tenant, self.tenant_id)
            self.assertTrue(should_block(tenant, "staging", usage_is_paused=True))
            self.assertFalse(should_block(tenant, "production", usage_is_paused=True))
            self.assertFalse(should_block(tenant, "staging", usage_is_paused=False))
        # Raising the budget lifts the pause and queues the key unblock.
        headers = self.login("admin@example.com")
        response = self.client.put(
            "/api/v1/tenant/usage/budget", headers=headers,
            json={"monthly_points": 40, "thresholds": [100], "recipients": [], "pause_non_production": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["paused"])
        with self.session_factory() as db:
            self.assertFalse(usage.usage_paused(db, self.tenant_id, now=self.now))
            self.assertEqual(len(list(db.scalars(select(Job).where(Job.kind == "gateway.sync_tenant_access")))), 2)

    def test_pause_is_lifted_when_a_new_month_begins(self) -> None:
        self.set_budget(monthly_points=4, thresholds=[100], pause_non_production=True)
        self.add_row("pause", tokens_in=2000, tokens_out=1000)
        self.pull()
        with self.session_factory() as db:
            self.assertTrue(usage.usage_paused(db, self.tenant_id, now=self.now))
            self.assertFalse(usage.usage_paused(db, self.tenant_id, now=self.now + timedelta(days=32)))  # next month
            db.get(usage.TenantBudget, self.tenant_id).paused_month = "2000-01"  # left over from an earlier month
            db.commit()
            before = len(list(db.scalars(select(Job).where(Job.kind == "gateway.sync_tenant_access"))))
            usage.month_rollover_job(db, {})
            self.assertIsNone(db.get(usage.TenantBudget, self.tenant_id).paused_month)
            after = len(list(db.scalars(select(Job).where(Job.kind == "gateway.sync_tenant_access"))))
            self.assertEqual(after, before + 1)  # the key unblock is queued

    # -- API --------------------------------------------------------------------------------------------------

    def seed_records(self) -> None:
        today = datetime.now(UTC).date()
        other_assistants = [uuid4() for _ in range(4)]
        with self.session_factory.begin() as db:
            def add(day, assistant, points, tenant=self.tenant_id, env="production"):
                db.add(UsageRecord(
                    tenant_id=tenant, day=day, environment=env, model_id=self.model_id, assistant_id=assistant,
                    source="live", requests=1, tokens_in=1000, tokens_out=500, points=Decimal(points), spend_usd=Decimal("0.001"),
                ))
            add(today, self.assistant_id, 50)
            for index, assistant in enumerate(other_assistants):
                add(today, assistant, 10 + index)
            add(usage.add_months(usage.month_start(today), -1), self.assistant_id, 70)
            add(usage.add_months(usage.month_start(today), -7), self.assistant_id, 999)  # outside the 6-month trend
            add(today, uuid4(), 5000, tenant=self.other_id)

    def test_summary_has_trend_top_assistants_budget_and_nothing_from_other_tenants(self) -> None:
        self.seed_records()
        headers = self.login("admin@example.com")
        response = self.client.get("/api/v1/tenant/usage", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["points_used"], 50 + 10 + 11 + 12 + 13)
        self.assertEqual(body["points_quota"], 10_000_000)
        self.assertEqual(len(body["history"]), 6)
        self.assertEqual(body["history"][-1], {"month": usage.month_key(datetime.now(UTC)), "points": 96})
        self.assertEqual(body["history"][-2]["points"], 70)
        self.assertEqual([item["points"] for item in body["by_assistant"]], [50, 13, 12, 21])
        self.assertTrue(body["by_assistant"][-1]["other"])
        self.assertAlmostEqual(sum(item["share"] for item in body["by_assistant"]), 1.0)
        self.assertEqual(body["by_assistant"][0]["name"], "=cmd|' /C calc'!A0")
        self.assertEqual(body["budget"]["monthly_points"], 50_000)
        self.assertEqual(body["budget"]["thresholds"], [80, 100])
        self.assertGreaterEqual(body["projected_points"], body["points_used"])
        self.assertEqual(body["environment_count"], 3)
        self.assertEqual(body["seats_used"], 2)

    def test_budget_update_validates_and_is_admin_only_and_audited(self) -> None:
        payload = {"monthly_points": 1000, "thresholds": [50, 100], "recipients": ["Ops@Example.com", "ops@example.com"], "pause_non_production": False}
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.put("/api/v1/tenant/usage/budget", headers=viewer, json=payload).status_code, 403)
        self.client.post("/api/v1/auth/logout", headers=viewer)
        headers = self.login("admin@example.com")
        for bad in (
            {**payload, "monthly_points": 0},
            {**payload, "thresholds": [70]},
            {**payload, "recipients": ["not-an-email"]},
        ):
            self.assertEqual(self.client.put("/api/v1/tenant/usage/budget", headers=headers, json=bad).status_code, 422)
        response = self.client.put("/api/v1/tenant/usage/budget", headers=headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["recipients"], ["ops@example.com"])  # normalised and de-duplicated
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(AuditEvent.action).where(AuditEvent.action == "usage.budget_updated")), "usage.budget_updated")

    def test_export_csv_is_scoped_ranged_and_formula_safe(self) -> None:
        self.seed_records()
        headers = self.login("admin@example.com")
        response = self.client.get("/api/v1/tenant/usage/export", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        text = response.content.decode("utf-8-sig")
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "date,assistant,model,environment,source,requests,tokens_in,tokens_out,points")
        self.assertEqual(len(lines), 1 + 5)  # only this month's five rows, none of the other tenant's
        self.assertIn("'=cmd", text)  # the assistant name starts with "=" and is neutralised
        self.assertNotIn(",5000.00", text)
        too_long = self.client.get("/api/v1/tenant/usage/export?start=2020-01-01&end=2026-01-01", headers=headers)
        self.assertEqual(too_long.status_code, 422)
        backwards = self.client.get("/api/v1/tenant/usage/export?start=2026-02-01&end=2026-01-01", headers=headers)
        self.assertEqual(backwards.status_code, 422)

    def test_usage_endpoints_require_a_session_and_membership(self) -> None:
        self.assertEqual(self.client.get("/api/v1/tenant/usage", headers={"X-Tenant-ID": str(self.tenant_id)}).status_code, 401)
        headers = self.login("other@example.com")  # logs in, but is not a member of the Acme tenant
        self.assertEqual(self.client.get("/api/v1/tenant/usage", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/tenant/usage/export", headers=headers).status_code, 403)


if __name__ == "__main__":
    unittest.main()
