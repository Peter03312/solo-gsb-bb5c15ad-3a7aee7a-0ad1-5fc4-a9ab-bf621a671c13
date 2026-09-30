"""采样仪器补传：读数批次提交 HTTP 集成测试。

运行：PYTHONPATH=. python -m pytest tests/test_batch_api.py -q

覆盖：
- 整批 created、逐项顺序、按最终状态重算审定；
- 空批次 / 批次内重复 reading_id / 窗口外读数 / 最终同刻 → 422 整批拒收；
- 内容冲突 / 旧修订号 → 409 整批拒收，活动读数与修订历史均不变；
- 两条读数以更高修订号互换采样时刻：两种写入顺序都必须成功（最终状态校验）；
- 同批重放全部 idempotent 且不产生新修订历史；
- created/updated/idempotent 混合；
- 既有单条提交仍可用；已签发证书快照不随后续补传变化。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        yield c


WINDOW_BASE = "2026-10-01T00:00:00Z"


def make_window(client, start=WINDOW_BASE, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=600.0, name=None):
    body = {
        "start_at": start,
        "end_at": (datetime.fromisoformat(start.replace("Z", "+00:00"))
                   + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    }
    if name:
        body["name"] = name
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def reading(rid, rev, minute, temp, *, second=0, tz=None):
    ts = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute, seconds=second)
    if tz is not None:
        ts = ts.astimezone(timezone(timedelta(hours=tz)))
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": ts.isoformat(),
        "temperature": temp,
    }


BATCH_PATH = "/api/v1/windows/{}/readings/batch"


def submit_batch(client, wid, items):
    return client.post(BATCH_PATH.format(wid), json={"readings": items})


def revision_count(db_path: str, window_id: int, reading_id: str) -> int:
    """直接查修订历史行数（只读取已提交数据）。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM reading_revisions WHERE window_id=? AND reading_id=?",
            (window_id, reading_id),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def active_map(client, wid):
    rows = client.get(f"/api/v1/windows/{wid}/readings").json()
    return {r["reading_id"]: r for r in rows}


# ---------------------------------------------------------------- 基本成功路径

def test_batch_create_all_created_and_evaluation_recomputed(client):
    wid = make_window(client)["id"]
    items = [reading("a", 1, 0, 20.0), reading("b", 1, 10, 20.1),
             reading("c", 1, 20, 20.2)]
    r = submit_batch(client, wid, items)
    assert r.status_code == 201, r.text
    body = r.json()
    assert [e["effect"] for e in body["effects"]] == ["created", "created", "created"]
    # 逐项带回读数，顺序与请求一致
    assert [e["reading_id"] for e in body["effects"]] == ["a", "b", "c"]
    for e, item in zip(body["effects"], items):
        assert e["reading"]["revision"] == 1
        assert e["reading"]["measured_at"].endswith("Z")
        assert e["reading"]["temperature"] == float(item["temperature"])
    # 审定按整批最终状态重算
    ev = body["current_evaluation"]
    assert ev["reading_count"] == 3
    assert ev["verdict"] == "pending"  # 边界空白 -> 待定
    # 与 evaluation 端点一致
    assert client.get(f"/api/v1/windows/{wid}/evaluation").json() == ev


def test_batch_missing_window_404(client):
    r = submit_batch(client, 999, [reading("a", 1, 0, 20.0)])
    assert r.status_code == 404


def test_batch_empty_rejected_422(client):
    wid = make_window(client)["id"]
    r = submit_batch(client, wid, [])
    assert r.status_code == 422


def test_batch_tz_offset_normalized(client):
    """时区沿用单条规则：+08:00 表示按时刻归一为 UTC。"""
    wid = make_window(client)["id"]
    # 2026-10-01T08:00:00+08:00 == 00:00:00Z
    r = submit_batch(client, wid, [reading("a", 1, 0, 20.0, tz=8)])
    assert r.status_code == 201, r.text
    assert r.json()["effects"][0]["reading"]["measured_at"] == "2026-10-01T00:00:00Z"
    # 不带时区 -> 422
    r = submit_batch(client, wid, [{
        "reading_id": "naive", "revision": 1,
        "measured_at": "2026-10-01T00:05:00", "temperature": 20.0}])
    assert r.status_code == 422


# ---------------------------------------------------------------- 整批拒收

