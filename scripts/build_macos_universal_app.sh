#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="${ROOT_DIR}/scripts"
PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_NAME="${APP_NAME:-HHLook}"
ENTRY_FILE="${SCRIPT_DIR}/windows_entry.py"
DIST_DIR="${ROOT_DIR}/dist"
STAGE_DIR="${ROOT_DIR}/tmp/macos_dmg_stage"

if [[ ! -f "${ENTRY_FILE}" ]]; then
  echo "Entry file not found: ${ENTRY_FILE}" >&2
  exit 1
fi

echo "[build] python: ${PYTHON_BIN}"
"${PYTHON_BIN}" --version

echo "[build] upgrade pip + install build deps"
"${PYTHON_BIN}" -m pip install --upgrade pip
"${PYTHON_BIN}" -m pip install -r "${ROOT_DIR}/requirements.txt" pyinstaller

echo "[build] install playwright chromium into package-local cache"
export PLAYWRIGHT_BROWSERS_PATH=0
"${PYTHON_BIN}" -m playwright install chromium

echo "[build] pyinstaller universal2 app"
"${PYTHON_BIN}" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "${APP_NAME}" \
  --paths "${ROOT_DIR}/src" \
  --collect-all playwright \
  --collect-all pydantic \
  --collect-all pydantic_core \
  --collect-all bs4 \
  --collect-all lxml \
  --target-arch universal2 \
  "${ENTRY_FILE}"

APP_PATH="${DIST_DIR}/${APP_NAME}.app"
if [[ ! -d "${APP_PATH}" ]]; then
  echo "Build failed: app bundle not found at ${APP_PATH}" >&2
  exit 1
fi

APP_BIN="${APP_PATH}/Contents/MacOS/${APP_NAME}"
if [[ -f "${APP_BIN}" ]]; then
  echo "[build] main binary architectures:"
  lipo -archs "${APP_BIN}"
fi

ZIP_OUT="${DIST_DIR}/${APP_NAME}-macOS-universal.zip"
DMG_OUT="${DIST_DIR}/${APP_NAME}-macOS-universal.dmg"

echo "[build] pack zip"
rm -f "${ZIP_OUT}"
ditto -c -k --sequesterRsrc --keepParent "${APP_PATH}" "${ZIP_OUT}"

echo "[build] pack dmg"
rm -f "${DMG_OUT}"
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"
cp -R "${APP_PATH}" "${STAGE_DIR}/"
hdiutil create -volname "${APP_NAME}" -srcfolder "${STAGE_DIR}" -ov -format UDZO "${DMG_OUT}" >/dev/null
rm -rf "${STAGE_DIR}"

echo "[build] done"
echo "  app: ${APP_PATH}"
echo "  zip: ${ZIP_OUT}"
echo "  dmg: ${DMG_OUT}"
