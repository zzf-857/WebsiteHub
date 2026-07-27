"""Hybrid search: keyword always, semantic when the account has a Provider.

The degradation rule is the important one and it runs in one direction only:
**semantic recall can be missing, keyword search cannot.**  No embedding
Provider, an unreachable vendor, an empty index — all of them return keyword
results, never an error.  A user who has never configured an embedding Provider
should not be able to tell this module exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from .embeddings import EmbeddingEndpoint, embed_query
from .fusion import Candidate, RankedHit, fuse
from .vectors import nearest

# How many vector hits feed the fusion.  Larger than the page size on purpose:
# fusion re-ranks, so the semantic list needs headroom to contribute.
_LOGGER = logging.getLogger(__name__)

SEMANTIC_CANDIDATE_LIMIT = 50


@dataclass(frozen=True, slots=True)
class HybridResult:
    hits: list[RankedHit]
    #: 语义召回是否真的参与了本次排序。前端据此说明「已启用语义检索」，
    #: 而不是让用户以为配了 Provider 就一定生效。
    semantic_used: bool


async def hybrid_search(
    session: AsyncSession,
    user_id: str,
    query: str,
    *,
    keyword_ids: list[str],
    candidates: list[Candidate],
    binding: EmbeddingEndpoint | None,
) -> HybridResult:
    """Fuse keyword hits with semantic recall when it is available.

    ``keyword_ids`` comes from the caller because FTS already runs inside the
    library query; re-running it here would mean two sources of truth for what
    "matches".
    """

    semantic_ids: list[str] = []
    semantic_used = False

    model = binding.model_name if binding is not None else None
    if binding is not None and model:
        # 整段兜住：``embed_query`` 已经把厂商故障塌缩成 None，但 ``nearest``
        # 打的是本地库，它抛出的 SQLAlchemyError 会一路冒到路由层变成 500——
        # 而本模块的全部承诺就是「语义可以缺席，关键词不能」。让一次向量查询
        # 失败带走整个搜索，就是把增强功能变成了单点故障。
        try:
            vector = await embed_query(binding, query)
            scored = (
                await nearest(
                    session,
                    user_id,
                    vector,
                    model=model,
                    limit=SEMANTIC_CANDIDATE_LIMIT,
                )
                if vector
                else []
            )
        except Exception as error:  # noqa: BLE001 - 语义故障必须静默降级
            _LOGGER.info("semantic recall unavailable", exc_info=error)
            scored = []
        else:
            semantic_ids = [item.site_id for item in scored]
            # Only claim semantic search ran if it actually contributed rows;
            # an empty index is indistinguishable from no Provider to the user,
            # and saying otherwise would be a lie about what they are seeing.
            semantic_used = bool(semantic_ids)

    return HybridResult(
        hits=fuse(query, keyword_ids, semantic_ids, candidates),
        semantic_used=semantic_used,
    )


__all__ = ["SEMANTIC_CANDIDATE_LIMIT", "HybridResult", "hybrid_search"]
