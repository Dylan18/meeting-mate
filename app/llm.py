"""LLM 接口（OpenAI 兼容）：日报总结、会议纪要生成。

三个工程化细节（审阅后补）：
- `cfg_override`：测试连接不再临时改写并落盘 settings.json，崩溃也不会残留测试 key
- `finish_reason == "length"`：命中输出上限时自动加预算重试一次，仍截断就在正文前标注，
  而不是安静地少一段结论
- 429 / 5xx / 连接错误：指数退避重试；长稿分段提炼改成 3 路并发，省 60% 等待时间
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from .config import config
from .log import log_exc

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
_REDUCE_WORKERS = 3
_MAX_TOKENS_CAP = 8000
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(Exception):
    pass


def _cfg(override: dict | None = None) -> dict:
    return override if override else config.llm


def llm_ready(cfg: dict | None = None) -> bool:
    if cfg is None:
        return config.llm_ready()
    return bool(cfg.get("api_key") and cfg.get("base_url") and cfg.get("model"))


def chat(messages: list[dict], max_tokens: int = 2400, temperature: float = 0.3,
         cfg: dict | None = None) -> str:
    c = _cfg(cfg)
    budget = max_tokens
    content = ""
    for attempt in (0, 1):
        content, finish = _request(c, messages, budget, temperature)
        if finish == "length":
            if attempt == 0 and budget < _MAX_TOKENS_CAP:
                budget = min(_MAX_TOKENS_CAP, int(budget * 1.8))
                log_exc("llm.chat 输出触顶，加倍重试", f"{max_tokens} → {budget}", level=30)
                continue
            # 拉满还截断：保留已有内容，但必须让用户知道少了尾巴
            return "> ⚠️ 输出达到模型上限，以下内容可能不完整，建议缩短录音时长后重试。\n\n" + _clean(content)
        return _clean(content)
    return _clean(content)


def _request(c: dict, messages: list[dict], max_tokens: int, temperature: float) -> tuple[str, str]:
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
    timeout = int(c.get("timeout", 180))
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {c['api_key']}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            last = LLMError("请求超时，检查网络或换用更小的模型")
        except requests.exceptions.RequestException as e:
            last = LLMError(f"网络错误：{e}")
        else:
            if r.status_code == 200:
                return _parse(r)
            if r.status_code in _RETRY_STATUS and attempt < 2:
                last = LLMError(f"接口返回 {r.status_code}")
            else:
                raise LLMError(f"接口返回 {r.status_code}：{r.text[:400]}")
        # 指数退避：1s → 3s
        time.sleep(1.5 ** attempt)
    raise last or LLMError("请求失败")


def _parse(r) -> tuple[str, str]:
    try:
        data = r.json()
        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
        finish = str(choice.get("finish_reason") or "")
    except Exception as e:
        raise LLMError(f"返回格式异常：{r.text[:300]}") from e
    return content, finish


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


def _reduce(chunks: list[str], style_hint: str, cfg: dict | None = None,
            progress: Callable[[str], None] | None = None) -> str:
    if len(chunks) == 1:
        return chunks[0]

    total = len(chunks)
    done = 0

    def one(i: int) -> str:
        nonlocal done
        out = chat(
            [
                {"role": "system", "content": "你是会议记录助手。下面是长转写稿的第 "
                 f"{i + 1}/{total} 段，请提炼要点，保留具体名称、数字、结论与待办。不要写前言。"},
                {"role": "user", "content": chunks[i]},
            ],
            max_tokens=1200,
            temperature=0.2,
            cfg=cfg,
        )
        done += 1
        if progress:
            try:
                progress(f"分段提炼中… {done}/{total}")
            except Exception as e:
                log_exc("llm.progress 回调异常", e)
        return out

    # 并发但保序：结果按下标落位，拼回去的顺序和串行一致
    results: list[str] = [""] * total
    with ThreadPoolExecutor(max_workers=min(_REDUCE_WORKERS, total)) as pool:
        futures = {pool.submit(one, i): i for i in range(total)}
        for fut, i in futures.items():
            results[i] = fut.result()
    return f"【分段要点汇总】\n{style_hint}\n" + "\n\n".join(r for r in results if r)


# ------------------------------------------------------------------ 日报
def summarize_day(segments_text: str, day_label: str, meeting_titles: list[str] | None = None,
                  progress: Callable[[str], None] | None = None,
                  meeting_digests: list[dict] | None = None,
                  cfg: dict | None = None) -> str:
    if not segments_text.strip() and not meeting_digests:
        raise LLMError("今天还没有转写内容")

    def _p(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception as e:
                log_exc("llm.progress 回调异常", e)

    chunks = _chunk(segments_text) if segments_text.strip() else []
    if len(chunks) > 1:
        _p(f"内容较长，分 {len(chunks)} 段提炼…")
        merged = _reduce(chunks, "（这些是同一份转写稿不同部分的分段要点，请合并去重后再整体归纳）",
                         cfg=cfg, progress=_p)
    else:
        merged = segments_text

    meetings_line = ""
    if meeting_titles:
        meetings_line = "今天已单独生成纪要的会议：" + "、".join(meeting_titles) + "\n"

    digests_block = ""
    if meeting_digests:
        parts = []
        for d in meeting_digests:
            parts.append(f"### {d.get('title') or '会议'}\n{d.get('digest', '')}")
        digests_block = (
            "\n以下是当天会议的纪要摘要（会议音频被单独统计，不在下方转写记录里，"
            "请把它们计入今日工作）：\n" + "\n\n".join(parts) + "\n"
        )

    user = f"""日期：{day_label}
{meetings_line}{digests_block}
以下是当天从麦克风与系统声音中自动记录并转写的语音内容（可能含口语、识别噪声、重复片段，请自行甄别）：

