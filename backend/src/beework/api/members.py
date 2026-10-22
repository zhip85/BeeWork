import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.api.common import (
    create_login_session,
    lock_tenant,
    make_current_user_response,
)
from beework.auth import (
    require_csrf,
    require_tenant_roles,
)
from beework.config import settings
from beework.db import get_db
from beework.models import (
    AuditEvent,
    AuthSession,
    MemberInvitation,
    Membership,
    Organization,
    Tenant,
    TenantRole,
    User,
)
from beework.rbac import (
    organization_is_within_scope,
)
from beework.schemas import (
    AcceptMemberInvitationRequest,
    ChangeMemberOrganizationRequest,
    ChangeMemberStatusRequest,
    ChangeRoleRequest,
    CreatedMemberInvitationResponse,
    CreateMemberInvitationsRequest,
    CreateMemberInvitationsResponse,
    CurrentUserResponse,
    MemberInvitationResponse,
    MemberListResponse,
    MemberResponse,
)
from beework.security import (
    hash_password,
    verify_password,
)

router = APIRouter()


def _seat_usage(db: Session, tenant_id: UUID) -> int:
    now = datetime.now(UTC)
    active_members = db.scalar(
        select(func.count())
        .select_from(Membership)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id == tenant_id,
            Membership.status == "active",
            User.is_active.is_(True),
        )
    ) or 0
    pending_invitations = db.scalar(
        select(func.count())
        .select_from(MemberInvitation)
        .where(
            MemberInvitation.tenant_id == tenant_id,
            MemberInvitation.status == "pending",
            MemberInvitation.expires_at > now,
        )
    ) or 0
    return active_members + pending_invitations


def _invitation_response(
    db: Session, invitation: MemberInvitation
) -> MemberInvitationResponse:
    custom_role = (
        db.get(TenantRole, invitation.custom_role_id)
        if invitation.custom_role_id
        else None
    )
    organization = db.get(Organization, invitation.organization_id)
    if organization is None:
        raise RuntimeError("Invitation references a missing organization")
    return MemberInvitationResponse(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        custom_role_id=invitation.custom_role_id,
        custom_role_name=custom_role.name if custom_role else None,
        organization_id=invitation.organization_id,
        organization_name=organization.name,
        status=(
            "expired"
            if invitation.status == "pending"
            and (
                invitation.expires_at.replace(tzinfo=UTC)
                if invitation.expires_at.tzinfo is None
                else invitation.expires_at
            ) <= datetime.now(UTC)
            else invitation.status
        ),
        expires_at=invitation.expires_at,
    )


def _member_response(
    db: Session, user: User, membership: Membership
) -> MemberResponse:
    organization = db.get(Organization, membership.organization_id)
    custom_role = (
        db.get(TenantRole, membership.custom_role_id)
        if membership.custom_role_id
        else None
    )
    if organization is None:
        raise RuntimeError("Membership references a missing organization")
    return MemberResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,
        is_active=user.is_active,
        status=membership.status,
        organization_id=membership.organization_id,
        organization_name=organization.name,
        custom_role_id=membership.custom_role_id,
        custom_role_name=custom_role.name if custom_role else None,
    )

@router.get("/members", response_model=MemberListResponse)
def list_members(
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    db: Session = Depends(get_db),
) -> MemberListResponse:
    members = db.execute(
        select(User, Membership, Organization.name, TenantRole.name)
        .join(Membership, Membership.user_id == User.id)
        .join(Organization, Organization.id == Membership.organization_id)
        .outerjoin(TenantRole, TenantRole.id == Membership.custom_role_id)
        .where(Membership.tenant_id == membership.tenant_id)
        .order_by(User.display_name, User.email)
    )
    return MemberListResponse(
        tenant_id=membership.tenant_id,
        seat_limit=db.get(Tenant, membership.tenant_id).seat_limit,
        seats_used=_seat_usage(db, membership.tenant_id),
        members=[
            MemberResponse(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=member.role,
                is_active=user.is_active,
                status=member.status,
                organization_id=member.organization_id,
                organization_name=organization_name,
                custom_role_id=member.custom_role_id,
                custom_role_name=custom_role_name,
            )
            for user, member, organization_name, custom_role_name in members
        ],
        invitations=[
            _invitation_response(db, invitation)
            for invitation in db.scalars(
                select(MemberInvitation)
                .where(MemberInvitation.tenant_id == membership.tenant_id)
                .order_by(MemberInvitation.created_at.desc())
            )
        ],
    )


