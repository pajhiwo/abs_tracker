/* ABS Tracker — Frontend Logic */

// ---------------------------------------------------------------------------
// DOM elements
// ---------------------------------------------------------------------------
const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");
const uploadStatus = document.getElementById("upload-status");
const controlsSection = document.getElementById("controls-section");
const summarySection = document.getElementById("summary-section");
const timelineSection = document.getElementById("timeline-section");
const liftSection = document.getElementById("lift-section");
const episodesSection = document.getElementById("episodes-section");
const hoursSlider = document.getElementById("hours-slider");
const hoursValue = document.getElementById("hours-value");
const minobsSlider = document.getElementById("minobs-slider");
const minobsValue = document.getElementById("minobs-value");
const recomputeBtn = document.getElementById("recompute-btn");
const periodSelect = document.getElementById("period-select");
const showLowConf = document.getElementById("show-low-conf");
const splitCompounds = document.getElementById("split-compounds");

let currentData = null;

// ---------------------------------------------------------------------------
// File upload — drag & drop + click
// ---------------------------------------------------------------------------
dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
});
fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) uploadFile(fileInput.files[0]);
});

async function uploadFile(file) {
    uploadStatus.textContent = `Uploading ${file.name}…`;
    uploadStatus.className = "status loading";

    const formData = new FormData();
    formData.append("file", file);

    const hours = hoursSlider.value;
    const minObs = minobsSlider.value;
    const split = splitCompounds.checked;

    try {
        const resp = await fetch(`/upload?hours=${hours}&min_obs=${minObs}&split_compounds=${split}`, {
            method: "POST",
            body: formData,
        });
        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || "Upload failed");
        }
        currentData = await resp.json();
        uploadStatus.textContent = `✓ Loaded ${file.name}`;
        uploadStatus.className = "status success";
        renderAll();
    } catch (e) {
        uploadStatus.textContent = `✗ ${e.message}`;
        uploadStatus.className = "status error";
    }
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------
hoursSlider.addEventListener("input", () => {
    hoursValue.textContent = hoursSlider.value;
});
minobsSlider.addEventListener("input", () => {
    minobsValue.textContent = minobsSlider.value;
});

recomputeBtn.addEventListener("click", async () => {
    if (!currentData) return;
    recomputeBtn.disabled = true;
    recomputeBtn.textContent = "Computing…";
    try {
        const resp = await fetch("/results", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                hours: parseFloat(hoursSlider.value),
                min_obs: parseInt(minobsSlider.value),
                split_compounds: splitCompounds.checked,
            }),
        });
        if (!resp.ok) throw new Error("Recompute failed");
        currentData = await resp.json();
        renderAll();
    } catch (e) {
        alert(e.message);
    } finally {
        recomputeBtn.disabled = false;
        recomputeBtn.textContent = "Recompute";
    }
});

periodSelect.addEventListener("change", renderLiftChart);
showLowConf.addEventListener("change", renderLiftChart);

// ---------------------------------------------------------------------------
// Render everything
// ---------------------------------------------------------------------------
function renderAll() {
    if (!currentData) return;

    controlsSection.classList.remove("hidden");
    summarySection.classList.remove("hidden");
    timelineSection.classList.remove("hidden");
    liftSection.classList.remove("hidden");
    episodesSection.classList.remove("hidden");

    hoursSlider.value = currentData.hours;
    hoursValue.textContent = currentData.hours;
    minobsSlider.value = currentData.min_obs;
    minobsValue.textContent = currentData.min_obs;
    splitCompounds.checked = currentData.split_compounds !== false;

    renderSummary();
    renderTimeline();
    populatePeriodSelect();
    renderLiftChart();
    renderEpisodeTable();
}

// ---------------------------------------------------------------------------
// Summary cards
// ---------------------------------------------------------------------------
function renderSummary() {
    const s = currentData.summary;
    document.getElementById("stat-readings").textContent = s.total_readings;
    document.getElementById("stat-ingredients").textContent = s.unique_ingredients;
    document.getElementById("stat-episodes").textContent = s.episodes;
    document.getElementById("stat-bac-mean").textContent = s.bac_mean + "‰";
    document.getElementById("stat-pairs").textContent = s.lookback_pairs;

    const d1 = s.date_min ? s.date_min.slice(0, 10) : "?";
    const d2 = s.date_max ? s.date_max.slice(0, 10) : "?";
    document.getElementById("stat-daterange").textContent = `${d1} — ${d2}`;
}

