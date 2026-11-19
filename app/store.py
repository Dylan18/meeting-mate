"""按天存储：原始转写 jsonl + 日报 / 会议纪要 markdown。"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import REPORT_DIR, SESSION_DIR

_lock = threading.RLock()


def today_key(d: date | None = None) -> str:
    return (d or date.today()).strftime("%Y-%m-%d")


def session_file(day: str | None = None) -> Path:
    return SESSION_DIR / f"{day or today_key()}.jsonl"


def append_segment(record: dict) -> None:
    """record: {ts, end, text, scope, meeting_id, source}"""
    path = session_file()
    line = json.dumps(record, ensure_ascii=False)
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def load_segments(day: str | None = None) -> list[dict]:
    path = session_file(day)
    if not path.exists():
        return []
    out = []
    with _lock:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def segments_between(start: datetime, end: datetime, day: str | None = None) -> list[dict]:
    s = start.timestamp()
    e = end.timestamp()
    return [r for r in load_segments(day) if s <= float(r.get("ts", 0)) <= e]


def day_text(day: str | None = None, exclude_meetings: bool = True) -> str:
    recs = load_segments(day)
    if exclude_meetings:
        recs = [r for r in recs if r.get("scope") != "meeting"]
    return _render(recs)


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


def save_report(title: str, content: str, day: str | None = None, kind: str = "日报") -> Path:
    d = day or today_key()
    safe = "".join(c for c in title if c not in r'\/:*?"<>|').strip() or kind
    path = REPORT_DIR / f"{safe}.md"
    header = f"# {title}\n\n> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    with _lock:
        path.write_text(header + content.strip() + "\n", encoding="utf-8")
    return path


def report_path_for(day: str, kind: str = "日报") -> Path:
    return REPORT_DIR / f"{kind}_{day}.md"


def list_reports() -> list[Path]:
    return sorted(REPORT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)


def has_daily_report(day: str) -> bool:
    return report_path_for(day, "日报").exists()


def yesterday() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def meeting_titles_for(day: str) -> list[str]:
    """扫描某天已生成的会议纪要，取出标题（供日报引用）。"""
    prefix = "会议_" + day.replace("-", "")
    titles: list[str] = []
    for p in sorted(REPORT_DIR.glob(f"{prefix}*.md")):
        try:
            first = p.read_text(encoding="utf-8").splitlines()[0].strip()
            if first.startswith("# "):
                titles.append(first[2:].strip())
        except Exception:
            continue
    return titles


def meeting_report_path(start: datetime, topic: str = "") -> Path:
    safe = "".join(c for c in (topic or "") if c not in r'\/:*?"<>|').strip()[:40]
    base = f"会议_{start.strftime('%Y%m%d_%H%M')}"
    return REPORT_DIR / (f"{base}_{safe}.md" if safe else f"{base}.md")


def meeting_seconds_stats(start: datetime, end: datetime) -> dict:
    recs = segments_between(start, end)
    n = len(recs)
    chars = sum(len((r.get("text") or "")) for r in recs)
    return {"segments": n, "chars": chars}
