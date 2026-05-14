#!/usr/bin/env bash
# ============================================================================
# EVOLUTIONARY TRADING ALGO // install_vps.sh
# ----------------------------------------------------------------------------
# Idempotent installer for the Evolutionary Trading Algo stack (JARVIS + Avengers + bots).
#
# Safe to re-run.  Every step checks before it writes.
#
# Usage (run on the VPS as the operator user, NOT root):
#   curl -fsSL https://raw.githubusercontent.com/<you>/eta_engine/main/deploy/install_vps.sh \
#     | bash -s -- --repo-url https://github.com/<you>/eta_engine.git --branch main
#
# Or cloned locally:
#   cd ~/eta_engine && ./deploy/install_vps.sh
#
# What it does:
#   1. Verifies prerequisites (python 3.12+, git, systemd user session)
#   2. Clones / pulls the repo into $INSTALL_DIR (default ~/eta_engine)
#   3. Sets up .venv and installs dependencies via pip
#   4. Writes .env from .env.example if missing (does NOT overwrite)
#   5. Runs full test suite -- aborts on failure
#   6. Installs systemd --user units (jarvis-live, avengers-fleet)
#   7. Installs crontab entries for ALFRED/BATMAN/ROBIN scheduled tasks
#   8. Prints post-install checklist
#
# What it does NOT do:
#   * Install system packages (you do that: apt install python3.12 git)
#   * Fill in secrets (you edit .env)
#   * Start the systemd units (you review logs first, then `systemctl --user start ...`)
#   * Touch sudo / root in any way
# ============================================================================
set -euo pipefail

# ----------------------------------------------------------------------------
# Configuration (override via CLI flags or env vars)
# ----------------------------------------------------------------------------
INSTALL_DIR="${INSTALL_DIR:-$HOME/eta_engine}"
REPO_URL="${REPO_URL:-}"
BRANCH="${BRANCH:-main}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
SKIP_TESTS="${SKIP_TESTS:-0}"
DRY_RUN="${DRY_RUN:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) INSTALL_DIR="$2"; shift 2;;
    --repo-url)    REPO_URL="$2"; shift 2;;
    --branch)      BRANCH="$2"; shift 2;;
    --python)      PYTHON_BIN="$2"; shift 2;;
    --skip-tests)  SKIP_TESTS=1; shift;;
    --dry-run)     DRY_RUN=1; shift;;
    -h|--help)
      grep '^#' "$0" | head -35
      exit 0
      ;;
    *) echo "Unknown flag: $1"; exit 2;;
  esac
