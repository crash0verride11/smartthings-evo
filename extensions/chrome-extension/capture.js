// Load the captured callback from storage. The background worker stores it
// under `lastCallback` instead of passing it through this page's URL — putting
// the full sasdk:// URL in a query param gets blocked by content blockers
// (ERR_BLOCKED_BY_CLIENT). No crypto here — `code`/`state`/`*_server_url` are
// opaque ciphertext.

function toast() {
  const el = document.getElementById("toast");
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2000);
}

function wireCopy(btnId, getText) {
  document.getElementById(btnId).addEventListener("click", async () => {
    await navigator.clipboard.writeText(getText());
    toast();
  });
}

function render(callback) {
  const raw = (callback && callback.raw) || "";
  const params = (callback && callback.params) || {};
  const code = params.code || "";
  const error = params.error;

  if (!raw && !code) {
    document.getElementById("noCode").style.display = "block";
    if (error) {
      const desc = params.error_description || "";
      document.getElementById("noCodeMsg").textContent =
        `Login returned an error: ${error}${desc ? ` — ${desc}` : ""}`;
    }
    return;
  }

  document.getElementById("success").style.display = "block";

  // Primary affordance: the full raw callback URL (that's what the POC wants).
  document.getElementById("rawUrl").textContent = raw;
  wireCopy("copyRaw", () => raw);

  // Secondary: masked code, showing only the last 4 chars.
  const masked =
    code.length > 4 ? "•".repeat(code.length - 4) + code.slice(-4) : code;
  document.getElementById("authCode").textContent = masked || "(none)";
  wireCopy("copyCode", () => code);

  // Debug: list every forwarded param.
  const table = document.getElementById("paramsTable");
  for (const [k, v] of Object.entries(params)) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.textContent = k;
    const td = document.createElement("td");
    td.textContent = v;
    tr.append(th, td);
    table.append(tr);
  }
}

chrome.storage.local.get("lastCallback", ({ lastCallback }) => {
  render(lastCallback);
});
