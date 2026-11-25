"""测试数据生成：刻意制造同时间戳、NULL 与不同基数的分类。

数据分布（默认 12000 行）
-------------------------
- created_at 只有约 40 个不同值 —— 每个时间戳下挂几百行，
  单列时间排序必然产生大量并列，必须靠复合键收尾的 id 定序。
- score 约 15% 为 NULL；label 约 20% 为 NULL。
- category 6 个分类，基数不均，方便观察筛选前后的计划差异。
- active 约 90% 为 true。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import psycopg

from .config import ConnInfo, SCHEMA_SQL

CATEGORIES = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
LABELS = ("red", "green", "blue", "yellow", None)


def reset_schema(conn_info: ConnInfo) -> None:
    with psycopg.connect(**conn_info.kwargs()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.autocommit = False


def seed_items(conn_info: ConnInfo, n_rows: int = 12000, seed: int = 42) -> dict:
    rng = random.Random(seed)
    reset_schema(conn_info)

    base = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    # 约 40 个时间桶，同桶内所有行时间戳完全相同
    bucket = n_rows // 40
    rows: list[tuple] = []
    for i in range(n_rows):
        ts = base + timedelta(minutes=(i // bucket))
        category = CATEGORIES[rng.randrange(len(CATEGORIES))]
        score = None if rng.random() < 0.15 else rng.randint(0, 1000)
        label = None if rng.random() < 0.20 else rng.choice(LABELS[:4])
        active = rng.random() < 0.9
        rows.append((category, score, ts, label, active))

    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO items (category, score, created_at, label, active)"
            " VALUES (%s, %s, %s, %s, %s)",
            rows,
        )
        cur.execute("ANALYZE items")
        cur.execute(
            "SELECT count(*) AS total, count(score) AS nn_score,"
            " count(label) AS nn_label,"
            " count(DISTINCT created_at) AS n_ts,"
            " count(DISTINCT category) AS n_cat FROM items"
        )
        stats_row = cur.fetchone()

    return {
        "rows": stats_row["total"],
        "non_null_score": stats_row["nn_score"],
        "null_score": stats_row["total"] - stats_row["nn_score"],
        "non_null_label": stats_row["nn_label"],
        "null_label": stats_row["total"] - stats_row["nn_label"],
        "distinct_timestamps": stats_row["n_ts"],
        "categories": stats_row["n_cat"],
    }


def insert_item(
    conn_info: ConnInfo,
    *,
    category: str = "alpha",
    score: int | None = 500,
    created_at: datetime | None = None,
    label: str | None = "green",
    active: bool = True,
) -> int:
    """插入一行并返回 id，用于“新插入记录在哪一页可见”的演示。"""
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    with psycopg.connect(**conn_info.kwargs()) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO items (category, score, created_at, label, active)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (category, score, created_at, label, active),
        )
        return cur.fetchone()["id"]
