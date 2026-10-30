const targetInput = document.getElementById("ha-target");
const saveStatus = document.getElementById("save-status");
const captureStatus = document.getElementById("capture-status");

// Restore saved settings and render the last-capture / target status line.
chrome.storage.local.get(["haTarget", "lastCapture"], ({ haTarget, lastCapture }) => {
  if (haTarget) targetInput.value = haTarget;

  const bits = [];
  bits.push(haTarget ? "Auto-forward: ON" : "Auto-forward: off (copy/paste)");
  if (lastCapture && lastCapture.time) {
    const when = new Date(lastCapture.time).toLocaleString();
    bits.push(
      `Last capture: ${when}${lastCapture.hasCode ? " ✓" : " (no code)"}`
    );
  } else {
    bits.push("No capture yet");
  }
  captureStatus.textContent = bits.join(" · ");
});

document.getElementById("save-target").addEventListener("click", () => {
  const value = targetInput.value.trim();
  if (!value) {
    saveStatus.textContent = "Enter a URL, or use Clear to disable forwarding.";
    return;
  }
  try {
    new URL(value); // validate before saving
  } catch {
    saveStatus.textContent = "That doesn't look like a valid URL.";
    return;
  }
  chrome.storage.local.set({ haTarget: value }, () => {
    saveStatus.textContent = "Saved. Callbacks will auto-forward here.";
  });
});

document.getElementById("clear-target").addEventListener("click", () => {
  chrome.storage.local.remove("haTarget", () => {
    targetInput.value = "";
    saveStatus.textContent = "Cleared. Callbacks will show the capture page.";
  });
});

// Mirrors the Mazda OAuth Helper's "Clear cookies" button (fano0001/home-assistant-mazda),
// scoped to account.samsung.com: clears a stale session so the next login is fresh
// (status=new) instead of reused (status=change, which returns a code not bound to our PKCE).
const SAMSUNG_COOKIE_DOMAIN = "account.samsung.com";

document.getElementById("reset-auth").addEventListener("click", () => {
  const statusEl = document.getElementById("reset-status");
  statusEl.textContent = "Clearing…";

  chrome.cookies.getAll({ domain: SAMSUNG_COOKIE_DOMAIN }, (cookies) => {
    if (!cookies.length) {
      statusEl.textContent = "No Samsung cookies found.";
      return;
    }

    let removed = 0;
    cookies.forEach((cookie) => {
      const url =
        "http" +
        (cookie.secure ? "s" : "") +
        "://" +
        cookie.domain.replace(/^\./, "") +
        cookie.path;
      chrome.cookies.remove({ url, name: cookie.name }, () => {
        removed++;
        if (removed === cookies.length) {
          statusEl.textContent = `Cleared ${removed} cookie${removed !== 1 ? "s" : ""}. Ready for a fresh Samsung login.`;
        }
      });
    });
  });
});
