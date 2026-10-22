from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.auth import (
    require_csrf,
    require_tenant_roles,
)
from beework.db import get_db
from beework.models import (
    AuditEvent,
    Membership,
    Organization,
    Tenant,
    TenantRole,
)
from beework.rbac import (
    organization_is_within_scope,
    require_tenant_permission,
)
from beework.schemas import (
    CreateOrganizationRequest,
    OrganizationListResponse,
    OrganizationResponse,
    RenameOrganizationRequest,
)

router = APIRouter()


@router.get("/organizations", response_model=OrganizationListResponse)
def list_organizations(
    membership: Membership = Depends(require_tenant_permission("organizations:view")),
    db: Session = Depends(get_db),
) -> OrganizationListResponse:
    organizations = list(
        db.scalars(
            select(Organization)
            .where(Organization.tenant_id == membership.tenant_id)
            .order_by(Organization.parent_id, Organization.name)
        )
    )
    scoped_role = (
        db.scalar(
            select(TenantRole).where(
                TenantRole.id == membership.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        if membership.custom_role_id
        else None
    )
    scope_organization_id = (
        scoped_role.scope_organization_id
        if scoped_role and scoped_role.scope == "organization"
        else None
    )
    if scope_organization_id is not None:
        organizations = [
            organization
            for organization in organizations
            if organization_is_within_scope(
                db,
                membership.tenant_id,
                organization.id,
                scope_organization_id,
            )
        ]
    organization_ids = [organization.id for organization in organizations]
    member_counts = dict(
        db.execute(
            select(Membership.organization_id, func.count(Membership.id))
            .where(Membership.organization_id.in_(organization_ids))
            .group_by(Membership.organization_id)
        ).all()
    ) if organization_ids else {}
    child_counts = dict(
        db.execute(
            select(Organization.parent_id, func.count(Organization.id))
            .where(Organization.parent_id.in_(organization_ids))
            .group_by(Organization.parent_id)
        ).all()
    ) if organization_ids else {}
    by_id = {organization.id: organization for organization in organizations}
    children: dict[UUID, list[Organization]] = {}
    roots: list[Organization] = []
    for organization in sorted(organizations, key=lambda item: item.name):
        if organization.parent_id is None:
            roots.append(organization)
        else:
            children.setdefault(organization.parent_id, []).append(organization)
    for siblings in children.values():
        siblings.sort(key=lambda item: item.name)

    def depth_of(organization: Organization) -> int:
        depth = 0
        parent_id = organization.parent_id
        while parent_id is not None:
            parent = by_id.get(parent_id)
            if parent is None:
                if scope_organization_id is not None:
                    break
                raise RuntimeError("组织树存在不属于当前租户的父节点")
            depth += 1
            parent_id = parent.parent_id
        return depth

    ordered_organizations: list[Organization] = []

    def append_subtree(organization: Organization) -> None:
        ordered_organizations.append(organization)
        for child in children.get(organization.id, []):
            append_subtree(child)

    if scope_organization_id is not None:
        for organization in organizations:
            if organization.id == scope_organization_id:
                append_subtree(organization)
    else:
        for root in roots:
            append_subtree(root)
    if scope_organization_id is None and len(roots) != 1:
        raise RuntimeError("企业必须且只能有一个根组织")

    return OrganizationListResponse(
        tenant_id=membership.tenant_id,
        organizations=[
            OrganizationResponse(
                id=organization.id,
                tenant_id=organization.tenant_id,
                parent_id=organization.parent_id,
                name=organization.name,
                depth=depth_of(organization),
                member_count=member_counts.get(organization.id, 0),
                child_count=child_counts.get(organization.id, 0),
                is_root=organization.parent_id is None,
            )
            for organization in ordered_organizations
        ],
    )


@router.post(
    "/organizations",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_organization(
    payload: CreateOrganizationRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> OrganizationResponse:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update(key_share=True)
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")

    roots = list(
        db.scalars(
            select(Organization).where(
                Organization.tenant_id == membership.tenant_id,
                Organization.parent_id.is_(None),
            )
        )
    )
    if len(roots) != 1:
        raise RuntimeError("企业必须且只能有一个根组织")
    parent_id = payload.parent_id or roots[0].id
    parent = db.scalar(
        select(Organization).where(
            Organization.id == parent_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if parent is None:
        raise HTTPException(status_code=404, detail="未找到所属部门")
    duplicate = db.scalar(
        select(Organization.id).where(
            Organization.tenant_id == membership.tenant_id,
            Organization.parent_id == parent.id,
            Organization.name == payload.name,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="同一部门下不能有重名部门")

    organization = Organization(
        tenant_id=membership.tenant_id,
        parent_id=parent.id,
        name=payload.name,
    )
    db.add(organization)
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="organization.created",
            details={"organization_id": str(organization.id), "name": organization.name},
        )
    )
    db.commit()
    return OrganizationResponse(
        id=organization.id,
        tenant_id=organization.tenant_id,
        parent_id=organization.parent_id,
        name=organization.name,
        depth=depth_of_new_organization(db, organization),
        member_count=0,
        child_count=0,
        is_root=False,
    )


@router.patch("/organizations/{organization_id}", response_model=OrganizationResponse)
def rename_organization(
    organization_id: UUID,
    payload: RenameOrganizationRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> OrganizationResponse:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update(key_share=True)
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    organization = db.scalar(
        select(Organization).where(
            Organization.id == organization_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="未找到部门")
    if organization.parent_id is None:
        raise HTTPException(
            status_code=409,
            detail="根组织名称需在企业信息中修改",
        )
    duplicate = db.scalar(
        select(Organization.id).where(
            Organization.tenant_id == membership.tenant_id,
            Organization.parent_id == organization.parent_id,
            Organization.name == payload.name,
            Organization.id != organization.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="同一部门下不能有重名部门")

    previous_name = organization.name
    organization.name = payload.name
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="organization.renamed",
            details={
                "organization_id": str(organization.id),
                "from": previous_name,
                "to": organization.name,
            },
        )
    )
    db.commit()
    return organization_response(db, organization)


@router.delete("/organizations/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_organization(
    organization_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update(key_share=True)
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    organization = db.scalar(
        select(Organization).where(
            Organization.id == organization_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="未找到部门")
    if organization.parent_id is None:
        raise HTTPException(status_code=409, detail="根组织不能删除")
    child_count = db.scalar(
        select(func.count()).select_from(Organization).where(
            Organization.parent_id == organization.id
        )
    )
    member_count = db.scalar(
        select(func.count()).select_from(Membership).where(
            Membership.organization_id == organization.id
        )
    )
    if child_count or member_count:
        raise HTTPException(
            status_code=409,
            detail="该部门仍有子部门或成员，请先迁移后再删除",
        )
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="organization.deleted",
            details={"organization_id": str(organization.id), "name": organization.name},
        )
    )
    db.delete(organization)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

def organization_response(
    db: Session, organization: Organization
) -> OrganizationResponse:
    member_count = db.scalar(
        select(func.count()).select_from(Membership).where(
            Membership.organization_id == organization.id
        )
    ) or 0
    child_count = db.scalar(
        select(func.count()).select_from(Organization).where(
            Organization.parent_id == organization.id
        )
    ) or 0
    depth = depth_of_new_organization(db, organization)
    return OrganizationResponse(
        id=organization.id,
        tenant_id=organization.tenant_id,
        parent_id=organization.parent_id,
        name=organization.name,
        depth=depth,
        member_count=member_count,
        child_count=child_count,
        is_root=organization.parent_id is None,
    )


def depth_of_new_organization(db: Session, organization: Organization) -> int:
    depth = 0
    parent_id = organization.parent_id
    while parent_id is not None:
        parent = db.get(Organization, parent_id)
        if parent is None or parent.tenant_id != organization.tenant_id:
            raise RuntimeError("组织树存在不属于当前租户的父节点")
        depth += 1
        parent_id = parent.parent_id
    return depth
