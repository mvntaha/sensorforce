/* The Snapshots page: take a photograph, view it full size, download it, and
 * delete the ones not wanted.
 */

import { api, toast } from "./app.js";
import { localTime, progressText, takeSnapshot } from "./capture.js";

const root = document.getElementById("fg-snapshots");
const captureButton = document.getElementById("fg-capture");
const status = document.getElementById("fg-capture-status");
const statusText = document.getElementById("fg-capture-text");
const gallery = document.getElementById("fg-gallery");
const empty = document.getElementById("fg-gallery-empty");
const count = document.getElementById("fg-snapshot-count");
const itemTemplate = document.getElementById("fg-gallery-item");

const dialog = document.getElementById("fg-photo-dialog");
const dialogTitle = document.getElementById("fg-photo-title");
const dialogImage = document.getElementById("fg-photo-img");
const dialogDownload = document.getElementById("fg-photo-download");
const dialogDelete = document.getElementById("fg-photo-delete");

const timeoutS = Number(root?.dataset.timeout || 90);
let shown = null; // the figure currently open in the dialog

function refreshCount() {
  const total = gallery.querySelectorAll("[data-snapshot]").length;
  empty.hidden = total > 0;
  count.textContent = `${total} photo${total === 1 ? "" : "s"}, newest first.`;
}

function labelTime(figure) {
  const time = figure.querySelector("time");
  time.dateTime = figure.dataset.taken;
  time.textContent = localTime(figure.dataset.taken);
  figure.querySelector("img").alt = `The inside of the fridge, ${time.textContent}`;
}

/** Build a gallery entry for a snapshot the server has just returned. */
function addToGallery(snapshot) {
  const figure = itemTemplate.content.firstElementChild.cloneNode(true);
  figure.dataset.snapshot = snapshot.id;
  figure.dataset.url = snapshot.url;
  figure.dataset.download = snapshot.download_url;
  figure.dataset.taken = snapshot.taken_at;
  figure.querySelector("img").src = snapshot.url;
  const link = figure.querySelector("[data-download-link]");
  link.href = snapshot.download_url;
  link.download = snapshot.download_name;
  labelTime(figure);
  gallery.prepend(figure);
  refreshCount();
  return figure;
}

function setBusy(busy, text = "") {
  captureButton.disabled = busy;
  status.hidden = !busy;
  if (text) statusText.textContent = text;
}

async function capture(requestId = null) {
  setBusy(true, "Asking the fridge for a photo…");
  try {
    const snapshot = await takeSnapshot({
      requestId,
      timeoutS,
      onProgress: (request) => setBusy(true, progressText(request)),
    });
    // The same photograph may already be listed if the page was reloaded.
    const existing = gallery.querySelector(`[data-snapshot="${snapshot.id}"]`);
    openViewer(existing ?? addToGallery(snapshot));
    toast("Snapshot saved.", "info");
  } catch (error) {
    toast(error.message, "critical");
  } finally {
    setBusy(false);
  }
}

function openViewer(figure) {
  shown = figure;
  const when = localTime(figure.dataset.taken);
  dialogTitle.textContent = `Snapshot · ${when}`;
  dialogImage.src = figure.dataset.url;
  dialogImage.alt = `The inside of the fridge, ${when}`;
  dialogDownload.href = figure.dataset.download;
  dialogDownload.download = figure.querySelector("[download]")?.getAttribute("download") || "";
  dialog.showModal();
}

async function remove(figure) {
  const when = localTime(figure.dataset.taken);
  if (!window.confirm(`Delete the snapshot from ${when}? This cannot be undone.`)) return false;
  try {
    await api(`/api/snapshots/${figure.dataset.snapshot}`, { method: "DELETE" });
  } catch (error) {
    toast(error.message, "critical");
    return false;
  }
  figure.remove();
  refreshCount();
  toast("Snapshot deleted.", "info");
  return true;
}

// ------------------------------------------------------------------ wiring
gallery.querySelectorAll("[data-snapshot]").forEach(labelTime);

captureButton.addEventListener("click", () => void capture());

gallery.addEventListener("click", (event) => {
  const figure = event.target.closest("[data-snapshot]");
  if (!figure) return;
  if (event.target.closest("[data-view]")) openViewer(figure);
  else if (event.target.closest("[data-delete]")) void remove(figure);
});

dialogDelete.addEventListener("click", async () => {
  if (shown && (await remove(shown))) dialog.close();
});

dialog.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => dialog.close());
});

// Clicking the dimmed backdrop closes the viewer, as Escape already does.
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});

// A request still open when the page loaded (another tab, or a reload) is
// picked up again rather than left spinning.
if (root?.dataset.openRequest) {
  void capture(Number(root.dataset.openRequest));
}
