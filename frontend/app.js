// Point this at your backend (local dev)
const API_BASE_URL = "http://127.0.0.1:8000";

document.getElementById("api-base").textContent = API_BASE_URL;

// tiny helper
const qs = (sel) => document.querySelector(sel);

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function loadLatest() {
  try {
    const latest = await fetchJSON(`${API_BASE_URL}/api/data/latest`);
    qs("#latest").textContent = JSON.stringify(latest, null, 2);
  } catch (e) {
    qs("#latest").textContent = `Failed to load latest: ${e.message}`;
  }
}

async function loadTable() {
  const tbody = qs("#data-body");
  tbody.innerHTML = "";
  try {
    const rows = await fetchJSON(`${API_BASE_URL}/api/data`);
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono">${r.timestamp}</td>
        <td>${r.temperature.toFixed(1)}</td>
        <td>${r.humidity.toFixed(1)}</td>
        <td>${r.voltage.toFixed(2)}</td>
      `;
      tbody.appendChild(tr);
    }
  } catch (e) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="4">Failed to load data: ${e.message}</td>`;
    tbody.appendChild(tr);
  }
}

loadLatest();
loadTable();
