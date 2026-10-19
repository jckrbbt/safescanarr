// Sign-out UI regression (round-2 fix 5): /logout returning non-2xx must
// NOT redirect to /login; the error is shown and the user can retry.
// Loads app.js into a node vm with a minimal DOM/fetch stub.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import vm from "node:vm";

const src = readFileSync(new URL("../../web/static/app.js", import.meta.url), "utf8");

function makeHarness({ logoutStatus, networkError }) {
  const calls = { redirected: false, toasts: [], location: null };
  const document = {
    _nodes: {},
    querySelector: () => null,
    getElementById: (id) => document._nodes[id] || null,
    createElement: () => ({
      style: {}, classList: { add() {}, toggle() {} },
      setAttribute() {}, appendChild() {}, addEventListener() {},
    }),
    body: { appendChild() {} },
    addEventListener: () => {},
  };
  const sandbox = {
    window: {},
    document,
    setInterval: () => 0,
    clearInterval: () => {},
    setTimeout: (fn) => { sandbox._timeouts.push(fn); return 0; },
    _timeouts: [],
    localStorage: { getItem: () => null, setItem() {} },
    matchMedia: () => ({ matches: false }),
    URLSearchParams: class { get() { return null; } },
    navigator: {},
    console,
    fetch: async () => {
      if (networkError) throw new TypeError("network down");
      return {
        ok: logoutStatus >= 200 && logoutStatus < 300,
        status: logoutStatus,
        json: async () => ({ status: "error", message: "Sign-out failed: could not revoke the remembered session server-side. Try again." }),
      };
    },
  };
  sandbox.window = sandbox;
  sandbox.window.location = { set href(v) { calls.redirected = true; calls.location = v; }, get href() { return calls.location || "/"; } };
  sandbox.window.fetch = sandbox.fetch;
  sandbox.window.matchMedia = sandbox.matchMedia;
  sandbox.setTimeout = sandbox.setTimeout;
  sandbox.window.setTimeout = sandbox.setTimeout;
  // toast() writes into #toast-container via document helpers above; capture
  // messages by wrapping createElement to record text content set later.
  const origCreate = document.createElement.bind(document);
  document.createElement = () => {
    const el = {
      style: {}, classList: { add() {}, toggle() {} },
      setAttribute() {}, appendChild(child) {
        if (child && child._text) calls.toasts.push(child._text);
      },
      addEventListener() {},
      set innerHTML(v) {
        const m = /<span>([^<]*)<\/span>/.exec(v);
        if (m) { el._text = m[1]; calls.toasts.push(m[1]); }
      },
      appendChild2() {},
    };
    return el;
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(src, ctx, { filename: "app.js" });
  return { ctx, calls };
}

async function run() {
  // 500 from /logout: no redirect, error surfaced
  {
    const { ctx, calls } = makeHarness({ logoutStatus: 500 });
    await vm.runInContext("signOut()", ctx);
    if (calls.redirected) throw new Error("FAIL: redirected to /login on 500");
    if (!calls.toasts.some(t => /sign-out failed/i.test(t)))
      throw new Error("FAIL: no error toast on 500 (toasts=" + JSON.stringify(calls.toasts) + ")");
    console.log("OK logout-500: no redirect, error shown");
  }
  // network error: no redirect, error surfaced
  {
    const { ctx, calls } = makeHarness({ logoutStatus: 0, networkError: true });
    await vm.runInContext("signOut()", ctx);
    if (calls.redirected) throw new Error("FAIL: redirected on network error");
    if (!calls.toasts.some(t => /sign-out failed/i.test(t)))
      throw new Error("FAIL: no error toast on network error");
    console.log("OK network-error: no redirect, error shown");
  }
  // 2xx: redirect happens
  {
    const { ctx, calls } = makeHarness({ logoutStatus: 200 });
    await vm.runInContext("signOut()", ctx);
    if (!calls.redirected) throw new Error("FAIL: no redirect on success");
    console.log("OK logout-200: redirects to /login");
  }
  console.log("ALL OK");
}

run().catch((e) => { console.error(e.message || e); process.exit(1); });