// ---------------------------------------------------------------------------
// BAC timeline (Plotly)
// ---------------------------------------------------------------------------
function renderTimeline() {
    const readings = currentData.bac_readings;
    const medPeriods = currentData.medication_periods;

    // Main BAC trace
    const normal = readings.filter(r => !r.episode);
    const episodes = readings.filter(r => r.episode);

    const traces = [
        {
            x: normal.map(r => r.bac_datetime),
            y: normal.map(r => r.promille),
            mode: "lines+markers",
            type: "scatter",
            name: "BAC Reading",
            marker: { color: "#6366f1", size: 6 },
            line: { color: "#6366f1", width: 1.5 },
            hovertemplate: "%{y:.2f}‰<br>%{x}<extra></extra>",
        },
        {
            x: episodes.map(r => r.bac_datetime),
            y: episodes.map(r => r.promille),
            mode: "markers",
            type: "scatter",
            name: "Episode",
            marker: { color: "#ef4444", size: 10, symbol: "diamond" },
            hovertemplate: "⚠ %{y:.2f}‰<br>%{x}<extra>Episode</extra>",
        },
    ];

    // Medication period shading
    const shapes = [];
    const colors = {
        "Activated Charcoal": "rgba(34,197,94,0.08)",
        "Rifaximin": "rgba(99,102,241,0.08)",
    };
    const borderColors = {
        "Activated Charcoal": "rgba(34,197,94,0.3)",
        "Rifaximin": "rgba(99,102,241,0.3)",
    };

    for (const [med, ranges] of Object.entries(medPeriods)) {
        for (const range of ranges) {
            const stop = range.stop || new Date().toISOString();
            shapes.push({
                type: "rect",
                xref: "x", yref: "paper",
                x0: range.start, x1: stop,
                y0: 0, y1: 1,
                fillcolor: colors[med] || "rgba(200,200,200,0.08)",
                line: { color: borderColors[med] || "rgba(200,200,200,0.2)", width: 1 },
            });
        }
    }

    // Medication legend annotations — stagger vertically to avoid overlap
    const annotations = [];
    const medNames = Object.keys(medPeriods);
    for (let mi = 0; mi < medNames.length; mi++) {
        const med = medNames[mi];
        const ranges = medPeriods[med];
        // Short label to save space
        const shortLabel = med.replace("Activated Charcoal", "A. Charcoal");
        for (const range of ranges) {
            const yOffset = 1.02 + mi * 0.05; // stagger each medication up
            annotations.push({
                x: range.start,
                y: yOffset,
                xref: "x", yref: "paper",
                text: `<b>${shortLabel}</b>`,
                showarrow: false,
                font: { size: 10, color: borderColors[med] || "#8b8fa3" },
                xanchor: "left",
                yanchor: "bottom",
            });
        }
    }

    const layout = {
        paper_bgcolor: "#1a1d27",
        plot_bgcolor: "#1a1d27",
        font: { color: "#e1e4eb" },
        margin: { l: 50, r: 20, t: 40, b: 50 },
        xaxis: {
            gridcolor: "#2a2d3a",
            title: "Date",
        },
        yaxis: {
            gridcolor: "#2a2d3a",
            title: "BAC (‰)",
            rangemode: "tozero",
        },
        legend: { x: 0, y: 1.12, orientation: "h" },
        shapes: shapes,
        annotations: annotations,
        hovermode: "closest",
    };

    Plotly.newPlot("bac-timeline-chart", traces, layout, { responsive: true });
}

// ---------------------------------------------------------------------------
// Lift score chart
// ---------------------------------------------------------------------------
function populatePeriodSelect() {
    periodSelect.innerHTML = '<option value="overall">Overall</option>';
    for (const period of Object.keys(currentData.lift_scores_by_period)) {
        const opt = document.createElement("option");
        opt.value = period;
        opt.textContent = period;
        periodSelect.appendChild(opt);
    }
}

