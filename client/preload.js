// EVOLUTIONARY TRADING ALGO  //  client/preload.js
// ====================================
// Secure IPC bridge between renderer (sandboxed UI) and main process.
// The renderer gets a narrow, allowlisted API -- NO direct WS, NO fs, NO Node.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("apex", {
  // --- bundle ------------------------------------------------------------
  loadBundle: () => ipcRenderer.invoke("bundle:get"),

  // --- websocket ---------------------------------------------------------
  connect: (sessionToken) => ipcRenderer.invoke("ws:connect", { sessionToken }),
  send: (command) => ipcRenderer.invoke("ws:send", command),
  disconnect: () => ipcRenderer.invoke("ws:close"),

  // --- event streams -----------------------------------------------------
  onMessage: (handler) => {
    const listener = (_evt, msg) => handler(msg);
    ipcRenderer.on("ws:message", listener);
    return () => ipcRenderer.removeListener("ws:message", listener);
  },
  onError: (handler) => {
    const listener = (_evt, err) => handler(err);
    ipcRenderer.on("ws:error", listener);
    return () => ipcRenderer.removeListener("ws:error", listener);
  },
  onClosed: (handler) => {
    const listener = () => handler();
    ipcRenderer.on("ws:closed", listener);
    return () => ipcRenderer.removeListener("ws:closed", listener);
  },
});
