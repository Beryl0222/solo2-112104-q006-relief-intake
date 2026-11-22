"""演示：用合成求助材料走完 接收 → 整理 → 协调员判断 → 派援 的全流程。

用法：python3 -m relief [记录文件路径]（默认 data/relief.json）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import views
from .registry import DispatchBlocked, Registry

COORDINATOR = "协调员"


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = Path(__file__).resolve().parents[1] / "data" / "relief.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    # 追加一条合成记录，演示完整的派发 → 送达流程
    records.append({
        "id": "demo-5",
        "channel": "phone",
        "text": "王女士说家里食物不够，孩子还小。",
    })

    reg = Registry()

    print("=== 1. 接收求助记录 ===")
    for record in records:
        seq = reg.ingest(record)
        print(f"[E{seq}] {reg.events[seq - 1].summary}")

    print("\n=== 2. 当前派援队列（紧急且信息不足的请求保持紧急排位并标注待核实） ===")
    print(views.render_queue(views.queue_view(reg)))

    print("\n=== 3. 协调员调整紧急等级：H1 的药品需求升为紧急 ===")
    med_h1 = next(n for n in reg.needs.values()
                  if n.household_id == "H1" and n.category == "medication")
    adjust_seq = reg.adjust_urgency(
        med_h1.id, "high", actor=COORDINATOR, reason="老人降压药中断，回电确认前按紧急处理")
    print(f"[E{adjust_seq}] {reg.events[adjust_seq - 1].summary}")

    print("\n=== 4. 协调员确认两条记录属于同一户，执行合并 ===")
    merge_seq = reg.merge_households("H2", "H1", actor=COORDINATOR,
                                     reason="回电确认志愿者转述与来电为同一户")
    print(f"[E{merge_seq}] {reg.events[merge_seq - 1].summary}")

    print("\n=== 5. 协调员确认转述内容 ===")
    relay_item = next(v for v in reg.verifications.values()
                      if v.kind == "relay_confirmation" and v.status == "open")
    confirm_seq = reg.confirm(relay_item.id, actor=COORDINATOR,
                              resolution="已回电当事人确认")
    print(f"[E{confirm_seq}] {reg.events[confirm_seq - 1].summary}")

    print("\n=== 6. 尝试派发药品（安全信息不足，应被拦截并转人工） ===")
    try:
        reg.dispatch(med_h1.id, actor=COORDINATOR)
        print("药品已派发")
    except DispatchBlocked as exc:
        print(f"已拦截：{exc}")

    print("\n=== 7. 派发食物需求并完成送达 ===")
    food = next(n for n in reg.needs.values()
                if n.category == "food" and n.status == "open")
    delivery = reg.dispatch(food.id, actor=COORDINATOR, note="食物包一份")
    deliver_seq = reg.mark_delivered(delivery.id, actor=COORDINATOR)
    print(f"[E{deliver_seq}] {reg.events[deliver_seq - 1].summary}")

    print("\n=== 8. 合并后的派援队列 ===")
    print(views.render_queue(views.queue_view(reg)))

    print("\n=== 9. 家庭视图（H1：需求、派援进度、待核实及原始表述） ===")
    print(views.render_household(views.household_view(reg, "H1")))

    print("\n=== 10. 全部待核事项 ===")
    for item in views.pending_verifications(reg):
        print(f"- {item['id']} [{item['kind_label']}] {item['household_id']}："
              f"{item['question']}（原文：{item['original_text']}）")

    print("\n=== 11. 合并事件影响了哪些待办 ===")
    impact = views.event_impact(reg, merge_seq)
    for need in impact["needs"]:
        print(f"- 需求 {need['id']}：{need['category']} 状态={need['status']} "
              f"归属={need['household_id']}")
    for item in impact["verifications"]:
        print(f"- 待核 {item['id']}：{item['kind']} 状态={item['status']} "
              f"归属={item['household_id']}")
    for delivery in impact["deliveries"]:
        print(f"- 交付 {delivery['id']}：{delivery['category']} 状态={delivery['status']} "
              f"原归属={delivery['original_household_id']}")

    print("\n=== 12. 等级调整事件影响了哪些待办 ===")
    impact = views.event_impact(reg, adjust_seq)
    for need in impact["needs"]:
        print(f"- 需求 {need['id']}：紧急程度={need['urgency']}")
    for item in impact["verifications"]:
        print(f"- 待核 {item['id']}：{item['kind']} 状态={item['status']}")

    print("\n=== 13. H1 事件轨迹 ===")
    for entry in views.audit_trail(reg, "H1"):
        print(f"[E{entry['seq']}] {entry['kind']}：{entry['summary']}")


if __name__ == "__main__":
    main()
