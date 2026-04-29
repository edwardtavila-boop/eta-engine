// eta_engine/deploy/status_page/js/bot_fleet.js
// 12 fleet panels + lifecycle button handlers.
// Wave-7 dashboard, 2026-04-27.

import { Panel, formatPct, formatR, formatTime, formatNumber, escapeHtml,
         selection, selectBot } from '/js/panels.js';
import { liveStream, poller } from '/js/live.js';
import { onAuthenticated, authedPost } from '/js/auth.js';

const ACTION_COOLDOWN_MS = 12_000;
let lastDangerActionAt = 0;

function notify(message, type = 'success') {
  const host = document.getElementById('toast-container');
  if (!host) {
    console[type === 'error' ? 'error' : 'log'](message);
    return;
  }
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

// --- 1. Roster table ---
class RosterPanel extends Panel {
  constructor() {
    super('fl-roster', '/api/bot-fleet?since_days=1', 'Bot Fleet Roster');
    let timer = null;
    const kick = () => {
      if (timer) return;
      timer = setTimeout(() => {
        timer = null;
        this.refresh();
      }, 300);
    };
    window.addEventListener('eta-trade-update', kick);
    setInterval(() => this.refresh(), 4000);
  }
  async refresh() {
    this.setLoading();
    const base = '/api/bot-fleet?since_days=1';
    const endpoints = [base, `${base}&_t=${Date.now()}`];
    let okData = null;
    for (const endpoint of endpoints) {
      try {
        const r = await fetch(endpoint, { credentials: 'same-origin', cache: 'no-store' });
        if (!r.ok) continue;
        const data = await r.json();
        okData = data;
        this.endpoint = endpoint;
        break;
      } catch {
        // Try next endpoint.
      }
    }
    if (!okData) {
      this.setError('roster_unavailable');
      return;
    }
    this.render(okData);
    this.element.classList.remove('loading', 'error', 'stale');
    this.lastRefreshAt = Date.now();
    this.element.dataset.lastRefreshAt = String(this.lastRefreshAt);
    this.updateRefreshLabel();
  }
  render(data) {
    const bots = data.bots || [];
    if (bots.length === 0) {
      const line = data.truth_summary_line || data._warning || 'No live ETA bot roster is reporting.';
      const status = data.truth_status || 'empty';
      const runtime = [data.runtime_mode, data.runtime_detail].filter(Boolean).join(' / ') || 'not reported';
      const warnings = Array.isArray(data.truth_warnings) ? data.truth_warnings.slice(0, 3) : [];
      this.body.innerHTML = `
        <div class="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm">
          <div class="font-semibold text-amber-200">No live fleet rows from canonical ETA state</div>
          <div class="mt-1 text-zinc-300">${escapeHtml(line)}</div>
          <div class="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
            <div class="metric-card p-2"><div class="metric-label">Truth Status</div><div class="metric-value text-sm">${escapeHtml(status)}</div></div>
            <div class="metric-card p-2"><div class="metric-label">Runtime</div><div class="metric-value text-sm">${escapeHtml(runtime)}</div></div>
            <div class="metric-card p-2"><div class="metric-label">Confirmed Bots</div><div class="metric-value text-sm">${Number(data.confirmed_bots || 0)}</div></div>
          </div>
          ${warnings.length ? `<div class="mt-2 text-[11px] text-zinc-500">${warnings.map(escapeHtml).join('<br>')}</div>` : ''}
        </div>`;
      return;
    }
    const srvTs = data.server_ts ? new Date(data.server_ts * 1000).toLocaleTimeString() : '-';
    const live = data.live || {};
    const dataAge = data.data_age_s ?? data.fleet_age_s ?? live.last_fill_age_s ?? null;
    const quality = data.stale_payload_alert ? 'stale' : data.truth_status || 'live';
    const liveLine = `server ${srvTs} | fills 1h ${live.fills_1h ?? 0} | fills 24h ${live.fills_24h ?? 0} | last fill ${formatTime(live.last_fill_ts)}`;
    this.body.innerHTML = `<div class="dashboard-freshness-line text-[10px] text-zinc-500 mb-1" data-quality="${escapeHtml(String(quality))}">live roster | ${escapeHtml(liveLine)} | age ${dataAge ?? 'n/a'}s | confirmed ${Number(data.confirmed_bots || 0)} | window yesterday-&gt;now</div><table class="mobile-card-table w-full text-xs"><thead class="text-zinc-500">
      <tr><th class="text-left">Bot</th><th class="text-left">Symbol</th><th class="text-left">Tier</th><th class="text-left">Venue</th><th class="text-left">Status</th><th class="text-right">Day PnL</th><th class="text-left">Last Trade</th><th class="text-left">Age</th><th class="text-right">R</th></tr>
      </thead><tbody>${bots.map(b => {
        const statusCls = b.status === 'running' ? 'text-emerald-400'
                        : b.status === 'paused' ? 'text-amber-400'
                        : b.status === 'killed' ? 'text-red-400' : 'text-zinc-400';
        const pnlCls = (b.todays_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400';
        const isSel = selection.botId === b.name ? 'bg-zinc-800' : '';
        const age = Number(b.last_trade_age_s || 0);
        const ageLabel = age > 0 ? `${Math.floor(age / 60)}m` : '-';
        const side = b.last_trade_side ? String(b.last_trade_side).toUpperCase() : '-';
        return `<tr class="cursor-pointer hover:bg-zinc-800 ${isSel}" data-bot-id="${escapeHtml(b.name)}" data-symbol="${escapeHtml(b.symbol)}">
          <td data-label="Bot">${escapeHtml(b.name)}</td>
          <td data-label="Symbol">${escapeHtml(b.symbol)}</td>
          <td data-label="Tier">${escapeHtml(b.tier)}</td>
          <td data-label="Venue">${escapeHtml(b.venue)}</td>
          <td data-label="Status" class="${statusCls}">${escapeHtml(b.status)}</td>
          <td data-label="Day PnL" class="text-right ${pnlCls}">${formatNumber(b.todays_pnl)}</td>
          <td data-label="Last Trade" class="text-zinc-300">${side} | ${formatTime(b.last_trade_ts)}</td>
          <td data-label="Age" class="text-zinc-500">${ageLabel}</td>
          <td data-label="R" class="text-right ${(b.last_trade_r || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">${formatR(b.last_trade_r)}</td>
        </tr>`;
      }).join('')}</tbody></table>`;
    this.body.querySelectorAll('tr[data-bot-id]').forEach(tr => {
      tr.addEventListener('click', () => selectBot(tr.dataset.botId, tr.dataset.symbol));
    });
  }
}

// --- 2. Drill-down ---
class DrilldownPanel extends Panel {
  constructor() {
    super('fl-drilldown', `/api/bot-fleet/${selection.botId}`, 'Last Trade & Drill-Down');
    window.addEventListener('selection-changed', (e) => {
      this.endpoint = `/api/bot-fleet/${e.detail.botId}`;
      this.refresh();
    });
  }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">no bot selected</div>`; return; }
    const status = data.status || {};
    const fills = data.recent_fills || [];
    const verdicts = data.recent_verdicts || [];
    const latest = fills[0] || null;
    let latestDur = Number(latest?.hold_seconds || latest?.duration_s || latest?.time_in_trade_s || 0);
    if (!latestDur && Number(status.open_positions || 0) > 0 && status.last_signal_ts) {
      const enteredAt = new Date(status.last_signal_ts);
      if (!Number.isNaN(enteredAt.getTime())) {
        latestDur = Math.max(0, Math.floor((Date.now() - enteredAt.getTime()) / 1000));
      }
    }
    const latestDurLabel = latestDur > 0 ? `${Math.floor(latestDur / 60)}m ${Math.floor(latestDur % 60)}s` : '—';
    this.body.innerHTML = `
      <div class="grid grid-cols-4 gap-2 mb-3 text-xs">
        <div class="metric-card p-2"><div class="metric-label">Last Side</div><div class="metric-value">${escapeHtml(String(latest?.side || '—').toUpperCase())}</div></div>
        <div class="metric-card p-2"><div class="metric-label">Last Size</div><div class="metric-value">${latest?.qty ?? '—'}</div></div>
        <div class="metric-card p-2"><div class="metric-label">Last R / PnL</div><div class="metric-value ${(latest?.realized_r || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">${formatR(latest?.realized_r)}</div></div>
        <div class="metric-card p-2"><div class="metric-label">Time in Trade</div><div class="metric-value">${latestDurLabel}</div></div>
      </div>
      <div class="text-xs text-zinc-500 mb-1">Recent Fills</div>
      <div class="space-y-1 text-xs font-mono mb-3">${fills.slice(0, 5).map(f =>
        `<div>${formatTime(f.ts)} ${escapeHtml(f.side)} ${formatNumber(f.price)} qty=${f.qty} ${formatR(f.realized_r)}</div>`
      ).join('') || '<div class="text-zinc-600">none</div>'}</div>
      <div class="text-xs text-zinc-500 mb-1">Recent Verdicts</div>
      <div class="space-y-1 text-xs">${verdicts.slice(0, 5).map(v =>
        `<div><span class="text-emerald-400">${escapeHtml(v.verdict)}</span> ${escapeHtml(v.sage_modulation || '')}</div>`
      ).join('') || '<div class="text-zinc-600">none</div>'}</div>`;
  }
}

