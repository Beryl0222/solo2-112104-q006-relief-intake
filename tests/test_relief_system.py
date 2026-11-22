import json
import unittest
from pathlib import Path

from relief import (
    CONFIRMED,
    COVERED,
    DELIVERED,
    DISPATCHED,
    DispatchBlocked,
    HIGH,
    MEDIUM,
    MERGED,
    MERGE_REVIEW,
    MEDICATION_SAFETY,
    OPEN,
    RELAYED,
    RELAY_CONFIRMATION,
    Registry,
    intake,
    medication,
    views,
)

DATA = Path(__file__).parents[1] / "data" / "relief.json"


def load_records():
    return json.loads(DATA.read_text(encoding="utf-8"))


class IntakeTests(unittest.TestCase):
    """接收解析：区分求助人、受助对象、地点、需求类别、紧急线索、信息来源。"""

    def test_entities_and_cues_are_distinguished(self):
        obs = intake.parse_record({
            "id": "x", "channel": "phone",
            "text": "王女士说母亲暂住河东安置点，需要降压药，药名稍后确认。",
        })
        self.assertEqual(obs.channel, "phone")
        self.assertIn("王女士", obs.callers)
        self.assertIn("母亲", obs.recipients)
        self.assertIn("河东点", [norm for _, norm, _ in obs.locations])
        med = next(n for n in obs.needs if n.category == "medication")
        self.assertTrue(med.safety_missing)  # 药名稍后确认 → 安全信息不足
        self.assertIn("需要", obs.urgency_cues)

    def test_location_normalization_links_variants(self):
        first = intake.parse_record({"id": "a", "channel": "phone", "text": "暂住河东安置点。"})
        second = intake.parse_record({"id": "b", "channel": "volunteer", "text": "在河东点。"})
        self.assertEqual(first.locations[0][1], second.locations[0][1])

    def test_bed_correction_is_parsed(self):
        obs = intake.parse_record({
            "id": "y", "channel": "paper",
            "text": "同一联系人补充床位为 H-18，之前登记写成 H-81。",
        })
        self.assertTrue(obs.refers_to_prior_contact)
        self.assertEqual(obs.corrections[0].old, "H-81")
        self.assertEqual(obs.corrections[0].new, "H-18")


class RelayTests(unittest.TestCase):
    """转述不能自动当成当事人确认。"""

    def test_relayed_need_is_not_confirmed_and_cannot_dispatch(self):
        reg = Registry()
        reg.ingest({"id": "r1", "channel": "volunteer",
                    "text": "志愿者转述：李先生一家在城西点，孩子发烧。"})
        need = next(iter(reg.needs.values()))
        self.assertEqual(need.confirmation, RELAYED)
        with self.assertRaises(DispatchBlocked):
            reg.dispatch(need.id, actor="协调员")
        item = next(v for v in reg.verifications.values() if v.kind == RELAY_CONFIRMATION)
        self.assertEqual(item.status, "open")
        self.assertIn("孩子发烧", item.original_text)  # 原始表述保留

    def test_confirming_relay_unlocks_dispatch(self):
        reg = Registry()
        reg.ingest({"id": "r1", "channel": "volunteer",
                    "text": "志愿者转述：李先生一家在城西点，孩子发烧。"})
        need = next(iter(reg.needs.values()))
        item = next(v for v in reg.verifications.values() if v.kind == RELAY_CONFIRMATION)
        reg.confirm(item.id, actor="协调员", resolution="已电话回访确认")
        self.assertEqual(need.confirmation, CONFIRMED)
        delivery = reg.dispatch(need.id, actor="协调员")
        self.assertEqual(delivery.status, DISPATCHED)


