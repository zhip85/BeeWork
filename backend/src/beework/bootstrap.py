from sqlalchemy import select

from beework.config import settings
from beework.db import SessionLocal
from beework.models import Membership, Organization, Tenant, User
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

        root_organization = db.scalar(
            select(Organization).where(
                Organization.tenant_id == tenant.id,
                Organization.parent_id.is_(None),
            )
        )
        if root_organization is None:
            root_organization = Organization(
                tenant_id=tenant.id,
                name=tenant.name,
            )
            db.add(root_organization)
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
                is_platform_admin=False,
            )
            db.add(user)
            db.flush()
        elif user.is_platform_admin:
            user.is_platform_admin = False

        for platform_email, platform_name, platform_password in (
            (
                settings.bootstrap_platform_admin_email,
                settings.bootstrap_platform_admin_name,
                settings.bootstrap_platform_admin_password,
            ),
            (
                settings.bootstrap_platform_reviewer_email,
                settings.bootstrap_platform_reviewer_name,
                settings.bootstrap_platform_reviewer_password,
            ),
        ):
            platform_email = platform_email.strip().lower()
            platform_admin = db.scalar(select(User).where(User.email == platform_email))
            if platform_admin is None:
                db.add(
                    User(
                        email=platform_email,
                        display_name=platform_name,
                        password_hash=hash_password(platform_password),
                        is_platform_admin=True,
                    )
                )
            else:
                platform_admin.is_platform_admin = True

        membership = db.scalar(
            select(Membership).where(
                Membership.tenant_id == tenant.id,
                Membership.user_id == user.id,
            )
        )
        if membership is None:
            db.add(
                Membership(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    organization_id=root_organization.id,
                    role="tenant_admin",
                )
            )


if __name__ == "__main__":
    bootstrap_development_tenant()
