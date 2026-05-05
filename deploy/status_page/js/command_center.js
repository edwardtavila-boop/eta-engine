// eta_engine/deploy/status_page/js/command_center.js
// JARVIS panels for the ETA live dashboard view.
// Wave-7 dashboard, 2026-04-27.

import { Panel, formatPct, formatR, formatTime, escapeHtml, selection } from '/js/panels.js';
import { liveStream, poller } from '/js/live.js';
import { onAuthenticated, authedPost } from '/js/auth.js';

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

function setAlertDock(items) {
  const body = document.getElementById('alert-dock-body');
  const top = document.getElementById('top-alerts');
  const rows = (items || []).slice(0, 6);
  if (top) {
    const high = rows.filter((x) => x.severity === 'high').length;
    const medium = rows.filter((x) => x.severity === 'medium').length;
    const low = rows.filter((x) => x.severity === 'low').length;
    top.innerHTML = `<span>alerts</span><span class="alert-pill alert-high">${high}H</span><span class="alert-pill alert-medium">${medium}M</span><span class="alert-pill alert-low">${low}L</span>`;
  }
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = '<div class="text-zinc-500">No active alerts.</div>';
    window.dispatchEvent(new CustomEvent('eta-alerts-updated', { detail: { items: [] } }));
    return;
  }
  body.innerHTML = rows.map((r) =>
    `<div class="flex items-start justify-between gap-2 py-1">
      <span class="text-zinc-200">${escapeHtml(r.label)}</span>
      <span class="alert-pill alert-${escapeHtml(r.severity)}">${escapeHtml(r.severity)}</span>
    </div>`,
  ).join('');
  window.dispatchEvent(new CustomEvent('eta-alerts-updated', { detail: { items: rows } }));
}

// --- 1. Live verdict stream (SSE) ---
class VerdictStreamPanel extends Panel {
  constructor() {
    super('cc-verdict-stream', null, 'Live Verdict Stream');
    this.rows = [];
    if (this.body) this.body.innerHTML = '<div data-list class="space-y-1 text-xs font-mono max-h-96 overflow-y-auto"></div>';
    this.list = this.body?.querySelector('[data-list]');
    liveStream.on('verdict', (v) => this.add(v));
  }
  add(v) {
    this.rows.unshift(v);
    if (this.rows.length > 50) this.rows.length = 50;
    this.repaint();
  }
  repaint() {
    if (!this.list) return;
    this.list.innerHTML = this.rows.map(v => {
      const verdict = v?.response?.verdict || '?';
      const cls = verdict === 'APPROVED' ? 'text-emerald-400'
                : verdict === 'CONDITIONAL' ? 'text-amber-400'
                : verdict === 'DENIED' ? 'text-red-400' : 'text-zinc-400';
      const sym = v?.request?.payload?.symbol || '?';
      const action = v?.request?.action || '?';
      const sage = (v?.response?.conditions || []).filter(c => c.startsWith('v22_')).join(',');
      return `<div><span class="text-zinc-500">${escapeHtml(formatTime(v.ts))}</span> <span class="${cls}">${escapeHtml(verdict)}</span> ${escapeHtml(sym)} ${escapeHtml(action)} ${sage ? `<span class="text-purple-400">[${escapeHtml(sage)}]</span>` : ''}</div>`;
    }).join('');
  }
  refresh() { /* SSE-driven; no poll */ }
}

// --- 2. Sage explain ---
class SageExplainPanel extends Panel {
  constructor() {
    super('cc-sage-explain', `/api/jarvis/sage_explain?symbol=${selection.symbol}&side=long`, 'Sage Explain');
    window.addEventListener('selection-changed', (e) => {
      this.endpoint = `/api/jarvis/sage_explain?symbol=${e.detail.symbol}&side=long`;
      this.refresh();
    });
  }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    if (data.error_code) { this.setError(data.error_code); return; }
    this.body.innerHTML = `
      <div class="text-sm leading-relaxed text-zinc-200">${escapeHtml(data.narrative || '—')}</div>
      <div class="text-xs text-zinc-500 mt-2 font-mono">${escapeHtml(data.summary_line || '')}</div>`;
  }
}

