# eta_engine/tests/test_dashboard_e2e.py
"""Playwright end-to-end tests for the dashboard.

Run with:
  pytest eta_engine/tests/test_dashboard_e2e.py -v
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest


@pytest.fixture(scope="module")
def dashboard_server(tmp_path_factory):
    """Spin up the dashboard on port 8521 (test port) for e2e tests."""
    from pathlib import Path

    state_dir = tmp_path_factory.mktemp("dashboard_state")
    users_path = state_dir / "auth" / "users.json"
    sessions_path = state_dir / "auth" / "sessions.json"

    # Seed an operator account
    from eta_engine.deploy.scripts.dashboard_auth import create_user
    create_user(users_path, "edward", "test-pass")

    # Resolve the project root (parent of the eta_engine package dir) so the
    # subprocess can import `eta_engine.*` regardless of pytest's cwd.
    repo_root = Path(__file__).resolve().parents[2]

    env = {
        "APEX_STATE_DIR": str(state_dir),
        "ETA_DASHBOARD_USERS_PATH": str(users_path),
        "ETA_DASHBOARD_SESSIONS_PATH": str(sessions_path),
        "ETA_DASHBOARD_STEP_UP_PIN": "1234",
        # Ensure subprocess can resolve the eta_engine package
        "PYTHONPATH": str(repo_root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    full_env = {**os.environ, **env}

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "eta_engine.deploy.scripts.dashboard_api:app",
         "--port", "8521", "--host", "127.0.0.1"],
        env=full_env,
        cwd=str(repo_root),
    )
    # Wait for it to start
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:8521/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.25)
    yield "http://127.0.0.1:8521"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.mark.asyncio
async def test_login_and_render_no_console_errors(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    errors: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

        await page.goto(dashboard_server)
        # Login modal should appear
        await page.wait_for_selector("#login-modal:not(.hidden)")
        await page.fill("#login-username", "edward")
        await page.fill("#login-password", "test-pass")
        await page.click("#login-form button[type=submit]")

        # Wait for at least one panel to be present
        await page.wait_for_selector("[data-panel-id]")
        await asyncio.sleep(2)  # let panels paint

        await browser.close()
    # Filter out known-benign errors (Tailwind CDN warnings, favicon 404, etc.)
    filtered = [e for e in errors if "tailwindcss.com" not in e and "favicon" not in e.lower()]
    assert filtered == [], f"console errors: {filtered}"


@pytest.mark.asyncio
async def test_every_panel_has_no_error_class(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(dashboard_server)
        await page.fill("#login-username", "edward")
        await page.fill("#login-password", "test-pass")
        await page.click("#login-form button[type=submit]")
        await page.wait_for_selector("[data-panel-id]")
        await asyncio.sleep(3)  # let panels paint + first refresh complete

        # Switch to fleet tab so its panels also paint
        await page.click('button[data-tab="fleet"]')
        await asyncio.sleep(2)

        panels = await page.query_selector_all("[data-panel-id]")
        assert len(panels) >= 21, f"expected at least 21 panels, got {len(panels)}"
        errored = []
        for panel in panels:
            cls = await panel.get_attribute("class") or ""
            if "error" in cls.split():
                pid = await panel.get_attribute("data-panel-id")
                errored.append(pid)
        # Cold-start endpoints SHOULD return _warning, not error.
        # Real errors mean the panel renderer threw.
        assert errored == [], f"panels errored on initial render: {errored}"
        await browser.close()


@pytest.mark.asyncio
async def test_lifecycle_button_prompts_step_up(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(dashboard_server)
        await page.fill("#login-username", "edward")
        await page.fill("#login-password", "test-pass")
        await page.click("#login-form button[type=submit]")
        await page.wait_for_selector("[data-panel-id]")
        await page.click('button[data-tab="fleet"]')
        await page.wait_for_selector('[data-panel-id="fl-controls"] button[data-act="kill"]')

        # The kill button raises TWO dialogs in sequence:
        #   1. ``confirm("KILL <id> -- are you sure?")``  -> accept
        #   2. ``prompt('Type "kill <id>" to confirm')``  -> accept WITH the
        #      exact phrase, otherwise the dashboard cancels the action with
        #      "phrase mismatch" before ever reaching the step-up modal.
        # The default selection.botId is "mnq" (see panels.js).
        def _handle_dialog(dialog) -> None:
            if dialog.type == "prompt":
                # Type the exact confirmation phrase the dashboard expects
                import asyncio as _asyncio
                _asyncio.create_task(dialog.accept("kill mnq"))
            else:
                import asyncio as _asyncio
                _asyncio.create_task(dialog.accept())

        page.on("dialog", _handle_dialog)

        await page.click('[data-panel-id="fl-controls"] button[data-act="kill"]')
        # Step-up modal should appear
        await page.wait_for_selector("#step-up-modal:not(.hidden)", timeout=5000)
        await browser.close()


@pytest.mark.asyncio
async def test_sse_status_dot_turns_green(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(dashboard_server)
        await page.fill("#login-username", "edward")
        await page.fill("#login-password", "test-pass")
        await page.click("#login-form button[type=submit]")
        # Wait for SSE to connect (status dot becomes green)
        await page.wait_for_selector("#top-sse-status .sse-connected", timeout=8000)
        await browser.close()


@pytest.mark.asyncio
async def test_unauthenticated_blocks_app_load(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(dashboard_server)
        # Login modal should be visible from the get-go
        modal = await page.wait_for_selector("#login-modal")
        cls = await modal.get_attribute("class") or ""
        assert "hidden" not in cls.split(), "login modal should be visible"
        await browser.close()