class MergeTests(unittest.TestCase):
    """合并：去重未派发需求、保留交付追溯、待核事项同步。"""

    def _build_two_households(self):
        reg = Registry()
        reg.ingest({"id": "a1", "channel": "phone",
                    "text": "王女士说母亲暂住河东安置点，需要降压药，药名稍后确认。"})
        reg.ingest({"id": "a2", "channel": "volunteer",
                    "text": "志愿者转述：王女士一家在河东点，老人缺药，尚未核对药盒。"})
        return reg

    def test_relay_record_gets_tentative_household_and_merge_review(self):
        reg = self._build_two_households()
        active = [h for h in reg.households.values() if h.active]
        self.assertEqual(len(active), 2)
        reviews = [v for v in reg.verifications.values() if v.kind == MERGE_REVIEW]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].related_household_id, "H1")

    def test_merge_deduplicates_undispatched_needs(self):
        reg = self._build_two_households()
        reg.merge_households("H2", "H1", actor="协调员", reason="回电确认同一户")
        open_meds = [n for n in reg.needs.values()
                     if n.category == "medication" and n.status == OPEN]
        self.assertEqual(len(open_meds), 1)  # 重复来电不再计成多户多需求
        merged_need = open_meds[0]
        self.assertEqual(merged_need.household_id, "H1")
        self.assertEqual(merged_need.urgency, HIGH)  # “缺药”线索并入，紧急不降级
        self.assertEqual(merged_need.confirmation, RELAYED)  # 转述未被洗成自述
        self.assertTrue(any(n.status == MERGED for n in reg.needs.values()))
        self.assertFalse(reg.households["H2"].active)

    def test_merge_preserves_executed_deliveries(self):
        reg = self._build_two_households()
        reg.ingest({"id": "a3", "channel": "dispatch", "text": "饮水已送达。"})
        reg.merge_households("H2", "H1", actor="协调员")
        deliveries = list(reg.deliveries.values())
        self.assertEqual(len(deliveries), 1)
        delivery = deliveries[0]
        self.assertEqual(delivery.status, DELIVERED)
        self.assertEqual(delivery.record_id, "a3")  # 交付来源仍可追溯
        self.assertEqual(delivery.household_id, "H1")  # 当前归属已更新
        self.assertEqual(delivery.original_household_id, "H2")  # 原归属留痕

    def test_open_duplicate_is_covered_by_existing_delivery(self):
        reg = Registry()
        reg.ingest({"id": "b1", "channel": "phone", "text": "王女士说需要饮用水。"})
        need = next(iter(reg.needs.values()))
        delivery = reg.dispatch(need.id, actor="协调员")
        reg.mark_delivered(delivery.id, actor="协调员")
        reg.ingest({"id": "b2", "channel": "volunteer",
                    "text": "志愿者转述：王女士一家缺水。"})
        reg.merge_households("H2", "H1", actor="协调员")
        covered = [n for n in reg.needs.values() if n.status == COVERED]
        self.assertEqual(len(covered), 1)  # 未派发的重复需求不再重复派援
        self.assertEqual(covered[0].covered_by, delivery.id)
        self.assertEqual(len(reg.deliveries), 1)  # 交付只有一笔，可追溯

    def test_merge_repoints_pending_verifications(self):
        reg = self._build_two_households()
        reg.merge_households("H2", "H1", actor="协调员")
        for item in views.pending_verifications(reg):
            self.assertEqual(item["household_id"], "H1")
        reviews = [v for v in reg.verifications.values() if v.kind == MERGE_REVIEW]
        self.assertEqual(reviews[0].status, "resolved")


class MedicationTests(unittest.TestCase):
    """药品名称相似不代表可互换；缺少安全信息时转人工核实。"""

    def test_similar_names_are_not_interchangeable(self):
        result = medication.compare_names("苯磺酸氨氯地平片", "苯磺酸左旋氨氯地平片")
        self.assertTrue(result.similar)
        self.assertFalse(result.interchangeable)
        self.assertTrue(result.requires_human_review)

    def test_missing_safety_info_requires_human_review(self):
        self.assertTrue(medication.safety_review(None, False).requires_human_review)
        self.assertTrue(
            medication.safety_review("苯磺酸氨氯地平片", False).requires_human_review)

    def test_conflicting_drug_names_are_not_merged(self):
        reg = Registry()
        reg.ingest({"id": "m1", "channel": "phone",
                    "text": "王女士说母亲需要苯磺酸氨氯地平片。"})
        reg.ingest({"id": "m2", "channel": "volunteer",
                    "text": "志愿者转述：王女士一家在河东点，老人需要苯磺酸左旋氨氯地平片。"})
        reg.merge_households("H2", "H1", actor="协调员")
        open_meds = [n for n in reg.needs.values()
                     if n.category == "medication" and n.status == OPEN]
        self.assertEqual(len(open_meds), 2)  # 药名不同，不自动合并
        conflicts = [v for v in reg.verifications.values()
                     if v.kind == MEDICATION_SAFETY and v.status == "open"]
        self.assertTrue(any("相似" in v.question or "不同" in v.question
                            for v in conflicts))

    def test_missing_safety_info_blocks_dispatch(self):
        reg = Registry()
        reg.ingest({"id": "s1", "channel": "phone",
                    "text": "王女士说母亲需要降压药，药名稍后确认。"})
        need = next(iter(reg.needs.values()))
        safety = [v for v in reg.verifications.values() if v.kind == MEDICATION_SAFETY]
        self.assertEqual(len(safety), 1)
        with self.assertRaises(DispatchBlocked):
            reg.dispatch(need.id, actor="协调员")