// --- 3. Sage health alerts ---
class SageHealthPanel extends Panel {
  constructor() { super('cc-sage-health', '/api/jarvis/health', 'Sage Health'); }
  render(data) {
    const issues = data.issues || [];
    const quantum = data.quantum || {};
    const authority = data.policy_authority || 'JARVIS';
    const quantumLine = `<div class="text-xs text-zinc-500 mt-2">Authority: ${escapeHtml(authority)} | Quantum: ${escapeHtml(quantum.status || 'idle')} | fallbacks ${Number(quantum.recent_fallbacks || 0)} | cost $${Number(quantum.recent_cost_estimate_usd || 0).toFixed(4)}</div>`;
    if (issues.length === 0) {
      this.body.innerHTML = '<div class="text-emerald-400 text-sm">◉ all schools healthy</div>';
      setAlertDock([]);
      this.body.innerHTML += quantumLine;
      return;
    }
    const dockRows = issues.slice(0, 8).map((i) => ({
      label: `${i.school} neutral ${formatPct(i.neutral_rate)}`,
      severity: i.severity === 'critical' ? 'high' : 'medium',
    }));
    setAlertDock(dockRows);
    this.body.innerHTML = '<ul class="space-y-1 text-xs">' + issues.map(i => {
      const cls = i.severity === 'critical' ? 'text-red-400' : 'text-amber-400';
      return `<li><span class="${cls}">◉</span> ${escapeHtml(i.school)} ${formatPct(i.neutral_rate)} neutral (${i.n_consultations})</li>`;
    }).join('') + '</ul>';
    this.body.innerHTML += quantumLine;
  }
}

// --- 4. Disagreement heatmap ---
class DisagreementHeatmapPanel extends Panel {
  constructor() {
    super('cc-disagreement-heatmap', `/api/jarvis/sage_disagreement_heatmap?symbol=${selection.symbol}`, 'School Disagreement');
    window.addEventListener('selection-changed', (e) => {
      this.endpoint = `/api/jarvis/sage_disagreement_heatmap?symbol=${e.detail.symbol}`;
      this.refresh();
    });
  }
  render(data) {
    const schools = Object.entries(data.per_school || {});
    if (schools.length === 0) {
      this.body.innerHTML = `<div class="text-zinc-500 text-sm">No disagreement data yet for ${escapeHtml(selection.symbol)}. Waiting for fresh Sage consultations.</div>`;
      return;
    }
    const rows = schools
      .sort((a, b) => Number(a[1].aligned_with_composite) - Number(b[1].aligned_with_composite))
      .map(([name, row]) => {
        const aligned = !!row.aligned_with_composite;
        const bias = String(row.bias || 'neutral').toUpperCase();
        const conv = Number(row.conviction || 0);
        const tone = aligned ? 'text-emerald-400' : 'text-red-400';
        const chip = aligned ? 'Aligned' : 'Disagreeing';
        return `<tr>
          <td class="py-1">${escapeHtml(name)}</td>
          <td class="py-1 text-zinc-300">${escapeHtml(bias)}</td>
          <td class="py-1 text-right ${tone}">${formatPct(conv)}</td>
          <td class="py-1 text-right ${tone}">${chip}</td>
        </tr>`;
      }).join('');
    const clashes = (data.named_clashes || []).slice(0, 3).map(c =>
      `<li class="text-zinc-400">${escapeHtml(c.name)} — ${escapeHtml(c.interpretation || 'n/a')}</li>`,
    ).join('');
    this.body.innerHTML = `
      <div class="text-xs text-zinc-500 mb-2">Composite bias: <span class="text-zinc-200">${escapeHtml(String(data.composite_bias || '—').toUpperCase())}</span></div>
      <table class="w-full text-xs">
        <tr class="text-zinc-500"><th class="text-left">School</th><th class="text-left">Bias</th><th class="text-right">Conviction</th><th class="text-right">Status</th></tr>
        ${rows}
      </table>
      <ul class="mt-2 text-xs space-y-1">${clashes || '<li class="text-zinc-600">No named clashes in the current window.</li>'}</ul>`;
  }
}

