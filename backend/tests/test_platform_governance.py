"""T-050: platform PII rules, the security baseline, distribution scopes, retention limits and the two-person review."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework import governance
from beework.compliance import allowed_retention_choices, get_policy
from beework.config import settings
from beework.db import get_db
from beework.main import app
from beework.models import AuditEvent, Base, Membership, Organization, Tenant, User
from beework.models.governance import PlatformChangeRequest, PlatformPiiRule
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "platform admin safe password"
PII = "/api/v1/platform/pii-rules"
BASELINE = "/api/v1/platform/baseline-rules"
CHANGES = "/api/v1/platform/rule-changes"


class GovernanceTests(unittest.TestCase):
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
        with self.session_factory.begin() as db:
            tenants = {
                "enterprise_cn": Tenant(name="Ent CN", slug="ent-cn", plan="enterprise", region="cn_shanghai"),
                "standard_sg": Tenant(name="Std SG", slug="std-sg", plan="standard", region="ap_singapore"),
            }
            db.add_all(tenants.values())
            db.flush()
            users = {}
            for email, platform in (
                ("platform@example.com", True), ("reviewer@example.com", True), ("admin@example.com", False),
            ):
                users[email] = User(email=email, display_name=email.split("@")[0], password_hash=hash_password(PASSWORD), is_platform_admin=platform)
                db.add(users[email])
            db.flush()
            root = Organization(tenant_id=tenants["standard_sg"].id, name="Std")
            db.add(root)
            db.flush()
            db.add(Membership(tenant_id=tenants["standard_sg"].id, user_id=users["admin@example.com"].id, organization_id=root.id, role="tenant_admin"))
            self.tenant_ids = {key: tenant.id for key, tenant in tenants.items()}
        self.login("platform@example.com")

    # -- helpers ------------------------------------------------------------------

    def login(self, email: str) -> None:
        self.client.cookies.clear()
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)

    @property
    def csrf(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf")}

    def pii(self, **overrides):
        body = {"name": "手机号", "match_type": "regex", "pattern": r"1[3-9]\d{9}", "stage": "output", "action": "mask", **overrides}
        return self.client.post(PII, json=body, headers=self.csrf)

    def baseline(self, **overrides):
        body = {"name": "内容安全", "category": "content_safety", "stage": "output", "level": "recommended", "scope": "all",
                "status": "enabled", "action": "block", "match_type": "classifier", "pattern": "safety", **overrides}
        return self.client.post(BASELINE, json=body, headers=self.csrf)

    def decide(self, request_id: str, who: str, action: str = "approve", comment: str = ""):
        self.login(who)
        response = self.client.post(f"{CHANGES}/{request_id}/{action}", json={"comment": comment}, headers=self.csrf)
        self.login("platform@example.com")
        return response

    def tenant(self, key: str) -> Tenant:
        with self.session_factory() as db:
            return db.get(Tenant, self.tenant_ids[key])

    def audit_actions(self) -> list[str]:
        with self.session_factory() as db:
            return [
                a.action for a in db.scalars(select(AuditEvent).order_by(AuditEvent.created_at))
                if a.tenant_id is None and not a.action.startswith("auth.")
            ]

    # -- PII rules ----------------------------------------------------------------

    def test_create_edit_toggle_and_delete_a_plain_rule_apply_at_once(self) -> None:
        created = self.pii()
        self.assertEqual(created.status_code, 201, created.text)
        rule = created.json()
        self.assertEqual((rule["enforced"], rule["enabled"], rule["row_version"]), (False, True, 1))
        edited = self.client.put(f"{PII}/{rule['id']}", json={**rule, "enabled": False, "pattern": r"1\d{10}"}, headers=self.csrf)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual((edited.json()["enabled"], edited.json()["pattern"], edited.json()["row_version"]), (False, r"1\d{10}", 2))
        listing = self.client.get(PII).json()
        self.assertEqual((listing["total"], listing["enforced"], listing["enabled"]), (1, 0, 0))
        self.assertEqual(self.client.delete(f"{PII}/{rule['id']}", headers=self.csrf).status_code, 204)
        self.assertEqual(self.client.get(PII).json()["total"], 0)
        self.assertEqual(self.audit_actions(), ["platform_pii_rule.created", "platform_pii_rule.updated", "platform_pii_rule.deleted"])

    def test_validation_names_regexes_and_stale_versions(self) -> None:
        bad = self.pii(name="坏正则", pattern="([")
        self.assertEqual((bad.status_code, bad.json()["code"]), (422, "governance.invalid"))
        self.assertIn("正则表达式无效", bad.json()["detail"])
        self.assertEqual(self.pii(name="  ").status_code, 422)
        self.assertEqual(self.pii(name="空", pattern="").status_code, 422)
        self.assertEqual(self.pii(name="长" * 81).status_code, 422)
        self.assertEqual(self.pii(match_type="nonsense").status_code, 422)
        keywords = self.pii(name="关键词", match_type="keyword", pattern="机密， 绝密\n机密")
        self.assertEqual(keywords.json()["pattern"], "机密,绝密")
        block = self.pii(name="拦截", action="block", block_message="请勿发送")
        self.assertEqual(block.json()["block_message"], "请勿发送")
        mask = self.pii(name="脱敏带提示", action="mask", block_message="会被清空")
        self.assertEqual(mask.json()["block_message"], "")
        self.assertEqual(self.pii(name="手机号").status_code, 201)
        self.assertEqual((self.pii(name="手机号").status_code, self.pii(name="手机号").json()["code"]), (409, "governance.duplicate_name"))
        rule = self.pii(name="并发").json()
        self.client.put(f"{PII}/{rule['id']}", json={**rule, "name": "并发改名"}, headers=self.csrf)
        stale = self.client.put(f"{PII}/{rule['id']}", json={**rule, "name": "并发再改"}, headers=self.csrf)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        self.assertEqual(self.client.put(f"{PII}/{rule['id']}", json={**rule, "row_version": None}, headers=self.csrf).status_code, 422)

    def test_enforced_changes_wait_for_a_second_administrator(self) -> None:
        pending = self.pii(name="身份证号", pattern=r"\d{17}[\dXx]", stage="both", enforced=True, reason="合规要求")
        self.assertEqual(pending.status_code, 202, pending.text)
        request = pending.json()
        self.assertEqual((request["status"], request["kind"], request["operation"], request["reason"]), ("pending", "pii_rule", "create", "合规要求"))
        self.assertEqual(self.client.get(PII).json()["total"], 0)  # nothing took effect yet
        # The requester cannot review their own change.
        own = self.client.post(f"{CHANGES}/{request['id']}/approve", json={}, headers=self.csrf)
        self.assertEqual((own.status_code, own.json()["code"]), (403, "governance.self_review"))
        self.assertEqual(self.client.get(PII).json()["total"], 0)
        approved = self.decide(request["id"], "reviewer@example.com")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual((approved.json()["status"], approved.json()["reviewed_by_name"]), ("succeeded", "reviewer"))
        rule = self.client.get(PII).json()["items"][0]
        self.assertEqual((rule["name"], rule["enforced"], rule["updated_by"]), ("身份证号", True, "platform"))
        self.assertEqual(self.decide(request["id"], "reviewer@example.com").status_code, 409)  # already closed
        self.assertEqual(
            self.audit_actions(),
            ["platform_rule.change_requested", "platform_pii_rule.created", "platform_rule.change_approved"],
        )

    def test_switching_off_retiring_and_deleting_an_enforced_rule_need_review(self) -> None:
        with self.session_factory.begin() as db:
            db.add(PlatformPiiRule(name="银行卡号", match_type="regex", pattern=r"\b\d{16,19}\b", stage="both", action="mask", enforced=True, enabled=True))
        rule = self.client.get(PII).json()["items"][0]
        off = self.client.put(f"{PII}/{rule['id']}", json={**rule, "enabled": False}, headers=self.csrf)
        retire = self.client.put(f"{PII}/{rule['id']}", json={**rule, "enforced": False}, headers=self.csrf)
        self.assertEqual((off.status_code, retire.status_code), (202, 409))  # a second pending change for the rule is refused
        self.assertEqual(retire.json()["code"], "governance.pending_exists")
        self.assertEqual(self.client.get(PII).json()["items"][0]["enabled"], True)
        self.assertEqual(self.client.get(PII).json()["items"][0]["pending_request_id"], off.json()["id"])
        self.decide(off.json()["id"], "reviewer@example.com", "reject", "")  # a rejection needs a reason
        rejected = self.decide(off.json()["id"], "reviewer@example.com", "reject", "不同意停用")
        self.assertEqual((rejected.status_code, rejected.json()["status"]), (200, "rejected"))
        self.assertEqual(self.client.get(PII).json()["items"][0]["enabled"], True)
        rule = self.client.get(PII).json()["items"][0]
        deleted = self.client.delete(f"{PII}/{rule['id']}", params={"reason": "不再需要"}, headers=self.csrf)
        self.assertEqual((deleted.status_code, deleted.json()["operation"]), (202, "delete"))
        self.assertEqual(self.decide(deleted.json()["id"], "reviewer@example.com").status_code, 200)
        self.assertEqual(self.client.get(PII).json()["total"], 0)

    def test_content_edits_of_an_enforced_rule_and_enabling_do_not_need_review(self) -> None:
        with self.session_factory.begin() as db:
            db.add(PlatformPiiRule(name="身份证号", match_type="regex", pattern=r"\d{18}", stage="both", action="mask", enforced=True, enabled=False))
        rule = self.client.get(PII).json()["items"][0]
        edited = self.client.put(f"{PII}/{rule['id']}", json={**rule, "pattern": r"\d{17}[\dXx]", "enabled": True}, headers=self.csrf)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual((edited.json()["pattern"], edited.json()["enabled"], edited.json()["enforced"]), (r"\d{17}[\dXx]", True, True))
        promoted = self.pii(name="护照号", pattern=r"[EG]\d{8}")
        becomes = self.client.put(f"{PII}/{promoted.json()['id']}", json={**promoted.json(), "enforced": True}, headers=self.csrf)
        self.assertEqual(becomes.status_code, 202)  # becoming enforced is a new enforced rule

    def test_a_pending_change_expires_after_72_hours(self) -> None:
        request = self.pii(name="银行卡号", enforced=True).json()
        with self.session_factory.begin() as db:
            db.get(PlatformChangeRequest, UUID(request["id"])).expires_at = datetime.now(UTC) - timedelta(minutes=1)
        late = self.decide(request["id"], "reviewer@example.com")
        self.assertEqual((late.status_code, late.json()["code"]), (409, "governance.request_closed"))
        self.assertEqual(self.client.get(f"{CHANGES}?status=expired").json()[0]["status"], "expired")
        self.assertEqual(self.client.get(PII).json()["total"], 0)
        self.assertIn("platform_rule.change_expired", self.audit_actions())

    def test_a_stale_request_cannot_overwrite_a_newer_edit(self) -> None:
        with self.session_factory.begin() as db:
            db.add(PlatformPiiRule(name="银行卡号", match_type="regex", pattern="x", stage="both", action="mask", enforced=True, enabled=True))
        rule = self.client.get(PII).json()["items"][0]
        pending = self.client.put(f"{PII}/{rule['id']}", json={**rule, "enabled": False}, headers=self.csrf).json()
        with self.session_factory.begin() as db:
            row = db.get(PlatformPiiRule, UUID(rule["id"]))
            row.pattern = "changed"
            row.row_version += 1
        stale = self.decide(pending["id"], "reviewer@example.com")
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "governance.stale_request"))
        self.assertEqual(self.client.get(PII).json()["items"][0]["enabled"], True)

    def test_review_can_be_switched_off(self) -> None:
        with mock.patch.object(settings, "platform_rule_review", False):
            created = self.pii(name="身份证号", enforced=True)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(self.client.get(PII).json()["enforced"], 1)

    # -- Baseline -----------------------------------------------------------------

    def test_baseline_crud_filters_and_validation(self) -> None:
        self.assertEqual(self.baseline().status_code, 201)
        self.assertEqual(self.baseline(name="越狱检测", category="prompt_attack", stage="input", status="draft", match_type="keyword", pattern="忽略以上指令").status_code, 201)
        listing = self.client.get(BASELINE).json()
        self.assertEqual((listing["total"], listing["counts"]), (2, {"enabled": 1, "draft": 1, "all": 2}))
        self.assertEqual([r["name"] for r in self.client.get(BASELINE, params={"status": "draft"}).json()["items"]], ["越狱检测"])
        self.assertEqual([r["name"] for r in self.client.get(BASELINE, params={"q": "内容"}).json()["items"]], ["内容安全"])
        rule = listing["items"][0]
        edited = self.client.put(f"{BASELINE}/{rule['id']}", json={**rule, "status": "draft"}, headers=self.csrf)
        self.assertEqual((edited.status_code, edited.json()["status"]), (200, "draft"))
        self.assertEqual(self.client.delete(f"{BASELINE}/{rule['id']}", headers=self.csrf).status_code, 204)
        self.assertEqual(self.baseline(name="x", category="data_retention", stage="both", pattern="180").status_code, 422)  # retention needs the data stage
        self.assertEqual(self.baseline(name="y", category="content_safety", stage="data").status_code, 422)
        self.assertEqual(self.baseline(name="z", category="data_retention", stage="data", pattern="abc").status_code, 422)
        self.assertEqual(self.baseline(name="w", category="prompt_attack", match_type="regex", pattern="([").status_code, 422)
        retention = self.baseline(name="保留", category="data_retention", stage="data", pattern=" 90 ", action="block")
        self.assertEqual((retention.json()["pattern"], retention.json()["action"], retention.json()["match_type"]), ("90", "flag", "keyword"))

    def test_pii_baseline_rows_are_rule_set_references(self) -> None:
        first = self.pii(name="身份证号").json()
        self.pii(name="银行卡号")
        ref = self.baseline(name="PII 规则集", category="pii", stage="both", pattern="ignored", ruleset={"kind": "rules", "rule_ids": [first["id"]]})
        self.assertEqual(ref.status_code, 201, ref.text)
        self.assertEqual((ref.json()["pattern"], ref.json()["ruleset"]), ("", {"kind": "rules", "rule_ids": [first["id"]]}))
        missing = self.baseline(name="坏引用", category="pii", ruleset={"kind": "rules", "rule_ids": ["00000000-0000-4000-8000-000000000001"]})
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(self.baseline(name="空引用", category="pii", ruleset={"kind": "rules", "rule_ids": []}).status_code, 422)
        # Deleting a platform rule drops it from rule sets that listed it.
        self.client.delete(f"{PII}/{first['id']}", headers=self.csrf)
        refreshed = next(r for r in self.client.get(BASELINE).json()["items"] if r["name"] == "PII 规则集")
        self.assertEqual(refreshed["ruleset"]["rule_ids"], [])
        with self.session_factory() as db:
            self.assertEqual([r.name for r in governance.resolve_ruleset(db, {"kind": "all"})], ["银行卡号"])
            self.assertEqual(governance.resolve_ruleset(db, {"kind": "enforced"}), [])

    def test_enforced_baseline_changes_need_review(self) -> None:
        pending = self.baseline(name="强制内容安全", level="enforced")
        self.assertEqual(pending.status_code, 202, pending.text)
        self.assertEqual(self.client.get(BASELINE).json()["total"], 0)
        self.assertEqual(self.decide(pending.json()["id"], "reviewer@example.com").status_code, 200)
        rule = self.client.get(BASELINE).json()["items"][0]
        self.assertEqual(rule["level"], "enforced")
        draft = self.client.put(f"{BASELINE}/{rule['id']}", json={**rule, "status": "draft"}, headers=self.csrf)
        self.assertEqual(draft.status_code, 202)  # taking an enforced rule out of service
        relaxed = self.client.put(f"{BASELINE}/{rule['id']}", json={**rule, "level": "recommended"}, headers=self.csrf)
        self.assertEqual(relaxed.status_code, 409)
        self.decide(draft.json()["id"], "reviewer@example.com", "reject", "no")
        # Plain recommended rules never need review.
        self.assertEqual(self.baseline(name="推荐规则", level="recommended").status_code, 201)

    # -- Distribution (PLT-141) and retention (PLT-142) -----------------------------

    def test_distribution_scopes_and_drafts(self) -> None:
        for name, scope, status in (
            ("全部", "all", "enabled"), ("企业版", "enterprise", "enabled"), ("中国区", "region_cn", "enabled"),
            ("金融", "industry_finance", "enabled"), ("草稿", "all", "draft"),
        ):
            self.assertEqual(self.baseline(name=name, scope=scope, status=status).status_code, 201)

        def names(key: str, industry: str | None = None) -> list[str]:
            with self.session_factory() as db:
                resolver = lambda _db, tenant: industry  # noqa: E731
                governance.INDUSTRY_RESOLVERS.append(resolver)
                try:
                    return [r.name for r in governance.baseline_rules_for_tenant(db, db.get(Tenant, self.tenant_ids[key]))]
                finally:
                    governance.INDUSTRY_RESOLVERS.remove(resolver)

        self.assertEqual(names("enterprise_cn"), ["全部", "企业版", "中国区"])
        self.assertEqual(names("standard_sg"), ["全部"])
        self.assertEqual(names("standard_sg", "banking"), ["全部", "金融"])
        self.assertEqual(names("standard_sg", "retail"), ["全部"])
        self.assertEqual(names("enterprise_cn", "insurance"), ["全部", "企业版", "中国区", "金融"])  # the draft is never distributed

    def test_retention_rules_cap_or_default_the_tenant_policy(self) -> None:
        self.baseline(name="强制保留", category="data_retention", stage="data", level="enforced", scope="region_cn", pattern="90", action="flag", match_type="keyword")
        self.baseline(name="推荐保留", category="data_retention", stage="data", level="recommended", scope="enterprise", pattern="30")
        # Enforced rules are two-person reviewed.
        self.assertEqual(self.client.get(BASELINE).json()["total"], 1)
        pending = self.client.get(CHANGES, params={"status": "pending"}).json()
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.decide(pending[0]["id"], "reviewer@example.com").status_code, 200)
        with self.session_factory() as db:
            cn = db.get(Tenant, self.tenant_ids["enterprise_cn"])
            sg = db.get(Tenant, self.tenant_ids["standard_sg"])
            self.assertEqual(governance.retention_limit_for_tenant(db, cn), governance.RetentionLimit(90, 30))
            self.assertEqual(governance.retention_limit_for_tenant(db, sg), governance.RetentionLimit(None, None))
            self.assertEqual(governance.retention_limit_for_tenant(db, None), governance.RetentionLimit(None, None))
            # The tenant's data & privacy policy honours the cap and the recommended default (T-026 integration).
            policy = get_policy(db, cn.id)
            self.assertEqual((policy.max_retention_days, policy.retention_days), (90, 30))
            self.assertEqual(allowed_retention_choices(policy.max_retention_days), [30, 90])

    def test_the_tenant_privacy_page_rejects_a_value_above_the_baseline_cap(self) -> None:
        with self.session_factory.begin() as db:  # an uncapped plan, so the baseline rule is the only limit
            db.get(Tenant, self.tenant_ids["standard_sg"]).plan = "enterprise"
        with mock.patch.object(settings, "platform_rule_review", False):
            self.baseline(name="强制保留", category="data_retention", stage="data", level="enforced", scope="all", pattern="90")
        self.login("admin@example.com")
        me = self.client.get("/api/v1/auth/me").json()["memberships"][0]
        headers = {**self.csrf, "X-Tenant-ID": me["id"]}
        current = self.client.get("/api/v1/tenant/compliance", headers=headers).json()
        self.assertEqual(current["max_retention_days"], 90)
        self.assertEqual(current["retention_choices"], [30, 90])
        body = {"retention_days": 180, "redact_pii": True, "allow_export": True, "row_version": current["row_version"]}
        rejected = self.client.put("/api/v1/tenant/compliance", json=body, headers=headers)
        self.assertEqual((rejected.status_code, rejected.json()["code"]), (422, "compliance.retention_invalid"))
        self.assertEqual(self.client.put("/api/v1/tenant/compliance", json={**body, "retention_days": 90}, headers=headers).status_code, 200)

    # -- Access -------------------------------------------------------------------

    def test_only_platform_admins_can_use_these_endpoints(self) -> None:
        rule = self.pii().json()
        self.login("admin@example.com")  # a tenant admin
        for method, path in (("get", PII), ("get", BASELINE), ("get", CHANGES)):
            self.assertEqual(getattr(self.client, method)(path).status_code, 403, path)
        self.assertEqual(self.client.post(PII, json={"name": "x", "pattern": "x"}, headers=self.csrf).status_code, 403)
        self.assertEqual(self.client.delete(f"{PII}/{rule['id']}", headers=self.csrf).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(PII).status_code, 401)
        self.login("platform@example.com")
        self.assertEqual(self.client.post(PII, json={"name": "x", "pattern": "x"}).status_code, 403)  # no CSRF token
        self.assertEqual(self.client.delete(f"{PII}/00000000-0000-4000-8000-000000000001", headers=self.csrf).status_code, 404)


if __name__ == "__main__":
    unittest.main()
