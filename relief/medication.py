"""药品安全守卫。

原则：
- 药品名称相似不代表可以互换，系统永不判定“可互换”；
- 安全信息（具体药名、药盒 / 剂量核对）不足时，一律转人工核实；
- 最终用药处置由医护人员决定，本模块只负责拦截与提示。
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass
class GuardResult:
    interchangeable: bool        # 系统不判断可互换性，恒为 False
    similar: bool                # 名称是否相似
    requires_human_review: bool  # 是否需要转人工核实
    reason: str


def edit_distance(a: str, b: str) -> int:
    """标准 Levenshtein 距离（动态规划，标准库实现）。"""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        previous = current
    return previous[-1]


def names_similar(a: str, b: str) -> bool:
    """名称相似：互相包含、编辑距离极小，或字符重合度高。"""
    if a == b:
        return False
    if a in b or b in a:
        return True
    if min(len(a), len(b)) >= 3 and edit_distance(a, b) <= 1:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.6


def compare_names(name_a: str, name_b: str) -> GuardResult:
    """比较两个药名。无论是否相似，都不允许自动互换。"""
    if name_a == name_b:
        return GuardResult(False, False, False, f"药名一致：{name_a}")
    if names_similar(name_a, name_b):
        return GuardResult(
            False, True, True,
            f"“{name_a}”与“{name_b}”名称相似，相似不代表可互换，需人工核实",
        )
    return GuardResult(
        False, False, True,
        f"“{name_a}”与“{name_b}”为不同药品，系统不判断可互换性，需人工核实",
    )


def safety_review(drug_name: str | None, safety_info_complete: bool) -> GuardResult:
    """安全信息完整性检查；不足时转人工核实。"""
    if not drug_name:
        return GuardResult(False, False, True, "具体药名缺失（仅有泛称），需人工核实")
    if not safety_info_complete:
        return GuardResult(
            False, False, True,
            f"“{drug_name}”的药盒 / 剂量等安全信息未核对，需人工核实",
        )
    return GuardResult(False, False, False, "安全信息已记录（最终处置仍由医护人员决定）")