// --- 5. Stress / mood ---
class StressMoodPanel extends Panel {
  constructor() { super('cc-stress-mood', '/api/jarvis/summary', 'Stress & Session');
  }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    const stress = data.stress_composite ?? 0;
    const phase = data.session_phase || '—';
    const kill = data.kill_switch_state || 'unknown';
    const killCls = kill === 'tripped' ? 'text-red-400' : kill === 'armed' ? 'text-amber-400' : 'text-emerald-400';
    this.body.innerHTML = `
      <div class="flex items-center justify-between mb-2">
        <span class="text-xs text-zinc-500">Stress Composite</span>
        <span class="text-2xl font-mono">${formatPct(stress)}</span>
      </div>
      <div class="w-full h-2 bg-zinc-800 rounded mb-2 overflow-hidden"><div class="h-full bg-cyan-500" style="width:${Math.min(100, Math.max(0, stress * 100))}%"></div></div>
      <div class="text-xs text-zinc-500">Session Phase: <span class="text-zinc-100">${escapeHtml(phase)}</span></div>
      <div class="text-xs text-zinc-500">Kill-Switch State: <span class="${killCls}">${escapeHtml(kill)}</span></div>`;
    const stressAlerts = [];
    if (Number(stress) >= 0.7) stressAlerts.push({ label: `Stress composite elevated (${formatPct(stress)})`, severity: 'high' });
    else if (Number(stress) >= 0.5) stressAlerts.push({ label: `Stress composite watch (${formatPct(stress)})`, severity: 'medium' });
    if (kill === 'armed') stressAlerts.push({ label: 'Kill switch armed', severity: 'medium' });
    if (kill === 'tripped') stressAlerts.push({ label: 'Kill switch tripped', severity: 'high' });
    if (stressAlerts.length) setAlertDock(stressAlerts);
    const topStress = document.getElementById('top-stress');
    if (topStress) topStress.innerHTML = `<span>stress</span><span class="${Number(stress) >= 0.7 ? 'text-red-400' : Number(stress) >= 0.5 ? 'text-amber-300' : 'text-emerald-400'}">${formatPct(stress)}</span>`;
  }
}

// --- 5b. Operator blockers ---
class OperatorQueuePanel extends Panel {
  constructor() { super('cc-operator-queue', '/api/jarvis/operator_queue', 'Operator Blockers'); }
  render(data) {
    const summary = data.summary || {};
    const blockers = data.top_blockers || [];
    const blocked = Number(summary.BLOCKED || 0);
    const observed = Number(summary.OBSERVED || 0);
    const unknown = Number(summary.UNKNOWN || 0);
    const top = document.getElementById('top-operator-queue');
    if (top) {
      const cls = blocked > 0 ? 'text-amber-300' : 'text-emerald-300';
      top.innerHTML = `<span>ops</span><span class="${cls}">${blocked} blocked</span>`;
    }
    if (data.error) {
      this.body.innerHTML = `<div class="text-amber-300 text-sm">Operator queue degraded: ${escapeHtml(data.error)}</div>`;
      return;
    }
    const actions = data.next_actions || [];
    const rows = blockers.slice(0, 5).map((item) => {
      const sev = String(item?.evidence?.overall_severity || '').toUpperCase();
      const sevChip = sev ? `<span class="text-amber-300">${escapeHtml(sev)}</span>` : '';
      return `<li class="border-b border-zinc-800/70 pb-2">
        <div class="flex items-center justify-between gap-2">
          <span class="font-mono text-cyan-300">${escapeHtml(item.op_id || 'OP')}</span>
          ${sevChip}
        </div>
        <div class="text-zinc-100">${escapeHtml(item.title || '')}</div>
        <div class="text-zinc-500">${escapeHtml(item.detail || item.where || '')}</div>
      </li>`;
    }).join('');
    const actionRows = actions.slice(0, 3).map((action) =>
      `<li class="font-mono text-zinc-300">${escapeHtml(action)}</li>`,
    ).join('');
    this.body.innerHTML = `
      <div class="grid grid-cols-3 gap-2 text-xs mb-3">
        <div><div class="text-zinc-500">blocked</div><div class="text-amber-300 text-lg font-mono">${blocked}</div></div>
        <div><div class="text-zinc-500">observed</div><div class="text-cyan-300 text-lg font-mono">${observed}</div></div>
        <div><div class="text-zinc-500">unknown</div><div class="text-zinc-300 text-lg font-mono">${unknown}</div></div>
      </div>
      <ul class="space-y-2 text-xs">${rows || '<li class="text-emerald-300">No active operator blockers.</li>'}</ul>
      ${actionRows ? `<div class="text-xs text-zinc-500 mt-3 mb-1">next actions</div><ul class="space-y-1 text-xs">${actionRows}</ul>` : ''}`;
  }
}

