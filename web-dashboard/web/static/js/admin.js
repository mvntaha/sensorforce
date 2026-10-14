/* Admin page: saving thresholds, the door timeout and account management. */

import { api, toast } from "./app.js";

function rowValues(row) {
  const values = {};
  row.querySelectorAll("input").forEach((input) => {
    values[input.name] = input.value;
  });
  return values;
}

document.querySelectorAll("[data-save-threshold]").forEach((button) => {
  button.addEventListener("click", async () => {
    const category = button.dataset.saveThreshold;
    const row = button.closest("tr");
    button.disabled = true;
    try {
      await api(`/api/thresholds/${encodeURIComponent(category)}`, {
        method: "PUT",
        body: rowValues(row),
      });
      toast(`Saved limits for ${category}.`, "info");
    } catch (error) {
      toast(error.message, "critical");
    } finally {
      button.disabled = false;
    }
  });
});

document.getElementById("fg-settings-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/settings", {
      method: "PUT",
      body: { door_open_timeout_s: form.elements.door_open_timeout_s.value },
    });
    toast("Door timeout saved.", "info");
  } catch (error) {
    toast(error.message, "critical");
  }
});

document.getElementById("fg-user-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/users", {
      method: "POST",
      body: {
        username: form.elements.username.value,
        password: form.elements.password.value,
        role: form.elements.role.value,
      },
    });
    window.location.reload();
  } catch (error) {
    toast(error.message, "critical");
  }
});

document.querySelectorAll("[data-delete-user]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await api(`/api/users/${button.dataset.deleteUser}`, { method: "DELETE" });
      window.location.reload();
    } catch (error) {
      toast(error.message, "critical");
    }
  });
});
