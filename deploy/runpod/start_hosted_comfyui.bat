@echo off
setlocal
title Hosted ComfyUI
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_hosted_comfyui.ps1"
echo.
pause