@router.post(
    "/members/invitations",
    response_model=CreateMemberInvitationsResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_member_invitations(
    payload: CreateMemberInvitationsRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CreateMemberInvitationsResponse:
    tenant = lock_tenant(db, membership.tenant_id)
    organization = db.scalar(
        select(Organization).where(
            Organization.id == payload.organization_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="邀请部门不属于当前企业")
    custom_role = None
    if payload.role == "custom":
        custom_role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == payload.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        if custom_role is None:
            raise HTTPException(status_code=404, detail="未找到可用的自定义角色")
        if custom_role.scope == "organization" and not organization_is_within_scope(
            db, membership.tenant_id, organization.id, custom_role.scope_organization_id
        ):
            raise HTTPException(status_code=409, detail="所选部门不属于该角色的数据范围")

    emails = [str(email).strip().lower() for email in payload.emails]
    for email in emails:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user:
            existing_membership = db.scalar(
                select(Membership).where(
                    Membership.tenant_id == membership.tenant_id,
                    Membership.user_id == user.id,
                )
            )
            if existing_membership:
                raise HTTPException(status_code=409, detail=f"{email} 已是本企业成员")
        pending_invitation = db.scalar(
            select(MemberInvitation).where(
                MemberInvitation.tenant_id == membership.tenant_id,
                MemberInvitation.email == email,
                MemberInvitation.status == "pending",
                MemberInvitation.expires_at > datetime.now(UTC),
            )
        )
        if pending_invitation:
            raise HTTPException(status_code=409, detail=f"{email} 已有待处理邀请")

    seats_used = _seat_usage(db, membership.tenant_id)
    if seats_used + len(emails) > tenant.seat_limit:
        raise HTTPException(
            status_code=409,
            detail=f"席位已满（{seats_used}/{tenant.seat_limit}），请联系平台扩容",
        )

    created: list[CreatedMemberInvitationResponse] = []
    now = datetime.now(UTC)
    for email in emails:
        token = secrets.token_urlsafe(32)
        invitation = MemberInvitation(
            tenant_id=membership.tenant_id,
            email=email,
            role=payload.role,
            custom_role_id=custom_role.id if custom_role else None,
            organization_id=organization.id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            status="pending",
            invited_by=membership.user_id,
            expires_at=now + timedelta(days=7),
        )
        db.add(invitation)
        db.flush()
        base = _invitation_response(db, invitation)
        created.append(
            CreatedMemberInvitationResponse(
                **base.model_dump(),
                invitation_url=f"{settings.app_origin}/invite?token={token}",
            )
        )
        db.add(
            AuditEvent(
                tenant_id=membership.tenant_id,
                actor_user_id=membership.user_id,
                action="membership.invited",
                details={"invitation_id": str(invitation.id), "email": email},
            )
        )
    db.commit()
    return CreateMemberInvitationsResponse(
        invitations=created,
        seat_limit=tenant.seat_limit,
        seats_used=seats_used + len(created),
    )


@router.post(
    "/members/invitations/accept",
    response_model=CurrentUserResponse,
)
def accept_member_invitation(
    payload: AcceptMemberInvitationRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> CurrentUserResponse:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != settings.app_origin.rstrip("/"):
        raise HTTPException(status_code=403, detail="请求来源不合法")
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    invitation = db.scalar(
        select(MemberInvitation).where(MemberInvitation.token_hash == token_hash)
    )
    if invitation is None:
        raise HTTPException(status_code=410, detail="邀请链接无效或已使用")
    tenant = lock_tenant(db, invitation.tenant_id)
    invitation = db.scalar(
        select(MemberInvitation)
        .where(
            MemberInvitation.id == invitation.id,
            MemberInvitation.tenant_id == tenant.id,
            MemberInvitation.token_hash == token_hash,
        )
        .with_for_update()
    )
    if invitation is None or invitation.status != "pending":
        raise HTTPException(status_code=410, detail="邀请链接无效或已使用")
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        invitation.status = "expired"
        db.commit()
        raise HTTPException(status_code=410, detail="邀请链接已过期")

    organization = db.scalar(
        select(Organization).where(
            Organization.id == invitation.organization_id,
            Organization.tenant_id == invitation.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=409, detail="邀请所属部门已不可用")
    if invitation.custom_role_id:
        invitation_role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == invitation.custom_role_id,
                TenantRole.tenant_id == invitation.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        if invitation_role is None or (
            invitation_role.scope == "organization"
            and not organization_is_within_scope(
                db,
                invitation.tenant_id,
                invitation.organization_id,
                invitation_role.scope_organization_id,
            )
        ):
            raise HTTPException(status_code=409, detail="邀请角色已不可用，请联系企业管理员")
    user = db.scalar(select(User).where(func.lower(User.email) == invitation.email))
    if user:
        if not user.is_active or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="邮箱或密码不正确")
        if db.scalar(
            select(Membership.id).where(
                Membership.tenant_id == invitation.tenant_id,
                Membership.user_id == user.id,
            )
        ):
            raise HTTPException(status_code=409, detail="该账号已是企业成员")
    else:
        if not payload.display_name:
            raise HTTPException(status_code=422, detail="请填写姓名")
        user = User(
            email=invitation.email,
            display_name=payload.display_name,
            password_hash=hash_password(payload.password),
        )
        db.add(user)
        db.flush()

    if _seat_usage(db, invitation.tenant_id) > tenant.seat_limit:
        raise HTTPException(status_code=409, detail="企业席位已调整，请联系企业管理员")
    member = Membership(
        tenant_id=invitation.tenant_id,
        user_id=user.id,
        organization_id=invitation.organization_id,
        role=invitation.role,
        custom_role_id=invitation.custom_role_id,
        status="active",
    )
    invitation.status = "accepted"
    db.add(member)
    db.add(
        AuditEvent(
            tenant_id=invitation.tenant_id,
            actor_user_id=user.id,
            target_user_id=user.id,
            action="membership.invitation_accepted",
            details={"invitation_id": str(invitation.id)},
        )
    )
    create_login_session(db, user, response)
    db.commit()
    return make_current_user_response(db, user)


@router.post(
    "/members/invitations/{invitation_id}/resend",
    response_model=CreatedMemberInvitationResponse,
)
def resend_member_invitation(
    invitation_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CreatedMemberInvitationResponse:
    lock_tenant(db, membership.tenant_id)
    invitation = db.scalar(
        select(MemberInvitation)
        .where(
            MemberInvitation.id == invitation_id,
            MemberInvitation.tenant_id == membership.tenant_id,
        )
        .with_for_update()
    )
    if invitation is None or invitation.status not in {"pending", "expired"}:
        raise HTTPException(status_code=404, detail="未找到可重新发送的邀请")
    previous_expiry = invitation.expires_at
    if previous_expiry.tzinfo is None:
        previous_expiry = previous_expiry.replace(tzinfo=UTC)
    if (
        previous_expiry <= datetime.now(UTC)
        and _seat_usage(db, membership.tenant_id)
        >= db.get(Tenant, membership.tenant_id).seat_limit
    ):
        raise HTTPException(
            status_code=409,
            detail="席位已满，请联系平台扩容后再重新发送邀请",
        )
    token = secrets.token_urlsafe(32)
    invitation.token_hash = hashlib.sha256(token.encode()).hexdigest()
    invitation.status = "pending"
    invitation.expires_at = datetime.now(UTC) + timedelta(days=7)
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="membership.invitation_resent",
            details={"invitation_id": str(invitation.id), "email": invitation.email},
        )
    )
    db.commit()
    return CreatedMemberInvitationResponse(
        **_invitation_response(db, invitation).model_dump(),
        invitation_url=f"{settings.app_origin}/invite?token={token}",
    )


@router.delete(
    "/members/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_member_invitation(
    invitation_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    lock_tenant(db, membership.tenant_id)
    invitation = db.scalar(
        select(MemberInvitation).where(
            MemberInvitation.id == invitation_id,
            MemberInvitation.tenant_id == membership.tenant_id,
            MemberInvitation.status == "pending",
        )
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="未找到待撤销的邀请")
    invitation.status = "revoked"
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="membership.invitation_revoked",
            details={"invitation_id": str(invitation.id), "email": invitation.email},
        )
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/members/{member_user_id}/status",
    response_model=MemberResponse,
)
def change_member_status(
    member_user_id: UUID,
    payload: ChangeMemberStatusRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MemberResponse:
    lock_tenant(db, membership.tenant_id)
    target_membership = db.scalar(
        select(Membership)
        .where(
            Membership.tenant_id == membership.tenant_id,
            Membership.user_id == member_user_id,
        )
        .with_for_update()
    )
    target_user = db.get(User, member_user_id)
    if target_membership is None or target_user is None:
        raise HTTPException(status_code=404, detail="未找到该企业成员")
    if target_membership.status == payload.status:
        return _member_response(db, target_user, target_membership)
    if payload.status == "active":
        if _seat_usage(db, membership.tenant_id) >= db.get(
            Tenant, membership.tenant_id
        ).seat_limit:
            raise HTTPException(
                status_code=409,
                detail="席位已满，请联系平台扩容后再恢复成员",
            )
    if (
        payload.status == "suspended"
        and target_membership.role == "tenant_admin"
        and target_user.is_active
    ):
        admin_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == "tenant_admin",
                Membership.status == "active",
                User.is_active.is_(True),
            )
        ) or 0
        if admin_count <= 1:
            raise HTTPException(status_code=409, detail="企业至少需要保留一名启用的管理员")

    previous_status = target_membership.status
    target_membership.status = payload.status
    if payload.status == "suspended":
        now = datetime.now(UTC)
        db.query(AuthSession).filter(
            AuthSession.user_id == target_user.id,
            AuthSession.revoked_at.is_(None),
        ).update({"revoked_at": now}, synchronize_session=False)
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            target_user_id=target_user.id,
            action=f"membership.{payload.status}",
            details={"from": previous_status, "to": payload.status},
        )
    )
    db.commit()
    return _member_response(db, target_user, target_membership)


