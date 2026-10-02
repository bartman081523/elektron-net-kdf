// Connect view: URL + rpc password -> probes the daemon (`version`, public
// method; then `get_enabled_coins` with the password) -> sessionStorage.
// Until a session exists every other view is gated off (router in main.mjs).

import { loadSession, saveSession, clearSession, newClientId, emit } from '../store.mjs';
import { esc } from '../format.mjs';

// Legacy JSON-RPC over POST. The daemon accepts the legacy top-level envelope
// ({"method": ..., "userpass": ...}); the v2 params envelope arrives with the
// api layer (F1). Errors: legacy failures come back as {"error": "<string>"},
// HTTP-failures (e.g. the daemon's empty 500) as an unparseable body.
async function rpc(base, body) {
  const resp = await fetch(base + '/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let json = null;
  try {
    json = await resp.json();
  } catch {
    throw new Error('no JSON body (HTTP ' + resp.status + ') — is this a kdf RPC port?');
  }
  if (json && json.error) {
    throw new Error(typeof json.error === 'string' ? json.error : JSON.stringify(json.error));
  }
  return json;
}

function versionStr(resp) {
  const res = resp && resp.result;
  if (typeof res === 'string') return res;
  if (res && typeof res === 'object') return res.rpc_version || res.version || 'unknown';
  return 'unknown';
}

export function render(root) {
  const session = loadSession();
  if (session) {
    renderConnected(root, session);
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
    const go = root.querySelector('#c-go');
    const err = root.querySelector('#c-err');
    const base = root.querySelector('#c-url').value.trim().replace(/\/+$/, '');
    const userpass = root.querySelector('#c-pass').value;
    go.disabled = true;
    err.textContent = 'connecting…';
    (async () => {
      // 1) URL probe: `version` is a public method (no password involved).
      const ver = await rpc(base, { method: 'version' });
      const version = versionStr(ver);
      // 2) password probe: legacy envelope carries userpass at top level.
      const coins = await rpc(base, { userpass, method: 'get_enabled_coins' });
      const list = Array.isArray(coins.result) ? coins.result : [];
      if (coins.error) throw new Error(String(coins.error));
      saveSession({ url: base, userpass, clientId: newClientId(), version, coins: list.length });
      emit('toast', { msg: 'connected: kdf ' + version + ', ' + list.length + ' coin(s) active' });
      emit('connected');
    })().catch((e) => {
      err.textContent = 'connection failed: ' + e.message;
    }).finally(() => {
      go.disabled = false;
    });
  });
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