/* Shared browser helpers.
 *
 * Vanilla ES module, no framework and no CDN: the dashboard has to work on a
 * LAN with no internet. Event handlers are attached here, never inline in a
 * template.
 */

/** Wrapper around the JSON API. Unwraps { ok, data, error } responses. */
export async function api(path, { method = "GET", body = null, headers = {} } = {}) {
  const options = {
    method,
    headers: { Accept: "application/json", ...headers },
    credentials: "same-origin",
  };

  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD") {
    const token = csrfToken();
    if (token) {
      options.headers["X-CSRF-Token"] = token;
    }
  }

  const response = await fetch(path, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`The server returned a non-JSON response (${response.status}).`);
  }

  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `Request failed with status ${response.status}.`);
  }
  return payload.data;
}

/** The session's CSRF token, taken from any form on the page. */
export function csrfToken() {
  return document.querySelector('input[name="csrf_token"]')?.value ?? "";
}

/**
 * Call `task` every `intervalMs`, pausing while the tab is hidden so a
 * backgrounded dashboard does not keep polling the Pi.
 * Returns a function that stops the polling.
 */
export function poll(task, intervalMs = 5000) {
  let timer = null;

  const run = async () => {
    try {
      await task();
    } catch (error) {
      console.error("Polling failed:", error);
    }
  };

  const start = () => {
    if (timer === null) {
      timer = window.setInterval(run, intervalMs);
    }
  };

  const stop = () => {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  };

  const onVisibilityChange = () => (document.hidden ? stop() : start());

  document.addEventListener("visibilitychange", onVisibilityChange);
  start();
  void run();

  return () => {
    document.removeEventListener("visibilitychange", onVisibilityChange);
    stop();
  };
}

/** Read a design token, so scripts never hard-code a colour. */
export function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Map a freshness status to its badge modifier. */
export function statusModifier(status) {
  return (
    {
      "Fresh/Normal": "fresh",
      "Use Soon": "soon",
      "Check Food": "check",
      "Sensor Fault/Data Unavailable": "fault",
    }[status] ?? "fault"
  );
}

/** Announce a short message to sighted and screen-reader users alike. */
/* Icons match components.html so a toast and an alert never disagree about
 * what a severity looks like. Drawn here rather than fetched: the page has to
 * work with no network. */
const TOAST_ICONS = {
  info: '<path d="M12 16v-4"/><path d="M12 8h.01"/><circle cx="12" cy="12" r="10"/>',
  warning: '<path d="m21.7 16.4-.9-.3"/><path d="m15.2 13.9-.9-.3"/><path d="m16.6 18.7.3-.9"/>' +
    '<path d="m19.1 12.2.3-.9"/><path d="m19.6 18.7-.4-1"/><path d="m16.8 12.3-.4-1"/>' +
    '<path d="m14.3 16.6 1-.4"/><path d="m20.7 13.8 1-.4"/>' +
    '<path d="M9.3 3.6c-.4.5-.6 1.2-.5 1.8a3 3 0 0 1-2 3.5 3 3 0 0 0-1.8 4.3 3 3 0 0 1-1 3.9 3 3 0 0 0 .3 4.6"/>' +
    '<path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/>',
  critical: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>' +
    '<path d="M12 9v4"/><path d="M12 17h.01"/>',
};

export function toast(message, severity = "info") {
  const region = document.getElementById("fg-toast-region");
  if (!region) {
    return;
  }

  const item = document.createElement("div");
  item.className = `fg-toast fg-toast--${severity}`;

  const mark = document.createElement("span");
  mark.className = "fg-toast__mark";
  // Static icon geometry, never anything a user supplied.
  mark.innerHTML = `<svg class="fg-icon" viewBox="0 0 24 24" aria-hidden="true">${
    TOAST_ICONS[severity] ?? TOAST_ICONS.info
  }</svg>`;

  const body = document.createElement("span");
  body.className = "fg-toast__body";
  // textContent, so a message carrying a stray angle bracket stays text.
  body.textContent = message;

  item.append(mark, body);
  region.append(item);
  window.setTimeout(() => item.remove(), 6000);
}


/** Replace an element's children with plain text, never with markup. */
export function setText(element, text) {
  if (element) {
    element.textContent = text;
  }
}

function wireThemeToggle() {
  const button = document.getElementById("fg-theme-toggle");
  if (!button || !window.freshguardTheme) {
    return;
  }
  const sync = () =>
    button.setAttribute("aria-pressed", String(window.freshguardTheme.current() === "dark"));
  sync();
  button.addEventListener("click", () => {
    window.freshguardTheme.toggle();
    sync();
  });
}

/* The sidebar is off-canvas below 60rem; the header button slides it in. */
function wireSidebar() {
  const button = document.getElementById("fg-menu");
  const sidebar = document.getElementById("fg-sidebar");
  if (!button || !sidebar) {
    return;
  }

  const setOpen = (open) => {
    sidebar.dataset.open = String(open);
    button.setAttribute("aria-expanded", String(open));
  };

  button.addEventListener("click", () => setOpen(sidebar.dataset.open !== "true"));

  // Escape closes it, which is what a keyboard user expects of an overlay.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sidebar.dataset.open === "true") {
      setOpen(false);
      button.focus();
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  wireThemeToggle();
  wireSidebar();
});

/**
 * The fridge camera.
 *
 * The frame lives at a fixed URL and is replaced in place, so the browser is
 * given a changing query string to stop it serving the previous picture from
 * cache. A 204 means the device has sent nothing yet, which is an ordinary
 * state rather than an error, so the picture stays hidden and the empty line
 * stays visible.
 */
export function startCamera(intervalMs = 10000) {
  const card = document.querySelector("[data-camera]");
  if (card === null) return null;

  const image = document.getElementById("fg-camera-img");
  const empty = document.getElementById("fg-camera-empty");
  const time = document.getElementById("fg-camera-time");
  if (image === null) return null;

  const base = image.getAttribute("src");
  let objectUrl = null;

  const show = (blob) => {
    // Replace the object URL rather than appending to it: a page left open
    // for an hour would otherwise hold on to every frame it had ever shown.
    const next = URL.createObjectURL(blob);
    image.src = next;
    if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    objectUrl = next;

    image.hidden = false;
    if (empty !== null) empty.hidden = true;
    if (time !== null) {
      time.hidden = false;
      time.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }
  };

  const hide = () => {
    image.hidden = true;
    if (empty !== null) empty.hidden = false;
    if (time !== null) time.hidden = true;
  };

  const refresh = async () => {
    try {
      const response = await fetch(`${base}?t=${Date.now()}`, { cache: "no-store" });
      if (response.status === 204 || !response.ok) {
        hide();
        return;
      }
      show(await response.blob());
    } catch (error) {
      // A failed refresh keeps the previous picture rather than blanking the
      // card: the last known frame is more useful than nothing.
      console.error("Camera refresh failed:", error);
    }
  };

  refresh();
  const timer = window.setInterval(refresh, intervalMs);
  return () => window.clearInterval(timer);
}
