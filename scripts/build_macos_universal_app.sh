#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="${ROOT_DIR}/scripts"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PYTHON_ARCH="${PYTHON_ARCH:-}"
APP_NAME="${APP_NAME:-HHLook}"
ENTRY_FILE="${SCRIPT_DIR}/windows_entry.py"
DIST_DIR="${ROOT_DIR}/dist"
ARCH_LABEL="${1:-native}"
APP_STAGE_DIR="${ROOT_DIR}/tmp/macos_${ARCH_LABEL}_stage"

if [[ ! -f "${ENTRY_FILE}" ]]; then
  echo "Entry file not found: ${ENTRY_FILE}" >&2
  exit 1
fi

run_python() {
  if [[ -n "${PYTHON_ARCH}" ]]; then
    /usr/bin/arch "-${PYTHON_ARCH}" "${PYTHON_BIN}" "$@"
  else
    "${PYTHON_BIN}" "$@"
  fi
}

echo "[build] python: ${PYTHON_BIN}"
run_python --version

echo "[build] upgrade pip + install build deps"
run_python -m pip install --upgrade pip
run_python -m pip install -r "${ROOT_DIR}/requirements.txt" pyinstaller

echo "[build] pyinstaller app (${ARCH_LABEL})"
run_python -m PyInstaller \
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

ARCH_APP_PATH="${DIST_DIR}/${APP_NAME}-${ARCH_LABEL}.app"
ZIP_OUT="${DIST_DIR}/${APP_NAME}-macOS-${ARCH_LABEL}.zip"
DMG_OUT="${DIST_DIR}/${APP_NAME}-macOS-${ARCH_LABEL}.dmg"

rm -rf "${ARCH_APP_PATH}"
cp -R "${APP_PATH}" "${ARCH_APP_PATH}"

echo "[build] pack zip"
rm -f "${ZIP_OUT}"
ditto -c -k --sequesterRsrc --keepParent "${ARCH_APP_PATH}" "${ZIP_OUT}"

echo "[build] pack dmg"
rm -f "${DMG_OUT}"
rm -rf "${APP_STAGE_DIR}"
mkdir -p "${APP_STAGE_DIR}"
cp -R "${ARCH_APP_PATH}" "${APP_STAGE_DIR}/"
hdiutil create -volname "${APP_NAME}-${ARCH_LABEL}" -srcfolder "${APP_STAGE_DIR}" -ov -format UDZO "${DMG_OUT}" >/dev/null
rm -rf "${APP_STAGE_DIR}"

echo "[build] done"
echo "  app: ${ARCH_APP_PATH}"
echo "  zip: ${ZIP_OUT}"
echo "  dmg: ${DMG_OUT}"
