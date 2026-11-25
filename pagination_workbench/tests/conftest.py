"""共享 pytest fixture：本地 PG 上的小数据集，覆盖同时间戳与 NULL。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from pagination_workbench.config import ConnInfo, SCHEMA_SQL
from pagination_workbench.sorts import Filters, SORT_SPECS, SortSpec

CONN = ConnInfo()
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def conn_info() -> ConnInfo:
    return CONN


@pytest.fixture()
def tiny_table(conn_info):
    """每个测试一张独立小表，含同时间戳、NULL、不同类型字段。"""
    rows = [
        # id, category, score, created_at, label, active
        (1, "alpha", 100, T0, "red", True),
        (2, "alpha", None, T0, None, True),       # 同时间戳 + 双 NULL
        (3, "beta", 50, T0, "blue", False),       # 同时间戳
        (4, "beta", None, T0 - timedelta(days=1), None, True),
        (5, "gamma", 1, T0 - timedelta(days=1), "red", True),
        (6, "alpha", 50, T0 - timedelta(days=2), "blue", False),
    ]
    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS items")
        cur.execute(SCHEMA_SQL)
        cur.executemany(
            "INSERT INTO items (id,category,score,created_at,label,active)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            rows,
        )
        # 重置序列，避免显式 id 与后续 insert 冲突
        cur.execute("SELECT setval(pg_get_serial_sequence('items','id'),"
                    " (SELECT max(id) FROM items))")
        conn.commit()
    yield
    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS items")


@pytest.fixture()
def all_specs() -> dict[str, SortSpec]:
    return SORT_SPECS


@pytest.fixture()
def no_filters() -> Filters:
    return Filters()
