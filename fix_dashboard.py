"""ETA Dashboard Fix — branding + restart."""
import os, sys, subprocess, time
from pathlib import Path

ROOT = Path(r"C:\EvolutionaryTradingAlgo")
print("=== ETA Dashboard Fix ===")

# 1. Fix branding in all HTML files
html_files = [f for f in ROOT.rglob("*.html")
              if "node_modules" not in str(f) and ".venv" not in str(f)]
print(f"Found {len(html_files)} HTML files")

branding = {"Batman": "Reasoner", "Robin": "Executor", "Alfred": "Steward"}
fixed = 0
for f in html_files:
    try:
        content = f.read_text(encoding="utf-8", errors="ignore")
        new = content
        for old, new_name in branding.items():
            new = new.replace(old, new_name)
        if "Apex" in new:
            new = new.replace("Apex", "ETA")
        if new != content:
            f.write_text(new, encoding="utf-8")
            fixed += 1
    except Exception:
        pass
print(f"Fixed branding in {fixed} files")

# 2. Kill existing Python
subprocess.run("taskkill /f /im python.exe 2>nul", shell=True)
time.sleep(2)
print("Killed old Python processes")

# 3. Find Python executable
python_path = None
for p in [
    ROOT / "firm_command_center" / "eta_engine" / ".venv" / "Scripts" / "python.exe",
    ROOT / "firm" / "eta_engine" / ".venv" / "Scripts" / "python.exe",
    ROOT / "eta_engine" / ".venv" / "Scripts" / "python.exe",
]:
    if p.exists():
        python_path = p
        break
if not python_path:
    # Use system Python
    import sys
    python_path = Path(sys.executable)
print(f"Using Python: {python_path}")

# 4. Find command_center
cc_root = None
for p in [
    ROOT / "firm" / "eta_engine",
    ROOT / "eta_engine",
]:
    if (p / "command_center" / "server" / "app.py").exists():
        cc_root = p
        break
print(f"Command center root: {cc_root}")

# 5. Start dashboard
os.chdir(str(cc_root))
env = os.environ.copy()
env["PYTHONPATH"] = str(cc_root) + ";" + str(ROOT)

proc = subprocess.Popen(
    [str(python_path), "-m", "uvicorn", "command_center.server.app:app",
     "--host", "127.0.0.1", "--port", "8000"],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(f"Dashboard PID: {proc.pid}")

# 6. Verify
time.sleep(5)
try:
    import urllib.request
    r = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5)
    print(f"Health: HTTP {r.status}")
except Exception as e:
    print(f"Health: {e}")

# 7. Update scheduled task
try:
    subprocess.run([
        "schtasks", "/create", "/tn", "ETA-Dashboard", "/tr",
        f'"{python_path}" -m uvicorn command_center.server.app:app --host 127.0.0.1 --port 8000',
        "/sc", "ONLOGON", "/ru", "SYSTEM", "/f", "/rl", "HIGHEST",
    ], capture_output=True)
    subprocess.run(["schtasks", "/run", "/tn", "ETA-Dashboard"], capture_output=True)
    print("ETA-Dashboard task updated")
except Exception as e:
    print(f"Task: {e}")

print("=== DONE === Refresh: https://ops.evolutionarytradingalgo.com")
