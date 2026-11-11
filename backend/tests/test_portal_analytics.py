"""T-147: AI portal usage analytics — only portal runs of this tenant, window boundaries, zeros instead of errors."""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

import test_portal_chat
from beework import entitlements, portal_analytics
from beework.models import Organization, Tenant
from beework.models.assistants import Assistant
from beework.models.conversations import Conversation, EndUser, Message
from beework.models.portal import PortalApp
from beework.models.runtime import Run


class PortalAnalyticsTests(test_portal_chat.PortalChatTests):
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now(UTC)

    # -- fixtures --------------------------------------------------------------------------------

    def employee_end_user(self, name: str, tenant=None) -> UUID:
        with self.session_factory.begin() as db:
            end_user = EndUser(tenant_id=tenant or self.ids["tenant"], channel_type="portal", external_id=name, name=name)
            db.add(end_user)
            db.flush()
            return end_user.id

    def add_run(self, employee: UUID, age: timedelta, *, status="succeeded", source="portal", tenant=None, app: str | None = None) -> UUID:
        tenant = tenant or self.ids["tenant"]
        with self.session_factory.begin() as db:
            conversation = Conversation(
                tenant_id=tenant, assistant_id=UUID(self.assistant_id), environment="production", source=source,
                end_user_id=employee, portal_app_id=UUID(app or self.app_id), title="t",
            )
            db.add(conversation)
            db.flush()
            run = Run(
                tenant_id=tenant, assistant_id=UUID(self.assistant_id), conversation_id=conversation.id, environment="production",
                source=source, status=status, config={}, input={}, created_at=self.now - age,
            )
            db.add(run)
            db.flush()
            return conversation.id

    def analytics(self, days=30, headers=None):
        return self.client.get("/api/v1/portal/analytics", params={"days": days}, headers=headers or self.login("admin"))

    def body(self, days=30) -> dict:
        response = self.analytics(days)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # -- no data ------------------------------------------------------------------------------------

    def test_a_tenant_without_usage_gets_zeros_and_empty_lists(self) -> None:
        body = self.body(7)
        self.assertEqual((body["active_employees"]["value"], body["calls"]["value"], body["calls"]["change"]), (0, 0, None))
        self.assertEqual((body["success_rate"]["value"], body["satisfaction"]["value"]), (None, None))  # no rate, not a made-up 0 or 100
        self.assertEqual((body["top_apps"], len(body["trend"]), sum(d["calls"] for d in body["trend"])), ([], 7, 0))
        self.assertEqual((body["seats"]["monthly_active_employees"], body["ratings_up"], body["ratings_down"]), (0, 0, 0))
        summary = self.client.get("/api/v1/portal/summary", headers=self.login("admin")).json()
        self.assertEqual((summary["monthly_active_employees"], summary["monthly_calls"]), (0, 0))

    # -- what is counted ------------------------------------------------------------------------------

    def test_only_portal_runs_of_this_tenant_are_counted(self) -> None:
        mine = self.employee_end_user("e1")
        self.add_run(mine, timedelta(days=1))
        self.add_run(mine, timedelta(days=1), source="api")
        self.add_run(mine, timedelta(days=1), source="debug")
        foreign = self.employee_end_user("f1", tenant=self.ids["other"])
        with self.session_factory.begin() as db:
            other_app = PortalApp(tenant_id=self.ids["other"], source_type="agent", source_id=None, name="x", status="listed")
            other_org = db.scalar(select(Organization.id).where(Organization.tenant_id == self.ids["other"]))
            other_assistant = Assistant(tenant_id=self.ids["other"], organization_id=other_org, name="o", description="", draft_config={})
            db.add_all([other_app, other_assistant])
            db.flush()
            conversation = Conversation(tenant_id=self.ids["other"], assistant_id=other_assistant.id, source="portal", end_user_id=foreign, portal_app_id=other_app.id)
            db.add(conversation)
            db.flush()
            db.add(Run(tenant_id=self.ids["other"], assistant_id=other_assistant.id, conversation_id=conversation.id, source="portal", status="succeeded", config={}, input={}, created_at=self.now - timedelta(days=1)))
        body = self.body(30)
        self.assertEqual((body["calls"]["value"], body["active_employees"]["value"]), (1, 1))

    def test_window_boundaries_and_the_comparison_window(self) -> None:
        employee = self.employee_end_user("e1")
        end = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
        self.now = end
        self.add_run(employee, timedelta(0))                                  # at `end`: excluded (the window is [start, end))
        self.add_run(employee, timedelta(days=7))                             # exactly `start`: included
        self.add_run(employee, timedelta(days=7, seconds=1))                  # just before start: previous window
        self.add_run(employee, timedelta(days=14))                            # exactly the previous start: included there
        self.add_run(employee, timedelta(days=14, seconds=1))                 # older than both: dropped
        with self.session_factory() as db:
            result = portal_analytics.compute(db, UUID(str(self.ids["tenant"])), 7, now=end)
        self.assertEqual((result.calls.value, result.calls.previous, result.calls.change), (1, 2, -0.5))
        self.assertEqual((result.active_employees.value, result.active_employees.previous), (1, 1))

    def test_success_rate_distinct_employees_top_apps_and_trend(self) -> None:
        a, b = self.employee_end_user("a"), self.employee_end_user("b")
        second_app = self.add_app("差旅助手", "行政")
        for _ in range(3):
            self.add_run(a, timedelta(hours=2))
        self.add_run(b, timedelta(hours=3), status="failed")
        self.add_run(b, timedelta(hours=3), status="running")      # in flight: not a success or a failure
        self.add_run(b, timedelta(hours=3), status="cancelled")    # the employee stopped it
        self.add_run(b, timedelta(days=2), app=second_app)
        body = self.body(7)
        self.assertEqual((body["calls"]["value"], body["active_employees"]["value"]), (7, 2))
        self.assertEqual(body["success_rate"]["value"], round(4 / 5, 4))  # 4 succeeded, 1 failed
        self.assertEqual([(t["name"], t["calls"], t["employees"]) for t in body["top_apps"]], [("员工助手", 6, 2), ("差旅助手", 1, 1)])
        self.assertEqual(body["top_apps"][1]["success_rate"], 1.0)
        today = datetime.now(UTC).date().isoformat()
        by_day = {d["day"]: d for d in body["trend"]}
        self.assertEqual(len(body["trend"]), 7)
        self.assertGreaterEqual(sum(d["calls"] for d in body["trend"]), 6)
        self.assertEqual(body["trend"][-1]["day"], today)
        self.assertEqual(sum(d["employees"] for d in by_day.values()) >= 2, True)

    def test_changes_compare_with_the_previous_window(self) -> None:
        employee = self.employee_end_user("e1")
        for _ in range(3):
            self.add_run(employee, timedelta(days=1))
        for _ in range(2):
            self.add_run(employee, timedelta(days=10))
        calls = self.body(7)["calls"]
        self.assertEqual((calls["value"], calls["previous"], calls["change"]), (3, 2, 0.5))  # 3 now vs 2 in the 7 days before
        calls = self.body(30)["calls"]
        self.assertEqual((calls["value"], calls["previous"], calls["change"]), (5, 0, None))  # nothing before: no change, not infinity

    # -- satisfaction -----------------------------------------------------------------------------------

    def test_employees_rate_answers_and_the_score_follows(self) -> None:
        headers = self.emp("emp1")
        first = self.send(headers, "one").json()["conversation_id"]
        second = self.send(self.emp("emp1"), "two").json()["conversation_id"]
        third = self.send(self.emp("emp1"), "three").json()["conversation_id"]
        def answer(conversation):
            with self.session_factory() as db:
                return str(db.scalar(select(Message.id).where(Message.conversation_id == UUID(conversation), Message.role == "assistant")))
        def rate(conversation, value, who="emp1"):
            return self.client.put(f"/api/v1/portal/me/conversations/{conversation}/messages/{answer(conversation)}/feedback", json={"value": value}, headers=self.emp(who))
        self.assertEqual(rate(first, "up").json()["feedback"], "up")
        self.assertEqual(rate(second, "up").status_code, 200)
        self.assertEqual(rate(third, "down").status_code, 200)
        body = self.body(7)
        self.assertEqual((body["ratings_up"], body["ratings_down"], body["satisfaction"]["value"]), (2, 1, round(2 / 3, 4)))
        self.assertEqual(rate(third, None).json()["feedback"], None)  # clearing a rating
        self.assertEqual(self.body(7)["satisfaction"]["value"], 1.0)
        # not someone else's answer, not a user message, not a made-up id
        self.assertEqual(rate(first, "up", who="emp2").status_code, 404)
        with self.session_factory() as db:
            user_message = str(db.scalar(select(Message.id).where(Message.conversation_id == UUID(first), Message.role == "user")))
        mine = self.emp("emp1")
        self.assertEqual(self.client.put(f"/api/v1/portal/me/conversations/{first}/messages/{user_message}/feedback", json={"value": "up"}, headers=mine).status_code, 404)
        self.assertEqual(self.client.put(f"/api/v1/portal/me/conversations/{first}/messages/{uuid4()}/feedback", json={"value": "up"}, headers=mine).status_code, 404)
        self.assertEqual(self.client.put(f"/api/v1/portal/me/conversations/{first}/messages/{answer(first)}/feedback", json={"value": "meh"}, headers=mine).status_code, 422)
        history = self.client.get(f"/api/v1/portal/me/conversations/{first}/messages", headers=mine).json()
        self.assertEqual([m["feedback"] for m in history["messages"]], [None, "up"])

    # -- seats ------------------------------------------------------------------------------------------------

    def test_seat_metrics_and_the_monthly_active_alert(self) -> None:
        with self.session_factory.begin() as db:
            tenant = db.get(Tenant, self.ids["tenant"])
            entitlements.change_subscription(db, tenant, tenant.plan, portal_mode="active", portal_seats=1, portal_overage="hard")
        first, second = self.employee_end_user("a"), self.employee_end_user("b")
        self.add_run(first, timedelta(minutes=5))
        self.add_run(second, timedelta(minutes=5))
        seats = self.body(7)["seats"]
        self.assertEqual((seats["mode"], seats["contract_seats"], seats["monthly_active_employees"], seats["over_active_limit"]), ("active", 1, 2, True))
        with self.session_factory.begin() as db:
            tenant = db.get(Tenant, self.ids["tenant"])
            entitlements.change_subscription(db, tenant, tenant.plan, portal_mode="named", portal_seats=5, portal_overage="hard")
        named = self.body(7)["seats"]
        self.assertEqual((named["mode"], named["over_active_limit"], named["assigned"], named["requested"]), ("named", False, 0, 0))

    # -- access -------------------------------------------------------------------------------------------------

    def test_validation_permissions_and_isolation(self) -> None:
        self.assertEqual(self.analytics(14).status_code, 422)
        self.assertEqual(self.analytics(0).status_code, 422)
        self.assertEqual(self.analytics(7, headers=self.emp()).status_code, 403)  # employees have no console permission
        self.assertEqual(self.analytics(7, headers=self.login("viewer")).status_code, 200)  # portal:view
        foreign = {**self.login("foreign", self.ids["other"]), "X-Tenant-ID": str(self.ids["tenant"])}
        self.assertEqual(self.analytics(7, headers=foreign).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/v1/portal/analytics").status_code, 401)

    def test_the_overview_cards_show_real_monthly_figures(self) -> None:
        a, b = self.employee_end_user("a"), self.employee_end_user("b")
        self.add_run(a, timedelta(minutes=1))
        self.add_run(a, timedelta(minutes=2))
        self.add_run(b, timedelta(minutes=3))
        summary = self.client.get("/api/v1/portal/summary", headers=self.login("admin")).json()
        self.assertEqual((summary["monthly_active_employees"], summary["monthly_calls"]), (2, 3))


for _name in [n for n in dir(test_portal_chat.PortalChatTests) if n.startswith("test_")]:
    setattr(PortalAnalyticsTests, _name, None)

if __name__ == "__main__":
    unittest.main()
