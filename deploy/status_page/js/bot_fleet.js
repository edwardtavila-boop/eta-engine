// eta_engine/deploy/status_page/js/bot_fleet.js
// 12 fleet panels + lifecycle button handlers.
// Wave-7 dashboard, 2026-04-27.

import { Panel, formatPct, formatR, formatTime, formatNumber, escapeHtml,
         selection, selectBot } from '/js/panels.js';
import { liveStream, poller } from '/js/live.js';
import { onAuthenticated, authedPost } from '/js/auth.js';

// --- 1. Roster table ---
class RosterPanel extends Panel {
  constructor() { super('fl-roster', '/api/bot-fleet', 'Bot Fleet Roster'); }
  render(data) {
    const bots = data.bots || [];
    if (bots.length === 0) {
      this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning || 'no bots reporting')}</div>`;
      return;
    }
    this.body.innerHTML = `<table class="w-full text-xs"><thead class="text-zinc-500">
      <tr><th class="text-left">bot</th><th class="text-left">symbol</th><th class="text-left">tier</th><th class="text-left">venue</th><th class="text-left">status</th><th class="text-right">PnL</th><th class="text-right">open</th><th class="text-left">last sig</th></tr>
      </thead><tbody>${bots.map(b => {
        const statusCls = b.status === 'running' ? 'text-emerald-400'
                        : b.status === 'paused' ? 'text-amber-400'
                        : b.status === 'killed' ? 'text-red-400' : 'text-zinc-400';
        const pnlCls = (b.todays_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400';
        const isSel = selection.botId === b.name ? 'bg-zinc-800' : '';
        return `<tr class="cursor-pointer hover:bg-zinc-800 ${isSel}" data-bot-id="${escapeHtml(b.name)}" data-symbol="${escapeHtml(b.symbol)}">
          <td>${escapeHtml(b.name)}</td>
          <td>${escapeHtml(b.symbol)}</td>
          <td>${escapeHtml(b.tier)}</td>
          <td>${escapeHtml(b.venue)}</td>
          <td class="${statusCls}">${escapeHtml(b.status)}</td>
          <td class="text-right ${pnlCls}">${formatNumber(b.todays_pnl)}</td>
          <td class="text-right">${b.open_positions ?? 0}</td>
          <td class="text-zinc-500">${formatTime(b.last_signal_ts)}</td>
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
    super('fl-drilldown', `/api/bot-fleet/${selection.botId}`, 'Drill-Down');
    window.addEventListener('selection-changed', (e) => {
      this.endpoint = `/api/bot-fleet/${e.detail.botId}`;
      this.refresh();
    });
  }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">no bot selected</div>`; return; }
    const fills = data.recent_fills || [];
    const verdicts = data.recent_verdicts || [];
    this.body.innerHTML = `
      <div class="text-xs text-zinc-500 mb-1">recent fills</div>
      <div class="space-y-1 text-xs font-mono mb-3">${fills.slice(0, 5).map(f =>
        `<div>${formatTime(f.ts)} ${escapeHtml(f.side)} ${formatNumber(f.price)} qty=${f.qty} ${formatR(f.realized_r)}</div>`
      ).join('') || '<div class="text-zinc-600">none</div>'}</div>
      <div class="text-xs text-zinc-500 mb-1">recent verdicts</div>
      <div class="space-y-1 text-xs">${verdicts.slice(0, 5).map(v =>
        `<div><span class="text-emerald-400">${escapeHtml(v.verdict)}</span> ${escapeHtml(v.sage_modulation || '')}</div>`
      ).join('') || '<div class="text-zinc-600">none</div>'}</div>`;
  }
}

// --- 3. Equity curve (Chart.js) — per-bot + timeframe selector + KPI cards ---
class EquityCurvePanel extends Panel {
  constructor() {
    super('fl-equity-curve', '/api/equity?range=1d', 'Fleet Equity');
    this.chart = null;
    this.selectedBot = null;   // null = fleet aggregate
    this.range = '1d';
    this._buildShell();

    // Auto-switch to selected bot when user clicks roster row
    window.addEventListener('selection-changed', (e) => {
      this.selectedBot = e.detail.botId;
      this._updateEndpoint();
      this.refresh();
    });
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
        <div class="bg-zinc-800 rounded p-2"><div class="text-[10px] text-zinc-500 uppercase">current</div><div data-k="current_equity" class="text-sm font-mono">—</div></div>
        <div class="bg-zinc-800 rounded p-2"><div class="text-[10px] text-zinc-500 uppercase">today</div><div data-k="today_pnl" class="text-sm font-mono">—</div></div>
        <div class="bg-zinc-800 rounded p-2"><div class="text-[10px] text-zinc-500 uppercase">week</div><div data-k="week_pnl" class="text-sm font-mono">—</div></div>
        <div class="bg-zinc-800 rounded p-2"><div class="text-[10px] text-zinc-500 uppercase">month</div><div data-k="month_pnl" class="text-sm font-mono">—</div></div>
      </div>

      <div style="height: 220px"><canvas id="eq-chart"></canvas></div>`;

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
      const r = await fetch('/api/bot-fleet', { credentials: 'same-origin' });
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
    if (this.selectedBot) params.set('bot', this.selectedBot);
    this.endpoint = `/api/equity?${params.toString()}`;
    // Update title
    if (this.element) {
      const t = this.element.querySelector('.panel-title');
      if (t) t.textContent = this.selectedBot
        ? `Equity — ${this.selectedBot} (${this.range.toUpperCase()})`
        : `Fleet Equity (${this.range.toUpperCase()})`;
    }
    // Sync the dropdown
    const sel = this.body?.querySelector('#eq-bot-select');
    if (sel) sel.value = this.selectedBot || '';
  }

  render(data) {
    if (data._warning) {
      this._renderKPIs({});
      this._renderChart([]);
      return;
    }
    this._renderKPIs(data.summary || {});
    this._renderChart(data.series || []);
  }

  _renderKPIs(summary) {
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
      if (key === 'current_equity') {
        el.textContent = val !== null && val !== undefined ? `$${formatNumber(val)}` : '—';
        el.className = 'text-sm font-mono text-zinc-100';
      } else {
        el.textContent = fmt(val);
        el.className = `text-sm font-mono ${pnlClass(val)}`;
      }
    });
  }

  _renderChart(series) {
    if (this.chart) { this.chart.destroy(); this.chart = null; }
    let canvas = this.body?.querySelector('#eq-chart');
    // If someone replaced canvas with the empty-state div, restore it
    if (!canvas) {
      const wrap = document.createElement('div');
      wrap.style.height = '220px';
      wrap.innerHTML = '<canvas id="eq-chart"></canvas>';
      this.body.appendChild(wrap);
      canvas = this.body.querySelector('#eq-chart');
    }
    if (!series.length) {
      const parent = canvas.parentElement;
      parent.innerHTML = '<div class="text-zinc-500 text-sm py-12 text-center">no data for this range</div>';
      return;
    }
    const ctx = canvas.getContext('2d');
    const startEq = series[0].equity;
    const endEq   = series[series.length - 1].equity;
    const lineColor = endEq >= startEq ? '#10b981' : '#ef4444';
    this.chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: series.map(p => p.ts),
        datasets: [{
          label: 'equity',
          data: series.map(p => p.equity),
          borderColor: lineColor,
          backgroundColor: lineColor + '22',
          tension: 0.2,
          pointRadius: 0,
          fill: true,
        }],
      },
      options: {
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false },
          y: { ticks: { color: '#a1a1aa', font: { size: 10 } } },
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
    if (throttles.length === 0) { this.body.innerHTML = '<div class="text-emerald-400 text-sm">✓ no active throttles</div>'; return; }
    this.body.innerHTML = '<ul class="space-y-1 text-xs font-mono">' + throttles.map(t =>
      `<li>${escapeHtml(t.symbol_a)}↔${escapeHtml(t.symbol_b)} ρ=${(t.rho ?? 0).toFixed(2)} cap=${formatPct(t.cap_mult)}</li>`
    ).join('') + '</ul>';
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
    this.body.innerHTML = '<table class="w-full text-xs"><tr class="text-zinc-500"><th class="text-left">bot</th><th>latch</th><th>DD</th><th>cap</th></tr>' +
      bots.map(b => {
        const latchCls = b.latch_state === 'tripped' ? 'text-red-400'
                       : b.latch_state === 'armed'   ? 'text-amber-400' : 'text-emerald-400';
        return `<tr><td>${escapeHtml(b.bot_id)}</td><td class="${latchCls}">${escapeHtml(b.latch_state || 'unknown')}</td><td>${(b.dd_pct ?? 0).toFixed(1)}%</td><td>${formatPct(b.cap_mult ?? 1)}</td></tr>`;
      }).join('') + '</table>';
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
        }
        try {
          const r = await authedPost(`/api/bot/${id}/${act}`, {},
            { stepUpReason: `${act.toUpperCase()} ${id} requires step-up.` });
          if (r && r.ok) console.info(`${act} ${id} OK`);
        } catch (e) { console.error(`${act} ${id} failed`, e); }
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
    liveStream.on('fill', (f) => this.add(f));
  }
  add(f) {
    this.rows.unshift(f);
    if (this.rows.length > 30) this.rows.length = 30;
    if (!this.container) return;
    this.container.innerHTML = this.rows.map(f => {
      const cls = (f.realized_r ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400';
      return `<span class="px-2 py-1 bg-zinc-800 rounded ${cls}">${escapeHtml(f.bot)}/${escapeHtml(f.symbol)} ${escapeHtml(f.side)} ${formatNumber(f.price)} ${formatR(f.realized_r)}</span>`;
    }).join('');
  }
}

// --- 12. Health badges ---
class HealthBadgesPanel extends Panel {
  constructor() { super('fl-health-badges', '/api/bot-fleet', 'Bot Health Badges'); }
  render(data) {
    const bots = data.bots || [];
    if (bots.length === 0) { this.body.innerHTML = '<div class="text-zinc-500 text-sm">no data</div>'; return; }
    this.body.innerHTML = '<div class="grid grid-cols-2 gap-2 text-xs">' + bots.map(b => {
      const beat = formatTime(b.heartbeat_ts);
      return `<div class="border border-zinc-800 rounded p-2">
        <div class="font-mono text-zinc-200 mb-1">${escapeHtml(b.name)}</div>
        <div class="text-zinc-500">heartbeat: ${beat}</div>
        <div>${b.jarvis_attached ? '✓ jarvis' : '✗ jarvis'}</div>
        <div>${b.journal_attached ? '✓ journal' : '✗ journal'}</div>
        <div>${b.online_learner_attached ? '✓ learner' : '○ learner'}</div>
      </div>`;
    }).join('') + '</div>';
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
    new HealthBadgesPanel(),
  ];
  panels.forEach(p => { if (p.endpoint) poller.register(p); });
  new FillTapeManager();
});
