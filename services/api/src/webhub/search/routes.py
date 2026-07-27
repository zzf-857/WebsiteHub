"""Semantic index management endpoints.

**This is the only module under ``search/`` allowed to import ``agent``.**
``embeddings.py`` and ``backfill.py`` declare a Protocol precisely so the search
domain never points at ``agent``; resolving a Provider — reading
``provider_configs`` and decrypting a key — is composition, and composition
belongs in the route layer.  That stays safe because nothing imports a
``routes`` module except ``webhub/routes.py``: even once ``agent`` starts using
``search`` for semantic recall, no cycle can form through here.  The same
arrangement already exists in ``library/routes.py``, which imports ``ingestion``.

Every endpoint here can spend the user's money, so none of them does it
implicitly: ``GET`` reports the cost, ``POST`` acts on it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from webhub.agent.provider_binding import resolve_optional_binding
from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    require_trusted_origin,
)

from . import worker
from .backfill import index_status
from .schemas import (
    SemanticIndexRebuildRequest,
    SemanticIndexRunResponse,
    SemanticIndexStatusResponse,
)
from .vectors import drop_index

router = APIRouter(prefix="/search", tags=["search"])

# 与其他写端点一致：跨站请求不得触发花钱的操作。
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


@router.get("/index", response_model=SemanticIndexStatusResponse)
async def read_index_status(
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> SemanticIndexStatusResponse:
    """Report coverage and what a backfill would cost.

    Deliberately a plain read: a status page that quietly started indexing
    would spend the user's quota for merely looking.
    """

    user_id = str(identity.user.id)
    binding = await resolve_optional_binding(
        session,
        request.app.state.settings,
        user_id=user_id,
        kind="embedding",
    )
    status = await index_status(session, user_id, binding=binding)
    return SemanticIndexStatusResponse(
        configured=status.configured,
        model_name=status.model,
        total_sites=status.total_sites,
        indexed=status.indexed,
        pending=status.pending,
        pending_capped=status.pending_capped,
        estimated_requests=status.estimated_requests,
        running=worker.is_running(user_id),
    )


@router.post("/index/rebuild", response_model=SemanticIndexRunResponse)
async def rebuild_index(
    request: Request,
    payload: SemanticIndexRebuildRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _origin: WriteOriginDependency,
) -> SemanticIndexRunResponse:
    """Queue a backfill pass, optionally dropping the existing vectors first.

    Without an embedding Provider this reports ``scheduled=false`` rather than
    failing: the account simply has no such capability, and an error would
    imply the user broke something.
    """

    user_id = str(identity.user.id)
    binding = await resolve_optional_binding(
        session,
        request.app.state.settings,
        user_id=user_id,
        kind="embedding",
    )
    if binding is None or not binding.model_name:
        return SemanticIndexRunResponse(scheduled=False, dropped=0, estimated_requests=0)

    # 顺序要紧：先确认能排上再决定丢不丢。反过来写的话，已有一轮在跑时会
    # 「删掉了全部向量却没排上新一轮」——用户一次误点就凭空丢失整个索引。
    if worker.is_running(user_id):
        status = await index_status(session, user_id, binding=binding)
        return SemanticIndexRunResponse(
            scheduled=False,
            dropped=0,
            estimated_requests=status.estimated_requests,
        )

    dropped = await drop_index(session, user_id) if payload.drop_existing else 0
    # 预估必须在 drop 之后算：丢弃后待办数会变，先算就会低报花钱数字。
    status = await index_status(session, user_id, binding=binding)
    scheduled = worker.schedule_backfill(
        request.app.state.database,
        user_id=user_id,
        binding=binding,
        limit=payload.limit,
    )
    return SemanticIndexRunResponse(
        scheduled=scheduled,
        dropped=dropped,
        estimated_requests=status.estimated_requests,
    )


__all__ = ["router"]
