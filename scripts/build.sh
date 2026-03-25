#!/bin/sh
# Build ramp-cli into a standalone native binary using Nuitka.
# Usage: ./scripts/build.sh          (run from repo root)
# Output: dist/ramp-{os}-{arch}
set -eu

# ---------------------------------------------------------------------------
# Detect platform
# ---------------------------------------------------------------------------
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"   # darwin | linux
RAW_ARCH="$(uname -m)"                           # arm64 | x86_64 | aarch64

case "${RAW_ARCH}" in
    x86_64)  ARCH="amd64" ;;
    aarch64) ARCH="arm64" ;;
    arm64)   ARCH="arm64" ;;
    *)
        echo "Error: unsupported architecture '${RAW_ARCH}'" >&2
        exit 1
        ;;
esac

ARTIFACT="ramp-${OS}-${ARCH}"
echo "Building ${ARTIFACT} ..."

# ---------------------------------------------------------------------------
# Ensure we are at the repo root (where pyproject.toml lives)
# ---------------------------------------------------------------------------
if [ ! -f pyproject.toml ]; then
    echo "Error: run this script from the repository root." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Run Nuitka
# ---------------------------------------------------------------------------
uv run python -m nuitka \
    --standalone \
    --output-filename="${ARTIFACT}" \
    --output-dir=dist \
    --include-package=ramp_cli \
    --include-package-data=ramp_cli \
    --include-data-files=src/ramp_cli/specs/*.json=ramp_cli/specs/ \
    --python-flag=no_site \
    --assume-yes-for-downloads \
    src/ramp_cli/main.py

DIST_DIR="dist/main.dist"

echo ""
echo "Built: ${DIST_DIR}/${ARTIFACT}"
ls -lh "${DIST_DIR}/${ARTIFACT}"
echo "Directory size:"
du -sh "${DIST_DIR}"

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
echo ""
echo "Running smoke tests ..."
"./${DIST_DIR}/${ARTIFACT}" --version
"./${DIST_DIR}/${ARTIFACT}" --help >/dev/null && echo "  --help OK"
echo "Done."
