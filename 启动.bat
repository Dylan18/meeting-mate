@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title meeting-mate

if exist ".venv\Scripts\pythonw.exe" goto run

echo ================================================
echo  首次运行，正在准备运行环境
echo ================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

echo [1/2] 创建虚拟环境...
python -m venv .venv
if errorlevel 1 goto venvfail

echo [2/2] 安装依赖（约 2-3 分钟）...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto pipfail
echo.
echo 环境准备完成，正在启动...
echo.

:run
start "" ".venv\Scripts\pythonw.exe" "main.py"
exit /b 0

:nopython
echo [错误] 没有找到 python 命令。
echo 请先安装 Python 3.10 或更高版本，安装时务必勾选
echo   "Add python.exe to PATH"
echo 下载地址：https://www.python.org/downloads/
echo.
pause
exit /b 1

:venvfail
echo [错误] 创建虚拟环境失败，请检查 Python 安装是否完整。
pause
exit /b 1

:pipfail
echo [错误] 依赖安装失败。可以试试国内镜像：
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1