// --- 3. Equity curve (Chart.js) — per-bot + timeframe selector + KPI cards ---
class EquityCurvePanel extends Panel {
  constructor() {
    super('fl-equity-curve', `/api/equity?range=1d&normalize=1&since_days=1&bot=${encodeURIComponent(selection.botId)}`, 'Fleet Equity Curve');
    this.chart = null;
    this.selectedBot = null; // default to fleet overview mode
    this.range = '1d';
    this.rosterSnapshot = [];
    this._buildShell();
    let timer = null;
    const kick = () => {
      if (timer) return;
      timer = setTimeout(() => {
        timer = null;
        this.refresh();
      }, 280);
    };
    window.addEventListener('eta-trade-update', kick);
    setInterval(() => this.refresh(), 4500);

    // Keep fleet equity as the default view; bot-specific mode is opt-in via dropdown.
  }

  _buildShell() {
    if (!this.body) return;
    this.body.innerHTML = `
      <div class="flex items-center justify-between mb-2 text-xs">
        <select id="eq-bot-select" class="bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-zinc-100">
          <option value="">Fleet aggregate</option>
        </select>
        <div class="flex gap-1" id="eq-range-pills">
          <button data-r="1d"  class="px-2 py-1 rounded bg-emerald-600 text-white">1D</button>
          <button data-r="1w"  class="px-2 py-1 rounded bg-zinc-700 text-zinc-300 hover:bg-zinc-600">1W</button>
          <button data-r="1m"  class="px-2 py-1 rounded bg-zinc-700 text-zinc-300 hover:bg-zinc-600">1M</button>
          <button data-r="all" class="px-2 py-1 rounded bg-zinc-700 text-zinc-300 hover:bg-zinc-600">All</button>
        </div>
      </div>

      <div class="grid grid-cols-4 gap-2 mb-3" id="eq-kpis">
        <div class="metric-card p-2"><div class="metric-label">Current</div><div data-k="current_equity" class="metric-value text-sm">—</div></div>
        <div class="metric-card p-2"><div class="metric-label">Today</div><div data-k="today_pnl" class="metric-value text-sm">—</div></div>
        <div class="metric-card p-2"><div class="metric-label">Week</div><div data-k="week_pnl" class="metric-value text-sm">—</div></div>
        <div class="metric-card p-2"><div class="metric-label">Month</div><div data-k="month_pnl" class="metric-value text-sm">—</div></div>
      </div>
      <div id="eq-live-overview" class="text-[11px] text-zinc-400 mb-2"></div>
      <div id="eq-portfolio-overview" class="hidden mb-2"></div>

      <div class="mobile-chart-shell"><canvas id="eq-chart"></canvas></div>`;

    // Wire range-pill clicks
    this.body.querySelectorAll('#eq-range-pills button').forEach(btn => {
      btn.addEventListener('click', () => {
        this.range = btn.dataset.r;
        // visual state
        this.body.querySelectorAll('#eq-range-pills button').forEach(b => {
          b.className = (b === btn)
            ? 'px-2 py-1 rounded bg-emerald-600 text-white'
            : 'px-2 py-1 rounded bg-zinc-700 text-zinc-300 hover:bg-zinc-600';
        });
        this._updateEndpoint();
        this.refresh();
      });
    });

    // Wire bot selector
    const sel = this.body.querySelector('#eq-bot-select');
    sel.addEventListener('change', (e) => {
      this.selectedBot = e.target.value || null;
      this._updateEndpoint();
      this.refresh();
    });

    // Populate bot list once we get the roster
    this._populateBotSelectFromRoster();
  }

