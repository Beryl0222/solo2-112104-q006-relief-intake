# 灾后求助叙述的需求拆分与人工派援

本项目整理临时救助点从电话转写、纸面登记和志愿者转述接收求助。一个家庭可能同时报告失联成员、药品需求和临时住所，重复来电也可能只是补充位置；按关键词直接排序会把重复记录计成多户，或让一条高风险但信息不足的请求被误派。

演练资料位于 `data/relief.json`，内容为合成记录，保留不同来源的原始表述和修订关系。项目环境使用 Python 3.11 标准库。

## 能力概览（`relief/` 包）

- **文本接收（`relief.intake`）**：把电话转写、纸面登记、志愿者转述、派援回报解析为结构化观察，区分求助人、受助对象、地点（含床位更正）、需求类别、紧急程度线索和信息来源，并保留原始表述。
- **登记与事件日志（`relief.registry.Registry`）**：
  - 转述只记为“转述待确认”，不会自动当成当事人确认；确认前禁止派发；
  - 转述记录先单独立户并给出合并建议，由协调员执行 `merge_households`：未派发的重复需求合并去重、避免重复派援，已被既有交付覆盖的标记为 covered，已执行的交付保留原记录并可追溯（原归属留痕）；
  - `adjust_urgency` 调整紧急等级后，队列、派援记录、待核事项从同一份状态派生，反映同一判断；
  - 每次合并 / 调整都记录事件，可用 `views.event_impact` 查清影响了哪些待办。
- **药品安全守卫（`relief.medication`）**：药名相似不代表可互换（系统永不判定可互换）；具体药名缺失或药盒未核对时生成待核事项并拦截派发，转人工核实。
- **派生视图（`relief.views`）**：派援队列（紧急且信息不足的请求保持紧急排位并标注待核实）、单户视图（当前需求、派援进度、待核事项及原始表述）、待核事项清单、事件影响范围、事件轨迹。

系统只辅助排序与整理，不自动决定救助资格或医疗处置。

## 使用

```bash
python3 -m relief                 # 用 data/relief.json 演示全流程
python3 -m relief 路径/记录.json   # 处理其他记录文件
python3 -m unittest discover -s tests -v   # 运行检查
```

```python
from relief import Registry, views

reg = Registry()
for record in records:          # {"id", "channel", "text"}
    reg.ingest(record)
reg.merge_households("H2", "H1", actor="协调员", reason="回电确认同一户")
reg.adjust_urgency("N1", "high", actor="协调员", reason="老人断药风险")
views.queue_view(reg)           # 当前派援队列
views.household_view(reg, "H1") # 单户需求 / 进度 / 待核实
views.event_impact(reg, 7)      # 某次合并或调整影响了哪些待办
```
