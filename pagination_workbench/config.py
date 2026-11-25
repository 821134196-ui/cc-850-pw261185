"""本地 PostgreSQL 连接与测试数据模型。

连接走 unix socket（listen_addresses=''），不需要 TCP 与密码，
实例由 scripts/pg_local.sh 在用户态管理（见仓库 scripts 目录）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

SOCKET_DIR = os.environ.get("PG_SOCKET_DIR", "/tmp")
PG_PORT = int(os.environ.get("PG_PORT", "55432"))
PG_USER = os.environ.get("PG_USER", "pguser")
PG_DB = os.environ.get("PG_DB", "bench")

# 游标 HMAC 密钥。真实服务里应来自密钥管理；这里演示用固定默认值，
# 保证同进程签发/校验闭环，同时密钥不进 token 本身。
CURSOR_SECRET = os.environ.get(
    "CURSOR_HMAC_SECRET", "workbench-demo-secret-change-me"
).encode()

CURSOR_VERSION = 1


@dataclass(frozen=True)
class ConnInfo:
    host: str = SOCKET_DIR
    port: int = PG_PORT
    user: str = PG_USER
    dbname: str = PG_DB

    def kwargs(self) -> dict:
        from psycopg.rows import dict_row

        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "dbname": self.dbname,
            "row_factory": dict_row,
        }


# ---------------------------------------------------------------------------
# 表结构
#
# 刻意覆盖任务要求的三类“麻烦数据”：
#   1. created_at 大量同时间戳（同毫秒批次写入），单列排序无法确定顺序；
#   2. label / score 允许 NULL，排序必须明确 NULL 位置；
#   3. 多个不同类型、不同方向的排序字段（时间、数值、文本、布尔）。
#
# id 是 bigserial 主键，永远作为复合排序的最后一个 tie-breaker，
# 保证整组排序键严格唯一 —— 这是键集分页可续用、不重不漏的前提。
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
DROP TABLE IF EXISTS items CASCADE;

CREATE TABLE items (
    id          bigserial PRIMARY KEY,
    category    text        NOT NULL,
    score       integer,                         -- 可空
    created_at  timestamptz NOT NULL,            -- 刻意制造同值
    label       text,                            -- 可空
    active      boolean     NOT NULL DEFAULT true
);

-- 与排序规格一一对应的复合索引：列顺序、方向、NULL 位置都必须与
-- ORDER BY 完全一致，规划器才能直接按索引次序输出（免 Sort 节点）。
-- 混合方向索引在 PG 13+ 可被 B-tree 直接维护。
CREATE INDEX items_created_desc_idx
    ON items (created_at DESC NULLS LAST, id DESC NULLS LAST);

CREATE INDEX items_cat_created_idx
    ON items (category ASC NULLS LAST, created_at DESC NULLS LAST, id DESC NULLS LAST);

CREATE INDEX items_created_score_idx
    ON items (created_at DESC NULLS LAST, score ASC NULLS LAST, id DESC NULLS LAST);

CREATE INDEX items_score_created_idx
    ON items (score DESC NULLS LAST, created_at DESC NULLS LAST, id DESC NULLS LAST);

CREATE INDEX items_label_score_idx
    ON items (label ASC NULLS LAST, score DESC NULLS LAST, id DESC NULLS LAST);

CREATE INDEX items_active_cat_idx
    ON items (active DESC NULLS LAST, category ASC NULLS LAST, id DESC NULLS LAST);
"""
