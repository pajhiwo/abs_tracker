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
function renderEpisodeTable() {
    const tbody = document.querySelector("#episodes-table tbody");
    tbody.innerHTML = "";

    const episodes = currentData.bac_readings.filter(r => r.episode);

    if (episodes.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#8b8fa3">No episodes recorded</td></tr>';
        return;
    }

    for (const ep of episodes) {
        const tr = document.createElement("tr");
        const date = ep.bac_datetime ? ep.bac_datetime.slice(0, 10) : ep.date || "—";
        const time = ep.bac_time || "—";
        tr.innerHTML = `
            <td>${date}</td>
            <td>${time}</td>
            <td><strong style="color:#ef4444">${ep.promille}‰</strong></td>
            <td>${ep.active_medications}</td>
            <td>${ep.comment || "—"}</td>
        `;
        tbody.appendChild(tr);
    }
}
