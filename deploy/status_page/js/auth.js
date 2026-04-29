// eta_engine/deploy/status_page/js/auth.js
// Auth flow: session check on load, login modal, step-up modal,
// global 401-handler that re-prompts login.
// Wave-7 dashboard, 2026-04-27.

import { escapeHtml } from '/js/panels.js';

export const session = {
  authenticated: false,
  user: null,
  steppedUp: false,
};

let _afterLoginCallbacks = [];

function renderUserChip() {
  const el = document.getElementById('top-user-chip');
  if (!el) return;
  if (!session.authenticated) {
    el.textContent = 'signed out';
    return;
  }
  const suffix = session.steppedUp ? ' • step-up' : '';
  el.textContent = `${session.user || 'operator'}${suffix}`;
}

export function onAuthenticated(cb) {
  if (session.authenticated) cb();
  else _afterLoginCallbacks.push(cb);
}

export async function checkSession() {
  try {
    const r = await fetch('/api/auth/session', {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    if (!r.ok) return false;
    const body = await r.json();
    session.authenticated = !!body.authenticated;
    session.user = body.user || null;
    session.steppedUp = !!body.stepped_up;
    renderUserChip();
    return session.authenticated;
  } catch (e) {
    console.error('session check failed', e);
    return false;
  }
}

export function showLoginModal() {
  const modal = document.getElementById('login-modal');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  document.getElementById('login-username').focus();
}

export function hideLoginModal() {
  const modal = document.getElementById('login-modal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
}

async function doLogin(username, password) {
  const errEl = document.getElementById('login-error');
  errEl.classList.add('hidden');
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ username, password }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      const code = body?.detail?.error_code || `http_${r.status}`;
      errEl.textContent = `login failed (${code})`;
      errEl.classList.remove('hidden');
      return false;
    }
    const body = await r.json();
    session.authenticated = true;
    session.user = body.user;
    session.steppedUp = false;
    renderUserChip();
    hideLoginModal();
    _afterLoginCallbacks.forEach(cb => { try { cb(); } catch(e) { console.error(e); }});
    _afterLoginCallbacks = [];
    return true;
  } catch (e) {
    errEl.textContent = `network: ${e.message}`;
    errEl.classList.remove('hidden');
    return false;
  }
}

export async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
  } catch (e) { /* ignore */ }
  session.authenticated = false;
  session.user = null;
  session.steppedUp = false;
  renderUserChip();
  showLoginModal();
}

export function showStepUpModal(reason = 'Sensitive action requires PIN.') {
  const modal = document.getElementById('step-up-modal');
  document.getElementById('step-up-reason').textContent = reason;
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  document.getElementById('step-up-pin').focus();
  return new Promise((resolve) => {
    _stepUpResolver = resolve;
  });
}

let _stepUpResolver = null;

function hideStepUpModal() {
  const modal = document.getElementById('step-up-modal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
  document.getElementById('step-up-pin').value = '';
}

async function doStepUp(pin) {
  const errEl = document.getElementById('step-up-error');
  errEl.classList.add('hidden');
  try {
    const r = await fetch('/api/auth/step-up', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ pin }),
    });
    if (!r.ok) {
      errEl.textContent = `bad PIN (${r.status})`;
      errEl.classList.remove('hidden');
      return false;
    }
    session.steppedUp = true;
    renderUserChip();
    hideStepUpModal();
    if (_stepUpResolver) { _stepUpResolver(true); _stepUpResolver = null; }
    return true;
  } catch (e) {
    errEl.textContent = `network: ${e.message}`;
    errEl.classList.remove('hidden');
    return false;
  }
}

/** Authenticated POST helper that handles 403 step_up_required via a modal. */
export async function authedPost(url, body, opts = {}) {
  const reason = opts.stepUpReason || 'Sensitive action requires PIN.';
  for (let attempt = 0; attempt < 2; attempt++) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {}),
    });
    if (r.status === 401) {
      session.authenticated = false;
      session.user = null;
      session.steppedUp = false;
      renderUserChip();
      showLoginModal();
      throw new Error('not authenticated');
    }
    if (r.status === 403) {
      const detail = await r.json().catch(() => ({}));
      if (detail?.detail?.error_code === 'step_up_required' && attempt === 0) {
        const ok = await showStepUpModal(reason);
        if (!ok) throw new Error('step-up cancelled');
        continue;
      }
    }
    return r;
  }
}

// --- wire up the modals ---

document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    await doLogin(
      document.getElementById('login-username').value,
      document.getElementById('login-password').value,
    );
  });

  document.getElementById('step-up-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    await doStepUp(document.getElementById('step-up-pin').value);
  });

  document.getElementById('step-up-cancel').addEventListener('click', () => {
    hideStepUpModal();
    if (_stepUpResolver) { _stepUpResolver(false); _stepUpResolver = null; }
  });

  document.getElementById('top-logout').addEventListener('click', logout);

  // Initial session check
  const ok = await checkSession();
  if (!ok) {
    showLoginModal();
  } else {
    renderUserChip();
    _afterLoginCallbacks.forEach(cb => { try { cb(); } catch(e) { console.error(e); }});
    _afterLoginCallbacks = [];
  }
});
