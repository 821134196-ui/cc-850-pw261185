# 设计与正确性说明

## 1. 为什么是键集分页而不是 OFFSET

`OFFSET n LIMIT k` 在深翻页时要排序并丢弃前 n 行，成本随页数线性增长；
更糟的是写入活跃时，新行插入会把窗口整体推移，产生重复或跳过。
键集分页把“跳到第 n 页”变成“取排序键严格大于上一页末行的前 k 行”，
复杂度只与 k 和索引定位有关，与已经翻过多深无关。

前提是排序结果必须是**全序**：任何并列都要有确定性的收尾键。
本工作台所有规格都以主键 `id` 作为最后一个排序键，因此同时间戳、
同分数的行也有唯一确定次序。

## 2. 排序规格与索引必须逐字符对齐

`sorts.py` 的每个 `SortSpec` 声明列、方向（asc/desc）、NULL 位置
（统一显式 NULLS LAST）和 PG 类型。`config.py` 里的复合索引按完全相同的
`(列, 方向, NULL 位置)` 定义。只要有一处不一致（例如索引写 `active DESC`
而 ORDER BY 写 `active DESC NULLS LAST`），规划器就无法直接按索引次序
输出，会退化为 `Index Scan + Incremental Sort` 或 `Seq Scan + Sort`。
`explain` 与 `tests/test_plans.py` 专门盯这一点：12000 行下每个规格的
计划都必须是不带 Sort 节点的 Index Scan。

## 3. 游标谓词的字典序推导

排序键 `(c0,…,c(n-1))`，游标行键值 `(v0,…,v(n-1))`。行 x 严格排在游标行
之后，当且仅当存在某一级 `j` 使前 j 列全部相等、第 j 列严格越过：

```
(c0 == v0 AND … AND c(j-1) == v(j-1)) AND jump(cj, vj)
```

各级之间取 OR。等值统一用 `IS NOT DISTINCT FROM`，因为普通 `=` 对
NULL 得 UNKNOWN，会丢掉含空值的边界行。

### 3.1 单列 jump 的真值表（NULLS LAST）

NULL 组永远在非 NULL 组之后，与方向无关；方向只决定非 NULL 组内部的
比较符 `op`（ASC 用 `>`，DESC 用 `<`）。

| 游标值 v | 列值 c | 是否越过 | 原因 |
| --- | --- | --- | --- |
| 非 NULL | 非 NULL，c op v | 是 | 同组按方向比较 |
| 非 NULL | NULL | **否（本列相等，交后续列）** | NULL 排在非 NULL 之后 |
| NULL | 非 NULL | 否 | 非 NULL 在 NULL 之前 |
| NULL | NULL | 否（本列相等，交后续列） | 同组，靠后续列定序 |

对应 SQL（NULLS LAST）：

```sql
(v::type IS NOT NULL AND (c IS NULL OR (c IS NOT NULL AND c op v)))
```

NULLS FIRST 是镜像：

```sql
((v::type IS NULL AND c IS NOT NULL)
 OR (v::type IS NOT NULL AND c IS NOT NULL AND c op v))
```

### 3.2 开发过程中实际踩到的两个坑

1. **把“v 非 NULL、c 为 NULL”当成越过**：在 DESC + NULLS LAST 下方向
   写反，导致每一页都把边界行捞回来，遍历永不收敛、重复上万行。
2. **简单写成 `c IS NOT NULL AND c op v`**：非末尾排序列上为 NULL 的行
   被整体排除，全量遍历恰好少掉 NULL 行（1801 个 score NULL 一个不漏地
   “消失”）。全量遍历对照基线的测试立刻暴露了它。

裸参数 `v` 出现在 `IS NULL` 判断里时没有列上下文，参数本身为 NULL 会让
PG 报 `could not determine data type`，因此显式写 `v::type`，类型来自
`SortKey.pg_type`。

## 4. 游标 token 的防错设计

弱一致分页最隐蔽的故障模式：客户端缓存了第 3 页的游标，随后筛选条件或
排序被修改，服务端若只拿游标里的行值续查，会在新结果集里静默错位。
token 因此自带四类上下文指纹，`validate_cursor_context` 逐项比对：

- `m` 模式：weak/stable 互窜直接拒绝，两种模式从设计上不共用游标；
- `s` 排序签名：排序结构（列/方向/NULL 位置）的 SHA-256 截断；
- `f` 筛选指纹：`Filters.canonical()` 对字段排序后序列化，保证语义相同
  的筛选指纹相同、任何字段不同指纹就不同；
- `i` 快照 xid：仅 stable 使用，游标只能在签发它的快照事务内续用。

HMAC-SHA256 覆盖除签名外的全部字段，防止客户端伪造或篡改键值。
datetime 在签发时转 ISO 字符串，回绑时由 psycopg 还原为 timestamptz。

## 5. 两种一致性模式的实现

- **weak**：单连接 `autocommit=True`，每条 SELECT 是独立虚拟事务，
  取查询瞬间的最新快照。页结果附带 `pg_snapshot_xmin(pg_current_snapshot())`
  作为观测标识——写入提交后 xmin 前进，`contention` 里能看到 weak 一轮
  出现几十个不同快照，而 stable 恒为 1。
- **stable**：一个 `REPEATABLE READ` 事务包住整个会话，进入时记录
  `pg_current_xact_id()` 写入游标，所有页共享同一快照；会话结束才提交/
  回滚。游标跨会话使用必然 xid 不符，触发 `snapshot_mismatch`。

### 5.1 stable 的代价不是 CPU，是 vacuum horizon

长事务的快照 xmin 会阻止 VACUUM 清理任何更新版本。`cost` 命令控制变量
对照：无长事务时 UPDATE 后 VACUUM 能回收；长事务存活时重复 UPDATE 使
`pg_relation_size('items')` 单调增长。普通 VACUUM 只标记空间可复用、
不向 OS 缩容，`VACUUM FULL` 重写堆才能直观看到空间归还。

### 5.2 为什么 keyset 下 weak 也不会重复行

漂移演示里 weak 会读到遍历中新提交的行（结果集是多快照并集），但不会
把同一行返回两次：谓词判定基于游标行的具体键值，单行插入不会改变任何
已返回行与游标行的字典序关系。会产生重复的是 OFFSET——插入把窗口往后
推，边界行被重新计入。这是选择 keyset 的额外收益。

## 6. 测试策略

- `test_predicate.py`：6 个正式规格 + 手工 NULLS FIRST 规格，对小数据集
  的**每一行作为边界**断言“谓词结果 == ORDER BY 中该行之后的序列”；
  端到端逐页（page size=2，制造最多边界）拼接必须无重无漏。
- `test_cursor.py`：HMAC 篡改、结构伪造、模式/排序/筛选/快照四类拒绝、
  指纹确定性。
- `test_modes.py`：stable 中途提交不可见、weak 后续页可见、新行位置由
  排序值决定、异常退出不留 idle in transaction。
- `test_plans.py`：12000 行下全部规格走 Index Scan 且无 Sort 节点。
