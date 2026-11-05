"""Uploaded files on a local (or mounted) directory, addressed by tenant / knowledge base / document ids only.

User-supplied names never reach the file system: the key is built from UUIDs, and every path is checked to stay inside
the storage root. Swap this module for an object store when deploying across hosts.
"""

from pathlib import Path
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session

from beework.config import settings


def _root() -> Path:
    root = Path(settings.file_storage_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_key(tenant_id: UUID, kb_id: UUID, document_id: UUID) -> str:
    return f"{tenant_id}/{kb_id}/{document_id}"


def _path(key: str) -> Path:
    root = _root()
    path = (root / key).resolve()
    if root != path and root not in path.parents:
        raise ValueError("file key escapes the storage root")
    return path


def save(key: str, data: bytes) -> None:
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)  # readers never see a half-written file


def read(key: str) -> bytes:
    return _path(key).read_bytes()


def exists(key: str) -> bool:
    return _path(key).is_file()


def delete(key: str) -> None:
    _path(key).unlink(missing_ok=True)


def delete_after_commit(db: Session, key: str) -> None:
    """Remove the file only once the transaction that drops its row has committed: a rollback must not leave a
    document row whose original is already gone."""
    db.info.setdefault(_PENDING, []).append(key)


_PENDING = "file_store.pending_deletes"


@event.listens_for(Session, "after_commit")
def _delete_committed(session: Session) -> None:
    for key in session.info.pop(_PENDING, []):
        try:
            delete(key)
        except OSError:  # an undeletable leftover is not worth failing a committed request
            pass


@event.listens_for(Session, "after_rollback")
def _forget_rolled_back(session: Session) -> None:
    session.info.pop(_PENDING, None)
