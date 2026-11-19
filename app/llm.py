"""LLM 接口（OpenAI 兼容）：日报总结、会议纪要生成。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable

import requests

from .config import config

SYSTEM_DAILY = (
    "你是一位资深个人助理，擅长把零散的口头记录整理成结构清晰的工作日报。\n"
    "规则：\n"
    "1) 只依据输入内容，绝不编造未出现的事实、人名、数字；信息不足就留空或写“未提及”。\n"
    "2) 按主题归类，不要按时间顺序写成流水账；同一件事的多次提及要合并。\n"
    "3) 去掉口语、语气词、寒暄、重复。用简洁的书面中文。\n"
    "4) 只输出 Markdown 正文，不要加代码块围栏，不要解释你在做什么。"
)

SYSTEM_MEETING = (
    "你是一位专业的会议记录员，擅长把会议录音转写稿整理成可执行的会议纪要。\n"
    "规则：\n"
    "1) 严格基于转写内容，不推测、不补充未提及的信息；不确定的地方标注“需确认”。\n"
    "2) 转写稿可能没有区分发言人，涉及“谁说的”时使用中性表述。\n"
    "3) 行动项必须明确到具体事项，能识别出责任人就写，识别不出写“待定”。\n"
    "4) 只输出 Markdown 正文，不要加代码块围栏，不要解释你在做什么。"
)

CHUNK_CHARS = 9000


class LLMError(Exception):
    pass


def _cfg() -> dict:
    return config.llm


def llm_ready() -> bool:
    return config.llm_ready()


def chat(messages: list[dict], max_tokens: int = 2400, temperature: float = 0.3) -> str:
    c = _cfg()
    base = (c.get("base_url") or "").rstrip("/")
    if not base:
        raise LLMError("未配置 LLM 接口地址")
    if not c.get("api_key"):
        raise LLMError("未配置 API Key")
    url = base + "/chat/completions" if not base.endswith("/chat/completions") else base
    payload = {
        "model": c.get("model") or "deepseek-chat",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {c['api_key']}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=int(c.get("timeout", 180)),
        )
    except requests.exceptions.Timeout:
        raise LLMError("请求超时，检查网络或换用更小的模型")
    except requests.exceptions.RequestException as e:
        raise LLMError(f"网络错误：{e}")

    if r.status_code != 200:
        detail = r.text[:400]
        raise LLMError(f"接口返回 {r.status_code}：{detail}")

    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:
        raise LLMError(f"返回格式异常：{r.text[:300]}")
    return _clean(content)


def _clean(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:markdown|md)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    lines = text.splitlines()
    chunks, buf, n = [], [], 0
    for ln in lines:
        if n + len(ln) > size and buf:
            chunks.append("\n".join(buf))
            buf, n = [ln], len(ln)
        else:
            buf.append(ln)
            n += len(ln) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [text]


def _reduce(chunks: list[str], style_hint: str) -> str:
    if len(chunks) == 1:
        return chunks[0]
    parts = []
    for i, ch in enumerate(chunks):
        parts.append(
            chat(
                [
                    {"role": "system", "content": "你是会议记录助手。下面是长转写稿的第 "
                     f"{i + 1}/{len(chunks)} 段，请提炼要点，保留具体名称、数字、结论与待办。不要写前言。"},
                    {"role": "user", "content": ch},
                ],
                max_tokens=1200,
                temperature=0.2,
            )
        )
    return f"【分段要点汇总】\n{style_hint}\n" + "\n\n".join(parts)


# ------------------------------------------------------------------ 日报
def summarize_day(segments_text: str, day_label: str, meeting_titles: list[str] | None = None,
                  progress: Callable[[str], None] | None = None) -> str:
    if not segments_text.strip():
        raise LLMError("今天还没有转写内容")

    def _p(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    chunks = _chunk(segments_text)
    if len(chunks) > 1:
        _p(f"内容较长，分 {len(chunks)} 段提炼…")
        merged = _reduce(chunks, "（这些是同一份转写稿不同部分的分段要点，请合并去重后再整体归纳）")
    else:
        merged = segments_text

    meetings_line = ""
    if meeting_titles:
        meetings_line = "今天已单独生成纪要的会议：" + "、".join(meeting_titles) + "\n（这些会议内容可能被排除或部分包含在下方记录中）\n"

    user = f"""日期：{day_label}
{meetings_line}
以下是当天从麦克风与系统声音中自动记录并转写的语音内容（可能含口语、识别噪声、重复片段，请自行甄别）：

