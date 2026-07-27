"""模型层公用件：SQLAlchemy 基类的转出、主键生成与时间戳。

拆包前这些和 26 个模型类挤在同一个 1227 行文件里。抽出来是为了让每个模型
子模块只 import 一次，而不是各自复制一份 ``new_id``/``utc_now`` 定义——
``default=utc_now`` 传的是函数对象，复制多份虽然不影响正确性，但同名不同身
的函数在调试时极易误导。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from webhub.db.base import Base

DEFAULT_CATEGORY_NAME = "未分类"


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)



__all__ = ["Base", "DEFAULT_CATEGORY_NAME", "new_id", "utc_now"]
