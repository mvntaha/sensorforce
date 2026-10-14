/* Inventory filters. Filtering happens in the page: the rows are already here,
 * so a filter must not cost a round trip to the Pi. */

import { setText } from "./app.js";

const filters = { status: "", zone: "", category: "", query: "" };

function apply() {
  const rows = document.querySelectorAll("#fg-inventory-body tr");
  let shown = 0;

  rows.forEach((row) => {
    const matchesStatus = !filters.status || row.dataset.status === filters.status;
    const matchesZone = !filters.zone || row.dataset.zone === filters.zone;
    const matchesCategory = !filters.category || row.dataset.category === filters.category;
    // The searchable text is built server-side, so a search matches the name,
    // the category and the identifier without re-reading the cells.
    const matchesQuery = !filters.query || (row.dataset.search ?? "").includes(filters.query);
    const visible = matchesStatus && matchesZone && matchesCategory && matchesQuery;
    row.hidden = !visible;
    if (visible) {
      shown += 1;
    }
  });

  const empty = document.getElementById("fg-inventory-empty");
  if (empty) {
    empty.hidden = shown > 0;
    setText(empty, rows.length === 0 ? "Nothing stored yet." : "Nothing matches these filters.");
  }
}

function wire(attribute, key) {
  const buttons = document.querySelectorAll(`[data-filter-${attribute}]`);
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      filters[key] = button.dataset[`filter${attribute[0].toUpperCase()}${attribute.slice(1)}`];
      buttons.forEach((other) => other.setAttribute("aria-pressed", String(other === button)));
      apply();
    });
  });
}

wire("status", "status");
wire("zone", "zone");

document.getElementById("fg-inventory-category")?.addEventListener("change", (event) => {
  filters.category = event.target.value;
  apply();
});

document.getElementById("fg-inventory-search")?.addEventListener("input", (event) => {
  filters.query = event.target.value.trim().toLowerCase();
  apply();
});

apply();
