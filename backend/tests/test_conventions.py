import unittest
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import String, create_engine, event, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import Uuid
from starlette.exceptions import HTTPException

from beework.api.conventions import (
    ApiError,
    Page,
    Pagination,
    SoftDeleteMixin,
    TenantOwnedMixin,
    TimestampMixin,
    VersionedMixin,
    api_error_handler,
    check_row_version,
    ensure_resource_in_scope,
    ensure_unique_name,
    get_pagination,
    normalize_name,
    paginate,
    require_platform_admin,
    scope_filter,
    validate_public_https_url,
    validation_error_handler,
)
from beework.auth import get_current_user
from beework.models import Base, Membership, Organization, Tenant, TenantRole, User
from isolation import disable_external_integrations


class ConventionItem(TenantOwnedMixin, TimestampMixin, VersionedMixin, SoftDeleteMixin, Base):
    __tablename__ = "convention_test_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    organization_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


def resolver_for(*addresses: str):
    return lambda _url: list(addresses)


class ConventionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        cls.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(cls.engine, "connect")
        def enable_fk(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(cls.engine)
        cls.session_factory = sessionmaker(bind=cls.engine, expire_on_commit=False)

    def setUp(self) -> None:
        self.db: Session = self.session_factory()
        self.tenant = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        self.other_tenant = Tenant(name="Other", slug=f"other-{uuid4().hex[:8]}")
        self.db.add_all([self.tenant, self.other_tenant])
        self.db.flush()

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()

    # --- error envelope -------------------------------------------------

    def test_error_envelope_for_api_and_http_errors(self) -> None:
        app = FastAPI()
        app.exception_handler(ApiError)(api_error_handler)
        app.exception_handler(HTTPException)(api_error_handler)
        app.exception_handler(RequestValidationError)(validation_error_handler)

        @app.get("/boom")
        def boom():
            raise ApiError(418, "demo.teapot", "我是茶壶")

        @app.get("/legacy")
        def legacy():
            raise HTTPException(status_code=403, detail="旧错误", headers={"X-Test": "1"})

        client = TestClient(app)
        response = client.get("/boom")
        self.assertEqual(response.status_code, 418)
        self.assertEqual(response.json(), {"detail": "我是茶壶", "code": "demo.teapot"})
        response = client.get("/legacy")
        self.assertEqual(response.json(), {"detail": "旧错误", "code": "error"})
        self.assertEqual(response.headers["X-Test"], "1")
        @app.get("/typed")
        def typed(count: int):
            return {"count": count}

        invalid = client.get("/typed?count=abc")
        self.assertEqual(invalid.status_code, 422)
        body = invalid.json()
        self.assertEqual(body["code"], "validation.invalid")
        self.assertTrue(body["detail"].startswith("count："))
        self.assertEqual(body["errors"][0]["loc"], ["query", "count"])
        missing = client.get("/nope")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "error")

    # --- pagination -----------------------------------------------------

    def test_pagination_defaults_bounds_and_envelope(self) -> None:
        for index in range(25):
            self.db.add(ConventionItem(tenant_id=self.tenant.id, name=f"item-{index:02d}"))
        self.db.flush()
        statement = (
            select(ConventionItem)
            .where(ConventionItem.tenant_id == self.tenant.id)
            .order_by(ConventionItem.name)
        )
        items, total = paginate(self.db, statement)
        self.assertEqual((len(items), total), (20, 25))
        items, total = paginate(self.db, statement, Pagination(limit=20, offset=20))
        self.assertEqual((len(items), total), (5, 25))
        items, total = paginate(self.db, statement, Pagination(limit=10, offset=100))
        self.assertEqual((items, total), ([], 25))

        app = FastAPI()

        @app.get("/items", response_model=Page[str])
        def list_items(pagination: Pagination = Depends(get_pagination)):
            return Page[str](items=["a"], total=1, limit=pagination.limit, offset=pagination.offset)

        client = TestClient(app)
        self.assertEqual(
            client.get("/items").json(), {"items": ["a"], "total": 1, "limit": 20, "offset": 0}
        )
        self.assertEqual(client.get("/items?limit=100&offset=5").json()["limit"], 100)
        for query in ("limit=101", "limit=0", "offset=-1"):
            self.assertEqual(client.get(f"/items?{query}").status_code, 422, query)

    # --- optimistic locking --------------------------------------------

    def test_row_version_conflict_names_last_editor(self) -> None:
        item = ConventionItem(tenant_id=self.tenant.id, name="a", updated_by="王芳")
        self.db.add(item)
        self.db.flush()
        self.assertEqual(item.row_version, 1)
        check_row_version(item, 1, self.db)
        item.row_version = 2
        with self.assertRaises(ApiError) as caught:
            check_row_version(item, 1, self.db)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "conflict.stale")
        self.assertEqual(caught.exception.detail, "该对象已被 王芳 修改，请刷新后重试")
        self.assertIsNone(item.deleted_at)
        self.assertIsNotNone(item.created_at)

    # --- platform admin -------------------------------------------------

    def test_require_platform_admin(self) -> None:
        app = FastAPI()
        app.exception_handler(ApiError)(api_error_handler)
        current = {"is_platform_admin": False}

        @app.get("/admin")
        def admin(user=Depends(require_platform_admin)):
            return {"ok": True}

        app.dependency_overrides[get_current_user] = lambda: User(
            is_platform_admin=current["is_platform_admin"]
        )
        client = TestClient(app)
        denied = client.get("/admin")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["code"], "auth.forbidden")
        current["is_platform_admin"] = True
        self.assertEqual(client.get("/admin").status_code, 200)

    # --- names ----------------------------------------------------------

    def test_normalize_name(self) -> None:
        self.assertEqual(normalize_name("  客服助理 "), "客服助理")
        for bad in ("", "   ", None):
            with self.assertRaises(ApiError) as caught:
                normalize_name(bad)
            self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(ApiError):
            normalize_name("x" * 11, max_len=10)
        self.assertEqual(normalize_name("x" * 10, max_len=10), "x" * 10)

    def test_unique_name_is_case_insensitive_and_tenant_scoped(self) -> None:
        item = ConventionItem(tenant_id=self.tenant.id, name="Support Bot")
        self.db.add(item)
        self.db.flush()
        with self.assertRaises(ApiError) as caught:
            ensure_unique_name(self.db, ConventionItem, self.tenant.id, " support bot ")
        self.assertEqual(caught.exception.status_code, 409)
        ensure_unique_name(self.db, ConventionItem, self.tenant.id, "support bot", exclude_id=item.id)
        ensure_unique_name(self.db, ConventionItem, self.other_tenant.id, "Support Bot")

    # --- URL safety -----------------------------------------------------

    def test_url_validation_rejects_unsafe_targets(self) -> None:
        public = resolver_for("93.184.216.34")
        self.assertEqual(
            validate_public_https_url("https://api.example.com/v1", public),
            "https://api.example.com/v1",
        )
        rejected = [
            ("https://127.0.0.1", public),
            ("https://10.0.0.1", public),
            ("http://example.com", public),
            ("https://localhost", public),
            ("https://internal.example.com", resolver_for("10.1.2.3")),
            ("https://mixed.example.com", resolver_for("93.184.216.34", "192.168.1.5")),
            ("https://meta.example.com", resolver_for("169.254.169.254")),
            ("https://v6.example.com", resolver_for("::1")),
            ("https://nores.example.com", resolver_for()),
        ]
        for url, resolver in rejected:
            with self.assertRaises(ApiError, msg=url) as caught:
                validate_public_https_url(url, resolver)
            self.assertEqual(caught.exception.code, "security.invalid_url")

        def failing(_url: str) -> list[str]:
            raise OSError("no dns")

        with self.assertRaises(ApiError) as caught:
            validate_public_https_url("https://gone.example.com", failing)
        self.assertIn("无法解析", caught.exception.detail)

    # --- resource scope -------------------------------------------------

    def _scope_fixture(self):
        root = Organization(tenant_id=self.tenant.id, name="Root")
        self.db.add(root)
        self.db.flush()
        child = Organization(tenant_id=self.tenant.id, parent_id=root.id, name="Child")
        sibling = Organization(tenant_id=self.tenant.id, parent_id=root.id, name="Sibling")
        self.db.add_all([child, sibling])
        self.db.flush()
        role = TenantRole(
            tenant_id=self.tenant.id,
            name="scoped",
            scope="organization",
            scope_organization_id=child.id,
            permissions={},
        )
        self.db.add(role)
        self.db.flush()
        membership = Membership(
            tenant_id=self.tenant.id,
            user_id=uuid4(),
            organization_id=child.id,
            role="viewer",
            custom_role_id=role.id,
        )
        return root, child, sibling, membership

    def test_resource_scope_returns_404_outside_scope(self) -> None:
        root, child, sibling, membership = self._scope_fixture()
        in_scope = ConventionItem(tenant_id=self.tenant.id, name="in", organization_id=child.id)
        out_of_scope = ConventionItem(tenant_id=self.tenant.id, name="out", organization_id=sibling.id)
        foreign = ConventionItem(tenant_id=self.other_tenant.id, name="foreign")
        unscoped = ConventionItem(tenant_id=self.tenant.id, name="none")
        ensure_resource_in_scope(membership, in_scope, self.db)
        ensure_resource_in_scope(membership, unscoped, self.db)
        for resource in (out_of_scope, foreign):
            with self.assertRaises(ApiError) as caught:
                ensure_resource_in_scope(membership, resource, self.db)
            self.assertEqual(caught.exception.status_code, 404)

    def test_scope_filter_limits_lists_to_organization_subtree(self) -> None:
        root, child, sibling, membership = self._scope_fixture()
        self.db.add_all(
            [
                ConventionItem(tenant_id=self.tenant.id, name="in", organization_id=child.id),
                ConventionItem(tenant_id=self.tenant.id, name="out", organization_id=sibling.id),
            ]
        )
        self.db.flush()
        statement = scope_filter(
            select(ConventionItem).where(ConventionItem.tenant_id == self.tenant.id),
            ConventionItem,
            membership,
            self.db,
        )
        self.assertEqual([item.name for item in self.db.scalars(statement)], ["in"])
        admin = Membership(tenant_id=self.tenant.id, user_id=uuid4(), role="tenant_admin")
        unfiltered = scope_filter(select(ConventionItem), ConventionItem, admin, self.db)
        self.assertEqual(len(list(self.db.scalars(unfiltered))), 2)


if __name__ == "__main__":
    unittest.main()
