"""面向协调员与现场人员的派生视图。

所有视图都从 Registry 的同一份状态实时派生：
协调员调整紧急等级或合并归属后，队列、派援记录、待核事项反映同一判断。
"""
from __future__ import annotations

from .models import (
    DELIVERED,
    DISPATCHED,
    MEDICATION_SAFETY,
    MERGE_REVIEW,
    OPEN,
    RELAYED,
    URGENCY_RANK,
)

URGENCY_LABEL = {"high": "紧急", "medium": "较急", "low": "常规"}
STATUS_LABEL = {
    "open": "待派援",
    "dispatched": "已派发",
    "delivered": "已送达",
    "covered": "已被既有交付覆盖",
    "merged": "已合并",
}
CONFIRMATION_LABEL = {
    "reported": "当事人自述",
    "relayed": "转述待确认",
    "confirmed": "已核实",
}
CHANNEL_LABEL = {
    "phone": "电话",
    "paper": "纸面登记",
    "volunteer": "志愿者转述",
    "dispatch": "派援回报",
}
KIND_LABEL = {
    "relay_confirmation": "转述待确认",
    "medication_safety": "药品安全待核实",
    "merge_review": "疑似重复户",
    "location_conflict": "地点冲突",
}
CATEGORY_LABEL = {
    "medication": "药品",
    "water": "饮水",
    "food": "食物",
    "shelter": "住所",
    "missing_person": "失联",
    "medical": "医疗",
    "other": "其他",
}


def _blocked_reasons(reg, need) -> list[str]:
    reasons = []
    if need.confirmation == RELAYED:
        reasons.append("转述信息待确认")
    for item in reg.verifications.values():
        if item.status != "open":
            continue
        if item.kind == MEDICATION_SAFETY and need.id in item.need_ids:
            reasons.append("药品安全信息待人工核实")
            break
    household = reg.households.get(need.household_id)
    if household and any(
            v.status == "open" and v.kind == MERGE_REVIEW
            and v.household_id == household.id
            for v in reg.verifications.values()):
        reasons.append("疑似重复户待确认合并")
    return reasons


def queue_view(reg) -> list[dict]:
    """当前派援队列：未派发需求按紧急程度排序，并标注拦截原因。

    信息不足的紧急请求保持其紧急排位，同时显示“待核实”标记，
    不会被当成普通咨询压到队尾。
    """
    entries = []
    for need in reg.needs.values():
        if need.status != OPEN:
            continue
        household = reg.households.get(need.household_id)
        if household is None or not household.active:
            continue
        blocked = _blocked_reasons(reg, need)
        entries.append({
            "need_id": need.id,
            "household_id": need.household_id,
            "category": need.category,
            "category_label": CATEGORY_LABEL.get(need.category, need.category),
            "urgency": need.urgency,
            "urgency_label": URGENCY_LABEL[need.urgency],
            "confirmation": need.confirmation,
            "confirmation_label": CONFIRMATION_LABEL[need.confirmation],
            "recipient": need.recipient,
            "summary": need.phrasings[0] if need.phrasings else "",
            "blocked_by": blocked,
            "dispatchable": not blocked,
            "source_records": list(need.source_record_ids),
            "seq": need.seq,
        })
    entries.sort(key=lambda e: (URGENCY_RANK[e["urgency"]], e["seq"]))
    return entries


def household_view(reg, household_id: str) -> dict:
    """单户视图：当前需求、派援进度、待核事项及其原始表述。"""
    household = reg.households[household_id]
    needs = sorted(
        (n for n in reg.needs.values() if n.household_id == household_id),
        key=lambda n: n.seq)
    deliveries = sorted(
        (d for d in reg.deliveries.values() if d.household_id == household_id),
        key=lambda d: d.seq)
    open_verifications = [
        v for v in reg.verifications.values()
        if v.household_id == household_id and v.status == "open"
    ]
    return {
        "household_id": household.id,
        "active": household.active,
        "merged_into": household.merged_into,
        "callers": list(household.callers),
        "recipients": list(household.recipients),
        "locations": [
            {"value": f.value, "kind": f.kind, "status": f.status,
             "record_id": f.record_id}
            for f in household.locations
        ],
        "needs": [
            {
                "id": n.id,
                "category": n.category,
                "category_label": CATEGORY_LABEL.get(n.category, n.category),
                "status": n.status,
                "status_label": STATUS_LABEL.get(n.status, n.status),
                "urgency": n.urgency,
                "urgency_label": URGENCY_LABEL[n.urgency],
                "confirmation": n.confirmation,
                "confirmation_label": CONFIRMATION_LABEL[n.confirmation],
                "recipient": n.recipient,
                "medication_name": n.medication_name,
                "phrasings": list(n.phrasings),
                "source_records": list(n.source_record_ids),
                "merged_into": n.merged_into,
                "covered_by": n.covered_by,
            }
            for n in needs
        ],
        "deliveries": [
            {
                "id": d.id,
                "need_id": d.need_id,
                "category": d.category,
                "category_label": CATEGORY_LABEL.get(d.category, d.category),
                "status": d.status,
                "status_label": STATUS_LABEL.get(d.status, d.status),
                "note": d.note,
                "record_id": d.record_id,
                "original_household_id": d.original_household_id,
            }
            for d in deliveries
        ],
        "open_verifications": [
            {
                "id": v.id,
                "kind": v.kind,
                "kind_label": KIND_LABEL.get(v.kind, v.kind),
                "question": v.question,
                "original_text": v.original_text,
                "need_ids": list(v.need_ids),
                "related_household_id": v.related_household_id,
            }
            for v in open_verifications
        ],
    }


