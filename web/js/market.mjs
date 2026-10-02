import { enabledCoins } from './api.mjs';

// Pair choice shared by the orderbook and trade views. The last pair lives
// in sessionStorage — it dies with the tab, like the daemon session itself.

const KEY = 'elek.marketPair';

export function loadPair() {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (p && p.base && p.rel) return p;
    }
  } catch { /* fall through to empty */ }
  return { base: '', rel: '' };
}

export function savePair(pair) {
  if (pair && pair.base && pair.rel) {
    sessionStorage.setItem(KEY, JSON.stringify(pair));
  }
}

/** Tickers of the enabled coins, in daemon order. */
export async function coinList() {
  const r = await enabledCoins();
  const list = Array.isArray(r && r.result) ? r.result : [];
  return list.map((c) => c.ticker || c.coin).filter(Boolean);
}