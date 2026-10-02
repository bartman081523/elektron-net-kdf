import { esc } from '../format.mjs';

// STUB — replaced in phase F2 with the trade form (sell/buy legacy envelope,
// FOK/GTC, min_volume), trade_preimage fee preview and my_orders + cancel.
export function render(root) {
  root.innerHTML = `
  <div class="page stub">
    <section class="panel state">
      <h2>trade</h2>
      <p class="p muted">Order form + own orders — arrives with phase F2
      (sell/buy, trade_preimage fee preview, cancel).</p>
    </section>
  </div>`;
}