def pending_verifications(reg) -> list[dict]:
    """全部待核事项（未处理），附原始表述。"""
    return [
        {
            "id": v.id,
            "household_id": v.household_id,
            "kind": v.kind,
            "kind_label": KIND_LABEL.get(v.kind, v.kind),
            "question": v.question,
            "original_text": v.original_text,
            "need_ids": list(v.need_ids),
            "related_household_id": v.related_household_id,
            "record_id": v.record_id,
        }
        for v in reg.verifications.values()
        if v.status == "open"
    ]


def event_impact(reg, seq: int) -> dict:
    """查清一次合并或等级调整影响了哪些待办：需求、待核事项、交付记录。"""
    event = next((e for e in reg.events if e.seq == seq), None)
    if event is None:
        raise KeyError(f"没有序号为 {seq} 的事件")
    return {
        "event": {
            "seq": event.seq,
            "kind": event.kind,
            "actor": event.actor,
            "summary": event.summary,
            "detail": event.detail,
        },
        "needs": [
            {"id": n.id, "household_id": n.household_id, "category": n.category,
             "status": n.status, "urgency": n.urgency, "confirmation": n.confirmation,
             "merged_into": n.merged_into, "covered_by": n.covered_by}
            for n in (reg.needs.get(i) for i in event.need_ids) if n
        ],
        "verifications": [
            {"id": v.id, "household_id": v.household_id, "kind": v.kind,
             "status": v.status, "question": v.question, "resolution": v.resolution}
            for v in (reg.verifications.get(i) for i in event.verification_ids) if v
        ],
        "deliveries": [
            {"id": d.id, "household_id": d.household_id, "category": d.category,
             "status": d.status, "original_household_id": d.original_household_id}
            for d in (reg.deliveries.get(i) for i in event.delivery_ids) if d
        ],
        "households": [
            {"id": h.id, "active": h.active, "merged_into": h.merged_into}
            for h in (reg.households.get(i) for i in event.household_ids) if h
        ],
    }


def audit_trail(reg, household_id: str) -> list[dict]:
    """该户相关的全部事件，按时间顺序。"""
    return [
        {"seq": e.seq, "kind": e.kind, "actor": e.actor, "summary": e.summary}
        for e in reg.events
        if household_id in e.household_ids
    ]


# ---------- 文本渲染（供现场打印 / 终端查看） ----------

def render_queue(entries: list[dict]) -> str:
    if not entries:
        return "（队列空）"
    lines = []
    for i, e in enumerate(entries, 1):
        flags = f" 〔拦截：{'；'.join(e['blocked_by'])}〕" if e["blocked_by"] else ""
        recipient = f"（{e['recipient']}）" if e["recipient"] else ""
        lines.append(
            f"{i}. [{e['urgency_label']}] {e['need_id']} {e['category_label']}"
            f"{recipient} — {e['summary']}（{e['confirmation_label']}，{e['household_id']}）{flags}")
    return "\n".join(lines)


def render_household(view: dict) -> str:
    lines = [f"家庭 {view['household_id']}"
             + ("" if view["active"] else f"（已并入 {view['merged_into']}）")]
    if view["callers"]:
        lines.append("  求助人：" + "、".join(view["callers"]))
    if view["recipients"]:
        lines.append("  受助对象：" + "、".join(view["recipients"]))
    if view["locations"]:
        parts = [f"{loc['value']}（{'当前' if loc['status'] == 'current' else '已更正'}）"
                 for loc in view["locations"]]
        lines.append("  地点：" + "、".join(parts))
    lines.append("  需求：")
    for n in view["needs"]:
        extra = f"（{n['medication_name']}）" if n["medication_name"] else ""
        lines.append(
            f"    - {n['id']} {n['category_label']}{extra} [{n['urgency_label']}/"
            f"{n['status_label']}/{n['confirmation_label']}] {'；'.join(n['phrasings'])}")
    if view["deliveries"]:
        lines.append("  派援进度：")
        for d in view["deliveries"]:
            origin = f"，原属 {d['original_household_id']}" if d["original_household_id"] else ""
            lines.append(
                f"    - {d['id']} {d['category_label']} [{d['status_label']}]"
                f"（需求 {d['need_id']}，来源 {d['record_id'] or '协调员'}{origin}）{d['note']}")
    if view["open_verifications"]:
        lines.append("  待核实：")
        for v in view["open_verifications"]:
            lines.append(f"    - {v['id']} [{v['kind_label']}] {v['question']}"
                         f"（原文：{v['original_text']}）")
    return "\n".join(lines)
