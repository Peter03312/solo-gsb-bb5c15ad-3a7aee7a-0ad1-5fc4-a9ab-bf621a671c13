"""业务服务层：窗口、读数、审定、签发。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from fastapi import HTTPException, status

from .evaluation import evaluate
from .models import VERDICT_LABELS_ZH
from .report import build_revision_report
from .schemas import ReadingBatchSubmit, ReadingSubmit, WindowCreate
from .timeutils import parse_stored, to_iso, to_utc


# ---------------------------------------------------------------- 窗口

def create_window(conn: sqlite3.Connection, payload: WindowCreate) -> dict[str, Any]:
    start = to_utc(payload.start_at)
    end = to_utc(payload.end_at)
    if end <= start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无效窗口：end_at 必须晚于 start_at",
        )
    if payload.allowed_temp_max < payload.allowed_temp_min:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无效窗口：allowed_temp_max 不能小于 allowed_temp_min",
        )

    cur = conn.execute(
        """
        INSERT INTO windows (name, start_at, end_at, allowed_temp_min,
                             allowed_temp_max, max_continuous_exceed_seconds,
                             max_sample_interval_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name,
            to_iso(start),
            to_iso(end),
            payload.allowed_temp_min,
            payload.allowed_temp_max,
            payload.max_continuous_exceed_seconds,
            payload.max_sample_interval_seconds,
        ),
    )
    row = conn.execute("SELECT * FROM windows WHERE id = ?", (cur.lastrowid,)).fetchone()
    return window_to_dict(conn, row)


