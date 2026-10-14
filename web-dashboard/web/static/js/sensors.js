/* Scenario control for the simulated sensors.
 *
 * The button writes a scenario name through the API; monitor.py reads it on
 * its next tick and starts producing those readings. Nothing on this page
 * fakes a reading itself, so what appears afterwards came through the real
 * engine, the real alert path and the real database.
 */

import { api, setText, toast } from "./app.js";

const LABELS = {
  normal: "Normal",
  high_temp: "High temperature",
  high_humidity: "High humidity",
  gas_spike: "Abnormal gas",
  door_open: "Door left open",
  sensor_disconnected: "Disconnected sensor",
  dht_intermittent: "Intermittent sensor",
};

const group = document.getElementById("fg-scenarios");

async function choose(button) {
  const { scenario } = button.dataset;
  const buttons = group.querySelectorAll("[data-scenario]");
  buttons.forEach((element) => {
    element.disabled = true;
  });

  try {
    await api("/api/simulator", { method: "POST", body: { scenario } });
    buttons.forEach((element) => {
      element.setAttribute("aria-pressed", String(element === button));
    });
    setText(document.getElementById("fg-scenario-current"), LABELS[scenario] ?? scenario);
    toast(`Simulating ${LABELS[scenario] ?? scenario}. Give the monitor a few readings.`);
  } catch (error) {
    toast(error.message, "critical");
  } finally {
    buttons.forEach((element) => {
      element.disabled = false;
    });
  }
}

group?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-scenario]");
  if (button) {
    void choose(button);
  }
});
