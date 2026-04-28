// eta_engine/deploy/status_page/js/live.js
// LiveStream (EventSource wrapper with backoff) + Poller (5s scheduler).
// Wave-7 dashboard, 2026-04-27.

import { onAuthenticated } from '/js/auth.js';

export class LiveStream {
  constructor() {
    this.es = null;
    this.handlers = { verdict: [], fill: [] };
    this.reconnectDelayMs = 1000;
    this.maxReconnectDelayMs = 30_000;
    this.statusEl = document.getElementById('top-sse-status');
  }

  on(event, handler) {
    if (!this.handlers[event]) this.handlers[event] = [];
    this.handlers[event].push(handler);
  }

  connect() {
    this._setStatus('reconnecting');
    try {
      this.es = new EventSource('/api/live/stream');
    } catch (e) {
      console.error('EventSource construct failed', e);
      this._scheduleReconnect();
      return;
    }
    this.es.onopen = () => {
      this.reconnectDelayMs = 1000;  // reset backoff on success
      this._setStatus('connected');
    };
    this.es.onerror = () => {
      this._setStatus('reconnecting');
      this.es.close();
      this._scheduleReconnect();
    };
    ['verdict', 'fill'].forEach(eventType => {
      this.es.addEventListener(eventType, (msg) => {
        let data;
        try { data = JSON.parse(msg.data); }
        catch (e) { console.warn(`bad SSE ${eventType} JSON`, e); return; }
        (this.handlers[eventType] || []).forEach(h => {
          try { h(data); } catch (e) { console.error(`SSE ${eventType} handler`, e); }
        });
      });
    });
  }

  _scheduleReconnect() {
    setTimeout(() => this.connect(), this.reconnectDelayMs);
    this.reconnectDelayMs = Math.min(
      this.reconnectDelayMs * 2,
      this.maxReconnectDelayMs,
    );
    if (this.reconnectDelayMs >= 30_000) this._setStatus('down');
  }

  _setStatus(s) {
    if (!this.statusEl) return;
    const dot = this.statusEl.querySelector('span');
    if (!dot) return;
    dot.classList.remove('sse-connected', 'sse-reconnecting', 'sse-down', 'bg-zinc-500');
    if (s === 'connected') dot.classList.add('sse-connected');
    else if (s === 'reconnecting') dot.classList.add('sse-reconnecting');
    else dot.classList.add('sse-down');
  }
}

export class Poller {
  constructor(intervalMs = 5000) {
    this.intervalMs = intervalMs;
    this.panels = [];
    this.timer = null;
    this.active = true;
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        this.active = false;
      } else {
        this.active = true;
        this._tick();   // immediate force-refresh on return
      }
    });
  }

  register(panel) {
    this.panels.push(panel);
  }

  start() {
    this._tick();
    this.timer = setInterval(() => this._tick(), this.intervalMs);
  }

  async _tick() {
    if (!this.active) return;
    for (const panel of this.panels) {
      panel.refresh().catch(e => console.error(`poller refresh ${panel.containerId}`, e));
    }
    // Also re-render the "updated Xs ago" label on every panel
    this.panels.forEach(p => p.updateRefreshLabel?.());
  }
}

// Singleton instances
export const liveStream = new LiveStream();
export const poller = new Poller(5000);

// Wire on authenticated
onAuthenticated(() => {
  liveStream.connect();
  poller.start();
});