// --- 5c. Bot strategy readiness ---
class BotStrategyReadinessPanel extends Panel {
  constructor() { super('cc-bot-strategy-readiness', '/api/jarvis/bot_strategy_readiness', 'Bot Strategy Readiness'); }
  render(data) {
    const top = document.getElementById('top-bot-readiness');
    const summary = data.summary || {};
    const lanes = summary.launch_lanes || {};
    const blockedData = Number(summary.blocked_data || lanes.blocked_data || 0);
    const paperReady = Number(summary.can_paper_trade || 0);
    if (top) {
      if (data.error || data.status !== 'ready') {
        top.setAttribute('data-readiness', 'degraded');
        top.textContent = `bots: ${data.status || 'degraded'}`;
      } else {
        top.setAttribute('data-readiness', blockedData > 0 ? 'blocked' : 'ready');
        top.textContent = `bots: ${paperReady} paper ready / ${blockedData} blocked`;
      }
    }
    if (data.error) {
      this.body.innerHTML = `<div class="text-amber-300 text-sm">Readiness snapshot degraded: ${escapeHtml(data.error)}</div>`;
      return;
    }
    const actions = data.top_actions || [];
    const laneRows = Object.entries(lanes).map(([lane, count]) =>
      `<div><div class="text-zinc-500">${escapeHtml(lane)}</div><div class="text-cyan-300 text-lg font-mono">${count}</div></div>`,
    ).join('');
    const actionRows = actions.slice(0, 4).map((item) =>
      `<li class="border-b border-zinc-800/70 pb-2">
        <div class="flex items-center justify-between gap-2">
          <span class="font-mono text-cyan-300">${escapeHtml(item.bot_id || 'bot')}</span>
          <span class="text-zinc-400">${escapeHtml(item.launch_lane || '')}</span>
        </div>
        <div class="text-zinc-300">${escapeHtml(item.next_action || '')}</div>
      </li>`,
    ).join('');
    this.body.innerHTML = `
      <div class="grid grid-cols-2 gap-2 text-xs mb-3">
        ${laneRows || '<div class="text-zinc-500">snapshot missing</div>'}
      </div>
      <div class="text-xs text-zinc-500 mb-1">next actions</div>
      <ul class="space-y-2 text-xs">${actionRows || '<li class="text-emerald-300">No readiness actions.</li>'}</ul>`;
  }
}

