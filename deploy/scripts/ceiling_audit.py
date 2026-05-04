"""ETA Basement-to-Ceiling Audit"""
import json,subprocess,sys,time,urllib.request,ssl as _ssl
from datetime import datetime,UTC
from pathlib import Path

R=Path(r"C:\EvolutionaryTradingAlgo")
S=R/"var"/"eta_engine"/"state"
E=R/"eta_engine"
ctx=_ssl._create_unverified_context()
P,W,F=0,0,0

def say(l,ok=None):
    global P,W,F
    if ok is True:print(f"  [PASS] {l}");P+=1
    elif ok is False:print(f"  [FAIL] {l}");F+=1
    else:print(f"  [WARN] {l}");W+=1

def api(p):
    try: return json.loads(urllib.request.urlopen(f"http://127.0.0.1:8000{p}",timeout=10))
    except: return None

def ibkr(p):
    try: return json.loads(urllib.request.urlopen(f"https://127.0.0.1:5000/v1/api{p}",context=ctx,timeout=10))
    except Exception as e: return {"error":str(e)}

def ps(cmd):
    try: return subprocess.check_output(f'powershell -c "{cmd}"',shell=True,text=True)
    except: return ""

# 1. INFRA
print("\n"+"="*60+"\n  1. INFRA\n"+"="*60)
pc=int(ps("(Get-Process python* -ea 0).Count")or"0");say(f"Python: {pc}",pc>=4)
jc=int(ps("(Get-Process java* -ea 0).Count")or"0");say(f"Java(IBKR): {jc}",jc>=1)
for pt,nm in[(5000,"IBKR"),(8000,"Dashboard")]:
    s=ps(f"netstat -ano|sls ':{pt} .*LISTENING'");say(f"Port {pt}({nm})",bool(s.strip()))
fg=round(float(ps("(Get-PSDrive C).Free/1GB")or"0"),1);say(f"Disk:{fg}GB",fg>5)
cf=int(ps("(Get-Process cloudflared* -ea 0).Count")or"0");say(f"Cloudflared:{cf}",cf>=1)

# 2. DATA
print("\n"+"="*60+"\n  2. DATA\n"+"="*60)
for d in[S, E/"state"]:
    if d.exists():
        fs=sorted(d.rglob("*.json"),key=lambda f:f.stat().st_mtime,reverse=True)
        if fs:
            a=(datetime.now()-datetime.fromtimestamp(fs[0].stat().st_mtime)).total_seconds()/60
            say(f"State newest:{fs[0].name} ({a:.0f}m)",a<15)
            break
else:say("No state files",False)

vp=E/"state"/"jarvis_intel"/"verdicts.jsonl"
if vp.exists():
    a=(datetime.now()-datetime.fromtimestamp(vp.stat().st_mtime)).total_seconds()/60
    say(f"Verdicts log: {a:.0f}m old",a<15)
else:say("Verdicts missing",False)

hp=S/"jarvis_live_health.json"
if hp.exists():
    try:
        d=json.loads(hp.read_text());h=d.get("health","?")
        say(f"JARVIS health: {h}",h in("GREEN","YELLOW"))
    except:say("JARVIS health parse error",False)
else:say("JARVIS health file missing",False)

# 3. SIGNALS
print("\n"+"="*60+"\n  3. SIGNALS & BOTS\n"+"="*60)
fleet=api("/api/bot-fleet")
if fleet:
    bots=fleet.get("bots",[])
    t=len(bots)
    active=sum(1 for b in bots if b.get("status")not in("idle","readiness_only"))
    running=sum(1 for b in bots if b.get("status")=="running")
    app=sum(1 for b in bots if b.get("last_jarvis_verdict")=="APPROVED")
    cond=sum(1 for b in bots if b.get("last_jarvis_verdict")=="CONDITIONAL")
    deny=sum(1 for b in bots if b.get("last_jarvis_verdict")=="DENIED")
    say(f"Bots:{t} Active:{active} Running:{running}",running>0)
    say(f"APPROVED:{app} COND:{cond} DENIED:{deny}",app+cond>0)
    pnl=sum(b.get("todays_pnl",0)for b in bots)
    say(f"Today PnL:${pnl:.0f}",pnl>-200)
    errs=[b.get("id","?")for b in bots if b.get("status")=="error"]
    ro=[b.get("id","?")for b in bots if b.get("status")=="readiness_only"]
    say(f"Error bots:{len(errs)}",len(errs)==0)
    if errs:print(f"         ERRORS: {errs}")
    if ro:print(f"         INACTIVE(by design): {ro}")
else:say("Fleet API unreachable",False)

