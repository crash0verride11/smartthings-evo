# SmartThings (Samsung) OAuth Helper — Chrome/Edge Extension

Captures the Samsung account `sasdk://` OAuth callback that browsers can't
navigate to, and hands its parameters to you (copy/paste) or to Home Assistant
(auto-forward). It is the SmartThings counterpart of the Mazda OAuth helper.

## Why this is needed

The SmartThings (Samsung account) login finishes by redirecting the browser to
a **custom URI scheme** the browser cannot open:

```
sasdk://saccount.auth.com.samsung.android.oneconnect?code=<enc>&api_server_url=<enc>
  &auth_server_url=<enc>&code_expires_in=300&state=<enc>&scope=iot.client%20mcs.client%20galaxystore.openapi&status=new
```

This extension intercepts that navigation and forwards **all** callback
parameters verbatim so the token exchange can complete.

## ⚠️ How this differs from the Mazda extension

In the SmartThings flow the callback `code`, `state`, and `*_server_url` are
**AES-encrypted** (hex ciphertext), not a readable JWT. Therefore:

- There is **no JWT/`state` parsing** (`isHomeAssistantFlow` was removed).
- The extension performs **no crypto**. It forwards params verbatim.
- Decryption and flow correlation happen **server-side in Home Assistant**,
  which needs the full param set — including `auth_server_url` and the encrypted
  `state` — not just `code`.

## Installation (load unpacked)

1. Open `chrome://extensions/` (or `edge://extensions/`).
2. Enable **Developer mode** (top-right toggle).
3. Click **Load unpacked** and select this `chrome-extension/` folder.
4. The SmartThings OAuth Helper icon appears in the toolbar.

After updating the repository, return to the extensions page and click
**Reload** for the unpacked extension. Verify that the extension has permissions
for `*.samsung.com` and `ha-oauth-assist.example.com` before starting a new HA
flow.

## Usage

### Automatic (paste-free) — with the Home Assistant integration ✅ working

This is the intended path and is confirmed working end-to-end (July 2026). The extension
does **no crypto**; all decryption/exchange happens server-side in the integration.

1. In HA, add the SmartThings integration and choose **Automatic (browser extension)**.
2. HA's config flow drives the browser to a sentinel
   `https://ha-oauth-assist.example.com/initiate/smartthings#state_HA=<jwt>&signin_url=<samsung>`
   (RFC 2606 reserved host; params in the never-transmitted fragment). This is the generic
   [OAuth Assist for HA](https://github.com/crash0verride11/ha-oauth-assist) sentinel, so either
   extension can drive the flow — install **one**, not both, or both will act on the same
   handoff and callback. The extension intercepts it before any request goes out, stores
   `state_HA` as `pendingFlow`, and opens the Samsung login. (The legacy
   `https://ha-sasdk.example.com/start?state_HA=…` query form is still accepted.)
3. Sign in (sign out first → `status=new`). On the `sasdk://` callback the extension rewrites
   to `https://my.home-assistant.io/redirect/oauth?code=<enc>&state=<state_HA>`. HA validates
   the JWT, matches the flow, and decrypts + exchanges using the `state`/`osp_host` it already
   holds. The entry is created — no pasting.

**How the two legs relate to the two states:** Samsung's own `state` is consumed as an AES key
and returned encrypted, so it can't carry HA's routing JWT. The sentinel is how HA hands
`state_HA` to the extension out-of-band; the extension only shuttles params.

> ⚠️ **Known issue (cosmetic, tabled):** the `/auth/external/callback` tab may show an
> HTTP 500 and be left open as a dead tab **even though authentication completes** in the
> original HA window. Safe to close it. Root cause is at HA's OAuth callback-view layer; the
> token exchange and config-entry creation can still succeed in the original HA window.

### Default: copy/paste (always works)

1. Start the login flow (e.g. run `python3 ../poc/samsung_st_auth.py` and open
   the printed `…/accounts/ANDROIDSDK/signInGate?…` URL in this browser).
2. **Sign out of Samsung first** (or use a private window) so it's a *fresh*
   login → `status=new`. A reused session returns `status=change` and a code not
   bound to the PKCE challenge.
3. Complete the Samsung login. On the final redirect the extension shows
   **`capture.html`** with the full `sasdk://…` URL and a Copy button; the
   toolbar badge shows **✓**.
4. Copy the full URL and paste it back into the POC prompt (or your HA flow).
   The code is valid for **~300s** — use it promptly.

### Optional: auto-forward to Home Assistant

Open the extension popup and set a **Home Assistant redirect URL** (e.g.
`https://<ha-host>/api/smartthings/oauth-callback`). When set, the extension
forwards the full param set there instead of showing the capture page. Only
enable this once the integration's custom redirect view exists (it must decrypt
`state` per pending flow and round-trip it for correlation). Clear the field to
return to the copy/paste page.

