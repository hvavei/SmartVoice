@echo off
setlocal
set "OLD=%LOCALAPPDATA%\AzureTTSStudio"
if not exist "%OLD%" (
  echo Legacy AzureTTSStudio installation was not found.
  pause
  exit /b 0
)
echo This removes only the legacy program/build files.
echo config.json, output, caches and user data will be preserved.
choice /M "Continue"
if errorlevel 2 exit /b 0
if exist "%OLD%\unins000.exe" (
  start "" /wait "%OLD%\unins000.exe"
) else (
  del /Q "%OLD%\AzureTTSStudio.exe" 2>nul
  del /Q "%OLD%\SmartVoice.exe" 2>nul
  echo Legacy executable removed. User data was preserved at:
  echo %OLD%
)
pause
