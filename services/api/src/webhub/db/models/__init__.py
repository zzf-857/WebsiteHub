"""ORM 模型。

原本是单文件 1227 行、26 个模型类平铺。按领域拆成子模块后，本模块只做门面：
调用方沿用 ``from webhub.db.models import X``，34 处 import 一个字都不用改。

**子模块必须在这里全部导入**，否则 SQLAlchemy 的 mapper 配置和 Alembic 的
``Base.metadata`` 会缺表——这不是风格问题，是漏一个就静默少建表。
"""

from __future__ import annotations

from webhub.db.models._base import (
    DEFAULT_CATEGORY_NAME,
    Base,
    new_id,
    utc_now,
)
from webhub.db.models.accounts import (
    LoginSession,
    ProviderConfig,
    User,
    UserPreference,
)
from webhub.db.models.bookmark_jobs import (
    BookmarkImportCheckpoint,
    BookmarkImportCurrentRun,
    BookmarkImportJob,
    BookmarkImportRun,
    BookmarkImportSnapshot,
)
from webhub.db.models.bookmark_source import (
    BookmarkSourceFolder,
    BookmarkSourceOccurrence,
)
from webhub.db.models.bookmark_staging import (
    BookmarkStagingCandidate,
    BookmarkStagingCandidateFolder,
    BookmarkStagingCandidateOccurrence,
    BookmarkStagingCandidateSiteMatch,
    BookmarkStagingFolder,
    BookmarkStagingOccurrence,
)
from webhub.db.models.library import (
    Category,
    Site,
    SiteEmbedding,
    SiteImportOrigin,
    SiteTag,
    Tag,
)
from webhub.db.models.metadata_backfill import (
    SiteMetadataBackfillItem,
    SiteMetadataBackfillRun,
)
from webhub.db.models.site_metadata_preferences import SiteMetadataPreference
from webhub.db.models.spaces import (
    Space,
    SpaceBatchOperationReceipt,
    SpaceMember,
)

__all__ = [
    "Base",
    "BookmarkImportCheckpoint",
    "BookmarkImportCurrentRun",
    "BookmarkImportJob",
    "BookmarkImportRun",
    "BookmarkImportSnapshot",
    "BookmarkSourceFolder",
    "BookmarkSourceOccurrence",
    "BookmarkStagingCandidate",
    "BookmarkStagingCandidateFolder",
    "BookmarkStagingCandidateOccurrence",
    "BookmarkStagingCandidateSiteMatch",
    "BookmarkStagingFolder",
    "BookmarkStagingOccurrence",
    "Category",
    "DEFAULT_CATEGORY_NAME",
    "LoginSession",
    "ProviderConfig",
    "Site",
    "SiteEmbedding",
    "SiteImportOrigin",
    "SiteMetadataBackfillItem",
    "SiteMetadataBackfillRun",
    "SiteMetadataPreference",
    "SiteTag",
    "Space",
    "SpaceBatchOperationReceipt",
    "SpaceMember",
    "Tag",
    "User",
    "UserPreference",
    "new_id",
    "utc_now",
]
