import { esc } from '../format.mjs';

// STUB — replaced in phase F2 with the live orderbook (SSE ORDERBOOK_UPDATE
// on topic "orbk:<alb-pair>", asks/bids depth bars, own-order highlight via
// is_mine / own pubkey).
export function render(root) {
  root.innerHTML = `
  <div class="page stub">
    <section class="panel state">
      <h2>orderbook</h2>
      <p class="p muted">Live market view — arrives with phase F2:
      asks/bids with depth bars, live updates via SSE
      (<code>ORDERBOOK_UPDATE:orbk:…</code>).</p>
    </section>
  </div>`;
}