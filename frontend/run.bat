@echo off
REM Start a simple static server on http://127.0.0.1:8080
cd /d %~dp0
py -m http.server 8080