# 4. GATES
print("\n"+"="*60+"\n  4. GATES & RISK\n"+"="*60)
risk=api("/api/risk_gates")
if risk:
    any_k=risk.get("any_latched")or risk.get("any_killed")
    say(f"Kill latch: {any_k}",not any_k)
lp=E/"state"/"safety"/"kill_switch_latch.json"
if lp.exists():
    try:
        d=json.loads(lp.read_text())
        say(f"Global kill:{d.get('flatten_all',False)}",not d.get("flatten_all"))
    except:say("Kill latch bad",False)
else:say("No kill latch",True)

# 5. EXECUTION
print("\n"+"="*60+"\n  5. IBKR EXECUTION\n"+"="*60)
auth=ibkr("/iserver/auth/status")
if isinstance(auth,dict)and"error"not in auth:
    say(f"Auth: auth={auth.get('authenticated')} conn={auth.get('connected')}",auth.get("authenticated"))
else:say("IBKR auth fail",False)
acct=ibkr("/portfolio/accounts")
if isinstance(acct,list)and acct:
    say(f"Account:{acct[0].get('accountId','?')}({acct[0].get('type','?')})",True)
pos=ibkr("/portfolio/DUQ319869/positions/0")
if isinstance(pos,list):say(f"Positions:{len(pos)}",True)

# 6. LLM
print("\n"+"="*60+"\n  6. LLM / DEEPSEEK\n"+"="*60)
ep=E/".env"
if ep.exists():
    c=ep.read_text()
    say(f"DeepSeek key: present","DEEPSEEK_API_KEY=sk-" in c)
ah=S/"avengers_heartbeat.json"
if ah.exists():
    try:
        d=json.loads(ah.read_text());qs=d.get("quota_state","?")
        say(f"Quota:{qs} (h{d.get('hourly_pct',0):.0%} d{d.get('daily_pct',0):.0%})",qs=="NORMAL")
    except:say("Quota parse error",False)

# 7. SAFETY
print("\n"+"="*60+"\n  7. SAFETY & DRAWDOWN\n"+"="*60)
if fleet:
    mdd=max((b.get("max_dd",0)or 0)for b in bots)
    say(f"Max DD:${mdd:.0f}",mdd<500)

# 8. EFFICIENCY
print("\n"+"="*60+"\n  8. EFFICIENCY\n"+"="*60)
dup=set()
for tn in["jarvis_live","avengers_daemon"]:
    c=int(ps(f"(gci Win32_Process -Filter 'Name=\"python.exe\"'|?{{$_.CommandLine -match '{tn}'}}).Count")or"0")
    if c>1:dup.add(f"{tn}x{c}")
if dup:say(f"Duplicates:{dup}",False)
else:say("No duplicate processes",True)

# 9. VERDICT QUALITY
print("\n"+"="*60+"\n  9. VERDICT QUALITY\n"+"="*60)
if vp.exists():
    try:
        with open(vp,"rb")as f:lines=f.readlines()[-200:]
        vs=[json.loads(l)for l in lines]
        av=[v for v in vs if v.get("base_verdict")=="APPROVED"]
        cv=[v for v in vs if v.get("base_verdict")=="CONDITIONAL"]
        dv=[v for v in vs if v.get("base_verdict")=="DENIED"]
        say(f"Last 200: {len(av)}APP/{len(cv)}COND/{len(dv)}DEN",len(dv)<len(av)*2)
        confs=[v.get("confidence",0)for v in av+cv]
        ac=sum(confs)/len(confs)if confs else 0
        say(f"Avg confidence:{ac:.2f}",ac>0.4)
        subs={}
        for v in av+cv:
            s=v.get("subsystem","?");subs[s]=subs.get(s,0)+1
        if subs:
            print("         By asset class:")
            for s,cnt in sorted(subs.items(),key=lambda x:-x[1]):print(f"           {s:<20s} {cnt:>3d}")
    except Exception as e:say(f"Verdict analysis err:{e}",False)

# 10. DASHBOARD
print("\n"+"="*60+"\n  10. DASHBOARD HEALTH\n"+"="*60)
h=api("/health")
if h:say(f"API status:{h.get('status','?')}",h.get("status")=="ok")
else:say("Health endpoint fail",False)

# SUMMARY
print("\n"+"="*60+f"\n  PASS={P}  WARN={W}  FAIL={F}\n"+"="*60)
if F==0 and W<=3:print("  VERDICT: SYSTEM HEALTHY")
elif F<=2:print("  VERDICT: MINOR ISSUES")
else:print("  VERDICT: ATTENTION REQUIRED")
