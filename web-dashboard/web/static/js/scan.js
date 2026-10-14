/* QR scanning.
 *
 * The camera is a convenience. The manual identifier box on this page is the
 * guaranteed path and always works, because a browser will refuse camera
 * access over plain HTTP and the Pi may be served without a certificate.
 */

import { toast } from "./app.js";

const ID_PATTERN = /FG-\d{4}/i;

const startButton = document.getElementById("fg-scan-start");
const stopButton = document.getElementById("fg-scan-stop");
const container = document.getElementById("fg-scanner");
const errorLine = document.getElementById("fg-scan-error");
const hint = document.getElementById("fg-scan-hint");

let scanner = null;

function showError(message) {
  if (!errorLine) {
    return;
  }
  errorLine.textContent = message;
  errorLine.hidden = !message;
}

if (window.isSecureContext && hint) {
  hint.textContent = "Point the camera at the sticker on the item.";
}

function identifierFrom(text) {
  const match = ID_PATTERN.exec(text ?? "");
  return match ? match[0].toUpperCase() : null;
}

async function stop() {
  if (scanner) {
    try {
      await scanner.stop();
    } catch {
      // Already stopped; nothing to undo.
    }
    scanner = null;
  }
  if (container) {
    container.hidden = true;
  }
  if (stopButton) {
    stopButton.hidden = true;
  }
  if (startButton) {
    startButton.hidden = false;
  }
}

startButton?.addEventListener("click", async () => {
  showError("");

  if (!window.isSecureContext) {
    showError("The camera needs HTTPS. Type the identifier above instead.");
    return;
  }
  if (!window.Html5Qrcode) {
    showError("The scanner did not load. Type the identifier above instead.");
    return;
  }

  container.hidden = false;
  startButton.hidden = true;
  stopButton.hidden = false;

  scanner = new window.Html5Qrcode("fg-scanner");
  try {
    await scanner.start(
      { facingMode: "environment" },
      { fps: 10, qrbox: 220 },
      async (decoded) => {
        const identifier = identifierFrom(decoded);
        if (!identifier) {
          toast("That code is not a FreshGuard sticker.", "warning");
          return;
        }
        await stop();
        window.location.assign(`/item/${identifier}`);
      },
      () => {
        // Called for every frame without a code; not an error worth showing.
      },
    );
  } catch (error) {
    await stop();
    showError(`${error}. Type the identifier above instead.`);
  }
});

stopButton?.addEventListener("click", () => void stop());
