"""核心登记：接收记录、维护家庭 / 需求 / 交付 / 待核事项，并记录事件日志。

设计要点：
- 转述（志愿者等第三方来源）只记为“转述待确认”，不会自动当成当事人确认；
- 后来补充证明两条记录属于同一户时，由协调员执行合并：
  未派发的重复需求合并去重，避免重复派援；已执行的交付保留并可追溯；
- 药品名称相似不代表可互换，安全信息不足一律转人工核实；
- 协调员调整紧急等级或合并归属后，队列、派援记录、待核事项
  都从同一份状态派生，反映同一判断；
- 系统只辅助排序与整理，不自动决定救助资格或医疗处置。
"""
from __future__ import annotations

from . import intake, medication
from .models import (
    CHANNEL_DISPATCH,
    CONFIRMED,
    COVERED,
    DELIVERED,
    DISPATCHED,
    FIRST_PARTY_CHANNELS,
    HIGH,
    MEDIUM,
    MERGED,
    MERGE_REVIEW,
    MEDICATION_SAFETY,
    OPEN,
    RELAY_CHANNELS,
    RELAY_CONFIRMATION,
    RELAYED,
    REPORTED,
    URGENCY_RANK,
    Delivery,
    Event,
    Household,
    LocationFact,
    Need,
    VerificationItem,
)


class DispatchBlocked(Exception):
    """派援被安全规则拦截，需转人工处理。"""


def _extend_unique(target: list, items) -> None:
    for item in items:
        if item not in target:
            target.append(item)


def _min_confirmation(a: str, b: str) -> str:
    """合并时取较保守的确认状态：转述不会因合并被“洗”成自述。"""
    if RELAYED in (a, b):
        return RELAYED
    if REPORTED in (a, b):
        return REPORTED
    return CONFIRMED