function renderLiftChart() {
    const period = periodSelect.value;
    let scores;
    if (period === "overall") {
        scores = currentData.lift_scores_overall;
    } else {
        scores = currentData.lift_scores_by_period[period] || [];
    }

    const includeLow = showLowConf.checked;
    if (!includeLow) {
        scores = scores.filter(s => !s.low_confidence && !s.always_present);
    }

    // Sort by lift ascending (horizontal bars render bottom-up)
    scores = [...scores].sort((a, b) => (a.lift || 0) - (b.lift || 0));

    const colors = scores.map(s => {
        if (s.always_present) return "#8b8fa3";
        if (s.low_confidence) return "#f59e0b";
        if (s.lift && s.lift > 1) return "#ef4444";
        return "#22c55e";
    });

    const traces = [{
        y: scores.map(s => s.ingredient),
        x: scores.map(s => s.lift || 0),
        type: "bar",
        orientation: "h",
        marker: { color: colors },
        text: scores.map(s => {
            const lift = s.lift != null ? s.lift.toFixed(2) : "N/A";
            const conf = s.low_confidence ? " (low conf)" : "";
            return `Lift: ${lift}${conf} | n=${s.n_present}`;
        }),
        hovertemplate: "%{y}<br>Lift: %{x:.2f}<br>Mean BAC present: %{customdata[0]:.3f}‰<br>Mean BAC absent: %{customdata[1]:.3f}‰<br>n=%{customdata[2]}<extra></extra>",
        customdata: scores.map(s => [s.mean_bac_present || 0, s.mean_bac_absent || 0, s.n_present]),
    }];

    // Reference line at lift = 1
    const shapes = [{
        type: "line",
        x0: 1, x1: 1,
        y0: -0.5, y1: scores.length - 0.5,
        line: { color: "#8b8fa3", width: 1, dash: "dash" },
    }];

    const height = Math.max(300, scores.length * 28 + 60);

    const layout = {
        paper_bgcolor: "#1a1d27",
        plot_bgcolor: "#1a1d27",
        font: { color: "#e1e4eb", size: 11 },
        margin: { l: 220, r: 20, t: 10, b: 40 },
        xaxis: {
            gridcolor: "#2a2d3a",
            title: "Lift Score",
            rangemode: "tozero",
        },
        yaxis: {
            gridcolor: "#2a2d3a",
            automargin: true,
        },
        shapes: shapes,
        height: height,
    };

    Plotly.newPlot("lift-chart", traces, layout, { responsive: true });
}

// ---------------------------------------------------------------------------
// Episode table
// ---------------------------------------------------------------------------

// Sort state for episode table
let episodeSort = { column: "date", direction: "desc" };

// Build a quick lift lookup from the overall scores
function _buildLiftMap() {
    const map = {};
    if (currentData && currentData.lift_scores_overall) {
        for (const s of currentData.lift_scores_overall) {
            map[s.ingredient] = s;
        }
    }
    return map;
}

function _ingredientColor(score) {
    if (!score) return "#8b8fa3";           // unknown → grey
    if (score.low_confidence) return "#f59e0b"; // low confidence → amber
    if (score.lift != null && score.lift > 1) return "#ef4444"; // suspect → red
    return "#22c55e";                         // safe → green
}

function _sortReadings(readings) {
    const dir = episodeSort.direction === "desc" ? -1 : 1;
    if (episodeSort.column === "date") {
        return readings.sort((a, b) => dir * (a.bac_datetime || "").localeCompare(b.bac_datetime || ""));
    } else if (episodeSort.column === "bac") {
        return readings.sort((a, b) => dir * (a.promille - b.promille));
    }
    return readings;
}

function _updateSortArrows() {
    document.querySelectorAll("#episodes-table th.sortable").forEach(th => {
        const arrow = th.querySelector(".sort-arrow");
        if (th.dataset.sort === episodeSort.column) {
            arrow.textContent = episodeSort.direction === "desc" ? "▼" : "▲";
            th.setAttribute("aria-sort", episodeSort.direction === "desc" ? "descending" : "ascending");
        } else {
            arrow.textContent = "";
            th.setAttribute("aria-sort", "none");
        }
    });
}

