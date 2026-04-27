# Full ETA rebrand — staged migration plan, 2026-04-27

User directive: "i am getting rid of the apex predator name and want
everything labeled evolutionary trading algo or eta" + "all levels".

This **OVERRIDES** the prior `eta_branding_split.md` decision. Memory
note saved at `~/.claude/.../memory/eta_full_rebrand_override.md`.

## Done in this session (commit forthcoming)

### Level 2 — Cloudflare assets

* ✅ Tunnel `apex-predator` (id `d8b901a6...`) renamed to `eta-engine`
  * Status: healthy, 2 connectors preserved
  * 3 ingress rules preserved (ops/app/jarvis on apexpredator.live —
    will be migrated to evolutionarytradingalgo.com once DNS-edit
    token is provided; see L1 below)
* ✅ Tunnel `firm-command-center` (id `2342fd00...`) renamed to
  `eta-command-center`
* ⚠️ Dormant `Apex Tunnel` (id `dd9d7dfb...`) — Cloudflare API
  blocked deletion ("has private network routes" but route list
  returns empty). Dashboard manual delete needed.

### Level 3 — Visible config files

* ✅ `firm_command_center/services/FirmCommandCenter.xml` description
  → "Evolutionary Trading Algo command center"
* ✅ `firm_command_center/services/FirmCore.xml` description
  → "Evolutionary Trading Algo live runtime core"
* ✅ `firm_command_center/services/FirmWatchdog.xml` description
  → "Evolutionary Trading Algo watchdog service"
* ✅ `firm_command_center/eta_engine/setup_private_vps.ps1`
  `$PrivateHostname` default → `command-center.eta.internal`

## Blocked on user action

### Level 1 — Public DNS migration

Token has `Cloudflare Tunnel:Edit` but not `Zone:DNS:Edit`. To execute
the URL migration from `*.apexpredator.live` to `*.evolutionarytradingalgo.com`:

1. Generate fresh API token at https://dash.cloudflare.com/profile/api-tokens
   with permissions:
   * `Account → Cloudflare Tunnel → Edit`
   * `Zone → DNS → Edit` (scope to `evolutionarytradingalgo.com`)
   * `Zone → Zone → Read`
2. Save it the same way as before (paste back in chat)
3. I will then:
   * Add CNAME records on `evolutionarytradingalgo.com`:
     * `command-center.evolutionarytradingalgo.com` → `eta-command-center` tunnel
     * `ops.evolutionarytradingalgo.com` → `eta-engine` tunnel (8420)
     * `app.evolutionarytradingalgo.com` → already exists (the gated app)
     * `jarvis.evolutionarytradingalgo.com` → `eta-engine` tunnel (8000)
   * Update both tunnels' ingress configs to add the new ETA hostnames
     (dual-routing during transition)
   * Verify each ETA URL resolves + serves correctly
   * Remove the apexpredator.live ingress entries (cutover)
   * Optionally delete apexpredator.live DNS records (final sunset)

## Level 4 — Code rebrand (staged, multi-session)

### Real scope (corrected from "4143+ refs" memory note)

A targeted audit found that **most "apex_predator" references are
strings, not Python imports**:

* No active Python package named `apex_predator` to migrate
* `firm_command_center/apex_predator/` is just a 1-file state dir
  (`data/runtime_state.json`) actively written by
  `command_center/server/app.py` — renaming requires coordinated
  code change (current write paths are hardcoded)
* The 4143 figure includes:
  * Docstrings + code comments (low-risk text replace)
  * Path-string literals (e.g. `"apex_predator/scripts/jarvis_assess.py"`)
  * Test assertion strings (`"uv sync ... from apex_predator root"`)
  * Workflow filenames (`.github/workflows/apex_predator.yml`)
  * Released artifact names (`apex_predator_windows_release.zip`)
  * The `_archive/` trees (which we don't touch)

### Phase 1 — Safe text replaces (~1-2 hours of focused work)

Replace strings that are NOT path-load-bearing:
* Docstrings
* Code comments
* Pure print/log strings
* README / doc files
* Service descriptions (already done in L3)

Skip in Phase 1: anything that reads/writes a path from disk that
mentions `apex_predator` in the actual filesystem layout.

Run regression tests after each module's text-replace batch.

### Phase 2 — Coordinated path rename (~1-2 days, careful)

The `firm_command_center/apex_predator/` directory rename. This requires:
1. Create new `firm_command_center/eta_state/` directory
2. Update all writers (`command_center/server/app.py`,
   `preflight.py`, `remote_ops.py`, `vps_status.py`) to write to
   the new path
3. Migrate `runtime_state.json` (single file) to the new location
4. Add a back-compat shim: if old path exists, read from there once
   then move to new path
5. After 1 week of running cleanly, delete the old path
6. Restart FirmCommandCenter service to pick up new path

### Phase 3 — Workflow + artifact rename (~30 min)

* Rename `.github/workflows/apex_predator.yml` →
  `.github/workflows/eta_engine.yml`
* Update workflow internals (description, output artifacts)
* Rename released zip: `apex_predator_windows_release.zip` →
  `eta_engine_windows_release.zip`
* Update version manifest
* CI re-runs the renamed workflow on next push

### Phase 4 — Memory + doc consolidation

* Update each memory note that mentions apex_predator to use ETA
  language (already done in MEMORY.md index)
* Update CLAUDE.md if it references apex_predator
* Update README files

## Risk register

| Risk | Mitigation |
|---|---|
| FirmCommandCenter dashboard breaks during apex_predator/ rename | Dual-write back-compat shim (Phase 2 step 4) |
| Strategy IDs in `strategy_baselines.json` invalidated | None — strategy IDs (`mnq_orb_v1`, `btc_sage_daily_etf_v1`, etc.) are **asset-named**, not branding-named. Safe. |
| Bot IDs in `per_bot_registry.py` invalidated | Same — `bot_id` values like `mnq_futures`, `btc_hybrid`, `eth_compression` are asset/strategy-named. Safe. |
| Test fixtures with golden-file assertions break | Run pytest after each Phase 1 batch; fix assertion strings inline |
| GitHub workflow rename breaks deployed CI runs | Phase 3 is a clean cut; old runs preserved in Actions history |
| `apex_predator_batman/` sister repo references | Acknowledge in docs as legacy; rename in same Phase 3 sweep if user has access to that repo |

## Out of scope (user explicitly noted)

* Strategy IDs / bot IDs (already asset-named, no rename needed)
* `apexpredator.llc` Cloudflare zone (kept for legal LLC name; ETA
  branding sits at company-name level not legal-entity level)
* Archived dirs under `_archive/` (snapshot of old state, untouched)

## Bottom line

L1 is gated on a fresh API token with DNS-edit scope (1 minute
operator action). L2 + L3 visible config done this session. L4 is
a 1-3 day staged migration; Phase 1 is doable next session, Phase 2
needs careful coordination on the live VPS, Phase 3 + 4 are quick
cleanup once Phase 2 lands.

The trading layer + 24/7 runtime is unaffected by all of this —
service auto-restart picks up new XML descriptions on next reboot
without dropping any in-flight trades.
