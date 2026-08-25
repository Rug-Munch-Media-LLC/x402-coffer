"""Read-only ops dashboard for x402 Coffer.

A single self-contained page (no CDN, no build step) served at ``/dashboard``
that renders live revenue/settlement data from ``/v1/x402/metrics`` and
``/v1/x402/health``. Read-only by construction — it only GETs.
"""

from __future__ import annotations

from typing import Any

_DARK = "#0b0e14"
_CARD = "#131824"
_ACCENT = "#4ade80"
_TEXT = "#e6edf3"
_MUTED = "#8b949e"

_HTML = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>x402 Coffer — Ops</title>
<style>
  body {{ background:{_DARK}; color:{_TEXT}; font:14px/1.5 ui-monospace,Menlo,monospace;
         margin:0; padding:32px; }}
  h1 {{ font-size:18px; margin:0 0 4px; }} .sub {{ color:{_MUTED}; margin-bottom:24px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px;
          max-width:1100px; }}
  .card {{ background:{_CARD}; border:1px solid #1f2735; border-radius:10px; padding:16px; }}
  .k {{ color:{_MUTED}; font-size:11px; text-transform:uppercase; letter-spacing:.08em; }}
  .v {{ font-size:26px; margin-top:6px; }} .v.good {{ color:{_ACCENT}; }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
  td,th {{ padding:6px 8px; text-align:left; border-bottom:1px solid #1f2735; font-size:12px; }}
  th {{ color:{_MUTED}; font-weight:normal; text-transform:uppercase; font-size:10px; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px; }}
  .live {{ background:#12261a; color:{_ACCENT}; }}
  .scaffolded {{ background:#2a2113; color:#fbbf24; }}
  #err {{ color:#f87171; }}
</style></head><body>
<h1>🫖 x402 Coffer — Ops</h1>
<div class="sub">self-hosted x402 payment facilitator · read-only</div>
<div id="err"></div>
<div class="grid">
  <div class="card"><div class="k">Settled (atomic total)</div><div class="v good" id="settled">—</div></div>
  <div class="card"><div class="k">Verifications ✓ / ✗</div><div class="v" id="verifs">—</div></div>
  <div class="card"><div class="k">Settlements ✓ / ✗</div><div class="v" id="setts">—</div></div>
  <div class="card"><div class="k">Chain status</div><div id="chains" style="margin-top:8px">—</div></div>
</div>
<div class="grid" style="margin-top:16px">
  <div class="card" style="grid-column:1/-1">
    <div class="k">Recent settlements</div>
    <table><thead><tr><th>when</th><th>network</th><th>asset</th><th>amount (atomic)</th><th>tx</th></tr></thead>
    <tbody id="rows"></tbody></table>
  </div>
</div>
<script>
const j = async u => (await fetch(u)).json();
const esc = s => String(s ?? "").replace(/[<>&]/g, c => ({{"<":"&lt;",">":"&gt;","&":"&amp;"}})[c]);
async function refresh() {{
  try {{
    const [m, h] = await Promise.all([j("/v1/x402/metrics"), j("/v1/x402/health")]);
    const c = m.counters || {{}};
    document.getElementById("settled").textContent =
      Number(m.settled_amount_atomic_total || 0).toLocaleString();
    document.getElementById("verifs").textContent =
      `${{c.verifications_verified || 0}} / ${{c.verifications_rejected || 0}}`;
    document.getElementById("setts").textContent =
      `${{c.settlements_success || 0}} / ${{c.settlements_failed || 0}}`;
    const vs = (h.verification_status) || {{}};
    document.getElementById("chains").innerHTML = Object.entries(vs).map(([k, v]) =>
      `<span class="badge ${{String(v).startsWith("live") ? "live" : "scaffolded"}}">${{esc(k)}}: ${{esc(v)}}</span>`
    ).join(" ");
    const evs = (m.recent_settlements || []).slice(0, 12);
    document.getElementById("rows").innerHTML = evs.map(e =>
      `<tr><td>${{esc(e.at)}}</td><td>${{esc(e.network)}}</td><td>${{esc(e.asset)}}</td>` +
      `<td>${{esc(e.amount_atomic)}}</td><td>${{esc(String(e.tx_hash).slice(0, 14))}}…</td></tr>`
    ).join("") || "<tr><td colspan=5 style='color:#8b949e'>no settlements yet</td></tr>";
  }} catch (e) {{ document.getElementById("err").textContent = "fetch failed: " + e; }}
}}
refresh(); setInterval(refresh, 10000);
</script></body></html>"""


def register_dashboard_router() -> Any:
    """APIRouter serving the read-only dashboard at ``GET /dashboard``."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter(tags=["x402-dashboard"])

    @router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> str:
        return _HTML

    return router
