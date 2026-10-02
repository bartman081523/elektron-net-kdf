// Connect view: URL + rpc password -> probes the daemon (`version`, public
// method; then `get_enabled_coins` with the password) -> sessionStorage.
// Until a session exists every other view is gated off (router in main.mjs).
//
// Dev autologin: #/connect?dev-url=…&dev-pass=…&next=<view> connects without
// interaction — honored ONLY for localhost origins, so headless verification
// and local test loops can script the SPA. Test credentials only; never put
// a real password in a URL.

import { loadSession, saveSession, clearSession, newClientId, emit } from '../store.mjs';
import { probe } from '../api.mjs';
import { esc } from '../format.mjs';

function hashQuery() {
  return new URLSearchParams(location.hash.split('?').slice(1).join('?'));
}

export function render(root) {
  const session = loadSession();
  if (session) {
    renderConnected(root, session);
    return;
  }

  const q = hashQuery();
  const devUrl = q.get('dev-url');
  const isLocalhost = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
  if (devUrl && isLocalhost) {
    root.innerHTML = `
      <section class="panel connect">
        <h1>connect to daemon</h1>
        <p id="c-err" class="form-err">dev autologin → ${esc(devUrl.replace(/\/+$/, ''))}…</p>
      </section>`;
    // fall through without user interaction; password stays in the tab only
    connectTo(devUrl.replace(/\/+$/, ''), q.get('dev-pass') || 'mock', q.get('next') || 'orderbook', root);
    return;
  }

  root.innerHTML = `
  <section class="panel connect">
    <h1>connect to daemon</h1>
    <p class="muted">The browser talks straight to your local kdf — no proxy, no
    accounts. The RPC password stays in this tab (sessionStorage) and is sent
    nowhere except your own daemon.</p>
    <form id="connect-form" autocomplete="off">
      <label class="field"><span>daemon url</span>
        <input id="c-url" type="url" required spellcheck="false"
               placeholder="http://127.0.0.1:7796" value="">
      </label>
      <label class="field"><span>rpc password</span>
        <input id="c-pass" type="password" required autocomplete="off">
      </label>
      <div class="connect-actions">
        <button class="btn primary" type="submit" id="c-go">connect</button>
      </div>
      <p id="c-err" class="form-err"></p>
    </form>
    <div class="connect-hint muted">
      <p>Default is the local market node (rpcport 7796); the seed node listens
      on 7795. Live orderbook updates need
      <code>event_streaming_configuration</code> in the daemon's MM2.json.</p>
    </div>
  </section>`;

  root.querySelector('#connect-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const err = root.querySelector('#c-err');
    const base = root.querySelector('#c-url').value.trim().replace(/\/+$/, '');
    const userpass = root.querySelector('#c-pass').value;
    err.textContent = 'connecting…';
    connectTo(base, userpass, 'orderbook', root, err);
  });
}

async function connectTo(base, userpass, nextView, root, errEl) {
  const setErr = (msg) => {
    if (errEl) errEl.textContent = 'connection failed: ' + msg;
    else {
      const el = root.querySelector('#c-err');
      if (el) el.textContent = 'connection failed: ' + msg;
    }
  };
  try {
    const { version, coins } = await probe(base, userpass);
    saveSession({ url: base, userpass, clientId: newClientId(), version, coins: coins.length });
    // Set the target hash BEFORE emit('connected'): main.mjs only redirects
    // to #/orderbook when the hash is still the connect view — otherwise it
    // would clobber a dev-autologin `next` (and the view would render twice).
    const suffix = nextView === 'selftest' ? '?auto=1' : '';
    location.hash = '#/' + nextView + suffix;
    emit('toast', { msg: 'connected: kdf ' + version + ', ' + coins.length + ' coin(s) active' });
    emit('connected');
  } catch (e) {
    setErr(e.message);
  }
}

function renderConnected(root, session) {
  root.innerHTML = `
  <section class="panel connect">
    <h1>connected</h1>
    <table class="table">
      <tbody>
        <tr><td class="muted">daemon</td><td class="num">${esc(session.url)}</td></tr>
        <tr><td class="muted">version</td><td class="num">${esc(session.version)}</td></tr>
        <tr><td class="muted">coins active</td><td class="num">${esc(session.coins)}</td></tr>
      </tbody>
    </table>
    <div class="connect-actions">
      <button class="btn primary" type="button" id="c-open">open market</button>
      <button class="btn danger" type="button" id="c-drop">forget this daemon</button>
    </div>
  </section>`;
  root.querySelector('#c-open').addEventListener('click', () => {
    location.hash = '#/orderbook';
  });
  root.querySelector('#c-drop').addEventListener('click', () => {
    clearSession();
    emit('toast', { msg: 'session removed from this tab' });
    emit('disconnected');
  });
}