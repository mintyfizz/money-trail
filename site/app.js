/* Money Trail dashboard. Reads data/index.json and one data/fy{year}.json per fiscal year.
   All money arrives as integer cents and is only converted to dollars for display. */
(function () {
  "use strict";

  const FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY"
  };
  const RAMP = d3.quantize(d3.interpolateLab("#dde7d6", "#1e5a43"), 7);
  const RED = "#a8352f";
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const state = { years: [], data: {}, year: null, selected: null, measure: "total", sort: { key: "net", dir: -1 }, timer: null };
  const $ = (id) => document.getElementById(id);

  /* ---------- Formatting ---------- */
  function dollars(cents) { return cents / 100; }
  function money(cents) {
    if (cents === null || cents === undefined) return "not available";
    const value = dollars(cents);
    const sign = value < 0 ? "−" : "";
    const abs = Math.abs(value);
    const units = [[1e12, "trillion"], [1e9, "billion"], [1e6, "million"]];
    for (const [size, word] of units) {
      if (abs >= size) {
        const n = abs / size;
        const digits = n >= 100 ? 0 : n >= 10 ? 1 : 2;
        return `${sign}$${n.toFixed(digits)} ${word}`;
      }
    }
    return `${sign}$${d3.format(",.0f")(abs)}`;
  }
  function shortMoney(cents) {
    const value = Math.abs(dollars(cents));
    const sign = cents < 0 ? "−" : "";
    if (value >= 1e9) return `${sign}$${d3.format(".3~r")(value / 1e9)}B`;
    if (value >= 1e6) return `${sign}$${d3.format(".3~r")(value / 1e6)}M`;
    if (value >= 1e3) return `${sign}$${d3.format(".3~r")(value / 1e3)}K`;
    return `${sign}$${d3.format(",.0f")(value)}`;
  }
  function perResident(cents, population) {
    if (!population) return null;
    return dollars(cents) / population;
  }
  function perText(value) { return value === null ? "not available" : `$${d3.format(",.0f")(value)}`; }
  function pct(x, digits = 1) { return `${d3.format(`.${digits}f`)(x * 100)}%`; }
  function ordinal(n) { const s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); }
  function esc(text) { return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]); }
  function titleCase(text) {
    if (!text) return text;
    if (text !== text.toUpperCase()) return text;
    return text.toLowerCase().replace(/\b([a-z])/g, (m) => m.toUpperCase())
      .replace(/\b(Llc|Inc|Lp|Llp|Usa|Us|Nasa|Ibm|Bae|Ii|Iii)\b/g, (m) => m.toUpperCase());
  }

  function sentence(text) {
    // Award descriptions arrive in capitals; show them in sentence case, trimmed.
    const t = text.trim().replace(/\s+/g, " ");
    const lower = t === t.toUpperCase() ? t.toLowerCase() : t;
    const cut = lower.length > 110 ? lower.slice(0, 107).replace(/\s\S*$/, "") + "\u2026" : lower;
    return cut.charAt(0).toUpperCase() + cut.slice(1);
  }

  /* ---------- Values ---------- */
  function yearData(year = state.year) { return state.data[year]; }
  function stateRecord(code, year = state.year) { const d = yearData(year); return d && d.states[code]; }
  function value(code, year = state.year) {
    const s = stateRecord(code, year);
    if (!s) return null;
    return state.measure === "per_resident" ? perResident(s.net_obligations_cents, s.population) : dollars(s.net_obligations_cents);
  }
  function hasPopulation(year = state.year) {
    const d = yearData(year);
    return d && Object.values(d.states).some((s) => s.population);
  }

  /* ---------- Map ---------- */
  let paths, scale;
  function drawMap(topo) {
    const svg = d3.select("#map");
    const path = d3.geoPath();
    const states = topojson.feature(topo, topo.objects.states).features.filter((f) => FIPS[f.id]);
    paths = svg.append("g").selectAll("path").data(states).join("path")
      .attr("class", "state")
      .attr("d", path)
      .attr("tabindex", 0)
      .attr("role", "button")
      .on("click", (event, f) => select(FIPS[f.id]))
      .on("keydown", (event, f) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(FIPS[f.id]); } })
      .on("mousemove", (event, f) => showTip(event, FIPS[f.id]))
      .on("mouseleave", hideTip)
      .on("focus", (event, f) => showTip(event, FIPS[f.id], true))
      .on("blur", hideTip);
    svg.append("path").attr("class", "nation").attr("d", path(topojson.mesh(topo, topo.objects.states, (a, b) => a === b)));
    svg.append("path").attr("class", "selected-outline").attr("id", "selected-outline");
    state.geo = { path, states };
  }

  function colour(v) {
    if (v === null || v === undefined) return "#e4e7e0";
    if (v < 0) return RED;
    return scale(v);
  }

  function renderMap() {
    const values = Object.keys(yearData().states).map((c) => value(c)).filter((v) => v !== null && v >= 0);
    scale = d3.scaleQuantile().domain(values).range(RAMP);
    paths.transition().duration(reduceMotion ? 0 : 450).attr("fill", (f) => colour(value(FIPS[f.id])));
    paths.attr("aria-label", (f) => {
      const code = FIPS[f.id], s = stateRecord(code);
      return s ? `${s.name}: ${label(code)}, ${ordinal(s.rank)} of 51` : code;
    }).classed("selected", (f) => FIPS[f.id] === state.selected);
    const chosen = state.geo.states.find((f) => FIPS[f.id] === state.selected);
    d3.select("#selected-outline").attr("d", chosen ? state.geo.path(chosen) : null);
    renderLegend(values);
  }

  function label(code) {
    const s = stateRecord(code);
    if (!s) return "no data";
    return state.measure === "per_resident"
      ? `${perText(perResident(s.net_obligations_cents, s.population))} per resident`
      : money(s.net_obligations_cents);
  }

  function renderLegend(values) {
    const cuts = scale.quantiles();
    const fmt = state.measure === "per_resident" ? (v) => `$${d3.format(",.0f")(v)}` : (v) => shortMoney(v * 100);
    const anyNegative = Object.keys(yearData().states).some((c) => (value(c) ?? 0) < 0);
    $("legend").innerHTML = `
      <div class="legend-swatches">${RAMP.map((c) => `<span style="background:${c}"></span>`).join("")}</div>
      <div class="legend-labels">${cuts.map((c, i) => `<span style="left:calc(${i + 1} * 3.5rem)">${fmt(c)}</span>`).join("")}</div>
      ${anyNegative ? `<span class="legend-negative"><i></i> Net de-obligation (more taken back than committed)</span>` : ""}`;
    $("map-note").textContent = state.measure === "per_resident"
      ? "Shading groups states into seven equal-count bands of net obligations per resident."
      : "Shading groups states into seven equal-count bands of net obligations. Alaska and Hawaii are not to scale.";
  }

  function showTip(event, code, fromFocus) {
    const s = stateRecord(code);
    if (!s) return;
    const tip = $("tip");
    const box = $("map").getBoundingClientRect();
    const wrap = $("map").parentElement.getBoundingClientRect();
    let x, y;
    if (fromFocus) {
      const b = event.target.getBoundingClientRect();
      x = b.left + b.width / 2 - wrap.left; y = b.top - wrap.top;
    } else { x = event.clientX - wrap.left; y = event.clientY - wrap.top; }
    tip.innerHTML = `<strong>${esc(s.name)}</strong><br>${esc(label(code))}, ${ordinal(s.rank)} of 51`;
    tip.style.left = `${Math.max(80, Math.min(x, box.width - 80))}px`;
    tip.style.top = `${y}px`;
    tip.hidden = false;
  }
  function hideTip() { $("tip").hidden = true; }

  /* ---------- Headline ---------- */
  function renderLede() {
    const d = yearData();
    $("lede").innerHTML = `Federal agencies committed a net <span class="figure">${money(d.national.net_obligations_cents)}</span> in contracts in fiscal year ${d.fiscal_year}.`;
    $("lede-sub").textContent = "Select a state to see which agencies committed funding for work there and which companies received it.";
    document.title = `Money Trail: federal contracts, fiscal year ${d.fiscal_year}`;
  }

  /* ---------- Panel ---------- */
  function bars(items, total, nameKey = "name") {
    const max = d3.max(items, (i) => Math.abs(i.amount_cents)) || 1;
    return `<ul class="bars">${items.map((i) => `
      <li><span>${esc(titleCase(i[nameKey]))}</span>
        <span class="amount${i.amount_cents < 0 ? " negative" : ""}">${money(i.amount_cents)}</span>
        <span class="bar" aria-hidden="true"><span class="${i.amount_cents < 0 ? "negative" : ""}" style="width:${(100 * Math.abs(i.amount_cents) / max).toFixed(1)}%"></span></span>
      </li>`).join("")}</ul>`;
  }

  function sparkline(code) {
    const points = state.years.map((y) => ({ y, s: stateRecord(code, y) })).filter((p) => p.s);
    if (points.length < 2) return "";
    const w = 260, h = 64, pad = 18;
    const x = d3.scalePoint().domain(points.map((p) => p.y)).range([pad, w - pad]);
    const yv = points.map((p) => state.measure === "per_resident" ? perResident(p.s.net_obligations_cents, p.s.population) ?? 0 : dollars(p.s.net_obligations_cents));
    const y = d3.scaleLinear().domain([Math.min(0, d3.min(yv)), d3.max(yv)]).nice().range([h - 16, 6]);
    const line = d3.line().x((p, i) => x(points[i].y)).y((p, i) => y(yv[i]));
    const current = points.findIndex((p) => p.y === state.year);
    return `<svg class="spark" width="${w}" height="${h}" role="img" aria-label="Net obligations by fiscal year, ${points[0].y} to ${points[points.length - 1].y}">
      <path d="${line(points)}"></path>
      ${current >= 0 ? `<circle cx="${x(state.year)}" cy="${y(yv[current])}" r="3.5"></circle>` : ""}
      <text x="${x(points[0].y)}" y="${h - 2}" text-anchor="middle">${points[0].y}</text>
      <text x="${x(points[points.length - 1].y)}" y="${h - 2}" text-anchor="middle">${points[points.length - 1].y}</text>
    </svg>`;
  }

  function reconciliationLine(s) {
    const r = s.reconciliation && s.reconciliation.contracts;
    if (!r) return `<p class="check">Not yet compared with USAspending's published figure.</p>`;
    const lead = "Contracts excluding indefinite-delivery vehicles";
    if (r.official_cents === null || r.official_cents === undefined || r.ours_cents === null || r.ours_cents === undefined) {
      return `<p class="check">${lead}: could not be compared, because ${r.official_cents == null ? "USAspending's" : "this project's"} figure is missing.</p>`;
    }
    if (r.official_cents === 0) {
      return `<p class="check">${lead}: USAspending publishes $0 for this state; this project finds ${money(r.ours_cents)}.${r.ours_cents ? " That difference cannot be expressed as a percentage and fails the check." : ""}</p>`;
    }
    const within = Math.abs(r.gap_pct) <= 1;
    return `<p class="check">${lead}: ${money(r.ours_cents)} here against ${money(r.official_cents)} published by USAspending, a difference of ${money(r.gap_cents)} (${d3.format("+.3f")(r.gap_pct)}%), ${within ? "within" : "outside"} the 1% tolerance per state.</p>`;
  }

  function changesBlock(s) {
    const c = s.changes, meta = yearData().changes;
    if (!c || !meta) return "";
    const unknown = meta.unknown_year && meta.unknown_year.rows
      ? ` A further ${d3.format(",")(meta.unknown_year.rows)} changed rows nationally (${money(meta.unknown_year.cents)}) have no date or fiscal year, so they may belong to this year too; they are listed in the source notes.`
      : "";
    return `<h3>Changed in Treasury's latest monthly release</h3>
      <p class="note">The ${esc(meta.snapshot_date || "latest")} release added or updated ${d3.format(",")(c.added_or_updated_rows)} transactions for this state and year${c.flagged_correction_rows ? `, ${d3.format(",")(c.flagged_correction_rows)} of them flagged as corrections` : ""}. ${c.deobligation_rows ? `${d3.format(",")(c.deobligation_rows)} of these take money back (${money(c.deobligation_cents)}).` : ""} The file cannot separate new transactions from updated ones.${unknown}</p>`;
  }

  function renderPanel() {
    const d = yearData();
    const panel = $("panel");
    if (!state.selected || !d.states[state.selected]) {
      panel.innerHTML = `
        <h2>United States</h2>
        <p class="state-figure figure">${money(d.national.net_obligations_cents)}</p>
        <p class="facts">${d3.format(",")(d.national.transactions)} contract transactions on ${d3.format(",")(d.national.awards)} awards, fiscal year ${d.fiscal_year}</p>
        <h3>Largest agencies nationally</h3>
        ${bars(d.national.agencies.slice(0, 8))}
        <p class="note hint">Select a state on the map or in the table for its agencies and largest recipients.</p>`;
      return;
    }
    const code = state.selected, s = d.states[code];
    const prev = stateRecord(code, state.year - 1);
    const per = perResident(s.net_obligations_cents, s.population);
    const change = prev && prev.net_obligations_cents ? (s.net_obligations_cents - prev.net_obligations_cents) / Math.abs(prev.net_obligations_cents) : null;
    const types = s.types.filter((t) => t.amount_cents !== 0);
    panel.innerHTML = `
      <h2>${esc(s.name)}</h2>
      <p class="state-figure figure${s.net_obligations_cents < 0 ? " negative" : ""}">${money(s.net_obligations_cents)}</p>
      <p class="facts">${ordinal(s.rank)} of 51 states${per === null ? "" : `, ${perText(per)} per resident`}${change === null ? "" : `, ${change >= 0 ? "up" : "down"} ${pct(Math.abs(change))} on ${state.year - 1}`}. ${d3.format(",")(s.transactions)} transactions on ${d3.format(",")(s.awards)} awards.</p>
      ${sparkline(code)}
      ${s.deobligations_cents ? `<p class="note">Includes ${money(s.deobligations_cents)} taken back on earlier commitments.</p>` : ""}
      <h3>Awarding agencies</h3>
      ${bars(s.agencies.slice(0, 6))}
      <h3>Largest recipients</h3>
      <ol class="recipients">${s.recipients.slice(0, 6).map((r) => `
        <li><div class="row"><span class="name">${esc(titleCase(r.name))}</span><span class="figure${r.amount_cents < 0 ? " negative" : ""}">${money(r.amount_cents)}</span></div>
          <div class="meta">${d3.format(",")(r.awards)} award${r.awards === 1 ? "" : "s"}${r.parent && r.parent !== r.name ? `; part of ${esc(titleCase(r.parent))}` : ""}${r.example ? `. Largest: ${esc(sentence(r.example))}` : ""}</div></li>`).join("")}</ol>
      <p class="note">Recipients are grouped by their federal entity ID, never by name.</p>
      <h3>By contract type</h3>
      ${bars(types)}
      ${changesBlock(s)}
      ${reconciliationLine(s)}
      <p class="note"><a href="https://www.usaspending.gov/state/${s.name.toLowerCase().replace(/ /g, "-")}/${state.year}">${esc(s.name)} on USAspending.gov</a></p>
      <button type="button" class="clear" id="clear">Back to the national view</button>`;
    $("clear").addEventListener("click", () => select(null));
  }

  /* ---------- Outside the map ---------- */
  function renderOutside() {
    const o = yearData().outside_map;
    const terr = (o.territories || []).slice(0, 4).map((t) => `${esc(t.name)} ${money(t.amount_cents)}`).join("; ");
    const items = [
      ["Territories and freely associated states", o.territory_cents ?? 0, terr || "None recorded"],
      ["Outside the United States", o.foreign_cents ?? 0, "Work performed abroad, by country code"],
      ["No location recorded", o.unknown_cents ?? 0, "Transactions with no place of performance in the record"],
    ];
    $("outside").innerHTML = items.map(([name, cents, detail]) => `
      <div class="item"><div class="label">${name}</div>
        <div class="value figure${cents < 0 ? " negative" : ""}">${money(cents)}</div>
        <div class="detail">${detail}</div></div>`).join("");
  }

  /* ---------- Table ---------- */
  function renderTable() {
    const d = yearData();
    const rows = Object.entries(d.states).map(([code, s]) => {
      const prev = stateRecord(code, state.year - 1);
      return {
        code, name: s.name, net: s.net_obligations_cents, per: perResident(s.net_obligations_cents, s.population),
        change: prev && prev.net_obligations_cents ? (s.net_obligations_cents - prev.net_obligations_cents) / Math.abs(prev.net_obligations_cents) : null,
        agency: s.agencies[0] ? titleCase(s.agencies[0].name) : "",
      };
    });
    const hasPrior = state.years.includes(state.year - 1);
    document.querySelectorAll('#state-table [data-col="change"]').forEach((el) => { el.hidden = !hasPrior; });
    const { key, dir } = state.sort;
    rows.sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av === null) return 1; if (bv === null) return -1;
      return (typeof av === "string" ? av.localeCompare(bv) : av - bv) * dir;
    });
    document.querySelectorAll("#state-table th").forEach((th) => {
      const b = th.querySelector("button");
      if (b && b.dataset.sort === key) th.setAttribute("aria-sort", dir > 0 ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });
    $("state-table").querySelector("tbody").innerHTML = rows.map((r) => `
      <tr data-code="${r.code}" class="${r.code === state.selected ? "selected" : ""}">
        <td><a href="#fy=${state.year}&state=${r.code}">${esc(r.name)}</a></td>
        <td class="num${r.net < 0 ? " negative" : ""}">${money(r.net)}</td>
        <td class="num">${perText(r.per)}</td>
        <td class="num">${r.change === null ? "" : `${r.change >= 0 ? "+" : "−"}${pct(Math.abs(r.change))}`}</td>
        <td>${esc(r.agency)}</td>
      </tr>`).join("");
  }

  /* ---------- About ---------- */
  function renderAbout() {
    const words = { passed: "passed", gaps: "did not pass", incomplete: "incomplete" };
    const parts = state.years.map((y) => {
      const r = state.data[y].reconciliation;
      if (!r) return `<li>FY${y}: not yet compared.</li>`;
      const c = r.contracts;
      const compared = 51 - new Set([...(c.missing_ours || []), ...(c.missing_official || [])]).size;
      return `<li>FY${y}: check <strong>${words[c.status] || c.status}</strong>. For the ${compared} of 51 places compared, this project totals ${money(c.total_ours_cents)} against USAspending's ${money(c.total_official_cents)} (${c.total_gap_pct === null ? "no percentage" : d3.format("+.3f")(c.total_gap_pct) + "%"}). ${c.states_within_1_pct} places are within 1%, ${c.states_within_0_1_pct} within 0.1%.</li>`;
    });
    $("reconciliation").innerHTML = `<p>Each state's contract total is compared with USAspending's own published figure, using the same dates, place and contract types. The check passes only if all 51 places are present, the national gap is within 0.1% and every place is within 1%. USAspending's figures are live, so records updated after the archive was made can open small gaps.</p><ul>${parts.join("")}</ul>`;
    const latest = yearData().reconciliation;
    const agencies = latest && latest.agencies ? latest.agencies.filter((a) => a.gap_cents !== null) : [];
    if (agencies.length) {
      const big = agencies.filter((a) => Math.abs(a.gap_cents) >= 100000000 * 100);
      const rest = agencies.filter((a) => Math.abs(a.gap_cents) < 100000000 * 100);
      $("reconciliation").insertAdjacentHTML("beforeend", `<p>Compared agency by agency over the same 51 places, ${rest.length} of the ${agencies.length} largest agencies are within $100 million of USAspending's figure. ${big.length ? `The gap sits in ${big.map((a) => `${esc(titleCase(a.agency))} (${money(a.gap_cents)})`).join(", ")}.` : ""}</p>`);
    }
    const d = yearData();
    const matches = d.deleted_key_matches;
    const cleaned = matches && matches.rows && !matches.applied
      ? ` Figures are the archive as published. It contains ${d3.format(",")(matches.rows)} rows (${money(matches.cents)}) whose keys Treasury's delta file of the same date lists as deleted; they are counted, because removing them did not match USAspending's published totals either.`
      : "";
    const ch = d.changes;
    const unknownRows = ch && ch.unknown_year_rows && ch.unknown_year_rows.length
      ? ` The ${esc(ch.source_file || "delta file")} has ${ch.unknown_year_rows.length} changed rows with no date or fiscal year: ${ch.unknown_year_rows.map((r) => `${r.key} (${r.amount_raw || "no amount"}; ${r.source_file} row ${r.source_row})`).join("; ")}.`
      : "";
    $("source").textContent = `Source: Treasury's national contracts archive for fiscal year ${d.fiscal_year} (${d.source_zip}, snapshot ${d.snapshot_date}).${cleaned}${unknownRows} Summaries generated ${d.generated_at.slice(0, 10)}.`;
  }

  /* ---------- Controls ---------- */
  function renderYearButtons() {
    $("year-buttons").innerHTML = state.years.map((y) =>
      `<button type="button" data-year="${y}" aria-pressed="${y === state.year}">${y}</button>`).join("");
    $("play").hidden = state.years.length < 2;
  }

  function measureControls() {
    const per = document.querySelector('input[name="measure"][value="per_resident"]');
    const ok = hasPopulation();
    per.disabled = !ok;
    per.parentElement.classList.toggle("disabled", !ok);
    per.parentElement.title = ok ? "" : "Population figures are not available for this year";
    if (!ok && state.measure === "per_resident") {
      state.measure = "total";
      document.querySelector('input[name="measure"][value="total"]').checked = true;
    }
  }

  function render() {
    measureControls();
    renderYearButtons();
    renderLede();
    renderMap();
    renderPanel();
    renderOutside();
    renderTable();
    renderAbout();
    const hash = `#fy=${state.year}${state.selected ? `&state=${state.selected}` : ""}`;
    if (location.hash !== hash) history.replaceState(null, "", hash);
  }

  function select(code) {
    state.selected = code;
    render();
    if (code && window.matchMedia("(max-width: 900px)").matches) $("panel").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
  }

  function setYear(year) { state.year = year; render(); }

  function stopPlay() {
    clearInterval(state.timer); state.timer = null;
    $("play").setAttribute("aria-pressed", "false"); $("play").textContent = "Play years";
  }

  function bind() {
    $("year-buttons").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-year]"); if (!b) return;
      stopPlay(); setYear(+b.dataset.year);
    });
    $("play").addEventListener("click", () => {
      if (state.timer) return stopPlay();
      $("play").setAttribute("aria-pressed", "true"); $("play").textContent = "Stop";
      let i = 0; setYear(state.years[0]);
      state.timer = setInterval(() => {
        i += 1;
        if (i >= state.years.length) return stopPlay();
        setYear(state.years[i]);
      }, 1600);
    });
    document.querySelectorAll('input[name="measure"]').forEach((input) =>
      input.addEventListener("change", () => { state.measure = input.value; render(); }));
    document.querySelectorAll("#state-table th button").forEach((b) => b.addEventListener("click", () => {
      const key = b.dataset.sort;
      state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : (key === "name" ? 1 : -1) };
      renderTable();
    }));
    $("state-table").querySelector("tbody").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-code]"); if (!tr) return;
      e.preventDefault(); select(tr.dataset.code);
      $("map").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.selected) select(null); });
  }

  function readHash() {
    const params = new URLSearchParams(location.hash.slice(1));
    const fy = +params.get("fy"), code = (params.get("state") || "").toUpperCase();
    if (state.years.includes(fy)) state.year = fy;
    if (code && stateRecord(code)) state.selected = code;
  }

  async function start() {
    try {
      const [index, topo] = await Promise.all([d3.json("data/index.json"), d3.json("vendor/states-albers-10m.json")]);
      state.years = index.years.map((y) => y.fiscal_year).sort();
      const files = await Promise.all(state.years.map((y) => d3.json(`data/fy${y}.json`)));
      files.forEach((f) => { state.data[f.fiscal_year] = f; });
      state.year = state.years[state.years.length - 1];
      readHash();
      drawMap(topo);
      bind();
      render();
    } catch (err) {
      $("lede").innerHTML = `<span class="error">The data files did not load (${esc(err.message)}). If you opened this page from disk, serve the folder over HTTP instead.</span>`;
      console.error(err);
    }
  }

  start();
})();