  async _populateBotSelectFromRoster() {
    try {
      const r = await fetch('/api/bot-fleet', { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      const sel = this.body?.querySelector('#eq-bot-select');
      if (!sel) return;
      (body.bots || []).forEach(b => {
        const opt = document.createElement('option');
        opt.value = b.name;
        opt.textContent = `${b.name} (${b.symbol})`;
        sel.appendChild(opt);
      });
    } catch (_e) { /* ignore */ }
  }

  _updateEndpoint() {
    const params = new URLSearchParams();
    params.set('range', this.range);
    params.set('normalize', '1');
    params.set('since_days', '1');
    if (this.selectedBot) params.set('bot', this.selectedBot);
    this.endpoint = `/api/equity?${params.toString()}`;
    // Update title
    if (this.element) {
      const t = this.element.querySelector('.panel-title');
      if (t) t.textContent = this.selectedBot
        ? `Equity — ${this.selectedBot} (${this.range.toUpperCase()})`
        : `Fleet Equity Curve (${this.range.toUpperCase()})`;
    }
    // Sync the dropdown
    const sel = this.body?.querySelector('#eq-bot-select');
    if (sel) sel.value = this.selectedBot || '';
  }

  async refresh() {
    if (!this.element) return;
    this.setLoading();
    this._updateEndpoint();
    const base = this.endpoint || '/api/equity?range=1d&normalize=1';
    const sep = base.includes('?') ? '&' : '?';
    const endpoints = [base, `${base}${sep}_t=${Date.now()}`];
    let payload = null;
    for (const endpoint of endpoints) {
      try {
        const resp = await fetch(endpoint, { credentials: 'same-origin', cache: 'no-store' });
        if (!resp.ok) continue;
        payload = await resp.json();
        this.endpoint = endpoint;
        break;
      } catch {
        // Try next endpoint candidate.
      }
    }
    if (!payload) {
      this.setError('equity_unavailable');
      return;
    }
    if (!this.selectedBot) {
      try {
        const rr = await fetch('/api/bot-fleet?since_days=1', { credentials: 'same-origin', cache: 'no-store' });
        if (rr.ok) {
          const rj = await rr.json();
          this.rosterSnapshot = Array.isArray(rj?.bots) ? rj.bots : [];
        }
      } catch {
        // Keep current snapshot if roster fetch fails.
      }
    }
    this.render(payload);
    this.element.classList.remove('loading', 'error', 'stale');
    this.lastRefreshAt = Date.now();
    this.element.dataset.lastRefreshAt = String(this.lastRefreshAt);
    this.updateRefreshLabel();
  }

  render(data) {
    if (data._warning) {
      this._renderKPIs({});
      const ov = this.body?.querySelector('#eq-live-overview');
      if (ov) ov.textContent = data.session_truth_line || data.truth_summary_line || 'No live ETA equity curve is publishing into canonical state.';
      this._renderChart([], data.session_truth_line || data.truth_summary_line || 'no live equity data from canonical ETA state');
      return;
    }
    let series = Array.isArray(data.series) ? data.series : [];
    // Client-side safety net: if an old endpoint leaks legacy scale, rebase bot 1D to 5k.
    if (this.selectedBot && this.range === '1d' && series.length > 1) {
      const firstEq = Number(series[0]?.equity ?? NaN);
      if (Number.isFinite(firstEq) && firstEq > 10_000) {
        const anchor = firstEq;
        series = series.map((p) => {
          const eq = Number(p?.equity ?? NaN);
          if (!Number.isFinite(eq)) return p;
          return { ...p, equity: Number((5000 + (eq - anchor)).toFixed(2)) };
        });
      }
    }
    const t = this.element?.querySelector('.panel-title');
    if (t) {
      const srv = data.server_ts ? new Date(data.server_ts * 1000).toLocaleTimeString() : '-';
      t.textContent = `${this.selectedBot ? `Equity - ${this.selectedBot}` : 'Fleet Equity Curve'} (${this.range.toUpperCase()}) | ${data.source || 'live'} | ${srv}`;
    }
    const ov = this.body?.querySelector('#eq-live-overview');
    if (ov) {
      const lv = data.live || {};
      const sourceAge = data.source_age_s ?? (
        data.data_ts && data.server_ts ? Math.max(0, Math.round(data.server_ts - data.data_ts)) : null
      );
      const sourceUpdatedAt = data.source_updated_at || (
        data.data_ts ? new Date(data.data_ts * 1000).toISOString() : null
      );
      ov.textContent = `source heartbeat: ${formatTime(sourceUpdatedAt)} | source age: ${sourceAge ?? 'n/a'}s | last fill: ${formatTime(lv.last_fill_ts)} | fills 1h: ${lv.fills_1h ?? 0} | fills 24h: ${lv.fills_24h ?? 0}`;
    }
    this._renderPortfolioOverview(this.rosterSnapshot);
    this._renderKPIs(data.summary || {}, series);
    this._renderChart(series);
  }

  _renderPortfolioOverview(bots) {
    const wrap = this.body?.querySelector('#eq-portfolio-overview');
    const chartHost = this.body?.querySelector('canvas#eq-chart')?.parentElement;
    if (!wrap || !chartHost) return;
    if (this.selectedBot) {
      wrap.classList.add('hidden');
      chartHost.classList.remove('hidden');
      return;
    }
    const rows = Array.isArray(bots) ? bots : [];
    if (!rows.length) {
      wrap.classList.add('hidden');
      chartHost.classList.remove('hidden');
      return;
    }
    const equityRows = rows.map((b) => {
      const pnl = Number(b?.todays_pnl || 0);
      return {
        id: b?.name || 'bot',
        pnl,
        eq: 5000 + pnl,
        heartbeatAge: Number(b?.heartbeat_age_s || 0),
        lastAge: Number(b?.last_trade_age_s || 0),
      };
    });
    const totalEq = equityRows.reduce((acc, r) => acc + r.eq, 0);
    const totalAbs = equityRows.reduce((acc, r) => acc + Math.abs(r.pnl), 0);
    const leader = [...equityRows].sort((a, b) => b.eq - a.eq)[0];
    const lagger = [...equityRows].sort((a, b) => a.eq - b.eq)[0];
    const concentration = totalAbs > 0
      ? Math.max(...equityRows.map((r) => Math.abs(r.pnl))) / totalAbs
      : 0;
    wrap.classList.remove('hidden');
    chartHost.classList.add('hidden');
    wrap.innerHTML = `
      <div class="grid grid-cols-2 xl:grid-cols-4 gap-2 mb-2">
        <div class="metric-card p-2"><div class="metric-label">Portfolio Equity</div><div class="metric-value text-cyan-300">$${formatNumber(totalEq)}</div></div>
        <div class="metric-card p-2"><div class="metric-label">Leader</div><div class="metric-value text-emerald-400">${escapeHtml(leader.id)} $${formatNumber(leader.eq)}</div></div>
        <div class="metric-card p-2"><div class="metric-label">Lagger</div><div class="metric-value text-amber-300">${escapeHtml(lagger.id)} $${formatNumber(lagger.eq)}</div></div>
        <div class="metric-card p-2"><div class="metric-label">Concentration</div><div class="metric-value">${formatPct(concentration, 1)}</div></div>
      </div>
      <div class="space-y-1">
        ${equityRows
          .sort((a, b) => b.eq - a.eq)
          .map((r) => {
            const width = totalEq > 0 ? Math.max(5, Math.round((r.eq / totalEq) * 100)) : 5;
            const hb = r.heartbeatAge <= 90 ? 'text-emerald-400' : r.heartbeatAge <= 300 ? 'text-amber-300' : 'text-red-400';
            return `<div class="text-xs">
              <div class="flex items-center justify-between mb-0.5">
                <span>${escapeHtml(r.id)}</span>
                <span class="font-mono">$${formatNumber(r.eq)} <span class="${r.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}">(${r.pnl >= 0 ? '+' : ''}${formatNumber(r.pnl)})</span></span>
              </div>
              <div class="h-1.5 bg-zinc-800 rounded overflow-hidden"><div class="h-1.5 bg-cyan-400/70" style="width:${width}%"></div></div>
              <div class="text-[10px] text-zinc-500">heartbeat <span class="${hb}">${Math.max(0, r.heartbeatAge)}s</span> | last trade ${Math.max(0, r.lastAge)}s ago</div>
            </div>`;
          })
          .join('')}
      </div>`;
  }

  _renderKPIs(summary, series = []) {
    const fmt = (v) => {
      if (v === null || v === undefined) return '—';
      const sign = v >= 0 ? '+' : '';
      return `${sign}${formatNumber(v)}`;
    };
    const pnlClass = (v) => {
      if (v === null || v === undefined) return 'text-zinc-400';
      return v >= 0 ? 'text-emerald-400' : 'text-red-400';
    };

    const grid = this.body?.querySelector('#eq-kpis');
    if (!grid) return;
    grid.querySelectorAll('[data-k]').forEach(el => {
      const key = el.dataset.k;
      const val = summary[key];
      const prev = el.dataset.prevValue;
      const curr = val === null || val === undefined ? '' : String(val);
      if (key === 'current_equity') {
        const liveCurrent = Array.isArray(series) && series.length
          ? Number(series[series.length - 1]?.equity ?? NaN)
          : NaN;
        const resolved = Number.isFinite(liveCurrent) ? liveCurrent : val;
        el.textContent = resolved !== null && resolved !== undefined && Number.isFinite(Number(resolved))
          ? `$${formatNumber(resolved)}`
          : '—';
        el.className = 'text-sm font-mono text-zinc-100';
      } else if (key === 'today_pnl') {
        const first = Array.isArray(series) && series.length ? Number(series[0]?.equity ?? NaN) : NaN;
        const last = Array.isArray(series) && series.length ? Number(series[series.length - 1]?.equity ?? NaN) : NaN;
        const livePnl = Number.isFinite(first) && Number.isFinite(last) ? Number((last - first).toFixed(2)) : val;
        el.textContent = fmt(livePnl);
        el.className = `text-sm font-mono ${pnlClass(livePnl)}`;
      } else {
        el.textContent = fmt(val);
        el.className = `text-sm font-mono ${pnlClass(val)}`;
      }
      if (prev !== undefined && prev !== curr) {
        el.classList.remove('kpi-flash');
        // Force restart for repeat updates.
        void el.offsetWidth;
        el.classList.add('kpi-flash');
      }
      el.dataset.prevValue = curr;
    });
  }

  _renderChart(series, emptyMessage = 'no data for this range') {
    if (this.chart) { this.chart.destroy(); this.chart = null; }
    let canvas = this.body?.querySelector('#eq-chart');
    // If someone replaced canvas with the empty-state div, restore it
    if (!canvas) {
      const wrap = document.createElement('div');
      wrap.className = 'mobile-chart-shell';
      wrap.innerHTML = '<canvas id="eq-chart"></canvas>';
      this.body.appendChild(wrap);
      canvas = this.body.querySelector('#eq-chart');
    }
    if (!series.length) {
      const parent = canvas.parentElement;
      parent.innerHTML = `<div class="text-zinc-500 text-sm py-12 text-center">${escapeHtml(emptyMessage)}</div>`;
      return;
    }
    const ctx = canvas.getContext('2d');
    const startEq = series[0].equity;
    const endEq   = series[series.length - 1].equity;
    const lineColor = endEq >= startEq ? '#10b981' : '#ef4444';
    const formatTick = (value) => {
      const d = new Date(value);
      if (Number.isNaN(d.getTime())) return '';
      if (this.range === '1d') {
        const hh = String(d.getHours()).padStart(2, '0');
        const mm = String(d.getMinutes()).padStart(2, '0');
        return `${hh}:${mm}`;
      }
      if (this.range === '1w' || this.range === '1m') {
        const mo = String(d.getMonth() + 1).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        return `${mo}/${dd}`;
      }
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    };
    const formatTooltipTitle = (value) => {
      const d = new Date(value);
      if (Number.isNaN(d.getTime())) return String(value || '');
      if (this.range === '1d') {
        return d.toLocaleString(undefined, {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        });
      }
      return d.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    };
    this.chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: series.map(p => p.ts),
        datasets: [{
          label: 'equity',
          data: series.map(p => p.equity),
          borderColor: lineColor,
          borderWidth: 2,
          backgroundColor: lineColor + '20',
          tension: 0.28,
          pointRadius: 0,
          pointHoverRadius: 2,
          fill: true,
        }],
      },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            backgroundColor: 'rgba(7, 12, 24, 0.94)',
            borderColor: 'rgba(34, 211, 238, 0.4)',
            borderWidth: 1,
            titleColor: '#9fb6d8',
            bodyColor: '#d9e8ff',
            callbacks: {
              title: (items) => formatTooltipTitle(items?.[0]?.label),
              label: (ctx) => ` ${formatNumber(ctx.parsed.y)}`,
            },
          },
        },
        scales: {
          x: {
            display: true,
            ticks: {
              color: '#6f88ad',
              maxTicksLimit: 6,
              font: { size: 9 },
              callback: (_v, idx) => formatTick(series[idx]?.ts),
            },
            grid: { color: 'rgba(51, 65, 85, 0.22)' },
          },
          y: {
            ticks: {
              color: '#9db2cf',
              font: { size: 10 },
              callback: (v) => formatNumber(v, 0),
            },
            grid: { color: 'rgba(51, 65, 85, 0.26)' },
          },
        },
        animation: false,
        maintainAspectRatio: false,
      },
    });
  }
}

