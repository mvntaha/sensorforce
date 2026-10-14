/* The alerts page: acknowledging one alert, acknowledging the lot, and hiding
 * the ones already seen.
 *
 * Acknowledging silences an alert here. It does not change the condition that
 * raised it, and the wording on the page says so.
 */

import { api, toast } from "./app.js";

/** Replace an alert's action cell once it has been acknowledged. */
function markAcknowledged(button) {
  const row = button.closest("tr");
  if (row) {
    row.dataset.acknowledged = "1";
  }
  const cell = button.parentElement;
  if (cell) {
    cell.textContent = "Acknowledged";
  }
  applyVisibility();
}

async function acknowledge(button) {
  button.disabled = true;
  try {
    await api(`/api/alerts/${button.dataset.ack}/ack`, { method: "POST" });
    markAcknowledged(button);
    toast("Alert acknowledged.", "info");
  } catch (error) {
    button.disabled = false;
    toast(error.message, "critical");
  }
}

async function acknowledgeAll(trigger) {
  const buttons = [...document.querySelectorAll("[data-ack]")];
  if (buttons.length === 0) {
    return;
  }
  trigger.disabled = true;

  // One request per alert, in sequence: each acknowledgement is its own
  // decision on the server, and a partial failure must leave the rest intact.
  let done = 0;
  for (const button of buttons) {
    try {
      await api(`/api/alerts/${button.dataset.ack}/ack`, { method: "POST" });
      markAcknowledged(button);
      done += 1;
    } catch (error) {
      toast(error.message, "critical");
      break;
    }
  }

  if (done > 0) {
    toast(`${done} alert${done === 1 ? "" : "s"} acknowledged.`, "info");
  }
  trigger.disabled = document.querySelectorAll("[data-ack]").length === 0;
}

/** Hide or show the rows that have already been dealt with. */
function applyVisibility() {
  const show = document.getElementById("fg-show-acknowledged")?.checked ?? true;
  document.querySelectorAll("#fg-alert-body tr[data-acknowledged]").forEach((row) => {
    row.hidden = !show && row.dataset.acknowledged === "1";
  });
}

document.getElementById("fg-alert-body")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-ack]");
  if (button) {
    void acknowledge(button);
  }
});

document.getElementById("fg-ack-all")?.addEventListener("click", (event) => {
  void acknowledgeAll(event.currentTarget);
});

document.getElementById("fg-show-acknowledged")?.addEventListener("change", applyVisibility);