def get_window_row(conn: sqlite3.Connection, window_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM windows WHERE id = ?", (window_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"窗口 {window_id} 不存在")
    return row


def list_windows(conn: sqlite3.Connection, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM windows ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return [window_to_dict(conn, r) for r in rows]


# ---------------------------------------------------------------- 读数

def submit_reading(
    conn: sqlite3.Connection, window_id: int, payload: ReadingSubmit
) -> tuple[str, dict[str, Any]]:
    """提交/修订读数，返回 (effect, reading_dict)。调用方需已持有写事务。"""
    win = get_window_row(conn, window_id)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])

    measured = to_utc(payload.measured_at)
    # 窗口对读数提交为闭区间 [start, end]：终点时刻读数作为“覆盖终止采样”，
    # 用于结束上一条读数的覆盖（其自身区段时长为 0）。严格早于起点或晚于
    # 终点的读数才拒收。输出的温度区段仍是半开 [start, end)。
    if measured < win_start or measured > win_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"读数时间 {to_iso(measured)} 超出窗口 "
                f"[{to_iso(win_start)}, {to_iso(win_end)}]（两端含）"
            ),
        )
    measured_iso = to_iso(measured)
    temperature = float(payload.temperature)

    # 当前活动行（同一 window_id + reading_id 唯一）
    active = conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, payload.reading_id),
    ).fetchone()

    if active is not None:
        if payload.revision == active["revision"]:
            # 相同 ID + 修订号重复提交：内容一致幂等，不一致冲突
            if active["measured_at"] != measured_iso or float(active["temperature"]) != temperature:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {payload.reading_id} 修订号 {payload.revision} 已提交但内容冲突："
                        f"已存 ({active['measured_at']}, {active['temperature']})，"
                        f"本次 ({measured_iso}, {temperature})。相同 ID+修订号重复提交内容必须一致。"
                    ),
                )
            return "idempotent", reading_to_dict(active)

        if payload.revision < active["revision"]:
            # 旧修订号一律拒绝，内容是否相同都不得覆盖更高修订号
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"读数 {payload.reading_id} 当前修订号为 {active['revision']}，"
                    f"拒绝修订号 {payload.revision}：旧修订号不能覆盖更高修订号。"
                ),
            )
        effect = "updated"
    else:
        # 无活动行（全新读数）。history 唯一约束兜底，极端情况下可据其判幂等/冲突。
        hist = conn.execute(
            "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? AND revision=?",
            (window_id, payload.reading_id, payload.revision),
        ).fetchone()
        if hist is not None:
            if hist["measured_at"] != measured_iso or float(hist["temperature"]) != temperature:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {payload.reading_id} 修订号 {payload.revision} 历史内容冲突："
                        f"({hist['measured_at']}, {hist['temperature']}) vs "
                        f"({measured_iso}, {temperature})"
                    ),
                )
            active_row = _insert_active(conn, window_id, payload.reading_id,
                                        payload.revision, measured_iso, temperature)
            return "idempotent", reading_to_dict(active_row)
        effect = "created"

    # 同刻不同读数拒收（UNIQUE(window_id, measured_at) 兜底；先给清晰报错）。
    # 更新自身读数且时间戳未变时不算冲突。
    clash = conn.execute(
        "SELECT reading_id FROM readings WHERE window_id=? AND measured_at=?",
        (window_id, measured_iso),
    ).fetchone()
    if clash is not None and clash["reading_id"] != payload.reading_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"时刻 {measured_iso} 已存在读数 {clash['reading_id']}，同一窗口不允许同刻的不同读数",
        )

    # 落库：新历史行（active）+ 活动表 upsert；旧历史行标记 superseded
    if effect == "updated":
        conn.execute(
            "UPDATE reading_revisions SET status='superseded' "
            "WHERE window_id=? AND reading_id=? AND revision=?",
            (window_id, payload.reading_id, active["revision"]),
        )
    conn.execute(
        """
        INSERT INTO reading_revisions (window_id, reading_id, revision, measured_at,
                                       temperature, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        """,
        (window_id, payload.reading_id, payload.revision, measured_iso, temperature),
    )
    try:
        conn.execute(
            """
            INSERT INTO readings (window_id, reading_id, revision, measured_at, temperature)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(window_id, reading_id) DO UPDATE SET
                revision=excluded.revision,
                measured_at=excluded.measured_at,
                temperature=excluded.temperature,
                created_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (window_id, payload.reading_id, payload.revision, measured_iso, temperature),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"读数拒收：时间戳与窗口内其它读数同刻（{measured_iso}），{exc}",
        )
    active_row = conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, payload.reading_id),
    ).fetchone()
    return effect, reading_to_dict(active_row)


def submit_reading_batch(
    conn: sqlite3.Connection, window_id: int, payload: ReadingBatchSubmit
) -> list[tuple[str, dict[str, Any]]]:
    """批量补传读数：整批校验、整批落库，返回与请求同序的 [(effect, reading_dict)]。

    调用方需已持有写事务。任一单项冲突或无效即抛 HTTPException，由事务
    上下文整体回滚（活动读数与修订历史均不变）。每项沿用 submit_reading
    的窗口边界、时区、修订与冲突语义，差异在于：

    - 同批 reading_id 不得重复（422）；
    - 同刻唯一按“批次全部应用后的最终状态”校验，因此允许两条读数都以更高
      修订号互换采样时刻，不按写入顺序误拒；
    - 校验全部通过后一次提交全部变化。
    """
    win = get_window_row(conn, window_id)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])

    # ---- 1) 批内重复 reading_id（422） ----
    seen_ids: set[str] = set()
    for p in payload.readings:
        if p.reading_id in seen_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"批次拒收：reading_id {p.reading_id} 在同批中重复，同批不得重复 reading_id",
            )
        seen_ids.add(p.reading_id)

    # 现有活动读数与修订历史（批前快照，整个事务内没有其它写入方）
    active_rows = conn.execute(
        "SELECT * FROM readings WHERE window_id=?", (window_id,)
    ).fetchall()
    active: dict[str, sqlite3.Row] = {r["reading_id"]: r for r in active_rows}
    hist_rows = conn.execute(
        "SELECT * FROM reading_revisions WHERE window_id=?", (window_id,)
    ).fetchall()
    history: dict[tuple[str, int], sqlite3.Row] = {
        (r["reading_id"], r["revision"]): r for r in hist_rows
    }

    # ---- 2) 逐项校验：窗口边界、修订号、内容冲突；得到最终状态 ----
    # final[reading_id] = (revision, measured_iso, temperature, effect)
    final: dict[str, tuple[int, str, float, str]] = {}
    # 活动行缺失但历史行存在的幂等项：需把活动行补回（与单条提交同语义）
    recreate_active: set[str] = set()
    for p in payload.readings:
        measured = to_utc(p.measured_at)
        # 窗口对读数提交为闭区间 [start, end]（与单条提交一致）。
        if measured < win_start or measured > win_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"批次拒收：读数 {p.reading_id} 时间 {to_iso(measured)} 超出窗口 "
                    f"[{to_iso(win_start)}, {to_iso(win_end)}]（两端含）"
                ),
            )
        measured_iso = to_iso(measured)
        temperature = float(p.temperature)

        cur = active.get(p.reading_id)
        if cur is not None:
            if p.revision == cur["revision"]:
                if cur["measured_at"] != measured_iso or float(cur["temperature"]) != temperature:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {p.reading_id} 修订号 {p.revision} 已提交但内容冲突："
                            f"已存 ({cur['measured_at']}, {cur['temperature']})，"
                            f"本次 ({measured_iso}, {temperature})。相同 ID+修订号重复提交内容必须一致。"
                        ),
                    )
                effect = "idempotent"
            elif p.revision < cur["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {p.reading_id} 当前修订号为 {cur['revision']}，"
                        f"拒绝修订号 {p.revision}：旧修订号不能覆盖更高修订号。"
                    ),
                )
            else:
                effect = "updated"
            final[p.reading_id] = (p.revision, measured_iso, temperature, effect)
            continue

        # 无活动行（全新读数，或活动行缺失的历史异常场景）。
        hist = history.get((p.reading_id, p.revision))
        if hist is not None:
            if hist["measured_at"] != measured_iso or float(hist["temperature"]) != temperature:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {p.reading_id} 修订号 {p.revision} 历史内容冲突："
                        f"({hist['measured_at']}, {hist['temperature']}) vs "
                        f"({measured_iso}, {temperature})"
                    ),
                )
            effect = "idempotent"
            recreate_active.add(p.reading_id)
        else:
            effect = "created"
        final[p.reading_id] = (p.revision, measured_iso, temperature, effect)
    # ---- 3) 同刻唯一：按批次最终状态校验 ----
    # 未在批内出现的既有读数保留其当前时刻；互换时刻（两条读数同时更新到
    # 对方原时刻）在最终状态下不冲突，不按写入顺序误拒。
    final_time: dict[str, str] = {rid: v[1] for rid, v in final.items()}
    for rid, row in active.items():
        final_time.setdefault(rid, row["measured_at"])
    by_time: dict[str, list[str]] = {}
    for rid, ts in final_time.items():
        by_time.setdefault(ts, []).append(rid)
    for ts, ids in by_time.items():
        if len(ids) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"批次拒收：时刻 {ts} 在最终状态下存在多条读数 "
                    f"{sorted(ids)}，同一窗口不允许同刻的不同读数"
                ),
            )

    # ---- 4) 一次提交全部变化 ----
    updated_ids = [rid for rid, v in final.items() if v[3] == "updated"]
    if updated_ids:
        conn.executemany(
            "UPDATE reading_revisions SET status='superseded' "
            "WHERE window_id=? AND reading_id=? AND revision=?",
            [
                (window_id, rid, active[rid]["revision"])
                for rid in updated_ids
            ],
        )
    conn.executemany(
        """
        INSERT INTO reading_revisions (window_id, reading_id, revision, measured_at,
                                       temperature, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        """,
        [
            (window_id, rid, rev, ts, temp)
            for rid, (rev, ts, temp, effect) in final.items()
            if effect in ("created", "updated")
        ],
    )
    # 先删除被替换的活动行、再统一插入：避免“互换时刻”时 readings 的
    # UNIQUE(window_id, measured_at) 在中间状态误触；同批整事务提交。
    if updated_ids:
        conn.executemany(
            "DELETE FROM readings WHERE window_id=? AND reading_id=?",
            [(window_id, rid) for rid in updated_ids],
        )
    insert_effects = ("created", "updated")
    try:
        conn.executemany(
            """
            INSERT INTO readings (window_id, reading_id, revision, measured_at, temperature)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (window_id, rid, rev, ts, temp)
                for rid, (rev, ts, temp, effect) in final.items()
                if effect in insert_effects or rid in recreate_active
            ],
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"批次拒收：时间戳与窗口内其它读数同刻，{exc}",
        )

    rows = conn.execute("SELECT * FROM readings WHERE window_id=?", (window_id,)).fetchall()
    rows_by_id = {r["reading_id"]: r for r in rows}
    return [
        (final[p.reading_id][3], reading_to_dict(rows_by_id[p.reading_id]))
        for p in payload.readings
    ]


