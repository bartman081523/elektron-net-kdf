import { esc } from '../format.mjs';

// STUB — replaced in phase F1 with balances (my_balance per activated coin),
// receive address (optional QR), send via withdraw (v2 envelope) +
// send_raw_transaction.
export function render(root) {
  root.innerHTML = `
  <div class="page">
    <section class="panel state">
      <h2>wallet</h2>
      <p class="p muted">Balances, receive address, send — arrives with
      phase F1.</p>
    </section>
  </div>`;
}