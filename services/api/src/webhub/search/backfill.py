"""Fill in missing site embeddings using the account's own Provider.

``search/vectors.py`` can store and rank vectors; ``search/embeddings.py`` can
produce them.  Nothing called either one — this module is the missing middle.

**Why it takes an ``EmbeddingEndpoint`` instead of resolving one.** Resolving a
Provider means reading ``provider_configs`` and decrypting a key, which lives in
``agent.provider_binding``.  Importing that here would point ``search`` at
``agent``, the exact direction ``embeddings.py`` declared a Protocol to avoid.
The caller — a route, which is the composition layer — resolves the binding and
passes it in.

**Spending the user's quota is the whole risk here**, so two properties are
structural rather than remembered:

* Nothing is re-embedded.  ``stale_sites`` only returns sites whose stored
  digest no longer matches their text, so a second run over an unchanged library
  makes zero vendor calls.
* A batch that fails is skipped, not retried in a loop.  A vendor that is down
  would otherwise burn the whole quota on retries; the next run picks the sites
  up again because their digest still does not match.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Site, SiteEmbedding

from .embeddings import MAX_BATCH_INPUTS, EmbeddingEndpoint, embed_texts
from .vectors import content_digest, embedding_text, stale_sites, store_embedding

_LOGGER = logging.getLogger(__name__)

# How many sites one backfill pass will embed.  A pass is bounded so a large
# library turns into several short runs rather than one long one holding a
# session open — and so an accidental trigger cannot spend an unbounded amount.
MAX_SITES_PER_PASS = 512


@dataclass(frozen=True, slots=True)
class IndexStatus:
    """What the user needs to see before agreeing to spend their quota."""

    #: 该账号的站点总数。
    total_sites: int
    #: 已有可用向量的站点数（模型匹配且摘要未变）。
    indexed: int
    #: 还需要嵌入的站点数（**受单轮上限截断**，见 pending_capped）。
    pending: int
    #: pending 是否被单轮上限截断。为 true 时说明这一轮跑完还有剩，
    #: 界面必须说清「本轮」而不是「全部」——否则用户按一个偏小的数字
    #: 点了确认，实际要跑好几轮才完。
    pending_capped: bool
    #: 本次回填预计要发出的厂商请求数。**这是给用户看的花钱数字。**
    estimated_requests: int
    #: 「全部重建」覆盖全库预计发出的请求数；与当前缺失量无关。
    rebuild_estimated_requests: int
    #: 「全部重建」第一轮实际会处理的站点数。
    rebuild_pass_sites: int
    #: 「全部重建」第一轮实际会发出的请求数。
    rebuild_pass_estimated_requests: int
    #: 全库是否超过单轮处理上限，需要后续继续补齐。
    rebuild_capped: bool
    #: 单轮最多处理的站点数。由后端下发，前端不得复制常量。
    pass_limit: int
    #: 当前绑定的模型名；未配 Provider 时为 None。
    model: str | None

    @property
    def configured(self) -> bool:
        return self.model is not None


@dataclass(frozen=True, slots=True)
class BackfillResult:
    embedded: int
    failed_batches: int
    #: 本次实际发出的厂商请求数，用于和预估对账。
    requests: int


async def index_status(
    session: AsyncSession,
    user_id: str,
    *,
    binding: EmbeddingEndpoint | None,
) -> IndexStatus:
    """Count what is indexed and what a backfill would cost.

    ``pending`` is computed with the same ``stale_sites`` query the backfill
    uses, not an approximation: a number that disagrees with what the job
    actually does would make the cost estimate a lie.
    """

    total = int(
        await session.scalar(select(func.count()).select_from(Site).where(Site.user_id == user_id))
        or 0
    )
    rebuild_pass_sites = min(total, MAX_SITES_PER_PASS)
    rebuild_estimated_requests = -(-total // MAX_BATCH_INPUTS)
    rebuild_pass_estimated_requests = -(-rebuild_pass_sites // MAX_BATCH_INPUTS)
    model = binding.model_name if binding is not None else None
    if binding is None or not model:
        # 未配 Provider 时不谎报「全部待索引」：一次也不会发，pending 就是 0。
        return IndexStatus(
            total_sites=total,
            indexed=0,
            pending=0,
            pending_capped=False,
            estimated_requests=0,
            rebuild_estimated_requests=rebuild_estimated_requests,
            rebuild_pass_sites=rebuild_pass_sites,
            rebuild_pass_estimated_requests=rebuild_pass_estimated_requests,
            rebuild_capped=total > MAX_SITES_PER_PASS,
            pass_limit=MAX_SITES_PER_PASS,
            model=None,
        )

    # A row is usable only when both its model and digest still match the
    # current site text. Counting rows by model alone makes an edited site show
    # up in both "indexed" and "pending" until the next paid backfill.
    embedding_rows = (
        await session.execute(
            select(
                Site.name,
                Site.description,
                SiteEmbedding.model,
                SiteEmbedding.content_hash,
            )
            .join(
                SiteEmbedding,
                (SiteEmbedding.user_id == Site.user_id)
                & (SiteEmbedding.site_id == Site.id),
                isouter=True,
            )
            .where(Site.user_id == user_id)
        )
    ).all()
    indexed = sum(
        1
        for name, description, embedding_model, content_hash in embedding_rows
        if embedding_model == model
        and content_hash == content_digest(embedding_text(name, description, None), model)
    )
    # 多取一个用来判断是否还有剩，而不是让 pending == 上限时无法区分
    # 「正好这么多」和「还有很多」。
    probe = await stale_sites(session, user_id, model=model, limit=MAX_SITES_PER_PASS + 1)
    capped = len(probe) > MAX_SITES_PER_PASS
    pending = min(len(probe), MAX_SITES_PER_PASS)
    return IndexStatus(
        total_sites=total,
        indexed=indexed,
        pending=pending,
        pending_capped=capped,
        # 向上取整：63 个站点也要发一次。
        estimated_requests=-(-pending // MAX_BATCH_INPUTS),
        rebuild_estimated_requests=rebuild_estimated_requests,
        rebuild_pass_sites=rebuild_pass_sites,
        rebuild_pass_estimated_requests=rebuild_pass_estimated_requests,
        rebuild_capped=total > MAX_SITES_PER_PASS,
        pass_limit=MAX_SITES_PER_PASS,
        model=model,
    )


async def backfill_embeddings(
    session: AsyncSession,
    user_id: str,
    *,
    binding: EmbeddingEndpoint,
    limit: int = MAX_SITES_PER_PASS,
) -> BackfillResult:
    """Embed this account's stale sites in batches.

    Returns counts rather than raising: the caller is a background task, and a
    Provider outage is not a user-visible failure — semantic recall degrades,
    keyword search keeps working.
    """

    model = binding.model_name
    if not model:
        return BackfillResult(embedded=0, failed_batches=0, requests=0)

    pending = await stale_sites(session, user_id, model=model, limit=limit)
    embedded = 0
    failed = 0
    requests = 0
    for start in range(0, len(pending), MAX_BATCH_INPUTS):
        batch = pending[start : start + MAX_BATCH_INPUTS]
        requests += 1
        vectors = await embed_texts(binding, [text for _site_id, text in batch])
        if vectors is None:
            # 整批失败就跳过，不重试。厂商挂了的话重试会把额度烧光，
            # 而这些站点的摘要仍不匹配，下一次回填自然会再取到它们。
            failed += 1
            continue
        for (site_id, text), vector in zip(batch, vectors, strict=True):
            await store_embedding(
                session,
                user_id,
                site_id,
                model=model,
                vector=vector,
                content_hash=content_digest(text, model),
                commit=False,
            )
            embedded += 1
        # 一批一次事务，而不是一行一次：SQLite 下 512 次提交会反复抢写锁。
        await session.commit()
    if failed:
        _LOGGER.info("embedding backfill skipped %s batch(es) for %s", failed, user_id)
    return BackfillResult(embedded=embedded, failed_batches=failed, requests=requests)


__all__ = [
    "MAX_SITES_PER_PASS",
    "BackfillResult",
    "IndexStatus",
    "backfill_embeddings",
    "index_status",
]
