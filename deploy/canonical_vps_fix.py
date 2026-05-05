"""
Canonical VPS fix — single source of truth at C:\EvolutionaryTradingAlgo.
Fixes service XML paths, re-registers WinSW services, validates, starts everything.
"""
import os
import subprocess
import sys
from pathlib import Path

CANONICAL = Path(r"C:\EvolutionaryTradingAlgo")
OLD = Path(r"C:\TheFirm")

XML_SEARCH = [
    CANONICAL / "firm" / "eta_engine" / "deploy" / "windows",
    CANONICAL / "firm_command_center" / "services",
    CANONICAL / "firm_command_center",
]


def find_all_xmls() -> list[Path]:
    xmls = []
    for search_dir in XML_SEARCH:
        if search_dir.exists():
            for f in search_dir.rglob("*.xml"):
                xmls.append(f)
    return xmls


def fix_xml_paths(xml_path: Path) -> bool:
    try:
        content = xml_path.read_text(encoding="utf-8")
    except Exception:
        content = xml_path.read_text(encoding="latin-1")

    changed = False
    if str(OLD) in content and str(CANONICAL) not in content:
        content = content.replace(str(OLD), str(CANONICAL))
        changed = True
    # Also fix firm_command_center\eta_engine paths to canonical
    if r"firm_command_center\eta_engine" in content:
        content = content.replace(r"firm_command_center\eta_engine", r"firm\eta_engine")
        changed = True

    if changed:
        xml_path.write_text(content, encoding="utf-8")
        print(f"  FIXED: {xml_path.name}")
    return changed


def find_winsw() -> Path | None:
    candidates = [
        CANONICAL / "firm_command_center" / "services" / "winsw.exe",
        CANONICAL / "firm" / "eta_engine" / "deploy" / "windows" / "winsw.exe",
        CANONICAL / "firm_command_center" / "winsw.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def reinstall_service(winsw: Path, xml_path: Path) -> bool:
    name = xml_path.stem
    svc_dir = xml_path.parent / name
    svc_dir.mkdir(parents=True, exist_ok=True)

    # Copy xml and winsw
    import shutil
    dest_xml = svc_dir / xml_path.name
    shutil.copy2(str(xml_path), str(dest_xml))
    dest_exe = svc_dir / "winsw.exe"
    shutil.copy2(str(winsw), str(dest_exe))

    # Stop, uninstall, install, start
    for action, args in [
        ("stop", []),
        ("uninstall", []),
        ("install", [str(dest_xml)]),
        ("start", []),
    ]:
        try:
            result = subprocess.run(
                [str(dest_exe), action] + args,
                capture_output=True, text=True, timeout=30,
                cwd=str(svc_dir),
            )
            if result.returncode != 0 and action != "stop":
                print(f"  {name} {action}: {result.stderr[:100]}")
        except Exception as e:
            print(f"  {name} {action}: {e}")

    print(f"  {name}: REINSTALLED")
    return True


def main():
    print("=" * 60)
    print("  CANONICAL VPS FIX — Source of truth: C:\\EvolutionaryTradingAlgo")
    print("=" * 60)

    # Step 1: Find and fix all XMLs
    print("\n--- Step 1: Fix XML paths ---")
    xmls = find_all_xmls()
    print(f"  Found {len(xmls)} XML files")
    fixed = 0
    for xml in xmls:
        if fix_xml_paths(xml):
            fixed += 1
    print(f"  Total fixed: {fixed}")

    # Step 2: Find WinSW
    print("\n--- Step 2: Find WinSW ---")
    winsw = find_winsw()
    if not winsw:
        print("  ERROR: winsw.exe not found")
        return 1
    print(f"  WinSW: {winsw}")

    # Step 3: Reinstall all services from fixed XMLs
    print("\n--- Step 3: Reinstall services ---")
    service_xmls = [x for x in xmls if x.suffix == ".xml" and "Caddyfile" not in str(x)]
    for xml in sorted(service_xmls, key=lambda x: x.name):
        reinstall_service(winsw, xml)

    # Step 4: Run health check
    print("\n--- Step 4: Health check ---")
    python_exe = CANONICAL / "eta_engine" / ".venv" / "Scripts" / "python.exe"
    if python_exe.exists():
        result = subprocess.run(
            [str(python_exe), str(CANONICAL / "eta_engine" / "scripts" / "health_check.py")],
            capture_output=True, text=True, timeout=30,
            cwd=str(CANONICAL),
        )
        print(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)

    # Step 5: List all services
    print("\n--- Step 5: Service status ---")
    result = subprocess.run(
        ["sc", "query"], capture_output=True, text=True, timeout=15,
    )
    for line in result.stdout.splitlines():
        if any(s in line for s in ["Firm", "Hermes", "ETA", "SERVICE_NAME", "STATE"]):
            print(line.strip())

    print("\n" + "=" * 60)
    print("  DONE — C:\\EvolutionaryTradingAlgo is now canonical")
    print("=" * 60)


if __name__ == "__main__":
    sys.exit(main() or 0)
