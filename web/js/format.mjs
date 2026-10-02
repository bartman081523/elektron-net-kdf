// Formatting helpers. Prices/volumes arrive as strings from the daemon
// (MmNumber); never let them go through float rounding beyond display.

export function num(x, dp = 8) {
  if (x === null || x === undefined || x === '') return '–';
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  let s = n.toFixed(dp);
  if (s.includes('.')) s = s.replace(/0+$/, '').replace(/\.$/, '');
  return s === '-0' ? '0' : s;
}

// Compact volume for depth bars / summaries: 1.2M, 890K, 12.5
export function vol(x) {
  if (x === null || x === undefined || x === '') return '–';
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return num(x, 4);
}

// "12s" / "3m" / "2h" / "5d" since a unix-seconds timestamp.
export function ago(unixSeconds) {
  if (!unixSeconds) return '–';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - Number(unixSeconds)));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}

export function cut(s, head = 6, tail = 4) {
  if (typeof s !== 'string') return '–';
  return s.length <= head + tail + 1 ? s : s.slice(0, head) + '…' + s.slice(-tail);
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}