"""资料库服务层。

原本是单文件 1097 行，把分类、标签、站点、列表检索四件事揉在一起。按它们拆开，
本模块只做门面——调用方沿用 ``from webhub.library import service`` 再用
``service.X``，签名与行为一个字不变。

- ``_common``     异常、校验、归属查询、响应构造
- ``categories``  分类增删改查
- ``tags``        标签增删改查
- ``sites``       站点增删改查
- ``listing``     游标分页、搜索条件、列表与重排
"""

from __future__ import annotations

from ._common import (
    _CJK_CHARACTER,
    _SEARCH_TOKEN,
    LibraryConflictError,
    LibraryError,
    LibraryNotFoundError,
    LibraryValidationError,
    _category_count,
    _category_response,
    _default_category,
    _display_name,
    _owned_category,
    _owned_site,
    _owned_space,
    _owned_tag,
    _owned_tags,
    _safe_favicon_url,
    _site_url,
    _tag_response,
)
from .categories import (
    category_delete_preview,
    create_category,
    delete_category,
    list_categories,
    update_category,
)
from .listing import (
    SortDirection,
    SortKey,
    _cursor_scope,
    _decode_cursor,
    _encode_cursor,
    _fts_query,
    _like_token_condition,
    _search_condition,
    _search_tokens,
    _site_filters,
    list_sites,
    reorder_sites,
)
from .sites import (
    _site_response,
    bulk_delete_sites,
    create_site,
    delete_site,
    get_site,
    update_site,
)
from .tags import (
    create_tag,
    delete_tag,
    list_tags,
    update_tag,
)

__all__ = [
    "LibraryConflictError",
    "LibraryError",
    "LibraryNotFoundError",
    "LibraryValidationError",
    "SortDirection",
    "SortKey",
    "_CJK_CHARACTER",
    "_SEARCH_TOKEN",
    "_category_count",
    "_category_response",
    "_cursor_scope",
    "_decode_cursor",
    "_default_category",
    "_display_name",
    "_encode_cursor",
    "_fts_query",
    "_like_token_condition",
    "_owned_category",
    "_owned_site",
    "_owned_space",
    "_owned_tag",
    "_owned_tags",
    "_safe_favicon_url",
    "_search_condition",
    "_search_tokens",
    "_site_filters",
    "_site_response",
    "_site_url",
    "_tag_response",
    "category_delete_preview",
    "bulk_delete_sites",
    "create_category",
    "create_site",
    "create_tag",
    "delete_category",
    "delete_site",
    "delete_tag",
    "get_site",
    "list_categories",
    "list_sites",
    "list_tags",
    "reorder_sites",
    "update_category",
    "update_site",
    "update_tag",
]
