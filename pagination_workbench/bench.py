"""性能与一致性度量：页成本、整轮遍历、写入活跃下的两模式对照。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import ConnInfo
from .query import PaginationSession, explain_page
from .sorts import Filters, SortSpec


@dataclass
class PageCost:
    page_no: int
    rows: int
    ms: float
    plan: list[str]
    index_scan: bool
    buffers_hit: int
    buffers_read: int


_BUFFERS_RE = re.compile(r"Buffers: shared hit=(\d+)(?: read=(\d+))?")


def _parse_buffers(plan: list[str]) -> tuple[int, int]:
    hit = read = 0
    for line in plan:
        m = _BUFFERS_RE.search(line)
        if m:
            hit += int(m.group(1))
            read += int(m.group(2) or 0)
    return hit, read


def measure_page_cost(
    conn_info: ConnInfo,
    sort_spec: SortSpec,
    filters: Filters,
    cursor_key: list[Any] | None,
    limit: int,
) -> PageCost:
    plan = explain_page(
        conn_info, sort_spec, filters, cursor_key, limit, analyze=True
    )
    joined = "\n".join(plan)
    hit, read = _parse_buffers(plan)
    return PageCost(
        page_no=0,
        rows=limit,
        ms=_parse_exec_ms(joined),
        plan=plan,
        index_scan="Index Scan" in joined or "Index Only Scan" in joined,
        buffers_hit=hit,
        buffers_read=read,
    )


def _parse_exec_ms(plan_text: str) -> float:
    m = re.search(r"Execution Time: ([\d.]+) ms", plan_text)
    return float(m.group(1)) if m else float("nan")


@dataclass
class WalkResult:
    mode: str
    pages: int
    rows_seen: int
    unique_rows: int
    duplicate_rows: int
    missing_from_baseline: int
    extra_vs_baseline: int
    elapsed_ms: float
    baseline_size: int = 0
    snapshot_xids: list[int] = field(default_factory=list)
    page_sizes: list[int] = field(default_factory=list)


def walk_all_pages(
    conn_info: ConnInfo,
    mode: str,
    sort_spec: SortSpec,
    filters: Filters,
    limit: int = 100,
    max_pages: int = 500,
    between_pages=None,
) -> WalkResult:
    """从头遍历全部页，收集 id 序列，检查重复/遗漏。

    对照基线在分页会话内、任何并发回调之前采集：
      stable：显式 RR 事务，基线与全部页共享同一冻结快照；
      weak：autocommit，基线是首页之前最后一次独立快照，作为起点参照，
            后续页若并入新提交，会体现在 extra_vs_baseline。
    between_pages: 可选回调 (page_no) -> None，每页间执行（注入并发写入）。
    """
    seen: list[int] = []
    xids: list[int] = []
    sizes: list[int] = []
    t0 = time.perf_counter()
    with PaginationSession(conn_info, mode, sort_spec, filters) as session:
        baseline = _expected_ids_session(session)
        cursor: str | None = None
        page_no = 0
        while page_no < max_pages:
            page = session.fetch_page(cursor, limit=limit)
            seen.extend(r["id"] for r in page.rows)
            xids.append(page.snapshot_xid)
            sizes.append(len(page.rows))
            page_no += 1
            if not page.has_more:
                break
            cursor = page.next_cursor
            if between_pages is not None:
                between_pages(page_no)
    elapsed = (time.perf_counter() - t0) * 1000

    seen_set = set(seen)
    return WalkResult(
        mode=mode,
        pages=page_no,
        rows_seen=len(seen),
        unique_rows=len(seen_set),
        duplicate_rows=len(seen) - len(seen_set),
        # 基线条目缺失数（stable 应为 0）；遍历中新出现的条目数（weak 可 >0）
        missing_from_baseline=len(baseline - seen_set),
        extra_vs_baseline=len(seen_set - baseline),
        elapsed_ms=elapsed,
        baseline_size=len(baseline),
        snapshot_xids=xids,
        page_sizes=sizes,
    )


def _expected_ids_session(session: "PaginationSession") -> set[int]:
    params: list[Any] = []
    where = session.filters.where_sql(params) or ""
    with session._conn.cursor() as cur:
        cur.execute(f"SELECT id FROM items {where}", params)
        return {r["id"] for r in cur.fetchall()}
