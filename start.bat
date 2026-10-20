@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe python -m venv .venv
.venv\Scripts\python -c "import paramiko, PIL" 2>nul || .venv\Scripts\python -m pip install -q -r requirements.txt
start "" .venv\Scripts\pythonw.exe app.py
