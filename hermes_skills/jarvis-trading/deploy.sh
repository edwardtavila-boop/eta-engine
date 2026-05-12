#!/usr/bin/env bash
#
# Install the jarvis-trading Hermes skill into the operator's local Hermes Agent.
#
# Copies the directory this script lives in into $HOME/.hermes/skills/jarvis-trading.
# Diagnoses if Hermes Agent isn't installed. Prompts before overwriting an existing
# install unless --force / -f is supplied.
#
# Usage:
#   bash deploy.sh
#   bash deploy.sh --force

set -euo pipefail

FORCE=0
for arg in "$@"; do
    case "$arg" in
        -f|--force)
            FORCE=1
            ;;
        -h|--help)
            sed -n '1,/^set -euo/p' "$0" | sed 's/^#//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: bash deploy.sh [--force]" >&2
            exit 2
            ;;
    esac
done

# Resolve source (the directory containing this script) and destination.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_SKILLS_ROOT="${HOME}/.hermes/skills"
DEST_DIR="${HERMES_SKILLS_ROOT}/jarvis-trading"

echo "jarvis-trading deploy"
echo "  source:      ${SCRIPT_DIR}"
echo "  destination: ${DEST_DIR}"
echo ""

# Diagnose: Hermes skills root must exist.
if [ ! -d "${HERMES_SKILLS_ROOT}" ]; then
    echo "ERROR: Hermes skills directory not found at ${HERMES_SKILLS_ROOT}" >&2
    echo "Hermes not installed - run hermes-desktop first to bootstrap ~/.hermes/." >&2
    echo "After installing Hermes Agent, re-run this script." >&2
    exit 1
fi

# Token warning (non-fatal).
if [ -z "${JARVIS_MCP_TOKEN:-}" ]; then
    echo "WARNING: JARVIS_MCP_TOKEN env var is not set." >&2
    echo "Set JARVIS_MCP_TOKEN before starting Hermes Agent or JARVIS calls will fail with 401." >&2
    echo ""
fi

# Prompt to overwrite.
if [ -d "${DEST_DIR}" ]; then
    if [ "${FORCE}" -eq 1 ]; then
        echo "Existing install at ${DEST_DIR} will be overwritten (--force)."
    else
        printf "jarvis-trading already exists at %s. Overwrite? (y/N) " "${DEST_DIR}"
        read -r response
        case "${response}" in
            [Yy]*)
                ;;
            *)
                echo "Aborted. No changes made."
                exit 0
                ;;
        esac
    fi
    rm -rf "${DEST_DIR}"
fi

# Copy the tree.
mkdir -p "${HERMES_SKILLS_ROOT}"
cp -R "${SCRIPT_DIR}" "${DEST_DIR}"

# Verify manifest landed.
MANIFEST_PATH="${DEST_DIR}/manifest.yaml"
if [ ! -f "${MANIFEST_PATH}" ]; then
    echo "ERROR: manifest.yaml missing in destination after copy." >&2
    echo "Expected at: ${MANIFEST_PATH}" >&2
    exit 1
fi

echo ""
echo "Installed. Restart Hermes Agent to pick up the new skill."
exit 0
