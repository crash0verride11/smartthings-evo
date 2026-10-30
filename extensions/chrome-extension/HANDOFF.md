# Handoff — SmartThings (Samsung OSP) callback-capture Chrome extension

## Objective
Build a Chrome/Edge MV3 extension that captures the **`sasdk://`** OAuth callback emitted by
the Samsung account login and hands it to Home Assistant (and/or shows it for copy‑paste).
Repurpose the extension I maintain in **fano0001/home-assistant-mazda**
(`browser-extensions/chrome-extension/`) and place the result in **this `chrome-extension/`
folder**. This is a self‑contained task — the auth reverse‑engineering is already done.

## Why the extension exists
The SmartThings login (Samsung account) finishes by redirecting the browser to a **custom URI
scheme** that the browser can’t navigate to:

```
sasdk://saccount.auth.com.samsung.android.oneconnect?code=<enc>&api_server_url=<enc>
   &auth_server_url=<enc>&code_expires_in=300&state=<enc>&scope=iot.client%20mcs.client%20galaxystore.openapi&status=new
```

The extension intercepts that redirect and forwards its query params to Home Assistant so the
integration can finish the token exchange. Exactly the role the Mazda extension plays.

## What the auth flow is (context — you do NOT need to reproduce it)
Proven end‑to‑end in `../poc/samsung_st_auth.py` (read it if useful). The browser‑facing parts:
- HA opens the Samsung login at `https://account.samsung.com/accounts/ANDROIDSDK/signInGate?locale=en_US&svcParam=…`
  (HA builds `svcParam`, which embeds a PKCE `code_challenge` and a random `state`).
- The user signs in; the page redirects to the `sasdk://…` URL above.
- **The extension’s only job: capture that `sasdk://` URL and deliver its params to HA.**
- HA then (server‑side) decrypts `state`/`code`/`auth_server_url` and does the token POST.

## ⚠️ THE ONE CRITICAL DIFFERENCE FROM MAZDA (read this)
In the Mazda extension, `background.js` decides “is this a Home Assistant flow?” by parsing the
`state` param as a **JWT with `flow_id`** (`isHomeAssistantFlow`). **That will NOT work here.**

In the SmartThings flow the callback `code`, `state`, and `*_server_url` are **AES‑encrypted**
(`state` is `AES-ECB(our_original_state)`, not a readable JWT). So:
- **Do NOT try to parse `state` as a JWT.** It’s opaque ciphertext.
- The extension must **forward ALL callback params verbatim** (`code`, `state`,
  `api_server_url`, `auth_server_url`, `code_expires_in`, `scope`, `status`, plus `error`/
  `error_description` if present). HA needs them to decrypt + exchange.
- The extension performs **no crypto**. Decryption + correlation happen HA‑side.

### HA correlation note (for whoever builds the integration, not the extension)
Because the returned `state` is encrypted, HA’s *standard* OAuth2 `state`→flow routing (what
`my.home-assistant.io/redirect/oauth` relies on) can’t match it. The integration will need a
custom redirect view that correlates by **trying each pending flow**: decrypt the callback
`state` with that flow’s stored `state[:16]` (AES‑ECB) and keep the flow where it round‑trips
back to its own `state` (the integrity check). The extension just needs to get the params to
that HA view. Keep the extension dumb; keep options open (see “Delivery targets”).

## Callback specifics (constants for the extension)
- **Scheme/prefix to match:** `sasdk://saccount.auth.com.samsung.android.oneconnect`
- **Login host to scope permissions to:** `https://account.samsung.com/*`
- **Params present:** `code`, `api_server_url`, `auth_server_url`, `code_expires_in` (=300),
  `state`, `scope` (`iot.client mcs.client galaxystore.openapi`), `status` (`new`/`change`).
  `code`/`state`/`*_server_url` are hex ciphertext; `scope`/`status`/`code_expires_in` plaintext.
- Code TTL is **300s** — deliver promptly.

