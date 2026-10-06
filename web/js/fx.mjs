// The unified ELEK price estimate (doc/elektron.md §13). electrs is the
// single point of responsibility: it derives the rate from the live P2P
// order book (kdf), falling back to the project's registry reference rate
// and, as a bound, the mining cost floor. elek-web serves the resulting
// rich-shape file same-origin at /fx/rates.json (env MM_WEB_FX_RATES).
// A rate is never fabricated here: endpoint off (404), unreachable, or an
// untrustworthy payload all return null and views render no rate line.

import { esc } from './format.mjs';

export const FX_PATH = '/fx/rates.json';

const num = (x) => (typeof x === 'number' && Number.isFinite(x) && x > 0) ? x : null;

/** Fetch + validate the snapshot. Returns null instead of inventing anything:
 *  - endpoint off / not JSON / wrong shapes → null
 *  - usd/eur ≤ 0 or missing (mempool "-1" padding, disabled fiat feed) → null
 *  - time ≤ 0 or missing → null ("no rate available" in mempool shape) */
export async function fetchFx() {
  let resp;
  try {
    resp = await fetch(FX_PATH, { cache: 'no-cache' });
  } catch {
    return null;
  }
  if (!resp.ok) return null;
  let v;
  try {
    v = await resp.json();
  } catch {
    return null;
  }
  if (!v || typeof v !== 'object') return null;
  const usd = num(v.usd);
  const eur = num(v.eur);
  if (!usd && !eur) return null;
  if (!(typeof v.time === 'number' && v.time > 0)) return null;
  const m = v.market;
  const market = m && typeof m === 'object' && typeof m.pair === 'string'
    ? {
      pair: m.pair,
      bestBid: num(m.best_bid),
      bestAsk: num(m.best_ask),
      mid: num(m.mid),
      asks: num(m.asks),
      bids: num(m.bids),
    }
    : null;
  return {
    ticker: typeof v.ticker === 'string' ? v.ticker : '',
    usd,
    eur,
    source: typeof v.source === 'string' ? v.source : 'unknown',
    time: v.time,
    usdPerBtc: num(v.usd_per_btc),
    market,
  };
}

/** "1 ELEK ≈ $103.20 ≈ €94.85 (p2p_market · 42s old)" — already escaped,
 *  ready to concatenate into innerHTML next to suggestion buttons.
 *  Age is recomputed from `time` at render call (electrs' own age_secs field
 *  freezes with the file — now - time is the honest freshness). */
export function fxLineHtml(snap) {
  if (!snap) return '';
  const parts = [];
  if (snap.usd !== null) parts.push('$' + fmtFiat(snap.usd));
  if (snap.eur !== null) parts.push('€' + fmtFiat(snap.eur));
  if (!parts.length) return '';
  const bits = [snap.source];
  bits.push(fmtAge(Math.max(0, Math.floor(Date.now() / 1000) - snap.time)) + ' old');
  return esc(`1 ${snap.ticker || 'ELEK'} ≈ ${parts.join(' ≈ ')} (${bits.join(' · ')})`);
}

// Display-only fiat shaping (values are electrs outputs, not daemon strings):
// large numbers keep 3 decimals, sub-unit rates keep 4 significant digits.
function fmtFiat(n) {
  return n >= 1000 ? n.toFixed(0) : n >= 1 ? n.toFixed(3) : Number(n.toPrecision(4)).toString();
}

function fmtAge(s) {
  if (s < 60) return s + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.round(s / 3600) + 'h';
  return Math.round(s / 86400) + 'd';
}