@router.delete(
    "/members/{member_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_member(
    member_user_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    lock_tenant(db, membership.tenant_id)
    target_membership = db.scalar(
        select(Membership)
        .where(
            Membership.tenant_id == membership.tenant_id,
            Membership.user_id == member_user_id,
        )
        .with_for_update()
    )
    target_user = db.get(User, member_user_id)
    if target_membership is None or target_user is None:
        raise HTTPException(status_code=404, detail="未找到该企业成员")
    if (
        target_membership.status == "active"
        and target_membership.role == "tenant_admin"
        and target_user.is_active
    ):
        admin_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == "tenant_admin",
                Membership.status == "active",
                User.is_active.is_(True),
            )
        ) or 0
        if admin_count <= 1:
            raise HTTPException(status_code=409, detail="企业至少需要保留一名启用的管理员")

    db.query(AuthSession).filter(
        AuthSession.user_id == target_user.id,
        AuthSession.revoked_at.is_(None),
    ).update({"revoked_at": datetime.now(UTC)}, synchronize_session=False)
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            target_user_id=target_user.id,
            action="membership.removed",
            details={"email": target_user.email},
        )
    )
    db.delete(target_membership)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/members/{member_user_id}/role", response_model=MemberResponse)
def change_member_role(
    member_user_id: UUID,
    payload: ChangeRoleRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MemberResponse:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update(key_share=True)
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    target_membership = db.scalar(
        select(Membership).where(
            Membership.tenant_id == membership.tenant_id,
            Membership.user_id == member_user_id,
        )
    )
    target_user = db.get(User, member_user_id)
    if not target_membership or not target_user:
        raise HTTPException(status_code=404, detail="未找到该企业成员")
    if member_user_id == membership.user_id:
        raise HTTPException(status_code=409, detail="不能修改自己的角色")
    custom_role: TenantRole | None = None
    if payload.role == "custom":
        custom_role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == payload.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        if custom_role is None:
            raise HTTPException(status_code=404, detail="未找到可用的自定义角色")
        if custom_role.scope == "organization" and not organization_is_within_scope(
            db,
            membership.tenant_id,
            target_membership.organization_id,
            custom_role.scope_organization_id,
        ):
            raise HTTPException(status_code=409, detail="该成员不属于此角色的数据范围")
    if (
        target_user.is_active
        and target_membership.status == "active"
        and target_membership.role == "tenant_admin"
        and payload.role != "tenant_admin"
    ):
        admin_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == "tenant_admin",
                Membership.status == "active",
                User.is_active.is_(True),
            )
        )
        if admin_count <= 1:
            raise HTTPException(
                status_code=409, detail="企业至少需要保留一名管理员"
            )

    previous_role = target_membership.role
    previous_custom_role_id = target_membership.custom_role_id
    target_membership.role = payload.role
    target_membership.custom_role_id = custom_role.id if custom_role else None
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            target_user_id=target_user.id,
            action="membership.role_changed",
            details={
                "from": previous_role,
                "to": payload.role,
                "from_custom_role_id": (
                    str(previous_custom_role_id) if previous_custom_role_id else ""
                ),
                "custom_role_id": str(custom_role.id) if custom_role else "",
            },
        )
    )
    db.commit()
    return MemberResponse(
        id=target_user.id,
        email=target_user.email,
        display_name=target_user.display_name,
        role=target_membership.role,
        is_active=target_user.is_active,
        status=target_membership.status,
        organization_id=target_membership.organization_id,
        organization_name=db.get(
            Organization, target_membership.organization_id
        ).name,
        custom_role_id=target_membership.custom_role_id,
        custom_role_name=custom_role.name if custom_role else None,
    )