## Delivery targets (make it configurable; default to the safe one)
1. **Capture page (default, always works):** rewrite the tab to `capture.html?…` showing the
   **full raw callback URL** with a Copy button. This unblocks both the POC (paste into
   `samsung_st_auth.py`) and any HA flow, with zero assumptions. Keep the Mazda masking/toast UX.
2. **Auto‑forward to HA (optional, popup‑configurable):** POST/redirect the full param set to a
   user‑entered HA redirect URL (e.g. `https://<ha-host>/api/smartthings/oauth-callback` or
   `https://my.home-assistant.io/redirect/oauth`). Only enable once the integration’s custom
   redirect view exists (see correlation note). Store the target in `chrome.storage.local` via
   the popup.

## Source to repurpose (from fano0001/home-assistant-mazda, `browser-extensions/chrome-extension/`)
Files: `manifest.json`, `background.js`, `capture.html`, `capture.js`, `popup.html`, `popup.js`,
`icon16.svg`, `icon48.svg`, `icon128.svg`, `README.md`. Fetch them from the repo (raw URLs under
`https://raw.githubusercontent.com/fano0001/home-assistant-mazda/main/browser-extensions/chrome-extension/…`).

### Reference: the Mazda `background.js` (the thing you’re adapting)
```javascript
const MAZDA_REDIRECT_PREFIXES = [
  "msauth.com.mazdausa.mazdaiphone://auth",
  "msauth://com.interrait.mymazda",
];
function isMazdaRedirect(url){ return MAZDA_REDIRECT_PREFIXES.some(p => url.startsWith(p)); }
function isHomeAssistantFlow(state){ /* parses state as JWT w/ flow_id — REMOVE for ST */ }

chrome.webNavigation.onBeforeNavigate.addListener(async (details) => {
  const url = details.url;
  if (isMazdaRedirect(url)) {
    const q = new URL(url).searchParams;
    const code = q.get("code"), state = q.get("state");
    if (code && isHomeAssistantFlow(state)) {
      const haUrl = new URL("https://my.home-assistant.io/redirect/oauth");
      haUrl.searchParams.set("code", code); haUrl.searchParams.set("state", state);
      chrome.tabs.update(details.tabId, { url: haUrl.toString() }); return;
    }
    const captureUrl = new URL(chrome.runtime.getURL("capture.html"));
    captureUrl.searchParams.set("code", code || ""); /* …error params… */
    if (code){ chrome.action.setBadgeText({text:"✓"}); chrome.action.setBadgeBackgroundColor({color:"#4CAF50"}); }
    chrome.tabs.update(details.tabId, { url: captureUrl.toString() });
  }
});
chrome.webNavigation.onErrorOccurred.addListener(/* same handling; fires when Chrome can't open the scheme */);
```

### Target: adapted `background.js` (starting point)
```javascript
const ST_REDIRECT_PREFIX = "sasdk://saccount.auth.com.samsung.android.oneconnect";
const FORWARD_PARAMS = ["code","state","api_server_url","auth_server_url",
  "code_expires_in","scope","status","error","error_description"];

function isStRedirect(url){ return typeof url === "string" && url.startsWith(ST_REDIRECT_PREFIX); }

async function handle(details){
  if (!isStRedirect(details.url)) return;
  const q = new URL(details.url).searchParams;

  // Default: capture page with full param set + raw URL for copy. (No JWT parsing — state is
  // encrypted.) HA does all decryption/correlation.
  const cap = new URL(chrome.runtime.getURL("capture.html"));
  for (const k of FORWARD_PARAMS){ const v = q.get(k); if (v !== null) cap.searchParams.set(k, v); }
  cap.searchParams.set("raw", details.url);

  if (q.get("code")){
    chrome.action.setBadgeText({ text: "✓" });
    chrome.action.setBadgeBackgroundColor({ color: "#4CAF50" });
  }

  // Optional auto-forward (only if user configured a HA target in the popup):
  const { haTarget } = await chrome.storage.local.get("haTarget");
  if (haTarget){
    const ha = new URL(haTarget);
    for (const k of FORWARD_PARAMS){ const v = q.get(k); if (v !== null) ha.searchParams.set(k, v); }
    chrome.tabs.update(details.tabId, { url: ha.toString() });
    return;
  }
  chrome.tabs.update(details.tabId, { url: cap.toString() });
}

chrome.webNavigation.onBeforeNavigate.addListener(handle);   // some browsers fire this for custom schemes
chrome.webNavigation.onErrorOccurred.addListener(handle);    // most fire this (scheme not navigable)
```
Note: whether `onBeforeNavigate` or `onErrorOccurred` fires for a non‑navigable scheme varies by
browser/version — register both (as Mazda does) and de‑dupe if needed.