// --- 4. Drawdown ---
class DrawdownPanel extends Panel {
  constructor() { super('fl-drawdown', '/api/risk_gates', 'Drawdown vs Threshold'); }
  render(data) {
    const bots = data.bots || [];
    const fleet = data.fleet_aggregate || {};
    this.body.innerHTML = `
      <div class="space-y-1">
        ${bots.map(b => {
          const dd = b.dd_pct ?? 0;
          const th = b.kill_threshold_pct ?? 8;
          const w = Math.min(100, (dd / th) * 100);
          return `<div class="flex items-center gap-2 text-xs">
            <span class="w-20 truncate">${escapeHtml(b.bot_id)}</span>
            <div class="flex-1 bg-zinc-800 h-2 rounded"><div class="h-full bg-amber-500 rounded" style="width:${w}%"></div></div>
            <span class="text-zinc-500">${dd.toFixed(1)}/${th}%</span>
          </div>`;
        }).join('') || '<div class="text-zinc-600 text-sm">no data</div>'}
      </div>
      <div class="mt-3 text-xs text-zinc-500">fleet: ${formatPct((fleet.fleet_dd_pct || 0) / 100)} of ${fleet.fleet_dd_threshold_pct || '?'}%</div>`;
  }
}

// --- 5. Sage modulation effect ---
class SageEffectPanel extends Panel {
  constructor() { super('fl-sage-effect', '/api/jarvis/sage_modulation_stats', 'Sage Modulation (24h)'); }
  render(data) {
    const perBot = data.per_bot || {};
    const rows = Object.entries(perBot);
    if (rows.length === 0) { this.body.innerHTML = '<div class="text-zinc-500 text-sm">no v22 firings yet</div>'; return; }
    this.body.innerHTML = `<table class="w-full text-xs">
      <tr class="text-zinc-500"><th class="text-left">bot</th><th class="text-right">loosen</th><th class="text-right">tighten</th><th class="text-right">defer</th></tr>
      ${rows.map(([bot, s]) =>
        `<tr><td>${escapeHtml(bot)}</td><td class="text-right text-emerald-400">${s.loosen ?? 0}</td><td class="text-right text-amber-400">${s.tighten ?? 0}</td><td class="text-right text-red-400">${s.defer ?? 0}</td></tr>`
      ).join('')}
    </table>`;
  }
}

