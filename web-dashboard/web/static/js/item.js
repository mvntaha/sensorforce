/* The item page: create, update and a two-step delete. */

import { api, toast } from "./app.js";

const form = document.getElementById("fg-item-form");
const errorLine = document.getElementById("fg-item-error");

function showError(message) {
  if (!errorLine) {
    return;
  }
  errorLine.textContent = message;
  errorLine.hidden = !message;
}

function body() {
  const data = new FormData(form);
  const payload = {};
  for (const [key, value] of data.entries()) {
    if (key !== "csrf_token") {
      payload[key] = value;
    }
  }
  payload.qr_id = form.dataset.qrId;
  return payload;
}

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const creating = form.dataset.mode === "create";
  const target = creating ? "/api/items" : `/api/items/${form.dataset.qrId}`;

  try {
    await api(target, { method: creating ? "POST" : "PUT", body: body() });
    toast(creating ? "Item stored." : "Changes saved.", "info");
    window.location.assign("/inventory");
  } catch (error) {
    showError(error.message);
  }
});

// Deletion asks twice: the first press only reveals the confirmation.
const remove = document.getElementById("fg-delete");
const confirmRow = document.getElementById("fg-delete-confirm");

remove?.addEventListener("click", () => {
  remove.hidden = true;
  if (confirmRow) {
    confirmRow.hidden = false;
  }
});

document.getElementById("fg-delete-no")?.addEventListener("click", () => {
  if (confirmRow) {
    confirmRow.hidden = true;
  }
  if (remove) {
    remove.hidden = false;
  }
});

document.getElementById("fg-delete-yes")?.addEventListener("click", async () => {
  try {
    await api(`/api/items/${form.dataset.qrId}`, { method: "DELETE" });
    window.location.assign("/inventory");
  } catch (error) {
    showError(error.message);
  }
});

/* Recording what became of the item. The same write the planner makes, offered
 * here because this is where someone looking at one container already is. */
document.querySelectorAll("[data-state]").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/api/items/${button.dataset.qr}/consume`, {
        method: "POST",
        body: { outcome: button.dataset.state },
      });
      // The item no longer exists, so there is nothing on this page to return
      // to; the inventory is where the person can see the result.
      window.location.assign("/inventory");
    } catch (error) {
      button.disabled = false;
      showError(error.message);
    }
  });
});

/* Copying the identifier, for typing it into the lookup box on another device.
 * The clipboard is not available over plain HTTP in every browser, so a
 * failure says what to do instead rather than silently doing nothing. */
document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      toast(`${button.dataset.copy} copied.`);
    } catch {
      toast("The clipboard is unavailable. Select the identifier above and copy it.", "warning");
    }
  });
});
