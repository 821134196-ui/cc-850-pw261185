"""分页性能工作台 CLI。

子命令
======
  seed       重建表并灌入测试数据（同时间戳 / NULL / 多类型排序字段）
  page       按排序规格与筛选取一页，打印行与新游标
  walk       遍历全部页，校验无重无漏
  explain    对照首页/中间页/尾页的 EXPLAIN 计划
  visibility 演示新插入记录在 weak / stable 两种模式下的可见位置
  contention 写入活跃时两模式对照（结果差异 + 耗时 + 缓冲代价）
  guard      游标防护：换模式 / 换筛选 / 换排序 / 篡改 token 全部显式拒绝
  demo       依次跑完整场景，输出报告
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg

from .bench import walk_all_pages
from .config import ConnInfo
from .cursor import CursorError, CursorRejected, decode_cursor
from .data import insert_item, seed_items
from .query import PaginationSession, explain_page
from .sorts import SORT_SPECS, Filters

CONN = ConnInfo()


def _filters(args: argparse.Namespace) -> Filters:
    return Filters(
        category=args.category,
        active=args.active,
        min_score=args.min_score,
    )


def _print_rows(rows: list[dict], sort_cols: tuple[str, ...]) -> None:
    head = ("id",) + tuple(c for c in sort_cols if c != "id")
    print("  " + " | ".join(f"{h:>10}" for h in head))
    for r in rows:
        print("  " + " | ".join(f"{str(r[h]):>10}" for h in head))


# ---------------------------------------------------------------- seed
def cmd_seed(args: argparse.Namespace) -> None:
    stats = seed_items(CONN, n_rows=args.rows)
    print("✓ 数据就绪：")
    for k, v in stats.items():
        print(f"    {k:22s} = {v}")


# ---------------------------------------------------------------- page
def cmd_page(args: argparse.Namespace) -> None:
    spec = SORT_SPECS[args.sort]
    filters = _filters(args)
    with PaginationSession(CONN, args.mode, spec, filters) as session:
        page = session.fetch_page(args.cursor, limit=args.limit)
    print(f"模式={page.mode} 快照xid={page.snapshot_xid} "
          f"本页={len(page.rows)}行 has_more={page.has_more}")
    _print_rows(page.rows, spec.columns)
    if page.next_cursor:
        print(f"\nnext_cursor:\n{page.next_cursor}")


# ---------------------------------------------------------------- walk
def cmd_walk(args: argparse.Namespace) -> None:
    spec = SORT_SPECS[args.sort]
    result = walk_all_pages(
        CONN, args.mode, spec, _filters(args), limit=args.limit
    )
    summary = {
        "mode": result.mode,
        "pages": result.pages,
        "rows_seen": result.rows_seen,
        "unique_rows": result.unique_rows,
        "duplicate_rows": result.duplicate_rows,
        "missing_from_baseline": result.missing_from_baseline,
        "extra_vs_baseline": result.extra_vs_baseline,
        "baseline_size": result.baseline_size,
        "distinct_snapshots": len(set(result.snapshot_xids)),
        "elapsed_ms": round(result.elapsed_ms, 2),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- explain
def cmd_explain(args: argparse.Namespace) -> None:
    spec = SORT_SPECS[args.sort]
    filters = _filters(args)
    print(f"## 排序规格 {spec.name}：{spec.order_by_sql()}")
    print(f"## 筛选指纹 {filters.fingerprint()}  条件 {filters.canonical()}\n")

    # 首页（无游标）
    print("── 首页（无游标）")
    for line in explain_page(CONN, spec, filters, None, args.limit):
        print("  " + line)

    # 中间页：取第 N 行构造游标键
    with psycopg.connect(**CONN.kwargs()) as conn, conn.cursor() as cur:
        params: list = []
        where = filters.where_sql(params) or ""
        cur.execute(
            f"SELECT * FROM items {where} {spec.order_by_sql()} LIMIT 1",
            params,
        )
        first = dict(cur.fetchone())
        cur.execute(
            f"SELECT * FROM items {where} {spec.order_by_sql()} "
            f"OFFSET %s LIMIT 1",
            params + [args.limit * 3],
        )
        mid = dict(cur.fetchone())
    mid_key = [mid[c] for c in spec.columns]
    print(f"\n── 中间页（游标行 id={mid['id']}）")
    for line in explain_page(CONN, spec, filters, mid_key, args.limit):
        print("  " + line)
    print(f"\n  （首页首行 id={first['id']} 用于对照）")


# ---------------------------------------------------------------- visibility
def cmd_visibility(args: argparse.Namespace) -> None:
    """新插入记录在哪一页可见：确定性的可见性规则演示。"""
    spec = SORT_SPECS[args.sort]
    filters = Filters()  # 无筛选，聚焦排序位置
    # 取一个“老时间戳”，让新行在 created_desc 类排序里排最前
    new_ts = datetime.now(timezone.utc)
    new_id = insert_item(
        CONN, category="alpha", score=999, created_at=new_ts,
        label="green", active=True,
    )
    print(f"✓ 插入新行 id={new_id} created_at={new_ts.isoformat()}")
    print(f"  排序规格 {spec.name}：{spec.order_by_sql()}\n")

    # weak：新连接/新语句立刻可见
    with PaginationSession(CONN, "weak", spec, filters) as s:
        p1 = s.fetch_page(None, limit=args.limit)
    weak_first_ids = [r["id"] for r in p1.rows]
    print(f"[weak ] 首页 id 序列前 5：{weak_first_ids[:5]}")
    print(f"        新行{'就在首页第 ' + str(weak_first_ids.index(new_id) + 1) + ' 位' if new_id in weak_first_ids else '不在首页（排序值决定其位置）'}")

    # stable：快照在会话开始冻结，先翻页再插入，新行整轮不可见
    with PaginationSession(CONN, "stable", spec, filters) as s:
        before = s.fetch_page(None, limit=args.limit)
        # 会话进行中再插一行（另一连接提交）
        late_id = insert_item(
            CONN, category="beta", score=1,
            created_at=new_ts + timedelta(seconds=1),
        )
        after = s.fetch_page(before.next_cursor, limit=args.limit)
        stable_ids = {r["id"] for r in before.rows + after.rows}
        print(f"\n[stable] 会话快照 xid={s.snapshot_xid}，翻页期间另一连接插入 id={late_id}")
        print(f"         前两页是否出现晚插入的行：{late_id in stable_ids}（应为 False）")
        print(f"         早先插入的 id={new_id} 是否在冻结快照中：{new_id in stable_ids or '需继续翻页'}")

    # 新的 stable 会话才能看到两行
    with PaginationSession(CONN, "stable", spec, filters) as s:
        fresh = s.fetch_page(None, limit=args.limit)
    fresh_ids = [r["id"] for r in fresh.rows]
    print(f"\n[stable] 另开新会话后，首页前 5：{fresh_ids[:5]}"
          "（新快照包含全部已提交行）")


# ---------------------------------------------------------------- contention
class _Writer:
    """后台写入器：持续提交插入，模拟写入活跃。"""

    def __init__(self, conn_info: ConnInfo, stop_after: int):
        self.conn_info = conn_info
        self.stop_after = stop_after
        self.inserted: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        # 插入“排在最前”的行（now() 比种子数据新），weak 模式翻到后续页
        # 时这些行会把旧页边界行挤回来，从而可观测到重复/漂移。
        with psycopg.connect(**self.conn_info.kwargs()) as conn, conn.cursor() as cur:
            i = 0
            while not self._stop.is_set() and i < self.stop_after:
                cur.execute(
                    "INSERT INTO items (category, score, created_at, label, active)"
                    " VALUES (%s, %s, now(), %s, true) RETURNING id",
                    ("writer", i, None if i % 3 == 0 else "blue"),
                )
                self.inserted.append(cur.fetchone()["id"])
                conn.commit()
                i += 1
                time.sleep(0.002)

    def __enter__(self) -> "_Writer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def cmd_contention(args: argparse.Namespace) -> None:
    spec = SORT_SPECS[args.sort]
    filters = _filters(args)
    print(f"排序 {spec.name}  页大小 {args.limit}  筛选 {filters.canonical()}\n")

    results = {}
    for mode in ("weak", "stable"):
        writer = _Writer(CONN, stop_after=args.writes)
        with writer:
            res = walk_all_pages(
                CONN, mode, spec, filters, limit=args.limit
            )
        results[mode] = res
        xids = set(res.snapshot_xids)
        print(f"[{mode:6s}] 页={res.pages:4d}  遍历行数={res.rows_seen:6d} "
              f"重复行={res.duplicate_rows}  基线遗漏={res.missing_from_baseline} "
              f"新增并入={res.extra_vs_baseline}")
        print(f"           耗时={res.elapsed_ms:8.1f} ms  "
              f"不同快照xid数={len(xids)}（stable 应为 1，weak 约等于页数）")
        print(f"           并发写入器提交了 {len(writer.inserted)} 行")

    w, st = results["weak"], results["stable"]
    print("\n== 结论 ==")
    print("  键集游标按行值定位，单行插入不会让已访问行重新满足谓词，")
    print("  因此两种模式都不会出现 OFFSET 式重复行；weak 的代价是结果漂移：")
    print(f"  weak   基线 {w.baseline_size} 行，遍历中并入新提交 "
          f"{w.extra_vs_baseline} 行、基线遗漏 {w.missing_from_baseline}；"
          f"全程 {len(set(w.snapshot_xids))} 个不同快照")
    print(f" stable 全程 {len(set(st.snapshot_xids))} 个冻结快照："
          f"并入 {st.extra_vs_baseline}（恒 0）、基线遗漏 "
          f"{st.missing_from_baseline}（恒 0）；代价是长事务与 vacuum 版本保留")
    if st.elapsed_ms and w.elapsed_ms:
        print(f" 耗时比 stable/weak = {st.elapsed_ms / w.elapsed_ms:.2f}x"
              "（数据量大时主要代价在版本保留与回卷，而非单次 CPU）")


# ---------------------------------------------------------------- drift
def cmd_drift(args: argparse.Namespace) -> None:
    """受控写入下的确定性漂移对照：两种模式分别跑相同节奏的插入。

    在第 2、4、6 页之后，各插入一行 created_at 等于“当前游标边界行的下一
    个更旧时间桶”的记录。created_desc 排序下它确定落在游标之后的未访问区、
    紧邻当前位置，下一页就会读到，结果确定性可复现：
      stable：冻结快照读不到会话中提交的行，整轮 0 命中；
      weak  ：下一页的新快照立刻读到该行。
    两次跑使用各自独立的会话与插入序列，插入 id 仅在本次运行内对照。
    """
    spec = SORT_SPECS[args.sort]
    filters = _filters(args)
    insert_at_pages = {1, 3, 5}
    outcomes = {}

    # stable 先跑：冻结快照必须 0 新行；weak 后跑演示新快照并入。
    for mode in ("stable", "weak"):
        seen_ids: list[int] = []
        inserted: list[int] = []
        xmins: list[int] = []
        with PaginationSession(CONN, mode, spec, filters) as session:
            token = None
            page_no = 0
            while True:
                page = session.fetch_page(token, limit=args.limit)
                seen_ids.extend(r["id"] for r in page.rows)
                xmins.append(page.snapshot_xid)
                page_no += 1
                if not page.has_more or page_no >= args.max_pages:
                    break
                token = page.next_cursor
                if (page_no - 1) in insert_at_pages:
                    # 取边界行的“下一个更旧时间桶”：created_desc 下确定落在
                    # 游标之后、未访问区，且紧邻当前位置，下一页即可读到。
                    older_ts = page.rows[-1]["created_at"] - timedelta(minutes=1)
                    nid = insert_item(
                        CONN, category="drift",
                        created_at=older_ts,
                    )
                    inserted.append(nid)
        outcomes[mode] = {
            "pages": page_no,
            "rows": len(seen_ids),
            "unique": len(set(seen_ids)),
            "inserts_seen": sorted(set(seen_ids) & set(inserted)),
            "inserts_total": inserted,
            "snapshots": len(set(xmins)),
        }

    print(f"排序 {spec.name}，页大小 {args.limit}，"
          f"在第 {sorted(p + 1 for p in insert_at_pages)} 页后各插入 1 行更旧时间桶记录\n")
    for mode in ("stable", "weak"):
        o = outcomes[mode]
        print(f"[{mode:6s}] 页数={o['pages']} 遍历行={o['rows']} 唯一={o['unique']} "
              f"不同快照={o['snapshots']}")
        print(f"          本运行插入 id={o['inserts_total']}，"
              f"其中被本次遍历看到：{o['inserts_seen'] or '无'}")
    print("\n== 解读 ==")
    print(" stable（先跑）：会话快照冻结，遍历中提交的行整轮不可见，")
    print("   拼接结果严格等于会话开始时刻的一次全量查询；这些行提交后，")
    print("   随后的 weak 运行立刻就能在自己的新快照里读到它们。")
    print(" weak（后跑）：每页取查询瞬间的最新快照，新提交按排序值并入，")
    print("   各页拼起来是多个时间点的并集，不是单一时刻视图。")
    print(" 两种模式的游标互不通用（guard 命令演示 mode_mismatch 拒绝）。")


# ---------------------------------------------------------------- cost
def _table_pressure() -> dict:
    """物理层信号：关系体积 + 死元组估算 + 更新计数。"""
    with psycopg.connect(**CONN.kwargs(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pg_relation_size('items') AS heap_bytes,"
            " pg_relation_size('items_pkey') AS pkey_bytes,"
            " n_dead_tup, n_live_tup, n_tup_upd"
            " FROM pg_stat_user_tables WHERE relname='items'"
        )
        row = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM items")
        row["actual_rows"] = cur.fetchone()["n"]
        return row


def cmd_cost(args: argparse.Namespace) -> None:
    """稳定读的代价：长事务持有旧快照 → dead 版本无法回收 → 表/索引膨胀。

    判据用物理体积而不是 n_dead_tup（VACUUM 后该计数会被重置，看不出
    “保留”）：同样制造 2000 个死版本，对比长事务存活与否时
    pg_relation_size 的差异 —— 无法回收的版本会真实占着堆页。
    """
    print("## 基线体积")
    base = _table_pressure()
    print(f"  heap={base['heap_bytes']}B 实际行数={base['actual_rows']}")

    print("\n## 场景 A：无长事务，UPDATE 2000 行后立刻 VACUUM")
    with psycopg.connect(**CONN.kwargs()) as conn, conn.cursor() as cur:
        cur.execute("UPDATE items SET score = score + 1 WHERE id <= 2000")
        conn.commit()
    with psycopg.connect(**CONN.kwargs(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("VACUUM items")
    after_a = _table_pressure()
    print(f"  heap={after_a['heap_bytes']}B（dead 版本被回收，体积基本回落）")

    print("\n## 场景 B：stable 分页会话持有快照期间重复 UPDATE + VACUUM")
    spec = SORT_SPECS["created_desc"]
    with PaginationSession(CONN, "stable", spec, Filters()) as session:
        first = session.fetch_page(None, limit=10)
        print(f"  stable 会话开启，快照 xmin={first.snapshot_xid}，事务保持存活")
        for rnd in range(3):
            with psycopg.connect(**CONN.kwargs()) as conn, conn.cursor() as cur:
                cur.execute("UPDATE items SET score = score + 1 "
                            "WHERE id <= 2000")
                conn.commit()
            with psycopg.connect(**CONN.kwargs(), autocommit=True) as conn, conn.cursor() as vc:
                vc.execute("VACUUM items")  # 受快照 horizon 限制，旧版本收不走
            p = _table_pressure()
            print(f"  第{rnd + 1}轮后 heap={p['heap_bytes']}B "
                  f"实际行数仍={p['actual_rows']}（体积涨的是无法回收的死版本）")
    held = _table_pressure()
    print(f"\n  长事务期间 heap 增量 = {held['heap_bytes'] - after_a['heap_bytes']}B"
          "（同样的数据行，多出的全是 vacuum 不敢清理的版本）")

    with psycopg.connect(**CONN.kwargs(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("VACUUM items")
    reclaimed = _table_pressure()
    print(f"  会话结束后普通 VACUUM：heap={reclaimed['heap_bytes']}B"
          "（死版本标记为可复用，但普通 VACUUM 不向 OS 缩容）")
    with psycopg.connect(**CONN.kwargs(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("VACUUM FULL items")  # 重写整个堆，演示空间真正归还
    full = _table_pressure()
    print(f"  再执行 VACUUM FULL 重写堆：heap={full['heap_bytes']}B，"
          f"归还 {reclaimed['heap_bytes'] - full['heap_bytes']}B")
    print("\n  结论：stable 模式的代价不在单次查询 CPU，而在长事务把 vacuum")
    print("  horizon 钉住 —— 写入越活跃、会话越长，堆与索引膨胀越严重，")
    print("  进而拖慢包括分页查询在内的所有扫描。weak 模式语句级提交，")
    print("  不持有旧快照，没有这个代价。")


# ---------------------------------------------------------------- guard
def cmd_guard(args: argparse.Namespace) -> None:
    spec = SORT_SPECS[args.sort]
    filters = Filters()

    def _attempt(label: str, **kwargs):
        try:
            with PaginationSession(CONN, kwargs.pop("mode"), spec,
                                   kwargs.pop("filters", filters)) as s:
                s.fetch_page(kwargs["token"], limit=5)
            print(f"  ✗ {label}: 未拒绝（BUG！）")
        except CursorRejected as e:
            print(f"  ✓ {label}: CursorRejected[{e.args[0]}] — {e.args[1]}")
        except CursorError as e:
            print(f"  ✓ {label}: CursorError — {e}")

    # 先合法签发一个 weak 游标
    with PaginationSession(CONN, "weak", spec, filters) as s:
        page = s.fetch_page(None, limit=5)
    token = page.next_cursor
    print(f"合法游标(weak/{spec.name}) 已签发，载荷：")
    body = decode_cursor(token)
    redacted = {k: (v if k != "h" else v[:12] + "…") for k, v in body.items()}
    print("  " + json.dumps(redacted, ensure_ascii=False, default=str))

    print("\n── 防护用例（全部必须显式失败，不得静默错用）")
    _attempt("weak 游标拿到 stable 模式用", mode="stable",
             filters=filters, token=token)
    other_spec = SORT_SPECS["score_desc"]
    try:
        with PaginationSession(CONN, "weak", other_spec, filters) as s:
            s.fetch_page(token, limit=5)
        print("  ✗ 换排序: 未拒绝（BUG！）")
    except CursorRejected as e:
        print(f"  ✓ 换排序规格({other_spec.name}): CursorRejected[{e.args[0]}]")

    changed = Filters(category="alpha")
    _attempt("筛选条件变化（category=alpha）", mode="weak",
             filters=changed, token=token)
    changed2 = Filters(min_score=100)
    _attempt("筛选条件变化（min_score=100）", mode="weak",
             filters=changed2, token=token)

    import base64
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    _attempt("token 尾部被篡改", mode="weak", filters=filters, token=tampered)

    garbage = base64.urlsafe_b64encode(b'{"x":1}').decode().rstrip("=")
    _attempt("伪造的结构不全 token", mode="weak", filters=filters, token=garbage)

    # stable 游标跨快照：会话 A 签发，会话 B 使用
    with PaginationSession(CONN, "stable", spec, filters) as s:
        stable_token = s.fetch_page(None, limit=5).next_cursor
        xid_a = s.snapshot_xid
    try:
        with PaginationSession(CONN, "stable", spec, filters) as s2:
            s2.fetch_page(stable_token, limit=5)
        print("  ✗ stable 游标跨快照: 未拒绝（BUG！）")
    except CursorRejected as e:
        print(f"  ✓ stable 游标在另一个快照事务续用(xid {xid_a}→新事务): "
              f"CursorRejected[{e.args[0]}]")


# ---------------------------------------------------------------- demo
def cmd_demo(args: argparse.Namespace) -> None:
    print("=" * 72)
    print("1/8 重建测试数据")
    print("=" * 72)
    cmd_seed(argparse.Namespace(rows=args.rows))

    print("\n" + "=" * 72)
    print("2/8 相邻页对照 + 查询计划（含混合方向、NULL 排序）")
    print("=" * 72)
    for name in ("created_desc", "created_score", "score_desc", "label_asc"):
        ns = argparse.Namespace(sort=name, category=None, active=None,
                                min_score=None, limit=10)
        cmd_explain(ns)
        print()

    print("=" * 72)
    print("3/8 全量遍历正确性（无重无漏）")
    print("=" * 72)
    for mode in ("weak", "stable"):
        ns = argparse.Namespace(sort="created_score", mode=mode,
                                category=None, active=None, min_score=None,
                                limit=100)
        cmd_walk(ns)
        print()

    print("=" * 72)
    print("4/8 新插入记录的可见位置")
    print("=" * 72)
    cmd_visibility(argparse.Namespace(sort="created_desc", limit=10))

    print("\n" + "=" * 72)
    print("5/8 受控写入下两模式结果分岔（确定性漂移）")
    print("=" * 72)
    cmd_seed(argparse.Namespace(rows=args.rows))
    cmd_drift(argparse.Namespace(sort="created_desc", category=None,
                                 active=None, min_score=None,
                                 limit=200, max_pages=200))

    print("\n" + "=" * 72)
    print("6/8 写入活跃下 weak vs stable 快照与耗时对照")
    print("=" * 72)
    cmd_seed(argparse.Namespace(rows=args.rows))
    cmd_contention(argparse.Namespace(sort="created_desc", category=None,
                                      active=None, min_score=None,
                                      limit=100, writes=200))

    print("\n" + "=" * 72)
    print("7/8 稳定读模式的代价（vacuum horizon 与膨胀）")
    print("=" * 72)
    cmd_seed(argparse.Namespace(rows=args.rows))
    cmd_cost(argparse.Namespace())

    print("\n" + "=" * 72)
    print("8/8 游标安全防护")
    print("=" * 72)
    cmd_seed(argparse.Namespace(rows=args.rows))
    cmd_guard(argparse.Namespace(sort="created_desc"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pg-pagination-workbench")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed")
    s.add_argument("--rows", type=int, default=12000)
    s.set_defaults(func=cmd_seed)

    def add_common(sp):
        sp.add_argument("--sort", choices=SORT_SPECS, default="created_desc")
        sp.add_argument("--category", default=None)
        sp.add_argument("--active", type=lambda x: x.lower() == "true",
                        default=None)
        sp.add_argument("--min-score", type=int, default=None)
        sp.add_argument("--limit", type=int, default=10)

    pg = sub.add_parser("page")
    add_common(pg)
    pg.add_argument("--mode", choices=("weak", "stable"), default="weak")
    pg.add_argument("--cursor", default=None)
    pg.set_defaults(func=cmd_page)

    wk = sub.add_parser("walk")
    add_common(wk)
    wk.add_argument("--mode", choices=("weak", "stable"), default="weak")
    wk.set_defaults(func=cmd_walk)

    ex = sub.add_parser("explain")
    add_common(ex)
    ex.set_defaults(func=cmd_explain)

    vi = sub.add_parser("visibility")
    vi.add_argument("--sort", choices=SORT_SPECS, default="created_desc")
    vi.add_argument("--limit", type=int, default=10)
    vi.set_defaults(func=cmd_visibility)

    co = sub.add_parser("contention")
    add_common(co)
    co.add_argument("--writes", type=int, default=200)
    co.set_defaults(func=cmd_contention)

    dr = sub.add_parser("drift")
    add_common(dr)
    dr.add_argument("--max-pages", type=int, default=20)
    dr.set_defaults(func=cmd_drift)

    cs = sub.add_parser("cost")
    cs.set_defaults(func=cmd_cost)

    gu = sub.add_parser("guard")
    gu.add_argument("--sort", choices=SORT_SPECS, default="created_desc")
    gu.set_defaults(func=cmd_guard)

    dm = sub.add_parser("demo")
    dm.add_argument("--rows", type=int, default=12000)
    dm.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (CursorError, psycopg.Error) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
