"""键集分页查询：谓词构造、两种一致性会话、执行计划采集。

游标谓词怎么构造
================
复合排序 (k1 d1, k2 d2, ..., id dn) 下，“排在游标行之后”是严格的
字典序比较，展开成 OR 链::

    (k1 >  v1)
 OR (k1 =  v1 AND k2 >  v2)
 OR (k1 =  v1 AND k2 = v2 AND ... id > vid)

其中每一级的 ``>`` / ``<`` 由该列方向决定；等值用
``IS NOT DISTINCT FROM`` 而不是 ``=``，这样 NULL 与 NULL 也能正确判等
（普通 ``NULL = NULL`` 是 NULL，会把含空值的边界行整个漏掉）。

两种一致性模式
==============
weak   每页一条独立语句，每条语句拿查询瞬间的最新快照 —— 写入活跃时
       新提交的行会并入后续页，可能出现重复/漂移，但没有长事务代价。
stable 整个分页会话在一个 REPEATABLE READ 事务里，所有页冻结在同一快照；
       游标额外绑定快照 xid，事务结束或换快照即拒绝续用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from .config import ConnInfo
from .cursor import encode_cursor, decode_cursor, validate_cursor_context
from .sorts import Filters, SortSpec


@dataclass
class Page:
    rows: list[dict[str, Any]]
    next_cursor: str | None
    has_more: bool
    snapshot_xid: int | None
    mode: str


def _cursor_predicate(sort_spec: SortSpec) -> tuple[str, list[int]]:
    """生成字典序 OR 链谓词，返回 (SQL, 每占位符对应的游标键下标)。

    psycopg3 的 %s 按出现顺序绑定参数，因此每个 OR 分支都带自己的完整
    前缀；下标表让调用方按游标键取值，参数顺序不会写乱。

    单列在“方向 + NULL 位置”下严格越过游标值的判定
    ------------------------------------------------
    NULLS LAST：NULL 组排在所有非 NULL 之后（与方向无关）。令 op 为方向
    对应的严格比较符（ASC→>，DESC→<），c 严格越过 v 当且仅当：

      v 非 NULL：
        c 为 NULL                          —— NULL 组在非 NULL 组之后
        或 c 非 NULL 且 c op v             —— 同组内按方向越过
      v 是 NULL：
        （c 为 NULL 时本列相等，交后续列；c 非 NULL 反而在 v 前面）
        → 本列不产生越过条件

    NULLS FIRST 是镜像：v 是 NULL 时仅非 NULL 按 op 越过；v 非 NULL 时
    c 为 NULL 直接越过。裸参数显式 ::type，否则参数为 NULL 时 PG 无法
    推断参数类型。
    """
    keys = sort_spec.keys
    or_terms: list[str] = []
    param_index: list[int] = []
    for level in range(len(keys)):
        conjuncts: list[str] = []
        for prev in range(level):  # 前缀列：NULL 安全判等，取该前缀列的值
            col = keys[prev].column
            param_index.append(prev)
            conjuncts.append(f"{col} IS NOT DISTINCT FROM %s")

        col = keys[level].column
        pg_type = keys[level].pg_type
        op = ">" if keys[level].direction == "asc" else "<"
        v = f"%s::{pg_type}"
        if keys[level].nulls == "nulls_last":
            # 占位符：v 判空 1 个 + op 比较 1 个 = 2
            jump = (
                f"({v} IS NOT NULL AND ("
                f"{col} IS NULL OR ({col} IS NOT NULL AND {col} {op} %s)))"
            )
            param_index.extend([level] * 2)
        else:  # NULLS FIRST 镜像：NULL 组在最前，v 出现 2 次 + op 1 次
            # v 是 NULL：c 非 NULL 即越过（NULL 组之后全是非 NULL）
            # v 非 NULL：c 必须非 NULL 且按 op 越过（c=NULL 反而在前面）
            jump = (
                f"(({v} IS NULL AND {col} IS NOT NULL)"
                f" OR ({v} IS NOT NULL AND {col} IS NOT NULL AND {col} {op} %s))"
            )
            param_index.extend([level] * 3)
        conjuncts.append(jump)
        or_terms.append("(" + " AND ".join(conjuncts) + ")")
    return "(" + " OR ".join(or_terms) + ")", param_index


def _build_sql(
    sort_spec: SortSpec,
    filters: Filters,
    cursor_key: list[Any] | None,
    params: list[Any],
    limit: int,
) -> str:
    where_parts: list[str] = []
    filter_sql = filters.where_sql(params)
    if filter_sql:
        where_parts.append(filter_sql.removeprefix("WHERE "))
    if cursor_key is not None:
        pred, param_index = _cursor_predicate(sort_spec)
        where_parts.append(pred)
        params.extend(cursor_key[i] for i in param_index)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    params.append(limit + 1)  # 多取 1 行判断 has_more
    return (
        f"SELECT * FROM items {where} "
        f"{sort_spec.order_by_sql()} LIMIT %s"
    )


class PaginationSession:
    """一次分页会话；weak 模式每页独立连接，stable 模式持有快照事务。"""

    def __init__(
        self,
        conn_info: ConnInfo,
        mode: str,
        sort_spec: SortSpec,
        filters: Filters,
    ):
        if mode not in ("weak", "stable"):
            raise ValueError(f"未知模式: {mode}")
        self.conn_info = conn_info
        self.mode = mode
        self.sort_spec = sort_spec
        self.filters = filters
        self._conn: psycopg.Connection | None = None
        self.snapshot_xid: int | None = None

    def __enter__(self) -> "PaginationSession":
        self._conn = psycopg.connect(**self.conn_info.kwargs())
        if self.mode == "stable":
            # 整个会话一个 REPEATABLE READ 事务，所有页冻结在同一快照
            self._conn.autocommit = False
            with self._conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("SELECT pg_current_xact_id() AS xid")
                self.snapshot_xid = int(cur.fetchone()["xid"])
        else:
            # weak：每条语句独立自动提交，各自拿查询瞬间的最新快照
            self._conn.autocommit = True
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._conn is not None:
            if self.mode == "stable" and exc[0] is None:
                self._conn.commit()
            self._conn.close()
            self._conn = None

    def fetch_page(self, cursor: str | None, limit: int = 10) -> Page:
        if self._conn is None:
            raise RuntimeError("会话未开启（请用 with 语句）")

        cursor_key: list[Any] | None = None
        if cursor is not None:
            body = decode_cursor(cursor)
            validate_cursor_context(
                body,
                mode=self.mode,
                sort_spec=self.sort_spec,
                filters=self.filters,
                snapshot_xid=self.snapshot_xid,
            )
            cursor_key = body["k"]
            if len(cursor_key) != len(self.sort_spec.columns):
                from .cursor import CursorError

                raise CursorError("cursor 键数量与排序规格不匹配")

        params: list[Any] = []
        sql = _build_sql(
            self.sort_spec, self.filters, cursor_key, params, limit
        )
        # 同一条语句里附带快照下界 xmin：weak 模式下写入提交后 xmin 会前进，
        # stable 模式下整轮 xmin 不变 —— 这正是两种模式差异的直接证据。
        sql = f"WITH page AS ({sql.rstrip(';')}) " \
              "SELECT (SELECT pg_snapshot_xmin(pg_current_snapshot())) AS snap_xmin, * FROM page"

        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall()
            if raw:
                page_xid = int(raw[0]["snap_xmin"])
            else:
                # 空页拿不到 CTE 里的快照标识，单独取一次（同一快照语义）
                cur.execute(
                    "SELECT pg_snapshot_xmin(pg_current_snapshot()) AS xmin"
                )
                page_xid = int(cur.fetchone()["xmin"])
            batch = [{k: v for k, v in row.items() if k != "snap_xmin"}
                     for row in raw]

        has_more = len(batch) > limit
        page_rows = batch[:limit]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = encode_cursor(
                mode=self.mode,
                sort_spec=self.sort_spec,
                filters=self.filters,
                last_key=[last[c] for c in self.sort_spec.columns],
                snapshot_xid=self.snapshot_xid,
            )
        return Page(
            rows=page_rows,
            next_cursor=next_cursor,
            has_more=has_more,
            snapshot_xid=page_xid,
            mode=self.mode,
        )


def explain_page(
    conn_info: ConnInfo,
    sort_spec: SortSpec,
    filters: Filters,
    cursor_key: list[Any] | None,
    limit: int = 10,
    analyze: bool = False,
) -> list[str]:
    """返回页查询的 EXPLAIN 文本，用于核对是否命中复合索引。"""
    params: list[Any] = []
    sql = _build_sql(sort_spec, filters, cursor_key, params, limit)
    prefix = "EXPLAIN (ANALYZE, BUFFERS, " if analyze else "EXPLAIN ("
    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        cur.execute(prefix + "FORMAT TEXT) " + sql, params)
        return [r["QUERY PLAN"] if isinstance(r, dict) else r[0]
                for r in cur.fetchall()]
