"""Supercharge fleet: apply optimal parameters from lab sweep + deactivate losers."""
import json
from pathlib import Path

REGISTRY = Path(r"C:\EvolutionaryTradingAlgo\eta_engine\strategies\per_bot_registry.py")
content = REGISTRY.read_text(encoding="utf-8")

# ─── Parameter overrides from heatmap analysis ────────────────────
# Each entry: (bot_id, atr_stop_mult, rr_target, additional)
OPTIMIZATIONS = {
    "eth_sage_daily":   {"atr_stop_mult": 0.75, "rr_target": 2.0},
    "eth_perp":         {"atr_stop_mult": 0.75, "rr_target": 2.0},
    "btc_optimized":    {"atr_stop_mult": 2.5,  "rr_target": 3.0},
    "mnq_futures_sage": {"atr_stop_mult": 2.5,  "rr_target": 3.0},
    "cross_asset_mnq":  {"atr_stop_mult": 2.5,  "rr_target": 2.0},
    "vwap_mr_btc":      {"atr_stop_mult": 0.75, "rr_target": 2.0},
    "eth_sweep_reclaim": {"atr_stop_mult": 2.0,  "rr_target": 3.0},
}

# ─── Bots to deactivate (can't be saved by parameter tweaks) ───────
DEACTIVATE = [
    "mnq_sweep_reclaim",  # WR 17.65%, sharpe -3.06, max_dd 17R
    "vwap_mr_mnq",        # WR 29.75%, sharpe -1.25
    "vwap_mr_nq",         # WR 29.77%, sharpe -1.24
]

print("=== SUPERCHARGING FLEET ===")

# Apply parameter fixes for each bot
for bot_id, params in OPTIMIZATIONS.items():
    # Find the bot's extras section
    search = f'bot_id="{bot_id}"'
    idx = content.find(search)
    if idx == -1:
        print(f"  SKIP {bot_id}: not found in registry")
        continue
    
    # Find the extras block for this bot
    extras_start = content.find("extras={", idx)
    if extras_start == -1:
        print(f"  SKIP {bot_id}: no extras block")
        continue
    
    # Extract the bot's atr_stop_mult and rr_target from extras
    modified = False
    lines = content[extras_start:].split("\n")
    
    for param, value in params.items():
        # Look for the parameter in the extras block
        old_pattern = f'"{param}":'
        if old_pattern in content[extras_start:extras_start+2000]:
            old_line_start = content.rfind("\n", extras_start, extras_start+2000)
            # Replace the specific parameter value
            # Find the exact line with this param
            block = content[extras_start:extras_start+3000]
            for line_start in range(len(block)):
                if old_pattern in block[line_start:line_start+50]:
                    # Found the parameter line
                    pass
    
    lines = content[extras_start:extras_start+3000].split("\n")
    new_block = []
    for line in lines:
        for param, value in params.items():
            pattern = f'"{param}":'
            if pattern in line and f'"{param}": {value}' not in line:
                indent = len(line) - len(line.lstrip())
                new_line = " " * indent + f'"{param}": {value},'
                if line.strip().endswith(","):
                    new_line = " " * indent + f'"{param}": {value},'
                modified = True
                line = new_line
                print(f"  {bot_id}: {param} → {value}")
                break
        new_block.append(line)
    
    if modified:
        old_block = "\n".join(lines)
        new_block_str = "\n".join(new_block)
        content = content[:extras_start] + new_block_str + content[extras_start+len(old_block):]

print(f"\nApplied {len(OPTIMIZATIONS)} parameter optimizations")

# Deactivate losing bots
for bot_id in DEACTIVATE:
    search = f'bot_id="{bot_id}"'
    idx = content.find(search)
    if idx == -1:
        print(f"  SKIP deactivate {bot_id}: not found")
        continue
    
    # Check if already deactivated
    extras = content[idx:idx+500]
    if '"deactivated": True' in extras or '"deactivated": true' in extras:
        print(f"  SKIP deactivate {bot_id}: already deactivated")
        continue
    
    # Add deactivation to extras
    extras_start = content.find("extras={", idx)
    if extras_start == -1 or extras_start > idx + 500:
        print(f"  SKIP deactivate {bot_id}: no extras block near bot")
        continue
    
    # Add deactivated flag
    bracket_idx = content.find("{", extras_start)
    if bracket_idx == -1 or bracket_idx > extras_start + 100:
        print(f"  SKIP deactivate {bot_id}: could not find extras bracket")
        continue
    
    insert = '\n            "deactivated": True,\n            "deactivation_reason": "lab sweep 2026-05-04 — failing all gates",'
    content = content[:bracket_idx+1] + insert + content[bracket_idx+1:]
    print(f"  DEACTIVATE: {bot_id}")

# Also deactivate nq_futures_sage if it's a loser
nq_search = content.find('bot_id="nq_futures_sage"')
if nq_search > -1:
    nq_extras = content.find("extras={", nq_search)
    if nq_extras > -1 and nq_extras < nq_search + 500:
        if '"deactivated": True' not in content[nq_extras:nq_extras+200]:
            bracket_idx = content.find("{", nq_extras)
            if bracket_idx > -1 and bracket_idx < nq_extras + 100:
                insert = '\n            "deactivated": True,\n            "deactivation_reason": "lab sweep 2026-05-04 — sharpe 0.26, failing gate",'
                content = content[:bracket_idx+1] + insert + content[bracket_idx+1:]
                print(f"  DEACTIVATE: nq_futures_sage")

print(f"\nDeactivated {len(DEACTIVATE)+1} losing bots")
print(f"Total file size: {len(content)} chars")

# Write back
REGISTRY.write_text(content, encoding="utf-8")
print("\nRegistry updated!")
