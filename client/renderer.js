// EVOLUTIONARY TRADING ALGO  //  client/renderer.js
// =====================================
// Renderer-side logic. Runs in the sandboxed UI. Only API is `window.apex`
// exposed by preload.js.

const $ = (id) => document.getElementById(id);

let currentBundle = null;
let selectedSku = null;

async function bootstrap() {
  currentBundle = await window.apex.loadBundle();
  if (!currentBundle) {
    appendLog("no client bundle found -- contact support");
    return;
  }
  $("tenant-id").textContent = currentBundle.tenant_id;
  $("tier").textContent = currentBundle.tier || "-";
  $("disclaimer").textContent = currentBundle.disclaimer || "";

  // Bot list is derived from planned_containers in the tenant record; if
  // the bundle doesn't carry them we render a placeholder.
  const botsList = $("bots-list");
  botsList.innerHTML = "";
  const skus = currentBundle.planned_containers || [];
  if (skus.length === 0) {
    botsList.innerHTML = '<li class="empty">no bots provisioned yet</li>';
  } else {
    for (const s of skus) {
      const li = document.createElement("li");
      li.textContent = s.sku || s;
      li.dataset.sku = s.sku || s;
      li.addEventListener("click", () => selectSku(li.dataset.sku));
      botsList.appendChild(li);
    }
    selectSku(skus[0].sku || skus[0]);
  }

  await connect();
  wireButtons();
  wireWsEvents();
}

function selectSku(sku) {
  selectedSku = sku;
  document.querySelectorAll("#bots-list li").forEach((li) => {
    li.classList.toggle("active", li.dataset.sku === sku);
  });
}

async function connect() {
  const token = genSessionToken();
  const res = await window.apex.connect(token);
  setConnState(res.ok);
  if (!res.ok) appendLog("connect failed: " + res.error);
}

function genSessionToken() {
  // Client-generated random token; the server binds it to the tenant on HELLO.
  const arr = new Uint8Array(24);
  crypto.getRandomValues(arr);
  return Array.from(arr, (b) => b.toString(16).padStart(2, "0")).join("");
}

function setConnState(ok) {
  const el = $("conn-state");
  el.textContent = ok ? "ONLINE" : "OFFLINE";
  el.className = ok ? "online" : "offline";
}

function wireButtons() {
  $("btn-start").addEventListener("click", () => send("BOT_START", { sku: selectedSku, mode: "paper" }));
  $("btn-stop").addEventListener("click", () => send("BOT_STOP", { sku: selectedSku }));
  $("btn-reset").addEventListener("click", () => send("BOT_RESET", { sku: selectedSku, confirm: "true" }));
  $("btn-ask").addEventListener("click", () => {
    const q = $("jarvis-input").value.trim();
    if (q) send("QUERY_JARVIS", { question: q, sku: selectedSku });
  });
}

async function send(kind, params) {
  const res = await window.apex.send({
    kind,
    session_token: "bound-on-hello",
    tenant_id: currentBundle.tenant_id,
    params,
  });
  if (!res.ok) appendLog("send failed: " + res.error);
}

function wireWsEvents() {
  window.apex.onMessage((msg) => {
    appendLog(`[${msg.kind}] ${JSON.stringify(msg.payload)}`);
    switch (msg.kind) {
      case "STATUS":
        applyStatus(msg.payload);
        break;
      case "JARVIS_ANSWER":
        $("jarvis-answer").textContent = msg.payload.answer || "-";
        break;
      case "ERROR":
        $("jarvis-answer").textContent = `ERR ${msg.payload.code}: ${msg.payload.message}`;
        break;
    }
  });
  window.apex.onError((err) => appendLog("ws error: " + err));
  window.apex.onClosed(() => setConnState(false));
}

function applyStatus(p) {
  $("stat-equity").textContent = "$" + Number(p.equity).toLocaleString();
  $("stat-daily-pnl").textContent = "$" + Number(p.daily_pnl).toLocaleString();
  $("stat-regime").textContent = p.regime || "-";
  $("stat-session").textContent = p.session_phase || "-";
  $("stat-confidence").textContent = Number(p.confidence).toFixed(2);
  $("stat-kill").textContent = p.kill_switch_active ? "ACTIVE" : "clear";
  $("stat-kill").className = p.kill_switch_active ? "kill" : "";
}

function appendLog(line) {
  const pre = $("log");
  const ts = new Date().toISOString();
  pre.textContent = `[${ts}] ${line}\n` + pre.textContent.slice(0, 8_000);
}

bootstrap();
