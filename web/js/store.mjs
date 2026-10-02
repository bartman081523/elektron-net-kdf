// Session + event bus.
//
// The UI keeps NO trading state (the daemon is the source of truth); the only
// thing sessionStorage holds is how to REACH the daemon (url + rpc password +
// an SSE client id), and sessionStorage dies with the tab. Nothing is ever
// sent anywhere except the user's own daemon.

const KEY = 'elek.session';

export function loadSession() {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function saveSession(session) {
  sessionStorage.setItem(KEY, JSON.stringify(session));
}

export function clearSession() {
  sessionStorage.removeItem(KEY);
}

// Minimal event bus: on/off/emit. Views subscribe to connection lifecycle
// (connected / disconnected) and, later, to SSE topics.
const listeners = new Map();

export function on(type, fn) {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(fn);
}

export function off(type, fn) {
  const set = listeners.get(type);
  if (set) set.delete(fn);
}

export function emit(type, detail = undefined) {
  const set = listeners.get(type);
  if (!set) return;
  for (const fn of [...set]) {
    try {
      fn(detail);
    } catch (e) {
      console.error('bus listener failed', type, e);
    }
  }
}

// Random u64-ish client id for the SSE endpoint (kdf expects u64; values up
// to 2^53 are exact in JSON numbers, so build the id from two u32 draws with
// the top draw masked).
export function newClientId() {
  const lo = crypto.getRandomValues(new Uint32Array(1))[0];
  const hi = crypto.getRandomValues(new Uint32Array(1))[0] & 0x1fffff;
  return hi * 4294967296 + lo;
}