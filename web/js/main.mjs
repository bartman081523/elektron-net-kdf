// Router + chrome. Views render into #view; the hash is the only routing
// state (the file server never sees a sub-path). Views communicate through
// the store bus (connected / disconnected / toast / daemon-state / sse-state).
// A view's render() may return a cleanup fn — called before the next render.

import { loadSession, saveSession, clearSession, newClientId, on, emit } from './store.mjs';
import { probe } from './api.mjs';
import * as connectView from './views/connect.mjs';
import * as orderbookView from './views/orderbook.mjs';
import * as tradeView from './views/trade.mjs';
import * as walletView from './views/wallet.mjs';
import * as swapsView from './views/swaps.mjs';
import * as coinsView from './views/coins.mjs';
import * as selftestView from './views/selftest.mjs';

const views = {
  connect: connectView,
  orderbook: orderbookView,
  trade: tradeView,
  wallet: walletView,
  swaps: swapsView,
  coins: coinsView,
  selftest: selftestView,   // dev tool, reachable by URL only (not in nav)
};

const viewRoot = document.getElementById('view');
const navEl = document.getElementById('nav');
const ledEl = document.getElementById('led');
const verEl = document.getElementById('ver');
const btnConnect = document.getElementById('btn-connect');
const btnDisconnect = document.getElementById('btn-disconnect');
const toastsEl = document.getElementById('toasts');

let cleanup = null;         // cleanup fn of the currently rendered view
let lastHash = null;        // renders are deduped on an unchanged hash

function currentView() {
  const hash = location.hash || '';
  const name = hash.startsWith('#/') ? hash.slice(2).split('?')[0] : '';
  return views[name] ? name : 'orderbook';
}

function route() {
  const hash = location.hash || '#/orderbook';
  // hashchange AND direct calls (connected/disconnected both fire on connect)
  // race onto the same hash — render once.
  if (hash === lastHash) return;
  const hasSession = !!loadSession();
  let name = currentView();
  // connect gate: no session -> nothing else is reachable
  if (!hasSession && name !== 'connect') {
    location.hash = '#/connect';
    return; // the hashchange listener re-routes
  }
  if (cleanup) {
    try { cleanup(); } catch (e) { /* a view must not block routing */ }
    cleanup = null;
  }
  viewRoot.replaceChildren();
  const maybeCleanup = views[name].render(viewRoot);
  if (typeof maybeCleanup === 'function') cleanup = maybeCleanup;
  navEl.classList.toggle('hidden', !hasSession);
  for (const a of navEl.querySelectorAll('[data-nav]')) {
    a.classList.toggle('active', a.dataset.nav === name);
  }
  const session = loadSession();
  verEl.textContent = session && session.version ? session.version : '';
  btnConnect.classList.toggle('hidden', !!hasSession);
  btnDisconnect.classList.toggle('hidden', !hasSession);
  lastHash = hash;
}

// LED: union of the last known RPC and SSE health, with toast on DOWN-transitions.
let lastLed = '';
function setLed(ok) {
  const next = ok ? 'ok' : 'down';
  if (next === lastLed) return;
  ledEl.className = 'led ' + (ok ? 'ok' : 'down');
  if (next === 'down' && lastLed === 'ok') emit('toast', { msg: 'daemon connection lost', kind: 'err', ms: 6000 });
  if (next === 'ok' && lastLed === 'down') emit('toast', { msg: 'daemon connection restored' });
  lastLed = next;
}

on('daemon-state', ({ ok }) => setLed(ok));
on('sse-state', ({ state }) => {
  if (state === 'ok') setLed(true);
  else if (state === 'error') setLed(false);
  // 'connecting'/'off' leave the LED as-is
});

// toasts (views only emit; the chrome renders)
on('toast', ({ msg, kind = '', ms = 4000 }) => {
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  toastsEl.append(el);
  setTimeout(() => el.remove(), ms);
});

on('connected', () => {
  // leave the connect view unless a view was already chosen (dev `next`, or
  // the connect view itself set the target hash before emitting)
  if (location.hash.startsWith('#/connect')) location.hash = '#/orderbook';
  route();
});

on('disconnected', () => {
  cleanup = null;
  route();
});

btnConnect.addEventListener('click', () => {
  location.hash = '#/connect';
  route();
});

btnDisconnect.addEventListener('click', () => {
  clearSession();
  location.hash = '#/connect';
  route();
});

window.addEventListener('hashchange', route);

// Auto-connect: when elek-web runs with MM_WEB_RPC_URL + MM_WEB_RPC_PASS, it
// serves /elek-web-config.json and the SPA connects at boot without the
// connect form. Precedence: an existing session in this tab wins, then the
// dev autologin query (test flows only), then this endpoint; whenever
// anything is missing or fails, the connect form (no session -> the router
// gates every other view) is still the fallback.
async function tryAutoConnect() {
  if (loadSession()) return;
  const query = new URLSearchParams(location.hash.split('?').slice(1).join('?'));
  if (query.get('dev-url')) return;
  let cfg;
  try {
    const resp = await fetch('/elek-web-config.json');
    if (!resp.ok) return;                      // endpoint off -> manual flow
    cfg = await resp.json();
  } catch {
    return;
  }
  if (!cfg || typeof cfg !== 'object') return;
  const base = typeof cfg.rpc_url === 'string' ? cfg.rpc_url.replace(/\/+$/, '') : '';
  const pass = typeof cfg.rpc_pass === 'string' ? cfg.rpc_pass : '';
  // Proxy deployments (elek-web MM_WEB_PROXY_URL): same-origin /rpc, password
  // injected server-side — the SPA connects without ever knowing the pass.
  if (cfg.rpc_proxy === true) {
    if (!base) return;
    try {
      const { version, coins } = await probe(base, '');
      saveSession({ url: base, userpass: '', clientId: newClientId(), version, coins: coins.length, proxy: true });
      if (!location.hash || location.hash === '#/connect') location.hash = '#/orderbook';
      emit('toast', { msg: 'connected: kdf ' + version + ', ' + coins.length + ' coin(s) active (proxy mode)' });
    } catch (e) {
      emit('toast', { msg: 'auto-connect failed: ' + (e.message || e), kind: 'err', ms: 6000 });
    }
    return;
  }
  if (!base || !pass) return;
  try {
    const { version, coins } = await probe(base, pass);
    saveSession({ url: base, userpass: pass, clientId: newClientId(), version, coins: coins.length });
    // keep deep links (e.g. #/wallet); only redirect connect/empty hashes
    if (!location.hash || location.hash === '#/connect') location.hash = '#/orderbook';
    emit('toast', { msg: 'connected: kdf ' + version + ', ' + coins.length + ' coin(s) active' });
  } catch (e) {
    emit('toast', { msg: 'auto-connect failed: ' + (e.message || e), kind: 'err', ms: 6000 });
    // fall through: route() below shows the connect form (no session saved)
  }
}

tryAutoConnect().finally(route);