def test_batch_duplicate_reading_id_rejected(client):
    wid = make_window(client)["id"]
    r = submit_batch(client, wid, [
        reading("x", 1, 0, 20.0), reading("x", 2, 5, 20.0)])
    assert r.status_code == 422 and "重复" in r.json()["detail"]
    assert active_map(client, wid) == {}


def test_batch_outside_window_rejected_and_atomic(client):
    wid = make_window(client)["id"]
    # 第一项合法 created、第二项越界 -> 整批拒收，第一项也不得落库
    r = submit_batch(client, wid, [
        reading("a", 1, 0, 20.0), reading("b", 1, 61, 20.0)])
    assert r.status_code == 422 and "超出窗口" in r.json()["detail"]
    assert active_map(client, wid) == {}
    assert revision_count(settings.db_path, wid, "a") == 0


def test_batch_same_timestamp_rejected(client):
    wid = make_window(client)["id"]
    r = submit_batch(client, wid, [
        reading("a", 1, 5, 20.0), reading("b", 1, 5, 21.0)])
    assert r.status_code == 422 and "同刻" in r.json()["detail"]
    assert active_map(client, wid) == {}


def test_batch_content_conflict_rolls_back_active_and_history(client):
    wid = make_window(client)["id"]
    submit_batch(client, wid, [reading("a", 1, 0, 20.0)])
    # 批次内：新读数 b（应 created）+ 对 a rev1 改温度（409）
    r = submit_batch(client, wid, [
        reading("b", 1, 5, 20.0), reading("a", 1, 0, 99.9)])
    assert r.status_code == 409 and "内容冲突" in r.json()["detail"]
    rows = active_map(client, wid)
    assert set(rows) == {"a"} and rows["a"]["temperature"] == 20.0
    # b 的历史行也不得写入；a 仍只有 rev1 一行历史
    assert revision_count(settings.db_path, wid, "b") == 0
    assert revision_count(settings.db_path, wid, "a") == 1


def test_batch_lower_revision_rejected_and_atomic(client):
    wid = make_window(client)["id"]
    submit_batch(client, wid, [reading("a", 2, 0, 20.0)])
    r = submit_batch(client, wid, [
        reading("b", 1, 5, 20.0), reading("a", 1, 0, 20.0)])
    assert r.status_code == 409 and "旧修订号" in r.json()["detail"]
    rows = active_map(client, wid)
    assert set(rows) == {"a"} and rows["a"]["revision"] == 2
    assert revision_count(settings.db_path, wid, "b") == 0


def test_batch_reject_when_final_timestamp_clashes_with_untouched_reading(client):
    wid = make_window(client)["id"]
    submit_batch(client, wid, [
        reading("a", 1, 0, 20.0), reading("b", 1, 5, 20.0)])
    # 只提交 a rev2 挪到 b 的时刻，b 不在批次里腾位 -> 最终状态同刻 -> 422
    r = submit_batch(client, wid, [reading("a", 2, 5, 20.0)])
    assert r.status_code == 422
    rows = active_map(client, wid)
    assert rows["a"]["revision"] == 1 and rows["a"]["measured_at"] == "2026-10-01T00:00:00Z"


# ---------------------------------------------------------------- 同刻互换（最终状态校验，与顺序无关）

def _swap_batch(client, wid, order):
    """a<->b 互换采样时刻，均升 rev。order=('ab'|'ba') 控制批次内排列。"""
    items_ab = [
        reading("a", 2, 5, 20.0),   # a: 00:00 -> 00:05
        reading("b", 2, 0, 20.0),   # b: 00:05 -> 00:00
    ]
    items = items_ab if order == "ab" else list(reversed(items_ab))
    return submit_batch(client, wid, items)


def test_batch_swap_timestamps_succeeds_both_orders(client):
    for order in ("ab", "ba"):
        # 每个顺序用独立窗口，保证 rev2 对双方都是“更高修订号”
        wid = make_window(client)["id"]
        submit_batch(client, wid, [
            reading("a", 1, 0, 20.0), reading("b", 1, 5, 20.0)])
        r = _swap_batch(client, wid, order)
        assert r.status_code == 201, r.text
        rows = active_map(client, wid)
        assert rows["a"]["revision"] == 2 and rows["a"]["measured_at"] == "2026-10-01T00:05:00Z"
        assert rows["b"]["revision"] == 2 and rows["b"]["measured_at"] == "2026-10-01T00:00:00Z"
        # 三阶段写入的哨兵时刻不得残留在活动表：全部读数时刻仍在窗口内
        listed = client.get(f"/api/v1/windows/{wid}/readings").json()
        assert len(listed) == 2
        assert all(WINDOW_BASE <= x["measured_at"] <= "2026-10-01T01:00:00Z"
                   for x in listed), listed


