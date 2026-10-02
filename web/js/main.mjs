// Router + chrome. Views render into #view; the hash is the only routing
// state (the file server never sees a sub-path). Views communicate through
// the store bus (connected / disconnected / toast).

import { loadSession, clearSession, on } from './store.mjs';
import * as connectView from './views/connect.mjs';
import * as orderbookView from './views/orderbook.mjs';
import * as tradeView from './views/trade.mjs';
import * as walletView from './views/wallet.mjs';
import * as swapsView from './views/swaps.mjs';
import * as coinsView from './views/coins.mjs';

const views = {
  connect: connectView,
  orderbook: orderbookView,
  trade: tradeView,
  wallet: walletView,
  swaps: swapsView,
  coins: coinsView,
};

const viewRoot = document.getElementById('view');
const navEl = document.getElementById('nav');
const ledEl = document.getElementById('led');
const verEl = document.getElementById('ver');
const btnConnect = document.getElementById('btn-connect');
const btnDisconnect = document.getElementById('btn-disconnect');
const toastsEl = document.getElementById('toasts');

function currentView() {
  const hash = location.hash || '';
  const name = hash.startsWith('#/') ? hash.slice(2).split('?')[0] : '';
  return views[name] ? name : 'orderbook';
}

function route() {
  const hasSession = !!loadSession();
  let name = currentView();
  // connect gate: no session -> nothing else is reachable
  if (!hasSession && name !== 'connect') {
    location.hash = '#/connect';
    return; // the hashchange listener re-routes
  }
  viewRoot.replaceChildren();
  views[name].render(viewRoot);
  navEl.classList.toggle('hidden', !hasSession);
  for (const a of navEl.querySelectorAll('[data-nav]')) {
    a.classList.toggle('active', a.dataset.nav === name);
  }
  const session = loadSession();
  verEl.textContent = session && session.version ? session.version : '';
  ledEl.className = hasSession ? 'led ok' : 'led off';
  btnConnect.classList.toggle('hidden', !!hasSession);
  btnDisconnect.classList.toggle('hidden', !hasSession);
}

// toasts (views only emit; the chrome renders)
on('toast', ({ msg, kind = '' , ms = 4000}) => {
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  toastsEl.append(el);
  setTimeout(() => el.remove(), ms);
});

on('connected', () => {
  if (location.hash !== '#/orderbook') location.hash = '#/orderbook';
  route();
});

on('disconnected', () => route());

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
route();