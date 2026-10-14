/* RFID tags tapped in with no details yet: keeps the banner at the top of
 * every page and the bell's count current while the page is open, so a tag
 * tapped at the fridge shows up without a reload.
 */

import { api, poll } from "./app.js";

const banner = document.getElementById("fg-rfid-banner");
const rowTemplate = document.getElementById("fg-rfid-item");
const bell = document.getElementById("fg-bell");
const bellCount = document.getElementById("fg-bell-count");

let shown = [...(banner?.querySelectorAll("a[href]") ?? [])].map((link) => link.href).join("|");

function render(tags) {
  const key = tags.map((tag) => new URL(tag.add_url, window.location.href).href).join("|");
  if (key !== shown) {
    shown = key;
    banner.replaceChildren(
      ...tags.map((tag) => {
        const row = rowTemplate.content.firstElementChild.cloneNode(true);
        row.querySelector("[data-short]").textContent = tag.short;
        row.querySelector("[data-add]").href = tag.add_url;
        return row;
      }),
    );
    banner.hidden = tags.length === 0;
  }

  const total = Number(bell?.dataset.alerts || 0) + tags.length;
  if (bellCount) {
    bellCount.textContent = String(total);
    bellCount.hidden = total === 0;
  }
  bell?.setAttribute("aria-label", total ? `Alerts, ${total} needing attention` : "Alerts");
}

if (banner && rowTemplate) {
  poll(async () => render(await api("/api/rfid/pending")), 5000);
}
