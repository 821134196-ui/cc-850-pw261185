"""边界情况：空结果、末页终止、极小页大小、筛选组合。"""
from __future__ import annotations

from pagination_workbench.query import PaginationSession
from pagination_workbench.sorts import Filters, SORT_SPECS


def test_empty_filter_returns_no_cursor(conn_info, tiny_table):
    spec = SORT_SPECS["created_desc"]
    with PaginationSession(conn_info, "weak", spec,
                           Filters(category="nonexistent")) as s:
        page = s.fetch_page(None, limit=10)
    assert page.rows == []
    assert page.has_more is False
    assert page.next_cursor is None


def test_last_page_has_no_cursor(conn_info, tiny_table):
    spec = SORT_SPECS["created_desc"]
    with PaginationSession(conn_info, "stable", spec, Filters()) as s:
        page = s.fetch_page(None, limit=10_000)  # 一次取完全部 6 行
    assert len(page.rows) == 6
    assert page.has_more is False
    assert page.next_cursor is None


def test_page_size_one_covers_all_rows(conn_info, tiny_table):
    spec = SORT_SPECS["score_desc"]
    seen: list[int] = []
    with PaginationSession(conn_info, "weak", spec, Filters()) as s:
        token = None
        for _ in range(20):
            page = s.fetch_page(token, limit=1)
            seen.extend(r["id"] for r in page.rows)
            token = page.next_cursor
            if token is None:
                break
    assert len(seen) == 6 and len(set(seen)) == 6


def test_combined_filters_walk(conn_info, tiny_table):
    spec = SORT_SPECS["cat_created"]
    flt = Filters(category="alpha", active=True)
    with PaginationSession(conn_info, "stable", spec, flt) as s:
        page = s.fetch_page(None, limit=100)
    assert all(r["category"] == "alpha" and r["active"] for r in page.rows)
    # 小表里 alpha+true 的行：id 1,2
    assert {r["id"] for r in page.rows} == {1, 2}


def test_min_score_excludes_null_scores(conn_info, tiny_table):
    spec = SORT_SPECS["score_desc"]
    with PaginationSession(conn_info, "weak", spec,
                           Filters(min_score=0)) as s:
        page = s.fetch_page(None, limit=100)
    assert all(r["score"] is not None and r["score"] >= 0
               for r in page.rows)
    assert len(page.rows) == 4  # 6 行里 2 行 score 为 NULL


def test_cursor_boundary_on_null_sort_column(conn_info, tiny_table):
    """游标边界恰好落在 NULL 排序列上时，续页仍须正确（回归 NULL 谓词 bug）。"""
    # label_asc：NULL label 排尾，先翻到 NULL 区再续页，不能报错也不能漏行
    spec = SORT_SPECS["label_asc"]
    seen: list[int] = []
    with PaginationSession(conn_info, "stable", spec, Filters()) as s:
        token = None
        for _ in range(10):
            page = s.fetch_page(token, limit=2)
            seen.extend(r["id"] for r in page.rows)
            token = page.next_cursor
            if token is None:
                break
    assert len(seen) == 6 and len(set(seen)) == 6
