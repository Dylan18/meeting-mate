"""不依赖大模型的本地纪要整理（抽出式）。

为什么需要这一层：LLM 网关的内容风控是**概率性**的 —— 实测同一段会议转写重复
请求，大约每三次就有一次返回 "The request was rejected because it was considered
high risk"，HTTP 状态还是 200。老版本把这句话当成模型输出直接写进了纪要文件，
用户看到的就是一句英文报错。

这里用纯规则从转写原文里抽出「议题 / 结论 / 行动项 / 风险」，保证任何情况下
纪要都有内容可读，而且不编造：所有条目都来自原文，只是重新归类、去掉口水话。

结构刻意和 `llm.summarize_meeting` 保持一致（同样的二级标题），因为
`store.meeting_digests_for` 依赖这些标题把会议结论喂给日报。
"""
from __future__ import annotations

import re
from datetime import datetime

# ---------------------------------------------------------------- 词表
_DECISION_STRONG = (
    "决定", "定了", "敲定", "结论", "一致认为", "就按", "按这个方案", "可以上线",
    "就这么", "原则上", "最终确认", "明确要求",
)
_DECISION_WEAK = ("确定", "同意", "采用", "通过", "没问题", "可以", "行")
_ACTION_RE = re.compile(
    r"(改|加|发|做|写|整理|确认|同步|对接|排期|上线|发布|测试|评估|创建|提交|补充"
    r"|修复|拉个|出个|给个|弄|试一下|看一下|找一下|联系|安排|跟进|准备|更新|导出|配置)"
)
_PRONOUN_RE = re.compile(r"(我|你|他|她|我们|咱们|你们|他们|大家|这边|那边)")
# 只有"要/会/先/稍后"这类意向或时间标记才当成待办；否则"我发到群里那个"会被误收
_MODAL_RE = re.compile(
    r"(要|需要|得|会|先|再|稍后|之后|回头|马上|尽快|安排|负责|帮|麻烦|计划|预计"
    r"|明天|今天|下周|本周|下班前|月底)"
)
# 带这些词的多半在复述已发生的事，不是待办
_PAST_RE = re.compile(r"(已经|之前|上次|那天|昨天|刚才|刚刚|上周|当时)")
_NEG_BEFORE = "不没未别难"
_RISK_WORDS = (
    "风险", "担心", "问题", "阻塞", "卡住", "来不及", "不确定", "待定",
    "有疑问", "再看看", "再说", "不一定", "不好说", "麻烦", "冲突",
)
_TIME_RE = re.compile(
    r"(今天|明天|后天|大后天|今晚|本周[一二三四五六日天]?|这周|下周[一二三四五六日天]?"
    r"|下个月|\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{1,2}\s*[日号]"
    r"|\d{1,2}\s*[点时](?:\s*\d{1,2}\s*分|半)?"
    r"|\d+\s*(?:个)?(?:工作)?[天周月]内?|月底|周末|下班前|节前)"
)
_FILLER_RE = re.compile(
    r"^(?:嗯+|啊+|哦+|额+|唉+|好|好的|行|行吧|可以|对|对的|是|是的|没错|谢谢|拜拜"
    r"|ok|okay|hello|hi)[。，,.!！?？~\s]*$",
    re.IGNORECASE,
)

_MIN_UNIT = 6          # 少于这个字数基本是"嗯""好的"
_MAX_UNIT = 130        # 单条太长就截断，避免整段糊成一行
_TOPIC_GAP = 150       # 超过这个静默时长视为换话题（秒）
_TOPIC_MAX_UNITS = 14
_CLAUSE_SPLIT = re.compile(r"[，,。；;！!？?]")
_QUESTION = re.compile(r"(吗|呢|是不是|要不要|能不能|有没有|对不对|好不好)[。，,.!！?？\s]*$|^要不")


# ---------------------------------------------------------------- 文本清洗
def _clean_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _is_noise(s: str) -> bool:
    if len(s) < _MIN_UNIT:
        return True
    if _FILLER_RE.match(s):
        return True
    # 纯标点 / 纯数字 / 全是重复单字
    if not re.search(r"[\u4e00-\u9fa5a-zA-Z]", s):
        return True
    return len(set(s)) <= 3


def _clip(s: str, limit: int = _MAX_UNIT) -> str:
    if len(s) <= limit:
        return s
    head = s[:limit]
    for sep in ("。", "！", "？", "，", "；"):
        idx = head.rfind(sep)
        if idx >= limit * 0.55:
            return head[:idx] + "…"
    return head + "…"


def _similar(a: str, b: str) -> bool:
    """ASR 会把同一句话吐两遍（外放回声），简单判重就够了。"""
    if a == b:
        return True
    if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.4:
        return False
    same = sum(1 for x, y in zip(a, b, strict=False) if x == y)
    return same / max(len(a), len(b)) > 0.85


