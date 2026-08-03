"""Conservative, token-free suggestions for same-site bookmark cleanup."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid5

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    BookmarkSimilarityCluster,
    BookmarkSimilarityClusterMember,
    BookmarkSimilarityDecisionState,
    BookmarkStagingCandidate,
    Site,
)

from .privacy import sensitive_url_keys

SIMILARITY_RULESET_VERSION = "bookmark-similarity.v1"
_CLUSTER_ID_NAMESPACE = UUID("7610b61d-300e-4d74-b6f8-fbe5eafe219c")
_ELIGIBLE_ACTIONS = frozenset({"create", "skip_existing", "merge_missing_metadata"})

# These authorities host unrelated users or documents. Treating every path on
# them as one website creates far more destructive suggestions than useful
# ones. More specific, provider-aware scopes can be added later without
# weakening this conservative first release.
_SHARED_CONTENT_AUTHORITIES = frozenset(
    {
        "bilibili.com",
        "docs.google.com",
        "drive.google.com",
        "facebook.com",
        "figma.com",
        "gitee.com",
        "github.com",
        "gitlab.com",
        "google.com",
        "linkedin.com",
        "medium.com",
        "notion.so",
        "reddit.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "youtu.be",
        "zhihu.com",
    }
)


@dataclass(frozen=True, slots=True)
class SimilarityCandidate:
    id: str
    identity_url: str
    title: str
    host: str
    fetch_policy: str
    has_sensitive_url: bool
    proposed_action: str
    occurrence_count: int
    first_source_sequence: int


@dataclass(frozen=True, slots=True)
class ExistingHomepage:
    identity_url: str
    title: str


@dataclass(frozen=True, slots=True)
class SimilarityProjection:
    site_key: str
    display_host: str
    canonical_candidate_id: str | None
    canonical_url: str
    canonical_title: str
    canonical_source: str
    confidence: str
    reason_codes: tuple[str, ...]
    candidate_count: int
    occurrence_count: int
    keep_original_create_count: int
    merge_create_count: int
    first_source_sequence: int
    members: tuple[SimilarityCandidate, ...]


def _normalized_hostname(value: str) -> str | None:
    hostname = value.rstrip(".").casefold()
    if not hostname:
        return None
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def site_key_for_url(url: str) -> str | None:
    """Return a same-authority key without guessing a public suffix.

    Only an exact ``www.`` alias is folded. Other subdomains remain separate,
    and every non-default port remains part of the key.
    """

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = _normalized_hostname(parts.hostname or "")
    if host is None:
        return None
    comparable = host.removeprefix("www.")
    if port is None:
        return comparable
    host_display = f"[{comparable}]" if ":" in comparable else comparable
    return f"{host_display}:{port}"


def is_shared_content_site_key(site_key: str) -> bool:
    """Return whether authority-wide grouping would cross unrelated content owners."""

    return site_key in _SHARED_CONTENT_AUTHORITIES


def safe_display_url(url: str) -> str:
    """Redact sensitive query/fragment values before a URL reaches the DOM."""

    sensitive = set(sensitive_url_keys(url))
    if not sensitive:
        return url
    parts = urlsplit(url)

    def redact(payload: str) -> str:
        values = []
        for key, value in parse_qsl(payload, keep_blank_values=True):
            normalized_key = key.strip().casefold().replace("-", "_").replace(".", "_")
            values.append((key, "[hidden]" if normalized_key in sensitive else value))
        return urlencode(values, doseq=True)

    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, redact(parts.query), redact(parts.fragment))
    )


def _is_clean_root(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path in {"", "/"} and not parts.query and not parts.fragment


def _path_depth(path: str) -> int:
    return len(tuple(part for part in path.split("/") if part))


def _is_path_ancestor(candidate_path: str, other_path: str) -> bool:
    normalized = candidate_path.rstrip("/") or "/"
    if normalized == "/":
        return True
    return other_path == normalized or other_path.startswith(f"{normalized}/")


def _clean_title(value: str, fallback: str) -> str:
    title = " ".join(value.split())[:160]
    return title or fallback[:160]


def _derived_root(members: tuple[SimilarityCandidate, ...]) -> str:
    authorities: Counter[tuple[str, str]] = Counter()
    first_sequence: dict[tuple[str, str], int] = {}
    for member in members:
        parts = urlsplit(member.identity_url)
        key = (parts.scheme.casefold(), parts.netloc)
        authorities[key] += 1
        first_sequence[key] = min(
            first_sequence.get(key, member.first_source_sequence),
            member.first_source_sequence,
        )
    scheme, netloc = min(
        authorities,
        key=lambda item: (
            -authorities[item],
            first_sequence[item],
            item[0],
            item[1],
        ),
    )
    return urlunsplit((scheme, netloc, "/", "", ""))


def _imported_canonical(
    members: tuple[SimilarityCandidate, ...],
) -> SimilarityCandidate | None:
    clean = [
        member
        for member in members
        if not urlsplit(member.identity_url).query
        and not urlsplit(member.identity_url).fragment
    ]
    roots = [member for member in clean if _is_clean_root(member.identity_url)]
    if roots:
        return min(
            roots,
            key=lambda item: (
                urlsplit(item.identity_url).scheme.casefold() != "https",
                item.first_source_sequence,
                item.id,
            ),
        )

    paths = {member.id: urlsplit(member.identity_url).path or "/" for member in members}
    ancestors = [
        member
        for member in clean
        if all(_is_path_ancestor(paths[member.id], other_path) for other_path in paths.values())
    ]
    if not ancestors:
        return None
    return min(
        ancestors,
        key=lambda item: (
            _path_depth(paths[item.id]),
            item.first_source_sequence,
            item.id,
        ),
    )


def _projection(
    site_key: str,
    raw_members: list[SimilarityCandidate],
    existing_homepage: ExistingHomepage | None,
) -> SimilarityProjection:
    members = tuple(sorted(raw_members, key=lambda item: (item.first_source_sequence, item.id)))
    imported = _imported_canonical(members)
    hosts = {member.host.casefold() for member in members}
    schemes = {urlsplit(member.identity_url).scheme.casefold() for member in members}
    paths = [urlsplit(member.identity_url).path or "/" for member in members]
    reason_codes = ["same_site_authority"]
    if len({host.removeprefix("www.") for host in hosts}) == 1 and len(hosts) > 1:
        reason_codes.append("www_alias")
    if len(schemes) > 1:
        reason_codes.append("http_https_variants")
    if len(set(paths)) < len(paths):
        reason_codes.append("shared_path_variants")

    if existing_homepage is not None:
        canonical_url = existing_homepage.identity_url
        canonical_title = _clean_title(existing_homepage.title, site_key)
        canonical_candidate_id = None
        canonical_source = "existing_library"
        confidence = "high"
        reason_codes.append("existing_library_homepage")
    elif imported is not None:
        canonical_url = imported.identity_url
        canonical_title = _clean_title(imported.title, imported.host)
        canonical_candidate_id = imported.id
        canonical_source = "imported_homepage"
        imported_path = urlsplit(imported.identity_url).path or "/"
        if imported_path == "/":
            confidence = "high"
            reason_codes.append("homepage_and_subpages")
        else:
            confidence = "medium"
            reason_codes.append("common_path_ancestor")
    else:
        canonical_url = _derived_root(members)
        canonical_title = _clean_title("", urlsplit(canonical_url).hostname or site_key)
        canonical_candidate_id = None
        canonical_source = "derived_origin_root"
        confidence = "low"
        reason_codes.append("derived_origin_root")

    create_count = sum(member.proposed_action == "create" for member in members)
    canonical_is_existing = existing_homepage is not None or any(
        member.identity_url == canonical_url and member.proposed_action == "skip_existing"
        for member in members
    )
    merge_create_count = 0 if canonical_is_existing else int(create_count > 0)
    return SimilarityProjection(
        site_key=site_key,
        display_host=urlsplit(canonical_url).netloc,
        canonical_candidate_id=canonical_candidate_id,
        canonical_url=canonical_url,
        canonical_title=canonical_title,
        canonical_source=canonical_source,
        confidence=confidence,
        reason_codes=tuple(reason_codes),
        candidate_count=len(members),
        occurrence_count=sum(member.occurrence_count for member in members),
        keep_original_create_count=create_count,
        merge_create_count=merge_create_count,
        first_source_sequence=members[0].first_source_sequence,
        members=members,
    )


def detect_similarity_clusters(
    candidates: Iterable[SimilarityCandidate],
    *,
    existing_homepages: Mapping[str, ExistingHomepage] | None = None,
) -> tuple[SimilarityProjection, ...]:
    """Group candidates in O(n) without embeddings, network I/O, or pairwise scans."""

    groups: defaultdict[str, list[SimilarityCandidate]] = defaultdict(list)
    for candidate in candidates:
        if (
            candidate.proposed_action not in _ELIGIBLE_ACTIONS
            or candidate.fetch_policy != "public_revalidation_required"
            or candidate.has_sensitive_url
        ):
            continue
        site_key = site_key_for_url(candidate.identity_url)
        if site_key is None or site_key in _SHARED_CONTENT_AUTHORITIES:
            continue
        groups[site_key].append(candidate)

    homepages = existing_homepages or {}
    projections = [
        _projection(site_key, members, homepages.get(site_key))
        for site_key, members in groups.items()
        if len(members) >= 2
    ]
    return tuple(
        sorted(projections, key=lambda item: (item.first_source_sequence, item.site_key))
    )


def _cluster_id(run_id: str, site_key: str) -> str:
    return str(uuid5(_CLUSTER_ID_NAMESPACE, f"{run_id}:{site_key}"))


async def rebuild_similarity_projections(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
) -> int:
    """Rebuild the immutable projection while the parse run is finalizing."""

    await session.execute(
        delete(BookmarkSimilarityDecisionState).where(
            BookmarkSimilarityDecisionState.user_id == user_id,
            BookmarkSimilarityDecisionState.run_id == run_id,
        )
    )
    await session.execute(
        delete(BookmarkSimilarityCluster).where(
            BookmarkSimilarityCluster.user_id == user_id,
            BookmarkSimilarityCluster.run_id == run_id,
        )
    )

    candidate_rows = (
        await session.execute(
            select(
                BookmarkStagingCandidate.id,
                BookmarkStagingCandidate.identity_url,
                BookmarkStagingCandidate.display_title,
                BookmarkStagingCandidate.host,
                BookmarkStagingCandidate.fetch_policy,
                BookmarkStagingCandidate.has_sensitive_url,
                BookmarkStagingCandidate.proposed_action,
                BookmarkStagingCandidate.occurrence_count,
                BookmarkStagingCandidate.first_source_sequence,
            ).where(
                BookmarkStagingCandidate.user_id == user_id,
                BookmarkStagingCandidate.run_id == run_id,
            )
        )
    ).all()
    candidates = tuple(SimilarityCandidate(*row) for row in candidate_rows)

    existing_homepages: dict[str, ExistingHomepage] = {}
    site_rows = (
        await session.execute(
            select(Site.identity_url, Site.name)
            .where(Site.user_id == user_id)
            .order_by(Site.created_at, Site.id)
        )
    ).all()
    for identity_url, title in site_rows:
        if not _is_clean_root(identity_url):
            continue
        site_key = site_key_for_url(identity_url)
        if site_key is not None and site_key not in existing_homepages:
            existing_homepages[site_key] = ExistingHomepage(identity_url, title)

    projections = detect_similarity_clusters(
        candidates,
        existing_homepages=existing_homepages,
    )
    for projection in projections:
        cluster_id = _cluster_id(run_id, projection.site_key)
        session.add(
            BookmarkSimilarityCluster(
                id=cluster_id,
                user_id=user_id,
                run_id=run_id,
                site_key=projection.site_key,
                ruleset_version=SIMILARITY_RULESET_VERSION,
                display_host=projection.display_host,
                canonical_candidate_id=projection.canonical_candidate_id,
                canonical_url=projection.canonical_url,
                canonical_title=projection.canonical_title,
                canonical_source=projection.canonical_source,
                confidence=projection.confidence,
                reason_codes_json=json.dumps(
                    projection.reason_codes,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                candidate_count=projection.candidate_count,
                occurrence_count=projection.occurrence_count,
                keep_original_create_count=projection.keep_original_create_count,
                merge_create_count=projection.merge_create_count,
                first_source_sequence=projection.first_source_sequence,
            )
        )
        for member in projection.members:
            session.add(
                BookmarkSimilarityClusterMember(
                    user_id=user_id,
                    run_id=run_id,
                    cluster_id=cluster_id,
                    candidate_id=member.id,
                    first_source_sequence=member.first_source_sequence,
                    is_canonical=member.id == projection.canonical_candidate_id,
                )
            )

    session.add(
        BookmarkSimilarityDecisionState(
            user_id=user_id,
            run_id=run_id,
            job_id=job_id,
            version=1,
        )
    )
    await session.flush()
    return len(projections)


__all__ = [
    "SIMILARITY_RULESET_VERSION",
    "ExistingHomepage",
    "SimilarityCandidate",
    "SimilarityProjection",
    "detect_similarity_clusters",
    "is_shared_content_site_key",
    "rebuild_similarity_projections",
    "safe_display_url",
    "site_key_for_url",
]
