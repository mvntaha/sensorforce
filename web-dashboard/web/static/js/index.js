/* The scanner page.
 *
 * Reading a label is a convenience; the box beside it is the path that always
 * works, because a browser refuses camera access unless the page is served
 * over HTTPS or from localhost.
 *
 * A decoded label navigates to "/?qr_id=..." rather than drawing the record
 * here. That costs one page load and buys the thing worth having: the record
 * a scan shows and the record a typed identifier shows are the same server
 * template, so they cannot drift apart, and the whole flow still works with
 * the camera refused and JavaScript off.
 *
 * Nothing here decides anything about the food. A scan opens a record, and the
 * verdict in it came from the engine.
 */

import { api, toast } from "./app.js";

const ID_PATTERN = /FG-\d{4}/i;
const SCANNER_ID = "fg-scanner-viewport";

const stage = document.getElementById("fg-scanner");
const startButton = document.getElementById("fg-scan-start");
const stopButton = document.getElementById("fg-scan-stop");
const hint = document.getElementById("fg-scanner-hint");
const errorLine = document.getElementById("fg-scan-error");
const dialog = document.getElementById("fg-item-dialog");

let scanner = null;
/* Set once a code has been accepted, so the several frames that decode the
 * same label do not each start a navigation. */
let leaving = false;

function setState(state) {
  stage?.setAttribute("data-state", state);
}

function showError(message) {
  if (!errorLine) {
    return;
  }
  errorLine.textContent = message ?? "";
  errorLine.hidden = !message;
}

/** Open the record for an identifier, by asking the server for it. */
function open(identifier) {
  leaving = true;
  setState("found");
  window.location.assign(`/?qr_id=${encodeURIComponent(identifier)}`);
}

function onDecode(text) {
  if (leaving) {
    return;
  }
  const match = ID_PATTERN.exec(text);
  if (!match) {
    showError("That code is not a FreshGuard label.");
    return;
  }
  showError("");
  open(match[0].toUpperCase());
}

async function start() {
  if (!window.isSecureContext) {
    showError(
      "The camera needs a secure connection. Open FreshGuard over HTTPS, or type the identifier below.",
    );
    return;
  }
  if (!window.Html5Qrcode) {
    showError("The scanner library did not load. Type the identifier below.");
    return;
  }

  try {
    scanner = new window.Html5Qrcode(SCANNER_ID);
    await scanner.start(
      { facingMode: "environment" },
      { fps: 10, qrbox: { width: 240, height: 240 } },
      onDecode,
      () => {
        // Called for every frame that holds no code, which is most of them.
      },
    );
    setState("scanning");
    showError("");
    if (hint) {
      hint.textContent = "Point the camera at the label.";
    }
    startButton.hidden = true;
    stopButton.hidden = false;
  } catch (error) {
    scanner = null;
    setState("idle");
    showError(`The camera could not be opened: ${error.message ?? error}`);
  }
}

async function stop() {
  if (scanner) {
    try {
      await scanner.stop();
      scanner.clear();
    } catch {
      // Already stopped, or the page is going away. Nothing to recover.
    }
    scanner = null;
  }
  setState("idle");
  startButton.hidden = false;
  stopButton.hidden = true;
}

/** Record what became of an item, which removes it and frees its label. */
async function record(button) {
  const { qr, outcome, name } = button.dataset;
  button.disabled = true;
  try {
    await api(`/api/items/${encodeURIComponent(qr)}/consume`, {
      method: "POST",
      body: { outcome },
    });
    dialog?.close();
    toast(
      outcome === "consumed"
        ? `${name} marked as eaten. ${qr} is free to reuse.`
        : `${name} recorded as thrown away.`,
    );
    // Back to a clean scanner: the item is gone, so the record behind this
    // dialog no longer describes anything.
    window.setTimeout(() => window.location.assign("/"), 900);
  } catch (error) {
    button.disabled = false;
    toast(error.message, "critical");
  }
}

/** Dismiss the record and take the identifier out of the address bar. */
function dismiss() {
  dialog?.close();
  window.history.replaceState(null, "", "/");
}

startButton?.addEventListener("click", () => void start());
stopButton?.addEventListener("click", () => void stop());

for (const button of document.querySelectorAll("[data-close-dialog]")) {
  button.addEventListener("click", dismiss);
}

for (const button of document.querySelectorAll("[data-outcome]")) {
  button.addEventListener("click", () => void record(button));
}

// Escape fires "close" without going through the buttons above.
dialog?.addEventListener("cancel", () => window.history.replaceState(null, "", "/"));

if (dialog?.dataset.open) {
  dialog.showModal();
  // showModal() focuses the first control inside, which drew a focus ring
  // around the close button the moment a record opened. The dialog takes
  // focus itself instead: a screen reader still announces the title, and
  // nothing looks pre-pressed.
  dialog.focus();
}

// Releasing the camera when the page goes away stops the indicator light
// staying on, which people reasonably read as still being watched.
window.addEventListener("pagehide", () => void stop());
