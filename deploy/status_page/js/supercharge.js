// eta_engine/deploy/status_page/js/supercharge.js
// V6 operator power tools (layout memory, macros, timeline) + UX memory.

import { authedPost } from '/js/auth.js';
import { liveStream, poller } from '/js/live.js';

const THEME_KEY = 'eta.command_center.theme_mode';
const PIN_KEY = 'eta.command_center.pinned_panels';
const LAYOUT_KEY = 'eta.command_center.layout_v1';
const HIDDEN_SURFACES_KEY = 'eta.command_center.hidden_surfaces_v1';
const COLLAPSED_STACKS_KEY = 'eta.command_center.collapsed_stacks_v1';
const MINIMAL_MODE_KEY = 'eta.command_center.minimal_mode';
const SESSION_LOG_MAX = 400;
const MACRO_COOLDOWN_MS = 20_000;
const LIVE_CARD_WATCHDOG_GRACE_MS = 12_000;
let lastDangerMacroAt = 0;
const sessionLog = [];

function pushSessionLog(kind, detail) {
  sessionLog.unshift({
    ts: new Date().toISOString(),
    kind,
    detail,
  });
  if (sessionLog.length > SESSION_LOG_MAX) sessionLog.length = SESSION_LOG_MAX;
}

function applyTheme(theme) {
  document.body.classList.remove('theme-neon', 'theme-stealth', 'theme-institutional');
  document.body.classList.add(`theme-${theme}`);
  const btn = document.getElementById('top-theme-toggle');
  if (btn) btn.textContent = `theme: ${theme}`;
}

function initThemeToggle() {
  const saved = localStorage.getItem(THEME_KEY) || 'neon';
  applyTheme(saved);
  const btn = document.getElementById('top-theme-toggle');
  const themes = ['neon', 'stealth', 'institutional'];
  btn?.addEventListener('click', () => {
    const current = localStorage.getItem(THEME_KEY) || 'neon';
    const idx = themes.indexOf(current);
    const next = themes[(idx + 1) % themes.length];
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  });
}

