/* Asking the fridge for a photograph, shared by the Snapshots page and the
 * dashboard's camera card.
 *
 * The browser never talks to the Pi or the cloud. It records a request with
 * the server and polls it; the ingest worker does the rest.
 */

import { api } from "./app.js";

const POLL_MS = 1500;

/** What to tell the person while a request is in flight. */
export function progressText(request) {
  if (request.status === "pending") return "Passing your request to the fridge…";
  if (request.status === "sent") return "The fridge is taking a photo…";
  return "";
}

/**
 * Request a photograph, or resume waiting on `requestId`, and resolve with the
 * stored snapshot once it arrives. Rejects with a readable message on failure.
 */
export async function takeSnapshot({ requestId = null, timeoutS = 90, onProgress = () => {} } = {}) {
  let request = requestId
    ? await api(`/api/snapshots/requests/${requestId}`)
    : await api("/api/snapshots/capture", { method: "POST" });

  // A little longer than the server's own limit, so the server's message,
  // which says why, normally wins over this one.
  const deadline = Date.now() + (timeoutS + 20) * 1000;

  for (;;) {
    onProgress(request);
    if (request.status === "done") {
      if (!request.snapshot) throw new Error("The photo arrived but has since been deleted.");
      return request.snapshot;
    }
    if (request.status === "failed") {
      throw new Error(request.error || "The fridge could not take a photo.");
    }
    if (Date.now() > deadline) {
      throw new Error("No answer yet. Check that the ingest worker is running on the server.");
    }
    await new Promise((resolve) => window.setTimeout(resolve, POLL_MS));
    request = await api(`/api/snapshots/requests/${request.id}`);
  }
}

/** A UTC timestamp as the person's own local date and time. */
export function localTime(iso) {
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return iso;
  return moment.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