@router.patch(
    "/members/{member_user_id}/organization",
    response_model=MemberResponse,
)
def change_member_organization(
    member_user_id: UUID,
    payload: ChangeMemberOrganizationRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MemberResponse:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update(key_share=True)
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    target_membership = db.scalar(
        select(Membership).where(
            Membership.tenant_id == membership.tenant_id,
            Membership.user_id == member_user_id,
        )
    )
    target_user = db.get(User, member_user_id)
    if target_membership is None or target_user is None:
        raise HTTPException(status_code=404, detail="未找到该企业成员")
    organization = db.scalar(
        select(Organization).where(
            Organization.id == payload.organization_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="未找到所属部门")
    if target_membership.custom_role_id:
        assigned_role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == target_membership.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
            )
        )
        if (
            assigned_role
            and assigned_role.scope == "organization"
            and not organization_is_within_scope(
                db,
                membership.tenant_id,
                organization.id,
                assigned_role.scope_organization_id,
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="目标部门超出成员当前角色的数据范围，请先调整角色",
            )

    previous_organization_id = target_membership.organization_id
    target_membership.organization_id = organization.id
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            target_user_id=target_user.id,
            action="membership.organization_changed",
            details={
                "from": str(previous_organization_id),
                "to": str(organization.id),
            },
        )
    )
    db.commit()
    return MemberResponse(
        id=target_user.id,
        email=target_user.email,
        display_name=target_user.display_name,
        role=target_membership.role,
        is_active=target_user.is_active,
        status=target_membership.status,
        organization_id=organization.id,
        organization_name=organization.name,
        custom_role_id=target_membership.custom_role_id,
        custom_role_name=(
            assigned_role.name
            if target_membership.custom_role_id and assigned_role
            else None
        ),
    )
