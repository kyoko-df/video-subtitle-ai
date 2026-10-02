@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\shengmu.exe" (
  echo Please install the project into .venv as described in README.md.
  pause
  exit /b 1
)
if not defined SHENGMU_MODEL_DIR set "SHENGMU_MODEL_DIR=%CD%\models"
".venv\Scripts\shengmu.exe" gui