// --- 6. Correlation throttle map ---
class CorrelationPanel extends Panel {
  constructor() { super('fl-correlation', '/api/preflight', 'Correlation Throttles'); }
  render(data) {
    const throttles = data.throttles || [];
    if (throttles.length === 0) {
      this.body.innerHTML = '<div class="text-emerald-400 text-sm">✓ no active throttles</div>';
      return;
    }
    const uniqSymbols = [];
    throttles.forEach((t) => {
      const a = String(t.symbol_a || '');
      const b = String(t.symbol_b || '');
      if (a && !uniqSymbols.includes(a)) uniqSymbols.push(a);
      if (b && !uniqSymbols.includes(b)) uniqSymbols.push(b);
    });
    const symbols = uniqSymbols.slice(0, 8);
    const cx = 210;
    const cy = 96;
    const radius = 70;
    const nodePos = {};
    symbols.forEach((s, i) => {
      const ang = (Math.PI * 2 * i) / Math.max(symbols.length, 1) - Math.PI / 2;
      nodePos[s] = {
        x: Math.round(cx + radius * Math.cos(ang)),
        y: Math.round(cy + radius * Math.sin(ang)),
      };
    });
    const edgeRows = throttles
      .filter((t) => nodePos[t.symbol_a] && nodePos[t.symbol_b])
      .slice(0, 16);
    const graphUid = `corr-${Date.now()}-${Math.floor(Math.random() * 10000)}`;
    const edgeDefs = edgeRows.map((t, idx) => {
        const p1 = nodePos[t.symbol_a];
        const p2 = nodePos[t.symbol_b];
        const rho = Number(t.rho || 0);
        const strong = Math.abs(rho) >= 0.75;
        const intensity = Math.max(0.2, Math.min(1, Math.abs(rho)));
        const dx = p2.x - p1.x;
        const dy = p2.y - p1.y;
        const len = Math.max(1, Math.hypot(dx, dy));
        const nx = -dy / len;
        const ny = dx / len;
        const bend = ((idx % 2 === 0 ? 1 : -1) * (14 + (idx % 5) * 6));
        const cx2 = Math.round((p1.x + p2.x) / 2 + nx * bend);
        const cy2 = Math.round((p1.y + p2.y) / 2 + ny * bend);
        const path = `M ${p1.x} ${p1.y} Q ${cx2} ${cy2} ${p2.x} ${p2.y}`;
        const pathId = `${graphUid}-edge-${idx}`;
        const speedA = (3.8 - intensity * 2.1).toFixed(2);
        const speedB = (5.6 - intensity * 2.4).toFixed(2);
        return {
          pathId,
          edge: `<path id="${pathId}" d="${path}" class="corr-edge ${strong ? 'strong' : 'soft'}" data-a="${escapeHtml(t.symbol_a)}" data-b="${escapeHtml(t.symbol_b)}" style="--corr-intensity:${intensity}" />`,
          packets: `
            <circle r="${strong ? '2.4' : '1.9'}" class="corr-packet ${strong ? 'strong' : 'soft'}">
              <animateMotion dur="${speedA}s" repeatCount="indefinite" rotate="auto">
                <mpath href="#${pathId}" />
              </animateMotion>
            </circle>
            <circle r="${strong ? '2.1' : '1.7'}" class="corr-packet ${strong ? 'strong' : 'soft'} dim">
              <animateMotion dur="${speedB}s" repeatCount="indefinite" rotate="auto" begin="${(idx % 7) * 0.28}s">
                <mpath href="#${pathId}" />
              </animateMotion>
            </circle>`,
        };
      });
    const edges = edgeDefs.map((e) => e.edge).join('');
    const packets = edgeDefs.map((e) => e.packets).join('');
    const nodes = symbols.map((s) => {
      const p = nodePos[s];
      return `<g>
        <circle cx="${p.x}" cy="${p.y}" r="12" class="corr-node" data-symbol="${escapeHtml(s)}" />
        <text x="${p.x}" y="${p.y + 4}" text-anchor="middle" class="corr-node-label" data-symbol="${escapeHtml(s)}">${escapeHtml(s)}</text>
      </g>`;
    }).join('');
    const rows = throttles.slice(0, 6).map((t) =>
      `<div class="corr-row" data-a="${escapeHtml(t.symbol_a)}" data-b="${escapeHtml(t.symbol_b)}">
        <span>${escapeHtml(t.symbol_a)} ↔ ${escapeHtml(t.symbol_b)}</span>
        <span class="text-zinc-400">ρ=${(Number(t.rho || 0)).toFixed(2)} · cap=${formatPct(t.cap_mult)}</span>
        <span class="corr-cap-bar"><i style="width:${Math.max(6, Math.min(100, Number(t.cap_mult || 0) * 100))}%"></i></span>
      </div>`,
    ).join('');
    const maxRho = throttles.reduce((m, t) => Math.max(m, Math.abs(Number(t.rho || 0))), 0);
    const avgCap = throttles.length
      ? throttles.reduce((s, t) => s + Number(t.cap_mult || 0), 0) / throttles.length
      : 0;
    this.body.innerHTML = `
      <div class="corr-graph-wrap mb-2">
        <div class="corr-ambient-layer"></div>
        <div class="corr-sweep-layer"></div>
        <svg viewBox="0 0 420 192" class="corr-graph">
          <defs>
            <radialGradient id="corrGlow" cx="50%" cy="50%" r="65%">
              <stop offset="0%" stop-color="rgba(34,211,238,0.22)" />
              <stop offset="100%" stop-color="rgba(34,211,238,0)" />
            </radialGradient>
            <radialGradient id="corrCore" cx="50%" cy="50%" r="62%">
              <stop offset="0%" stop-color="rgba(244,114,182,0.18)" />
              <stop offset="100%" stop-color="rgba(244,114,182,0)" />
            </radialGradient>
          </defs>
          <g class="corr-orbitals">
            <ellipse cx="210" cy="96" rx="124" ry="86" />
            <ellipse cx="210" cy="96" rx="108" ry="74" />
          </g>
          <g class="corr-grid">
            <line x1="68" y1="96" x2="352" y2="96" />
            <line x1="210" y1="22" x2="210" y2="170" />
          </g>
          <ellipse cx="210" cy="96" rx="95" ry="78" fill="url(#corrGlow)" />
          <ellipse cx="210" cy="96" rx="76" ry="62" fill="url(#corrCore)" />
          ${edges}
          ${packets}
          ${nodes}
        </svg>
      </div>
      <div class="corr-legend mb-2">
        <span><i class="corr-dot soft"></i>soft corr</span>
        <span><i class="corr-dot strong"></i>strong corr</span>
        <span><i class="corr-dot node"></i>symbol node</span>
      </div>
      <div class="corr-metrics mb-2 text-xs font-mono">
        <span>pairs: ${throttles.length}</span>
        <span>max |ρ|: ${maxRho.toFixed(2)}</span>
        <span>avg cap: ${formatPct(avgCap)}</span>
      </div>
      <div class="space-y-1 text-xs font-mono">${rows}</div>`;

    const panel = this.body;
    const edgesEls = [...panel.querySelectorAll('.corr-edge')];
    const nodeEls = [...panel.querySelectorAll('.corr-node, .corr-node-label')];
    const rowEls = [...panel.querySelectorAll('.corr-row')];
    let locked = null; // { type: 'symbol'|'pair', a, b }
    const clearFocus = () => {
      panel.classList.remove('corr-focus');
      edgesEls.forEach((el) => el.classList.remove('highlight'));
      nodeEls.forEach((el) => el.classList.remove('highlight'));
      rowEls.forEach((el) => el.classList.remove('highlight'));
    };
    const focusSymbol = (symbol) => {
      if (!symbol) return clearFocus();
      panel.classList.add('corr-focus');
      edgesEls.forEach((el) => {
        const a = el.getAttribute('data-a');
        const b = el.getAttribute('data-b');
        if (a === symbol || b === symbol) el.classList.add('highlight');
        else el.classList.remove('highlight');
      });
      nodeEls.forEach((el) => {
        if (el.getAttribute('data-symbol') === symbol) el.classList.add('highlight');
        else el.classList.remove('highlight');
      });
      rowEls.forEach((el) => {
        const a = el.getAttribute('data-a');
        const b = el.getAttribute('data-b');
        if (a === symbol || b === symbol) el.classList.add('highlight');
        else el.classList.remove('highlight');
      });
    };
    const focusPair = (a, b) => {
      panel.classList.add('corr-focus');
      edgesEls.forEach((el) => {
        const ea = el.getAttribute('data-a');
        const eb = el.getAttribute('data-b');
        const pairMatch = (ea === a && eb === b) || (ea === b && eb === a);
        el.classList.toggle('highlight', pairMatch);
      });
      nodeEls.forEach((el) => {
        const s = el.getAttribute('data-symbol');
        el.classList.toggle('highlight', s === a || s === b);
      });
      rowEls.forEach((el) => {
        const ra = el.getAttribute('data-a');
        const rb = el.getAttribute('data-b');
        const pairMatch = (ra === a && rb === b) || (ra === b && rb === a);
        el.classList.toggle('highlight', pairMatch);
      });
    };
    const applyLock = () => {
      if (!locked) return clearFocus();
      if (locked.type === 'symbol') focusSymbol(locked.a);
      else focusPair(locked.a, locked.b);
    };
    const toggleSymbolLock = (symbol) => {
      if (locked && locked.type === 'symbol' && locked.a === symbol) locked = null;
      else locked = { type: 'symbol', a: symbol, b: null };
      applyLock();
      panel.classList.toggle('corr-locked', !!locked);
    };
    const togglePairLock = (a, b) => {
      if (locked && locked.type === 'pair' && locked.a === a && locked.b === b) locked = null;
      else locked = { type: 'pair', a, b };
      applyLock();
      panel.classList.toggle('corr-locked', !!locked);
    };

    panel.querySelectorAll('[data-symbol]').forEach((el) => {
      el.addEventListener('mouseenter', () => focusSymbol(el.getAttribute('data-symbol')));
      el.addEventListener('mouseleave', () => { if (!locked) clearFocus(); });
      el.addEventListener('click', () => toggleSymbolLock(el.getAttribute('data-symbol')));
    });
    rowEls.forEach((el) => {
      el.addEventListener('mouseenter', () => focusPair(el.getAttribute('data-a'), el.getAttribute('data-b')));
      el.addEventListener('mouseleave', () => { if (!locked) clearFocus(); });
      el.addEventListener('click', () => togglePairLock(el.getAttribute('data-a'), el.getAttribute('data-b')));
    });
    panel.querySelector('.corr-graph-wrap')?.addEventListener('dblclick', () => {
      locked = null;
      panel.classList.remove('corr-locked');
      clearFocus();
    });
  }
}

