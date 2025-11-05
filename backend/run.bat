@echo off
call .venv\Scripts\activate
.\.venv\Scripts\python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
