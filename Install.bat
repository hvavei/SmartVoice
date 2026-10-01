@echo off
if not exist "%~dp0release\SmartVoice-Setup-Standard.exe" (
  echo Installer missing: release\SmartVoice-Setup-Standard.exe
  pause
  exit /b 1
)
start "" /wait "%~dp0release\SmartVoice-Setup-Standard.exe"
