// EVOLUTIONARY TRADING ALGO  //  client/main.js
// ================================
// Electron main process for the downloadable desktop client.
//
// Design rules
// ------------
//  - Renderer is sandboxed + contextIsolated. NO Node access in the UI layer.
//  - All WS traffic flows through this main process via the preload bridge.
//  - The client ONLY speaks the documented ClientCommand kinds listed in
//    the tenant's client_bundle.json. Anything else is rejected locally so
//    it never even reaches the server.
//  - The client never contains strategy code. This file is a thin shell.

const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");
const WebSocket = require("ws");

const BUNDLE_PATH =
  process.env.APEX_CLIENT_BUNDLE ||
  path.join(app.getPath("userData"), "client_bundle.json");

let ws = null;
let win = null;
let currentBundle = null;
let statusTimer = null;

function loadBundle() {
  try {
    const raw = fs.readFileSync(BUNDLE_PATH, "utf8");
    const bundle = JSON.parse(raw);
    currentBundle = bundle;
    return bundle;
  } catch (err) {
    return null;
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1200,
    height: 800,
    backgroundColor: "#0b0e14",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.loadFile("index.html");
}

app.whenReady().then(() => {
  loadBundle();
  createWindow();
});

app.on("window-all-closed", () => {
  if (statusTimer) clearInterval(statusTimer);
  if (ws) ws.terminate();
  if (process.platform !== "darwin") app.quit();
});

// -- IPC bridge (exposed via preload) ---------------------------------------

ipcMain.handle("bundle:get", () => loadBundle());

ipcMain.handle("ws:connect", async (_evt, { sessionToken }) => {
  if (!currentBundle || !currentBundle.ws_url) {
    return { ok: false, error: "no client bundle" };
  }
  return new Promise((resolve) => {
    try {
      ws = new WebSocket(currentBundle.ws_url);
    } catch (err) {
      resolve({ ok: false, error: err.message });
      return;
    }
    ws.on("open", () => {
      const hello = {
        kind: "HELLO",
        session_token: sessionToken,
        tenant_id: currentBundle.tenant_id,
        params: { client_version: currentBundle.client_version_min, os: process.platform },
      };
      ws.send(JSON.stringify(hello));
      resolve({ ok: true });
    });
    ws.on("message", (raw) => {
      if (!win) return;
      try {
        const msg = JSON.parse(raw.toString());
        win.webContents.send("ws:message", msg);
      } catch (err) {
        win.webContents.send("ws:error", err.message);
      }
    });
    ws.on("error", (err) => resolve({ ok: false, error: err.message }));
    ws.on("close", () => {
      if (win) win.webContents.send("ws:closed");
    });
  });
});

ipcMain.handle("ws:send", async (_evt, command) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    return { ok: false, error: "ws not connected" };
  }
  // Command kinds the thin client is allowed to send.
  const allowed = new Set(currentBundle?.supported_client_commands || []);
  if (allowed.size > 0 && !allowed.has(command.kind)) {
    return { ok: false, error: `command kind '${command.kind}' not in bundle allowlist` };
  }
  ws.send(JSON.stringify(command));
  return { ok: true };
});

ipcMain.handle("ws:close", async () => {
  if (ws) ws.close();
  return { ok: true };
});
