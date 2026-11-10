"""T-053: the tenant glossary — inherited industries, overrides, hiding, own terms, import, approvals, isolation."""

import unittest
from uuid import UUID

from sqlalchemy import select

import test_versions
from beework.approvals import ensure_policies
from beework.models import AuditEvent, Membership, Organization, User
from beework.models.glossary import Industry, IndustryGlossary, PlatformTerm
from beework.models.tenant_glossary import TenantGlossarySettings, TenantTerm
from beework.security import hash_password

URL = "/api/v1/tenant/glossary"


class TenantGlossaryTests(test_versions.VersionTests):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["tenant"])["rule"].enabled = False  # tests switch the approval on when needed
            db.add_all([Industry(code="retail", name_zh="零售", name_en="Retail"), Industry(code="insurance", name_zh="保险", name_en="Insurance")])
            retail = IndustryGlossary(name="零售电商", description="", industry_code="retail", sort_order=0)
            insurance = IndustryGlossary(name="保险", description="", industry_code="insurance", sort_order=1)
            db.add_all([retail, insurance])
            db.flush()
            for glossary, rows in (
                (retail, (("SKU", ["库存单位"], "retail sku"), ("GMV", [], "gmv"))),
                (insurance, (("理赔", ["赔付"], "claim"), ("sku", [], "insurance sku"))),
            ):
                for term, aliases, definition in rows:
                    db.add(PlatformTerm(glossary_id=glossary.id, term=term, term_key=term.lower(), aliases=aliases, definition=definition))
            auditor = User(email="auditor@example.com", display_name="Auditor", password_hash=hash_password(test_versions.PASSWORD))
            db.add(auditor)
            db.flush()
            root = db.scalar(select(Organization.id).where(Organization.tenant_id == self.ids["tenant"]))
            db.add(Membership(tenant_id=self.ids["tenant"], user_id=auditor.id, organization_id=root, role="auditor"))
            db.flush()
            self.glossary = {"retail": str(retail.id), "insurance": str(insurance.id)}
            self.platform = {t.term: str(t.id) for t in db.scalars(select(PlatformTerm))}  # "sku" of insurance overwrites "SKU" in the map

    # -- helpers ------------------------------------------------------------------

    def state(self, headers=None, **params):
        return self.client.get(URL, params=params, headers=headers or self.admin)

    def items(self, **params) -> dict[str, dict]:
        return {i["term"]: i for i in self.state(**params).json()["items"]}

    def select(self, *names: str, headers=None, version: int | None = None):
        current = self.state(headers).json()["settings_version"] if version is None else version
        return self.client.put(
            f"{URL}/settings", json={"glossary_ids": [self.glossary[n] for n in names], "row_version": current}, headers=headers or self.admin
        )

    def body(self, term: str = "会员号", **overrides) -> dict:
        return {"term": term, "aliases": ["member"], "definition": "x", **overrides}

    def create(self, headers=None, **overrides):
        return self.client.post(f"{URL}/terms", json=self.body(**overrides), headers=headers or self.admin)

    def put(self, key: str, headers=None, **overrides):
        return self.client.put(f"{URL}/terms/{key}", json=self.body(**overrides), headers=headers or self.admin)

    def rows(self) -> list[TenantTerm]:
        with self.session_factory() as db:
            return list(db.scalars(select(TenantTerm)))

    def audit_actions(self) -> list[str]:
        with self.session_factory() as db:
            return [a.action for a in db.scalars(select(AuditEvent).order_by(AuditEvent.created_at)) if a.action.startswith("tenant_term") or a.action.startswith("tenant_glossary")]

    def require_approval(self) -> None:
        with self.session_factory.begin() as db:
            policy = ensure_policies(db, self.ids["tenant"])["rule"]
            policy.enabled = True
            policy.approver_role = "tenant_admin"

    def retail_key(self, term: str) -> str:
        with self.session_factory() as db:
            return str(db.scalar(select(PlatformTerm.id).where(PlatformTerm.glossary_id == UUID(self.glossary["retail"]), PlatformTerm.term == term)))

    # -- inheriting industries ----------------------------------------------------

    def test_a_tenant_starts_without_inherited_glossaries_and_picks_industries_in_order(self) -> None:
        state = self.state().json()
        self.assertEqual((state["items"], state["chain"]["industries"], state["settings_version"]), ([], [], 0))
        self.assertEqual([(g["name"], g["term_count"], g["selected"]) for g in state["glossaries"]], [("零售电商", 2, False), ("保险", 2, False)])
        saved = self.select("insurance", "retail")
        self.assertEqual((saved.status_code, saved.json()["kind"]), (200, "settings"))
        state = self.state().json()
        self.assertEqual(state["chain"]["industries"], ["保险", "零售"])
        self.assertEqual({g["name"]: g["position"] for g in state["glossaries"]}, {"零售电商": 2, "保险": 1})
        self.assertEqual(sorted(i["term"] for i in state["items"]), ["GMV", "sku", "理赔"])  # "SKU" of retail is the duplicate here
        self.assertEqual(state["chain"]["inherited"], 3)

    def test_a_standard_term_in_two_selected_glossaries_comes_from_the_first_in_the_tenants_order(self) -> None:
        self.select("retail", "insurance")
        self.assertEqual(self.items()["SKU"]["definition"], "retail sku")
        self.select("insurance", "retail")
        self.assertEqual(self.items()["sku"]["definition"], "insurance sku")
        self.assertNotIn("SKU", self.items())

    def test_selection_is_validated_and_versioned(self) -> None:
        self.assertEqual(self.client.put(f"{URL}/settings", json={"glossary_ids": [str(UUID(int=1))], "row_version": 0}, headers=self.admin).status_code, 422)
        duplicate = [self.glossary["retail"], self.glossary["retail"]]
        self.assertEqual(self.client.put(f"{URL}/settings", json={"glossary_ids": duplicate, "row_version": 0}, headers=self.admin).status_code, 422)
        self.select("retail")
        stale = self.select("insurance", version=0)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        self.assertEqual(self.select().status_code, 200)  # deselecting everything is allowed
        self.assertEqual(self.items(), {})

    # -- overrides ----------------------------------------------------------------

    def test_editing_an_inherited_term_creates_an_override_and_leaves_the_platform_untouched(self) -> None:
        self.select("retail")
        key = self.items()["SKU"]["key"]
        response = self.put(key, term="SKU", aliases=["商品编码"], definition="tenant def")
        self.assertEqual((response.status_code, response.json()["kind"]), (200, "override_created"), response.text)
        item = self.items()["SKU"]
        self.assertEqual((item["label"], item["definition"], item["aliases"], item["restorable"], item["key"]), ("tenant_modified", "tenant def", ["商品编码"], True, key))
        with self.session_factory() as db:
            platform = db.get(PlatformTerm, UUID(key))
            self.assertEqual((platform.definition, platform.row_version), ("retail sku", 1))
        self.assertEqual(self.put(key, term="SKU", definition="again").json()["kind"], "updated")  # the same override
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.audit_actions(), ["tenant_glossary_settings.updated", "tenant_term.overridden", "tenant_term.updated"])

    def test_platform_edits_reach_the_tenant_unless_the_term_is_overridden(self) -> None:
        self.select("retail")
        self.put(self.items()["SKU"]["key"], term="SKU", definition="mine")
        with self.session_factory.begin() as db:
            for term in db.scalars(select(PlatformTerm).where(PlatformTerm.glossary_id == UUID(self.glossary["retail"]))):
                term.definition = f"platform changed {term.term}"
        items = self.items()
        self.assertEqual((items["SKU"]["definition"], items["GMV"]["definition"]), ("mine", "platform changed GMV"))

    def test_restore_deletes_the_override(self) -> None:
        self.select("retail")
        key = self.items()["SKU"]["key"]
        self.put(key, term="SKU", definition="mine")
        restored = self.client.post(f"{URL}/terms/{key}/restore", headers=self.admin)
        self.assertEqual((restored.status_code, restored.json()["kind"]), (200, "restored"))
        self.assertEqual((self.items()["SKU"]["label"], self.rows()), ("industry_inherited", []))
        again = self.client.post(f"{URL}/terms/{key}/restore", headers=self.admin)
        self.assertEqual((again.status_code, again.json()["code"]), (409, "tenant_glossary.nothing_to_restore"))

    # -- hiding -------------------------------------------------------------------

    def test_deleting_an_inherited_term_hides_it_and_restore_all_brings_it_back(self) -> None:
        self.select("retail")
        gmv = self.items()["GMV"]["key"]
        hidden = self.client.delete(f"{URL}/terms/{gmv}", headers=self.admin)
        self.assertEqual((hidden.status_code, hidden.json()["kind"]), (200, "hidden"))
        state = self.state().json()
        self.assertEqual(([i["term"] for i in state["items"]], state["chain"]["hidden"]), (["SKU"], 1))
        with self.session_factory() as db:
            self.assertIsNotNone(db.get(PlatformTerm, UUID(gmv)))  # the platform term is untouched
        self.assertEqual(self.client.delete(f"{URL}/terms/{self.items()['SKU']['key']}", headers=self.admin).json()["kind"], "hidden")
        restored = self.client.post(f"{URL}/restore-hidden", headers=self.admin)
        self.assertEqual((restored.status_code, restored.json()["kind"]), (200, "restored_hidden"))
        self.assertEqual(sorted(self.items()), ["GMV", "SKU"])
        self.assertEqual(self.client.post(f"{URL}/restore-hidden", headers=self.admin).status_code, 409)  # nothing left to restore
        self.assertEqual(self.rows(), [])

    def test_deleting_an_override_hides_the_term_and_a_hidden_name_can_be_reused(self) -> None:
        self.select("retail")
        key = self.items()["SKU"]["key"]
        self.put(key, term="SKU", definition="mine")
        self.client.delete(f"{URL}/terms/{key}", headers=self.admin)
        self.assertNotIn("SKU", self.items())
        self.assertEqual(len(self.rows()), 1)  # the override became a hidden marker
        mine = self.create(term="sku", aliases=[], definition="my own")  # free to define it again
        self.assertEqual(mine.status_code, 201, mine.text)
        self.assertEqual(self.items()["sku"]["label"], "tenant")

    # -- own terms ----------------------------------------------------------------

    def test_own_terms_can_be_created_edited_and_deleted(self) -> None:
        created = self.create()
        self.assertEqual((created.status_code, created.json()["kind"]), (201, "created"), created.text)
        item = self.items()["会员号"]
        self.assertEqual((item["label"], item["restorable"], item["aliases"]), ("tenant", False, ["member"]))
        self.assertEqual(self.put(item["key"], term="会员编号", aliases=[], definition="y").json()["kind"], "updated")
        self.assertEqual(self.items()["会员编号"]["definition"], "y")
        self.assertEqual(self.client.delete(f"{URL}/terms/{item['key']}", headers=self.admin).json()["kind"], "deleted")
        self.assertEqual(self.rows(), [])

    def test_validation_and_uniqueness(self) -> None:
        self.select("retail")
        for bad in ({"term": " "}, {"term": "x" * 201}, {"aliases": [f"a{i}" for i in range(21)]}, {"definition": "d" * 1001}, {"aliases": ["y" * 101]}):
            response = self.create(**bad)
            self.assertEqual((response.status_code, response.json()["code"]), (422, "glossary.invalid"), bad)
        self.assertEqual(self.create().status_code, 201)
        for clash in ("会员号", "  会员号 ", "sku", "GMV"):  # an own term, and inherited ones (case-insensitive)
            response = self.create(term=clash)
            self.assertEqual((response.status_code, response.json()["code"]), (409, "tenant_glossary.duplicate_term"), clash)
        own = self.items()["会员号"]["key"]
        self.assertEqual(self.put(own, term="GMV").status_code, 409)  # renaming onto an inherited term
        self.assertEqual(self.put(own, term="会员号", definition="same name is fine").status_code, 200)
        self.assertEqual(self.client.delete(f"{URL}/terms/{UUID(int=1)}", headers=self.admin).status_code, 404)
        self.assertEqual(self.put(str(UUID(int=1))).status_code, 404)
        self.assertEqual(self.client.post(f"{URL}/terms/{own}/restore", headers=self.admin).status_code, 409)

    def test_only_inherited_glossaries_can_be_overridden(self) -> None:
        self.select("retail")
        insurance_term = self.platform["理赔"]  # a term of a glossary the tenant did not select
        self.assertEqual(self.put(insurance_term, term="理赔").status_code, 404)
        self.assertEqual(self.client.delete(f"{URL}/terms/{insurance_term}", headers=self.admin).status_code, 404)

    def test_the_scope_filter_and_search(self) -> None:
        self.select("retail")
        self.create()
        self.put(self.items()["SKU"]["key"], term="SKU", definition="mine")
        self.assertEqual(sorted(self.items(scope="tenant")), ["SKU", "会员号"])
        self.assertEqual(sorted(self.items(q="库存")), [])  # the override replaced the aliases of SKU with "member"
        self.assertEqual(sorted(self.items(q="member")), ["SKU", "会员号"])
        self.assertEqual(sorted(self.items(q="gm")), ["GMV"])
        self.assertEqual(self.state(scope="bogus").status_code, 422)

    # -- platform changes flow down -------------------------------------------------

    def test_deleting_a_platform_term_turns_overrides_into_own_terms_and_drops_markers(self) -> None:
        from beework import glossary as service

        self.select("retail")
        sku, gmv = self.items()["SKU"]["key"], self.items()["GMV"]["key"]
        self.put(sku, term="SKU", definition="mine")
        self.client.delete(f"{URL}/terms/{gmv}", headers=self.admin)
        with self.session_factory.begin() as db:
            for term_id in (UUID(sku), UUID(gmv)):
                for hook in service.PLATFORM_TERM_DELETE_HOOKS:
                    hook(db, term_id)
                db.delete(db.get(PlatformTerm, term_id))
        rows = self.rows()
        self.assertEqual([(r.term, r.from_id, r.hidden) for r in rows], [("SKU", None, False)])  # the override is now the tenant's own term
        self.assertEqual({i["term"]: i["label"] for i in self.state().json()["items"]}, {"SKU": "tenant"})

    def test_deleting_a_glossary_removes_it_from_the_selection(self) -> None:
        self.select("retail", "insurance")
        from beework import glossary as service

        with self.session_factory.begin() as db:
            for hook in service.PLATFORM_GLOSSARY_DELETE_HOOKS:
                hook(db, UUID(self.glossary["retail"]))
        with self.session_factory() as db:
            row = db.get(TenantGlossarySettings, self.ids["tenant"])
            self.assertEqual(row.glossary_ids, [self.glossary["insurance"]])
            self.assertEqual(service.inheriting_tenant_count(db, db.get(IndustryGlossary, UUID(self.glossary["insurance"]))), 1)

    # -- import (SET-063) -----------------------------------------------------------

    def import_text(self, text: str | bytes, **params):
        data = text.encode() if isinstance(text, str) else text
        return self.client.post(f"{URL}/terms/import", params=params, content=data, headers={**self.admin, "Content-Type": "text/plain"})

    def test_import_previews_then_creates_tenant_terms_and_reports_every_bad_row(self) -> None:
        self.select("retail")
        text = "术语,别名,定义\n会员号,member;vip,会员编号\nSKU,商品编码,覆盖\n,空术语,x\n工号,,员工工号\n"
        preview = self.import_text(text, dry_run="true")
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertEqual((body["outcome"], body["recognized"], body["duplicates"]), ("preview", 3, 1))
        self.assertEqual([(r["term"], r["exists"]) for r in body["preview"]], [("会员号", False), ("SKU", True), ("工号", False)])
        self.assertEqual([(e["line"], e["message"]) for e in body["errors"]], [(4, "术语不能为空")])
        self.assertEqual(self.rows(), [])  # a preview changes nothing
        skipped = self.import_text(text, on_duplicate="skip").json()
        self.assertEqual(skipped["outcome"], "applied")
        items = self.items()
        self.assertEqual((sorted(items), items["SKU"]["label"], items["会员号"]["aliases"]), (["GMV", "SKU", "会员号", "工号"], "industry_inherited", ["member", "vip"]))
        self.assertEqual(self.import_text(text, on_duplicate="overwrite").json()["outcome"], "applied")
        items = self.items()
        self.assertEqual((items["SKU"]["label"], items["SKU"]["definition"], items["SKU"]["aliases"]), ("tenant_modified", "覆盖", ["商品编码"]))
        self.assertEqual(len(self.rows()), 3)  # two own terms and one override
        self.assertIn("tenant_term.imported", self.audit_actions())

    def test_import_encodings_limits_and_empty_input(self) -> None:
        self.assertEqual(self.import_text("术语,别名\n会员号,member".encode("gb18030"), dry_run="true").json()["recognized"], 1)
        self.assertEqual(self.import_text(b"\xef\xbb\xbf" + "工号,x".encode(), dry_run="true").json()["recognized"], 1)
        self.assertEqual(self.import_text("  \n").status_code, 422)
        self.assertEqual(self.import_text(b"\xff\xfe\x00\x80" * 50).status_code, 422)
        self.assertEqual(self.import_text(b"a,b\n" + b"x" * 1_100_000).status_code, 413)
        only_errors = self.import_text(",坏行\n")
        self.assertEqual((only_errors.json()["recognized"], len(only_errors.json()["errors"]), only_errors.json()["outcome"]), (0, 1, "preview"))

    # -- approval (same policy as the PII rules) ------------------------------------

    def test_changes_wait_for_approval_when_the_policy_is_on(self) -> None:
        self.select("retail")
        self.require_approval()
        key = self.items()["SKU"]["key"]
        pending = self.put(key, term="SKU", definition="mine")
        self.assertEqual((pending.status_code, pending.json()["outcome"], pending.json()["kind"]), (202, "pending", "override_created"))
        self.assertEqual(self.rows(), [])
        approval = self.client.get(f"/api/v1/approvals/{pending.json()['approval_id']}", headers=self.admin).json()
        self.assertEqual((approval["type"], approval["title"]), ("rule_change", "租户术语变更 · 修改 SKU"))
        self.assertTrue(any(d["field"] == "definition" and d["to"] == "mine" for d in approval["change_diff"]))
        self.assertEqual(self.items()["SKU"]["pending_approval_id"], pending.json()["approval_id"])
        again = self.put(key, term="SKU", definition="other")
        self.assertEqual((again.status_code, again.json()["code"]), (409, "approval.pending_exists"))
        self.assertEqual(self.decide(pending.json()["approval_id"]).status_code, 200)
        self.assertEqual(self.drain(), 1)
        self.assertEqual((self.items()["SKU"]["label"], self.items()["SKU"]["definition"]), ("tenant_modified", "mine"))

    def test_creations_imports_and_the_selection_do_not_block_each_other(self) -> None:
        self.require_approval()
        statuses = [
            self.create().status_code, self.create(term="工号").status_code, self.select("retail").status_code,
            self.import_text("项目代号,x,y").status_code,
        ]
        self.assertEqual(statuses, [202, 202, 202, 202])

    def test_a_rejected_change_never_applies(self) -> None:
        self.require_approval()
        pending = self.create().json()
        self.assertEqual(self.decide(pending["approval_id"], action="reject", comment="no").status_code, 200)
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.rows(), [])

    # -- permissions and isolation --------------------------------------------------

    def test_auditors_read_but_cannot_change_and_viewers_have_no_access(self) -> None:
        self.select("retail")
        key = self.items()["SKU"]["key"]
        auditor = self.login("auditor@example.com")
        self.assertEqual(self.state(auditor).status_code, 200)
        self.assertEqual(self.create(auditor).status_code, 403)
        self.assertEqual(self.put(key, auditor).status_code, 403)
        self.assertEqual(self.client.delete(f"{URL}/terms/{key}", headers=auditor).status_code, 403)
        self.assertEqual(self.client.post(f"{URL}/restore-hidden", headers=auditor).status_code, 403)
        self.assertEqual(self.client.post(f"{URL}/terms/import", content=b"a,b", headers={**auditor, "Content-Type": "text/plain"}).status_code, 403)
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.state(viewer).status_code, 403)
        self.admin = self.login("admin@example.com")
        self.client.cookies.clear()
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_other_tenants_have_their_own_library(self) -> None:
        self.select("retail")
        self.create()
        own_key = self.items()["会员号"]["key"]
        self.put(self.items()["SKU"]["key"], term="SKU", definition="mine")
        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["other"])["rule"].enabled = False
        other = self.login("other@example.com", self.ids["other"])
        state = self.state(other).json()
        self.assertEqual((state["items"], state["chain"]["industries"]), ([], []))
        self.assertEqual(self.put(own_key, other).status_code, 404)  # another tenant's own term id
        self.assertEqual(self.client.delete(f"{URL}/terms/{own_key}", headers=other).status_code, 404)
        self.assertEqual(self.select("insurance", headers=other).status_code, 200)  # their own selection, independent
        self.assertEqual(sorted(i["term"] for i in self.state(other).json()["items"]), ["sku", "理赔"])
        self.admin = self.login("admin@example.com")
        self.assertEqual(self.items()["SKU"]["definition"], "mine")
        self.assertEqual(len(self.rows()), 2)

    def test_the_approval_executor_reports_a_term_that_vanished(self) -> None:
        self.select("retail")
        self.require_approval()
        with self.session_factory.begin() as db:
            row = TenantTerm(tenant_id=self.ids["tenant"], term="临时", term_key="临时", aliases=[], definition="x")
            db.add(row)
            db.flush()
            own_id = str(row.id)
        pending = self.put(own_id, term="临时", definition="y")
        self.assertEqual(pending.status_code, 202)
        with self.session_factory.begin() as db:
            db.delete(db.get(TenantTerm, UUID(own_id)))
        self.decide(pending.json()["approval_id"])
        self.drain()
        approval = self.client.get(f"/api/v1/approvals/{pending.json()['approval_id']}", headers=self.admin).json()
        self.assertNotEqual(approval["status"], "applied")


# Reuse the harness only: the inherited tests already run in test_versions and must not run a second time here.
for _name in dir(test_versions.VersionTests):
    if _name.startswith("test_") and _name not in TenantGlossaryTests.__dict__:
        setattr(TenantGlossaryTests, _name, None)

if __name__ == "__main__":
    unittest.main()
