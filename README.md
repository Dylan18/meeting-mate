<div align="center">

# 桌面助理 · Desktop Assistant

**一个常驻桌面的语音记录助手：边听边记，下班自动出日报，开会点一下按钮就出纪要。**

All speech recognition runs **locally & offline** — your audio never leaves your computer.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)](#环境要求)
[![ASR](https://img.shields.io/badge/ASR-SenseVoice-FF6F61)](#它是怎么工作的)
[![License](https://img.shields.io/badge/License-MIT-3DA639)](LICENSE)

<img src="docs/screenshots/main.png" width="380" alt="主界面">
<img src="docs/screenshots/meeting.png" width="380" alt="会议模式">

</div>

---

## 它解决什么问题

开会时你在听，就没法记；在记，就没法听。会后凭记忆补纪要，细节永远是糊的。

这个工具的思路是把它变成后台进程：平时一直听着，重要的地方它替你记下来；一天结束自动归类成日报；开会时点一下按钮做个标记，会议结束后自动出结构化纪要（摘要 / 讨论要点 / 结论 / 行动项 / 风险）。

**特点**

- 🎙 **双路混录**：麦克风 + 系统声音回环同时采集。线上会议对方的声音从扬声器出来，只录麦克风等于只录了你自己。
- 🔒 **完全离线识别**：SenseVoice（INT8）跑在 CPU 上，音频不出本机，不需要联网，也不产生费用。
- ⚡ **比实时快 20 倍**：实测 3 秒音频转写耗时 0.15 秒，全天挂着也不会积压。
- ✍️ **自带标点、中英混说**：不用再读没有标点的转写文本。
- 📄 **总结接你自己的模型**：任何 OpenAI 兼容接口（DeepSeek / 通义 / Kimi / 智谱 / 本地 Ollama），不绑定服务商。
- 🧾 **纯文本落盘**：记录存成 `.jsonl`，报告存成 `.md`，随时能改、能 grep、能进 Git。

---

## 界面上的四个按钮

| 按钮 | 作用 |
|---|---|
| **开始监听** | 持续采集麦克风 + 系统声音，自动切句、转写、实时显示。可以一整天开着 |
| **开始会议 / 结束会议** | 进入会议模式（状态点变红并计时），结束时自动把这段录音整理成会议纪要并打开 |
| **生成今日日报** | 立刻把今天所有记录整理成结构化日报 |
| **历史记录** | 浏览所有已生成的日报与会议纪要 |

<table>
<tr>
<td><img src="docs/screenshots/history.png" alt="历史记录"></td>
<td><img src="docs/screenshots/settings.png" alt="设置"></td>
</tr>
</table>

---

## 环境要求

- **Windows 10 / 11**（依赖 WASAPI 回环录音，暂不支持 macOS / Linux）
- **Python 3.10+**（勾选 *Add python.exe to PATH*）
- WebView2 运行时（Win11 自带；Win10 若缺失，[下载安装](https://developer.microsoft.com/microsoft-edge/webview2/)）

---

## 快速开始

```bash
git clone https://github.com/Dylan18/meeting-mate.git
cd meeting-mate
```

**双击 `启动.bat`**。首次运行会自动创建虚拟环境、安装依赖；识别模型（约 1GB，只保留 INT8 版本共 229MB）在第一次点「开始监听」时自动下载，界面上会显示进度。

也可以用命令行：

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

> 依赖安装慢就加国内镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`

想让电脑开机就自动跑：`Win+R` → 输入 `shell:startup` → 把 `启动.bat` 的**快捷方式**拖进去。

---

## 第一次使用要做的两件事

### 1. 配大模型（不配也能用，只是没有总结）

设置 → 大模型 → 点一个预设 → 填 API Key → 测试连接 → 保存。

| 服务 | 接口地址 | 模型名 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` |

没配 Key 时功能降级为「原始转写流水」，不会报错。

### 2. 确认音源

- **线上会议**（腾讯会议 / 飞书 / Zoom）：麦克风和系统声音**都要开**。
- **线下面对面**：只开麦克风即可。

---

## 它是怎么工作的

```
麦克风 ─┐
        ├─ 100ms 对齐混音 → 16kHz 单声道 → 能量 VAD 切句 → SenseVoice 转写 ─┐
系统回环 ┘                                                              │
                                                                        ▼
                                              按天落盘 data/sessions/*.jsonl
                                                                        │
                                     ┌──────────────────────────────────┤
                                     ▼                                  ▼
                          日报（18:30 自动 / 手动）           会议纪要（按钮触发）
                                     └──────────► LLM 总结 ◄───────────┘
                                                     │
                                          data/reports/*.md
```

- **采集**：`soundcard` + WASAPI，麦克风与扬声器回环两路并行，按 100ms 块对齐混音
- **切句**：能量 VAD + 自适应噪声底，静音 0.7s 断句，保留 0.4s 预录防止吞字，单段上限 20s
- **识别**：`sherpa-onnx` + SenseVoice（INT8 / CPU），中英混说、自带标点；可选轻量 Paraformer
- **总结**：任意 OpenAI 兼容接口，长文本自动分段 map-reduce，避免超上下文
- **界面**：WebView2 + 原生 HTML/CSS/JS，无前端构建、无外部 CDN

---

## 数据存在哪

```
data/
├── sessions/2026-11-19.jsonl     ← 当天所有语音片段（带时间戳、来源标记）
├── reports/日报_2026-11-19.md
├── reports/会议_20261119_1430_周会.md
├── logs/                         ← 崩溃日志
└── settings.json                 ← 配置（含 API Key，已被 .gitignore 排除）
```

全部是纯文本，可以直接用编辑器打开、复制、备份。

---

## 自动日报

设置里可指定每天几点生成（默认 **18:30**）。到点后若当天已有记录，自动生成并弹出来。

第二天启动时，如果昨天漏了，会自动补一份。

---

## 目录结构

```
meeting-mate/
├── 启动.bat                一键启动（首次自动装环境）
├── main.py                 入口：创建窗口、加载位置记忆
├── requirements.txt
├── app/
│   ├── audio.py            采集 / 混音 / VAD 切句
│   ├── asr.py              识别引擎（sherpa-onnx）
│   ├── model_fetch.py      模型下载与精简（多镜像 + 断点续传）
│   ├── llm.py              大模型调用与 prompt
│   ├── store.py            按天存储
│   ├── assistant.py        业务编排
│   ├── config.py           配置读写
│   └── ui/
│       ├── bridge.py       Python ↔ JS 桥接
│       └── web/            界面（index.html / style.css / app.js）
├── docs/screenshots/       README 配图
└── data/ models/           运行时生成，不入库
```

---

## 常见问题

**录不到会议里对方的声音**
系统声音没开，或设备被会议软件独占。检查设置里「系统声音」是否勾选；用耳机时回环会自动跟随默认扬声器。

**识别不准**
- 说话离麦克风近一点，环境噪音大会影响切句
- 专业术语（型号、缩写）识别错误属正常现象，可在生成的纪要里手改
- 纯中文场景可换 `Paraformer 轻量版`（78MB，更快，但无标点）

**一直显示「加载中」**
首次使用要下载模型（约 1GB），进度显示在状态栏。下载失败会自动切换镜像重试。

**双击没反应 / 闪退**
看 `data/logs/crash.log`；命令行跑 `python main.py` 能看到实时报错。

**能不能录到别人的电脑？**
不能，也不打算支持。它只采集本机的麦克风和本机正在播放的声音。

---

## 已知边界

- 仅 Windows（WASAPI 回环是 Windows 独有）
- 说话人分离（谁在说）没有做，会议纪要里统一按内容归类
- 长时间连续说话（>20s 不停顿）会被强制切开，可能切断句子

---

## License

[MIT](LICENSE)
