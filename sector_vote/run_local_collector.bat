@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0.."

where py >nul 2>&1
if errorlevel 1 (
  echo Python launcher ^(py^)를 찾을 수 없습니다.
  echo https://www.python.org/downloads/ 에서 Python 3.12 이상을 설치하세요.
  pause
  exit /b 1
)

py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo Python 3.12를 찾을 수 없습니다.
  echo https://www.python.org/downloads/ 에서 Python 3.12를 설치하세요.
  pause
  exit /b 1
)

if not exist "sector_vote\.collector-venv\Scripts\python.exe" (
  echo [1/3] 전용 Python 환경을 만드는 중...
  py -3.12 -m venv "sector_vote\.collector-venv"
  if errorlevel 1 goto :error
)

echo [2/3] 필요한 패키지를 확인하는 중...
"sector_vote\.collector-venv\Scripts\python.exe" -m pip install -q -r "sector_vote\requirements.txt"
if errorlevel 1 goto :error

echo [3/3] 최근 유튜브 자막 수집을 시작합니다.
"sector_vote\.collector-venv\Scripts\python.exe" -m sector_vote.local_collector
set EXIT_CODE=%ERRORLEVEL%

echo.
if %EXIT_CODE% EQU 0 (
  echo 작업이 완료되었습니다. 웹페이지를 새로고침해 확인하세요.
) else (
  echo 일부 또는 전체 수집이 실패했습니다. 위 오류 내용을 확인하세요.
)
pause
exit /b %EXIT_CODE%

:error
echo 설치 중 오류가 발생했습니다.
pause
exit /b 1