--- 转写记录开始 ---
{merged}
--- 转写记录结束 ---

请输出今天的日报，严格使用以下结构：

## 今日概览
用 2-3 句话概括今天主要在做什么。

## 主要事项
按主题分组，每项一行，格式：- **主题**：具体内容与进展

## 关键信息
提到的重要数据、人名、公司、时间节点、网址等，列表呈现；没有就写“未提及”。

## 决策与结论
已经确定的事；没有就写“未提及”。

## 待办事项
- [ ] 事项（如能判断责任人或时间，写在后面）

## 需要跟进
悬而未决、有风险、或需要进一步确认的事；没有就写“未提及”。
"""
    _p("正在生成日报…")
    return chat([{"role": "system", "content": SYSTEM_DAILY}, {"role": "user", "content": user}],
                max_tokens=2600, temperature=0.3)


# ------------------------------------------------------------------ 会议
def summarize_meeting(transcript: str, start: datetime, end: datetime,
                      topic: str | None = None, progress: Callable[[str], None] | None = None) -> str:
    if not transcript.strip():
        raise LLMError("这场会议没有可用的转写内容")

    def _p(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    chunks = _chunk(transcript)
    if len(chunks) > 1:
        _p(f"内容较长，分 {len(chunks)} 段提炼…")
        merged = _reduce(chunks, "（这些是同一场会议不同部分的分段要点，请合并去重，注意保持时间顺序与逻辑关系）")
    else:
        merged = transcript

    dur = int((end - start).total_seconds() // 60)
    title = topic or "未命名会议"
    user = f"""会议主题（如已知）：{title}
会议时间：{start.strftime('%Y-%m-%d %H:%M')} — {end.strftime('%H:%M')}（约 {dur} 分钟）

以下是会议的自动转写稿，可能含口语、识别错误、重复片段：

--- 转写开始 ---
{merged}
--- 转写结束 ---

请输出会议纪要，结构如下（第一行必须是一级标题，即这场会议的标题，15 字以内）：

# 会议标题

## 会议摘要
3-5 句话说明这场会议在讨论什么、达成了什么。

## 讨论要点
按议题分小节，每个议题下用要点列出各方观点与关键信息。

## 关键结论
明确达成一致的结论；没有就写“本次会议未形成明确结论”。

## 行动项
用 Markdown 表格输出，列：| 事项 | 责任人 | 时间节点 | 备注 |
识别不出的写“待定”；没有行动项时输出一行“无”。

## 待确认 / 风险
需要后续确认、存在分歧或风险的点；没有就写“未提及”。
"""
    _p("正在生成会议纪要…")
    return chat([{"role": "system", "content": SYSTEM_MEETING}, {"role": "user", "content": user}],
                max_tokens=3000, temperature=0.25)


def test_connection() -> str:
    return chat([{"role": "user", "content": "回复两个字：可用"}], max_tokens=20, temperature=0.0)


# ------------------------------------------------------------------ 无 LLM 降级
def fallback_daily(segments: list[dict], day_label: str) -> str:
    lines = []
    for r in segments:
        t = (r.get("text") or "").strip()
        if not t:
            continue
        lines.append(f"- [{datetime.fromtimestamp(float(r.get('ts', 0))).strftime('%H:%M')}] {t}")
    body = "\n".join(lines) or "- 今天没有记录到内容"
    return (
        f"## 今日概览\n未配置大模型，以下为原始转写流水（共 {len(lines)} 条）。\n\n"
        f"## 主要事项\n{body}\n\n"
        "## 待办事项\n- [ ] 在“设置”中填写 LLM API Key 后重新生成，可获得结构化总结。\n"
    )
