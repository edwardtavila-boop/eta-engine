#!/usr/bin/env bash
# EVOLUTIONARY TRADING ALGO  //  install_uv.sh
#
# Install astral-sh's `uv` -- a Rust-rewrite of pip+venv that's 10-100x
# faster. After this, `make install` reinstalls drop from minutes to
# seconds, which matters during the rapid-redeploy loop.
#
# Installs uv into ~/.local/bin/uv (no sudo needed). The repo's
# Makefile already prefers uv when present.
#
# Idempotent: re-running upgrades to the latest stable.
#
# Usage:
#   bash install_uv.sh
#   bash install_uv.sh --pinned 0.4.20

set -euo pipefail

PINNED=""
if [[ "${1:-}" == "--pinned" ]]; then
  PINNED="$2"
fi

if command -v uv >/dev/null 2>&1; then
  echo "uv already installed: $(uv --version)"
fi

# Use the official one-liner installer, isolated to ~/.local.
export UV_INSTALL_DIR="${HOME}/.local/bin"
export UV_NO_MODIFY_PATH=1
mkdir -p "${UV_INSTALL_DIR}"

if [[ -n "${PINNED}" ]]; then
  curl -LsSf "https://astral.sh/uv/${PINNED}/install.sh" | sh
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Ensure ~/.local/bin is on PATH (operator's shell config; we suggest, not modify).
case ":${PATH}:" in
  *":${UV_INSTALL_DIR}:"*) : ;;
  *) echo "Note: add '${UV_INSTALL_DIR}' to your PATH to use uv directly." ;;
esac

"${UV_INSTALL_DIR}/uv" --version
echo "OK -- uv installed."
