// eta_engine/deploy/status_page/js/panels.js
// Panel base class + formatters + tab manager.
// Wave-7 dashboard, 2026-04-27.

const STALE_AFTER_MS = 30_000;
let PANEL_SEQ = 0;

function shouldDeferHiddenRefresh(panel) {
  if (!document.hidden) return false;
  if (!panel.lastRefreshAt) return false;
  panel.updateRefreshLabel();
  return true;
}

function panelIcon(containerId) {
  const id = String(containerId || '');
  if (id.includes('verdict')) return 'V';
  if (id.includes('stress')) return 'S';
  if (id.includes('supercharge')) return 'SC';
  if (id.includes('toggle')) return 'T';
  if (id.includes('explain')) return 'X';
  if (id.includes('health')) return 'H';
  if (id.includes('disagreement')) return 'D';
  if (id.includes('edge')) return 'E';
  if (id.includes('policy')) return 'P';
  if (id.includes('model')) return 'M';
  if (id.includes('kaizen')) return 'K';
  if (id.includes('roster')) return 'R';
  if (id.includes('drill')) return 'Q';
  if (id.includes('equity')) return '$';
  if (id.includes('drawdown')) return 'DD';
  if (id.includes('correlation')) return 'C';
  if (id.includes('position')) return 'POS';
  if (id.includes('risk')) return '!';
  if (id.includes('controls')) return 'CTL';
  return 'ETA';
}

function panelTone(containerId) {
  const id = String(containerId || '');
  if (id.includes('risk') || id.includes('drawdown') || id.includes('kill')) return 'tone-risk';
  if (id.includes('health') || id.includes('reconciler')) return 'tone-health';
  if (id.includes('equity') || id.includes('edge') || id.includes('policy')) return 'tone-alpha';
  if (id.includes('verdict') || id.includes('fills') || id.includes('roster') || id.includes('drill')) return 'tone-execution';
  return 'tone-neutral';
}

export class Panel {
  /**
   * @param {string} containerId - the data-panel-id value (without #)
   * @param {string} endpoint    - HTTP endpoint to fetch
   * @param {string} title       - human-readable panel title
   */
  constructor(containerId, endpoint, title) {
    this.containerId = containerId;
    this.endpoint = endpoint;
    this.title = title;
    this.lastRefreshAt = null;
    this.lastError = null;
    this.element = document.querySelector(`[data-panel-id="${containerId}"]`);
    if (this.element) {
      const icon = panelIcon(containerId);
      this.element.innerHTML = `<div class="panel-title"><span class="panel-badge">${icon}</span>${title}</div><div data-panel-body></div><div class="panel-refresh"></div>`;
      this.body = this.element.querySelector('[data-panel-body]');
      this.refreshLabel = this.element.querySelector('.panel-refresh');
      this.element.classList.add(panelTone(containerId));
      this.element.classList.add('panel-enter');
      this.element.style.setProperty('--panel-enter-delay', `${Math.min(PANEL_SEQ * 24, 420)}ms`);
      PANEL_SEQ += 1;
      requestAnimationFrame(() => this.element?.classList.add('panel-enter-visible'));
    }
  }

  setLoading() {
    if (!this.element) return;
    this.element.classList.add('loading');
    this.element.classList.remove('error', 'stale');
  }

  setError(message) {
    if (!this.element) return;
    this.element.classList.add('error');
    this.element.classList.remove('loading');
    this.body.innerHTML = `<div class="text-red-400 text-xs">error: ${escapeHtml(message)}</div>`;
    this.lastError = message;
    window.dispatchEvent(new CustomEvent('eta-panel-error', {
      detail: { panelId: this.containerId, at: Date.now(), message: String(message || '') },
    }));
  }

  markStale() {
    if (!this.element) return;
    this.element.classList.add('stale');
  }

  /** Subclasses override this. */
  render(_data) {
    if (!this.body) return;
    this.body.textContent = JSON.stringify(_data, null, 2);
  }

