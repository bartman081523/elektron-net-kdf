import { esc } from '../format.mjs';

// STUB — replaced in phase F4 with the coin activation panel (legacy
// `electrum` envelope exactly as scripts/elektron/testnet_rpc.py).
export function render(root) {
  root.innerHTML = `
  <div class="page stub">
    <section class="panel state">
      <h2>coins</h2>
      <p class="p muted">Coin activation (electrum servers) — arrives with
      phase F4.</p>
    </section>
  </div>`;
}