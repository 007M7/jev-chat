@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ================================================================
echo   Jev 副驾（Windows）  —— 只读读屏 + 判断 + 悬浮窗 + 复制
echo   不发送、不填入、不碰微信进程
echo ================================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [x] 没找到 python。装一个 Python 3.10+ 并勾选 "Add to PATH"。
  pause & exit /b 1
)

if not exist vendor\windows_capture (
  echo [ ] 首次运行：安装采集依赖到 desktop\vendor（装在仓库内，不占 C 盘）
  python -m pip install --target vendor --no-deps windows-capture
  if errorlevel 1 (
    echo [x] 依赖安装失败。手动执行： python -m pip install --target vendor --no-deps windows-capture
    pause & exit /b 1
  )
)

echo --- 预检 ---
python app.py --check
if errorlevel 1 (
  echo.
  echo 预检未通过。按上面 [x] 的提示处理后重跑本文件。
  pause & exit /b 1
)

echo.
echo --- 启动（关闭悬浮窗或用 Ctrl+C 退出）---
python app.py %*
pause
