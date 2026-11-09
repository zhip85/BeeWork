"""T-051: the import parser, the industry dictionary, platform glossaries and terms, and tenants' industry."""

import unittest
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework import governance
from beework.db import get_db
from beework.glossary_parser import parse_terms, split_aliases
from beework.main import app
from beework.models import AuditEvent, Base, Membership, Organization, Tenant, User
from beework.models.glossary import Industry, IndustryGlossary, PlatformTerm
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "platform admin safe password"
INDUSTRIES = "/api/v1/platform/industries"
GLOSSARIES = "/api/v1/platform/glossaries"


class ParserTests(unittest.TestCase):
    def terms(self, text: str):
        return [(t.line, t.term, t.aliases, t.definition) for t in parse_terms(text).terms]

    def test_basic_rows_blank_lines_and_the_third_column_onwards_form_the_definition(self) -> None:
        text = "理赔,赔付;出险理赔,被保险人申请赔偿，也可以是给付\n\n保费,,投保人支付的费用,含税\n"
        self.assertEqual(self.terms(text), [
            (1, "理赔", ["赔付", "出险理赔"], "被保险人申请赔偿，也可以是给付"),
            (3, "保费", [], "投保人支付的费用,含税"),
        ])

    def test_a_header_line_is_skipped_in_english_or_chinese(self) -> None:
        for header in ("term,aliases,definition", "术语，别名，定义", "Name\talias\tdef", "TERM"):
            result = parse_terms(f"{header}\nSKU,库存单位,最小单元")
            self.assertTrue(result.header_skipped, header)
            self.assertEqual([t.term for t in result.terms], ["SKU"], header)
        self.assertFalse(parse_terms("SKU,库存单位,最小单元").header_skipped)
        # Only the first line can be a header.
        self.assertEqual([t.term for t in parse_terms("SKU,a,b\nterm,x,y").terms], ["SKU", "term"])

    def test_field_separators_are_comma_chinese_comma_and_tab(self) -> None:
        self.assertEqual(self.terms("A,a1;a2,def\nB，b1，def2\nC\tc1\tdef3"), [
            (1, "A", ["a1", "a2"], "def"), (2, "B", ["b1"], "def2"), (3, "C", ["c1"], "def3"),
        ])

    def test_aliases_use_semicolons_and_are_trimmed_and_deduplicated(self) -> None:
        self.assertEqual(split_aliases(" a ; A；b;; sku ;", "SKU"), ["a", "b"])  # the term itself and duplicates drop out
        self.assertEqual(split_aliases("", "x"), [])
        self.assertEqual(self.terms("GMV, 成交总额；商品交易总额 ;成交总额 ,def"), [(1, "GMV", ["成交总额", "商品交易总额"], "def")])

    def test_missing_columns_and_whitespace_are_tolerated(self) -> None:
        self.assertEqual(self.terms("  仅术语  \n术语二,别名"), [(1, "仅术语", [], ""), (2, "术语二", ["别名"], "")])

    def test_bom_and_windows_line_endings(self) -> None:
        self.assertEqual(self.terms("﻿term,a,b\r\nSKU,x,y\r\n"), [(2, "SKU", ["x"], "y")])

    def test_every_bad_row_is_reported_with_its_line_and_the_good_ones_survive(self) -> None:
        text = (
            "好术语,a,def\n"
            ",空术语,def\n"
            f"{'长' * 201},a,def\n"
            f"别名多,{';'.join(f'a{i}' for i in range(21))},def\n"
            f"别名长,{'x' * 101},def\n"
            f"定义长,a,{'d' * 1001}\n"
            "好术语,b,重复\n"
            "GOOD,a,def\n"
            "good,b,重复大小写\n"
        )
        result = parse_terms(text)
        self.assertEqual([t.term for t in result.terms], ["好术语", "GOOD"])
        self.assertEqual([e["line"] for e in result.errors], [2, 3, 4, 5, 6, 7, 9])
        messages = [e["message"] for e in result.errors]
        self.assertIn("术语不能为空", messages[0])
        self.assertIn("200", messages[1])
        self.assertIn("20", messages[2])
        self.assertIn("100", messages[3])
        self.assertIn("1000", messages[4])
        self.assertIn("第 1 行重复", messages[5])
        self.assertIn("第 8 行重复", messages[6])

    def test_the_row_limit_stops_the_parse(self) -> None:
        result = parse_terms("\n".join(f"t{i},a,d" for i in range(5)), max_rows=3)
        self.assertEqual(len(result.terms), 3)
        self.assertIn("最多导入 3 条", result.errors[0]["message"])

    def test_empty_input(self) -> None:
        result = parse_terms("  \n\n")
        self.assertEqual((result.terms, result.errors, result.header_skipped), ([], [], False))


