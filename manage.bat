@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\runpod\manage_hosted_comfyui.ps1" %*
exit /b %errorlevel%
