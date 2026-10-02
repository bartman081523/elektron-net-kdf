import { esc } from '../format.mjs';

// STUB — replaced in phase F3 with active swaps (active_swaps + SSE
// SWAP_STATUS), timeline and history (my_recent_swaps).
export function render(root) {
  root.innerHTML = `
  <div class="page stub">
    <section class="panel state">
      <h2>swaps</h2>
      <p class="p muted">Active swaps, timeline, history — arrives with
      phase F3.</p>
    </section>
  </div>`;
}