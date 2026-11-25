"""复合游标 token：签发、解析、强校验。

token 结构（base64url 编码的 JSON）::

    v   版本号
    m   一致性模式："weak" | "stable"
    s   排序规格签名（SortSpec.signature）
    f   筛选条件指纹（Filters.fingerprint）
    k   上一页最后一行的排序键值（按 SortSpec.columns 顺序，含 id）
    i   签发时的快照 xid（stable 模式额外校验用）
    h   HMAC-SHA256(secret, 其余字段)

为什么游标必须自带指纹而不能只存最后一行 id
--------------------------------------------
缓存或筛选条件变化后，客户端可能拿着旧 token 来请求。如果服务端只按 id
续查，会在新的结果集里静默错位（漏行或重复行）。指纹 + 模式 + HMAC
三重校验让这种情况显式报错（CursorRejected），而不是静默错用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

from .config import CURSOR_SECRET, CURSOR_VERSION
from .sorts import Filters, SortSpec


class CursorError(ValueError):
    """游标不可用的基类（签名损坏 / 过期结构 / 内容不合法）。"""


def _json_default(obj: Any) -> str:
    # 排序键里唯一的非标量类型是 timestamptz → 带时区 ISO 字符串，
    # 回绑时 psycopg 能直接把 ISO 字符串还原成 timestamptz。
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise CursorError(f"游标含不可序列化的类型: {type(obj)!r}")


class CursorRejected(CursorError):
    """游标本身合法，但与当前查询上下文不匹配，禁止静默续用。

    原因码：
      mode_mismatch      弱一致 / 稳定读模式之间互窜游标
      sort_mismatch      排序规格变了
      filter_mismatch    筛选条件变了（含“缓存了不同筛选的首页”场景）
      snapshot_mismatch stable 模式下游标属于另一个快照
    """


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except Exception as exc:  # noqa: BLE001
        raise CursorError("cursor 不是合法 base64url") from exc


def _sign(body: dict[str, Any]) -> str:
    blob = json.dumps(
        body, separators=(",", ":"), sort_keys=True, default=_json_default
    ).encode()
    mac = hmac.new(CURSOR_SECRET, blob, hashlib.sha256).digest()
    return _b64e(mac)


def encode_cursor(
    *,
    mode: str,
    sort_spec: SortSpec,
    filters: Filters,
    last_key: list[Any],
    snapshot_xid: int | None = None,
) -> str:
    """用上一页最后一行的排序键值签发下一页游标。"""
    body = {
        "v": CURSOR_VERSION,
        "m": mode,
        "s": sort_spec.signature(),
        "f": filters.fingerprint(),
        "k": last_key,
        "i": snapshot_xid,
    }
    body["h"] = _sign({k: val for k, val in body.items() if k != "h"})
    return _b64e(
        json.dumps(body, separators=(",", ":"), default=_json_default).encode()
    )


def decode_cursor(token: str) -> dict[str, Any]:
    """只做结构与签名校验，不做查询上下文校验。"""
    try:
        body = json.loads(_b64d(token))
    except CursorError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CursorError("cursor 内容不是合法 JSON") from exc

    required = {"v", "m", "s", "f", "k", "i", "h"}
    if not isinstance(body, dict) or not required.issubset(body):
        raise CursorError("cursor 字段缺失或结构错误")
    if body["v"] != CURSOR_VERSION:
        raise CursorError(f"cursor 版本不支持: {body['v']}")
    if body["m"] not in ("weak", "stable"):
        raise CursorError(f"未知一致性模式: {body['m']}")

    provided = body.pop("h")
    expected = _sign(body)
    if not hmac.compare_digest(provided, expected):
        raise CursorError("cursor HMAC 校验失败（可能被篡改或密钥已轮换）")
    if not isinstance(body["k"], list):
        raise CursorError("cursor 排序键格式错误")
    return body


def validate_cursor_context(
    body: dict[str, Any],
    *,
    mode: str,
    sort_spec: SortSpec,
    filters: Filters,
    snapshot_xid: int | None,
) -> None:
    """结构合法的游标，还要与当前查询上下文逐项比对。

    任何一项不一致都抛 CursorRejected —— 调用方应当重新从首页开始，
    而不是拿旧游标在新结果集里“接着查”。
    """
    if body["m"] != mode:
        raise CursorRejected(
            "mode_mismatch",
            f"游标属于 {body['m']} 模式，不能在 {mode} 模式下使用"
            "（两种模式不得共用游标）",
        )
    if body["s"] != sort_spec.signature():
        raise CursorRejected(
            "sort_mismatch",
            f"游标排序签名 {body['s'][:8]} 与当前 {sort_spec.name}"
            f"({sort_spec.signature()[:8]}) 不一致",
        )
    if body["f"] != filters.fingerprint():
        raise CursorRejected(
            "filter_mismatch",
            "游标携带的筛选指纹与当前筛选条件不一致"
            f"（游标 {body['f'][:8]} / 当前 {filters.fingerprint()[:8]}），"
            "筛选或缓存键变化后必须从头分页",
        )
    if mode == "stable" and body["i"] != snapshot_xid:
        raise CursorRejected(
            "snapshot_mismatch",
            f"游标快照 xid={body['i']} 与当前事务快照 xid={snapshot_xid}"
            " 不一致：稳定读游标只能在签发它的快照事务内续用",
        )
