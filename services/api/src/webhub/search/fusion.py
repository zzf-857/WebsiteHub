"""Rank keyword and semantic hits into one ordered result list.

The rule the queue is explicit about: **an exact hit must come first.**  A user
who types a site's actual name is not asking to explore — they know what they
want and semantic recall must not bury it under things that are merely
*related*.  Everything else here is subordinate to that.

Pure functions on purpose: ranking is where a retrieval system is most likely
to be subtly wrong, and it is testable without a model, a database, or a
network.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

# Reciprocal-rank-fusion constant.  60 is the value from the original RRF paper
# and is deliberately large relative to typical result counts: it flattens the
# difference between rank 1 and rank 2 so a single list cannot dominate.
RRF_K = 60


def normalize_for_match(value: str) -> str:
    """Fold a string the way exact-match comparison needs it.

    NFKC + casefold + whitespace collapse, matching how the library normalizes
    names on write, so "GitHub" / "ｇｉｔｈｕｂ" / " github " all compare equal.
    """

    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


@dataclass(frozen=True, slots=True)
class Candidate:
    site_id: str
    name: str
    identity_url: str = ""


@dataclass(frozen=True, slots=True)
class RankedHit:
    site_id: str
    score: float
    exact: bool
    keyword_rank: int | None
    semantic_rank: int | None

    @property
    def sources(self) -> tuple[str, ...]:
        found: list[str] = []
        if self.keyword_rank is not None:
            found.append("keyword")
        if self.semantic_rank is not None:
            found.append("semantic")
        return tuple(found)


def exact_site_ids(query: str, candidates: Sequence[Candidate]) -> set[str]:
    """Sites whose name or URL *is* the query, not merely contains it.

    Substring matching is deliberately excluded: "git" appearing inside
    "GitHub" is a keyword hit, not an exact one, and promoting it would let a
    short query hijack the top slot from a genuine exact match.
    """

    needle = normalize_for_match(query)
    if not needle:
        return set()
    matched: set[str] = set()
    for candidate in candidates:
        if normalize_for_match(candidate.name) == needle:
            matched.add(candidate.site_id)
            continue
        url = normalize_for_match(candidate.identity_url)
        if not url:
            continue
        # 「github.com」应当命中「https://github.com」：用户很少连协议头一起打。
        bare = url.removeprefix("https://").removeprefix("http://")
        if needle in {url, bare}:
            matched.add(candidate.site_id)
    return matched


def fuse(
    query: str,
    keyword_ids: Sequence[str],
    semantic_ids: Sequence[str],
    candidates: Sequence[Candidate],
) -> list[RankedHit]:
    """Merge two ranked lists, exact hits first.

    Reciprocal rank fusion is used for the rest because the two lists have
    incomparable scores — an FTS rank and a cosine similarity cannot be added
    together meaningfully, but their *positions* can.

    Ties break on the keyword list's order, then on site id, so the same inputs
    always produce the same output.  A search that reshuffles equal-scoring rows
    between refreshes reads as broken even when it is not.
    """

    exact = exact_site_ids(query, candidates)
    keyword_positions = {site_id: index for index, site_id in enumerate(keyword_ids)}
    semantic_positions = {site_id: index for index, site_id in enumerate(semantic_ids)}

    hits: list[RankedHit] = []
    for site_id in {*keyword_ids, *semantic_ids}:
        keyword_rank = keyword_positions.get(site_id)
        semantic_rank = semantic_positions.get(site_id)
        score = 0.0
        if keyword_rank is not None:
            score += 1.0 / (RRF_K + keyword_rank + 1)
        if semantic_rank is not None:
            score += 1.0 / (RRF_K + semantic_rank + 1)
        hits.append(
            RankedHit(
                site_id=site_id,
                score=score,
                exact=site_id in exact,
                keyword_rank=keyword_rank,
                semantic_rank=semantic_rank,
            )
        )

    hits.sort(
        key=lambda hit: (
            # Exact first, then fused score, then a stable tiebreak.
            not hit.exact,
            -hit.score,
            keyword_positions.get(hit.site_id, len(keyword_positions)),
            hit.site_id,
        )
    )
    return hits


__all__ = [
    "RRF_K",
    "Candidate",
    "RankedHit",
    "exact_site_ids",
    "fuse",
    "normalize_for_match",
]