def _score(s: str) -> int:
    score = 0
    if 12 <= len(s) <= 80:
        score += 2
    elif len(s) > 80:
        score += 1
    if re.search(r"\d", s):
        score += 1
    if _has_any(s, _DECISION_STRONG):
        score += 3
    if _ACTION_RE.search(s) and _PRONOUN_RE.search(s):
        score += 2
    if any(w in s for w in _RISK_WORDS):
        score += 1
    if s.endswith(("吗", "呢", "?", "？")):
        score -= 1
    return score


def _has_word(s: str, w: str) -> bool:
    """带否定前缀的不算：「不确定」「没定」不是结论。"""
    start = 0
    while True:
        i = s.find(w, start)
        if i < 0:
            return False
        if i == 0 or s[i - 1] not in _NEG_BEFORE:
            return True
        start = i + 1


def _has_any(s: str, words: tuple[str, ...]) -> bool:
    return any(_has_word(s, w) for w in words)


# ---------------------------------------------------------------- 结构抽取
def _units(recs: list[dict]) -> list[dict]:
    """转成 [{ts, dt, text}]，去口水、去回声、去重复。"""
    out: list[dict] = []
    for r in recs or []:
        s = _clean_line(r.get("text") or "")
        if _is_noise(s):
            continue
        s = _clip(s)
        try:
            ts = float(r.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if out and _similar(out[-1]["text"], s):
            continue
        out.append({"ts": ts, "dt": datetime.fromtimestamp(ts) if ts else None, "text": s})
    return out


def _clauses(units: list[dict]) -> list[dict]:
    """ASR 一行里常挤着好几件事，按标点拆成子句再判断，精度高很多。"""
    out: list[dict] = []
    for u in units:
        for part in _CLAUSE_SPLIT.split(u["text"]):
            s = part.strip().strip("呃嗯哦啊哎呀哈吧呢嘛")
            # "现在现在的问题是呃哎" 这类尾字全是语气的，直接丢
            if len(s) < 6 or _FILLER_RE.match(s) or len(set(s)) <= 3:
                continue
            out.append({"ts": u["ts"], "dt": u["dt"], "text": s})
    return out


def _group_topics(units: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for u in units:
        if cur:
            gap = u["ts"] - cur[-1]["ts"]
            if gap > _TOPIC_GAP or len(cur) >= _TOPIC_MAX_UNITS:
                groups.append(cur)
                cur = []
        cur.append(u)
    if cur:
        groups.append(cur)
    return groups


def _pick(group: list[dict], limit: int = 4) -> list[dict]:
    """每组挑信息量最大的几句，按时间顺序输出。"""
    ranked = sorted(group, key=lambda u: _score(u["text"]), reverse=True)[:limit]
    keep = {id(u) for u in ranked}
    return [u for u in group if id(u) in keep]


def _dedupe(items: list[dict], limit: int) -> list[dict]:
    out: list[dict] = []
    seen: list[str] = []
    for u in items:
        s = u["text"]
        if any(_similar(s[:30], x) for x in seen):
            continue
        seen.append(s[:30])
        out.append(u)
        if len(out) >= limit:
            break
    return out


def _decisions(clauses: list[dict]) -> list[dict]:
    out = []
    for c in clauses:
        s = c["text"]
        if len(s) > 70 or _QUESTION.search(s):
            continue
        if _has_any(s, _DECISION_STRONG) or (
            _has_any(s, _DECISION_WEAK) and _has_any(s, ("我们", "咱们", "双方", "大家"))
        ):
            out.append(c)
    return _dedupe(out, 6)


def _actions(clauses: list[dict]) -> list[dict]:
    out = []
    for c in clauses:
        s = c["text"]
        if not (10 <= len(s) <= 45) or _QUESTION.search(s):
            continue
        if s.startswith(("如果", "要是", "假如")) or _PAST_RE.search(s):
            continue
        if not _ACTION_RE.search(s) or not _MODAL_RE.search(s):
            continue
        if not _PRONOUN_RE.search(s):
            continue
        out.append(c)
    return _dedupe(out, 8)


def _risks(clauses: list[dict]) -> list[dict]:
    out = []
    for c in clauses:
        s = c["text"]
        if not (8 <= len(s) <= 60) or _QUESTION.search(s):
            continue
        if "问题" in s and ("什么" in s or "啥" in s):
            continue
        if any(w in s for w in _RISK_WORDS):
            out.append(c)
    return _dedupe(out, 6)


def _owner(s: str) -> str:
    if re.search(r"(我们|咱们|大家)", s):
        return "团队"
    if "我" in s:
        return "我"
    if "你" in s:
        return "对方"
    if re.search(r"(他|她|他们|james|James)", s):
        return "第三方"
    return "待定"


def _strip_lead(s: str) -> str:
    """去掉"就是/然后/所以/但是"这类连接词，表格里读着利索些。"""
    for w in ("就是", "然后", "所以", "但是", "那么", "其实", "这个", "那个",
              "嗯", "呃", "啊", "另外"):
        while s.startswith(w) and len(s) > len(w) + 4:
            s = s[len(w):].lstrip("，, ")
    return s


def _when(s: str) -> str:
    m = _TIME_RE.search(s)
    return m.group(1) if m else "待定"


def _cell(s: str) -> str:
    return s.replace("|", "／").replace("\n", " ")


def _stamp(u: dict) -> str:
    return u["dt"].strftime("%H:%M") if u.get("dt") else ""


def _fmt_dur(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分钟"
    if seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{max(1, seconds)} 秒"


# ---------------------------------------------------------------- 对外入口
def local_meeting(recs: list[dict], start: datetime, end: datetime,
                  topic: str | None = None) -> str:
    """把转写片段整理成结构化的会议纪要（纯本地，不联网）。"""
    units = _units(recs)
    if not units:
        return ("## 会议摘要\n这场会议没有识别到有效语音内容。\n\n"
                "## 关键结论\n未提及。\n\n## 行动项\n无\n\n## 待确认 / 风险\n未提及。\n")

    secs = max(1, int((end - start).total_seconds()))
    groups = _group_topics(units)
    clauses = _clauses(units)
    decisions = _decisions(clauses)
    actions = _actions(clauses)
    risks = _risks(clauses)

    # ---- 摘要：优先挑带结论/待办信息的句子，"嗯""那个"开头的口水句不要
    def _leadish(c: dict) -> bool:
        s = c["text"]
        if _has_any(s, _DECISION_STRONG):
            return True
        if re.search(r"(不需要|不用|没必要|不想)", s):
            return False
        return bool(_ACTION_RE.search(s) and _MODAL_RE.search(s)
                    and _PRONOUN_RE.search(s) and not _PAST_RE.search(s))

    lead = _dedupe([c for c in clauses if _leadish(c)], 3)
    lead.sort(key=lambda c: c["ts"])
    lead_txt = "；".join(_clip(c["text"], 45).rstrip("。，") for c in lead)
    lines = [
        "## 会议摘要",
        f"本场会议共识别 {len(recs)} 段语音、约 {_fmt_dur(secs)}，"
        f"按时间分成 {len(groups)} 个阶段。以下要点由原文自动抽取（未经大模型改写），"
        "建议对照原文核对。",
    ]
    if lead_txt:
        lines.append(f"讨论涉及：{lead_txt}。")

    # ---- 讨论要点
    lines += ["", "## 讨论要点"]
    for g in groups:
        span = f"{_stamp(g[0])}–{_stamp(g[-1])}" if g[0].get("dt") and g[-1].get("dt") else ""
        lines.append(f"### {span or '片段'}")
        for u in _pick(g):
            lines.append(f"- [{_stamp(u)}] {_clip(u['text'], 110)}")

    # ---- 关键结论
    lines += ["", "## 关键结论"]
    if decisions:
        for u in decisions[:6]:
            lines.append(f"- {_clip(u['text'], 110)}（{_stamp(u)}）")
    else:
        lines.append("本次会议未出现明确的结论性表述，可参看「讨论要点」。")

    # ---- 行动项
    lines += ["", "## 行动项"]
    if actions:
        lines.append("| 事项 | 责任人 | 时间节点 | 备注 |")
        lines.append("|---|---|---|---|")
        for u in actions[:8]:
            item = _strip_lead(u["text"])
            lines.append(f"| {_cell(_clip(item, 45))} | {_owner(item)} "
                         f"| {_when(item)} | 原文 {_stamp(u)} |")
        lines.append("")
        lines.append("> 责任人/时间由原文关键词推断，未明确写出的标为「待定」。")
    else:
        lines.append("无")

    # ---- 待确认 / 风险
    lines += ["", "## 待确认 / 风险"]
    if risks:
        for u in risks[:6]:
            lines.append(f"- {_clip(u['text'], 110)}（{_stamp(u)}）")
    else:
        lines.append("未提及。")

    lines += ["", "---", "> 完整逐句转写见应用内「原文」标签页。"]
    return "\n".join(lines)


def local_daily(recs: list[dict], day_label: str, digests: list[dict] | None = None) -> str:
    """日报的本地兜底：按小时归并，比直接甩流水好看一点。"""
    units = _units(recs)
    if not units and not digests:
        return "## 今日概览\n今天没有记录到内容。\n"
    lines = [
        "## 今日概览",
        f"{day_label} 共记录 {len(recs)} 段语音（本地整理，未经大模型归纳）。",
        "", "## 主要事项",
    ]
    buckets: dict[str, list[dict]] = {}
    for u in units:
        key = u["dt"].strftime("%H:00") if u.get("dt") else "—"
        buckets.setdefault(key, []).append(u)
    for hour, items in sorted(buckets.items()):
        lines.append(f"- **{hour}**：{_clip(items[0]['text'], 70)}"
                     + (f"（共 {len(items)} 段）" if len(items) > 1 else ""))
    if digests:
        lines += ["", "## 会议纪要摘要"]
        for d in digests:
            lines.append(f"### {d.get('title') or '会议'}")
            lines.append(d.get("digest", ""))
    return "\n".join(lines)