class Registry:
    """救助点登记册：所有状态与事件日志的唯一来源。"""

    def __init__(self) -> None:
        self.households: dict[str, Household] = {}
        self.needs: dict[str, Need] = {}
        self.deliveries: dict[str, Delivery] = {}
        self.verifications: dict[str, VerificationItem] = {}
        self.events: list[Event] = []
        self._counters = {"H": 0, "N": 0, "D": 0, "V": 0}
        self._last_household_id: str | None = None
        self._last_first_party_household_id: str | None = None

    # ---------- 内部工具 ----------

    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}{self._counters[prefix]}"

    def _record_event(self, kind, actor, summary, detail=None,
                      need_ids=(), verification_ids=(), delivery_ids=(),
                      household_ids=()) -> Event:
        event = Event(
            seq=len(self.events) + 1,
            kind=kind, actor=actor, summary=summary,
            detail=detail or {},
            need_ids=list(need_ids),
            verification_ids=list(verification_ids),
            delivery_ids=list(delivery_ids),
            household_ids=list(household_ids),
        )
        self.events.append(event)
        return event

    def _new_household(self) -> Household:
        household = Household(id=self._next_id("H"))
        self.households[household.id] = household
        return household

    def _score(self, obs, household: Household) -> int:
        score = 0
        if set(obs.callers) & set(household.callers):
            score += 3
        current = {lf.normalized for lf in household.locations if lf.status == "current"}
        if {norm for _, norm, _ in obs.locations} & current:
            score += 2
        if set(obs.recipients) & set(household.recipients):
            score += 1
        return score

    def _resolve_household(self, obs):
        """决定记录归入哪一户。返回 (household, 疑似重复户id|None, 是否新建)。

        转述记录的归属本身未经当事人确认，因此即使与既有户高度重合，
        也先单独立户并给出合并建议，由协调员决定是否合并。
        """
        if obs.refers_to_prior_contact and self._last_first_party_household_id:
            household = self.households[self._last_first_party_household_id]
            if household.active:
                return household, None, False
        scored = sorted(
            ((self._score(obs, h), h.id) for h in self.households.values() if h.active),
            key=lambda pair: (-pair[0], int(pair[1][1:])),  # 并列时归入最早建立的户
        )
        best_score, best_id = scored[0] if scored else (0, None)
        if obs.channel in RELAY_CHANNELS:
            household = self._new_household()
            return household, (best_id if best_score >= 2 else None), True
        if best_score >= 2:
            return self.households[best_id], None, False
        if obs.channel == CHANNEL_DISPATCH and self._last_household_id:
            household = self.households[self._last_household_id]
            if household.active:
                return household, None, False
        return self._new_household(), None, True

    def _add_location(self, household, value, normalized, kind, record_id, status="current"):
        for fact in household.locations:
            if fact.normalized == normalized and fact.status == status:
                return fact
        fact = LocationFact(value=value, normalized=normalized, kind=kind,
                            status=status, record_id=record_id)
        household.locations.append(fact)
        return fact

    def _apply_correction(self, household, correction, record_id):
        old_norm = correction.old.upper()
        for fact in household.locations:
            if fact.normalized == old_norm and fact.status == "current":
                fact.status = "superseded"
                break
        else:
            self._add_location(household, correction.old, old_norm,
                               correction.kind, record_id, status="superseded")
        self._add_location(household, correction.new, correction.new.upper(),
                           correction.kind, record_id, status="current")

    def _find_compatible_open_need(self, household_id, category, recipient,
                                   medication_name, exclude_id=None):
        for need in self.needs.values():
            if need.id == exclude_id:
                continue
            if need.household_id != household_id or need.status != OPEN:
                continue
            if need.category != category:
                continue
            if recipient and need.recipient and recipient != need.recipient:
                continue
            if category == "medication":
                a, b = need.medication_name, medication_name
                if a and b and a != b:
                    continue  # 药名不同（哪怕相似）不自动合并
            return need
        return None

    def _find_delivery_for_category(self, household_id, category):
        for delivery in self.deliveries.values():
            if delivery.household_id != household_id:
                continue
            if delivery.status not in (DISPATCHED, DELIVERED):
                continue
            need = self.needs.get(delivery.need_id)
            if need and need.category == category:
                return delivery
        return None

    def _ensure_safety_verification(self, need, obs, question=None):
        for item in self.verifications.values():
            if (item.status == "open" and item.kind == MEDICATION_SAFETY
                    and need.id in item.need_ids):
                return None
        guard = medication.safety_review(need.medication_name, safety_info_complete=False)
        item = VerificationItem(
            id=self._next_id("V"), household_id=need.household_id,
            kind=MEDICATION_SAFETY,
            question=question or guard.reason,
            original_text=obs.raw_text,
            need_ids=[need.id], record_id=obs.record_id,
        )
        self.verifications[item.id] = item
        return item

    def _create_conflict_verification(self, household_id, need_a, need_b, record_id=None):
        guard = medication.compare_names(need_a.medication_name, need_b.medication_name)
        item = VerificationItem(
            id=self._next_id("V"), household_id=household_id,
            kind=MEDICATION_SAFETY,
            question=guard.reason,
            original_text=" / ".join(need_a.phrasings + need_b.phrasings),
            need_ids=[need_a.id, need_b.id], record_id=record_id,
        )
        self.verifications[item.id] = item
        return item

    def _flag_medication_conflicts(self, household_id, need, obs):
        """同一户内出现不同具体药名的未派发需求时，转人工核实（相似也不互换）。"""
        if need.category != "medication" or not need.medication_name:
            return None
        for other in self.needs.values():
            if other.id == need.id or other.household_id != household_id:
                continue
            if other.category != "medication" or other.status != OPEN:
                continue
            if not other.medication_name or other.medication_name == need.medication_name:
                continue
            return self._create_conflict_verification(household_id, other, need,
                                                      record_id=obs.record_id)
        return None

    # ---------- 接收 ----------

    def ingest(self, record: dict, actor: str = "intake") -> int:
        """接收一条来源记录，返回事件序号。"""
        obs = intake.parse_record(record)
        household, merge_target_id, created = self._resolve_household(obs)

        touched_needs: list[str] = []
        created_verifications: list[str] = []
        created_deliveries: list[str] = []

        _extend_unique(household.callers, obs.callers)
        _extend_unique(household.recipients, obs.recipients)
        if obs.record_id and obs.record_id not in household.source_record_ids:
            household.source_record_ids.append(obs.record_id)

        corrections_applied = []
        for value, normalized, kind in obs.locations:
            self._add_location(household, value, normalized, kind, obs.record_id)
        for correction in obs.corrections:
            self._apply_correction(household, correction, obs.record_id)
            corrections_applied.append(
                {"kind": correction.kind, "old": correction.old, "new": correction.new})

        for nobs in obs.needs:
            partner = self._find_compatible_open_need(
                household.id, nobs.category, nobs.recipient, nobs.medication_name)
            if partner is not None:
                _extend_unique(partner.phrasings, nobs.phrasings)
                if obs.record_id not in partner.source_record_ids:
                    partner.source_record_ids.append(obs.record_id)
                if URGENCY_RANK[obs.suggested_urgency] < URGENCY_RANK[partner.urgency]:
                    partner.urgency = obs.suggested_urgency
                need = partner
            else:
                need = Need(
                    id=self._next_id("N"), household_id=household.id,
                    category=nobs.category, phrasings=list(nobs.phrasings),
                    recipient=nobs.recipient, urgency=obs.suggested_urgency,
                    confirmation=RELAYED if obs.channel in RELAY_CHANNELS else REPORTED,
                    medication_name=nobs.medication_name,
                    source_record_ids=[obs.record_id],
                    seq=self._counters["N"] + 1,
                )
                self.needs[need.id] = need
            touched_needs.append(need.id)
            if nobs.category == "medication":
                if nobs.safety_missing:
                    item = self._ensure_safety_verification(need, obs)
                    if item:
                        created_verifications.append(item.id)
                conflict = self._flag_medication_conflicts(household.id, need, obs)
                if conflict:
                    created_verifications.append(conflict.id)

        for category, clause in obs.deliveries_reported:
            need = self._find_compatible_open_need(household.id, category, None, None)
            if need is None:
                need = Need(
                    id=self._next_id("N"), household_id=household.id,
                    category=category, phrasings=[clause], recipient=None,
                    urgency=MEDIUM, confirmation=REPORTED, status=DELIVERED,
                    source_record_ids=[obs.record_id], seq=self._counters["N"] + 1,
                )
                self.needs[need.id] = need
            else:
                need.status = DELIVERED
                _extend_unique(need.phrasings, [clause])
                if obs.record_id not in need.source_record_ids:
                    need.source_record_ids.append(obs.record_id)
            touched_needs.append(need.id)
            delivery = Delivery(
                id=self._next_id("D"), need_id=need.id, household_id=household.id,
                category=category, status=DELIVERED, note=clause,
                record_id=obs.record_id, seq=self._counters["D"],
            )
            self.deliveries[delivery.id] = delivery
            created_deliveries.append(delivery.id)

        pending_recorded = []
        for category, clause in obs.pending_notes:
            pending_recorded.append(clause)
            if category == "medication":
                need = self._find_compatible_open_need(household.id, "medication", None, None)
                if need is not None:
                    item = self._ensure_safety_verification(
                        need, obs, question="现场回报：药品需求等待医护人员核实")
                    if item:
                        created_verifications.append(item.id)

        if obs.channel in RELAY_CHANNELS and touched_needs:
            item = VerificationItem(
                id=self._next_id("V"), household_id=household.id,
                kind=RELAY_CONFIRMATION,
                question="转述内容需经当事人或协调员确认后才能派发",
                original_text=obs.raw_text,
                need_ids=list(touched_needs), record_id=obs.record_id,
            )
            self.verifications[item.id] = item
            created_verifications.append(item.id)

        if merge_target_id is not None:
            item = VerificationItem(
                id=self._next_id("V"), household_id=household.id,
                kind=MERGE_REVIEW,
                question=f"该记录与 {merge_target_id} 疑似同一户（联系人 / 地点重合），"
                         "待协调员确认合并",
                original_text=obs.raw_text,
                related_household_id=merge_target_id, record_id=obs.record_id,
            )
            self.verifications[item.id] = item
            created_verifications.append(item.id)

        self._last_household_id = household.id
        if obs.channel in FIRST_PARTY_CHANNELS:
            self._last_first_party_household_id = household.id

        event = self._record_event(
            "ingest", actor,
            f"接收 {obs.record_id}（{obs.channel}）→ 家庭 {household.id}",
            detail={
                "record_id": obs.record_id, "channel": obs.channel,
                "new_household": created,
                "corrections": corrections_applied,
                "pending_notes": pending_recorded,
                "suggested_urgency": obs.suggested_urgency,
                "urgency_cues": obs.urgency_cues,
            },
            need_ids=touched_needs,
            verification_ids=created_verifications,
            delivery_ids=created_deliveries,
            household_ids=[household.id],
        )
        return event.seq

    # ---------- 协调员操作 ----------

    def merge_households(self, source_id: str, target_id: str,
                         actor: str, reason: str = "") -> int:
        """把 source 户并入 target 户。

        - 未派发的重复需求合并去重，避免重复派援；
        - 已被既有交付覆盖的重复需求标记为 covered，不再进入队列；
        - 药名不同的药品需求不自动合并，转人工核实；
        - 已执行的交付保留原记录，仅更新当前归属，原归属留痕。
        """
        source = self.households[source_id]
        target = self.households[target_id]
        if source_id == target_id:
            raise ValueError("不能将家庭与自身合并")
        if not source.active or not target.active:
            raise ValueError("只能合并仍在跟进的家庭")

        seq = len(self.events) + 1
        affected_needs: list[str] = []
        affected_verifications: list[str] = []
        affected_deliveries: list[str] = []
        merged_pairs, covered, moved = [], [], []

        for need in list(self.needs.values()):
            if need.household_id != source_id:
                continue
            affected_needs.append(need.id)
            if need.status == OPEN:
                partner = self._find_compatible_open_need(
                    target_id, need.category, need.recipient,
                    need.medication_name, exclude_id=need.id)
                if partner is not None:
                    self._absorb_need(partner, need)
                    merged_pairs.append({"from": need.id, "into": partner.id})
                    if partner.id not in affected_needs:
                        affected_needs.append(partner.id)
                    continue
                conflict = self._medication_conflict_in(target_id, need)
                if conflict is not None:
                    need.household_id = target_id
                    moved.append(need.id)
                    item = self._create_conflict_verification(target_id, conflict, need)
                    affected_verifications.append(item.id)
                    continue
                delivery = self._find_delivery_for_category(target_id, need.category)
                if delivery is not None:
                    need.status = COVERED
                    need.covered_by = delivery.id
                    need.household_id = target_id
                    covered.append({"need": need.id, "delivery": delivery.id})
                    affected_deliveries.append(delivery.id)
                    continue
                need.household_id = target_id
                moved.append(need.id)
            else:
                # 已派发 / 已送达的需求保留状态与交付记录，仅更新归属
                need.household_id = target_id
                moved.append(need.id)

        for delivery in self.deliveries.values():
            if delivery.household_id == source_id:
                delivery.household_id = target_id
                if delivery.original_household_id is None:
                    delivery.original_household_id = source_id
                if delivery.id not in affected_deliveries:
                    affected_deliveries.append(delivery.id)

        resolved_reviews = []
        for item in self.verifications.values():
            if item.status != "open":
                continue
            if (item.kind == MERGE_REVIEW
                    and {item.household_id, item.related_household_id} == {source_id, target_id}):
                item.status = "resolved"
                item.resolution = f"协调员确认合并：{source_id} → {target_id}"
                item.affected_by.append(seq)
                resolved_reviews.append(item.id)
                affected_verifications.append(item.id)

        for item in self.verifications.values():
            if item.status != "open" or item.household_id != source_id:
                continue
            item.household_id = target_id
            item.need_ids = list(dict.fromkeys(
                self.needs[n].merged_into
                if n in self.needs and self.needs[n].merged_into else n
                for n in item.need_ids))
            item.affected_by.append(seq)
            affected_verifications.append(item.id)

        _extend_unique(target.callers, source.callers)
        _extend_unique(target.recipients, source.recipients)
        for fact in source.locations:
            self._add_location(target, fact.value, fact.normalized,
                               fact.kind, fact.record_id, status=fact.status)
        _extend_unique(target.source_record_ids, source.source_record_ids)
        source.active = False
        source.merged_into = target_id

        event = self._record_event(
            "merge_households", actor,
            f"合并家庭 {source_id} → {target_id}",
            detail={
                "reason": reason,
                "merged_need_pairs": merged_pairs,
                "covered_by_existing_delivery": covered,
                "moved_needs": moved,
                "preserved_deliveries": affected_deliveries,
                "resolved_merge_reviews": resolved_reviews,
            },
            need_ids=affected_needs,
            verification_ids=affected_verifications,
            delivery_ids=affected_deliveries,
            household_ids=[source_id, target_id],
        )
        return event.seq

    def _absorb_need(self, target_need: Need, source_need: Need) -> None:
        _extend_unique(target_need.phrasings, source_need.phrasings)
        _extend_unique(target_need.source_record_ids, source_need.source_record_ids)
        if URGENCY_RANK[source_need.urgency] < URGENCY_RANK[target_need.urgency]:
            target_need.urgency = source_need.urgency
        target_need.confirmation = _min_confirmation(
            target_need.confirmation, source_need.confirmation)
        target_need.medication_name = target_need.medication_name or source_need.medication_name
        source_need.status = MERGED
        source_need.merged_into = target_need.id
        source_need.household_id = target_need.household_id  # 归属随存续需求，原户留痕于事件日志

    def _medication_conflict_in(self, household_id, need):
        if need.category != "medication" or not need.medication_name:
            return None
        for other in self.needs.values():
            if other.household_id != household_id or other.status != OPEN:
                continue
            if other.category != "medication" or not other.medication_name:
                continue
            if other.medication_name != need.medication_name:
                return other
        return None

    def adjust_urgency(self, need_id: str, level: str, actor: str, reason: str = "") -> int:
        """协调员调整紧急等级；队列与待核事项随即反映同一判断。"""
        if level not in URGENCY_RANK:
            raise ValueError(f"未知紧急等级：{level}")
        need = self.needs[need_id]
        old = need.urgency
        need.urgency = level
        open_verifications = [
            item.id for item in self.verifications.values()
            if item.status == "open" and need_id in item.need_ids
        ]
        event = self._record_event(
            "adjust_urgency", actor,
            f"{need_id} 紧急程度 {old} → {level}",
            detail={"need_id": need_id, "old": old, "new": level, "reason": reason},
            need_ids=[need_id],
            verification_ids=open_verifications,
            household_ids=[need.household_id],
        )
        return event.seq

    def confirm(self, verification_id: str, actor: str, resolution: str = "已核实") -> int:
        """确认一项待核事项；转述确认后，相关需求升级为“已核实”。"""
        item = self.verifications[verification_id]
        if item.status != "open":
            raise ValueError(f"{verification_id} 已处理过")
        item.status = "resolved"
        item.resolution = resolution
        upgraded = []
        if item.kind == RELAY_CONFIRMATION:
            for need_id in item.need_ids:
                need = self.needs[need_id]
                if need.confirmation == RELAYED:
                    need.confirmation = CONFIRMED
                    upgraded.append(need_id)
        event = self._record_event(
            "confirm", actor,
            f"核实 {verification_id}（{item.kind}）：{resolution}",
            detail={"verification_id": verification_id, "kind": item.kind,
                    "resolution": resolution, "upgraded_needs": upgraded},
            need_ids=list(item.need_ids),
            verification_ids=[verification_id],
            household_ids=[item.household_id],
        )
        return event.seq

    # ---------- 派援 ----------

    def dispatch(self, need_id: str, actor: str, note: str = "") -> Delivery:
        """派发物资。转述未确认、药品安全信息不足时拦截并转人工。"""
        need = self.needs[need_id]
        if need.status != OPEN:
            raise DispatchBlocked(f"{need_id} 当前状态为 {need.status}，不能派发")
        if need.confirmation == RELAYED:
            raise DispatchBlocked("转述信息尚未经当事人或协调员确认，需先核实")
        blockers = [
            item for item in self.verifications.values()
            if item.status == "open" and item.kind == MEDICATION_SAFETY
            and need_id in item.need_ids
        ]
        if blockers:
            raise DispatchBlocked(f"药品安全信息未核实，转人工处理：{blockers[0].question}")
        delivery = Delivery(
            id=self._next_id("D"), need_id=need_id, household_id=need.household_id,
            category=need.category, status=DISPATCHED, note=note,
            seq=self._counters["D"],
        )
        self.deliveries[delivery.id] = delivery
        need.status = DISPATCHED
        self._record_event(
            "dispatch", actor,
            f"派发 {need_id}（{need.category}）→ {delivery.id}",
            detail={"note": note},
            need_ids=[need_id], delivery_ids=[delivery.id],
            household_ids=[need.household_id],
        )
        return delivery

    def mark_delivered(self, delivery_id: str, actor: str, note: str = "") -> int:
        delivery = self.deliveries[delivery_id]
        delivery.status = DELIVERED
        if note:
            delivery.note = note
        need = self.needs[delivery.need_id]
        need.status = DELIVERED
        event = self._record_event(
            "deliver", actor,
            f"送达 {delivery_id}（需求 {delivery.need_id}）",
            detail={"note": note},
            need_ids=[delivery.need_id], delivery_ids=[delivery_id],
            household_ids=[delivery.household_id],
        )
        return event.seq