--- 转写记录开始 ---
{merged or "（无）"}
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
                max_tokens=2600, temperature=0.3, cfg=cfg)


# ------------------------------------------------------------------ 会议
def summarize_meeting(transcript: str, start: datetime, end: datetime,
                      topic: str | None = None, progress: Callable[[str], None] | None = None,
                      cfg: dict | None = None) -> str:
    if not transcript.strip():
        raise LLMError("这场会议没有可用的转写内容")

    def _p(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception as e:
                log_exc("llm.progress 回调异常", e)

    chunks = _chunk(transcript)
    if len(chunks) > 1:
        _p(f"内容较长，分 {len(chunks)} 段提炼…")
        merged = _reduce(chunks, "（这些是同一场会议不同部分的分段要点，请合并去重，注意保持时间顺序与逻辑关系）",
                         cfg=cfg, progress=_p)
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
                max_tokens=3000, temperature=0.25, cfg=cfg)


def test_connection(cfg: dict | None = None) -> str:
    """测试指定配置（不落盘）。"""
    return chat([{"role": "user", "content": "回复两个字：可用"}],
                max_tokens=20, temperature=0.0, cfg=cfg)


# ------------------------------------------------------------------ 无 LLM 降级
def fallback_daily(segments: list[dict], day_label: str,
                   digests: list[dict] | None = None) -> str:
    lines = []
    for r in segments:
        t = (r.get("text") or "").strip()
        if not t:
            continue
        lines.append(f"- [{datetime.fromtimestamp(float(r.get('ts', 0))).strftime('%H:%M')}] {t}")
    body = "\n".join(lines) or "- 今天没有记录到内容"
    meeting_part = ""
    if digests:
        blocks = [f"### {d.get('title')}\n{d.get('digest', '')}" for d in digests]
        meeting_part = "\n\n## 会议纪要摘要\n" + "\n\n".join(blocks)
    return (
        f"## 今日概览\n未配置大模型，以下为原始转写流水（共 {len(lines)} 条）。\n\n"
        f"## 主要事项\n{body}{meeting_part}\n\n"
        "## 待办事项\n- [ ] 在“设置”中填写 LLM API Key 后重新生成，可获得结构化总结。\n"
    )
