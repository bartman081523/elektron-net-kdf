import { enabledCoins, balance, withdraw, sendRaw, RpcError } from '../api.mjs';
import { num, esc, cut } from '../format.mjs';
import { on, off, emit } from '../store.mjs';

// Wallet view: balances of every enabled coin (my_balance), receive (address
// copy; QR intentionally deferred — open point 1 of the plan), send as two
// steps: withdraw (v2, broadcast:false) -> preview -> send_raw_transaction.
// The daemon owns all state; this view shows it and drives the RPCs.

let rows = [];          // fetched balance rows: {coin, balance, unspendable, address} | {coin, error}
let sendFor = null;     // ticker whose send form is open
let pending = null;     // previewed withdraw result awaiting broadcast
let sendErr = null;     // last error message of the send flow

export function render(root) {
  root.innerHTML = `
    <div class="page">
      <div class="panel-head">
        <h2>wallet</h2>
        <button id="w-refresh" class="btn small ghost">refresh</button>
      </div>
      <div class="page-cols">
        <section class="panel">
          <div class="panel-head"><h3>balances</h3>
            <span id="w-note" class="muted"></span></div>
          <div id="w-table" class="state loading">loading balances…</div>
        </section>
        <section class="panel" id="w-send-panel" hidden>
          <div class="panel-head"><h3>send <span id="w-send-coin" class="muted"></span></h3></div>
          <div id="w-send-root"></div>
        </section>
      </div>
    </div>`;

  document.getElementById('w-refresh').addEventListener('click', refresh);
  on('sse:balance', onBalance);
  refresh();
  return () => off('sse:balance', onBalance);
}

// optional polish: a BALANCE event for a coin updates its row in place
function onBalance(msg) {
  if (!msg || !msg.coin) return;
  const cell = document.querySelector(`[data-coin-row="${CSS.escape(msg.coin)}"] .w-bal`);
  const value = msg.message && msg.message.balance;
  if (cell && value !== undefined) cell.textContent = num(value);
}

async function refresh() {
  const table = document.getElementById('w-table');
  const note = document.getElementById('w-note');
  const btn = document.getElementById('w-refresh');
  btn.disabled = true;
  table.innerHTML = '<div class="state">loading balances…</div>';
  try {
    const coins = await enabledCoins();
    const list = Array.isArray(coins.result) ? coins.result : [];
    if (!list.length) {
      table.innerHTML = '<div class="state empty">no coins enabled — activate some under “coins”.</div>';
      note.textContent = '';
      return;
    }
    const balances = await Promise.all(list.map((c) => {
      const t = c.ticker || c.coin;
      return balance(t).then((r) => ({ ...r.result, coin: r.result.coin || t }))
        .catch((e) => ({ coin: t, error: e.message, rpcType: e.rpcType }));
    }));
    rows = balances;
    pending = null; sendErr = null;
    if (sendFor && !rows.some((r) => r.coin === sendFor && !r.error)) sendFor = null;
    renderTable();
    note.textContent = list.length + ' enabled';
  } catch (e) {
    table.innerHTML = `<div class="state error">failed to load enabled coins: ${esc(e.message)}</div>`;
    note.textContent = '';
  } finally {
    btn.disabled = false;
  }
}

function renderTable() {
  const table = document.getElementById('w-table');
  table.className = '';
  table.innerHTML = `
    <table>
      <thead><tr><th>coin</th><th class="right">balance</th><th class="right">unspendable</th>
        <th>address</th><th></th></tr></thead>
      <tbody>
        ${rows.map((r) => r.error ? `
          <tr class="row-error" data-coin-row="${esc(r.coin)}">
            <td>${esc(r.coin)}</td>
            <td colspan="4" class="muted">${esc(r.error)} <span class="tag">${esc(r.rpcType || '')}</span></td>
          </tr>` : `
          <tr data-coin-row="${esc(r.coin)}">
            <td class="strong">${esc(r.coin)}</td>
            <td class="right num w-bal">${num(r.balance)}</td>
            <td class="right num muted">${num(r.unspendable_balance || '0')}</td>
            <td class="mono muted" title="${esc(r.address)}">${esc(cut(r.address || '', 8, 6))}</td>
            <td class="right">
              <button class="btn small ghost" data-act="receive" data-coin="${esc(r.coin)}">receive</button>
              <button class="btn small ghost" data-act="send" data-coin="${esc(r.coin)}">send</button>
            </td>
          </tr>`).join('')}
      </tbody>
    </table>`;

  table.querySelectorAll('button[data-act]').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.act === 'receive') receive(b.dataset.coin);
    else openSend(b.dataset.coin);
  }));
  renderSend();
}

async function receive(coin) {
  const row = rows.find((r) => r.coin === coin);
  const addr = row && row.address;
  if (!addr) { emit('toast', { msg: 'no address known for ' + coin, kind: 'err' }); return; }
  try {
    await navigator.clipboard.writeText(addr);
    emit('toast', { msg: 'address copied (' + coin + ')' });
  } catch (e) {
    emit('toast', { msg: 'copy failed — address: ' + addr });
  }
}

