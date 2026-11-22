"""数据模型与常量。

系统区分六类信息：求助人、受助对象、地点、需求类别、紧急程度线索、信息来源。
所有协调员操作（合并、调整紧急等级、确认、派援）都记录为事件；
队列、派援记录与待核事项均从同一份状态派生，保证反映同一判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---- 信息来源渠道 ----
CHANNEL_PHONE = "phone"          # 电话转写
CHANNEL_PAPER = "paper"          # 纸面登记
CHANNEL_VOLUNTEER = "volunteer"  # 志愿者转述
CHANNEL_DISPATCH = "dispatch"    # 派援 / 交付回报
CHANNELS = (CHANNEL_PHONE, CHANNEL_PAPER, CHANNEL_VOLUNTEER, CHANNEL_DISPATCH)
FIRST_PARTY_CHANNELS = (CHANNEL_PHONE, CHANNEL_PAPER)  # 当事人自述 / 本人登记
RELAY_CHANNELS = (CHANNEL_VOLUNTEER,)                  # 转述，不能自动当成当事人确认

# ---- 确认状态 ----
REPORTED = "reported"      # 当事人自述 / 本人登记
RELAYED = "relayed"        # 他人转述，未经当事人确认
CONFIRMED = "confirmed"    # 协调员已核实

# ---- 紧急程度 ----
HIGH = "high"
MEDIUM = "medium"
LOW = "low"
URGENCY_LEVELS = (HIGH, MEDIUM, LOW)
URGENCY_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}  # 数值越小越紧急

# ---- 需求状态 ----
OPEN = "open"            # 待派援
DISPATCHED = "dispatched"  # 已派发，在途
DELIVERED = "delivered"    # 已送达
COVERED = "covered"        # 已被既有交付覆盖，避免重复派援
MERGED = "merged"          # 已并入其他需求

# ---- 待核事项类型 ----
RELAY_CONFIRMATION = "relay_confirmation"  # 转述内容待当事人 / 协调员确认
MEDICATION_SAFETY = "medication_safety"    # 药品安全信息不足，转人工核实
MERGE_REVIEW = "merge_review"              # 疑似同一户，待协调员确认合并
LOCATION_CONFLICT = "location_conflict"    # 地点表述冲突

# ---- 需求类别 ----
CATEGORIES = ("medication", "water", "food", "shelter",
              "missing_person", "medical", "other")


@dataclass
class LocationFact:
    """一条地点事实；被更正的旧值保留为 superseded，原始表述不丢。"""
    value: str                 # 原始表述，如 “河东安置点”
    normalized: str            # 归一化后用于比对，如 “河东点”
    kind: str = "place"        # place | bed
    status: str = "current"    # current | superseded
    record_id: str = ""


@dataclass
class Correction:
    """记录内的更正关系，如 “床位为 H-18，之前写成 H-81”。"""
    kind: str
    old: str
    new: str


@dataclass
class NeedObservation:
    """从一条记录中解析出的需求线索（尚未入册）。"""
    category: str
    phrasings: list[str]           # 原始表述（分句）
    recipient: str | None = None
    medication_name: str | None = None   # 具体药名；泛称（如“降压药”）不算
    safety_missing: bool = False         # 药品安全信息是否不足


@dataclass
class Observation:
    """一条来源记录的结构化解析结果。"""
    record_id: str
    channel: str
    raw_text: str
    callers: list[str] = field(default_factory=list)          # 求助人
    recipients: list[str] = field(default_factory=list)       # 受助对象
    locations: list[tuple[str, str, str]] = field(default_factory=list)  # (原始, 归一化, 类型)
    corrections: list[Correction] = field(default_factory=list)
    needs: list[NeedObservation] = field(default_factory=list)
    urgency_cues: list[str] = field(default_factory=list)     # 紧急程度线索（原文）
    suggested_urgency: str = LOW
    refers_to_prior_contact: bool = False                     # “同一联系人”等回指
    deliveries_reported: list[tuple[str, str]] = field(default_factory=list)  # (类别, 原文)
    pending_notes: list[tuple[str, str]] = field(default_factory=list)        # (类别, 原文)


@dataclass
class Need:
    id: str
    household_id: str
    category: str
    phrasings: list[str]                  # 原始表述，可来自多条记录
    recipient: str | None
    urgency: str
    confirmation: str
    status: str = OPEN
    medication_name: str | None = None
    source_record_ids: list[str] = field(default_factory=list)
    merged_into: str | None = None        # status=merged 时指向存续需求
    covered_by: str | None = None         # status=covered 时指向既有交付
    seq: int = 0                          # 接收顺序，用于队列稳定排序


@dataclass
class Household:
    id: str
    callers: list[str] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)
    locations: list[LocationFact] = field(default_factory=list)
    source_record_ids: list[str] = field(default_factory=list)
    active: bool = True
    merged_into: str | None = None


@dataclass
class Delivery:
    """一次派援 / 交付。已执行的交付永不删除，只可追溯。"""
    id: str
    need_id: str
    household_id: str
    category: str
    status: str                          # dispatched | delivered
    note: str = ""
    record_id: str | None = None         # 来源记录（如现场回报）
    original_household_id: str | None = None  # 合并前的归属，便于追溯
    seq: int = 0


@dataclass
class VerificationItem:
    """待核事项：转人工核实的内容，附原始表述。"""
    id: str
    household_id: str
    kind: str
    question: str                        # 需要核实什么
    original_text: str                   # 原始表述
    status: str = "open"                 # open | resolved
    resolution: str | None = None
    need_ids: list[str] = field(default_factory=list)
    related_household_id: str | None = None
    record_id: str | None = None
    affected_by: list[int] = field(default_factory=list)  # 影响过它的事件序号


@dataclass
class Event:
    """协调员 / 接收动作的事件日志条目，支撑影响范围追溯。"""
    seq: int
    kind: str                            # ingest | merge_households | adjust_urgency | confirm | dispatch | deliver
    actor: str
    summary: str
    detail: dict = field(default_factory=dict)
    need_ids: list[str] = field(default_factory=list)
    verification_ids: list[str] = field(default_factory=list)
    delivery_ids: list[str] = field(default_factory=list)
    household_ids: list[str] = field(default_factory=list)
