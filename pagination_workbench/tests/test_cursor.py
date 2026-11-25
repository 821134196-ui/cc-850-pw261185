"""游标安全：HMAC、排序/筛选/模式/快照指纹，任何不匹配都必须显式拒绝。"""
from __future__ import annotations

import base64

import pytest

from pagination_workbench.cursor import (
    CursorError,
    CursorRejected,
    decode_cursor,
    encode_cursor,
    validate_cursor_context,
)
from pagination_workbench.query import PaginationSession
from pagination_workbench.sorts import Filters, SORT_SPECS


@pytest.fixture()
def spec():
    return SORT_SPECS["created_score"]


def _issue(mode, spec, filters, snapshot_xid=None, key=None):
    return encode_cursor(
        mode=mode,
        sort_spec=spec,
        filters=filters,
        last_key=key or [__import__("datetime").datetime(2026, 1, 1), 5, 7],
        snapshot_xid=snapshot_xid,
    )


def test_roundtrip_and_signature(spec):
    token = _issue("weak", spec, Filters())
    body = decode_cursor(token)
    assert body["m"] == "weak"
    assert body["s"] == spec.signature()
    assert body["f"] == Filters().fingerprint()


def test_tampered_token_rejected(spec):
    token = _issue("weak", spec, Filters())
    tampered = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    with pytest.raises(CursorError):
        decode_cursor(tampered)


def test_garbage_token_rejected():
    bad = base64.urlsafe_b64encode(b'{"hello":1}').decode().rstrip("=")
    with pytest.raises(CursorError):
        decode_cursor(bad)


def test_mode_mismatch_rejected(spec):
    token = _issue("weak", spec, Filters())
    body = decode_cursor(token)
    with pytest.raises(CursorRejected) as ei:
        validate_cursor_context(
            body, mode="stable", sort_spec=spec,
            filters=Filters(), snapshot_xid=None,
        )
    assert ei.value.args[0] == "mode_mismatch"


def test_sort_mismatch_rejected(spec):
    token = _issue("weak", spec, Filters())
    body = decode_cursor(token)
    with pytest.raises(CursorRejected) as ei:
        validate_cursor_context(
            body, mode="weak", sort_spec=SORT_SPECS["score_desc"],
            filters=Filters(), snapshot_xid=None,
        )
    assert ei.value.args[0] == "sort_mismatch"


def test_filter_mismatch_rejected(spec):
    token = _issue("weak", spec, Filters())
    body = decode_cursor(token)
    for changed in (Filters(category="alpha"), Filters(min_score=10),
                    Filters(active=False)):
        with pytest.raises(CursorRejected) as ei:
            validate_cursor_context(
                body, mode="weak", sort_spec=spec,
                filters=changed, snapshot_xid=None,
            )
        assert ei.value.args[0] == "filter_mismatch"


def test_snapshot_mismatch_rejected_stable(spec):
    token = _issue("stable", spec, Filters(), snapshot_xid=111)
    body = decode_cursor(token)
    with pytest.raises(CursorRejected) as ei:
        validate_cursor_context(
            body, mode="stable", sort_spec=spec,
            filters=Filters(), snapshot_xid=222,
        )
    assert ei.value.args[0] == "snapshot_mismatch"


def test_end_to_end_cursor_cannot_cross_modes(conn_info, tiny_table, spec):
    """weak 会话签发的游标，stable 会话必须拒绝（两模式不共用游标）。"""
    with PaginationSession(conn_info, "weak", spec, Filters()) as weak:
        token = weak.fetch_page(None, limit=2).next_cursor
    with PaginationSession(conn_info, "stable", spec, Filters()) as stable:
        with pytest.raises(CursorRejected) as ei:
            stable.fetch_page(token, limit=2)
        assert ei.value.args[0] == "mode_mismatch"


def test_stable_cursor_cannot_cross_snapshot(conn_info, tiny_table, spec):
    with PaginationSession(conn_info, "stable", spec, Filters()) as s1:
        token = s1.fetch_page(None, limit=2).next_cursor
    # 另一个 stable 会话 = 另一个快照
    with PaginationSession(conn_info, "stable", spec, Filters()) as s2:
        with pytest.raises(CursorRejected) as ei:
            s2.fetch_page(token, limit=2)
        assert ei.value.args[0] == "snapshot_mismatch"


def test_filter_fingerprint_deterministic():
    a = Filters(category="x", active=True, min_score=5)
    b = Filters(min_score=5, active=True, category="x")  # 字段顺序不同
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != Filters(category="y").fingerprint()