def test_batch_three_way_rotation_succeeds_both_orders(client):
    """三条读数轮换采样时刻（a->b 位、b->c 位、c->a 位），两种排列都接受。"""
    for order in ("abc", "cba"):
        wid = make_window(client)["id"]
        submit_batch(client, wid, [
            reading("a", 1, 0, 20.0), reading("b", 1, 5, 20.0),
            reading("c", 1, 10, 20.0)])
        rot = [reading("a", 2, 5, 20.0), reading("b", 2, 10, 20.0),
               reading("c", 2, 0, 20.0)]
        if order == "cba":
            rot = list(reversed(rot))
        r = submit_batch(client, wid, rot)
        assert r.status_code == 201, r.text
        rows = active_map(client, wid)
        assert rows["a"]["measured_at"] == "2026-10-01T00:05:00Z"
        assert rows["b"]["measured_at"] == "2026-10-01T00:10:00Z"
        assert rows["c"]["measured_at"] == "2026-10-01T00:00:00Z"


def test_batch_swap_requires_higher_revision(client):
    """互换但其中一方修订号不高于当前 -> 409 整批拒收。"""
    wid = make_window(client)["id"]
    submit_batch(client, wid, [
        reading("a", 1, 0, 20.0), reading("b", 1, 5, 20.0)])
    # a 用 rev2 换位，b 仍用 rev1 换位（同修订号+内容变化 => 冲突）
    r = submit_batch(client, wid, [
        reading("a", 2, 5, 20.0), reading("b", 1, 0, 20.0)])
    assert r.status_code == 409
    rows = active_map(client, wid)
    assert rows["a"]["revision"] == 1 and rows["a"]["measured_at"] == "2026-10-01T00:00:00Z"
    assert rows["b"]["revision"] == 1 and rows["b"]["measured_at"] == "2026-10-01T00:05:00Z"


def test_batch_swap_recomputes_evaluation_on_final_state(client):
    """互换前 a@00:00 超温、b@00:10 正常；互换后超温覆盖段移到 00:10 起，
    审定按最终状态重算，两种批次顺序结果一致。"""
    for order in ("ab", "ba"):
        wid = make_window(client, max_gap=600.0)["id"]
        submit_batch(client, wid, [
            reading("a", 1, 0, 23.0), reading("b", 1, 10, 20.0),
            reading("c", 1, 20, 20.0), reading("d", 1, 30, 20.0)])
        before = client.get(f"/api/v1/windows/{wid}/evaluation").json()
        assert before["max_continuous_exceeded_seconds"] == 600.0
        # 保持温度随读数一起换位：a 带超温到 00:10，b 带常温到 00:00
        swap = [reading("a", 2, 10, 23.0), reading("b", 2, 0, 20.0)]
        if order == "ba":
            swap = list(reversed(swap))
        r = submit_batch(client, wid, swap)
        assert r.status_code == 201, r.text
        ev = r.json()["current_evaluation"]
        # 互换后超温读数 a 在 00:10，覆盖 00:10->00:20 仍是 600s
        assert ev["max_continuous_exceeded_seconds"] == 600.0
        seg = ev["exceeded_segments"][0]
        assert seg["start"] == "2026-10-01T00:10:00Z"
        assert seg["end"] == "2026-10-01T00:20:00Z"


# ---------------------------------------------------------------- 幂等重放 / 混合 / 历史

def test_batch_replay_is_idempotent_and_creates_no_revision(client):
    wid = make_window(client)["id"]
    items = [reading("a", 1, 0, 20.0), reading("b", 1, 5, 20.1)]
    assert submit_batch(client, wid, items).status_code == 201
    hist_a_before = revision_count(settings.db_path, wid, "a")
    hist_b_before = revision_count(settings.db_path, wid, "b")
    r = submit_batch(client, wid, items)
    assert r.status_code == 201
    assert [e["effect"] for e in r.json()["effects"]] == ["idempotent", "idempotent"]
    assert revision_count(settings.db_path, wid, "a") == hist_a_before
    assert revision_count(settings.db_path, wid, "b") == hist_b_before
    # 活动读数内容不变
    rows = active_map(client, wid)
    assert rows["a"]["revision"] == 1 and rows["b"]["revision"] == 1


