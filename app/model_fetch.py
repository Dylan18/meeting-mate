"""ASR 模型下载与精简。

模型来源于 sherpa-onnx 官方 release（GitHub），下载 bz2 包后只保留 int8 模型与词表，
避免占用过多磁盘。全部使用标准库，无第三方下载依赖。
"""
from __future__ import annotations

import shutil
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .config import MODEL_DIR

Progress = Callable[[str], None]

_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
_MIRRORS = [
    _BASE,
    "https://ghproxy.net/" + _BASE,
    "https://gh-proxy.com/" + _BASE,
]

MODELS = {
    "sensevoice": {
        "archive": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
        "dir": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "keep": ["model.int8.onnx", "tokens.txt"],
        "title": "SenseVoice（中英混说·带标点）",
        "approx_mb": 1050,
    },
    "paraformer-small": {
        "archive": "sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2",
        "dir": "sherpa-onnx-paraformer-zh-small-2024-03-09",
        "keep": ["model.int8.onnx", "tokens.txt"],
        "title": "Paraformer 轻量版（纯中文·无标点）",
        "approx_mb": 78,
    },
}


def entry(name: str) -> dict:
    return MODELS.get(name) or MODELS["sensevoice"]


def model_dir(name: str) -> Path:
    return MODEL_DIR / entry(name)["dir"]


def is_ready(name: str) -> bool:
    d = model_dir(name)
    return all((d / f).exists() and (d / f).stat().st_size > 0 for f in entry(name)["keep"])


def _human(n: float) -> str:
    return f"{n / 1048576:.0f} MB" if n < 1073741824 else f"{n / 1073741824:.2f} GB"


def _download(url: str, dest: Path, progress: Progress | None, label: str) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    existing = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 desktop-assistant"})
    if existing > 0:
        req.add_header("Range", f"bytes={existing}-")
    with urllib.request.urlopen(req, timeout=40) as r:
        status = getattr(r, "status", 200)
        # 服务器不认 Range 时会直接返回 200 + 完整文件：此时必须重算 total，
        # 否则进度会从"已有大小"开始累加，最终超过 100%
        resuming = existing > 0 and status == 206
        mode = "ab" if resuming else "wb"
        done = existing if resuming else 0
        total = int(r.headers.get("Content-Length") or 0) + done
        last = 0.0
        with tmp.open(mode) as f:
            while True:
                chunk = r.read(1024 * 512)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if progress and now - last > 0.7:
                    last = now
                    if total:
                        progress(f"下载{label} {done * 100 / total:.0f}%"
                                 f"（{_human(done)} / {_human(total)}）")
                    else:
                        progress(f"下载{label} {_human(done)}")
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise RuntimeError("下载内容为空")
    if dest.exists():
        dest.unlink()
    tmp.replace(dest)


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except Exception:
        pass


def _extract_and_trim(archive: Path, target_dir: Path, keep: list[str],
                      progress: Progress | None) -> None:
    if progress:
        progress("解压模型（约需 1 分钟）…")
    tmp_dir = MODEL_DIR / "_extract"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tf:
        # 3.12+ 支持 filter="data"，能挡住 ../ 路径穿越与设备文件；低版本只能裸解压
        try:
            tf.extractall(tmp_dir, filter="data")
        except TypeError:
            tf.extractall(tmp_dir)

    # 找到包内的模型目录
    src = None
    for p in tmp_dir.iterdir():
        if p.is_dir():
            src = p
            break
    if src is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("压缩包结构异常")

    target_dir.mkdir(parents=True, exist_ok=True)
    for f in keep:
        s = src / f
        if s.exists():
            shutil.move(str(s), str(target_dir / f))

    shutil.rmtree(tmp_dir, ignore_errors=True)
    _unlink_quiet(archive)


def ensure_model(name: str, progress: Progress | None = None, force: bool = False) -> Path:
    e = entry(name)
    d = model_dir(name)
    if not force and is_ready(name):
        return d

    title = e["title"].split("（")[0]
    cache = MODEL_DIR / "_downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / e["archive"]

    last_err: Exception | None = None
    for base in _MIRRORS:
        try:
            if not archive.exists() or archive.stat().st_size < 1024 * 1024:
                if progress:
                    progress(f"下载{title}（约 {e['approx_mb']}MB，仅首次）…")
                _download(base + e["archive"], archive, progress,
                          f" {title}（约 {e['approx_mb']}MB）")
            try:
                _extract_and_trim(archive, d, e["keep"], progress)
            except Exception:
                # 断网截断的 .part 会留下一个"看起来已下载"的坏包，之后三个镜像全解压失败，
                # 用户只能手动删目录。这里删掉，让下一轮重新下。
                _unlink_quiet(archive)
                raise
            if is_ready(name):
                if progress:
                    progress("模型准备完成")
                return d
            raise RuntimeError("解压后缺少必需文件")
        except Exception as ex:
            last_err = ex
            continue
    raise RuntimeError(f"模型下载失败：{last_err}")
