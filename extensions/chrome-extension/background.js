/**
 * SmartThings (Samsung OSP) OAuth Helper - Background Service Worker
 *
 * The Samsung account login finishes by redirecting the browser to a custom
 * URI scheme the browser cannot navigate to:
 *
 *   sasdk://saccount.auth.com.samsung.android.oneconnect?code=<enc>&...
 *
 * This worker intercepts that redirect and forwards its query params, verbatim,
 * to a capture page (default) or a user-configured Home Assistant target.
 *
 * IMPORTANT — how this differs from the Mazda extension it was adapted from:
 *   The SmartThings callback `code`, `state`, and `*_server_url` are AES
 *   ciphertext (hex), NOT a readable JWT. So there is deliberately NO
 *   `isHomeAssistantFlow`/JWT parsing here, and NO crypto of any kind. We
 *   forward the full param set unchanged; Home Assistant decrypts + correlates.
 */

const ST_REDIRECT_PREFIX =
  "sasdk://saccount.auth.com.samsung.android.oneconnect";

// Every param the callback may carry. `code`/`state`/`*_server_url` are hex
// ciphertext; `scope`/`status`/`code_expires_in` are plaintext. Forward all.
const FORWARD_PARAMS = [
  "code",
  "state",
  "api_server_url",
  "auth_server_url",
  "code_expires_in",
  "scope",
  "status",
  "error",
  "error_description",
];

function isStRedirect(url) {
  return typeof url === "string" && url.startsWith(ST_REDIRECT_PREFIX);
}

// --------------------------------------------------------------------------- //
// Outbound leg: the HA -> extension handoff.
//
// HA's config flow can't hand the extension `state_HA` (its routing JWT) through
// the Samsung login, because Samsung's `state` is consumed as an AES key and
// comes back encrypted. So HA drives the browser to a sentinel URL carrying both
// `state_HA` and the real Samsung `signin_url`. We intercept it in
// `onBeforeNavigate` (which fires before the network request), stash `state_HA`,
// then navigate the tab on to `signin_url`. The extension does no crypto.
//
// The integration emits the generic OAuth Assist for HA sentinel, so either
// extension can drive the flow:
//
//   https://ha-oauth-assist.example.com/initiate/smartthings#state_HA=<jwt>&signin_url=<url>
//
// Params ride in the FRAGMENT, which is never transmitted. The legacy
// `https://ha-sasdk.example.com/…?state_HA=…` query form is still accepted for
// integration versions that predate the switch.
//
// Hosts are RFC 2606 reserved `example.com` subdomains — never a real third party.
// We deliberately use a real public suffix (`.com`) rather than a reserved
// special-use TLD like `.invalid`: browser omniboxes navigate a pasted
// `https://…example.com/…` link, but Edge's omnibox SEARCHES a pasted
// `…invalid/…` link instead (breaking the "copy the link into another browser"
// case). Interception happens pre-request, so nothing is ever sent to example.com
// when the extension is active.
// --------------------------------------------------------------------------- //

const SENTINEL_PREFIX = "https://ha-oauth-assist.example.com/initiate/smartthings#";
const LEGACY_SENTINEL_PREFIX = "https://ha-sasdk.example.com/";

// Where codes are forwarded. Fixed, never taken from the sentinel: it routes to
// the user's OWN HA instance, so a planted handoff can't steer a code elsewhere.
const HA_FORWARD_URL = "https://my.home-assistant.io/redirect/oauth";

// The handoff params of a sentinel URL (either form), or null if it isn't one.
function sentinelParams(url) {
  if (typeof url !== "string") return null;
  if (url.startsWith(SENTINEL_PREFIX)) {
    return new URLSearchParams(url.slice(SENTINEL_PREFIX.length));
  }
  if (url.startsWith(LEGACY_SENTINEL_PREFIX)) return new URL(url).searchParams;
  return null;
}

// ANY WEB PAGE CAN NAVIGATE TO THE SENTINEL, so its params are untrusted. Only
// send the tab on to a Samsung login page — otherwise this is an open redirect.
function isSamsungLogin(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    return (
      u.protocol === "https:" &&
      (host === "samsung.com" || host.endsWith(".samsung.com"))
    );
  } catch (e) {
    return false;
  }
}

// Structural check only (no crypto): HA's state is a JWT naming a flow_id.
// Rejects garbage before we act on it; HA itself verifies the signature.
function isHomeAssistantFlow(state) {
  if (!state) return false;
  try {
    const parts = state.split(".");
    if (parts.length !== 3) return false;
    const payload = JSON.parse(
      atob(parts[1].replace(/-/g, "+").replace(/_/g, "/")),
    );
    return payload && typeof payload.flow_id === "string";
  } catch (e) {
    return false;
  }
}

// De-dupe: both onBeforeNavigate and onErrorOccurred can fire for the same
// non-navigable custom scheme, and behavior varies by browser/version.
let lastHandledUrl = null;
let lastHandledAt = 0;

// De-dupe the sentinel per (tab, url): collapses onBeforeNavigate + onErrorOccurred
// firing for the SAME navigation, but still allows a genuine second attempt — e.g.
// the user clicking "Open website" after the auto-open, which lands in a different tab.
let lastSentinelKey = null;
let lastSentinelAt = 0;

