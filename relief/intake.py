"""文本接收解析：把电话转写、纸面登记、志愿者转述、派援回报解析为结构化观察。

只做规则化的整理与线索提取，不做资格判断，也不做医疗处置决定。
解析结果保留原始表述（分句），供现场人员回查。
"""
from __future__ import annotations

import re

from .models import (
    CHANNEL_DISPATCH,
    HIGH,
    LOW,
    MEDIUM,
    Correction,
    NeedObservation,
    Observation,
)

CLAUSE_SPLIT_RE = re.compile(r"[，。；！？!?；\n]+")
PREFIX_RE = re.compile(r"^.*?(?:转述|转达|记录|登记)[:：]\s*")
CALLER_RE = re.compile(r"([一-龥]{1,3}(?:女士|先生|阿姨|大叔|大爷|奶奶|爷爷))")
PRIOR_CONTACT_RE = re.compile(r"同一联系人|同一位联系人|前述联系人|同一人")
LOCATION_RE = re.compile(
    r"([一-龥]{2,6}?(?:安置点|安置区|避难所|收容点|安置所|服务点|站点|点|站))")
LOCATION_LEADING_NOISE_RE = re.compile(
    r"^(?:(?:母亲|父亲|老人|孩子|婴儿|幼儿|孕妇|病人|家人|爷爷|奶奶|儿子|女儿|"
    r"全家|一家)|暂住在|暂住于|暂住|居住在|居住于|住在|位于|说|在|住|于)+")
BED_RE = re.compile(r"([A-Za-z]-\d{1,4})")
BED_NEW_RE = re.compile(r"床位(?:为|是|号)?\s*[:：]?\s*([A-Za-z]-\d{1,4})")
BED_OLD_RE = re.compile(r"写成\s*([A-Za-z]-\d{1,4})")
MED_SAFETY_CUE_RE = re.compile(r"稍后确认|尚未核对|未核对|未确认|待核实|待确认|不清楚|药名不详")
DELIVERED_RE = re.compile(r"已送达|已送到|已发放|已交付|已派发|已签收")
PENDING_RE = re.compile(r"等待|待.{0,6}核实|尚未")
DRUG_SPECIFIC_RE = re.compile(
    r"([一-龥]{2,12}?(?:分散片|缓释片|肠溶片|胶囊|片|注射液|口服液|颗粒|糖浆|滴丸))")
DRUG_NOISE_RE = re.compile(r"需要|服用|使用|口服|每日|老人|孩子|母亲|父亲|病人|的|说|还")

RECIPIENT_KEYWORDS = (
    "母亲", "父亲", "老人", "孩子", "婴儿", "幼儿",
    "孕妇", "病人", "家人", "爷爷", "奶奶", "儿子", "女儿",
)

CATEGORY_KEYWORDS = {
    "medication": ("药", "降压", "降糖", "胰岛素"),
    "water": ("饮水", "饮用水", "缺水", "水"),
    "food": ("食物", "吃的", "口粮", "粮食", "奶粉", "饭"),
    "shelter": ("帐篷", "被子", "住所", "住处", "住宿", "床位"),
    "missing_person": ("失联", "下落不明", "找不到"),
    "medical": ("医护", "急救", "受伤", "发烧", "高烧", "昏迷", "生病"),
}

HIGH_CUES = ("失联", "下落不明", "急救", "昏迷", "高烧", "发烧",
             "危重", "断药", "缺药", "受伤")
MEDIUM_CUES = ("需要", "缺少", "短缺", "不足", "不够")


def parse_record(record: dict) -> Observation:
    """把一条来源记录解析为 Observation。"""
    record_id = str(record.get("id", ""))
    channel = str(record.get("channel", ""))
    text = str(record.get("text", ""))
    cleaned = PREFIX_RE.sub("", text)
    clauses = [c.strip() for c in CLAUSE_SPLIT_RE.split(cleaned) if c.strip()]

    obs = Observation(record_id=record_id, channel=channel, raw_text=text)
    obs.callers = _dedup(CALLER_RE.findall(cleaned))
    obs.refers_to_prior_contact = bool(PRIOR_CONTACT_RE.search(cleaned))
    obs.recipients = _extract_recipients(cleaned)
    obs.locations = _extract_locations(CALLER_RE.sub("", cleaned))
    obs.corrections = _extract_corrections(cleaned)
    obs.urgency_cues = _extract_urgency_cues(cleaned)
    obs.suggested_urgency = _suggest_urgency(obs.urgency_cues, cleaned)

    if channel == CHANNEL_DISPATCH:
        obs.deliveries_reported, obs.pending_notes = _parse_dispatch_clauses(clauses)
    else:
        obs.needs = _extract_needs(clauses, cleaned, obs)
    return obs


