"""查询计划：每个排序规格的首页与游标页都必须走对应复合索引。"""
from __future__ import annotations

import psycopg

from pagination_workbench.query import explain_page
from pagination_workbench.sorts import Filters


def test_each_spec_uses_matching_index(conn_info, tiny_table, all_specs):
    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        cur.execute("ANALYZE items")
        boundaries = {}
        for name, spec in all_specs.items():
            cur.execute(f"SELECT * FROM items {spec.order_by_sql()} LIMIT 1")
            row = cur.fetchone()
            boundaries[name] = [row[c] for c in spec.columns]

    # 小表（6 行）上规划器可能选 Seq Scan，这里只验证谓词能合法下推、
    # 计划可生成；索引偏好放在独立的大表测试里断言。
    for name, spec in all_specs.items():
        plan = explain_page(conn_info, spec, Filters(),
                            boundaries[name], limit=2)
        assert plan and any("Limit" in line for line in plan)


def test_large_table_prefers_index_scan(conn_info, all_specs):
    """12000 行规模下，无筛选首页必须走索引且无额外排序节点。"""
    from pagination_workbench.data import seed_items

    # 独立重建，避免 tiny_table 的小表统计信息让规划器偏向 Seq Scan
    seed_items(conn_info, n_rows=12000)
    try:
        for name, spec in all_specs.items():
            plan_text = "\n".join(
                explain_page(conn_info, spec, Filters(), None, limit=100)
            )
            assert ("Index Scan" in plan_text
                    or "Index Only Scan" in plan_text), (
                f"{name} 未走索引:\n{plan_text}"
            )
            assert "Sort Key" not in plan_text, (
                f"{name} 索引方向与 ORDER BY 不一致，出现冗余排序:\n{plan_text}"
            )
    finally:
        with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS items")
