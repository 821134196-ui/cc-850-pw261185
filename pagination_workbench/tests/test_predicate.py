"""谓词语义属性测试：游标谓词必须与 ORDER BY 的字典序严格一致。

对每个排序规格、每一行作为边界，谓词选出的集合必须恰好等于 ORDER BY
结果中该行之后的所有行。同时间戳、NULL、混合方向都在 tiny_table 里覆盖。
"""
from __future__ import annotations

import psycopg

from pagination_workbench.config import ConnInfo
from pagination_workbench.query import _build_sql, _cursor_predicate
from pagination_workbench.sorts import Filters, SortKey, SortSpec


def _full_order(cur, spec: SortSpec) -> list[dict]:
    cur.execute(f"SELECT * FROM items {spec.order_by_sql()}")
    return cur.fetchall()


def _rows_after(cur, spec, boundary_row) -> list[dict]:
    key = [boundary_row[c] for c in spec.columns]
    params: list = []
    sql = _build_sql(spec, Filters(), key, params, limit=10_000)
    cur.execute(sql, params)
    return cur.fetchall()


def test_placeholder_matches_param_count(all_specs):
    for spec in all_specs.values():
        sql, idx = _cursor_predicate(spec)
        assert sql.count("%s") == len(idx)


def test_predicate_matches_order_by_for_every_boundary(
    conn_info: ConnInfo, tiny_table, all_specs
):
    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        for spec in all_specs.values():
            ordered = _full_order(cur, spec)
            ids = [r["id"] for r in ordered]
            for pos, row in enumerate(ordered):
                got = _rows_after(cur, spec, row)
                got_ids = [r["id"] for r in got]
                expected_ids = ids[pos + 1:]
                assert got_ids == expected_ids, (
                    f"{spec.name}: boundary id={row['id']} 处谓词与排序不一致"
                )


def test_nulls_first_spec(conn_info: ConnInfo, tiny_table):
    # 显式覆盖 NULLS FIRST（工作台默认规格用 NULLS LAST，这里验证镜像分支）
    nf = SortSpec(
        "nf_test",
        (
            SortKey("score", "asc", "nulls_first", "integer"),
            SortKey("id", "desc", "nulls_last", "bigint"),
        ),
    )
    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        ordered = _full_order(cur, nf)
        ids = [r["id"] for r in ordered]
        # NULL 必须在最前面
        null_rows = [r["id"] for r in ordered if r["score"] is None]
        assert set(ids[: len(null_rows)]) == set(null_rows)
        for pos, row in enumerate(ordered):
            got = _rows_after(cur, nf, row)
            assert [r["id"] for r in got] == ids[pos + 1:]


def test_walk_every_page_no_gap_no_dup(conn_info: ConnInfo, tiny_table, all_specs):
    """端到端：逐页翻完全表，拼接结果必须等于一次完整 ORDER BY。"""
    from pagination_workbench.query import PaginationSession

    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM items")
        all_ids = {r["id"] for r in cur.fetchall()}

    for spec in all_specs.values():
        for mode in ("weak", "stable"):
            seen: list[int] = []
            with PaginationSession(conn_info, mode, spec, Filters()) as session:
                token = None
                for _ in range(50):
                    page = session.fetch_page(token, limit=2)  # 小页制造更多边界
                    seen.extend(r["id"] for r in page.rows)
                    if not page.has_more:
                        break
                    token = page.next_cursor
            assert len(seen) == len(set(seen)), f"{spec.name}/{mode} 出现重复行"
            assert set(seen) == all_ids, f"{spec.name}/{mode} 有遗漏或多余行"