// --- 7. Per-bot edge ---
class EdgePerBotPanel extends Panel {
  constructor() {
    super('fl-edge-per-bot', `/api/jarvis/edge_leaderboard?bot=${selection.botId}`, 'Per-Bot Edge');
    window.addEventListener('selection-changed', (e) => {
      this.endpoint = `/api/jarvis/edge_leaderboard?bot=${e.detail.botId}`;
      this.refresh();
    });
  }
  render(data) {
    const top = (data.top || []).concat(data.bottom || []);
    if (top.length === 0) { this.body.innerHTML = '<div class="text-zinc-500 text-sm">no per-bot edge data</div>'; return; }
    this.body.innerHTML = '<table class="w-full text-xs">' + top.map(s =>
      `<tr><td>${escapeHtml(s.school)}</td><td class="text-right">${formatR(s.avg_r)}</td></tr>`
    ).join('') + '</table>';
  }
}

// --- 8. Position reconciler ---
class PositionReconcilerPanel extends Panel {
  constructor() { super('fl-position-reconciler', '/api/positions/reconciler', 'Position Reconciler'); }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    const drifts = data.drifts || [];
    if (drifts.length === 0) { this.body.innerHTML = '<div class="text-emerald-400 text-sm">✓ no drift</div>'; return; }
    this.body.innerHTML = '<ul class="space-y-1 text-xs">' + drifts.map(d =>
      `<li class="text-red-400">${escapeHtml(d.bot)}: internal=${d.internal_qty} broker=${d.broker_qty}</li>`
    ).join('') + '</ul>';
  }
}

// --- 9. Risk ladder ---
class RiskLadderPanel extends Panel {
  constructor() { super('fl-risk-ladder', '/api/risk_gates', 'Risk Gate Ladder'); }
  render(data) {
    const bots = data.bots || [];
    if (bots.length === 0) { this.body.innerHTML = '<div class="text-zinc-500 text-sm">no data</div>'; return; }
    this.body.innerHTML = '<table class="w-full text-xs"><tr class="text-zinc-500"><th class="text-left">Bot</th><th>Latch</th><th>Drawdown</th><th>Cap</th><th>Threshold</th></tr>' +
      bots.map(b => {
        const latchCls = b.latch_state === 'tripped' ? 'text-red-400'
                       : b.latch_state === 'armed'   ? 'text-amber-400' : 'text-emerald-400';
        return `<tr><td>${escapeHtml(b.bot_id)}</td><td class="${latchCls}">${escapeHtml(b.latch_state || 'unknown')}</td><td>${(b.dd_pct ?? 0).toFixed(1)}%</td><td>${formatPct(b.cap_mult ?? 1)}</td><td>${(b.kill_threshold_pct ?? 8).toFixed(1)}%</td></tr>`;
      }).join('') + '</table>' +
      '<div class="text-xs text-zinc-500 mt-2">Ladder logic: healthy → armed (risk throttled) → tripped (no new risk).</div>';
  }
}

