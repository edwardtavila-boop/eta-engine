"""Promote BTC-related bots from research_candidate → production_candidate."""

from eta_engine.scripts import workspace_roots

fpath = workspace_roots.ETA_ENGINE_ROOT / "strategies" / "per_bot_registry.py"
content = fpath.read_text(encoding="utf-8")

# BTC bots that have research_candidate status:
# btc_optimized, btc_regime_trend_etf, btc_sage_daily_etf,
# volume_profile_btc, mbt_sweep_reclaim (Micro BTC)

# Find each entry and promote within its context
btc_bots = [
    "btc_optimized",
    "btc_regime_trend_etf",
    "btc_sage_daily_etf",
    "volume_profile_btc",
    "mbt_sweep_reclaim",
]

for bot_id in btc_bots:
    print(f"Promoting {bot_id}...")
    content = content.replace(
        f'bot_id="{bot_id}"',
        f'bot_id="{bot_id}"',
    )

# The actual promotion: change promotion_status and research_candidate flag
# We target entries that are near BTC bot_ids by looking at the regex pattern
# that finds bot_id="btc_*" followed by promotion_status

lines = content.split("\n")
in_btc_extras = False
btc_depth = 0
promoted = 0

for i, line in enumerate(lines):
    # Detect if we're in a BTC bot extras block
    if 'bot_id="btc' in line or 'bot_id="volume_profile_btc"' in line or 'bot_id="mbt_' in line:
        in_btc_extras = False
        btc_depth = 30  # search next 30 lines for extras
    elif btc_depth > 0:
        btc_depth -= 1
        if "extras={" in line and not in_btc_extras:
            in_btc_extras = True
        if in_btc_extras:
            if '"promotion_status": "research_candidate"' in line:
                lines[i] = line.replace("research_candidate", "production_candidate")
                promoted += 1
                print(f"  Line {i + 1}: promotion_status -> production_candidate")
            if '"research_candidate": True' in line:
                lines[i] = line.replace('"research_candidate": True', '"production_candidate": True')
                promoted += 1
                print(f"  Line {i + 1}: research_candidate -> production_candidate")

content = "\n".join(lines)
fpath.write_text(content, encoding="utf-8")
print(f"\nPromoted {promoted} fields across BTC bots")
