#!/usr/bin/env bash
# Build distributable tar.gz and zip releases of music-organiser.
#
# Usage:
#   bash make-release.sh                    # auto name from _VERSION in web_ui.py
#   bash make-release.sh 4chanRemix-v1.4.1  # explicit release name

set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── release name ──────────────────────────────────────────────────────────────
if [ "${1:-}" != "" ]; then
    RELEASE_NAME="${1}"
else
    VERSION=$(python3 -c "
import re
txt = open('${PROJ_DIR}/web_ui.py').read()
m = re.search(r'_VERSION\s*=\s*\"([^\"]+)\"', txt)
print(m.group(1) if m else '0.0.0')
" 2>/dev/null || echo "0.0.0")
    RELEASE_NAME="music-organiser-v${VERSION}"
fi

TAR_FILE="${RELEASE_NAME}.tar.gz"
ZIP_FILE="${RELEASE_NAME}.zip"
TMP_DIR=$(mktemp -d)
STAGE="${TMP_DIR}/${RELEASE_NAME}"

_green() { printf '\033[0;32m  ✓  %s\033[0m\n' "$*"; }
_step()  { printf '\033[1;34m  ▶  %s\033[0m\n' "$*"; }

echo ""
echo "  building  ${RELEASE_NAME}"
echo "  output    ${TAR_FILE}  +  ${ZIP_FILE}"
echo ""

# ── stage files ───────────────────────────────────────────────────────────────
_step "Staging files"
mkdir -p "${STAGE}"

cp "${PROJ_DIR}/web_ui.py"        "${STAGE}/"
cp "${PROJ_DIR}/install.sh"       "${STAGE}/"
cp "${PROJ_DIR}/Makefile"         "${STAGE}/"
cp "${PROJ_DIR}/requirements.txt" "${STAGE}/"
cp "${PROJ_DIR}/README.md"        "${STAGE}/"
cp "${PROJ_DIR}/make-release.sh"  "${STAGE}/"

for f in acoustid_helper.py detection.py; do
    [ -f "${PROJ_DIR}/${f}" ] && cp "${PROJ_DIR}/${f}" "${STAGE}/"
done

cp -r "${PROJ_DIR}/zzzzScriptstuff" "${STAGE}/"

[ -f "${PROJ_DIR}/config.default.toml" ] && \
    cp "${PROJ_DIR}/config.default.toml" "${STAGE}/"

_green "staged to ${STAGE}"

# ── strip build artifacts ─────────────────────────────────────────────────────
_step "Cleaning build artifacts"
find "${STAGE}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -name "*.pyc"   -delete 2>/dev/null || true
find "${STAGE}" -name "*.pyo"   -delete 2>/dev/null || true
find "${STAGE}" -name ".DS_Store" -delete 2>/dev/null || true
_green "cleaned"

# ── permissions ───────────────────────────────────────────────────────────────
chmod +x "${STAGE}/install.sh"
chmod +x "${STAGE}/make-release.sh"

# ── tar.gz ────────────────────────────────────────────────────────────────────
_step "Building ${TAR_FILE}"
(cd "${TMP_DIR}" && tar czf "${OLDPWD}/${TAR_FILE}" "${RELEASE_NAME}/")
TAR_SIZE=$(du -sh "${TAR_FILE}" | cut -f1)
TAR_COUNT=$(tar tzf "${TAR_FILE}" | wc -l)
_green "${TAR_FILE}  (${TAR_SIZE}, ${TAR_COUNT} files)"

# ── zip ───────────────────────────────────────────────────────────────────────
_step "Building ${ZIP_FILE}"
if ! command -v zip &>/dev/null; then
    printf '\033[0;33m  ⚠  zip not found — skipping .zip (install with: sudo apt install zip)\033[0m\n'
else
    (cd "${TMP_DIR}" && zip -qr "${OLDPWD}/${ZIP_FILE}" "${RELEASE_NAME}/")
    ZIP_SIZE=$(du -sh "${ZIP_FILE}" | cut -f1)
    ZIP_COUNT=$(unzip -l "${ZIP_FILE}" | tail -1 | awk '{print $2}')
    _green "${ZIP_FILE}  (${ZIP_SIZE}, ${ZIP_COUNT} files)"
fi

# ── cleanup ───────────────────────────────────────────────────────────────────
rm -rf "${TMP_DIR}"

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ┌─────────────────────────────────────────────────────────┐"
echo "  │  ${TAR_FILE}"
[ -f "${ZIP_FILE}" ] && \
echo "  │  ${ZIP_FILE}"
echo "  │"
echo "  │  recipient installs with:"
echo "  │    tar xzf ${TAR_FILE}"
echo "  │    cd ${RELEASE_NAME}"
echo "  │    bash install.sh --deps"
echo "  └─────────────────────────────────────────────────────────┘"
echo ""
