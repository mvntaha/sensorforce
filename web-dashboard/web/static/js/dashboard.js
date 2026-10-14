/* Dashboard: live figures, the sensor-fault banner and the readings chart.
 *
 * Chart.js is vendored locally, so nothing is fetched from the internet. Every
 * colour comes from a design token rather than a literal, so the chart follows
 * light and dark mode.
 */

import { api, poll, setText, startCamera, toast, token } from "./app.js";
import { localTime, progressText, takeSnapshot } from "./capture.js";

const REFRESH_MS = 5000;
const FAULT_LABEL = "Sensor Fault/Data Unavailable";

let chart = null;

/* What the two selects above the chart are currently set to. Kept here rather
 * than read on every draw so the chart keeps its selection across a refresh. */
const view = { zone: "", metric: "both" };

/* Each series the chart can draw: where the value comes from, whether the
 * reading is trustworthy, and which axis it belongs on. Gas is a relative
 * figure from an uncalibrated sensor, so its axis is labelled "relative". */
const SERIES = {
  temp: {
    label: "Temperature (°C)",
    value: (row) => (row.temp_ok ? row.temp_c : null),
    colour: "--primary",
    axis: "y",
    unit: "°C",
  },
  humidity: {
    label: "Humidity (%)",
    value: (row) => (row.hum_ok ? row.humidity : null),
    colour: "--status-soon-foreground",
    axis: "y1",
    unit: "%",
  },
  gas: {
    label: "Gas (relative)",
    value: (row) => (row.gas_ok ? row.gas_raw : null),
    colour: "--status-check",
    axis: "y",
    unit: "relative",
  },
};

const VIEWS = {
  both: ["temp", "humidity"],
  temp: ["temp"],
  humidity: ["humidity"],
  gas: ["gas"],
};

function updateHealth(health) {
  const monitor = document.getElementById("fg-monitor-state");
  if (monitor) {
    const running = health.monitor_running;
    monitor.innerHTML = "";
    const dot = document.createElement("span");
    dot.className = `fg-dot fg-dot--${running ? "online" : "offline"}`;
    monitor.append(dot);
    monitor.append(
      document.createTextNode(
        running
          ? `Monitor running (${Math.round(health.last_reading_age_s)}s ago)`
          : "Monitor not reporting",
      ),
    );
  }

  const sync = document.getElementById("fg-sync-state");
  if (sync) {
    const pending = health.sync_pending;
    setText(sync, pending === 0 ? "Sync: up to date" : `Sync: ${pending} waiting`);
  }
}

function updateFaultBanner(zones) {
  const banner = document.getElementById("fg-fault-banner");
  if (!banner) {
    return;
  }
  const faulted = zones.filter((zone) => zone.has_required_fault);
  banner.hidden = faulted.length === 0;
  if (faulted.length > 0) {
    setText(
      document.getElementById("fg-fault-text"),
      `${FAULT_LABEL} — ${faulted.map((zone) => zone.name).join(", ")}`,
    );
  }
}

function buildChart(canvas, readings) {
  const chosen = VIEWS[view.metric] ?? VIEWS.both;
  const grid = token("--border");
  const text = token("--muted-foreground");

  const datasets = chosen.map((key) => {
    const series = SERIES[key];
    return {
      label: series.label,
      data: readings.map(series.value),
      borderColor: token(series.colour),
      backgroundColor: token(series.colour),
      spanGaps: false,
      tension: 0.3,
      yAxisID: chosen.length === 1 ? "y" : series.axis,
    };
  });

  const axis = (id, position, unit) => ({
    position,
    ticks: { color: text },
    grid: id === "y" ? { color: grid } : { drawOnChartArea: false },
    title: { display: true, text: unit, color: text },
  });

  const scales = {
    x: { ticks: { color: text, maxTicksLimit: 8 }, grid: { color: grid } },
    y: axis("y", "left", SERIES[chosen[0]].unit),
  };
  if (chosen.length > 1) {
    scales.y1 = axis("y1", "right", SERIES[chosen[1]].unit);
  }

  return new window.Chart(canvas, {
    type: "line",
    data: { labels: readings.map((row) => row.ts.slice(11, 19)), datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: text } } },
      scales,
    },
  });
}

async function refreshChart() {
  const canvas = document.getElementById("fg-readings-chart");
  if (!canvas || !window.Chart) {
    return;
  }
  const query = new URLSearchParams({ limit: "60" });
  if (view.zone) {
    query.set("zone", view.zone);
  }
  const readings = await api(`/api/readings?${query}`);
  const empty = document.getElementById("fg-chart-empty");

  if (readings.length === 0) {
    if (empty) {
      empty.hidden = false;
    }
    canvas.hidden = true;
    return;
  }
  if (empty) {
    empty.hidden = true;
  }
  canvas.hidden = false;

  if (chart) {
    chart.destroy();
  }
  chart = buildChart(canvas, readings);
}

async function refresh() {
  const [health, status] = await Promise.all([api("/api/health"), api("/api/status")]);
  updateHealth(health);
  updateFaultBanner(status.zones);
  await refreshChart();
}

poll(refresh, REFRESH_MS);

// The chart follows the two selects above it. Redrawing is local work: the
// readings are already fetched, so changing the metric costs no round trip.
document.getElementById("fg-chart-zone")?.addEventListener("change", (event) => {
  view.zone = event.target.value;
  void refreshChart();
});

document.getElementById("fg-chart-metric")?.addEventListener("change", (event) => {
  view.metric = event.target.value;
  void refreshChart();
});

// Rebuild the chart when the theme changes so its colours follow the tokens.
document.getElementById("fg-theme-toggle")?.addEventListener("click", () => {
  window.setTimeout(() => void refreshChart(), 50);
});

// The fridge camera refreshes on its own timer: the frame changes far more
// slowly than the readings, and the picture is much larger than the figures.
startCamera();

// "Take snapshot" on the camera card: ask for a fresh photograph and show it
// here as soon as it arrives. The full history is on the Snapshots page.
const captureButton = document.getElementById("fg-dashboard-capture");
captureButton?.addEventListener("click", async () => {
  const label = captureButton.lastChild;
  const original = label.textContent;
  captureButton.disabled = true;
  try {
    const snapshot = await takeSnapshot({
      onProgress: (request) => {
        label.textContent = progressText(request) || original;
      },
    });
    const image = document.getElementById("fg-camera-img");
    image.src = snapshot.url;
    image.hidden = false;
    document.getElementById("fg-camera-empty")?.setAttribute("hidden", "");
    const time = document.getElementById("fg-camera-time");
    if (time) {
      time.hidden = false;
      time.textContent = `Taken ${localTime(snapshot.taken_at)}`;
    }
    toast("Snapshot saved. Find it any time on the Snapshots page.", "info");
  } catch (error) {
    toast(error.message, "critical");
  } finally {
    label.textContent = original;
    captureButton.disabled = false;
  }
});