// --- 5d. Strategy supercharge queue ---
class StrategySuperchargeManifestPanel extends Panel {
  constructor() { super('cc-strategy-supercharge', '/api/jarvis/strategy_supercharge_manifest', 'Strategy Supercharge Queue'); }
  render(data) {
    const summary = data.summary || {};
    const nextBatch = Array.isArray(data.next_batch) ? data.next_batch : [];
    const bLater = Array.isArray(data.b_later) ? data.b_later : [];
    const hold = Array.isArray(data.hold) ? data.hold : [];
    const commandCount = Number(summary.commands || (Array.isArray(data.commands) ? data.commands.length : 0));
    const nextBot = summary.next_bot || nextBatch[0]?.bot_id || '';
    if (data.error || data.status === 'unreadable') {
      this.body.innerHTML = `<div class="text-amber-300 text-sm">Strategy manifest degraded: ${escapeHtml(data.error || 'manifest unreadable')}</div>`;
      return;
    }
    const rows = nextBatch.slice(0, 4).map((item) => {
      const command = Array.isArray(item.command) ? item.command.join(' ') : '';
      const smoke = Array.isArray(item.smoke_command) ? item.smoke_command.join(' ') : '';
      const phase = item.execution_phase || item.supercharge_phase || item.action_type || 'queued';
      return `<li class="border-b border-zinc-800/70 pb-2">
        <div class="flex items-center justify-between gap-2">
          <span class="font-mono text-cyan-300">${escapeHtml(item.bot_id || 'bot')}</span>
          <span class="text-emerald-300">${escapeHtml(phase)}</span>
        </div>
        <div class="text-zinc-300">${escapeHtml(item.operator_note || item.next_gate || '')}</div>
        <code class="block text-[11px] text-zinc-500 mt-1 break-all">${escapeHtml(command || 'command pending')}</code>
        ${smoke ? `<code class="block text-[11px] text-zinc-600 mt-1 break-all">smoke: ${escapeHtml(smoke)}</code>` : ''}
      </li>`;
    }).join('');
    this.body.innerHTML = `
      <div class="grid grid-cols-4 gap-2 text-xs mb-3">
        <div><div class="text-zinc-500">A+C now</div><div class="text-emerald-300 text-lg font-mono">${Number(summary.a_c_now || 0)}</div></div>
        <div><div class="text-zinc-500">B later</div><div class="text-amber-300 text-lg font-mono">${bLater.length}</div></div>
        <div><div class="text-zinc-500">hold</div><div class="text-zinc-300 text-lg font-mono">${hold.length}</div></div>
        <div><div class="text-zinc-500">commands</div><div class="text-cyan-300 text-lg font-mono">${commandCount}</div></div>
      </div>
      <div class="text-xs text-zinc-500 mb-1">next bot: <span class="text-zinc-100 font-mono">${escapeHtml(nextBot || 'none')}</span></div>
      <ul class="space-y-2 text-xs">${rows || '<li class="text-emerald-300">No A+C retest commands queued.</li>'}</ul>`;
  }
}