function openSend(coin) {
  sendFor = coin;
  pending = null;
  sendErr = null;
  renderSend();
}

function renderSend() {
  const panel = document.getElementById('w-send-panel');
  const root = document.getElementById('w-send-root');
  document.getElementById('w-send-coin').textContent = sendFor ? '· ' + sendFor : '';
  if (!sendFor) { panel.hidden = true; root.innerHTML = ''; return; }
  panel.hidden = false;
  const r = rows.find((x) => x.coin === sendFor && !x.error);
  const bal = r ? num(r.balance) : '—';

  if (pending) { root.innerHTML = pendingHtml(bal); bindPending(); return; }

  root.innerHTML = `
    <div class="field"><label>coin</label>
      <select id="s-coin">${rows.filter((x) => !x.error)
        .map((x) => `<option value="${esc(x.coin)}" ${x.coin === sendFor ? 'selected' : ''}>${esc(x.coin)}</option>`).join('')}</select></div>
    <div class="field"><label>to address</label>
      <input id="s-to" placeholder="recipient address" spellcheck="false"></div>
    <div class="field"><label>amount <span class="muted">(available ${bal})</span></label>
      <div class="row"><input id="s-amount" placeholder="0.0" inputmode="decimal">
      <button id="s-max" class="btn small ghost">max</button></div></div>
    <div class="row">
      <button id="s-preview" class="btn">preview</button>
      <button id="s-close" class="btn ghost">close</button>
    </div>
    <div id="s-err" class="err-text">${sendErr ? esc(sendErr) : ''}</div>`;

  document.getElementById('s-coin').addEventListener('change', (e) => {
    sendFor = e.target.value; pending = null; sendErr = null; renderSend();
  });
  document.getElementById('s-max').addEventListener('click', () => {
    const r2 = rows.find((x) => x.coin === sendFor && !x.error);
    document.getElementById('s-amount').value = r2 ? num(r2.balance) : '';
  });
  document.getElementById('s-close').addEventListener('click', () => { sendFor = null; renderSend(); });
  document.getElementById('s-preview').addEventListener('click', preview);
}

function setSendErr(msg) {
  sendErr = msg;
  const el = document.getElementById('s-err');
  if (el) el.textContent = msg || '';
}

async function preview() {
  const to = document.getElementById('s-to').value.trim();
  const amount = document.getElementById('s-amount').value.trim();
  if (!to) { setSendErr('recipient address required'); return; }
  if (!(parseFloat(amount) > 0)) { setSendErr('amount must be > 0'); return; }
  setSendErr('');
  const btn = document.getElementById('s-preview');
  btn.disabled = true;
  try {
    const res = await withdraw({ coin: sendFor, to, amount, broadcast: false });
    pending = res;
    renderSend();
  } catch (e) {
    setSendErr(e.rpcType + ': ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

function pendingHtml(bal) {
  const p = pending;
  const fee = p.fee_details && (p.fee_details.amount !== undefined
    ? `${num(p.fee_details.amount)} ${esc(p.fee_details.coin || sendFor)}`
    : esc(JSON.stringify(p.fee_details)));
  return `
    <div class="state wait">unsigned transaction staged — daemon holds the inputs</div>
    <table>
      <tbody>
        <tr><td>tx hash</td><td class="mono">${esc(p.tx_hash || '')}</td></tr>
        <tr><td>amount</td><td class="num">${num(p.total_amount || p.amount || '')}</td></tr>
        <tr><td>fee</td><td class="num">${fee || '—'}</td></tr>
        <tr><td>balance change</td><td class="num">${num(p.my_balance_change || '')}</td></tr>
        <tr><td>to</td><td class="mono">${esc((p.to && p.to[0]) || '')}</td></tr>
      </tbody>
    </table>
    <div class="row" style="margin-top:var(--sp-3)">
      <button id="s-broadcast" class="btn">broadcast</button>
      <button id="s-discard" class="btn ghost danger">discard</button>
    </div>
    <div id="s-err" class="err-text">${sendErr ? esc(sendErr) : ''}</div>`;
}

function bindPending() {
  document.getElementById('s-broadcast').addEventListener('click', broadcast);
  document.getElementById('s-discard').addEventListener('click', () => {
    pending = null; sendErr = null; renderSend();
  });
}

async function broadcast() {
  const btn = document.getElementById('s-broadcast');
  btn.disabled = true;
  try {
    const res = await sendRaw(sendFor, pending.tx_hex);
    const hash = (res && res.result && res.result.tx_hash) || (res && res.tx_hash) || '';
    emit('toast', { msg: 'broadcast ' + sendFor + ' — ' + (hash ? cut(hash, 8, 8) : 'ok') });
    pending = null;
    sendFor = null;
    await refresh();
  } catch (e) {
    setSendErr(e.rpcType + ': ' + e.message);
    btn.disabled = false;
  }
}