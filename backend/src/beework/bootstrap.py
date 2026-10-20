from sqlalchemy import select

from beework.config import settings
from beework.db import SessionLocal
from beework.models import Membership, Tenant, User
from beework.security import hash_password


def bootstrap_development_tenant() -> None:
    if settings.environment != "development":
        return

    with SessionLocal.begin() as db:
        tenant = db.scalar(
            select(Tenant).where(Tenant.slug == settings.bootstrap_tenant_slug)
        )
        if tenant is None:
            tenant = Tenant(
                name=settings.bootstrap_tenant_name,
                slug=settings.bootstrap_tenant_slug,
            )
            db.add(tenant)
            db.flush()

        email = settings.bootstrap_admin_email.strip().lower()
        user = db.scalar(select(User).where(User.email == email))
        if user is None and email.endswith("@beework.dev"):
            user = db.scalar(
                select(User).where(User.email == email.replace("@beework.dev", "@beework.local"))
            )
            if user is not None:
                user.email = email
        if user is None:
            user = User(
                email=email,
                display_name=settings.bootstrap_admin_name,
                password_hash=hash_password(settings.bootstrap_admin_password),
                is_platform_admin=True,
            )
            db.add(user)
            db.flush()

        membership = db.scalar(
            select(Membership).where(
                Membership.tenant_id == tenant.id,
                Membership.user_id == user.id,
            )
        )
        if membership is None:
            db.add(
                Membership(tenant_id=tenant.id, user_id=user.id, role="tenant_admin")
            )


if __name__ == "__main__":
    bootstrap_development_tenant()
