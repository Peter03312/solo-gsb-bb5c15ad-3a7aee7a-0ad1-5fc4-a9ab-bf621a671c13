"""业务服务层：窗口、读数、审定、签发。"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any, Optional

from fastapi import HTTPException, status

from .evaluation import evaluate
from .models import VERDICT_LABELS_ZH
from .report import build_revision_report
from .schemas import ReadingSubmit, WindowCreate
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
    conn: sqlite3.Connection, window_id: int, items: list[ReadingSubmit]
) -> list[dict[str, Any]]:
    """采样仪器补传：在同一写事务内整批提交读数。

    每项沿用 submit_reading 的 reading_id/revision/measured_at/temperature
    语义（时区归一、窗口闭区间边界、相同 ID+修订号幂等/内容冲突 409、
    旧修订号 409、更高修订号 updated）；批次内 reading_id 不得重复；
    同刻唯一性按整批应用后的**最终状态**校验，因此两条读数可在同一批次里
    以更高修订号互换采样时刻，不受写入/校验顺序影响。

    任何一项冲突或无效都抛异常，由调用方事务整体回滚——活动读数与修订
    历史均不改变。全部校验通过后才一次性写入，返回按请求顺序排列的逐项
    结果（created/updated/idempotent + 最终读数行）；审定结果由调用方在
    提交后基于最终状态重算。
    """
    win = get_window_row(conn, window_id)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])

    # 批次内 reading_id 唯一（同批不得重复 reading_id）
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in items:
        if item.reading_id in seen_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"读数批次内 reading_id 重复：{item.reading_id} 在同一批次出现多次，"
                    "同一批次不得重复提交相同 reading_id。整批拒收。"
                ),
            )
        seen_ids.add(item.reading_id)

        measured = to_utc(item.measured_at)
        # 窗口闭区间 [start, end]：终点时刻读数是“覆盖终止采样”，严格越界才拒收。
        if measured < win_start or measured > win_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"读数 {item.reading_id} 时间 {to_iso(measured)} 超出窗口 "
                    f"[{to_iso(win_start)}, {to_iso(win_end)}]（两端含）。整批拒收。"
                ),
            )
        normalized.append({
            "reading_id": item.reading_id,
            "revision": item.revision,
            "measured_iso": to_iso(measured),
            "temperature": float(item.temperature),
        })

    # 加载本批次涉及的当前活动行（同一 window_id + reading_id 唯一）
    active_rows = {
        r["reading_id"]: r
        for r in conn.execute(
            "SELECT * FROM readings WHERE window_id=? AND reading_id IN (%s)"
            % ",".join("?" for _ in normalized),
            [window_id, *[n["reading_id"] for n in normalized]],
        ).fetchall()
    }

    # 逐项判定 effect（仅判定，不写入），与单条提交规则逐条对齐
    for n in normalized:
        active = active_rows.get(n["reading_id"])
        if active is not None:
            if n["revision"] == active["revision"]:
                if (active["measured_at"] != n["measured_iso"]
                        or float(active["temperature"]) != n["temperature"]):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {n['reading_id']} 修订号 {n['revision']} 已提交但内容冲突："
                            f"已存 ({active['measured_at']}, {active['temperature']})，"
                            f"本次 ({n['measured_iso']}, {n['temperature']})。"
                            "相同 ID+修订号重复提交内容必须一致。整批拒收。"
                        ),
                    )
                n["effect"] = "idempotent"
                continue
            if n["revision"] < active["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {n['reading_id']} 当前修订号为 {active['revision']}，"
                        f"拒绝修订号 {n['revision']}：旧修订号不能覆盖更高修订号。整批拒收。"
                    ),
                )
            n["effect"] = "updated"
            n["prev_revision"] = active["revision"]
            continue

        # 无活动行（全新读数）：查历史行兜底幂等/冲突
        hist = conn.execute(
            "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? AND revision=?",
            (window_id, n["reading_id"], n["revision"]),
        ).fetchone()
        if hist is not None:
            if hist["measured_at"] != n["measured_iso"] or float(hist["temperature"]) != n["temperature"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {n['reading_id']} 修订号 {n['revision']} 历史内容冲突："
                        f"({hist['measured_at']}, {hist['temperature']}) vs "
                        f"({n['measured_iso']}, {n['temperature']})。整批拒收。"
                    ),
                )
            n["effect"] = "idempotent"
        else:
            n["effect"] = "created"

    # 同刻唯一性按整批**最终状态**校验：先并入本批不涉及的既有活动读数，
    # 再用本批结果覆盖对应 reading_id。这样两条读数互换采样时刻、且双方
    # 都是更高修订号时可以通过；任何“不同 reading_id 最终落在同一时刻”
    # 都 422，且结论与批次内排列顺序无关。
    final_times: dict[str, str] = {}
    other_rows = conn.execute(
        "SELECT reading_id, measured_at FROM readings WHERE window_id=?",
        (window_id,),
    ).fetchall()
    for r in other_rows:
        if r["reading_id"] not in seen_ids:
            final_times[r["reading_id"]] = r["measured_at"]
    for n in normalized:
        final_times[n["reading_id"]] = n["measured_iso"]

    owners: dict[str, str] = {}
    for rid, ts in final_times.items():
        owner = owners.get(ts)
        if owner is not None and owner != rid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"批次最终状态下时刻 {ts} 同时属于读数 {owner} 与 {rid}，"
                    "同一窗口不允许同刻的不同读数。整批拒收。"
                ),
            )
        owners[ts] = rid

    # 全部校验通过：一次提交全部变化。
    # updated：旧活动修订历史标记 superseded；created/updated 插新历史行。
    for n in normalized:
        if n["effect"] == "updated":
            conn.execute(
                "UPDATE reading_revisions SET status='superseded' "
                "WHERE window_id=? AND reading_id=? AND revision=?",
                (window_id, n["reading_id"], n["prev_revision"]),
            )
        if n["effect"] in ("created", "updated"):
            try:
                conn.execute(
                    """
                    INSERT INTO reading_revisions (window_id, reading_id, revision,
                                                   measured_at, temperature, status)
                    VALUES (?, ?, ?, ?, ?, 'active')
                    """,
                    (window_id, n["reading_id"], n["revision"],
                     n["measured_iso"], n["temperature"]),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"读数批次拒收：修订历史唯一约束冲突，{exc}",
                )
    # 活动表三阶段写入，避免同刻互换时“中间态”触发即时生效的
    # UNIQUE(window_id, measured_at)（SQLite 的 UNIQUE 约束不能 DEFERRABLE）：
    #   1) updated 行先搬到“本批唯一、且严格早于窗口起点”的哨兵时刻，
    #      释放其原采样时刻；
    #   2) created / 历史幂等复活行以最终时刻插入（updated 行此刻不占任何
    #      真实时刻，不可能与之冲突）；
    #   3) updated 行再搬到最终时刻（最终状态的同刻唯一性上面已校验）。
    sentinel_base = win_start - timedelta(days=1)
    updated_items = [n for n in normalized if n["effect"] == "updated"]
    for idx, n in enumerate(updated_items):
        n["sentinel_iso"] = to_iso(sentinel_base - timedelta(seconds=idx))
        try:
            conn.execute(
                """
                UPDATE readings
                   SET revision=?, measured_at=?, temperature=?,
                       created_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE window_id=? AND reading_id=?
                """,
                (n["revision"], n["sentinel_iso"], n["temperature"],
                 window_id, n["reading_id"]),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"读数批次拒收：活动读数更新冲突，{exc}",
            )

    try:
        for n in normalized:
            if n["effect"] == "idempotent" and n["reading_id"] not in active_rows:
                # 与单条提交一致的历史幂等复活路径：只补活动行，不新增历史
                _insert_active(conn, window_id, n["reading_id"], n["revision"],
                               n["measured_iso"], n["temperature"])
            elif n["effect"] == "created":
                conn.execute(
                    """
                    INSERT INTO readings (window_id, reading_id, revision,
                                          measured_at, temperature)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (window_id, n["reading_id"], n["revision"],
                     n["measured_iso"], n["temperature"]),
                )
    except sqlite3.IntegrityError as exc:
        # UNIQUE(window_id, measured_at) 兜底（并发插入等极端情况）
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"读数批次拒收：时间戳与窗口内其它读数同刻，{exc}",
        )

    try:
        for n in updated_items:
            conn.execute(
                "UPDATE readings SET measured_at=? WHERE window_id=? AND reading_id=?",
                (n["measured_iso"], window_id, n["reading_id"]),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"读数批次拒收：时间戳与窗口内其它读数同刻（{n['measured_iso']}），{exc}",
        )

    # 逐项返回最终读数行，顺序与请求一致
    final_rows = {
        r["reading_id"]: r
        for r in conn.execute(
            "SELECT * FROM readings WHERE window_id=? AND reading_id IN (%s)"
            % ",".join("?" for _ in normalized),
            [window_id, *[n["reading_id"] for n in normalized]],
        ).fetchall()
    }
    return [
        {"reading_id": n["reading_id"], "effect": n["effect"],
         "reading": reading_to_dict(final_rows[n["reading_id"]])}
        for n in normalized
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
