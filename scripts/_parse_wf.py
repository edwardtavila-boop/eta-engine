"""Quick parser for the walk-forward JSON files written to /tmp."""
import glob
import json
import os

files = sorted(glob.glob(r'C:\Users\edwar\AppData\Local\Temp\wf_*.json'))
print('Bot                     IS-T  IS-WR%   IS-PnL    OOS-T  OOS-WR% OOS-PnL    Decay')
print('-' * 88)
for f in files:
    bot = os.path.basename(f).replace('wf_', '').replace('.json', '')
    if os.path.getsize(f) == 0:
        print(f'{bot:<22} EMPTY')
        continue
    try:
        with open(f) as fp:
            d = json.load(fp)
    except json.JSONDecodeError as e:
        print(f'{bot:<22} JSON ERROR: {e}')
        continue
    isb = d.get('in_sample', d)
    oosb = d.get('out_of_sample', {})
    ist = isb.get('trades', 0)
    iswr = isb.get('win_rate', 0)
    isp = isb.get('total_pnl', 0)
    oost = oosb.get('trades', 0)
    ooswr = oosb.get('win_rate', 0)
    oosp = oosb.get('total_pnl', 0)
    decay = ((oosp - isp) / abs(isp) * 100) if abs(isp) > 0.01 else 0
    print(f'{bot:<22} {ist:>4}  {iswr:>5.1f}  ${isp:>+7.0f}    {oost:>4}  {ooswr:>5.1f}  ${oosp:>+7.0f}  {decay:>+6.0f}%')