def _dedup(items):
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _extract_recipients(text: str) -> list[str]:
    found = [kw for kw in RECIPIENT_KEYWORDS if kw in text]
    if "一家" in text or "全家" in text:
        found.append("全家")
    return _dedup(found)


def normalize_location(value: str) -> str:
    for long_form in ("安置点", "收容点", "避难所", "安置所", "服务点"):
        value = value.replace(long_form, "点")
    return value.replace("安置区", "区")


def _extract_locations(text: str) -> list[tuple[str, str, str]]:
    locations: list[tuple[str, str, str]] = []
    for match in LOCATION_RE.finditer(text):
        value = LOCATION_LEADING_NOISE_RE.sub("", match.group(1))
        if len(value) < 2:
            continue
        locations.append((value, normalize_location(value), "place"))
    for match in BED_RE.finditer(text):
        value = match.group(1)
        locations.append((value, value.upper(), "bed"))
    return _dedup(locations)


def _extract_corrections(text: str) -> list[Correction]:
    new = BED_NEW_RE.search(text)
    old = BED_OLD_RE.search(text)
    if new and old:
        return [Correction(kind="bed", old=old.group(1), new=new.group(1))]
    return []


def _extract_urgency_cues(text: str) -> list[str]:
    return _dedup([c for c in (*HIGH_CUES, *MEDIUM_CUES) if c in text])


def _suggest_urgency(cues: list[str], text: str) -> str:
    if any(c in HIGH_CUES for c in cues):
        return HIGH
    if cues:
        return MEDIUM
    if any(kw in text for kws in CATEGORY_KEYWORDS.values() for kw in kws):
        return MEDIUM
    return LOW


def _category_of_clause(clause: str) -> str | None:
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in clause for kw in keywords):
            return category
    return None


def _extract_specific_drug(text: str) -> str | None:
    """提取具体药名；泛称（如“降压药”）不算具体药名。"""
    cleaned = CALLER_RE.sub("", text)
    cleaned = DRUG_NOISE_RE.sub("", cleaned)
    match = DRUG_SPECIFIC_RE.search(cleaned)
    return match.group(1) if match else None


def _extract_needs(clauses: list[str], full_text: str, obs: Observation) -> list[NeedObservation]:
    by_category: dict[str, list[str]] = {}
    for clause in clauses:
        if BED_NEW_RE.search(clause) or BED_OLD_RE.search(clause):
            continue  # 床位登记 / 更正属于地点事实，不作为需求
        category = _category_of_clause(clause)
        if category is None and _extract_specific_drug(clause):
            category = "medication"  # 具体药名本身即药品需求线索
        if category:
            by_category.setdefault(category, [])
            if clause not in by_category[category]:
                by_category[category].append(clause)

    safety_cue = bool(MED_SAFETY_CUE_RE.search(full_text))
    recipient = obs.recipients[0] if len(obs.recipients) == 1 else None
    needs = []
    for category, phrasings in by_category.items():
        medication_name = _extract_specific_drug(full_text) if category == "medication" else None
        safety_missing = category == "medication" and (safety_cue or medication_name is None)
        needs.append(NeedObservation(
            category=category,
            phrasings=phrasings,
            recipient=recipient,
            medication_name=medication_name,
            safety_missing=safety_missing,
        ))
    return needs


def _parse_dispatch_clauses(clauses: list[str]):
    """派援回报：已交付的物资与仍待核实的事项。"""
    delivered, pending = [], []
    for clause in clauses:
        category = _category_of_clause(clause) or "other"
        if DELIVERED_RE.search(clause):
            delivered.append((category, clause))
        elif PENDING_RE.search(clause):
            pending.append((category, clause))
    return delivered, pending
