const bounds = [[43.0, 20.2], [48.3, 32.5]];
const map = L.map('map', { zoomControl: false, minZoom: 5, maxZoom: 12 }).fitBounds(bounds);
L.control.zoom({ position: 'bottomright' }).addTo(map);
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19, attribution: 'Tiles © Esri' }).addTo(map);
L.rectangle(bounds, { color: '#f5efe0', weight: 1.5, fill: false, dashArray: '6 5', interactive: false }).addTo(map);

const ui = {
  date: document.querySelector('#dateSelect'), lens: document.querySelector('#lensSelect'),
  title: document.querySelector('#lensTitle'),
  legend: document.querySelector('#legend'), badge: document.querySelector('#dateBadge'),
  aircraft: document.querySelector('#aircraftMetric'), empty: document.querySelector('#emptyState'), status: null
};
let gridLayer = L.layerGroup().addTo(map);
let cells = [];
let gridSizeDegrees = 0.1;
const lenses = {
  density: { title: 'Flight density', description: 'Distinct aircraft observed in each grid cell.', field: 'distinct_aircraft', suffix: ' aircraft', colors: ['#d8eee0', '#8cc5a7', '#2c8e75', '#145b5b'], labels: ['0', '1–5', '6–20', '21+'], text: 'Density is shown by distinct aircraft, preventing repeated reports from inflating the count.' },
  nic: { title: 'Low NIC percentile', description: 'Aircraft-level share of cells with persistent low-NIC behavior.', field: 'low_nic_aircraft_ratio', suffix: '% low NIC', colors: ['#2c8e75', '#8cc5a7', '#e7b94f', '#d95f4e'], labels: ['0–2%', '2–10%', '10–25%', '25%+'], text: 'Only aircraft with valid ADS-B NIC are included. MLAT-only observations remain context, not NIC evidence.' },
  coverage: { title: 'ADS-B / MLAT coverage', description: 'Observation mix showing where each position source contributes.', field: 'adsb_observations', suffix: ' ADS-B obs', colors: ['#d8eee0', '#8cc5a7', '#2c8e75', '#145b5b'], labels: ['0', '1–100', '101–500', '500+'], text: 'Use this lens to separate aircraft-reported integrity from MLAT-derived position coverage.' }
};
function formatNumber(value) { return Number(value || 0).toLocaleString(); }
function valueFor(cell, lens) { if (lens === 'nic') return Number(cell.low_nic_aircraft_ratio || 0) * 100; return Number(cell[lenses[lens].field] || 0); }
function colorFor(value, lens) { if (lens === 'density') return value >= 21 ? 3 : value >= 6 ? 2 : value >= 1 ? 1 : 0; if (lens === 'coverage') return value >= 500 ? 3 : value >= 101 ? 2 : value >= 1 ? 1 : 0; return value >= 25 ? 3 : value >= 10 ? 2 : value >= 2 ? 1 : 0; }
function drawGrid() {
  const lens = ui.lens.value; const spec = lenses[lens]; gridLayer.clearLayers();
  ui.title.textContent = spec.title; ui.legend.innerHTML = spec.labels.map((label, i) => `<div class="legend-row"><span class="swatch" style="background:${spec.colors[i]}"></span>${label}</div>`).join('');
  cells.forEach(cell => { const value = valueFor(cell, lens); const index = colorFor(value, lens); const rectangle = L.rectangle([[cell.cell_lat_min, cell.cell_lon_min], [cell.cell_lat_min + gridSizeDegrees, cell.cell_lon_min + gridSizeDegrees]], { color: spec.colors[index], weight: 1, fillColor: spec.colors[index], fillOpacity: cell.sufficient_sample ? .72 : .28 }); rectangle.bindPopup(`<strong>${cell.archive_date}</strong><br>Grid ${cell.grid_row}, ${cell.grid_col}<br>${cell.distinct_aircraft} aircraft<br>${formatNumber(cell.observation_count)} observations<br>NIC: ${cell.aircraft_with_nic ? `${(cell.low_nic_aircraft_ratio * 100).toFixed(1)}% persistent low` : 'no valid data'}<br>Sample: ${cell.sufficient_sample ? 'sufficient' : 'limited'}`); rectangle.addTo(gridLayer); });
  const totalAircraft = cells.reduce((sum, cell) => sum + Number(cell.distinct_aircraft || 0), 0); ui.aircraft.textContent = formatNumber(totalAircraft);
  ui.empty.hidden = cells.length > 0;
}
async function loadStatus() { await fetch('/api/status'); }
async function loadDates() { const response = await fetch('/api/dates'); const data = await response.json(); ui.date.innerHTML = data.dates.length ? data.dates.map(date => `<option value="${date}">${date}</option>`).join('') : '<option value="">No imported days</option>'; if (data.dates.length) { ui.date.value = data.dates[0]; await loadGrid(); } else drawGrid(); }
async function loadGrid() { const date = ui.date.value; ui.badge.textContent = date || 'No day selected'; const response = await fetch(`/api/grid?date=${encodeURIComponent(date)}`); const data = await response.json(); gridSizeDegrees = Number(data.grid_size_degrees || gridSizeDegrees); cells = data.cells; drawGrid(); }
ui.lens.addEventListener('change', drawGrid); ui.date.addEventListener('change', loadGrid); Promise.all([loadStatus(), loadDates()]).catch(error => { console.error(error); });
