@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp02-start-api.ps1" %*
if errorlevel 1 pause
