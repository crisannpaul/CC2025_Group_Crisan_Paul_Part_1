@echo off
python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
echo Backend venv ready. Use: call .venv\Scripts\activate && run.bat