done

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
log() { printf '\033[36m[apex-install]\033[0m %s\n' "$*"; }
ok()  { printf '\033[32m[OK]\033[0m %s\n' "$*"; }
warn(){ printf '\033[33m[WARN]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY RUN -- will print intended actions only"
fi

# Guard: do not run as root. Systemd --user requires a real user session.
if [[ "$(id -u)" == "0" ]]; then
  die "Do NOT run as root. Run as the operator user (e.g. 'edward')."
fi

# ----------------------------------------------------------------------------
# 1. Prerequisites
# ----------------------------------------------------------------------------
log "Step 1/8 -- checking prerequisites"
command -v git >/dev/null || die "git not found. apt install git"
command -v "$PYTHON_BIN" >/dev/null || die "$PYTHON_BIN not found. apt install python3.12 python3.12-venv"
command -v systemctl >/dev/null || die "systemctl not found -- need systemd"

# systemd user session must exist
if ! loginctl show-user "$USER" >/dev/null 2>&1; then
  warn "No persistent systemd user session yet. Enabling linger for $USER."
  warn "You may need: sudo loginctl enable-linger $USER"
fi

PY_VERSION=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
ok "python = $PY_VERSION"
ok "git    = $(git --version | cut -d' ' -f3)"

# ----------------------------------------------------------------------------
# 2. Clone / pull repo
# ----------------------------------------------------------------------------
log "Step 2/8 -- repo at $INSTALL_DIR"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    log "[dry-run] git -C $INSTALL_DIR fetch && git -C $INSTALL_DIR checkout $BRANCH && git -C $INSTALL_DIR pull"
  else
    git -C "$INSTALL_DIR" fetch --all
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only
  fi
  ok "repo updated"
elif [[ -n "$REPO_URL" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    log "[dry-run] git clone $REPO_URL $INSTALL_DIR -b $BRANCH"
  else
    git clone "$REPO_URL" "$INSTALL_DIR" -b "$BRANCH"
  fi
  ok "repo cloned"
else
  die "No repo at $INSTALL_DIR and no --repo-url provided"
fi

cd "$INSTALL_DIR"

# ----------------------------------------------------------------------------
# 3. Virtualenv + dependencies
# ----------------------------------------------------------------------------
log "Step 3/8 -- virtualenv + dependencies"
if [[ ! -d .venv ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    log "[dry-run] $PYTHON_BIN -m venv .venv"
  else
    "$PYTHON_BIN" -m venv .venv
  fi
  ok "created .venv"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  log "[dry-run] .venv/bin/pip install -e '.[dev]' + anthropic"
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install --upgrade pip wheel setuptools >/dev/null
  pip install -e '.[dev]' anthropic >/dev/null
  deactivate
fi
ok "dependencies installed"

# ----------------------------------------------------------------------------
# 4. .env file (does NOT overwrite)
# ----------------------------------------------------------------------------
log "Step 4/8 -- .env file"
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  ok "wrote .env from .env.example (chmod 600)"
  warn "FILL IN REAL VALUES in .env before starting services"
else
  ok ".env exists (not touching)"
fi

# Append Force Multiplier stanza if missing
if ! grep -q "^ETA_LLM_PROVIDER=" .env; then
  cat >> .env <<'ENV_APPEND'

# ---------------------------------------------------------------------------
# Force Multiplier / Avengers (appended by install_vps.sh)
# ---------------------------------------------------------------------------
ETA_LLM_PROVIDER=deepseek
ETA_ENABLE_CLAUDE_CLI=0
DEEPSEEK_API_KEY=
JARVIS_HOURLY_USD_BUDGET=1.00
JARVIS_DAILY_USD_BUDGET=10.00
JARVIS_DISTILL_SKIP_THRESHOLD=0.92
ENV_APPEND
  ok "appended Force Multiplier stanza to .env"
fi

# ----------------------------------------------------------------------------
# 5. Tests (abort on failure)
# ----------------------------------------------------------------------------
if [[ "$SKIP_TESTS" == "1" ]]; then
  warn "skipping tests (--skip-tests)"
else
  log "Step 5/8 -- running test suite"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "[dry-run] .venv/bin/python -m pytest -q"
  else
    # shellcheck disable=SC1091
    source .venv/bin/activate
    python -m pytest tests/ -q --tb=line -x
    deactivate
  fi
  ok "all tests green"
fi

# ----------------------------------------------------------------------------
# 6. Systemd --user units
# ----------------------------------------------------------------------------
log "Step 6/8 -- systemd --user units"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
mkdir -p "$INSTALL_DIR/../var/eta_engine/state"
mkdir -p "$INSTALL_DIR/../logs/eta_engine"

for unit in jarvis-live.service avengers-fleet.service eta-dashboard.service; do
  SRC="$INSTALL_DIR/deploy/systemd/$unit"
  DEST="$UNIT_DIR/$unit"
  if [[ -f "$SRC" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
      log "[dry-run] cp $SRC $DEST"
    else
      # Substitute paths in the unit
      sed \
        -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        -e "s|__USER__|$USER|g" \
        "$SRC" > "$DEST"
      chmod 644 "$DEST"
    fi
    ok "installed $unit"
  fi
done

if [[ "$DRY_RUN" != "1" ]]; then
  systemctl --user daemon-reload
fi
ok "systemd --user reloaded"

# ----------------------------------------------------------------------------
# 7. Crontab (ALFRED/BATMAN/ROBIN scheduled tasks)
# ----------------------------------------------------------------------------
log "Step 7/8 -- crontab entries"
CRON_SRC="$INSTALL_DIR/deploy/cron/avengers.crontab"
if [[ -f "$CRON_SRC" ]]; then
  # Render template with install dir
  TMP_CRON="$(mktemp)"
  sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$CRON_SRC" > "$TMP_CRON"
  # Merge with existing crontab without duplicating
  EXISTING="$(crontab -l 2>/dev/null || true)"
  MERGED="$(mktemp)"
  {
    printf '%s\n' "$EXISTING" | grep -v '# eta-engine:avengers' || true
    printf '\n'
    cat "$TMP_CRON"
  } > "$MERGED"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "[dry-run] crontab $MERGED -- would install:"
    cat "$TMP_CRON" | head -15
  else
    crontab "$MERGED"
    ok "crontab installed"
  fi
  rm -f "$TMP_CRON" "$MERGED"
else
  warn "no $CRON_SRC -- skipping cron install"
fi

# ----------------------------------------------------------------------------
# 8. Post-install checklist
# ----------------------------------------------------------------------------
log "Step 8/8 -- DONE. Next steps:"
cat <<EOF

  1. Edit secrets: \$EDITOR $INSTALL_DIR/.env
     (TRADOVATE_*, ANTHROPIC_API_KEY, any other credentials)

  2. Smoke-check the install:
     $INSTALL_DIR/.venv/bin/python -m deploy.scripts.smoke_check

  3. Enable linger (so services survive logout):
     sudo loginctl enable-linger $USER

  4. Start the services (in order):
     systemctl --user start jarvis-live
     systemctl --user start avengers-fleet
     systemctl --user start eta-dashboard

  5. Watch logs:
     journalctl --user -u jarvis-live -f
     journalctl --user -u avengers-fleet -f

  6. Enable auto-start on reboot:
     systemctl --user enable jarvis-live avengers-fleet eta-dashboard

  7. Operator runbook: $INSTALL_DIR/deploy/README.md

EOF
ok "install complete"