// --- 5e. Strategy supercharge results ---
class StrategySuperchargeResultsPanel extends Panel {
  constructor() { super('cc-strategy-supercharge-results', '/api/jarvis/strategy_supercharge_results', 'Strategy Supercharge Results'); }
  render(data) {
    const summary = data.summary || {};
    const nearMisses = Array.isArray(data.near_misses) ? data.near_misses : [];
    const retuneQueue = Array.isArray(data.retune_queue) ? data.retune_queue : [];
    if (data.error || data.status === 'unreadable') {
      this.body.innerHTML = `<div class="text-amber-300 text-sm">Strategy results degraded: ${escapeHtml(data.error || 'results unreadable')}</div>`;
      return;
    }
    const nearRows = nearMisses.slice(0, 4).map((item) => `
      <li class="border-b border-zinc-800/70 pb-2">
        <div class="flex items-center justify-between gap-2">
          <span class="font-mono text-cyan-300">${escapeHtml(item.bot_id || 'bot')}</span>
          <span class="text-amber-300">${escapeHtml(item.result_status || 'near')}</span>
        </div>
        <div class="text-zinc-300">OOS Sharpe ${Number(item.oos_sharpe || 0).toFixed(2)} · DSR ${formatPct(item.dsr_pass_fraction || 0)}</div>
      </li>`).join('');
    const retuneRows = retuneQueue.slice(0, 4).map((item) => `
      <li class="border-b border-zinc-800/70 pb-2">
        <div class="flex items-center justify-between gap-2">
          <span class="font-mono text-cyan-300">${escapeHtml(item.bot_id || 'bot')}</span>
          <span class="text-emerald-300">${Number(item.priority_score || 0).toFixed(2)}</span>
        </div>
        <div class="text-zinc-300">${escapeHtml(item.issue_code || item.primary_focus || 'retune')}</div>
        <div class="text-zinc-500">${escapeHtml(item.next_step || '')}</div>
      </li>`).join('');
    this.body.innerHTML = `
      <div class="grid grid-cols-4 gap-2 text-xs mb-3">
        <div><div class="text-zinc-500">tested</div><div class="text-cyan-300 text-lg font-mono">${Number(summary.tested || 0)}</div></div>
        <div><div class="text-zinc-500">passed</div><div class="text-emerald-300 text-lg font-mono">${Number(summary.passed || 0)}</div></div>
        <div><div class="text-zinc-500">failed</div><div class="text-red-300 text-lg font-mono">${Number(summary.failed || 0)}</div></div>
        <div><div class="text-zinc-500">pending</div><div class="text-amber-300 text-lg font-mono">${Number(summary.pending || 0)}</div></div>
      </div>
      <div class="text-xs text-zinc-500 mb-3">best near miss: <span class="text-zinc-100 font-mono">${escapeHtml(summary.best_near_miss_bot || 'none')}</span></div>
      <div class="grid grid-cols-2 gap-3 text-xs">
        <div><div class="text-zinc-500 mb-1">near_misses</div><ul class="space-y-2">${nearRows || '<li class="text-zinc-500">No near misses.</li>'}</ul></div>
        <div><div class="text-zinc-500 mb-1">retune_queue</div><ul class="space-y-2">${retuneRows || '<li class="text-emerald-300">No retunes queued.</li>'}</ul></div>
      </div>`;
  }
}

// --- 6. Policy diff ---
class PolicyDiffPanel extends Panel {
  constructor() { super('cc-policy-diff', '/api/jarvis/policy_diff', 'Bandit Policy Diff'); }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    const candidates = data.candidates || {};
    const rows = Object.entries(candidates).map(([name, v]) => {
      if (v.error) {
        return `<tr><td>${escapeHtml(name)}</td><td colspan="3" class="text-red-400">Error: ${escapeHtml(v.error)}</td></tr>`;
      }
      return `<tr>
        <td>${escapeHtml(name)}</td>
        <td class="text-right">${v.n_records ?? '—'}</td>
        <td class="text-right ${Number(v.avg_r || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">${formatR(v.avg_r || 0)}</td>
        <td class="text-right">${formatPct(v.win_rate || 0)}</td>
      </tr>`;
    }).join('');
    this.body.innerHTML = `
      <div class="text-xs text-zinc-500 mb-2">Window: ${data.window_days ?? '—'} days · Records: ${data.n_records ?? 0}</div>
      <table class="text-xs w-full">
        <tr class="text-zinc-500"><th class="text-left">Candidate</th><th class="text-right">N</th><th class="text-right">Avg R</th><th class="text-right">Win Rate</th></tr>
        ${rows || '<tr><td colspan="4" class="text-zinc-500">No candidate deltas available yet.</td></tr>'}
      </table>`;
  }
}

