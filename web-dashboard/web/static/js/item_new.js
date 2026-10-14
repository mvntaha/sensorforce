/* Registering a container, by tapping.
 *
 * This runs on the screen stuck to the fridge, so the design rule throughout
 * is that a complete item can be stored without a keyboard ever appearing.
 * Every answer has a tap path; typing is offered behind a key for the cases a
 * fixed list cannot cover, which is most real kitchens eventually.
 *
 * Each step validates before it lets you past, so a mistake is caught on the
 * screen that made it rather than at the end. Nothing is written until the
 * check step is confirmed, and the server validates all of it again: this
 * wizard is a convenience, never the guard.
 */

import { api, toast } from "./app.js";

const form = document.getElementById("fg-wizard-form");
const steps = [...document.querySelectorAll("[data-step]")];
const markers = [...document.querySelectorAll("[data-step-marker]")];
const backButton = document.getElementById("fg-wizard-back");
const nextButton = document.getElementById("fg-wizard-next");
const saveButton = document.getElementById("fg-wizard-save");
const errorLine = document.getElementById("fg-wizard-error");
const review = document.getElementById("fg-review");

let current = 0;

/** Labels for the check step, in the order the steps asked for them. */
const REVIEW_FIELDS = [
  ["name", "Name"],
  ["category", "Kind"],
  ["zone_id", "Where"],
  ["qr_id", "Container"],
  ["rfid_tag", "RFID tag"],
  ["quantity", "How much"],
  ["unit", "Unit"],
  ["storage_date", "Put in"],
  ["expiry_date", "Use by"],
];

function showError(message) {
  if (!errorLine) {
    return;
  }
  errorLine.textContent = message ?? "";
  errorLine.hidden = !message;
}

function field(name) {
  return form.elements[name];
}

/** An ISO date `days` from today, which is how every date preset is built. */
function isoOffset(days) {
  const when = new Date();
  when.setDate(when.getDate() + days);
  return when.toISOString().slice(0, 10);
}

/* ------------------------------------------------------------- selection */
/* Every tap group works the same way: the button carries the value, pressing
 * it writes that value into the hidden input the form actually submits, and
 * aria-checked moves with it so the control is a real radio group to anything
 * that is not a pair of eyes. */

function selectWithin(group, button) {
  for (const option of group.querySelectorAll("[data-choice]")) {
    option.setAttribute("aria-checked", String(option === button));
  }
}

function onChoice(button) {
  const group = button.closest(".fg-choices");
  const { choice, value, offset } = button.dataset;
  if (group) {
    selectWithin(group, button);
  }

  if (offset !== undefined) {
    // A date preset: empty means "no date printed", which is a real answer
    // and not a missing one -- the category's storage limit takes over.
    field(choice).value = offset === "" ? "" : isoOffset(Number(offset));
    if (choice === "expiry_date") {
      echoExpiry();
    }
    showError("");
    return;
  }

  field(choice).value = value;
  if (choice === "category") {
    showNamesFor(value);
  }
  showError("");
}

/** Show the name grid belonging to the chosen category, hide the rest. */
function showNamesFor(category) {
  for (const grid of document.querySelectorAll("[data-names-for]")) {
    grid.hidden = grid.dataset.namesFor !== category;
  }
}

function echoExpiry() {
  const echo = document.getElementById("fg-expiry-echo");
  if (!echo) {
    return;
  }
  const value = field("expiry_date").value;
  echo.textContent = value
    ? `Use by ${value}.`
    : "No use-by date. The category's storage limit will be used.";
}

/* ------------------------------------------------------------- the pad */
/* Four digits is the only thing anyone edits here, so it gets a pad rather
 * than a keyboard. The FG- prefix is never editable: it is the format, not a
 * choice, and letting someone delete it would only produce an invalid code. */

function digits() {
  return (field("qr_id").value.match(/\d/g) ?? []).join("");
}

function setIdentifier(next) {
  const padded = next.padStart(4, "0").slice(-4);
  const value = `FG-${padded}`;
  field("qr_id").value = value;
  const display = document.getElementById("fg-qr-display");
  if (display) {
    display.textContent = value;
  }
}

function onKey(key) {
  const shown = digits();
  if (key === "clear") {
    setIdentifier("0000");
  } else if (key === "back") {
    setIdentifier(shown.slice(0, -1) || "0000");
  } else {
    // Sliding window: a fifth digit pushes the first one off, which is what
    // people expect from a fixed-width field and saves them clearing it.
    setIdentifier((shown === "0000" ? "" : shown) + key);
  }
}

/* ------------------------------------------------------------ the review */

/** The visible text of a choice, so the check step reads "Main shelf" not "1". */
function displayValue(name) {
  const chosen = document.querySelector(`[data-choice="${name}"][aria-checked="true"]`);
  return chosen?.dataset.label ?? field(name)?.value ?? "";
}

