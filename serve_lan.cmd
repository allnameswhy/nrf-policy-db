@echo off
rem 팀 테스트용 LAN 서버 기동 — 방화벽이 이미 인바운드 허용한 기본 파이썬(python310)으로 돌린다.
rem venv 파이썬(.venv\Scripts\python.exe)은 공용 네트워크 방화벽 허용 규칙이 없어
rem 타 기기 접속이 차단됨(2026-08 실측). 라이브러리는 venv site-packages를 그대로 빌려 쓴다
rem (pywin32는 .pth 트릭 설치라 하위 경로를 PYTHONPATH에 직접 나열).
cd /d "%~dp0"
set "SP=%~dp0.venv\Lib\site-packages"
set "PYTHONPATH=%SP%;%SP%\win32;%SP%\win32\lib;%SP%\Pythonwin;%SP%\pywin32_system32"
"%LOCALAPPDATA%\Programs\Python\Python310\python.exe" src\serve.py --host 0.0.0.0 --concurrency 2 %*