// --- 7. V22 toggle (operator-action panel) ---
class V22TogglePanel extends Panel {
  constructor() { super('cc-v22-toggle', '/api/jarvis/sage_modulation_toggle', 'V22 Modulation'); }
  render(data) {
    const enabled = !!data.enabled;
    const cls = enabled ? 'bg-emerald-600' : 'bg-zinc-700';
    this.body.innerHTML = `
      <div class="flex items-center justify-between">
        <span class="text-sm font-medium">${enabled ? 'Enabled' : 'Disabled'}</span>
        <button id="v22-toggle-btn" class="${cls} hover:opacity-80 px-3 py-1 rounded text-sm">${enabled ? 'Disable' : 'Enable'}</button>
      </div>
      <div class="text-xs text-zinc-500 mt-2">Flag: ${escapeHtml(data.flag_name || 'ETA_FF_V22_SAGE_MODULATION')}</div>
      <div class="text-xs text-zinc-500 mt-1">Controls whether School V22 can modulate execution confidence and size gates.</div>`;
    document.getElementById('v22-toggle-btn').addEventListener('click', async () => {
      try {
        const r = await authedPost('/api/jarvis/sage_modulation_toggle',
          { enabled: !enabled },
          { stepUpReason: 'Flipping V22 sage modulation. PIN required.' });
        if (!r) return;
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          const code = body?.detail?.error_code || `http_${r.status}`;
          notify(`V22 toggle failed (${code})`, 'error');
          return;
        }
        notify(`V22 toggled ${!enabled ? 'ON' : 'OFF'}`, 'success');
        this.refresh();
      } catch (e) {
        console.error('v22 toggle failed', e);
        notify(`V22 toggle failed (${e.message})`, 'error');
      }
    });
    // Also reflect on top bar
    const topEl = document.getElementById('top-v22-toggle');
    if (topEl) topEl.innerHTML = `<span class="${enabled ? 'text-emerald-400' : 'text-zinc-500'}">v22 ${enabled ? 'ON' : 'off'}</span>`;
  }
}

// --- 8. Edge tracker leaderboard ---
class EdgeLeaderboardPanel extends Panel {
  constructor() { super('cc-edge-leaderboard', '/api/jarvis/edge_leaderboard', 'Edge Leaderboard'); }
  render(data) {
    const top = data.top || [];
    const bot = data.bottom || [];
    const row = s => `<tr><td>${escapeHtml(s.school)}</td><td class="text-right">${formatR(s.avg_r)}</td><td class="text-right text-zinc-500">${s.n_aligned}</td></tr>`;
    this.body.innerHTML = `
      <div class="grid grid-cols-2 gap-3 text-xs">
        <div><div class="text-emerald-400 mb-1">top</div><table class="w-full">${top.map(row).join('') || '<tr><td>—</td></tr>'}</table></div>
        <div><div class="text-red-400 mb-1">bottom</div><table class="w-full">${bot.map(row).join('') || '<tr><td>—</td></tr>'}</table></div>
      </div>`;
  }
}

// --- 9. Model tier ---
class ModelTierPanel extends Panel {
  constructor() { super('cc-model-tier', '/api/jarvis/model_tier', 'Model Tier'); }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    this.body.innerHTML = `
      <div class="text-2xl font-mono text-emerald-400">${escapeHtml(data.tier || '—')}</div>
      <div class="text-xs text-zinc-500 mt-2">Subsystem: ${escapeHtml(data.subsystem || '—')}</div>
      <div class="text-xs text-zinc-500">Task Category: ${escapeHtml(data.task_category || '—')}</div>
      <div class="text-xs text-zinc-500">Last Selection: ${formatTime(data.ts)}</div>`;
  }
}

// --- 10. Latest kaizen ticket ---
class KaizenLatestPanel extends Panel {
  constructor() { super('cc-kaizen-latest', '/api/jarvis/kaizen_latest', 'Latest Kaizen Ticket'); }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    this.body.innerHTML = `
      <div class="font-semibold mb-1">${escapeHtml(data.title || '—')}</div>
      <pre class="text-xs whitespace-pre-wrap text-zinc-400 max-h-48 overflow-y-auto">${escapeHtml(data.markdown || '')}</pre>`;
  }
}

// --- Initialize JARVIS panels ---
onAuthenticated(() => {
  const panels = [
    new VerdictStreamPanel(),
    new SageExplainPanel(),
    new SageHealthPanel(),
    new DisagreementHeatmapPanel(),
    new StressMoodPanel(),
    new OperatorQueuePanel(),
    new BotStrategyReadinessPanel(),
    new StrategySuperchargeManifestPanel(),
    new StrategySuperchargeResultsPanel(),
    new PolicyDiffPanel(),
    new V22TogglePanel(),
    new EdgeLeaderboardPanel(),
    new ModelTierPanel(),
    new KaizenLatestPanel(),
  ];
  panels.forEach(p => { if (p.endpoint) poller.register(p); });
});
