@echo off
setlocal
title Hosted ComfyUI - Owner
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0manage_hosted_comfyui.ps1"
echo.
pause
