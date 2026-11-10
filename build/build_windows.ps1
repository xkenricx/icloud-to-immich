# Builds dist\iCloudToImmich.exe. Run from the iCloudToImmich folder:
#   powershell -ExecutionPolicy Bypass -File build\build_windows.ps1
$ErrorActionPreference = "Stop"
if (-not (Test-Path .venv)) { uv venv --python 3.12 .venv }
uv pip install --python .venv -r requirements.txt pyinstaller
# fido2 ships a data file it reads at import time; without --collect-data fido2 the exe crashes on start.
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --console --name iCloudToImmich `
  --add-data "app\static;app\static" `
  --collect-submodules pyicloud --collect-data pyicloud --collect-data fido2 --collect-data certifi `
  --workpath build\work --specpath build\work main.py
Write-Host "Built dist\iCloudToImmich.exe"
