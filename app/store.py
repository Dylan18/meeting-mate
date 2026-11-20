"""按天存储：原始转写 jsonl + 日报 / 会议纪要 markdown。

报告写入统一收在这里 —— 以前 header 在 assistant 和 store 各拼一遍，改格式要改两处。
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import REPORT_DIR, SESSION_DIR
from .log import log_exc

_lock = threading.RLock()


def today_key(d: date | None = None) -> str:
    return (d or date.today()).strftime("%Y-%m-%d")


def session_file(day: str | None = None) -> Path:
    return SESSION_DIR / f"{day or today_key()}.jsonl"


def append_segment(record: dict) -> None:
    """record: {ts, end, text, scope, meeting_id}"""
    path = session_file()
    line = json.dumps(record, ensure_ascii=False)
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_segments(day: str | None = None) -> list[dict]:
    path = session_file(day)
    if not path.exists():
        return []
    out = []
    bad = 0
    with _lock:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                bad += 1
                continue
    if bad:
        log_exc(f"store.load_segments 跳过 {bad} 行损坏记录", path.name)
    return out


def segments_between(start: datetime, end: datetime, day: str | None = None) -> list[dict]:
    s = start.timestamp()
    e = end.timestamp()
    return [r for r in load_segments(day) if s <= float(r.get("ts", 0)) <= e]


def _render(recs: list[dict], with_time: bool = True) -> str:
    lines = []
    for r in recs:
        txt = (r.get("text") or "").strip()
        if not txt:
            continue
        if with_time:
            ts = datetime.fromtimestamp(float(r.get("ts", 0))).strftime("%H:%M")
            lines.append(f"[{ts}] {txt}")
        else:
            lines.append(txt)
    return "\n".join(lines)


def render(recs: list[dict], with_time: bool = True) -> str:
    return _render(recs, with_time)


# ------------------------------------------------------------------ 报告
def _safe(name: str, limit: int = 60) -> str:
    cleaned = "".join(c for c in (name or "") if c not in r'\/:*?"<>|').strip()
    return cleaned[:limit]


def _write_atomic(path: Path, text: str) -> Path:
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    return path


def report_path_for(day: str, kind: str = "日报") -> Path:
    return REPORT_DIR / f"{kind}_{day}.md"


def has_daily_report(day: str) -> bool:
    return report_path_for(day, "日报").exists()


def write_daily_report(day: str, body: str, meta: dict | None = None) -> Path:
    meta = meta or {}
    header = (
        f"# {day} 工作日报\n\n"
        f"> 生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        f" ｜ 语音片段 {meta.get('segments', 0)} 条\n\n"
    )
    return _write_atomic(report_path_for(day, "日报"), header + body.strip() + "\n")


def meeting_report_path(start: datetime, topic: str = "") -> Path:
    base = f"会议_{start.strftime('%Y%m%d_%H%M')}"
    safe = _safe(topic, 40)
    return REPORT_DIR / (f"{base}_{safe}.md" if safe else f"{base}.md")


def write_meeting_report(start: datetime, title: str, body: str, meta: dict | None = None) -> Path:
    meta = meta or {}
    end = meta.get("end")
    end_txt = end.strftime("%H:%M") if isinstance(end, datetime) else ""
    header = (
        f"# {title or '会议纪要'}\n\n"
        f"> {start.strftime('%Y-%m-%d %H:%M')}"
        + (f" — {end_txt}" if end_txt else "")
        + f" ｜ 时长 {meta.get('duration', '—')}"
        + f" ｜ 转写 {meta.get('segments', 0)} 段\n\n"
    )
    clean = body.lstrip()
    if clean.startswith("# "):     # 模型自己给了标题，避免和 header 重复
        clean = "\n".join(clean.splitlines()[1:]).strip()
    return _write_atomic(meeting_report_path(start, title or ""), header + clean + "\n")


def list_reports() -> list[Path]:
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except Exception:
            return 0.0
    return sorted(REPORT_DIR.glob("*.md"), key=_mtime, reverse=True)


def yesterday() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ 会议索引
def meeting_titles_for(day: str) -> list[str]:
    """扫描某天已生成的会议纪要，取出标题（供日报引用）。"""
    prefix = "会议_" + day.replace("-", "")
    titles: list[str] = []
    for p in sorted(REPORT_DIR.glob(f"{prefix}*.md")):
        title = _read_title(p)
        if title:
            titles.append(title)
    return titles


def _read_title(p: Path) -> str | None:
    try:
        first = p.read_text(encoding="utf-8").splitlines()[0].strip()
    except Exception as e:
        log_exc(f"store._read_title 读取失败({p.name})", e)
        return None
    if first.startswith("# "):
        return first[2:].strip() or None
    return None


_CONCLUSION_HEADS = ("关键结论", "行动项", "待确认", "讨论要点")


def meeting_digests_for(day: str, max_chars: int = 900) -> list[dict]:
    """抽取当天会议纪要里的结论段，供日报引用。

    日报以前只看 scope != meeting 的片段，结果"开了一天会"的人日报是空的。这里把纪要
    的骨架摘出来当上下文，既不重复全文，也让日报能覆盖会议产出。
    """
    prefix = "会议_" + day.replace("-", "")
    out: list[dict] = []
    for p in sorted(REPORT_DIR.glob(f"{prefix}*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            log_exc(f"store.meeting_digests_for 读取失败({p.name})", e)
            continue
        title = _read_title(p) or p.stem
        digest = _extract_sections(text)
        if digest:
            out.append({"title": title, "digest": digest[:max_chars]})
    return out


def _extract_sections(md: str) -> str:
    """只保留"关键结论 / 行动项 / 待确认"这几个二级小节，摘要段不重复塞进日报。"""
    keep: list[str] = []
    cur = ""
    for line in md.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            if any(h in cur for h in _CONCLUSION_HEADS):
                keep.append("")
                keep.append(line)
            continue
        if line.startswith("# "):
            continue
        if cur and any(h in cur for h in _CONCLUSION_HEADS):
            keep.append(line)
    return "\n".join(keep).strip()
