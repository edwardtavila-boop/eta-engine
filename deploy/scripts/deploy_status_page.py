"""
Deploy // deploy_status_page
============================
One-shot deploy of the Evolutionary Trading Algo status page to Cloudflare Pages.

Uses the Cloudflare API directly (no wrangler CLI needed). Creates the
project if missing, uploads the HTML bundle via direct upload API, and
creates a CNAME for status.evolutionarytradingalgo.live.

Runs locally (not on the VPS) since it needs the CF API token + the
HTML source file from this repo.

Usage:
  export CF_API_TOKEN=...
  python -m deploy.scripts.deploy_status_page \
    --project apex-status \
    --zone evolutionarytradingalgo.live \
    --hostname status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

try:
    import httpx
except ImportError:
    print("FATAL: httpx required -- pip install httpx")
    sys.exit(2)


CF_API = "https://api.cloudflare.com/client/v4"


def die(msg: str) -> None:
    print(f"[FATAL] {msg}", file=sys.stderr)
    sys.exit(1)


def api(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=CF_API,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=60.0,
    )


def get_account_id(client: httpx.Client, zone: str) -> tuple[str, str]:
    """Resolve account_id + zone_id by zone name."""
    r = client.get(f"/zones?name={zone}")
    j = r.json()
    if not j.get("success") or not j.get("result"):
        die(f"zone {zone} not found: {json.dumps(j)[:200]}")
    z = j["result"][0]
    return z["account"]["id"], z["id"]


def ensure_project(client: httpx.Client, account_id: str, project: str, production_branch: str = "main") -> dict:
    """Create the Pages project if missing. Returns the project record."""
    # Check existing
    r = client.get(f"/accounts/{account_id}/pages/projects/{project}")
    if r.status_code == 200:
        print(f"[OK] project {project} already exists")
        return r.json()["result"]
    # Create
    body = {
        "name": project,
        "production_branch": production_branch,
    }
    r = client.post(f"/accounts/{account_id}/pages/projects", json=body)
    j = r.json()
    if not j.get("success"):
        die(f"create project failed: {json.dumps(j)[:300]}")
    print(f"[OK] created project {project}")
    return j["result"]


def direct_upload(client: httpx.Client, account_id: str, project: str, source_dir: Path, branch: str = "main") -> dict:
    """Upload the static files via direct-upload API.

    The CF direct-upload flow:
      1. POST /.../pages/projects/{name}/upload-token  -> get JWT
      2. Send files to upload endpoint with the JWT
      3. POST /.../pages/projects/{name}/deployments to register deployment

    For a tiny single-page deploy we use the simpler deployment-from-upload
    flow via the 'form' endpoint. Creates a deployment directly from a
    tar.gz of the directory.
    """
    if not source_dir.is_dir():
        die(f"source_dir {source_dir} is not a directory")
    files = [p for p in source_dir.rglob("*") if p.is_file()]
    if not files:
        die("no files to upload")

    print(f"[OK] found {len(files)} file(s) in {source_dir}")

    # Build a tar.gz archive with files at the TOP level (no wrapping dir)
    with tempfile.NamedTemporaryFile(
        suffix=".tar.gz",
        delete=False,
    ) as tmp:
        tar_path = Path(tmp.name)
    try:
        with tarfile.open(tar_path, "w:gz") as tar:
            for f in files:
                tar.add(f, arcname=f.relative_to(source_dir))

        # Upload via multipart form
        with tar_path.open("rb") as fp:
            files_arg = {"file": ("bundle.tar.gz", fp, "application/gzip")}
            data = {"branch": branch}
            r = httpx.post(
                f"{CF_API}/accounts/{account_id}/pages/projects/{project}/deployments",
                headers={"Authorization": client.headers["Authorization"]},
                files=files_arg,
                data=data,
                timeout=120.0,
            )
        j = r.json()
        if not j.get("success"):
            die(f"deploy failed: {json.dumps(j)[:500]}")
        dep = j["result"]
        print(f"[OK] deployment id={dep.get('id')}")
        print(f"     url={dep.get('url')}")
        return dep
    finally:
        import contextlib

        with contextlib.suppress(OSError):
            tar_path.unlink()


def ensure_cname(client: httpx.Client, zone_id: str, hostname: str, target: str) -> dict:
    """Create or update CNAME."""
    # Check existing
    r = client.get(f"/zones/{zone_id}/dns_records?name={hostname}&type=CNAME")
    j = r.json()
    if j.get("result"):
        rec = j["result"][0]
        body = {"type": "CNAME", "name": hostname, "content": target, "proxied": True, "ttl": 1}
        r = client.put(f"/zones/{zone_id}/dns_records/{rec['id']}", json=body)
        if r.json().get("success"):
            print(f"[OK] updated CNAME {hostname} -> {target}")
            return r.json()["result"]
        die(f"CNAME update failed: {r.text[:300]}")
    body = {"type": "CNAME", "name": hostname, "content": target, "proxied": True, "ttl": 1}
    r = client.post(f"/zones/{zone_id}/dns_records", json=body)
    j = r.json()
    if not j.get("success"):
        die(f"CNAME create failed: {json.dumps(j)[:300]}")
    print(f"[OK] created CNAME {hostname} -> {target}")
    return j["result"]


def attach_custom_domain(client: httpx.Client, account_id: str, project: str, hostname: str) -> dict:
    """Attach hostname as a custom domain on the Pages project."""
    # Check existing
    r = client.get(f"/accounts/{account_id}/pages/projects/{project}/domains")
    existing = {d.get("name") for d in r.json().get("result", [])}
    if hostname in existing:
        print(f"[OK] custom domain {hostname} already attached")
        return {"name": hostname, "already": True}
    r = client.post(
        f"/accounts/{account_id}/pages/projects/{project}/domains",
        json={"name": hostname},
    )
    j = r.json()
    if not j.get("success"):
        # Not fatal -- Pages sometimes needs the DNS to propagate first
        print(f"[WARN] custom domain attach failed (will auto-retry later): {json.dumps(j)[:200]}")
        return {"name": hostname, "deferred": True}
    print(f"[OK] attached custom domain {hostname}")
    return j["result"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default="apex-status", help="Cloudflare Pages project name")
    ap.add_argument("--zone", required=True, help="CF zone, e.g. evolutionarytradingalgo.live")
    ap.add_argument("--hostname", default="status", help="hostname prefix (status -> status.evolutionarytradingalgo.live)")
    ap.add_argument("--source", default=str(Path(__file__).resolve().parent.parent / "status_page"))
    args = ap.parse_args(argv)

    token = os.environ.get("CF_API_TOKEN", "").strip()
    if not token:
        die("CF_API_TOKEN not set")

    source = Path(args.source)
    if not source.exists():
        die(f"source dir missing: {source}")

    fqdn = f"{args.hostname}.{args.zone}"

    with api(token) as client:
        account_id, zone_id = get_account_id(client, args.zone)
        print(f"[OK] zone={args.zone} account_id={account_id}")

        ensure_project(client, account_id, args.project)
        dep = direct_upload(client, account_id, args.project, source)

        # Pages gives us <hash>.{project}.pages.dev
        pages_url = dep.get("url") or f"https://{args.project}.pages.dev"
        pages_target = pages_url.replace("https://", "").rstrip("/")

        ensure_cname(client, zone_id, fqdn, pages_target)
        attach_custom_domain(client, account_id, args.project, fqdn)

        print()
        print("[OK] Status page deployed.")
        print(f"     URL:          https://{fqdn}")
        print(f"     Pages URL:    {pages_url}")
        print(f"     Account:      {account_id}")
        print(f"     Project:      {args.project}")
        print("     Propagation:  up to 5 minutes for CNAME + custom domain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