function renderEpisodeTable() {
    const tbody = document.querySelector("#episodes-table tbody");
    tbody.innerHTML = "";

    const readings = _sortReadings(
        currentData.bac_readings.filter(r => r.promille > 0)
    );
    const lookback = currentData.lookback_by_reading || {};
    const liftMap = _buildLiftMap();

    // Find max BAC for proportional bars
    const maxBac = Math.max(...readings.map(r => r.promille), 0.01);

    if (readings.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#8b8fa3">No readings loaded</td></tr>';
        return;
    }

    for (const r of readings) {
        const tr = document.createElement("tr");
        if (r.episode) tr.classList.add("episode-row");

        const date = r.bac_datetime ? r.bac_datetime.slice(0, 10) : r.date || "—";
        const time = r.bac_time || "—";
        const bacPct = Math.round((r.promille / maxBac) * 100);
        const bacColor = r.episode ? "#ef4444" : "#6366f1";

        // Ingredients in window
        const ingredients = lookback[String(r.bac_idx)] || [];
        // Deduplicate (same ingredient can appear from multiple meals)
        const seen = new Set();
        const unique = [];
        for (const ing of ingredients) {
            const key = ing.ingredient;
            if (!seen.has(key)) {
                seen.add(key);
                unique.push(ing);
            }
        }
        // Sort by hours_before ascending (most recent meal first)
        unique.sort((a, b) => (a.hours_before || 99) - (b.hours_before || 99));

        // Build cells using DOM APIs to prevent HTML/script injection
        const tdDate = document.createElement("td");
        tdDate.textContent = date;

        const tdTime = document.createElement("td");
        tdTime.textContent = time;

        const tdBac = document.createElement("td");
        const bacCell = document.createElement("div");
        bacCell.className = "bac-cell";
        const bacStrong = document.createElement("strong");
        bacStrong.style.color = bacColor;
        bacStrong.textContent = `${r.promille}‰`;
        const bacBar = document.createElement("div");
        bacBar.className = "bac-bar";
        bacBar.style.width = `${bacPct}%`;
        bacBar.style.background = bacColor;
        bacCell.appendChild(bacStrong);
        bacCell.appendChild(bacBar);
        tdBac.appendChild(bacCell);

        const tdMeds = document.createElement("td");
        tdMeds.textContent = r.active_medications || "—";

        const tdIng = document.createElement("td");
        tdIng.className = "ing-cell";
        if (unique.length === 0) {
            const none = document.createElement("span");
            none.className = "ing-none";
            none.textContent = "—";
            tdIng.appendChild(none);
        } else {
            for (const ing of unique) {
                const score = liftMap[ing.ingredient];
                const color = _ingredientColor(score);
                const hrs = ing.hours_before != null ? `${ing.hours_before}h` : "≈";
                const approx = ing.approximate ? " ~" : "";
                const pill = document.createElement("span");
                pill.className = "ing-pill";
                pill.style.color = color;
                pill.textContent = `${ing.ingredient} `;
                const small = document.createElement("small");
                small.textContent = `${hrs}${approx}`;
                pill.appendChild(small);
                tdIng.appendChild(pill);
            }
        }

        const tdComment = document.createElement("td");
        tdComment.textContent = r.comment || "—";

        tr.appendChild(tdDate);
        tr.appendChild(tdTime);
        tr.appendChild(tdBac);
        tr.appendChild(tdMeds);
        tr.appendChild(tdIng);
        tr.appendChild(tdComment);
        tbody.appendChild(tr);
    }

    // Wire up filter (re-attach to avoid duplicates)
    const filterInput = document.getElementById("episode-filter");
    const applyFilter = () => {
        const q = filterInput.value.toLowerCase();
        const rows = tbody.querySelectorAll("tr");
        for (const row of rows) {
            const text = row.textContent.toLowerCase();
            row.style.display = text.includes(q) ? "" : "none";
        }
    };
    filterInput.oninput = applyFilter;
    // Re-apply current filter after sort/re-render
    applyFilter();

    // Wire up sortable column headers
    document.querySelectorAll("#episodes-table th.sortable").forEach(th => {
        const btn = th.querySelector("button");
        if (!btn) return;
        btn.onclick = () => {
            const col = th.dataset.sort;
            if (episodeSort.column === col) {
                episodeSort.direction = episodeSort.direction === "desc" ? "asc" : "desc";
            } else {
                episodeSort.column = col;
                episodeSort.direction = "desc";
            }
            renderEpisodeTable();
        };
    });

    _updateSortArrows();
}