def test_batch_mixed_created_updated_idempotent(client):
    wid = make_window(client)["id"]
    submit_batch(client, wid, [
        reading("a", 1, 0, 20.0), reading("b", 1, 5, 23.0)])
    r = submit_batch(client, wid, [
        reading("a", 1, 0, 20.0),    # idempotent
        reading("b", 2, 5, 20.0),    # updated（温度恢复正常）
        reading("c", 1, 10, 20.0),   # created
    ])
    assert r.status_code == 201, r.text
    assert [e["effect"] for e in r.json()["effects"]] == ["idempotent", "updated", "created"]
    rows = active_map(client, wid)
    assert rows["a"]["revision"] == 1
    assert rows["b"]["revision"] == 2 and rows["b"]["temperature"] == 20.0
    assert rows["c"]["revision"] == 1
    # b 的旧修订历史标记 superseded，且共两行历史
    assert revision_count(settings.db_path, wid, "b") == 2
    conn = sqlite3.connect(settings.db_path)
    try:
        statuses = {row[0]: row[1] for row in conn.execute(
            "SELECT revision, status FROM reading_revisions "
            "WHERE window_id=? AND reading_id='b' ORDER BY revision", (wid,))}
    finally:
        conn.close()
    assert statuses == {1: "superseded", 2: "active"}


def test_batch_partial_replay_subset(client):
    """重放批次的一个子集：子集内幂等，其它读数不受影响。"""
    wid = make_window(client)["id"]
    submit_batch(client, wid, [
        reading("a", 1, 0, 20.0), reading("b", 1, 5, 20.0),
        reading("c", 1, 10, 20.0)])
    r = submit_batch(client, wid, [reading("b", 1, 5, 20.0)])
    assert r.status_code == 201
    assert [e["effect"] for e in r.json()["effects"]] == ["idempotent"]
    assert len(active_map(client, wid)) == 3


# ---------------------------------------------------------------- 与既有能力共存

def test_single_submit_still_works_around_batch(client):
    wid = make_window(client)["id"]
    # 批次先建 a，单条再建 b
    submit_batch(client, wid, [reading("a", 1, 0, 20.0)])
    r = client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 5, 20.0))
    assert r.status_code == 201 and r.json()["effect"] == "created"
    # 单条对批次建立的读数幂等重放
    r = client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    assert r.status_code == 201 and r.json()["effect"] == "idempotent"
    # 批次对单条建立的读数修订
    r = submit_batch(client, wid, [reading("b", 2, 5, 20.5)])
    assert r.status_code == 201 and r.json()["effects"][0]["effect"] == "updated"


def test_issued_snapshot_unchanged_after_later_batch_backfill(client):
    """已签发快照不随补传变化：先签发，再用批次补传/修订，旧证书快照原样。"""
    wid = make_window(client, max_gap=600.0)["id"]
    submit_batch(client, wid, [
        reading("a", 1, 0, 23.0), reading("b", 1, 10, 23.0),
        reading("c", 1, 11, 20.0)])
    cert = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                       json={"note": "补传前签发"}).json()
    assert cert["version"] == 1 and cert["verdict"] == "fail"
    snap_before = client.get(f"/api/v1/certificates/{cert['id']}").json()["snapshot"]

    # 批次修订 + 新增读数后重算并再签发
    r = submit_batch(client, wid, [
        reading("a", 2, 0, 20.0), reading("b", 2, 10, 20.0),
        reading("d", 1, 20, 20.0)])
    assert r.status_code == 201
    cert2 = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                        json={"note": "补传后签发"}).json()
    assert cert2["version"] == 2

    snap_after = client.get(f"/api/v1/certificates/{cert['id']}").json()["snapshot"]
    assert snap_after == snap_before
    assert {(x["reading_id"], x["revision"]) for x in snap_after["readings"]} == {
        ("a", 1), ("b", 1), ("c", 1)}


def test_batch_issue_then_batch_reissue_versions(client):
    wid = make_window(client)["id"]
    submit_batch(client, wid, [reading("a", 1, 0, 20.0)])
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    submit_batch(client, wid, [reading("a", 2, 0, 20.2)])
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    assert c2["version"] == c1["version"] + 1
    versions = client.get(f"/api/v1/windows/{wid}/certificates").json()
    assert [c["version"] for c in versions] == [1, 2]
