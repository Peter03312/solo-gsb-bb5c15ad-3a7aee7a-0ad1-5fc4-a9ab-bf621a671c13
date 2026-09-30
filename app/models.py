"""审定结论与读数状态常量。"""
from __future__ import annotations

from enum import Enum


class Verdict(str, Enum):
    """审定结论。

    判定优先级：
    1. 存在超温且最长连续超限时长超过限值 -> fail（不合格）
    2. 否则存在未知区段                    -> pending（待定）
    3. 否则                              -> pass（合格）
    """

    PASS = "pass"
    PENDING = "pending"
    FAIL = "fail"


VERDICT_LABELS_ZH = {
    Verdict.PASS.value: "合格",
    Verdict.PENDING.value: "待定",
    Verdict.FAIL.value: "不合格",
}


# reading 状态：每个（窗口, 读数 ID）只有一条 active，历史行保留在 reading_revisions
READING_ACTIVE = "active"
READING_SUPERSEDED = "superseded"
