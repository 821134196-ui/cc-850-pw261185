# PostgreSQL 键集分页性能工作台

在**本地用户态 PostgreSQL 15** 上验证键集（keyset / seek）分页的正确性、
查询计划与一致性取舍。数据刻意包含**同时间戳、NULL、混合排序方向**，
并提供两种一致性模式与不可被静默错用的复合游标。

## 它回答什么问题

1. 含同时间戳与 NULL 的复合排序，怎样分页才能不重、不漏、可续用？
2. 新插入的一行到底出现在哪一页？由什么决定？
3. 首页 / 中间页 / 尾页的真实查询计划长什么样，能否命中复合索引？
4. 缓存或筛选条件变化后，旧游标为什么不能接着用，如何被显式拒绝？
5. 弱一致分页与稳定读（快照）分页在写入活跃时结果有何差异，代价各是什么？
6. 为什么两种模式不能共用一个游标？

## 目录结构

```
pagination_workbench/
  config.py    连接信息、表结构、与排序一一对应的复合索引
  sorts.py     排序规格（方向/NULL 位置/类型）、筛选条件规范化与指纹
  cursor.py    游标 token：base64url JSON + HMAC，上下文强校验
  query.py     字典序 OR 链谓词、weak/stable 会话、EXPLAIN
  data.py      测试数据生成（同时间戳桶、NULL、不均匀分类）
  bench.py     全量遍历校验、缓冲/耗时度量
  cli.py       命令行入口（seed/page/walk/explain/drift/contention/cost/guard/demo）
  tests/       pytest：谓词属性、游标防护、模式隔离、查询计划（21 项）
scripts/
  pg_local.sh  用户态 PG 实例的 start/stop/status/psql
docs/
  DESIGN.md    设计原理与正确性论证
```

## 快速开始

实例已经在 unix socket `/tmp`、端口 `55432` 上运行（无需 root/docker，
见文末“环境搭建”）。

```bash
# 虚拟环境（已装 psycopg[binary]、pytest）
source ~/venv/bin/activate

# 一条命令跑完整 8 段演示
python -m pagination_workbench.cli demo

# 或分步执行
python -m pagination_workbench.cli seed --rows 12000
python -m pagination_workbench.cli explain --sort created_score
python -m pagination_workbench.cli page --sort created_score --limit 10
python -m pagination_workbench.cli walk  --sort created_score --mode stable
python -m pagination_workbench.cli drift --sort created_desc        # 确定性结果分岔
python -m pagination_workbench.cli contention --writes 200          # 写入活跃对照
python -m pagination_workbench.cli cost                             # vacuum/膨胀代价
python -m pagination_workbench.cli guard                            # 游标防护用例

python -m pytest pagination_workbench/tests/ -q
```

## 核心结论速览

### 1. 复合游标 = 上一页最后一行的完整排序键 + 三重指纹

token 是 base64url 编码的 JSON，含：版本 `v`、模式 `m`、排序签名 `s`、
筛选指纹 `f`、排序键值 `k`、稳定读快照 xid `i`、HMAC `h`。
任一上下文不匹配都会抛 `CursorRejected`，而不是在新结果集里静默错位：

| 变化 | 拒绝原因码 |
| --- | --- |
| weak 游标拿到 stable（或反之） | `mode_mismatch` |
| 换了排序规格 | `sort_mismatch` |
| 筛选条件/缓存键变化 | `filter_mismatch` |
| stable 游标跨事务/快照续用 | `snapshot_mismatch` |
| token 被篡改或结构伪造 | `CursorError`（HMAC/结构校验） |

### 2. 新行的可见位置完全由排序值决定，与“何时插入”无关

- 排序值落在已访问区：后续页不会再遇到它（keyset 按行值定位，不会重复）；
- 落在未访问区：weak 模式的下一页新快照会读到；stable 模式整轮读不到；
- `visibility` 命令用未来/过去时间戳分别演示这两种位置。

### 3. 谓词是字典序 OR 链，NULL 语义是最容易写错的地方

`(k1 d1, k2 d2, …, id dn)` 下“排在游标行之后”展开为逐级 OR，
等值用 `IS NOT DISTINCT FROM`（NULL 安全），越界用方向相关的严格比较。
NULLS LAST/FIRST 的组顺序独立于 ASC/DESC，`tests/test_predicate.py`
对“每一行作为边界”做属性测试，保证谓词选出的集合恰好等于 ORDER BY
结果中该边界之后的全部行。

### 4. weak vs stable

| 维度 | weak（默认，语句级自动提交） | stable（REPEATABLE READ 长事务） |
| --- | --- | --- |
| 快照 | 每页取查询瞬间最新快照 | 整个会话一个冻结快照 |
| 遍历中提交的行 | 按排序值并入后续页（漂移） | 整轮不可见，结果等于会话时刻全量查询 |
| 重复行 | keyset 下不会出现 OFFSET 式重复 | 不会 |
| 主要代价 | 结果是多时间点并集，非单一时刻视图 | 长事务钉住 vacuum horizon，死版本无法回收→膨胀 |
| 游标 | `i=null`，仅校验排序/筛选/模式 | 绑定快照 xid，跨快照即 `snapshot_mismatch` |
| 游标互通 | **禁止**：`mode_mismatch` | **禁止** |

`cost` 命令用 `pg_relation_size` 实测：相同数据行在长事务存活时，
三轮 UPDATE+VACUUM 后堆持续增大（942KB→1212KB），事务结束并
`VACUUM FULL` 后才归还。

## 环境搭建（无 root / 无 docker）

仓库实例通过用户态 apt 下载 PostgreSQL 15 的 .deb 并解包到家目录：

```bash
# 一次性搭建（已在本机完成，记录于此便于复现）
mkdir -p ~/aptroot ~/pgsql
# 用 ~/aptroot/etc/apt/apt.conf 指定独立 Dir，apt-get download-only 后
# dpkg-deb -x 解包 postgresql-15 / client / libpq5 / libllvm14 / libxml2 / libicu72
~/pgsql/usr/lib/postgresql/15/bin/initdb -D ~/pgdata -U pguser --auth=trust
scripts/pg_local.sh start
python3 -m venv ~/venv && ~/venv/bin/pip install "psycopg[binary]" pytest
```

数据库只监听 unix socket（`listen_addresses=''`），不暴露网络端口。