## Files

- `manifest.json` — MV3 config (`webNavigation`, `tabs`, `storage`, `cookies`; hosts
  `https://*.samsung.com/*`, plus `https://ha-oauth-assist.example.com/*` and the
  legacy `https://ha-sasdk.example.com/*` for the sentinel).
- `background.js` — service worker. `handleSentinel` intercepts the outbound
  `https://ha-oauth-assist.example.com/initiate/smartthings#…` (or legacy
  `https://ha-sasdk.example.com/…`) handoff (stores `pendingFlow`, opens the login);
  the `sasdk://` handler rewrites the callback to HA (via `pendingFlow`) or falls
  back to the optional `haTarget`, else the capture page. Both listeners are
  de-duped on `tabId+url`.
- `capture.html` / `capture.js` — capture page (paste fallback): full raw URL +
  masked code + debug param table, with Copy buttons. Data comes from
  `chrome.storage` (`lastCallback`), not the page URL.
- `popup.html` / `popup.js` — configure/clear the optional HA target; shows last
  capture; **Reset Samsung Auth** clears `account.samsung.com` cookies (mirrors
  the Mazda extension's "Clear cookies" button, scoped to Samsung).
- `icon16/48/128.svg` — toolbar icons.

## Troubleshooting

- **The flow never starts:** confirm Home Assistant Cloud is enabled and that
  the integration is using the automatic extension method. Reload the unpacked
  extension after every source update.
- **The entry is created but devices do not change:** this integration polls
  REST status every 30 seconds; it does not receive the official push events.
- **A callback was exposed in a log or screenshot:** revoke the affected
  Samsung/SmartThings session and remove the artifact. Callback URLs and tokens
  are secrets.

- **Dead 500 tab after automatic sign-in:** known cosmetic issue (see above) —
  authentication still completes in the original HA window; close the tab.
- **"Open website" didn't capture:** fixed by de-duping on `tabId+url` (was
  URL-only, which swallowed the button click right after the auto-open). Reload
  the extension if you still see it.
- **Pasted sentinel link gets searched instead of opened:** why the sentinel host
  is an `example.com` subdomain (a real public suffix) and not `…invalid` — Edge's
  omnibox searches a pasted `.invalid` link. Clicking the link always worked; the
  host change makes copy/paste into another browser work too.
- **Nothing captured:** ensure Developer mode is on and reload the extension.
  Custom-scheme handling differs across Chrome/Edge/OS scheme handlers; both
  `onBeforeNavigate` and `onErrorOccurred` are registered, but test on your
  target browser.
- **`invalid_grant` / expired code:** codes expire in ~300s and are single-use.
  Re-run, sign out first, and paste promptly.
- **Wrong `status`:** if you see `status=change`, you reused a session — sign
  out of Samsung and retry for `status=new`. Use the popup's **Reset Samsung
  Auth** button to clear `account.samsung.com` cookies without a manual
  sign-out.