// --- 10. Lifecycle controls ---
class ControlsPanel extends Panel {
  constructor() {
    super('fl-controls', null, 'Lifecycle Controls');
    // ControlsPanel has no endpoint, so it's not poller-driven.
    // Render immediately so the lifecycle buttons are present from page load.
    if (this.body) this.render();
  }
  refresh() { this.render(); }
  render() {
    const id = selection.botId;
    this.body.innerHTML = `
      <div class="text-xs text-zinc-500 mb-2">acting on: <span class="text-zinc-100 font-mono">${escapeHtml(id)}</span></div>
      <div class="grid grid-cols-2 gap-2">
        <button data-act="pause"   class="bg-zinc-700 hover:bg-zinc-600 rounded py-2 text-sm">pause</button>
        <button data-act="resume"  class="bg-zinc-700 hover:bg-zinc-600 rounded py-2 text-sm">resume</button>
        <button data-act="flatten" class="bg-amber-700 hover:bg-amber-600 rounded py-2 text-sm">flatten</button>
        <button data-act="kill"    class="bg-red-700 hover:bg-red-600 rounded py-2 text-sm">kill</button>
      </div>`;
    this.body.querySelectorAll('button[data-act]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const act = btn.dataset.act;
        const id = selection.botId;
        if (act === 'flatten' || act === 'kill') {
          if (!confirm(`${act.toUpperCase()} ${id} — are you sure?`)) return;
          const phrase = `${act} ${id}`.toLowerCase();
          const typed = (prompt(`Type "${phrase}" to confirm`) || '').trim().toLowerCase();
          if (typed !== phrase) {
            notify(`${act.toUpperCase()} cancelled (phrase mismatch)`, 'error');
            return;
          }
          const now = Date.now();
          if (now - lastDangerActionAt < ACTION_COOLDOWN_MS) {
            notify(`${act.toUpperCase()} blocked by cooldown`, 'error');
            return;
          }
          lastDangerActionAt = now;
        }
        try {
          const r = await authedPost(`/api/bot/${id}/${act}`, {},
            { stepUpReason: `${act.toUpperCase()} ${id} requires step-up.` });
          if (!r) return;
          if (!r.ok) {
            const body = await r.json().catch(() => ({}));
            const code = body?.detail?.error_code || `http_${r.status}`;
            notify(`${act.toUpperCase()} ${id} failed (${code})`, 'error');
            return;
          }
          notify(`${act.toUpperCase()} ${id} submitted`, 'success');
          poller._tick();
        } catch (e) {
          console.error(`${act} ${id} failed`, e);
          notify(`${act.toUpperCase()} ${id} failed (${e.message})`, 'error');
        }
      });
    });
    window.addEventListener('selection-changed', () => this.render());
  }
}