function initExport() {
  const btn = document.getElementById('top-export-btn');
  btn?.addEventListener('click', () => {
    const panels = [...document.querySelectorAll('[data-panel-id]')].map((el) => ({
      panelId: el.getAttribute('data-panel-id'),
      hidden: el.classList.contains('surface-hidden') || el.classList.contains('hidden'),
      stale: el.classList.contains('stale'),
      error: el.classList.contains('error'),
      lastRefreshAt: Number(el.dataset.lastRefreshAt || 0) || null,
    }));
    const payload = {
      generatedAt: new Date().toISOString(),
      selection: {
        tab: document.querySelector('.tab-btn[aria-selected="true"]')?.getAttribute('data-tab') || 'jarvis',
      },
      topBar: {
        sse: document.getElementById('top-sse-status')?.textContent?.trim() || '',
        freshness: document.getElementById('top-data-freshness')?.textContent?.trim() || '',
        autopilot: document.getElementById('top-autopilot')?.textContent?.trim() || '',
      },
      panels,
      sessionLog: sessionLog.slice(0, 220),
      activeAlerts: document.getElementById('alert-dock-body')?.textContent?.trim() || '',
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `eta-operator-snapshot-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  });
}

function initPinPanels() {
  const bind = () => {
    const saved = new Set(JSON.parse(localStorage.getItem(PIN_KEY) || '[]'));
    document.querySelectorAll('[data-panel-id]').forEach((el) => {
      const id = el.getAttribute('data-panel-id');
      if (!id) return;
      if (saved.has(id)) el.classList.add('pinned');
      const title = el.querySelector('.panel-title');
      if (!title || title.dataset.pinBound === '1') return;
      title.dataset.pinBound = '1';
      title.addEventListener('dblclick', () => {
        el.classList.toggle('pinned');
        const next = new Set(JSON.parse(localStorage.getItem(PIN_KEY) || '[]'));
        if (el.classList.contains('pinned')) next.add(id);
        else next.delete(id);
        localStorage.setItem(PIN_KEY, JSON.stringify(Array.from(next)));
      });
    });
  };
  bind();
  setTimeout(bind, 1200);
}

function saveLayout() {
  const payload = {};
  document.querySelectorAll('section[id^="view-"]').forEach((section) => {
    const entries = [];
    section.querySelectorAll('[data-panel-id]').forEach((panel) => {
      const id = panel.getAttribute('data-panel-id');
      const parent = panel.parentElement;
      if (!id || !parent) return;
      const parentId = parent.id || parent.className || 'root';
      entries.push({ id, parentId });
    });
    payload[section.id] = entries;
  });
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(payload));
}

function initDraggableLayouts() {
  document.querySelectorAll('[data-panel-id]').forEach((panel) => {
    panel.setAttribute('draggable', 'true');
    panel.addEventListener('dragstart', (e) => {
      panel.classList.add('dragging');
      e.dataTransfer?.setData('text/panel-id', panel.getAttribute('data-panel-id') || '');
    });
    panel.addEventListener('dragend', () => {
      panel.classList.remove('dragging');
      saveLayout();
    });
  });
  document.querySelectorAll('section[id^="view-"], section[id^="view-"] > div').forEach((container) => {
    container.addEventListener('dragover', (e) => {
      e.preventDefault();
      const dragging = document.querySelector('.dragging');
      if (!dragging || !container.contains(dragging.parentElement || null)) return;
      const candidates = [...container.querySelectorAll(':scope > [data-panel-id]:not(.dragging)')];
      const next = candidates.find((el) => {
        const rect = el.getBoundingClientRect();
        return e.clientY < rect.top + rect.height / 2;
      });
      if (next) container.insertBefore(dragging, next);
      else container.appendChild(dragging);
    });
    container.addEventListener('drop', (e) => {
      e.preventDefault();
      saveLayout();
    });
  });
}

function restoreLayout() {
  const raw = localStorage.getItem(LAYOUT_KEY);
  if (!raw) return;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return;
  }
  Object.entries(parsed || {}).forEach(([sectionId, entries]) => {
    if (!Array.isArray(entries)) return;
    const section = document.getElementById(sectionId);
    if (!section) return;
    entries.forEach((entry) => {
      const panel = section.querySelector(`[data-panel-id="${entry.id}"]`);
      if (!panel) return;
      const targetParent = [...section.querySelectorAll(':scope > div, :scope')]
        .find((p) => (p.id || p.className || 'root') === entry.parentId);
      if (targetParent) targetParent.appendChild(panel);
    });
  });
}

async function runMacro(action, opts = {}) {
  if (action === 'flatten' || action === 'kill') {
    const phrase = `${action} all`;
    const typed = (prompt(`Type "${phrase}" to confirm`) || '').trim().toLowerCase();
    if (typed !== phrase) return;
    const now = Date.now();
    if (now - lastDangerMacroAt < MACRO_COOLDOWN_MS) return;
    lastDangerMacroAt = now;
  }
  const rosterResp = await fetch('/api/bot-fleet', { credentials: 'same-origin', cache: 'no-store' });
  if (!rosterResp.ok) return;
  const roster = await rosterResp.json();
  const bots = Array.isArray(roster.bots) ? roster.bots : [];
  if (!bots.length) return;
  for (const bot of bots) {
    const id = bot.name;
    if (!id) continue;
    try {
      await authedPost(`/api/bot/${id}/${action}`, {}, opts);
      pushSessionLog('macro', `${action}:${id}`);
    } catch {
      // Continue best-effort for fleet macros.
    }
  }
}

function initMacros() {
  document.getElementById('macro-pause-all')?.addEventListener('click', () => runMacro('pause'));
  document.getElementById('macro-resume-all')?.addEventListener('click', () => runMacro('resume'));
  document.getElementById('macro-flatten-all')?.addEventListener('click', () => runMacro('flatten', { stepUpReason: 'Flatten all bots requires step-up.' }));
  document.getElementById('macro-kill-all')?.addEventListener('click', () => runMacro('kill', { stepUpReason: 'Kill all bots requires step-up.' }));
}

function initSeverityTimeline() {
  const host = document.getElementById('alert-severity-timeline');
  if (!host) return;
  const history = Array(24).fill('none');
  const draw = () => {
    host.innerHTML = history.map((level) => `<div class="severity-cell ${level === 'none' ? '' : level}"></div>`).join('');
  };
  const push = (level) => {
    history.shift();
    history.push(level);
    draw();
  };
  draw();
  window.addEventListener('eta-alerts-updated', (e) => {
    const items = e.detail?.items || [];
    let level = 'none';
    if (items.some((x) => x.severity === 'high')) level = 'high';
    else if (items.some((x) => x.severity === 'medium')) level = 'medium';
    else if (items.some((x) => x.severity === 'low')) level = 'low';
    push(level);
  });
  setInterval(() => push(history[history.length - 1] || 'none'), 30_000);
}

function initCommandPalette() {
  const modal = document.getElementById('command-palette-modal');
  const input = document.getElementById('command-palette-input');
  const results = document.getElementById('command-palette-results');
  const openBtn = document.getElementById('top-command-palette-btn');

  const commands = [
    { name: 'Toggle compact mode', run: () => document.getElementById('top-compact-toggle')?.click() },
    { name: 'Toggle minimal mode', run: () => document.getElementById('top-minimal-toggle')?.click() },
    { name: 'Cycle theme', run: () => document.getElementById('top-theme-toggle')?.click() },
    { name: 'Export snapshot', run: () => document.getElementById('top-export-btn')?.click() },
    { name: 'Go to JARVIS tab', run: () => document.querySelector('[data-tab="jarvis"]')?.click() },
    { name: 'Go to Fleet tab', run: () => document.querySelector('[data-tab="fleet"]')?.click() },
    { name: 'Logout', run: () => document.getElementById('top-logout')?.click() },
  ];

  const close = () => {
    modal?.classList.add('hidden');
    modal?.classList.remove('flex');
  };
  const open = () => {
    if (!modal) return;
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    input?.focus();
    render('');
  };
  const render = (query) => {
    if (!results) return;
    const q = query.trim().toLowerCase();
    const list = commands.filter((c) => c.name.toLowerCase().includes(q));
    results.innerHTML = list.map((c, idx) => `<button data-cmd-idx="${idx}">${c.name}</button>`).join('') || '<div class="text-zinc-500 p-2">No commands</div>';
    results.querySelectorAll('button[data-cmd-idx]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const item = list[Number(btn.dataset.cmdIdx)];
        item?.run();
        close();
      });
    });
  };

  openBtn?.addEventListener('click', open);
  modal?.addEventListener('click', (e) => {
    if (e.target === modal) close();
  });
  input?.addEventListener('input', () => render(input.value));
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      open();
    }
    if (e.key === 'Escape') close();
  });
}

function ensureRestoreDock() {
  let dock = document.getElementById('hidden-surfaces-dock');
  if (dock) return dock;
  dock = document.createElement('div');
  dock.id = 'hidden-surfaces-dock';
  dock.className = 'hidden-surfaces-dock';
  document.body.appendChild(dock);
  return dock;
}

function initHideableFloatingSurfaces() {
  const selectors = ['#alert-dock', '#macro-tray', '#v6-rail'];
  const hidden = new Set(JSON.parse(localStorage.getItem(HIDDEN_SURFACES_KEY) || '[]'));
  const dock = ensureRestoreDock();

  const persist = () => {
    localStorage.setItem(HIDDEN_SURFACES_KEY, JSON.stringify(Array.from(hidden)));
  };

  const renderDock = () => {
    const ids = Array.from(hidden);
    if (!ids.length) {
      dock.innerHTML = '';
      dock.classList.add('hidden');
      return;
    }
    dock.classList.remove('hidden');
    dock.innerHTML = ids.map((id) => {
      const label = id.replace(/^#/, '').replaceAll('-', ' ');
      return `<button class="restore-surface-btn" data-restore-id="${id}">show ${label}</button>`;
    }).join('');
    dock.querySelectorAll('button[data-restore-id]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-restore-id');
        const el = document.querySelector(id);
        if (!el) return;
        el.classList.remove('surface-hidden');
        hidden.delete(id);
        persist();
        renderDock();
      });
    });
  };

  selectors.forEach((id) => {
    const el = document.querySelector(id);
    if (!el) return;
    const title = el.querySelector('.panel-title');
    if (title && !title.querySelector('[data-hide-surface]')) {
      const hideBtn = document.createElement('button');
      hideBtn.type = 'button';
      hideBtn.className = 'surface-hide-btn';
      hideBtn.setAttribute('data-hide-surface', id);
      hideBtn.textContent = 'hide';
      title.appendChild(hideBtn);
      hideBtn.addEventListener('click', () => {
        el.classList.add('surface-hidden');
        hidden.add(id);
        persist();
        renderDock();
      });
    }
    if (hidden.has(id)) el.classList.add('surface-hidden');
  });

  renderDock();
}

function initHideableStacks() {
  const collapsed = new Set(JSON.parse(localStorage.getItem(COLLAPSED_STACKS_KEY) || '[]'));
  const persist = () => {
    localStorage.setItem(COLLAPSED_STACKS_KEY, JSON.stringify(Array.from(collapsed)));
  };

  document.querySelectorAll('section[id^="view-"]').forEach((section) => {
    const sectionId = section.id;
    const candidates = [...section.children].filter((el) => el.matches('div') && !el.hasAttribute('data-panel-id'));
    candidates.forEach((stackEl, idx) => {
      if (stackEl.dataset.stackReady === '1') return;
      const stackId = `${sectionId}-stack-${idx + 1}`;
      stackEl.dataset.stackReady = '1';
      stackEl.dataset.stackId = stackId;
      stackEl.classList.add('stack-shell');

      const ctrl = document.createElement('div');
      ctrl.className = 'stack-controls';
      const label = document.createElement('span');
      label.className = 'stack-label';
      label.textContent = `stack ${idx + 1}`;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'stack-toggle-btn';
      btn.textContent = 'hide stack';
      ctrl.appendChild(label);
      ctrl.appendChild(btn);

      const body = document.createElement('div');
      body.className = 'stack-body';
      while (stackEl.firstChild) body.appendChild(stackEl.firstChild);
      stackEl.appendChild(ctrl);
      stackEl.appendChild(body);

      const setCollapsed = (isCollapsed) => {
        stackEl.classList.toggle('stack-collapsed', isCollapsed);
        btn.textContent = isCollapsed ? 'show stack' : 'hide stack';
        if (isCollapsed) collapsed.add(stackId);
        else collapsed.delete(stackId);
        persist();
      };
      btn.addEventListener('click', () => setCollapsed(!stackEl.classList.contains('stack-collapsed')));
      if (collapsed.has(stackId)) setCollapsed(true);
    });
  });
}

function initMinimalMode() {
  const btn = document.getElementById('top-minimal-toggle');
  const apply = (enabled) => {
    document.body.classList.toggle('minimal-mode', enabled);
    if (btn) btn.textContent = `minimal: ${enabled ? 'on' : 'off'}`;
  };
  const saved = localStorage.getItem(MINIMAL_MODE_KEY) === '1';
  apply(saved);
  btn?.addEventListener('click', () => {
    const next = !document.body.classList.contains('minimal-mode');
    localStorage.setItem(MINIMAL_MODE_KEY, next ? '1' : '0');
    apply(next);
  });
}

function initDataFreshnessTelemetry() {
  const chip = document.getElementById('top-data-freshness');
  if (!chip) return;
  const panelTouch = new Map();
  const PANEL_STALE_MS = 15_000;
  const PANEL_HARD_STALE_MS = 35_000;

  const classify = () => {
    const now = Date.now();
    let stale = 0;
    let hardStale = 0;
    panelTouch.forEach((ts) => {
      const age = now - ts;
      if (age > PANEL_HARD_STALE_MS) hardStale += 1;
      else if (age > PANEL_STALE_MS) stale += 1;
    });
    if (hardStale > 0) {
      chip.textContent = `data: degraded (${hardStale} hard stale)`;
      chip.classList.remove('text-emerald-300', 'text-amber-300');
      chip.classList.add('text-red-300');
      return;
    }
    if (stale > 0) {
      chip.textContent = `data: warming (${stale} stale)`;
      chip.classList.remove('text-emerald-300', 'text-red-300');
      chip.classList.add('text-amber-300');
      return;
    }
    chip.textContent = 'data: fresh';
    chip.classList.remove('text-amber-300', 'text-red-300');
    chip.classList.add('text-emerald-300');
  };

  window.addEventListener('eta-panel-refresh', (e) => {
    const id = e.detail?.panelId;
    const at = Number(e.detail?.at || Date.now());
    if (!id) return;
    panelTouch.set(id, at);
    pushSessionLog('panel_refresh', `${id}:${Number(e.detail?.latencyMs || 0)}ms`);
    classify();
  });

  window.addEventListener('eta-panel-error', () => classify());
  setInterval(classify, 2000);
}

function initCardHealthContract() {
  const chip = document.getElementById('top-card-health');
  if (!chip) return;
  const endpoint = chip.dataset.healthEndpoint || '/api/dashboard/card-health';
  let latestCards = [];
  let contractOk = false;
  let latestHealth = { dead_cards: [], stale_cards: [], total: 0, at: Date.now() };
  let inspector = null;
  const bootedAt = Date.now();

  const setChip = (label, health, title = '') => {
    chip.textContent = label;
    chip.dataset.health = health;
    chip.title = title;
  };

  const escapeText = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

  const ensureCardHealthInspector = () => {
    if (inspector) return inspector;
    inspector = document.createElement('aside');
    inspector.id = 'card-health-inspector';
    inspector.className = 'card-health-inspector hidden';
    inspector.innerHTML = `
      <div class="card-health-inspector-head">
        <span>Card Health Inspector</span>
        <div class="card-health-inspector-actions">
          <button type="button" class="card-health-retry" data-retry-card-health="1">Retry unhealthy</button>
          <button type="button" data-close-card-health="1">close</button>
        </div>
      </div>
      <div class="card-health-inspector-body" data-card-health-body></div>`;
    document.body.appendChild(inspector);
    inspector.querySelector('[data-close-card-health="1"]')?.addEventListener('click', () => {
      inspector?.classList.add('hidden');
    });
    inspector.addEventListener('click', (event) => {
      const target = event.target?.closest?.('[data-focus-card]');
      if (!target) return;
      focusCardHealthPanel(target.getAttribute('data-focus-card'));
    });
    inspector.querySelector('[data-retry-card-health="1"]')?.addEventListener('click', retryUnhealthyCards);
    return inspector;
  };

  const retryUnhealthyCards = () => {
    const unhealthy = [
      ...(Array.isArray(latestHealth.dead_cards) ? latestHealth.dead_cards : []),
      ...(Array.isArray(latestHealth.stale_cards) ? latestHealth.stale_cards : []),
    ];
    const ids = [...new Set(unhealthy.map((card) => String(card.id || '')).filter(Boolean))];
    if (!ids.length) {
      poller._tick?.();
      refresh();
      setChip('cards: refreshing all', 'ok', 'No unhealthy cards; refreshing the dashboard tick anyway.');
      return;
    }
    setChip(`cards: retrying ${ids.length}`, 'degraded', ids.join(', '));
    pushSessionLog('card_retry', ids.join(','));
    window.dispatchEvent(new CustomEvent('eta-card-retry', {
      detail: { at: Date.now(), ids },
    }));
    ids.forEach((id) => {
      const panel = poller.panels.find((candidate) => candidate.containerId === id);
      panel?.refresh?.().catch(() => undefined);
    });
    if (ids.includes('cc-verdict-stream')) {
      liveStream.connect();
    }
    setTimeout(classifyLiveCards, 900);
    setTimeout(refresh, 1200);
  };

  const focusCardHealthPanel = (panelId) => {
    const id = String(panelId || '');
    if (!id) return;
    const panel = document.querySelector(`[data-panel-id="${CSS.escape(id)}"]`);
    if (!panel) return;
    panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    panel.classList.remove('card-health-focus');
    void panel.offsetWidth;
    panel.classList.add('card-health-focus');
    setTimeout(() => panel.classList.remove('card-health-focus'), 2600);
  };

  const renderCardHealthInspector = () => {
    const el = ensureCardHealthInspector();
    const body = el.querySelector('[data-card-health-body]');
    if (!body) return;
    const dead = Array.isArray(latestHealth.dead_cards) ? latestHealth.dead_cards : [];
    const stale = Array.isArray(latestHealth.stale_cards) ? latestHealth.stale_cards : [];
    const rows = [
      ...dead.map((card) => ({ ...card, tone: 'dead' })),
      ...stale.map((card) => ({ ...card, tone: 'stale' })),
    ];
    if (!rows.length) {
      body.innerHTML = `
        <div class="card-health-ok">All ${Number(latestHealth.total || 0)} registered cards are live.</div>
        <div class="card-health-note">The watchdog is checking mounted panels, refresh age, registry drift, and render errors.</div>`;
      return;
    }
    body.innerHTML = rows.map((card) => `
      <button type="button" class="card-health-row ${escapeText(card.tone)}" data-focus-card="${escapeText(card.id || '')}">
        <span>${escapeText(card.id || 'unknown-card')}</span>
        <code>${escapeText(card.reason || card.status || card.tone)}</code>
      </button>`).join('');
  };

  const toggleCardHealthInspector = () => {
    const el = ensureCardHealthInspector();
    renderCardHealthInspector();
    el.classList.toggle('hidden');
  };

  chip.setAttribute('role', 'button');
  chip.setAttribute('tabindex', '0');
  chip.addEventListener('click', toggleCardHealthInspector);
  chip.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggleCardHealthInspector();
    }
  });

  const renderedPanelMap = () => {
    const rendered = new Map();
    document.querySelectorAll('[data-panel-id]').forEach((el) => {
      const id = String(el.getAttribute('data-panel-id') || '');
      if (id) rendered.set(id, el);
    });
    return rendered;
  };

  const publish = (dead_cards, stale_cards, totalCards) => {
    const dead = Array.isArray(dead_cards) ? dead_cards : [];
    const stale = Array.isArray(stale_cards) ? stale_cards : [];
    latestHealth = { dead_cards: dead, stale_cards: stale, total: totalCards, at: Date.now() };
    document.querySelectorAll('[data-panel-id]').forEach((el) => {
      el.classList.remove('card-health-dead', 'card-health-stale');
    });
    dead.forEach((card) => {
      const id = String(card.id || '');
      if (!id) return;
      document.querySelector(`[data-panel-id="${CSS.escape(id)}"]`)?.classList.add('card-health-dead');
    });
    stale.forEach((card) => {
      const id = String(card.id || '');
      if (!id) return;
      document.querySelector(`[data-panel-id="${CSS.escape(id)}"]`)?.classList.add('card-health-stale');
    });
    if (inspector && !inspector.classList.contains('hidden')) renderCardHealthInspector();
    window.dispatchEvent(new CustomEvent('eta-card-health', {
      detail: {
        at: Date.now(),
        total: totalCards,
        dead_cards: dead,
        stale_cards: stale,
      },
    }));
    if (dead.length > 0) {
      setChip(
        `cards: ${dead.length} dead`,
        'dead',
        dead.map((card) => `${card.id}:${card.reason || card.status || 'dead'}`).join(', '),
      );
      return;
    }
    if (stale.length > 0) {
      setChip(
        `cards: ${stale.length} stale`,
        'degraded',
        stale.map((card) => `${card.id}:${card.reason || card.status || 'stale'}`).join(', '),
      );
      return;
    }
    setChip(`cards: ${totalCards} live`, 'ok', `${totalCards} registered cards, 0 dead, 0 stale`);
  };

  const classifyLiveCards = () => {
    if (!contractOk || latestCards.length === 0) return;
    const rendered = renderedPanelMap();
    if (rendered.size === 0) {
      setChip(`cards: warming`, 'degraded', 'waiting for authenticated panels to mount');
      return;
    }
    const now = Date.now();
    const dead = [];
    const stale = [];
    latestCards.forEach((card) => {
      const id = String(card.id || '');
      if (!id || card.required === false) return;
      const el = rendered.get(id);
      if (!el) {
        dead.push({ id, reason: 'missing_dom_panel' });
        return;
      }
      if (el.classList.contains('error')) {
        dead.push({ id, reason: 'panel_error' });
        return;
      }
      if (el.classList.contains('stale')) {
        stale.push({ id, reason: 'panel_stale_class' });
        return;
      }
      if (card.source === 'endpoint') {
        const last = Number(el.dataset.lastRefreshAt || 0);
        if (!last && now - bootedAt > LIVE_CARD_WATCHDOG_GRACE_MS) {
          dead.push({ id, reason: 'never_refreshed' });
          return;
        }
        const staleAfterMs = Number(card.stale_after_s || 30) * 1000;
        if (last && now - last > staleAfterMs) {
          stale.push({ id, reason: 'refresh_age_exceeded' });
        }
      }
    });
    const registeredIds = new Set(latestCards.map((card) => String(card.id || '')).filter(Boolean));
    rendered.forEach((_el, id) => {
      if (!registeredIds.has(id)) dead.push({ id, reason: 'missing_registry_card' });
    });
    publish(dead, stale, latestCards.length);
  };

  const refresh = async () => {
    try {
      const resp = await fetch(`${endpoint}?_t=${Date.now()}`, {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (!resp.ok) {
        setChip(`cards: contract http ${resp.status}`, 'dead', endpoint);
        return;
      }
      const payload = await resp.json();
      const cards = Array.isArray(payload.cards) ? payload.cards : [];
      latestCards = cards;
      contractOk = true;
      publish(
        Array.isArray(payload.dead_cards) ? payload.dead_cards : [],
        Array.isArray(payload.stale_cards) ? payload.stale_cards : [],
        cards.length,
      );
      classifyLiveCards();
    } catch (err) {
      contractOk = false;
      setChip(`cards: contract down`, 'dead', String(err?.message || err));
    }
  };

  refresh();
  setInterval(classifyLiveCards, 2000);
  setInterval(refresh, 10_000);
  window.addEventListener('eta-panel-error', () => setTimeout(classifyLiveCards, 100));
  window.addEventListener('eta-panel-refresh', () => setTimeout(classifyLiveCards, 100));
}

function initCommandCenterDiagnostics() {
  const chip = document.getElementById('top-diagnostics');
  if (!chip) return;
  const endpoint = chip.dataset.diagnosticsEndpoint || '/api/dashboard/diagnostics';
  let latest = null;
  let inspector = null;

  const escapeText = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

  const setChip = (label, health, title = '') => {
    chip.textContent = label;
    chip.dataset.health = health;
    chip.title = title;
  };

  const ensureInspector = () => {
    if (inspector) return inspector;
    inspector = document.createElement('aside');
    inspector.id = 'diagnostics-inspector';
    inspector.className = 'diagnostics-inspector hidden';
    inspector.innerHTML = `
      <div class="diagnostics-inspector-head">
        <span>Command Center Diagnostics</span>
        <div class="diagnostics-inspector-actions">
          <button type="button" data-refresh-diagnostics="1">refresh</button>
          <button type="button" data-close-diagnostics="1">close</button>
        </div>
      </div>
      <div class="diagnostics-inspector-body" data-diagnostics-body></div>`;
    document.body.appendChild(inspector);
    inspector.querySelector('[data-close-diagnostics="1"]')?.addEventListener('click', () => {
      inspector?.classList.add('hidden');
    });
    inspector.querySelector('[data-refresh-diagnostics="1"]')?.addEventListener('click', refresh);
    return inspector;
  };

  const row = (label, value) => `
    <div class="diagnostics-row">
      <span>${escapeText(label)}</span>
      <code>${escapeText(value)}</code>
    </div>`;

  const render = () => {
    const el = ensureInspector();
    const body = el.querySelector('[data-diagnostics-body]');
    if (!body) return;
    if (!latest) {
      body.innerHTML = row('status', 'diagnostics loading');
      return;
    }
    const apiBuild = latest.api_build || {};
    const cards = latest.cards?.summary || {};
    const botFleet = latest.bot_fleet || {};
    const equity = latest.equity || {};
    const service = latest.service || {};
    const paths = latest.paths || {};
    body.innerHTML = [
      row('api_build', `${apiBuild.dashboard_version || '?'} ${apiBuild.release_stage || '?'} pid:${service.pid || apiBuild.pid || '?'}`),
      row('service', `${service.status || 'unknown'} uptime:${Math.round(Number(service.uptime_s || 0))}s`),
      row('cards', `${cards.total || 0} total / ${cards.dead || 0} dead / ${cards.stale || 0} stale`),
      row('bot_fleet', `${botFleet.confirmed_bots || 0}/${botFleet.bot_total || 0} confirmed - ${botFleet.truth_status || 'unknown'}`),
      row('equity', `${equity.source || 'unknown'} points:${equity.point_count || 0} age:${equity.source_age_s ?? 'n/a'}s`),
      row('state_dir', paths.state_dir || 'unknown'),
      row('generated', latest.generated_at || 'unknown'),
    ].join('');
  };

  const publish = (payload) => {
    latest = payload;
    const checks = payload.checks || {};
    const botFleet = payload.bot_fleet || {};
    const equity = payload.equity || {};
    const ok = checks.api_contract && checks.card_contract && checks.bot_fleet_contract && checks.equity_contract;
    const botTotal = Number(botFleet.bot_total || 0);
    const confirmed = Number(botFleet.confirmed_bots || 0);
    const label = ok
      ? `diagnostics: live ${confirmed}/${botTotal}`
      : 'diagnostics: degraded';
    setChip(label, ok ? 'ok' : 'degraded', `${botFleet.truth_status || 'unknown'} / ${equity.source || 'unknown'}`);
    render();
    window.dispatchEvent(new CustomEvent('eta-command-center-diagnostics', {
      detail: {
        at: Date.now(),
        api_build: payload.api_build,
        bot_fleet: botFleet,
        equity,
        checks,
      },
    }));
  };

  async function refresh() {
    try {
      const resp = await fetch(`${endpoint}?_t=${Date.now()}`, {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (!resp.ok) {
        setChip(`diagnostics: http ${resp.status}`, 'dead', 'The running service is stale or diagnostics is unavailable.');
        return;
      }
      publish(await resp.json());
    } catch (err) {
      setChip('diagnostics: down', 'dead', String(err?.message || err));
    }
  }

  chip.setAttribute('role', 'button');
  chip.setAttribute('tabindex', '0');
  chip.addEventListener('click', () => {
    const el = ensureInspector();
    render();
    el.classList.toggle('hidden');
  });
  chip.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      chip.click();
    }
  });
  refresh();
  setInterval(refresh, 15_000);
}

function initConsistencyGuardrails() {
  let previousFillTs = 0;
  setInterval(async () => {
    try {
      const [fillsResp, rosterResp] = await Promise.all([
        fetch(`/api/live/fills?limit=30&_t=${Date.now()}`, { credentials: 'same-origin', cache: 'no-store' }),
        fetch('/api/bot-fleet', { credentials: 'same-origin', cache: 'no-store' }),
      ]);
      if (!fillsResp.ok || !rosterResp.ok) return;
      const fillsBody = await fillsResp.json();
      const rosterBody = await rosterResp.json();
      const fills = Array.isArray(fillsBody.fills) ? fillsBody.fills : [];
      const bots = new Set((Array.isArray(rosterBody.bots) ? rosterBody.bots : []).map((b) => String(b.name)));
      const latestTs = fills.length ? new Date(fills[0].ts || 0).getTime() : 0;
      const unknownBots = fills.filter((f) => !bots.has(String(f.bot || ''))).length;
      if (latestTs && previousFillTs && latestTs < previousFillTs) {
        window.dispatchEvent(new CustomEvent('eta-remediation', { detail: { reason: 'fill timestamp rollback' } }));
      }
      if (unknownBots > 0) {
        window.dispatchEvent(new CustomEvent('eta-remediation', { detail: { reason: `unknown fill bots:${unknownBots}` } }));
      }
      previousFillTs = Math.max(previousFillTs, latestTs || 0);
      pushSessionLog('consistency', `fills:${fills.length} unknown:${unknownBots}`);
    } catch {
      // ignore transient consistency check failures
    }
  }, 9000);
}

function initLatencyBudgetGuardrails() {
  const budgets = {
    '/api/live/': 800,
    '/api/fleet-equity': 1300,
    '/api/bot-fleet': 1100,
    '/api/jarvis': 1500,
    default: 1200,
  };
  const counters = {};
  window.addEventListener('eta-panel-refresh', (e) => {
    const endpoint = String(e.detail?.endpoint || '');
    const latency = Number(e.detail?.latencyMs || 0);
    const budget = endpoint.includes('/api/live/') ? budgets['/api/live/']
      : endpoint.includes('/api/fleet-equity') ? budgets['/api/fleet-equity']
      : endpoint.includes('/api/bot-fleet') ? budgets['/api/bot-fleet']
      : endpoint.includes('/api/jarvis') ? budgets['/api/jarvis']
      : budgets.default;
    const key = endpoint || 'unknown';
    if (!counters[key]) counters[key] = 0;
    if (latency > budget) {
      counters[key] += 1;
      pushSessionLog('latency_breach', `${key}:${latency}>${budget}`);
      if (counters[key] >= 3) {
        window.dispatchEvent(new CustomEvent('eta-remediation', { detail: { reason: `latency breach ${key}` } }));
        counters[key] = 0;
      }
    } else {
      counters[key] = 0;
    }
  });
}

function initTradeRefreshFanout() {
  const REFRESH_IDS = new Set([
    'fl-roster',
    'fl-drilldown',
    'fl-equity-curve',
    'fl-fill-quality',
    'fl-performance-os',
    'fl-risk-sim',
    'fl-risk-ladder',
    'fl-health-badges',
  ]);
  let timer = null;
  const run = () => {
    timer = null;
    poller.panels
      .filter((p) => REFRESH_IDS.has(p.containerId))
      .forEach((p) => p.refresh().catch(() => undefined));
  };
  window.addEventListener('eta-trade-update', () => {
    if (timer) return;
    timer = setTimeout(run, 350);
  });
}

function initAutopilotWatchdog() {
  const chip = document.getElementById('top-autopilot');
  let lastHealAt = 0;
  const HEAL_COOLDOWN_MS = 10_000;
  const STREAM_STALE_MS = 25_000;

  const setChip = (label, tone) => {
    if (!chip) return;
    chip.textContent = label;
    chip.classList.remove('text-cyan-300', 'text-amber-300', 'text-red-300', 'text-emerald-300');
    chip.classList.add(tone);
  };

  const heal = (reason) => {
    const now = Date.now();
    if (now - lastHealAt < HEAL_COOLDOWN_MS) return;
    lastHealAt = now;
    setChip(`autopilot: healing (${reason})`, 'text-amber-300');
    try {
      liveStream.connect();
      poller._tick();
    } catch {
      setChip('autopilot: degraded', 'text-red-300');
    }
  };

  window.addEventListener('online', () => {
    setChip('autopilot: online', 'text-emerald-300');
    heal('network');
  });
  window.addEventListener('offline', () => setChip('autopilot: offline', 'text-red-300'));

  setInterval(() => {
    if (!navigator.onLine) {
      setChip('autopilot: offline', 'text-red-300');
      return;
    }
    const now = Date.now();
    if (!liveStream.connected) {
      heal('disconnect');
      return;
    }
    if (liveStream.lastEventAt && (now - liveStream.lastEventAt) > STREAM_STALE_MS) {
      heal('stale stream');
      return;
    }
    setChip('autopilot: armed', 'text-cyan-300');
  }, 4000);
}

function ensureIncidentTimeline() {
  let el = document.getElementById('incident-timeline');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'incident-timeline';
  el.className = 'incident-timeline hidden';
  el.innerHTML = '<div class="incident-title">Incident Timeline</div><div class="incident-body" id="incident-timeline-body"></div>';
  document.body.appendChild(el);
  return el;
}

function initIncidentTimelineMode() {
  const btn = document.getElementById('top-timeline-btn');
  const root = ensureIncidentTimeline();
  const body = root.querySelector('#incident-timeline-body');
  const events = [];
  const push = (line, tone = 'info') => {
    events.unshift({ ts: Date.now(), line, tone });
    if (events.length > 80) events.length = 80;
    if (!body) return;
    body.innerHTML = events.slice(0, 28).map((ev) => {
      const age = Math.max(0, Math.floor((Date.now() - ev.ts) / 1000));
      return `<div class="incident-row ${ev.tone}"><span>${age}s</span><span>${ev.line}</span></div>`;
    }).join('');
  };
  const toggle = () => root.classList.toggle('hidden');
  btn?.addEventListener('click', toggle);
  window.addEventListener('eta-trade-update', (e) => {
    const bot = e.detail?.fill?.bot || 'bot';
    const side = e.detail?.fill?.side || '?';
    push(`fill ${bot} ${String(side).toUpperCase()}`, 'ok');
  });
  window.addEventListener('eta-panel-error', (e) => {
    push(`panel error ${e.detail?.panelId || 'unknown'}`, 'bad');
  });
  window.addEventListener('eta-panel-refresh', (e) => {
    const p = e.detail?.panelId || 'panel';
    const ms = Number(e.detail?.latencyMs || 0);
    if (ms >= 1200) push(`slow refresh ${p} ${ms}ms`, 'warn');
  });
  window.addEventListener('eta-remediation', (e) => {
    push(`self-heal ${e.detail?.reason || 'action'}`, 'warn');
  });
}

function initWorkspacePresets() {
  const btn = document.getElementById('top-workspace-btn');
  const modes = ['balanced', 'execution', 'risk', 'research'];
  const apply = (mode) => {
    document.body.classList.remove('ws-balanced', 'ws-execution', 'ws-risk', 'ws-research');
    document.body.classList.add(`ws-${mode}`);
    if (btn) btn.textContent = `workspace: ${mode}`;
  };
  const saved = localStorage.getItem(WORKSPACE_KEY) || 'balanced';
  apply(saved);
  btn?.addEventListener('click', () => {
    const current = localStorage.getItem(WORKSPACE_KEY) || 'balanced';
    const next = modes[(modes.indexOf(current) + 1) % modes.length];
    localStorage.setItem(WORKSPACE_KEY, next);
    apply(next);
  });
}

function initPresentationMode() {
  const btn = document.getElementById('top-presentation-btn');
  const apply = (enabled) => {
    document.body.classList.toggle('presentation-mode', enabled);
    if (btn) btn.textContent = `presentation: ${enabled ? 'on' : 'off'}`;
  };
  const saved = localStorage.getItem(PRESENTATION_KEY) === '1';
  apply(saved);
  btn?.addEventListener('click', () => {
    const next = !document.body.classList.contains('presentation-mode');
    localStorage.setItem(PRESENTATION_KEY, next ? '1' : '0');
    apply(next);
  });
}

function initAutoRemediationPlaybooks() {
  const panelErrorCounts = new Map();
  const mark = (reason) => window.dispatchEvent(new CustomEvent('eta-remediation', { detail: { reason } }));

  window.addEventListener('eta-panel-error', (e) => {
    const id = String(e.detail?.panelId || '');
    if (!id) return;
    panelErrorCounts.set(id, (panelErrorCounts.get(id) || 0) + 1);
    if ((panelErrorCounts.get(id) || 0) >= 2) {
      const p = poller.panels.find((x) => x.containerId === id);
      p?.refresh().catch(() => undefined);
      mark(`retry ${id}`);
      panelErrorCounts.set(id, 0);
    }
  });

  setInterval(() => {
    const now = Date.now();
    poller.panels.forEach((p) => {
      const last = Number(p.element?.dataset?.lastRefreshAt || 0);
      if (!last) return;
      const age = now - last;
      if (age > 40_000) {
        p.refresh().catch(() => undefined);
        mark(`stale refresh ${p.containerId}`);
      }
    });
    if (liveStream.connected && liveStream.lastEventAt && now - liveStream.lastEventAt > 30_000) {
      liveStream.connect();
      poller._tick();
      mark('stream rebind');
    }
  }, 7000);
}

function initUpdateAuditStrip() {
  const host = document.getElementById('update-audit-strip');
  if (!host) return;
  const events = [];
  const render = () => {
    host.innerHTML = events.slice(-8).map((ev) =>
      `<div class="audit-chip">${ev.panel} · ${ev.latency}ms · ${ev.age}s</div>`,
    ).join('');
  };
  window.addEventListener('eta-panel-refresh', (e) => {
    const panel = String(e.detail?.panelId || 'panel');
    const at = Number(e.detail?.at || Date.now());
    const latency = Number(e.detail?.latencyMs || 0);
    events.push({ panel, at, latency, age: 0 });
    if (events.length > 80) events.splice(0, events.length - 80);
    render();
  });
  setInterval(() => {
    const now = Date.now();
    events.forEach((ev) => { ev.age = Math.max(0, Math.floor((now - ev.at) / 1000)); });
    render();
  }, 1000);
}

function initPerformanceAutopilot() {
  const key = 'eta.command_center.perf_mode';
  const pref = localStorage.getItem(key);
  if (pref === 'low') {
    document.body.classList.add('perf-low');
    return;
  }
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    document.body.classList.add('perf-low');
    localStorage.setItem(key, 'low');
    return;
  }
  let frames = 0;
  let last = performance.now();
  const tick = (ts) => {
    frames += 1;
    if (ts - last >= 2000) {
      const fps = (frames * 1000) / (ts - last);
      if (fps < 28) {
        document.body.classList.add('perf-low');
        localStorage.setItem(key, 'low');
        return;
      }
      frames = 0;
      last = ts;
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function initMobileCommandSheet() {
  if (window.matchMedia && !window.matchMedia('(max-width: 1024px)').matches) return;
  const sheet = document.createElement('div');
  sheet.id = 'mobile-command-sheet';
  sheet.className = 'mobile-sheet hidden';
  sheet.innerHTML = `
    <div class="mobile-sheet-backdrop" data-close-sheet="1"></div>
    <div class="mobile-sheet-card">
      <div class="mobile-sheet-head">
        <span>Operator Actions</span>
        <button type="button" id="mobile-sheet-close">close</button>
      </div>
      <div class="mobile-sheet-body">
        <button data-macro="pause">Pause All</button>
        <button data-macro="resume">Resume All</button>
        <button data-macro="flatten">Flatten All</button>
        <button data-macro="kill">Kill All</button>
      </div>
    </div>`;
  const fab = document.createElement('button');
  fab.id = 'mobile-command-fab';
  fab.className = 'mobile-command-fab';
  fab.textContent = 'ops';
  document.body.appendChild(sheet);
  document.body.appendChild(fab);
  const open = () => sheet.classList.remove('hidden');
  const close = () => sheet.classList.add('hidden');
  fab.addEventListener('click', open);
  sheet.querySelector('[data-close-sheet="1"]')?.addEventListener('click', close);
  sheet.querySelector('#mobile-sheet-close')?.addEventListener('click', close);
  sheet.querySelectorAll('[data-macro]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const act = btn.getAttribute('data-macro');
      if (!act) return;
      if (act === 'flatten') await runMacro('flatten', { stepUpReason: 'Flatten all bots requires step-up.' });
      else if (act === 'kill') await runMacro('kill', { stepUpReason: 'Kill all bots requires step-up.' });
      else await runMacro(act);
      close();
    });
  });
}

function boot() {
  initPerformanceAutopilot();
  initThemeToggle();
  initExport();
  initPinPanels();
  initCommandPalette();
  restoreLayout();
  initDraggableLayouts();
  initMacros();
  initSeverityTimeline();
  initHideableFloatingSurfaces();
  initMinimalMode();
  initDataFreshnessTelemetry();
  initCardHealthContract();
  initCommandCenterDiagnostics();
  initConsistencyGuardrails();
  initLatencyBudgetGuardrails();
  initTradeRefreshFanout();
  initAutopilotWatchdog();
  initUpdateAuditStrip();
  initMobileCommandSheet();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