class ConsistencyTests(unittest.TestCase):
    """等级调整 / 合并后，队列、派援记录、待核事项反映同一判断。"""

    def test_adjust_urgency_reflects_in_queue_household_and_impact(self):
        reg = Registry()
        reg.ingest({"id": "u1", "channel": "phone", "text": "王女士说母亲需要降压药。"})
        reg.ingest({"id": "u2", "channel": "phone", "text": "李先生说孩子需要食物。"})
        queue = views.queue_view(reg)
        self.assertEqual([e["need_id"] for e in queue], ["N1", "N2"])
        seq = reg.adjust_urgency("N2", HIGH, actor="协调员", reason="孩子年幼")
        queue = views.queue_view(reg)
        self.assertEqual(queue[0]["need_id"], "N2")  # 队列立即反映新等级
        household = views.household_view(reg, reg.needs["N2"].household_id)
        self.assertEqual(household["needs"][0]["urgency"], HIGH)
        impact = views.event_impact(reg, seq)
        self.assertEqual([n["id"] for n in impact["needs"]], ["N2"])

    def test_event_impact_traces_merge(self):
        reg = Registry()
        reg.ingest({"id": "a1", "channel": "phone",
                    "text": "王女士说母亲暂住河东安置点，需要降压药，药名稍后确认。"})
        reg.ingest({"id": "a2", "channel": "volunteer",
                    "text": "志愿者转述：王女士一家在河东点，老人缺药，尚未核对药盒。"})
        seq = reg.merge_households("H2", "H1", actor="协调员")
        impact = views.event_impact(reg, seq)
        self.assertEqual(impact["event"]["kind"], "merge_households")
        statuses = {n["status"] for n in impact["needs"]}
        self.assertIn(MERGED, statuses)
        self.assertIn(OPEN, statuses)
        self.assertTrue(impact["verifications"])  # 受影响的待核事项可查
        for item in impact["verifications"]:
            if item["status"] == "open":
                self.assertEqual(item["household_id"], "H1")

    def test_household_view_keeps_original_phrasing(self):
        reg = Registry()
        reg.ingest({"id": "v1", "channel": "volunteer",
                    "text": "志愿者转述：李先生一家在城西点，老人缺药，尚未核对药盒。"})
        view = views.household_view(reg, "H1")
        self.assertTrue(view["open_verifications"])
        self.assertIn("尚未核对药盒",
                      view["open_verifications"][0]["original_text"]
                      + "".join(n["phrasings"][0] for n in view["needs"]))


class EndToEndTests(unittest.TestCase):
    """用 data/relief.json 走完接收 → 合并 → 确认 → 派援的全流程。"""

    def test_full_flow_stays_consistent(self):
        reg = Registry()
        for record in load_records():
            reg.ingest(record)

        # 转述单独立户并给出合并建议；同一联系人的补充与交付归入 H1
        self.assertEqual(len(reg.households), 2)
        beds = {f.normalized: f.status for f in reg.households["H1"].locations
                if f.kind == "bed"}
        self.assertEqual(beds.get("H-18"), "current")
        self.assertEqual(beds.get("H-81"), "superseded")  # 更正留痕
        water = next(n for n in reg.needs.values() if n.category == "water")
        self.assertEqual(water.status, DELIVERED)

        reg.merge_households("H2", "H1", actor="协调员", reason="回电确认同一户")
        open_meds = [n for n in reg.needs.values()
                     if n.category == "medication" and n.status == OPEN]
        self.assertEqual(len(open_meds), 1)
        queue = views.queue_view(reg)
        self.assertTrue(all(e["household_id"] == "H1" for e in queue))
        self.assertTrue(all(v["household_id"] == "H1"
                            for v in views.pending_verifications(reg)))

        # 确认转述后，药品仍因安全信息不足被拦截，转人工核实
        relay = next(v for v in reg.verifications.values()
                     if v.kind == RELAY_CONFIRMATION and v.status == "open")
        reg.confirm(relay.id, actor="协调员")
        with self.assertRaises(DispatchBlocked):
            reg.dispatch(open_meds[0].id, actor="协调员")
        entry = views.queue_view(reg)[0]
        self.assertEqual(entry["urgency"], "high")  # 紧急且待核实，不会混同普通咨询
        self.assertTrue(entry["blocked_by"])


if __name__ == "__main__":
    unittest.main()
