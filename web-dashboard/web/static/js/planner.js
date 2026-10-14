/* The consumption planner.
 *
 * Marking an item consumed or thrown away is a write, so it goes through the
 * JSON API with the session's CSRF token. The row is removed from the plan on
 * success rather than the page being reloaded, so the cook keeps their place
 * in a long list.
 */

import { api, toast } from "./app.js";

const WORDING = {
  consumed: "marked as consumed",
  discarded: "recorded as thrown away",
};

/* Recording an outcome removes the item, so the label it was on comes back
 * into circulation. Saying so is the difference between a person reusing the
 * sticker and printing a new one. */
const FREED = " Its label is free to reuse.";

async function change(button) {
  const { qr, state } = button.dataset;
  const row = button.closest(".fg-plan__item");
  const buttons = row ? row.querySelectorAll("button") : [button];

  buttons.forEach((element) => {
    element.disabled = true;
  });

  try {
    await api(`/api/items/${encodeURIComponent(qr)}/consume`, {
      method: "POST",
      body: { outcome: state },
    });
    const name = row?.querySelector(".fg-plan__name")?.textContent.trim() ?? qr;
    row?.remove();
    toast(`${name} ${WORDING[state] ?? "updated"}.${FREED}`);
    showEmptyStateIfNeeded();
  } catch (error) {
    buttons.forEach((element) => {
      element.disabled = false;
    });
    toast(error.message, "critical");
  }
}

function showEmptyStateIfNeeded() {
  const list = document.getElementById("fg-plan");
  if (list && list.children.length === 0) {
    const empty = document.createElement("li");
    empty.className = "fg-empty";
    empty.textContent = "Nothing is in store. Add an item to start planning.";
    list.append(empty);
  }
}

document.getElementById("fg-plan")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-state]");
  if (button) {
    change(button);
  }
});
