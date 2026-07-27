"""Per-account embedding storage and brute-force nearest-neighbour search.

**Why not LlamaIndex / a vector database.**  The queue proposed LlamaIndex, but
its acceptance criteria are behavioural — exact hits first, no cross-account
leakage, rebuildable from SQLite, graceful degradation — and none of them
require it.  What is actually needed is: embed with the account's own Provider
(plumbing that already exists), keep the vectors somewhere, and rank by cosine.
A personal hub holds thousands of sites, not millions; a brute-force dot
product over a few thousand vectors costs single-digit milliseconds. Adding a
large dependency tree and a second datastore to avoid that would be a worse
trade, and it would put the corpus somewhere SQLite backups do not reach.

Vectors are stored as raw little-endian float32, which is both compact and
exactly what ``array``/``struct`` round-trip without a numeric dependency.
"""

from __future__ import annotations

import array
import hashlib
import math
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Site, SiteEmbedding, utc_now

# Cap on how many rows one search scores.  A personal library is far below
# this; the bound exists so a pathological account cannot stall a request.
MAX_SCANNED_VECTORS = 20_000


@dataclass(frozen=True, slots=True)
class ScoredSite:
    site_id: str
    similarity: float


def embedding_text(name: str, description: str | None, category: str | None) -> str:
    """The text that represents a site to the embedding model.

    URL is deliberately excluded: it contributes tokens that look like content
    but carry little meaning, and bookmark URLs can hold secrets.
    """

    parts = [name.strip(), (description or "").strip(), (category or "").strip()]
    return "\n".join(part for part in parts if part)


def content_digest(text: str, model: str) -> str:
    """Digest of *what was embedded, by which model*.

    Both matter: the same text embedded by a different model yields a vector
    that must not be compared against the old ones, so a model change has to
    invalidate the cache exactly like a text change does.
    """

    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()


def pack_vector(values: list[float]) -> bytes:
    return array.array("f", values).tobytes()


def unpack_vector(payload: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(payload)
    return list(values)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Plain cosine.  Mismatched dimensions score 0 rather than raising.

    A dimension mismatch means the two vectors came from different models —
    comparing them is meaningless, but it must not take down a search.
    """

    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


async def store_embedding(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    *,
    model: str,
    vector: list[float],
    content_hash: str,
    commit: bool = True,
) -> None:
    """Upsert one vector.

    ``commit=False`` lets a backfill write a whole batch in one transaction:
    committing per row turns a 512-site pass into 512 transactions, which on
    SQLite means holding and releasing the write lock 512 times while the rest
    of the app is trying to write.
    """

    existing = await session.get(SiteEmbedding, {"user_id": user_id, "site_id": site_id})
    now = utc_now()
    if existing is None:
        session.add(
            SiteEmbedding(
                user_id=user_id,
                site_id=site_id,
                model=model,
                dimensions=len(vector),
                vector=pack_vector(vector),
                content_hash=content_hash,
                updated_at=now,
            )
        )
    else:
        existing.model = model
        existing.dimensions = len(vector)
        existing.vector = pack_vector(vector)
        existing.content_hash = content_hash
        existing.updated_at = now
    if commit:
        await session.commit()


async def stale_sites(
    session: AsyncSession,
    user_id: str,
    *,
    model: str,
    limit: int,
) -> list[tuple[str, str]]:
    """Sites whose stored vector is missing or no longer matches their text.

    Returns ``(site_id, text)``.  Re-embedding spends the user's quota, so an
    unchanged site is never re-sent — that is the whole point of the digest.
    """

    rows = (
        await session.execute(
            select(Site, SiteEmbedding)
            .join(
                SiteEmbedding,
                (SiteEmbedding.user_id == Site.user_id) & (SiteEmbedding.site_id == Site.id),
                isouter=True,
            )
            .where(Site.user_id == user_id)
            .order_by(Site.updated_at.desc(), Site.id)
            .limit(MAX_SCANNED_VECTORS)
        )
    ).all()

    pending: list[tuple[str, str]] = []
    for site, embedding in rows:
        text = embedding_text(site.name, site.description, None)
        if not text:
            continue
        if embedding is not None and embedding.content_hash == content_digest(text, model):
            continue
        pending.append((site.id, text))
        if len(pending) >= limit:
            break
    return pending


async def has_embeddings(session: AsyncSession, user_id: str) -> bool:
    """Whether this account has any vector at all.

    Exists to keep the search path cheap.  Resolving an embedding Provider
    re-resolves its hostname through ``validate_connection_target`` — a real
    DNS lookup with a multi-second timeout, which is the right price for an SSRF
    guarantee but the wrong price to pay on every keystroke of a search box.
    An account with no vectors cannot get anything back from semantic recall,
    so the whole resolution is skipped rather than made unsafe.
    """

    found = await session.scalar(
        select(SiteEmbedding.site_id).where(SiteEmbedding.user_id == user_id).limit(1)
    )
    return found is not None


async def nearest(
    session: AsyncSession,
    user_id: str,
    query_vector: list[float],
    *,
    model: str,
    limit: int,
) -> list[ScoredSite]:
    """Rank this account's vectors against the query vector.

    Account scoping is in the ``WHERE`` clause, not applied afterwards: a
    filter that runs after scoring is one refactor away from leaking another
    account's corpus into the ranking.
    """

    if not query_vector:
        return []
    rows = (
        await session.scalars(
            select(SiteEmbedding)
            .where(SiteEmbedding.user_id == user_id, SiteEmbedding.model == model)
            .limit(MAX_SCANNED_VECTORS)
        )
    ).all()

    scored = [
        ScoredSite(row.site_id, cosine_similarity(query_vector, unpack_vector(row.vector)))
        for row in rows
    ]
    # Drop non-positive similarity: those are unrelated or from a mismatched
    # model, and padding the result list with them only dilutes the ranking.
    scored = [item for item in scored if item.similarity > 0.0]
    scored.sort(key=lambda item: (-item.similarity, item.site_id))
    return scored[:limit]


async def drop_index(session: AsyncSession, user_id: str) -> int:
    """Delete this account's vectors.

    Rebuilding from SQLite is always possible because the source of truth is
    the ``sites`` table — the vectors are a derived cache, never the record.
    """

    result = await session.execute(
        delete(SiteEmbedding).where(SiteEmbedding.user_id == user_id)
    )
    await session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


__all__ = [
    "MAX_SCANNED_VECTORS",
    "ScoredSite",
    "content_digest",
    "cosine_similarity",
    "drop_index",
    "embedding_text",
    "has_embeddings",
    "nearest",
    "pack_vector",
    "stale_sites",
    "store_embedding",
    "unpack_vector",
]