class GlossaryApiTests(unittest.TestCase):
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
            for order, (code, zh, en) in enumerate((("general_office", "通用办公", "General office"), ("insurance", "保险", "Insurance"), ("banking", "银行金融", "Banking"))):
                db.add(Industry(code=code, name_zh=zh, name_en=en, sort_order=order))
            db.add_all([
                Tenant(name="Insurer", slug="insurer", industry="insurance"),
                Tenant(name="Bank", slug="bank", industry="banking"),
                Tenant(name="Plain", slug="plain"),
            ])
            db.add(User(email="platform@example.com", display_name="Platform", password_hash=hash_password(PASSWORD), is_platform_admin=True))
            admin = User(email="admin@example.com", display_name="Admin", password_hash=hash_password(PASSWORD))
            db.add(admin)
            db.flush()
            plain = db.scalar(select(Tenant).where(Tenant.slug == "plain"))
            root = Organization(tenant_id=plain.id, name="Plain")
            db.add(root)
            db.flush()
            db.add(Membership(tenant_id=plain.id, user_id=admin.id, organization_id=root.id, role="tenant_admin"))
        self.login("platform@example.com")

    def login(self, email: str) -> None:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code, 200)

    @property
    def csrf(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf")}

    def glossary(self, name: str = "保险", industry: str = "insurance", **extra):
        return self.client.post(GLOSSARIES, json={"name": name, "industry_code": industry, "description": "d", **extra}, headers=self.csrf)

    def term(self, glossary_id: str, term: str = "理赔", **extra):
        return self.client.post(f"{GLOSSARIES}/{glossary_id}/terms", json={"term": term, "aliases": ["赔付"], "definition": "x", **extra}, headers=self.csrf)

    def import_text(self, glossary_id: str, text: str, **params):
        return self.client.post(
            f"{GLOSSARIES}/{glossary_id}/terms/import", params=params, content=text.encode(), headers={**self.csrf, "Content-Type": "text/plain"}
        )

    def audit_actions(self) -> list[str]:
        with self.session_factory() as db:
            return [a.action for a in db.scalars(select(AuditEvent).order_by(AuditEvent.created_at)) if not a.action.startswith("auth.")]

    # -- industry dictionary ------------------------------------------------------

    def test_industry_dictionary_crud_with_usage_counts_and_protection(self) -> None:
        listing = {i["code"]: i for i in self.client.get(INDUSTRIES).json()}
        self.assertEqual((listing["insurance"]["tenant_count"], listing["general_office"]["tenant_count"]), (1, 1))
        created = self.client.post(INDUSTRIES, json={"code": "healthcare", "name_zh": "医疗", "name_en": "Healthcare"}, headers=self.csrf)
        self.assertEqual((created.status_code, created.json()["status"]), (201, "active"))
        self.assertEqual(self.client.post(INDUSTRIES, json={"code": "healthcare", "name_zh": "x", "name_en": "x"}, headers=self.csrf).status_code, 409)
        for bad in ({"code": "Bad Code"}, {"code": "1abc"}, {"code": None}):
            self.assertEqual(self.client.post(INDUSTRIES, json={"name_zh": "x", "name_en": "x", **bad}, headers=self.csrf).status_code, 422, bad)
        renamed = self.client.put(f"{INDUSTRIES}/healthcare", json={"name_zh": "医疗健康", "name_en": "Health", "status": "disabled", "sort_order": 9}, headers=self.csrf)
        self.assertEqual((renamed.json()["name_zh"], renamed.json()["status"]), ("医疗健康", "disabled"))
        self.assertEqual(self.client.put(f"{INDUSTRIES}/general_office", json={"name_zh": "x", "name_en": "x", "status": "disabled"}, headers=self.csrf).status_code, 409)
        # An industry that tenants or glossaries use cannot be deleted; an unused one can.
        in_use = self.client.delete(f"{INDUSTRIES}/insurance", headers=self.csrf)
        self.assertEqual((in_use.status_code, in_use.json()["code"]), (409, "industry.in_use"))
        self.assertEqual(self.client.delete(f"{INDUSTRIES}/general_office", headers=self.csrf).status_code, 409)
        self.assertEqual(self.client.delete(f"{INDUSTRIES}/healthcare", headers=self.csrf).status_code, 204)
        self.assertEqual(self.client.delete(f"{INDUSTRIES}/healthcare", headers=self.csrf).status_code, 404)
        self.assertEqual(self.audit_actions(), ["industry.created", "industry.updated", "industry.deleted"])

    def test_glossary_usage_blocks_industry_deletion(self) -> None:
        self.client.post(INDUSTRIES, json={"code": "healthcare", "name_zh": "医疗", "name_en": "Healthcare"}, headers=self.csrf)
        self.glossary("医疗库", "healthcare")
        self.assertEqual(self.client.delete(f"{INDUSTRIES}/healthcare", headers=self.csrf).json()["code"], "industry.in_use")

    # -- glossaries ---------------------------------------------------------------

    def test_glossary_crud_counts_validation_and_tenant_count(self) -> None:
        created = self.glossary()
        self.assertEqual(created.status_code, 201, created.text)
        glossary = created.json()
        self.assertEqual((glossary["industry_name"], glossary["term_count"], glossary["tenant_count"]), ("保险", 0, 1))
        self.assertEqual(self.glossary(name="保险").status_code, 409)
        self.assertEqual(self.glossary(name="Retail", industry="banking").status_code, 201)
        self.assertEqual(self.glossary(name="retail", industry="banking").status_code, 409)  # names are unique ignoring case
        self.assertEqual(self.glossary(name="  ").status_code, 422)
        self.assertEqual(self.glossary(name="x", industry="nope").status_code, 422)
        renamed = self.client.put(f"{GLOSSARIES}/{glossary['id']}", json={**glossary, "name": "保险业", "industry_code": "banking"}, headers=self.csrf)
        self.assertEqual((renamed.json()["name"], renamed.json()["industry_name"], renamed.json()["tenant_count"]), ("保险业", "银行金融", 1))
        stale = self.client.put(f"{GLOSSARIES}/{glossary['id']}", json={**glossary, "name": "再改"}, headers=self.csrf)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        self.assertEqual([g["name"] for g in self.client.get(GLOSSARIES).json()], ["保险业", "Retail"])

    def test_deleting_a_glossary_that_tenants_inherit_needs_confirmation(self) -> None:
        glossary = self.glossary().json()
        self.term(glossary["id"])
        blocked = self.client.delete(f"{GLOSSARIES}/{glossary['id']}", headers=self.csrf)
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "glossary.in_use"))
        self.assertIn("1 个租户", blocked.json()["detail"])
        self.assertEqual(self.client.delete(f"{GLOSSARIES}/{glossary['id']}", params={"confirm": "true"}, headers=self.csrf).status_code, 204)
        with self.session_factory() as db:
            self.assertEqual(db.scalars(select(PlatformTerm)).all(), [])  # the terms went with it
        empty = self.glossary(name="空库", industry="general_office")  # one tenant ("Plain") is of this industry too
        self.assertEqual(self.client.delete(f"{GLOSSARIES}/{empty.json()['id']}", headers=self.csrf).status_code, 409)

    # -- terms --------------------------------------------------------------------

    def test_term_crud_uniqueness_search_and_normalisation(self) -> None:
        glossary = self.glossary().json()
        created = self.term(glossary["id"], term="  理赔 ", aliases=[" 赔付 ", "出险理赔", "赔付", "理赔"])
        self.assertEqual(created.status_code, 201, created.text)
        term = created.json()
        self.assertEqual((term["term"], term["aliases"]), ("理赔", ["赔付", "出险理赔"]))
        self.assertEqual(self.term(glossary["id"], term="理赔").status_code, 409)
        self.assertEqual(self.term(glossary["id"], term="sku").status_code, 201)
        self.assertEqual(self.term(glossary["id"], term="SKU").status_code, 409)  # case-insensitive
        self.assertEqual(self.term(glossary["id"], term=" ").status_code, 422)
        self.assertEqual(self.term(glossary["id"], term="x" * 201).status_code, 422)
        self.assertEqual(self.term(glossary["id"], term="y", aliases=[f"a{i}" for i in range(21)]).status_code, 422)
        self.assertEqual(self.term(glossary["id"], term="z", definition="d" * 1001).status_code, 422)
        base = f"{GLOSSARIES}/{glossary['id']}/terms"
        for query, expected in (("出险", ["理赔"]), ("sku", ["sku"]), ("理", ["理赔"]), ("不存在", [])):
            found = self.client.get(base, params={"q": query}).json()
            self.assertEqual([t["term"] for t in found["items"]], expected, query)  # matches terms and aliases
        edited = self.client.put(f"{base}/{term['id']}", json={**term, "term": "SKU", "aliases": []}, headers=self.csrf)
        self.assertEqual(edited.status_code, 409)  # collides with "sku"
        ok = self.client.put(f"{base}/{term['id']}", json={**term, "definition": "新定义"}, headers=self.csrf)
        self.assertEqual((ok.status_code, ok.json()["definition"], ok.json()["row_version"]), (200, "新定义", 2))
        stale = self.client.put(f"{base}/{term['id']}", json={**term, "definition": "再改"}, headers=self.csrf)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.delete(f"{base}/{term['id']}", headers=self.csrf).status_code, 204)
        self.assertEqual(self.client.get(base).json()["total"], 1)
        other = self.glossary(name="另一个", industry="banking").json()
        self.assertEqual(self.client.get(f"{GLOSSARIES}/{other['id']}/terms/{term['id']}", headers=self.csrf).status_code, 405)
        self.assertEqual(self.client.delete(f"{GLOSSARIES}/{other['id']}/terms/{term['id']}", headers=self.csrf).status_code, 404)  # not that glossary's term

    # -- import -------------------------------------------------------------------

    def test_import_preview_counts_without_changing_anything(self) -> None:
        glossary = self.glossary().json()
        self.term(glossary["id"], term="理赔")
        text = "术语,别名,定义\n理赔,赔付,新定义\n保费,保险费,投保人支付的费用\n,坏行,x\n"
        preview = self.import_text(glossary["id"], text, dry_run="true")
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertEqual((body["dry_run"], body["recognized"], body["duplicates"], body["created"]), (True, 2, 1, 0))
        self.assertEqual([(r["term"], r["exists"]) for r in body["preview"]], [("理赔", True), ("保费", False)])
        self.assertEqual([(e["line"], e["message"]) for e in body["errors"]], [(4, "术语不能为空")])
        self.assertEqual(self.client.get(f"{GLOSSARIES}/{glossary['id']}/terms").json()["total"], 1)

    def test_import_skips_or_overwrites_duplicates_as_chosen(self) -> None:
        glossary = self.glossary().json()
        self.term(glossary["id"], term="理赔")
        text = "理赔,出险理赔,新定义\n保费,,费用\n"
        skipped = self.import_text(glossary["id"], text, on_duplicate="skip").json()
        self.assertEqual((skipped["created"], skipped["updated"], skipped["skipped"]), (1, 0, 1))
        kept = next(t for t in self.client.get(f"{GLOSSARIES}/{glossary['id']}/terms").json()["items"] if t["term"] == "理赔")
        self.assertEqual(kept["definition"], "x")
        overwritten = self.import_text(glossary["id"], text, on_duplicate="overwrite").json()
        self.assertEqual((overwritten["created"], overwritten["updated"], overwritten["skipped"]), (0, 2, 0))
        changed = next(t for t in self.client.get(f"{GLOSSARIES}/{glossary['id']}/terms").json()["items"] if t["term"] == "理赔")
        self.assertEqual((changed["definition"], changed["aliases"], changed["row_version"]), ("新定义", ["出险理赔"], 2))
        self.assertIn("glossary_term.imported", self.audit_actions())

    def test_import_encodings_limits_and_empty_input(self) -> None:
        glossary = self.glossary().json()
        url = f"{GLOSSARIES}/{glossary['id']}/terms/import"
        headers = {**self.csrf, "Content-Type": "text/plain"}
        gbk = self.client.post(url, content="术语,别名\n理赔,赔付".encode("gb18030"), headers=headers)
        self.assertEqual(gbk.json()["created"], 1)
        bom = self.client.post(url, content=b"\xef\xbb\xbf" + "保费,x".encode(), headers=headers)
        self.assertEqual(bom.json()["created"], 1)
        self.assertEqual(self.import_text(glossary["id"], "  \n").status_code, 422)
        self.assertEqual(self.client.post(url, content=b"\xff\xfe\x00\x80" * 50, headers=headers).status_code, 422)
        self.assertEqual(self.client.post(url, content=b"a,b\n" + b"x" * 1_100_000, headers=headers).status_code, 413)
        only_errors = self.import_text(glossary["id"], ",只有错误\n")
        self.assertEqual((only_errors.json()["recognized"], len(only_errors.json()["errors"])), (0, 1))

    # -- tenants and the finance scope ---------------------------------------------

    def test_tenants_carry_an_industry_from_the_dictionary(self) -> None:
        tenants = {t["slug"]: t for t in self.client.get("/api/v1/platform/tenants").json()}
        self.assertEqual((tenants["insurer"]["industry"], tenants["plain"]["industry"]), ("insurance", "general_office"))
        created = self.client.post(
            "/api/v1/platform/tenants",
            json={"name": "New", "slug": "new-co", "seat_limit": 5, "industry": "banking", "admin_email": "n@example.com",
                  "admin_name": "N", "admin_password": "a-long-safe-password"},
            headers=self.csrf,
        )
        self.assertEqual((created.status_code, created.json()["industry"]), (201, "banking"), created.text)
        bad = self.client.post(
            "/api/v1/platform/tenants",
            json={"name": "Bad", "slug": "bad-co", "seat_limit": 5, "industry": "nope", "admin_email": "b@example.com",
                  "admin_name": "B", "admin_password": "a-long-safe-password"},
            headers=self.csrf,
        )
        self.assertEqual((bad.status_code, bad.json()["code"]), (422, "glossary.invalid"))
        default = self.client.post(
            "/api/v1/platform/tenants",
            json={"name": "Dflt", "slug": "dflt-co", "seat_limit": 5, "admin_email": "d@example.com", "admin_name": "D", "admin_password": "a-long-safe-password"},
            headers=self.csrf,
        )
        self.assertEqual(default.json()["industry"], "general_office")
        plain_id = tenants["plain"]["id"]
        edited = self.client.patch(f"/api/v1/platform/tenants/{plain_id}", json={"industry": "insurance"}, headers=self.csrf)
        self.assertEqual((edited.status_code, edited.json()["industry"]), (200, "insurance"))
        self.client.put(f"{INDUSTRIES}/banking", json={"name_zh": "银行金融", "name_en": "Banking", "status": "disabled"}, headers=self.csrf)
        disabled = self.client.patch(f"/api/v1/platform/tenants/{plain_id}", json={"industry": "banking"}, headers=self.csrf)
        self.assertEqual(disabled.status_code, 422)  # a disabled industry cannot be assigned any more

    def test_the_finance_distribution_scope_follows_the_tenant_industry(self) -> None:
        with self.session_factory() as db:
            by_slug = {t.slug: t for t in db.scalars(select(Tenant))}
            self.assertTrue(governance.scope_matches(db, by_slug["insurer"], "industry_finance"))
            self.assertTrue(governance.scope_matches(db, by_slug["bank"], "industry_finance"))
            self.assertFalse(governance.scope_matches(db, by_slug["plain"], "industry_finance"))

    def test_only_platform_admins_and_audit(self) -> None:
        glossary = self.glossary().json()
        self.login("admin@example.com")
        for method, path in (("get", INDUSTRIES), ("get", GLOSSARIES), ("get", f"{GLOSSARIES}/{glossary['id']}/terms")):
            self.assertEqual(getattr(self.client, method)(path).status_code, 403, path)
        self.assertEqual(self.client.post(GLOSSARIES, json={"name": "x", "industry_code": "insurance"}, headers=self.csrf).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(GLOSSARIES).status_code, 401)
        self.login("platform@example.com")
        self.assertEqual(self.client.post(GLOSSARIES, json={"name": "x", "industry_code": "insurance"}).status_code, 403)  # no CSRF token
        self.assertEqual(self.client.get(f"{GLOSSARIES}/{UUID(int=1)}/terms").status_code, 404)
        self.assertEqual(self.audit_actions(), ["glossary.created"])


