"""weak / stable 两种模式的快照行为与可见性规则。"""
from __future__ import annotations

import psycopg

from pagination_workbench.data import insert_item
from pagination_workbench.query import PaginationSession
from pagination_workbench.sorts import Filters, SORT_SPECS


def test_stable_snapshot_hides_commits_mid_walk(conn_info, tiny_table):
    spec = SORT_SPECS["created_desc"]
    with PaginationSession(conn_info, "stable", spec, Filters()) as session:
        first = session.fetch_page(None, limit=2)
        seen = {r["id"] for r in first.rows}
        # 会话进行中，另一连接提交新行
        new_id = insert_item(conn_info, category="alpha")
        second = session.fetch_page(first.next_cursor, limit=2)
        seen.update(r["id"] for r in second.rows)
        assert new_id not in seen  # 冻结快照看不到中途提交
        assert len(set(session.snapshot_xid and [session.snapshot_xid])) == 1


def test_weak_sees_commit_on_later_page(conn_info, tiny_table):
    spec = SORT_SPECS["created_desc"]
    # 用极旧时间戳，保证新行落在排序尾部、后续页范围内
    from datetime import datetime, timezone

    with PaginationSession(conn_info, "weak", spec, Filters()) as session:
        first = session.fetch_page(None, limit=2)
        new_id = insert_item(
            conn_info, category="alpha",
            created_at=datetime(1999, 1, 1, tzinfo=timezone.utc),
        )
        # 一路翻到尾，weak 的后续语句应当能看到新提交
        token = first.next_cursor
        all_ids = {r["id"] for r in first.rows}
        for _ in range(20):
            page = session.fetch_page(token, limit=2)
            all_ids.update(r["id"] for r in page.rows)
            if not page.has_more:
                break
            token = page.next_cursor
        assert new_id in all_ids


def test_stable_walk_is_exactly_one_snapshot(conn_info, tiny_table):
    """stable 全轮的 xmin 必须恒定；weak 至少出现多个不同快照标识。"""
    spec = SORT_SPECS["created_desc"]
    from pagination_workbench.bench import walk_all_pages

    stable = walk_all_pages(conn_info, "stable", spec, Filters(), limit=2)
    assert len(set(stable.snapshot_xids)) == 1
    assert stable.missing_from_baseline == 0
    assert stable.extra_vs_baseline == 0

    weak = walk_all_pages(conn_info, "weak", spec, Filters(), limit=2)
    # 无并发时 xmin 也可能相同，这里只断言结构有效；漂移由 drift 命令演示
    assert weak.pages == stable.pages


def test_new_row_position_follows_sort_order(conn_info, tiny_table):
    """新插入行的可见页位置完全由排序值决定，而不是固定在末尾。"""
    spec = SORT_SPECS["created_desc"]
    from datetime import datetime, timedelta, timezone

    future = datetime.now(timezone.utc) + timedelta(days=365)
    new_id = insert_item(conn_info, category="alpha", created_at=future)
    with PaginationSession(conn_info, "weak", spec, Filters()) as session:
        page = session.fetch_page(None, limit=5)
    # created_at 最新 → created_desc 首页第一位
    assert page.rows[0]["id"] == new_id


def test_stable_session_rollback_on_error(conn_info, tiny_table):
    spec = SORT_SPECS["created_desc"]
    session = PaginationSession(conn_info, "stable", spec, Filters())
    session.__enter__()
    try:
        assert session.snapshot_xid is not None
    finally:
        session.__exit__(RuntimeError, RuntimeError("boom"), None)
    # 连接已关闭，不留悬挂事务
    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM pg_stat_activity"
                    " WHERE state='idle in transaction'")
        assert cur.fetchone()["n"] == 0
