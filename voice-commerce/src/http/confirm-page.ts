/**
 * The GIMME-hosted purchase confirmation page, for channels that cannot run
 * a native confirmation (an MCP client without elicitation, or one that
 * supports only URL elicitation). Minimal, server-rendered, no scripts.
 */

const esc = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

type PageInput =
  | { notFound: true }
  | { error: string }
  | { done: "CONFIRM" | "DECLINE" }
  | { intentId: string; nonce: string; summary: string; total: number; currency: string; status: string };

export function confirmationPage(p: PageInput): string {
  let body: string;
  if ("notFound" in p) {
    body = `<h1>Link expired</h1><p>This confirmation link has expired or was already used. Ask your assistant to start the order again.</p>`;
  } else if ("error" in p) {
    body = `<h1>Not placed</h1><p>${esc(p.error)}</p>`;
  } else if ("done" in p) {
    body =
      p.done === "CONFIRM"
        ? `<h1>Confirmed</h1><p>Thanks. Return to your assistant to finish placing the order.</p>`
        : `<h1>Cancelled</h1><p>No problem — this order won't be placed.</p>`;
  } else if (p.status !== "AWAITING_CONFIRMATION") {
    body = `<h1>Nothing to confirm</h1><p>This order is ${esc(p.status.toLowerCase().replace(/_/g, " "))}.</p>`;
  } else {
    const action = `/v1/voice/confirm/${encodeURIComponent(p.intentId)}`;
    body = `<h1>Confirm your GIMME order</h1>
<p class="summary">${esc(p.summary)}</p>
<p class="total">$${p.total.toFixed(2)} ${esc(p.currency)}</p>
<form method="post" action="${esc(action)}">
  <input type="hidden" name="nonce" value="${esc(p.nonce)}">
  <button name="decision" value="CONFIRM" class="primary">Place order for $${p.total.toFixed(2)}</button>
  <button name="decision" value="DECLINE">Cancel</button>
</form>
<p class="fine">You must be 18 or over to buy alcohol. ID is checked on delivery.</p>`;
  }
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GIMME — confirm order</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:28rem;margin:2rem auto;padding:0 1rem;color:#111;background:#fff}
h1{font-size:1.4rem}.total{font-size:2rem;font-weight:700;margin:.5rem 0 1.5rem}button{display:block;width:100%;padding:.9rem;margin:.5rem 0;font-size:1rem;border-radius:.6rem;border:1px solid #ccc;background:#fff}
button.primary{background:#111;color:#fff;border-color:#111}.fine{color:#555;font-size:.85rem}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}button{background:#222;color:#eee;border-color:#444}button.primary{background:#eee;color:#111}.fine{color:#aaa}}</style>
</head><body>${body}</body></html>`;
}