class MigrationDataTests(unittest.TestCase):
    def test_the_seed_lists_cover_the_prd_industries_and_the_three_preset_glossaries(self) -> None:
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "migrations" / "versions" / "0035_industries.py"
        spec = importlib.util.spec_from_file_location("migration_0035", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        codes = [code for code, *_ in module.INDUSTRIES]
        self.assertEqual(codes, ["insurance", "banking", "retail_ecommerce", "healthcare", "general_office", "public_sector"])
        by_name = {name: (industry, items) for name, _desc, industry, items in module.GLOSSARIES}
        self.assertEqual({name: industry for name, (industry, _) in by_name.items()}, {"零售电商": "retail_ecommerce", "保险": "insurance", "银行金融": "banking"})
        terms = {name: {t for t, _a, _d in items} for name, (_i, items) in by_name.items()}
        self.assertTrue({"SKU", "高级会员", "退货时效", "GMV"} <= terms["零售电商"])
        self.assertTrue({"理赔", "保费", "等待期"} <= terms["保险"])
        self.assertTrue({"KYC", "年化利率"} <= terms["银行金融"])
        for _name, (_industry, items) in by_name.items():
            keys = [term.lower() for term, _a, _d in items]
            self.assertEqual(len(keys), len(set(keys)))  # unique per glossary
            for term, aliases, definition in items:
                self.assertTrue(term and definition)
                self.assertNotIn(term.lower(), [alias.lower() for alias in aliases])
        self.assertEqual(IndustryGlossary.__tablename__, "industry_glossaries")


if __name__ == "__main__":
    unittest.main()
