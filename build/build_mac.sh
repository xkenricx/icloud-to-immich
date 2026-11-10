#!/bin/sh
# Builds dist/iCloud to Immich.app (and a zip of it). Run on a Mac from the iCloudToImmich folder:
#   sh build/build_mac.sh
set -e
command -v uv >/dev/null || { echo "Install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv -r requirements.txt pyinstaller
# --windowed: a normal Mac app with no Terminal window; it opens the browser itself and
# quits from the page's "Sign out and close" button (or after 20 idle minutes).
.venv/bin/python -m PyInstaller --noconfirm --windowed --name "iCloud to Immich" \
  --osx-bundle-identifier io.github.xkenricx.icloudtoimmich \
  --add-data "app/static:app/static" \
  --collect-submodules pyicloud --collect-data pyicloud --collect-data fido2 --collect-data certifi \
  --workpath build/work --specpath build/work main.py
cd dist && rm -f iCloudToImmich-mac.zip && ditto -c -k --keepParent "iCloud to Immich.app" iCloudToImmich-mac.zip
echo "Built dist/iCloud to Immich.app and dist/iCloudToImmich-mac.zip"
