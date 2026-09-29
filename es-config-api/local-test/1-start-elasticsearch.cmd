@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp01-start-elasticsearch.ps1" %*
if errorlevel 1 pause