// --- 11. Live fill tape (SSE) ---
class FillTapeManager {
  constructor() {
    this.container = document.getElementById('fl-fill-tape-rows');
    this.rows = [];
    this.seen = new Set();
    this._bootstrap();
    this._pollFallback();
    liveStream.on('fill', (f) => this.add(f));
  }
  _key(f) {
    return `${f?.ts ?? ''}|${f?.bot ?? ''}|${f?.symbol ?? ''}|${f?.side ?? ''}|${f?.price ?? ''}|${f?.qty ?? ''}`;
  }
  async _bootstrap() {
    try {
      const ts = Date.now();
      const r = await fetch(`/api/live/fills?limit=30&_t=${ts}`, { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      const rows = Array.isArray(body.fills) ? body.fills : [];
      rows.reverse().forEach((f) => this.add(f));
    } catch (_e) {
      // ignore bootstrap failures; SSE/poller fallback still works
    }
  }
  _pollFallback() {
    setInterval(async () => {
      try {
        const ts = Date.now();
        const r = await fetch(`/api/live/fills?limit=30&_t=${ts}`, { credentials: 'same-origin', cache: 'no-store' });
        if (!r.ok) return;
        const body = await r.json();
        const rows = Array.isArray(body.fills) ? body.fills : [];
        rows.reverse().forEach((f) => this.add(f));
      } catch (_e) {
        // ignore transient network errors
      }
    }, 5000);
  }
  add(f) {
    const key = this._key(f);
    if (this.seen.has(key)) return;
    this.seen.add(key);
    this.rows.unshift(f);
    if (this.rows.length > 30) this.rows.length = 30;
    if (this.seen.size > 300) {
      this.seen = new Set(this.rows.map((x) => this._key(x)));
    }
    if (!this.container) return;
    const formatTapeTime = (ts) => {
      const d = new Date(ts || 0);
      if (Number.isNaN(d.getTime())) return '--:--:--';
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      return `${hh}:${mm}:${ss}`;
    };
    this.container.innerHTML = this.rows.map(f => {
      const cls = (f.realized_r ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400';
      return `<span class="fill-chip ${cls}">${formatTapeTime(f.ts)} · ${escapeHtml(f.bot)}/${escapeHtml(f.symbol)} · ${escapeHtml(String(f.side || '').toUpperCase())} @ ${formatNumber(f.price)} · ${formatR(f.realized_r)}</span>`;
    }).join('');
    window.dispatchEvent(new CustomEvent('eta-trade-update', {
      detail: {
        at: Date.now(),
        fill: f || null,
      },
    }));
  }
}

// --- 12. Health badges ---
class HealthBadgesPanel extends Panel {
  constructor() { super('fl-health-badges', '/api/bot-fleet', 'Bot Health Badges'); }
  render(data) {
    const bots = data.bots || [];
    if (bots.length === 0) { this.body.innerHTML = '<div class="text-zinc-500 text-sm">no data</div>'; return; }
    this.body.innerHTML = '<div class="grid grid-cols-2 gap-2 text-xs">' + bots.map(b => {
      return `<div class="border border-zinc-800 rounded p-2">
        <div class="font-mono text-zinc-200 mb-1">${escapeHtml(b.name)}</div>
        <div>${b.jarvis_attached ? '✓ jarvis' : '✗ jarvis'}</div>
        <div>${b.journal_attached ? '✓ journal' : '✗ journal'}</div>
        <div>${b.online_learner_attached ? '✓ learner' : '○ learner'}</div>
      </div>`;
    }).join('') + '</div>';
  }
}

// --- 13. Fill quality analytics ---
class FillQualityPanel extends Panel {
  constructor() { super('fl-fill-quality', '/api/live/fills?limit=80', 'Fill Quality'); }
  async refresh() {
    this.setLoading();
    const tryEndpoints = [
      `/api/live/fills?limit=80&_t=${Date.now()}`,
      `/api/bot-fleet/${encodeURIComponent(selection.botId)}`,
    ];
    let payload = null;
    for (const endpoint of tryEndpoints) {
      try {
        const resp = await fetch(endpoint, { credentials: 'same-origin', cache: 'no-store' });
        if (!resp.ok) continue;
        const data = await resp.json();
        if (endpoint.startsWith('/api/live/fills')) {
          payload = { fills: Array.isArray(data.fills) ? data.fills : [] };
        } else {
          payload = { fills: Array.isArray(data.recent_fills) ? data.recent_fills : [] };
        }
        this.endpoint = endpoint;
        break;
      } catch {
        // Try next endpoint.
      }
    }
    if (!payload) {
      this.setError('fill_quality_unavailable');
      return;
    }
    this.render(payload);
    this.element.classList.remove('loading', 'error', 'stale');
    this.lastRefreshAt = Date.now();
    this.element.dataset.lastRefreshAt = String(this.lastRefreshAt);
    this.updateRefreshLabel();
  }
  render(data) {
    const fills = Array.isArray(data.fills) ? data.fills : [];
    if (!fills.length) {
      this.body.innerHTML = '<div class="text-zinc-500 text-sm">no fills available</div>';
      return;
    }
    const rs = fills.map((f) => Number(f.realized_r || 0)).filter((n) => Number.isFinite(n));
    const wins = rs.filter((r) => r > 0).length;
    const losses = rs.filter((r) => r < 0).length;
    const avg = rs.length ? (rs.reduce((a, b) => a + b, 0) / rs.length) : 0;
    this.body.innerHTML = `
      <div class="grid grid-cols-3 gap-2 text-xs">
        <div class="metric-card p-2"><div class="metric-label">Samples</div><div class="metric-value">${rs.length}</div></div>
        <div class="metric-card p-2"><div class="metric-label">Win/Loss</div><div class="metric-value">${wins}/${losses}</div></div>
        <div class="metric-card p-2"><div class="metric-label">Avg R</div><div class="metric-value ${avg >= 0 ? 'text-emerald-400' : 'text-red-400'}">${formatR(avg)}</div></div>
      </div>
      <div class="mt-2 text-xs text-zinc-500">Latest: ${escapeHtml(String(fills[0]?.bot || '—'))} ${escapeHtml(String(fills[0]?.symbol || '—'))} ${formatR(fills[0]?.realized_r)}</div>`;
  }
}

// --- 14. Risk simulator ---
class RiskSimPanel extends Panel {
  constructor() { super('fl-risk-sim', '/api/risk_gates', 'Risk Simulator'); }
  render(data) {
    const bots = Array.isArray(data.bots) ? data.bots : [];
    if (!bots.length) {
      this.body.innerHTML = '<div class="text-zinc-500 text-sm">no risk state</div>';
      return;
    }
    const nearest = bots
      .map((b) => ({
        id: b.bot_id,
        dd: Number(b.dd_pct || 0),
        th: Number(b.kill_threshold_pct || 8),
        headroom: Number(b.kill_threshold_pct || 8) - Number(b.dd_pct || 0),
      }))
      .sort((a, b) => a.headroom - b.headroom)[0];
    const shock = 1.2;
    const projected = nearest.dd + shock;
    const trip = projected >= nearest.th;
    this.body.innerHTML = `
      <div class="text-xs text-zinc-400 mb-2">Scenario: +${shock.toFixed(1)}% adverse move</div>
      <div class="metric-card p-2 mb-2">
        <div class="metric-label">Nearest Gate</div>
        <div class="metric-value">${escapeHtml(nearest.id)} · ${nearest.dd.toFixed(1)} / ${nearest.th.toFixed(1)}%</div>
      </div>
      <div class="metric-card p-2">
        <div class="metric-label">Projected DD</div>
        <div class="metric-value ${trip ? 'text-red-400' : 'text-amber-300'}">${projected.toFixed(1)}% ${trip ? '(trip risk)' : '(watch)'}</div>
      </div>`;
  }
}

// --- 15. Performance OS ---
class PerformanceOSPanel extends Panel {
  constructor() { super('fl-performance-os', '/api/bot-fleet', 'Performance OS'); }
  render(data) {
    const bots = Array.isArray(data.bots) ? data.bots : [];
    if (!bots.length) {
      this.body.innerHTML = '<div class="text-zinc-500 text-sm">no performance data</div>';
      return;
    }
    const sorted = [...bots].sort((a, b) => Number(b.todays_pnl || 0) - Number(a.todays_pnl || 0));
    const top = sorted[0];
    const bottom = sorted[sorted.length - 1];
    const total = sorted.reduce((acc, b) => acc + Number(b.todays_pnl || 0), 0);
    const groups = {};
    sorted.forEach((b) => {
      const key = String(b.venue || 'unknown');
      if (!groups[key]) groups[key] = { pnl: 0, bots: 0 };
      groups[key].pnl += Number(b.todays_pnl || 0);
      groups[key].bots += 1;
    });
    const groupRows = Object.entries(groups)
      .sort((a, b) => b[1].pnl - a[1].pnl)
      .map(([name, g]) => `<div class="text-xs">${escapeHtml(name)} · ${g.bots} bots · <span class="${g.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}">${formatNumber(g.pnl)}</span></div>`)
      .join('');
    this.body.innerHTML = `
      <div class="metric-card p-2 mb-2"><div class="metric-label">Fleet Day PnL</div><div class="metric-value ${total >= 0 ? 'text-emerald-400' : 'text-red-400'}">${formatNumber(total)}</div></div>
      <div class="text-xs mb-1">Top: <span class="text-emerald-400">${escapeHtml(top.name)}</span> ${formatNumber(top.todays_pnl)}</div>
      <div class="text-xs mb-2">Bottom: <span class="text-red-400">${escapeHtml(bottom.name)}</span> ${formatNumber(bottom.todays_pnl)}</div>
      <div class="text-xs text-zinc-500 mb-1">Account/Group Drill (by venue)</div>
      <div class="space-y-1">${groupRows}</div>`;
  }
}

// --- Initialize all 12 ---
onAuthenticated(() => {
  const panels = [
    new RosterPanel(),
    new DrilldownPanel(),
    new EquityCurvePanel(),
    new DrawdownPanel(),
    new SageEffectPanel(),
    new CorrelationPanel(),
    new EdgePerBotPanel(),
    new PositionReconcilerPanel(),
    new RiskLadderPanel(),
    new ControlsPanel(),
    new FillQualityPanel(),
    new RiskSimPanel(),
    new PerformanceOSPanel(),
    new HealthBadgesPanel(),
  ];
  panels.forEach(p => { if (p.endpoint) poller.register(p); });
  new FillTapeManager();
  // Force immediate refresh when user returns focus; mitigates background timer throttling.
  window.addEventListener('focus', () => {
    panels.forEach((p) => p.refresh?.().catch(() => {}));
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) panels.forEach((p) => p.refresh?.().catch(() => {}));
  });
});
