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
const thresholdSlider = document.getElementById("threshold-slider");
const thresholdValue = document.getElementById("threshold-value");
const recomputeBtn = document.getElementById("recompute-btn");
const periodSelect = document.getElementById("period-select");
const showLowConf = document.getElementById("show-low-conf");
const hideProteins = document.getElementById("hide-proteins");
const splitCompounds = document.getElementById("split-compounds");
const excludeProteins = document.getElementById("exclude-proteins");
const reportSection = document.getElementById("report-section");
const plannerSection = document.getElementById("planner-section");
const generateReportBtn = document.getElementById("generate-report-btn");
const reportContent = document.getElementById("report-content");
const plannerInput = document.getElementById("planner-input");
const checkRiskBtn = document.getElementById("check-risk-btn");
const plannerResult = document.getElementById("planner-result");

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
    const excl = excludeProteins.checked;

    try {
        const resp = await fetch(`/upload?hours=${hours}&min_obs=${minObs}&split_compounds=${split}&exclude_proteins=${excl}`, {
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
thresholdSlider.addEventListener("input", () => {
    thresholdValue.textContent = thresholdSlider.value;
});
// Sync label with actual slider value on load
thresholdSlider.value = "2.0";
thresholdValue.textContent = thresholdSlider.value;

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
                exclude_proteins: excludeProteins.checked,
                episode_threshold: parseFloat(thresholdSlider.value),
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
hideProteins.addEventListener("change", renderLiftChart);

// ---------------------------------------------------------------------------
// Render everything
// ---------------------------------------------------------------------------
function renderAll() {
    if (!currentData) return;

    controlsSection.classList.remove("hidden");
    summarySection.classList.remove("hidden");
    timelineSection.classList.remove("hidden");
    liftSection.classList.remove("hidden");
    reportSection.classList.remove("hidden");
    plannerSection.classList.remove("hidden");
    episodesSection.classList.remove("hidden");

    // Reset report on new data
    reportContent.classList.add("hidden");
    reportContent.innerHTML = "";
    plannerResult.classList.add("hidden");

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
    const episodeThreshold = parseFloat(thresholdSlider.value);
    const episodes = readings.filter(r => r.promille >= episodeThreshold);

    // Build x/y arrays with null gaps where readings are >8h apart
    const GAP_MS = 8 * 60 * 60 * 1000; // 8 hours
    const bacX = [];
    const bacY = [];
    for (let i = 0; i < readings.length; i++) {
        if (i > 0) {
            const prev = new Date(readings[i - 1].bac_datetime).getTime();
            const curr = new Date(readings[i].bac_datetime).getTime();
            if (curr - prev > GAP_MS) {
                bacX.push(null);
                bacY.push(null);
            }
        }
        bacX.push(readings[i].bac_datetime);
        bacY.push(readings[i].promille);
    }

    const traces = [
        {
            x: bacX,
            y: bacY,
            mode: "lines+markers",
            type: "scatter",
            name: "BAC Reading",
            marker: { color: "#6366f1", size: 6 },
            line: { color: "#6366f1", width: 1.5 },
            connectgaps: false,
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

    const medColors = [
        "rgba(34,197,94,",   // green
        "rgba(245,158,11,",  // amber
        "rgba(99,102,241,",  // indigo
        "rgba(236,72,153,",  // pink
        "rgba(14,165,233,",  // sky
    ];
    const medNames = Object.keys(medPeriods);

    const layout = {
        paper_bgcolor: "#1a1d27",
        plot_bgcolor: "#1a1d27",
        font: { color: "#e1e4eb" },
        margin: { l: 140, r: 60, t: 30, b: 10 },
        xaxis: {
            gridcolor: "#2a2d3a",
            showticklabels: false,
        },
        yaxis: {
            gridcolor: "#2a2d3a",
            title: "BAC (‰)",
            rangemode: "tozero",
        },
        legend: { x: 0, y: 1.12, orientation: "h" },
        hovermode: "closest",
    };

    Plotly.newPlot("bac-timeline-chart", traces, layout, { responsive: true });

    // --- Carbs/meals strip below ---
    _renderCarbsTimeline();

    // --- Medication Gantt strip below ---
    _renderMedTimeline(medPeriods, medColors, medNames);
}

// ---------------------------------------------------------------------------
// Timeline zoom controls
// ---------------------------------------------------------------------------
(function () {
    const allBtn = document.getElementById("zoom-all-btn");
    const weekBtn = document.getElementById("zoom-7d-btn");
    const prevBtn = document.getElementById("zoom-prev-btn");
    const nextBtn = document.getElementById("zoom-next-btn");
    let windowEnd = null; // ms timestamp of current 7-day window end

    function applyRange(startMs, endMs) {
        const s = new Date(startMs).toISOString();
        const e = new Date(endMs).toISOString();
        const update = { "xaxis.range[0]": s, "xaxis.range[1]": e };
        const els = ["bac-timeline-chart", "carbs-timeline-chart", "med-timeline-chart"]
            .map(id => document.getElementById(id))
            .filter(el => el && el.data);
        Promise.all(els.map(el => Plotly.relayout(el, update)));
    }

    function resetToAll() {
        const els = ["bac-timeline-chart", "carbs-timeline-chart", "med-timeline-chart"]
            .map(id => document.getElementById(id))
            .filter(el => el && el.data);
        Promise.all(els.map(el => Plotly.relayout(el, { "xaxis.autorange": true })));
    }

    function enterWeekMode() {
        allBtn.disabled = false;
        weekBtn.disabled = true;
        prevBtn.classList.remove("hidden");
        nextBtn.classList.remove("hidden");
        const lastDate = currentData && currentData.summary && currentData.summary.date_max
            ? new Date(currentData.summary.date_max).getTime() + 86400000
            : Date.now();
        windowEnd = lastDate;
        applyRange(windowEnd - 7 * 86400000, windowEnd);
    }

    function exitWeekMode() {
        allBtn.disabled = true;
        weekBtn.disabled = false;
        prevBtn.classList.add("hidden");
        nextBtn.classList.add("hidden");
        windowEnd = null;
        resetToAll();
    }

    allBtn.addEventListener("click", exitWeekMode);
    weekBtn.addEventListener("click", enterWeekMode);
    prevBtn.addEventListener("click", () => {
        if (windowEnd == null) return;
        windowEnd -= 7 * 86400000;
        applyRange(windowEnd - 7 * 86400000, windowEnd);
    });
    nextBtn.addEventListener("click", () => {
        if (windowEnd == null) return;
        windowEnd += 7 * 86400000;
        applyRange(windowEnd - 7 * 86400000, windowEnd);
    });

    // Enable "All" button whenever user manually zooms on any chart
    const observer = new MutationObserver(() => {
        const bacEl = document.getElementById("bac-timeline-chart");
        if (bacEl && !bacEl._zoomListenerAttached) {
            bacEl._zoomListenerAttached = true;
            bacEl.on("plotly_relayout", (ev) => {
                if (ev["xaxis.range[0]"] != null) {
                    allBtn.disabled = false;
                }
            });
        }
    });
    observer.observe(document.getElementById("timeline-section"), { childList: true, subtree: true });
})();

function _renderCarbsTimeline() {
    const mealCarbs = currentData.meal_carbs || [];
    const el = document.getElementById("carbs-timeline-chart");
    if (mealCarbs.length === 0) {
        el.innerHTML = "";
        return;
    }

    const traces = [{
        x: mealCarbs.map(d => d.datetime),
        y: mealCarbs.map(d => d.carbs_g),
        type: "bar",
        name: "Carbs (g)",
        marker: { color: "rgba(245,158,11,0.5)" },
        hovertemplate: "<b>%{text}</b><br>Carbs: %{y:.0f}g<br>%{x}<extra></extra>",
        text: mealCarbs.map(d => d.meal || ""),
        width: 4 * 3600 * 1000,
    }];

    const bacChart = document.getElementById("bac-timeline-chart");
    const xRange = bacChart && bacChart.layout ? bacChart.layout.xaxis.range : undefined;

    const layout = {
        paper_bgcolor: "#1a1d27",
        plot_bgcolor: "#1a1d27",
        font: { color: "#e1e4eb", size: 11 },
        margin: { l: 140, r: 60, t: 0, b: 10 },
        height: 120,
        xaxis: {
            gridcolor: "#2a2d3a",
            showticklabels: false,
            range: xRange,
        },
        yaxis: {
            gridcolor: "#2a2d3a",
            title: "Carbs (g)",
            rangemode: "tozero",
            color: "rgba(245,158,11,0.8)",
        },
        hovermode: "closest",
        showlegend: false,
    };

    Plotly.newPlot("carbs-timeline-chart", traces, layout, { responsive: true });
}

function _renderMedTimeline(medPeriods, medColors, medNames) {
    if (medNames.length === 0) {
        document.getElementById("med-timeline-chart").innerHTML = "";
        return;
    }

    const traces = [];
    const shapes = [];

    for (let mi = 0; mi < medNames.length; mi++) {
        const med = medNames[mi];
        const colorBase = medColors[mi % medColors.length];
        const solidColor = colorBase + "0.8)";
        const ranges = medPeriods[med];

        for (let ri = 0; ri < ranges.length; ri++) {
            const range = ranges[ri];
            const start = range.start;
            const lastDataDate = currentData.summary.date_max || new Date().toISOString();
            const stop = range.stop || lastDataDate;
            const startDate = start.slice(0, 10);
            const stopDate = stop.slice(0, 10);

            // Use invisible scatter for hover
            traces.push({
                x: [start, stop],
                y: [mi, mi],
                mode: "markers",
                type: "scatter",
                marker: { size: 8, color: "rgba(0,0,0,0)" },
                showlegend: false,
                cliponaxis: false,
                hovertemplate: `<b>${med}</b><br>${startDate} → ${stopDate}<extra></extra>`,
            });

            // Rectangle shape for the bar
            shapes.push({
                type: "rect",
                xref: "x",
                yref: "y",
                x0: start,
                x1: stop,
                y0: mi - 0.3,
                y1: mi + 0.3,
                fillcolor: solidColor,
                line: { width: 0 },
            });
        }
    }

    // Get x-axis range from BAC chart to keep them aligned
    const bacChart = document.getElementById("bac-timeline-chart");
    const xRange = bacChart && bacChart.layout ? bacChart.layout.xaxis.range : undefined;

    const height = Math.max(60, medNames.length * 28 + 50);

    const layout = {
        paper_bgcolor: "#1a1d27",
        plot_bgcolor: "#1a1d27",
        font: { color: "#e1e4eb", size: 11 },
        margin: { l: 140, r: 60, t: 0, b: 40 },
        height: height,
        xaxis: {
            gridcolor: "#2a2d3a",
            title: "Date",
            range: xRange,
        },
        yaxis: {
            gridcolor: "rgba(0,0,0,0)",
            zeroline: false,
            automargin: true,
            fixedrange: true,
            tickvals: medNames.map((_, i) => i),
            ticktext: medNames,
            range: [-0.5, medNames.length - 0.5],
        },
        shapes: shapes,
        hovermode: "closest",
    };

    Plotly.newPlot("med-timeline-chart", traces, layout, { responsive: true });

    // Link x-axis zoom across all timeline charts
    const bacEl = document.getElementById("bac-timeline-chart");
    const carbsEl = document.getElementById("carbs-timeline-chart");
    const medEl = document.getElementById("med-timeline-chart");
    const allEls = [bacEl, carbsEl, medEl].filter(el => el && el.data);
    let syncing = false;

    function syncZoom(source, ev) {
        if (syncing) return;
        syncing = true;
        const update = {};
        if (ev["xaxis.range[0]"] != null && ev["xaxis.range[1]"] != null) {
            update["xaxis.range[0]"] = ev["xaxis.range[0]"];
            update["xaxis.range[1]"] = ev["xaxis.range[1]"];
        } else if (ev["xaxis.autorange"]) {
            update["xaxis.autorange"] = true;
        }
        if (Object.keys(update).length > 0) {
            const targets = allEls.filter(el => el !== source);
            Promise.all(targets.map(t => Plotly.relayout(t, update)))
                .then(() => Promise.all([_rescaleBacY(), _rescaleCarbsY()]))
                .then(() => { syncing = false; });
        } else {
            syncing = false;
        }
    }

    /** Rescale BAC y-axis to max visible value in current x range. */
    function _rescaleBacY() {
        if (!bacEl || !bacEl.layout) return;
        const readings = currentData.bac_readings || [];
        if (readings.length === 0) return;

        const xRange = bacEl.layout.xaxis.range;
        let maxY = 0;
        if (xRange) {
            const lo = new Date(xRange[0]).getTime();
            const hi = new Date(xRange[1]).getTime();
            for (const r of readings) {
                const t = new Date(r.bac_datetime).getTime();
                if (t >= lo && t <= hi && r.promille > maxY) maxY = r.promille;
            }
        } else {
            for (const r of readings) {
                if (r.promille > maxY) maxY = r.promille;
            }
        }
        if (maxY > 0) {
            return Plotly.relayout(bacEl, { "yaxis.range": [0, maxY * 1.1] });
        }
    }

    /** Rescale carbs y-axis to max visible value in current x range. */
    function _rescaleCarbsY() {
        if (!carbsEl || !carbsEl.layout || !carbsEl.data || !carbsEl.data[0]) return;
        const mealCarbs = currentData.meal_carbs || [];
        if (mealCarbs.length === 0) return;

        const xRange = carbsEl.layout.xaxis.range;
        let maxY = 0;
        if (xRange) {
            const lo = new Date(xRange[0]).getTime();
            const hi = new Date(xRange[1]).getTime();
            for (const d of mealCarbs) {
                const t = new Date(d.datetime).getTime();
                if (t >= lo && t <= hi && d.carbs_g > maxY) maxY = d.carbs_g;
            }
        } else {
            for (const d of mealCarbs) {
                if (d.carbs_g > maxY) maxY = d.carbs_g;
            }
        }
        if (maxY > 0) {
            return Plotly.relayout(carbsEl, { "yaxis.range": [0, maxY * 1.1] });
        }
    }

    allEls.forEach(el => {
        el.on("plotly_relayout", (ev) => syncZoom(el, ev));
    });
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

    if (hideProteins.checked) {
        const proteinKeywords = [
            "chicken", "beef", "pork", "lamb", "turkey", "duck", "veal",
            "venison", "bison", "rabbit", "goat", "ham", "bacon", "sausage",
            "salmon", "tuna", "cod", "trout", "shrimp", "prawn", "crab",
            "lobster", "mackerel", "sardine", "herring", "tilapia", "halibut",
            "bass", "perch", "catfish", "anchovy", "squid", "octopus",
            "mussel", "clam", "oyster", "scallop", "fish",
            "egg", "eggs",
        ];
        scores = scores.filter(s => {
            const name = s.ingredient.toLowerCase();
            return !proteinKeywords.some(kw => name.includes(kw));
        });
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
        hovertemplate: "%{y}<br>Lift: %{x:.2f}<br>Avg BAC present: %{customdata[0]:.3f}‰<br>Avg BAC absent: %{customdata[1]:.3f}‰<br>n=%{customdata[2]}<extra></extra>",
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
        if (r.promille >= parseFloat(thresholdSlider.value)) tr.classList.add("episode-row");

        const date = r.bac_datetime ? r.bac_datetime.slice(0, 10) : r.date || "—";
        const time = r.bac_time || "—";
        const bacPct = Math.round((r.promille / maxBac) * 100);
        const bacColor = r.promille >= parseFloat(thresholdSlider.value) ? "#ef4444" : "#6366f1";

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

// ---------------------------------------------------------------------------
// Analysis Report
// ---------------------------------------------------------------------------
generateReportBtn.addEventListener("click", async () => {
    generateReportBtn.disabled = true;
    generateReportBtn.textContent = "Generating…";
    try {
        const resp = await fetch("/report");
        if (!resp.ok) throw new Error("Report generation failed");
        const data = await resp.json();
        _renderReport(data);
    } catch (e) {
        alert(e.message);
    } finally {
        generateReportBtn.disabled = false;
        generateReportBtn.textContent = "Generate Report";
    }
});

function _renderReport(data) {
    reportContent.innerHTML = "";
    reportContent.classList.remove("hidden");

    // Summary
    const summaryEl = document.createElement("div");
    summaryEl.className = "report-block";
    const summaryH = document.createElement("h3");
    summaryH.textContent = "Summary";
    summaryEl.appendChild(summaryH);
    const table = document.createElement("table");
    table.className = "report-table summary-table";
    const tbody = document.createElement("tbody");
    const s = currentData ? currentData.summary : {};
    const rows = [
        ["Date range", `${s.date_min || "?"} \u2192 ${s.date_max || "?"}`],
        ["BAC readings", s.total_readings || 0],
        ["Episodes (BAC > 0)", s.episodes || 0],
        ["Avg BAC", `${(s.bac_mean || 0).toFixed(2)}\u2030`],
        ["Max BAC", `${(s.bac_max || 0).toFixed(2)}\u2030`],
        ["Unique ingredients", s.unique_ingredients || 0],
        ["Lookback pairs", s.lookback_pairs || 0],
    ];
    for (const [label, value] of rows) {
        const tr = document.createElement("tr");
        const th = document.createElement("td");
        th.className = "summary-label";
        th.textContent = label;
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(th);
        tr.appendChild(td);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    summaryEl.appendChild(table);
    reportContent.appendChild(summaryEl);

    // Top suspects
    if (data.top_suspects && data.top_suspects.length > 0) {
        const block = document.createElement("div");
        block.className = "report-block";
        const h = document.createElement("h3");
        h.textContent = "\uD83D\uDD34 Top Suspect Ingredients";
        block.appendChild(h);
        const table = document.createElement("table");
        table.className = "report-table";
        table.innerHTML = "<thead><tr><th>Ingredient</th><th>Lift</th><th>n</th><th>Assessment</th></tr></thead>";
        const tbody = document.createElement("tbody");
        for (const s of data.top_suspects) {
            const tr = document.createElement("tr");
            const cells = [s.ingredient, s.lift.toFixed(2), s.n, s.assessment];
            for (const val of cells) {
                const td = document.createElement("td");
                td.textContent = val;
                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        block.appendChild(table);
        reportContent.appendChild(block);
    }

    // Safe ingredients
    if (data.safe_ingredients && data.safe_ingredients.length > 0) {
        const block = document.createElement("div");
        block.className = "report-block";
        const h = document.createElement("h3");
        h.textContent = "\uD83D\uDFE2 Likely Safe Ingredients";
        block.appendChild(h);
        const table = document.createElement("table");
        table.className = "report-table";
        table.innerHTML = "<thead><tr><th>Ingredient</th><th>Lift</th><th>n</th><th>Avg BAC</th></tr></thead>";
        const tbody = document.createElement("tbody");
        for (const s of data.safe_ingredients) {
            const tr = document.createElement("tr");
            const cells = [s.ingredient, s.lift.toFixed(2), s.n, s.mean_bac_present.toFixed(3) + "\u2030"];
            for (const val of cells) {
                const td = document.createElement("td");
                td.textContent = val;
                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        block.appendChild(table);
        reportContent.appendChild(block);
    }

    // Medication comparison
    if (data.medication_comparison && data.medication_comparison.length > 0) {
        const block = document.createElement("div");
        block.className = "report-block";
        const h = document.createElement("h3");
        h.textContent = "\uD83D\uDC8A Medication Period Comparison";
        block.appendChild(h);
        const table = document.createElement("table");
        table.className = "report-table";
        table.innerHTML = "<thead><tr><th>Period</th><th>Avg BAC</th><th>Readings</th><th>Top Suspects</th></tr></thead>";
        const tbody = document.createElement("tbody");
        for (const m of data.medication_comparison) {
            const tr = document.createElement("tr");
            const cells = [
                m.period,
                m.mean_bac != null ? m.mean_bac.toFixed(3) + "\u2030" : "—",
                m.n_readings,
                m.top_3_suspects.join(", "),
            ];
            for (const val of cells) {
                const td = document.createElement("td");
                td.textContent = val;
                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        block.appendChild(table);
        reportContent.appendChild(block);
    }

    // Combinations
    if (data.combinations && data.combinations.length > 0) {
        const block = document.createElement("div");
        block.className = "report-block";
        const h = document.createElement("h3");
        h.textContent = "\uD83D\uDD17 Ingredient Combinations";
        block.appendChild(h);
        const table = document.createElement("table");
        table.className = "report-table";
        table.innerHTML = "<thead><tr><th>Pair</th><th>Count</th><th>Avg BAC</th><th>Pair Lift</th></tr></thead>";
        const tbody = document.createElement("tbody");
        for (const c of data.combinations) {
            const tr = document.createElement("tr");
            const cells = [
                c.pair.join(" + "),
                c.count,
                c.mean_bac.toFixed(3) + "\u2030",
                c.pair_lift.toFixed(2),
            ];
            for (const val of cells) {
                const td = document.createElement("td");
                td.textContent = val;
                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        block.appendChild(table);
        reportContent.appendChild(block);
    }

    // Caveats
    if (data.caveats && data.caveats.length > 0) {
        const block = document.createElement("div");
        block.className = "report-block";
        const h = document.createElement("h3");
        h.textContent = "\u26A0\uFE0F Caveats";
        block.appendChild(h);
        const ul = document.createElement("ul");
        for (const c of data.caveats) {
            const li = document.createElement("li");
            li.textContent = c;
            ul.appendChild(li);
        }
        block.appendChild(ul);
        reportContent.appendChild(block);
    }
}

// ---------------------------------------------------------------------------
// Meal Planner
// ---------------------------------------------------------------------------
checkRiskBtn.addEventListener("click", async () => {
    const raw = plannerInput.value.trim();
    if (!raw) return;
    const ingredients = raw.split(",").map(s => s.trim()).filter(Boolean);
    if (ingredients.length === 0) return;

    checkRiskBtn.disabled = true;
    checkRiskBtn.textContent = "Checking…";
    try {
        const resp = await fetch("/predict", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ingredients }),
        });
        if (!resp.ok) throw new Error("Prediction failed");
        const data = await resp.json();
        _renderPrediction(data);
    } catch (e) {
        alert(e.message);
    } finally {
        checkRiskBtn.disabled = false;
        checkRiskBtn.textContent = "Check Risk";
    }
});

function _renderPrediction(data) {
    plannerResult.innerHTML = "";
    plannerResult.classList.remove("hidden");

    // Risk level badge
    const badge = document.createElement("div");
    badge.className = "risk-badge risk-" + data.risk_level.toLowerCase();
    badge.textContent = data.risk_level;
    plannerResult.appendChild(badge);

    // Weighted lift
    if (data.weighted_lift != null) {
        const lift = document.createElement("p");
        lift.className = "risk-lift";
        lift.textContent = `Weighted lift: ${data.weighted_lift.toFixed(2)}`;
        plannerResult.appendChild(lift);
    }

    // Reasoning
    const reason = document.createElement("p");
    reason.className = "risk-reasoning";
    reason.textContent = data.reasoning;
    plannerResult.appendChild(reason);

    // Ingredient breakdown
    if (data.ingredient_details && data.ingredient_details.length > 0) {
        const list = document.createElement("div");
        list.className = "risk-details";
        for (const d of data.ingredient_details) {
            const item = document.createElement("span");
            item.className = "ing-pill";
            if (d.known) {
                const color = d.lift > 1.0 ? "#ef4444" : "#22c55e";
                item.style.color = color;
                item.textContent = `${d.ingredient} (${d.lift.toFixed(2)})`;
            } else {
                item.style.color = "#8b8fa3";
                item.textContent = `${d.ingredient} (?)`;
            }
            list.appendChild(item);
        }
        plannerResult.appendChild(list);
    }
}