function buildReview() {
  review.replaceChildren();
  for (const [name, label] of REVIEW_FIELDS) {
    // The tag row exists only for an item that arrived by an RFID tap.
    if (!field(name)) continue;
    const value = name === "zone_id" ? displayValue(name) : (field(name)?.value ?? "");
    const line = document.createElement("div");
    line.className = "fg-fact";
    const key = document.createElement("span");
    key.className = "fg-fact__label";
    key.textContent = label;
    const shown = document.createElement("span");
    shown.className = "fg-fact__value";
    shown.textContent = value || "—";
    line.append(key, shown);
    review.append(line);
  }
}

/* -------------------------------------------------------------- stepping */

function stepIsValid() {
  if (current === 0 && !field("category").value) {
    showError("Choose what kind of food it is.");
    return false;
  }
  if (current === 1 && !field("name").value.trim()) {
    showError("Tap what it is, or use “Something else” to type a name.");
    return false;
  }
  if (current === 2 && !/^FG-\d{4}$/.test(field("qr_id").value)) {
    showError("A container label looks like FG-0001.");
    return false;
  }
  if (current === 3 && !(Number(field("quantity").value) > 0)) {
    showError("How much is it? The amount has to be more than zero.");
    return false;
  }
  showError("");
  return true;
}

function render() {
  steps.forEach((step, index) => {
    step.hidden = index !== current;
  });
  markers.forEach((marker, index) => {
    marker.classList.toggle("is-done", index < current);
    if (index === current) {
      marker.setAttribute("aria-current", "step");
    } else {
      marker.removeAttribute("aria-current");
    }
  });

  const last = current === steps.length - 1;
  backButton.hidden = current === 0;
  nextButton.hidden = last;
  saveButton.hidden = !last;
  if (last) {
    buildReview();
  }
}

function payload() {
  const body = {};
  for (const [name] of REVIEW_FIELDS) {
    const value = field(name)?.value?.trim() ?? "";
    if (value) {
      body[name] = value;
    }
  }
  return body;
}

/* ---------------------------------------------------------------- wiring */

for (const button of document.querySelectorAll("[data-choice]")) {
  button.addEventListener("click", () => onChoice(button));
}

for (const key of document.querySelectorAll("[data-key]")) {
  key.addEventListener("click", () => onKey(key.dataset.key));
}

document.getElementById("fg-edit-qr")?.addEventListener("click", (event) => {
  const pad = document.getElementById("fg-keypad");
  pad.hidden = !pad.hidden;
  event.currentTarget.setAttribute("aria-expanded", String(!pad.hidden));
});

document.getElementById("fg-type-name")?.addEventListener("click", (event) => {
  const box = document.getElementById("fg-name-field");
  box.hidden = false;
  event.currentTarget.hidden = true;
  field("name").focus();
});

document.getElementById("fg-type-dates")?.addEventListener("click", (event) => {
  const box = document.getElementById("fg-date-fields");
  box.hidden = false;
  event.currentTarget.hidden = true;
});

// The exact-date inputs write through to the same hidden fields the presets
// do, so there is one source of truth whichever way the date was given.
document.getElementById("field-storage_date_exact")?.addEventListener("change", (event) => {
  field("storage_date").value = event.target.value;
});

document.getElementById("field-expiry_date_exact")?.addEventListener("change", (event) => {
  field("expiry_date").value = event.target.value;
  echoExpiry();
});

for (const button of document.querySelectorAll("[data-step-quantity]")) {
  button.addEventListener("click", () => {
    const input = field("quantity");
    const next = Number(input.value || 0) + Number(button.dataset.stepQuantity);
    // Rounded because repeated fractional steps otherwise drift into a long
    // float, and nobody wants to store 2.9999999999999996 kg.
    input.value = String(Math.max(0, Math.round(next * 100) / 100));
    showError("");
  });
}

nextButton?.addEventListener("click", () => {
  if (!stepIsValid()) {
    return;
  }
  current = Math.min(current + 1, steps.length - 1);
  render();
});

backButton?.addEventListener("click", () => {
  current = Math.max(current - 1, 0);
  render();
});

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveButton.disabled = true;
  try {
    const item = await api("/api/items", { method: "POST", body: payload() });
    toast(`${item.name} stored as ${item.qr_id}.`);
    // Back to the scanner showing what was just stored. The full item page
    // needs a sign-in, and whoever filled this in may not have one.
    window.location.assign(`/?qr_id=${encodeURIComponent(item.qr_id)}`);
  } catch (error) {
    saveButton.disabled = false;
    showError(error.message);
  }
});

// Enter should advance a step, not submit the half-filled form behind it.
form?.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && current < steps.length - 1) {
    event.preventDefault();
    nextButton.click();
  }
});

render();