def _insert_active(
    conn: sqlite3.Connection, window_id: int, reading_id: str,
    revision: int, measured_iso: str, temperature: float,
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO readings (window_id, reading_id, revision, measured_at, temperature)
        VALUES (?, ?, ?, ?, ?)
        """,
        (window_id, reading_id, revision, measured_iso, temperature),
    )
    return conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, reading_id),
    ).fetchone()


def list_active_readings(conn: sqlite3.Connection, window_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM readings WHERE window_id=? ORDER BY measured_at ASC, reading_id ASC",
        (window_id,),
    ).fetchall()


def reading_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "reading_id": row["reading_id"],
        "revision": row["revision"],
        "measured_at": row["measured_at"],
        "temperature": row["temperature"],
        "status": "active",
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------- 审定计算

def compute_evaluation(conn: sqlite3.Connection, win: sqlite3.Row) -> dict[str, Any]:
    """基于当前活动读数计算审定结果（不签发）。"""
    reading_rows = list_active_readings(conn, win["id"])
    readings = [
        (parse_stored(r["measured_at"]), float(r["temperature"])) for r in reading_rows
    ]
    return evaluate(
        window_start=parse_stored(win["start_at"]),
        window_end=parse_stored(win["end_at"]),
        temp_min=float(win["allowed_temp_min"]),
        temp_max=float(win["allowed_temp_max"]),
        max_exceed_seconds=float(win["max_continuous_exceed_seconds"]),
        max_sample_interval=float(win["max_sample_interval_seconds"]),
        readings=readings,
    )


def window_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    latest = conn.execute(
        "SELECT version, verdict FROM certificates WHERE window_id=? ORDER BY version DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "name": row["name"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "allowed_temp_min": row["allowed_temp_min"],
        "allowed_temp_max": row["allowed_temp_max"],
        "max_continuous_exceed_seconds": row["max_continuous_exceed_seconds"],
        "max_sample_interval_seconds": row["max_sample_interval_seconds"],
        "created_at": row["created_at"],
        "latest_certificate_version": latest["version"] if latest else None,
        "latest_verdict": latest["verdict"] if latest else None,
    }


def window_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    data = window_to_dict(conn, row)
    data["active_readings"] = [reading_to_dict(r) for r in list_active_readings(conn, row["id"])]
    data["current_evaluation"] = compute_evaluation(conn, row)
    return data


# ---------------------------------------------------------------- 签发

def issue_certificate(
    conn: sqlite3.Connection, window_id: int, note: Optional[str]
) -> dict[str, Any]:
    """签发证书：在当前写事务内读取同一已提交状态并固化快照。

    每次签发 version 自增；修订后再次签发形成新版本，旧版本原样保留。
    """
    win = get_window_row(conn, window_id)
    evaluation = compute_evaluation(conn, win)
    reading_rows = list_active_readings(conn, window_id)

    last = conn.execute(
        "SELECT MAX(version) AS v FROM certificates WHERE window_id=?", (window_id,)
    ).fetchone()
    version = (last["v"] or 0) + 1

    snapshot = {
        "window": {
            "id": win["id"],
            "name": win["name"],
            "start_at": win["start_at"],
            "end_at": win["end_at"],
            "allowed_temp_min": win["allowed_temp_min"],
            "allowed_temp_max": win["allowed_temp_max"],
            "max_continuous_exceed_seconds": win["max_continuous_exceed_seconds"],
            "max_sample_interval_seconds": win["max_sample_interval_seconds"],
        },
        "evaluation": evaluation,
        "readings": [reading_to_dict(r) for r in reading_rows],
    }

    cur = conn.execute(
        """
        INSERT INTO certificates (window_id, version, verdict, snapshot_json, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (window_id, version, evaluation["verdict"], json.dumps(snapshot, ensure_ascii=False), note),
    )
    return get_certificate(conn, int(cur.lastrowid))


def get_certificate_row(conn: sqlite3.Connection, certificate_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM certificates WHERE id=?", (certificate_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"证书 {certificate_id} 不存在")
    return row


def get_certificate(conn: sqlite3.Connection, certificate_id: int) -> dict[str, Any]:
    return certificate_to_dict(get_certificate_row(conn, certificate_id))


def list_certificates(conn: sqlite3.Connection, window_id: int) -> list[dict[str, Any]]:
    get_window_row(conn, window_id)
    rows = conn.execute(
        "SELECT * FROM certificates WHERE window_id=? ORDER BY version ASC", (window_id,)
    ).fetchall()
    return [certificate_to_dict(r) for r in rows]


def certificate_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    verdict = row["verdict"]
    return {
        "id": row["id"],
        "window_id": row["window_id"],
        "version": row["version"],
        "verdict": verdict,
        "verdict_label_zh": VERDICT_LABELS_ZH.get(verdict, verdict),
        "issued_at": row["issued_at"],
        "note": row["note"],
        "snapshot": json.loads(row["snapshot_json"]),
    }


# ---------------------------------------------------------------- 修订影响报告

def revision_report(conn: sqlite3.Connection,
                    old_certificate_id: int,
                    new_certificate_id: int) -> dict[str, Any]:
    """对同一审定窗口的两张已签发证书生成修订影响报告。

    只读取证书行的不可变快照，不触碰当前活动读数；窗口不一致或版本未按
    先后排列时以 422 明确拒绝，证书不存在由取行函数返回 404。
    """
    if old_certificate_id == new_certificate_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"old_certificate_id 与 new_certificate_id 相同（均为 {old_certificate_id}）："
                "修订影响报告必须输入新旧两个不同版本的证书。"
            ),
        )

    old_row = get_certificate_row(conn, old_certificate_id)
    new_row = get_certificate_row(conn, new_certificate_id)

    if old_row["window_id"] != new_row["window_id"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "两张证书不属于同一审定窗口，拒绝生成报告："
                f"旧证书 {old_row['id']} 属于窗口 {old_row['window_id']}，"
                f"新证书 {new_row['id']} 属于窗口 {new_row['window_id']}。"
            ),
        )

    # 同一窗口主键相同即视为同一窗口；快照内窗口时间为防御性二次校验。
    old_snap, new_snap = json.loads(old_row["snapshot_json"]), json.loads(new_row["snapshot_json"])
    ow, nw = old_snap["window"], new_snap["window"]
    if ow["start_at"] != nw["start_at"] or ow["end_at"] != nw["end_at"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"证书 {old_row['id']} 与 {new_row['id']} 的审定窗口边界快照不一致，"
                f"拒绝生成报告：[{ow['start_at']}, {ow['end_at']}) vs "
                f"[{nw['start_at']}, {nw['end_at']})。"
            ),
        )

    if old_row["version"] >= new_row["version"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "证书版本未按先后排列，拒绝生成报告："
                f"old_certificate_id={old_row['id']}（窗口 {old_row['window_id']} "
                f"版本 {old_row['version']}），new_certificate_id={new_row['id']}"
                f"（版本 {new_row['version']}）；要求旧版版本号严格小于新版。"
            ),
        )

    old_cert = certificate_to_dict(old_row)
    new_cert = certificate_to_dict(new_row)
    return build_revision_report(old_cert, new_cert)