async function handleSentinel(details) {
  // Top-level navigations only: a hidden iframe cannot plant a flow silently.
  if (details.frameId !== 0) return;

  const url = details.url;
  const q = sentinelParams(url);
  if (!q) return;

  const stateHa = q.get("state_HA");
  const signinUrl = q.get("signin_url");
  if (!isHomeAssistantFlow(stateHa) || !isSamsungLogin(signinUrl)) return;

  const now = Date.now();
  const key = `${details.tabId}|${url}`;
  if (key === lastSentinelKey && now - lastSentinelAt < 2000) return;
  lastSentinelKey = key;
  lastSentinelAt = now;

  // Redirect to the real Samsung login FIRST. The sentinel host resolves (it's a
  // real public suffix), so leaving early minimises any chance it paints/loads
  // before we take over. The callback that needs `pendingFlow` doesn't arrive
  // until after login (seconds later), so persisting it just after is fine.
  chrome.tabs.update(details.tabId, { url: signinUrl });

  chrome.action.setBadgeText({ text: "…" });
  chrome.action.setBadgeBackgroundColor({ color: "#1428a0" });

  // Remember which pending HA flow this tab belongs to, so the eventual
  // sasdk:// callback can be routed back with the right state_HA. The tabId
  // binding keeps a flow planted in one tab from capturing a login in another.
  await chrome.storage.local.set({
    pendingFlow: { stateHa, tabId: details.tabId, time: now },
  });
}

chrome.webNavigation.onBeforeNavigate.addListener(handleSentinel);
chrome.webNavigation.onErrorOccurred.addListener(handleSentinel);

async function handle(details) {
  const url = details.url;
  if (!isStRedirect(url)) return;

  const now = Date.now();
  if (url === lastHandledUrl && now - lastHandledAt < 5000) return;
  lastHandledUrl = url;
  lastHandledAt = now;

  const q = new URL(url).searchParams;

  const hasCode = Boolean(q.get("code"));
  if (hasCode) {
    chrome.action.setBadgeText({ text: "✓" });
    chrome.action.setBadgeBackgroundColor({ color: "#4CAF50" });
  }

  // Collect the forwarded params into a plain object.
  const params = {};
  for (const k of FORWARD_PARAMS) {
    const v = q.get(k);
    if (v !== null) params[k] = v;
  }

  // Persist the callback in storage (NOT in the capture URL). Passing the full
  // sasdk:// URL through capture.html's query string carried a nested URL in a
  // `raw=` param, which content blockers' anti-redirect filters treat as a
  // tracking redirect and refuse with ERR_BLOCKED_BY_CLIENT (Mazda's short,
  // token-only capture URL never trips this). Storage sidesteps it entirely and
  // also removes any URL-length limit. capture.js reads `lastCallback` back.
  await chrome.storage.local.set({
    lastCallback: { raw: url, params, time: now },
    lastCapture: { time: now, hasCode },
  });

  // Preferred path: an HA-initiated flow. The sentinel handoff left a
  // `pendingFlow` with `state_HA` (HA's routing JWT). Rewrite to the HA OAuth
  // router (my.home-assistant.io) carrying the encrypted `code` and `state_HA`
  // — HA validates the JWT, matches the flow, and decrypts/exchanges
  // server-side using the `state_S` it already holds. We forward ONLY code +
  // state (all my.home-assistant.io forwards anyway); HA needs nothing else.
  const { pendingFlow } = await chrome.storage.local.get("pendingFlow");
  const PENDING_TTL_MS = 10 * 60 * 1000;
  if (
    pendingFlow &&
    pendingFlow.stateHa &&
    pendingFlow.tabId === details.tabId &&
    now - (pendingFlow.time || 0) < PENDING_TTL_MS &&
    (params.code || params.error)
  ) {
    const ha = new URL(HA_FORWARD_URL);
    if (params.code) ha.searchParams.set("code", params.code);
    if (params.error) {
      ha.searchParams.set("error", params.error);
      if (params.error_description)
        ha.searchParams.set("error_description", params.error_description);
    }
    ha.searchParams.set("state", pendingFlow.stateHa);
    await chrome.storage.local.remove("pendingFlow");
    chrome.tabs.update(details.tabId, { url: ha.toString() });
    return;
  }

  // Optional auto-forward: only when the user configured a HA target. Forward
  // the full param set (URLSearchParams re-encodes the hex values losslessly).
  const { haTarget } = await chrome.storage.local.get("haTarget");
  if (haTarget) {
    try {
      const ha = new URL(haTarget);
      for (const [k, v] of Object.entries(params)) ha.searchParams.set(k, v);
      chrome.tabs.update(details.tabId, { url: ha.toString() });
      return;
    } catch (e) {
      // Bad target URL — fall through to the capture page so nothing is lost.
      console.warn("Invalid haTarget, falling back to capture page:", e);
    }
  }

  // Default: navigate to a bare capture page; it loads the data from storage.
  chrome.tabs.update(details.tabId, {
    url: chrome.runtime.getURL("capture.html"),
  });
}

// Some browsers fire onBeforeNavigate for custom schemes; most fire
// onErrorOccurred (the scheme isn't navigable). Register both.
chrome.webNavigation.onBeforeNavigate.addListener(handle);
chrome.webNavigation.onErrorOccurred.addListener(handle);
