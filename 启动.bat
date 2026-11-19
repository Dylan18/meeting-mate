@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [1/2] 首次运行，正在创建运行环境...
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo 没有找到 python，请先安装 Python 3.10+ 并勾选 "Add python.exe to PATH"
        echo 下载地址：https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
    python -m venv .venv
    if errorlevel 1 (
        echo 创建虚拟环境失败，请检查 Python 安装是否完整。
        pause
        exit /b 1
    )
    echo [2/2] 正在安装依赖（首次约需 2-3 分钟）...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，可尝试国内镜像重试：
        echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)

start "" ".venv\Scripts\pythonw.exe" "main.py"
