"""书签导入的持久化层。

原本是单文件 1864 行，混着四类关注点，按它们拆成子模块：

- ``_common``  异常、结果 dataclass、摘要与归属校验等公用件
- ``jobs``     导入任务的创建、排队、同源查找
- ``chunks``   解析分片的追加与断点续传
- ``staging``  暂存区校验、候选投影重建、预览摘要
- ``runs``     解析运行的开始、失败、恢复
- ``finalize`` 解析运行的收尾

本模块只做门面。调用方一律 ``from webhub.bookmarks import persistence`` 再用
``persistence.X``，拆分前后这个用法一个字都不用改——包括测试用到的那几个
下划线开头的名字，它们也在这里重新导出，免得为了拆文件去改测试。
"""

from __future__ import annotations

# 原单文件把它转手再导出，调用方用的是 ``persistence.NORMALIZER_VERSION``。
# 拆分不该改变这一点，所以门面继续转出去。
from webhub.bookmarks.normalization import NORMALIZER_VERSION

from ._common import (
    _SHA256,
    _STABLE_ID_NAMESPACE,
    MAX_STAGE_EVENTS,
    SKILL_VERSION,
    BookmarkPersistenceConflictError,
    BookmarkPersistenceError,
    BookmarkPersistenceNotFoundError,
    BookmarkPersistenceValidationError,
    ImportJobResult,
    ParseCompletion,
    ParsePreviewSummary,
    ParseRunResult,
    SameSourceResult,
    StageChunkResult,
    _assert_run_versions,
    _display_filename,
    _event_batch_hash,
    _event_payload,
    _is_database_busy,
    _job_result,
    _key_hash,
    _owned_import,
    _owned_job,
    _owned_run,
    _parse_chunk_checkpoint,
    _parse_chunk_replay,
    _parse_completion_hash,
    _parse_run_replay,
    _sha256,
    _stable_id,
    _validate_digest,
    is_database_storage_exhausted,
)
from .chunks import (
    _validate_event_batch,
    append_parse_chunk,
)
from .finalize import (
    finalize_parse_run,
    recover_finalizing_parse_run,
)
from .jobs import (
    create_import,
    find_same_source,
    queue_import_for_parse,
)
from .runs import (
    _completed_parse_preview_replay,
    _failed_parse_run_replay,
    _release_parse_run_seal,
    begin_parse_run,
    fail_parse_run,
)
from .staging import (
    _rebuild_candidate_projections,
    _staged_completion,
    _validate_complete_staging,
    get_current_preview_summary,
)

__all__ = [
    "NORMALIZER_VERSION",
    "BookmarkPersistenceConflictError",
    "BookmarkPersistenceError",
    "BookmarkPersistenceNotFoundError",
    "BookmarkPersistenceValidationError",
    "ImportJobResult",
    "MAX_STAGE_EVENTS",
    "ParseCompletion",
    "ParsePreviewSummary",
    "ParseRunResult",
    "SKILL_VERSION",
    "SameSourceResult",
    "StageChunkResult",
    "_SHA256",
    "_STABLE_ID_NAMESPACE",
    "_assert_run_versions",
    "_completed_parse_preview_replay",
    "_display_filename",
    "_event_batch_hash",
    "_event_payload",
    "_failed_parse_run_replay",
    "_is_database_busy",
    "_job_result",
    "_key_hash",
    "_owned_import",
    "_owned_job",
    "_owned_run",
    "_parse_chunk_checkpoint",
    "_parse_chunk_replay",
    "_parse_completion_hash",
    "_parse_run_replay",
    "_rebuild_candidate_projections",
    "_release_parse_run_seal",
    "_sha256",
    "_stable_id",
    "_staged_completion",
    "_validate_complete_staging",
    "_validate_digest",
    "_validate_event_batch",
    "append_parse_chunk",
    "begin_parse_run",
    "create_import",
    "fail_parse_run",
    "finalize_parse_run",
    "find_same_source",
    "get_current_preview_summary",
    "is_database_storage_exhausted",
    "queue_import_for_parse",
    "recover_finalizing_parse_run",
]
