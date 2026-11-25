"""排序规格（sort spec）与筛选条件的规范化定义。

设计要点
========
1. 每个排序规格是一组带方向的列 + 固定收尾的主键 ``id``。
   收尾的唯一键保证排序结果全序，同时间戳 / 同分数也有确定性次序。
2. NULL 统一显式声明 ``NULLS LAST``（两个方向都是），避免依赖数据库默认：
   PG 默认 ASC 是 NULLS LAST、DESC 是 NULLS FIRST，隐式默认会让混合方向
   的游标谓词写错。显式写出来，谓词和索引才能对齐。
3. 筛选条件（filters）被规范化成确定性字符串参与指纹哈希；
   游标只在“同一查询族”（同排序、同筛选、同一致性模式）内有效。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

Direction = Literal["asc", "desc"]
Nulls = Literal["nulls_first", "nulls_last"]


@dataclass(frozen=True)
class SortKey:
    column: str
    direction: Direction
    nulls: Nulls = "nulls_last"
    pg_type: str = "bigint"  # 用于游标谓词里裸参数的 ::type 转换

    def order_sql(self) -> str:
        return (
            f"{self.column} "
            f"{self.direction.upper()} "
            f"{self.nulls.upper().replace('_', ' ')}"
        )


@dataclass(frozen=True)
class SortSpec:
    """一个命名的复合排序规格。"""

    name: str
    keys: tuple[SortKey, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(k.column for k in self.keys)

    @property
    def pg_types(self) -> tuple[str, ...]:
        return tuple(k.pg_type for k in self.keys)

    def order_by_sql(self) -> str:
        return "ORDER BY " + ", ".join(k.order_sql() for k in self.keys)

    def signature(self) -> str:
        """排序结构的稳定指纹，写入游标，换排序即失效。"""
        payload = [
            [k.column, k.direction, k.nulls] for k in self.keys
        ]
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":")).encode()
        ).hexdigest()[:16]


def _spec(name: str, *keys: tuple[str, Direction, Nulls | None, str | None]) -> SortSpec:
    return SortSpec(
        name=name,
        keys=tuple(
            SortKey(c, d, n or "nulls_last", t or "bigint")
            for c, d, n, t in keys
        ),
    )


# 工作台支持的六个排序规格，覆盖不同数据类型与混合方向。
# 每个规格最后都由 id 收尾做唯一 tie-breaker。
# 元组第 4 项是列的 PG 类型，供游标谓词里裸参数显式转型。
SORT_SPECS: dict[str, SortSpec] = {
    s.name: s
    for s in (
        _spec(
            "created_desc",
            ("created_at", "desc", None, "timestamptz"),
            ("id", "desc", None, "bigint"),
        ),
        _spec(
            "cat_created",
            ("category", "asc", None, "text"),
            ("created_at", "desc", None, "timestamptz"),
            ("id", "desc", None, "bigint"),
        ),
        _spec(
            "created_score",          # 混合方向：时间倒序、分数正序
            ("created_at", "desc", None, "timestamptz"),
            ("score", "asc", None, "integer"),
            ("id", "desc", None, "bigint"),
        ),
        _spec(
            "score_desc",
            ("score", "desc", None, "integer"),  # 含 NULL，统一排尾
            ("created_at", "desc", None, "timestamptz"),
            ("id", "desc", None, "bigint"),
        ),
        _spec(
            "label_asc",
            ("label", "asc", None, "text"),      # 含 NULL 文本
            ("score", "desc", None, "integer"),
            ("id", "desc", None, "bigint"),
        ),
        _spec(
            "active_cat",
            ("active", "desc", None, "boolean"),
            ("category", "asc", None, "text"),
            ("id", "desc", None, "bigint"),
        ),
    )
}


@dataclass(frozen=True)
class Filters:
    """分页查询的可选筛选条件。

    字段保持扁平、可 JSON 序列化，``canonical()`` 输出确定性字符串，
    保证两个语义相同的筛选得到相同指纹。
    """

    category: str | None = None
    active: bool | None = None
    min_score: int | None = None

    def canonical(self) -> str:
        return json.dumps(
            {
                "category": self.category,
                "active": self.active,
                "min_score": self.min_score,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()[:16]

    def where_sql(self, params: list[Any]) -> str | None:
        """生成参数化 WHERE 子句；参数按顺序追加到 params。"""
        clauses: list[str] = []
        if self.category is not None:
            params.append(self.category)
            clauses.append("category = %s")
        if self.active is not None:
            params.append(self.active)
            clauses.append("active = %s")
        if self.min_score is not None:
            params.append(self.min_score)
            clauses.append("score >= %s")
        if not clauses:
            return None
        return "WHERE " + " AND ".join(clauses)