  /** Called by Poller. Fetches + renders + handles errors. */
  async refresh() {
    if (!this.element) return;
    if (shouldDeferHiddenRefresh(this)) return;
    const startedAt = Date.now();
    this._requestSeq = (this._requestSeq || 0) + 1;
    const requestSeq = this._requestSeq;
    this.setLoading();
    try {
      const resp = await fetch(this.endpoint, {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        const code = body?.detail?.error_code || body?.error_code || `http_${resp.status}`;
        const detail = body?.detail?.detail || body?.detail || '';
        this.setError(`${code}${detail ? ` (${detail})` : ''}`);
        return;
      }
      const data = await resp.json();
      // Replay-safe guard: ignore responses older than the newest in-flight request.
      if (requestSeq !== this._requestSeq) return;
      try {
        this.render(data);
        this.element.classList.remove('loading', 'error', 'stale');
        this.lastRefreshAt = Date.now();
        this.element.dataset.lastRefreshAt = String(this.lastRefreshAt);
        this.updateRefreshLabel();
        window.dispatchEvent(new CustomEvent('eta-panel-refresh', {
          detail: {
            panelId: this.containerId,
            endpoint: this.endpoint,
            at: this.lastRefreshAt,
            latencyMs: Math.max(0, this.lastRefreshAt - startedAt),
          },
        }));
      } catch (e) {
        console.error(`render failed for ${this.containerId}`, e);
        this.setError(`render: ${e.message}`);
      }
    } catch (e) {
      console.error(`fetch failed for ${this.containerId}`, e);
      this.setError(`network: ${e.message}`);
    }
  }

  updateRefreshLabel() {
    if (!this.refreshLabel || !this.lastRefreshAt) return;
    const ageS = Math.floor((Date.now() - this.lastRefreshAt) / 1000);
    if (ageS > STALE_AFTER_MS / 1000) {
      this.markStale();
      this.refreshLabel.textContent = `stale ${ageS}s`;
    } else {
      this.refreshLabel.textContent = `updated ${ageS}s ago`;
    }
  }
}

// --- formatters ---

export function formatNumber(n, digits = 2) {
  if (n === null || n === undefined || isNaN(n)) return '-';
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatPct(p, digits = 2) {
  if (p === null || p === undefined || isNaN(p)) return '-';
  return `${(Number(p) * 100).toFixed(digits)}%`;
}

export function formatR(r) {
  if (r === null || r === undefined || isNaN(r)) return '-';
  const sign = r >= 0 ? '+' : '';
  return `${sign}${Number(r).toFixed(2)}R`;
}

export function formatTime(isoOrEpoch) {
  if (!isoOrEpoch) return '-';
  const d = typeof isoOrEpoch === 'number' ? new Date(isoOrEpoch * 1000) : new Date(isoOrEpoch);
  if (isNaN(d.getTime())) return '-';
  return d.toLocaleTimeString();
}

export function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

// --- tab manager ---

export function initTabs() {
  const tabBtns = document.querySelectorAll('.tab-btn');
  const nav = document.querySelector('nav');
  if (nav && !nav.querySelector('.tab-indicator')) {
    const indicator = document.createElement('div');
    indicator.className = 'tab-indicator';
    nav.appendChild(indicator);
  }
  const indicator = nav?.querySelector('.tab-indicator');
  const moveIndicator = (btn) => {
    if (!indicator || !btn || !nav) return;
    const navRect = nav.getBoundingClientRect();
    const btnRect = btn.getBoundingClientRect();
    indicator.style.setProperty('--tab-left', `${btnRect.left - navRect.left}px`);
    indicator.style.setProperty('--tab-width', `${btnRect.width}px`);
  };
  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;
      tabBtns.forEach(b => {
        b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
        b.classList.toggle('border-emerald-500', b === btn);
        b.classList.toggle('text-emerald-400', b === btn);
        b.classList.toggle('border-transparent', b !== btn);
        b.classList.toggle('text-zinc-400', b !== btn);
      });
      document.querySelectorAll('section[id^="view-"]').forEach(sec => {
        sec.classList.toggle('hidden', sec.id !== `view-${target}`);
      });
      moveIndicator(btn);
    });
  });
  const selected = Array.from(tabBtns).find((b) => b.getAttribute('aria-selected') === 'true') || tabBtns[0];
  if (selected) moveIndicator(selected);
  window.addEventListener('resize', () => moveIndicator(
    Array.from(tabBtns).find((b) => b.getAttribute('aria-selected') === 'true') || tabBtns[0],
  ));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    const active = Array.from(tabBtns).find((b) => b.getAttribute('aria-selected') === 'true') || tabBtns[0];
    moveIndicator(active);
    window.dispatchEvent(new CustomEvent('eta-dashboard-visible', {
      detail: { at: Date.now() },
    }));
  });

  const compactBtn = document.getElementById('top-compact-toggle');
  const compactKey = 'eta.command_center.compact_mode';
  const applyCompact = (enabled) => {
    document.body.classList.toggle('compact', enabled);
    if (compactBtn) compactBtn.textContent = enabled ? 'compact on' : 'compact off';
  };
  const savedCompact = localStorage.getItem(compactKey) === '1';
  applyCompact(savedCompact);
  compactBtn?.addEventListener('click', () => {
    const next = !document.body.classList.contains('compact');
    localStorage.setItem(compactKey, next ? '1' : '0');
    applyCompact(next);
  });
}

// --- selection state ---

export const selection = {
  botId: 'mnq',     // default selected bot
  symbol: 'MNQ',
};

export function selectBot(botId, symbol) {
  selection.botId = botId;
  selection.symbol = symbol;
  window.dispatchEvent(new CustomEvent('selection-changed', {
    detail: { botId, symbol },
  }));
}