### `manifest.json` changes
```jsonc
{
  "manifest_version": 3,
  "name": "SmartThings (Samsung) OAuth Helper",
  "version": "0.1.0",
  "description": "Captures the Samsung account sasdk:// redirect for the Home Assistant SmartThings integration.",
  "permissions": ["webNavigation", "tabs", "storage"],
  "host_permissions": ["https://account.samsung.com/*"],
  "background": { "service_worker": "background.js" },
  "action": { "default_popup": "popup.html", "default_title": "SmartThings OAuth Helper" }
}
```
(Drop Mazda’s `cookies` permission and its `*.id.mazda.com` hosts. Add `storage` for the popup‑set HA target.)

### `capture.html` / `capture.js`
- Show the **full raw callback URL** (`?raw=…`) with a Copy button (primary affordance — that’s
  what the POC needs). Keep Mazda’s masked‑code display + toast for polish.
- Optionally list the decoded params in a table for debugging.

### `popup.html` / `popup.js`
- One field: “Home Assistant redirect URL (optional)” → save to `chrome.storage.local.haTarget`.
- Status line: last capture time / whether a target is configured.

### Icons
Reuse Mazda’s SVGs or drop in SmartThings‑flavored ones (`icon16/48/128.svg`). Cosmetic.

## Acceptance test
1. Load unpacked (`chrome://extensions` → Developer mode → Load unpacked → this folder).
2. Run `python3 ../poc/samsung_st_auth.py`, copy the printed `…/ANDROIDSDK/signInGate?…` URL,
   open it in the extension’s browser, complete a **fresh** Samsung login (sign out first so
   `status=new`).
3. On redirect, the extension should either show `capture.html` with the full `sasdk://…` URL +
   Copy, or (if a HA target is set) forward the params there. The toolbar badge shows `✓`.
4. Paste the captured URL back into the POC prompt → it decrypts + exchanges → token. (Confirms
   the extension captured a complete, uncorrupted callback.)

## Guardrails / gotchas
- **No crypto in the extension.** Forward params verbatim; do not URL‑re‑encode the hex values in
  a lossy way (use `URLSearchParams`/`URL`, which handle this correctly).
- **Don’t parse `state` as JWT** (it’s AES ciphertext). Removing `isHomeAssistantFlow` is required.
- **Forward the full param set**, not just `code`+`state` — HA needs `auth_server_url` (host) and
  the encrypted `state` for correlation.
- Custom‑scheme handling differs across Chrome/Edge/Firefox and OS scheme handlers; test on the
  target browser. Registering both `onBeforeNavigate` and `onErrorOccurred` is the robust move.
- Code TTL 300s — the capture page should encourage prompt use.

## Out of scope (later, in the integration task)
- The HA custom redirect view + flow correlation (decrypt `state` per pending flow; integrity
  round‑trip) and the token exchange itself — those live in the Python integration, not here.

## Pointers
- Working reference implementation of the full flow: `../poc/samsung_st_auth.py`
  (see `sasdk_decrypt`, `build_svc_param_vo`, `exchange_code`).
- Architecture writeup: `../CLAUDE.md`.
- Mazda extension to repurpose: https://github.com/fano0001/home-assistant-mazda
  (`browser-extensions/chrome-extension/`).
```
