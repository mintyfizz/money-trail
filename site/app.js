/* Money Trail dashboard. Reads data/index.json and one data/fy{year}.json per fiscal year.
   All money arrives as integer cents and is only converted to dollars for display.
   Every figure that a spending-type or agency selection changes is summed from the full
   agency x place x category cells; ranked lists are shown only for the scope they describe. */
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
  const TERRITORY_NAMES = { PR: "Puerto Rico", GU: "Guam", VI: "U.S. Virgin Islands", AS: "American Samoa",
    MP: "Northern Mariana Islands", UM: "U.S. Minor Outlying Islands" };
  const RAMP = d3.quantize(d3.interpolateLab("#dde7d6", "#1e5a43"), 7);
  const RED = "#a8352f";
  const NONE = "#f7f8f4";  // a loaded place with nothing in the selection: shown as none, not as a low band
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* Spending-type choices. A choice is a set of categories; it is available for a year only if
     every kind it needs is loaded for that year. */
  const SCOPES = [
    { id: "all", label: "All federal spending", cats: null },
    { id: "contracts-all", label: "Contracts, including IDVs", cats: ["contracts", "idvs"] },
    { id: "assistance-all", label: "All financial assistance", cats: ["grants", "direct_payments", "loans", "other_assistance", "not_specified"] },
    { id: "grants", label: "Grants and cooperative agreements", cats: ["grants"], group: "Financial assistance" },
    { id: "direct_payments", label: "Direct payments", cats: ["direct_payments"], group: "Financial assistance" },
    { id: "loans", label: "Loans (subsidy cost)", cats: ["loans"], group: "Financial assistance" },
    { id: "other_assistance", label: "Insurance and other assistance", cats: ["other_assistance"], group: "Financial assistance" },
    { id: "not_specified", label: "Assistance type not specified", cats: ["not_specified"], group: "Financial assistance" },
    { id: "contracts", label: "Contracts excluding IDVs", cats: ["contracts"], group: "Contracts" },
    { id: "idvs", label: "Indefinite-delivery vehicles", cats: ["idvs"], group: "Contracts" },
  ];

  const state = { years: [], index: null, data: {}, year: null, scope: "all", agency: null, selected: null,
                  measure: "total", sort: { key: "amount", dir: -1 }, timer: null, cache: new Map() };
  const $ = (id) => document.getElementById(id);

  /* ---------- Formatting ---------- */
  function dollars(cents) { return cents / 100; }
  function money(cents) {
    if (cents === null || cents === undefined) return "not available";
    const value = dollars(cents);
    const sign = value < 0 ? "−" : "";
    const abs = Math.abs(value);
    for (const [size, word] of [[1e12, "trillion"], [1e9, "billion"], [1e6, "million"]]) {
      if (abs >= size) {
        const n = abs / size;
        return `${sign}$${n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)} ${word}`;
      }
    }
    return `${sign}$${d3.format(abs > 0 && abs < 1 ? ".2f" : ",.0f")(abs)}`;
  }
  function shortMoney(cents) {
    const value = Math.abs(dollars(cents)), sign = cents < 0 ? "−" : "";
    if (value >= 1e9) return `${sign}$${d3.format(".3~r")(value / 1e9)}B`;
    if (value >= 1e6) return `${sign}$${d3.format(".3~r")(value / 1e6)}M`;
    if (value >= 1e3) return `${sign}$${d3.format(".3~r")(value / 1e3)}K`;
    return `${sign}$${d3.format(",.0f")(value)}`;
  }
  function perResident(cents, population) { return population && cents !== null ? dollars(cents) / population : null; }
  function perText(value) { return value === null ? "not available" : `${value < 0 ? "−" : ""}$${d3.format(",.0f")(Math.abs(value))}`; }
  function pct(x, digits = 1) { return `${d3.format(`.${digits}f`)(x * 100)}%`; }
  const count = d3.format(",");
  function rankText(t) {
    if (!t.tx && !t.amount) return "no awards in this selection";
    return `${ordinal(t.rank)} of 51${t.tied ? " (tied)" : ""}`;
  }
  function ordinal(n) { const s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); }
  function esc(text) { return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]); }
  function titleCase(text) {
    if (!text || text !== text.toUpperCase()) return text;
    return text.toLowerCase().replace(/\b([a-z])/g, (m) => m.toUpperCase())
      .replace(/\b(Llc|Inc|Lp|Llp|Usa|Us|Nasa|Ibm|Bae|Ii|Iii)\b/g, (m) => m.toUpperCase());
  }
  function sentence(text) {
    const t = text.trim().replace(/\s+/g, " ");
    const lower = t === t.toUpperCase() ? t.toLowerCase() : t;
    const cut = lower.length > 110 ? lower.slice(0, 107).replace(/\s\S*$/, "") + "…" : lower;
    return cut.charAt(0).toUpperCase() + cut.slice(1);
  }

  /* ---------- Scope ---------- */
  function yearData(year = state.year) { return state.data[year]; }
  function scopeDef(id = state.scope) { return SCOPES.find((s) => s.id === id) || SCOPES[0]; }
  function kindOf(d, cat) { return d.categories[cat].kind; }
  function loadedKinds(d) { return Object.keys(d.kinds).filter((k) => d.kinds[k].loaded); }
  /* Categories the scope covers in a year, or null when a kind it needs is not loaded. */
  function scopeCats(year = state.year, id = state.scope) {
    const d = yearData(year);
    if (!d) return null;
    const def = scopeDef(id);
    if (!def.cats) {
      const kinds = loadedKinds(d);
      return kinds.length ? d.category_ids.filter((c) => kinds.includes(kindOf(d, c))) : null;
    }
    return def.cats.every((c) => d.kinds[kindOf(d, c)] && d.kinds[kindOf(d, c)].loaded) ? def.cats : null;
  }
  function scopeKinds(year = state.year) {
    const d = yearData(year), cats = scopeCats(year);
    return cats ? [...new Set(cats.map((c) => kindOf(d, c)))] : [];
  }
  function scopeLabel(year = state.year) {
    const def = scopeDef();
    if (def.id !== "all") return def.label.charAt(0).toLowerCase() + def.label.slice(1);
    const kinds = scopeKinds(year);
    return kinds.length === 2 ? "contracts and financial assistance" : kinds[0] === "contracts" ? "contracts (financial assistance is not loaded for this year)" : "financial assistance (contracts are not loaded for this year)";
  }
  function agencyName(id = state.agency) {
    for (const y of [state.year, ...state.years.slice().reverse()]) {
      const a = yearData(y) && yearData(y).agencies.find((x) => x.id === id);
      if (a) return titleCase(a.name);
    }
    return id;
  }

  /* One pass over the cells for a year, scope and agency. Memoised. */
  function aggregate(year = state.year, agency = state.agency, scope = state.scope) {
    const key = `${year}|${scope}|${agency}`;
    if (state.cache.has(key)) return state.cache.get(key);
    const d = yearData(year), cats = scopeCats(year, scope);
    if (!cats) { state.cache.set(key, null); return null; }
    const catIdx = new Set(cats.map((c) => d.category_ids.indexOf(c)));
    const agencyIdx = agency === null ? null : d.agencies.findIndex((a) => a.id === agency);
    const empty = () => ({ amount: 0, tx: 0, missing: 0, face: 0, cats: new Map(), agencies: new Map() });
    const out = { places: new Map(), national: empty(), territories: new Map(), foreign: empty(), unknown: empty(),
                  agencyPresent: agencyIdx !== null && agencyIdx >= 0 };
    const fillPlaces = () => {
      // Every one of the 51 places gets a value: no rows in scope is a known zero, because the scope is loaded.
      for (const code of Object.keys(d.states)) if (!out.places.has(code)) out.places.set(code, empty());
      // Equal amounts share a rank (1, 2, 2, 4); ties are marked so no order is invented.
      const sorted = [...out.places.values()].sort((x, y) => y.amount - x.amount);
      sorted.forEach((t, i) => { t.rank = i > 0 && t.amount === sorted[i - 1].amount ? sorted[i - 1].rank : i + 1; });
      const counts = new Map();
      sorted.forEach((t) => counts.set(t.amount, (counts.get(t.amount) || 0) + 1));
      sorted.forEach((t) => { t.tied = counts.get(t.amount) > 1; });
    };
    if (agencyIdx === -1) { fillPlaces(); state.cache.set(key, out); return out; }  // agency has no awards this year
    const add = (t, a, cat, amount, tx, missing, face) => {
      t.amount += amount; t.tx += tx; t.missing += missing; t.face += face || 0;
      t.cats.set(cat, (t.cats.get(cat) || 0) + amount);
      t.agencies.set(a, (t.agencies.get(a) || 0) + amount);
    };
    for (const [a, placeKind, place, cat, amount, tx, missing, face] of d.cells) {
      if (!catIdx.has(cat) || (agencyIdx !== null && a !== agencyIdx)) continue;
      add(out.national, a, cat, amount, tx, missing, face);
      if (placeKind === "state") {
        if (!out.places.has(place)) out.places.set(place, empty());
        add(out.places.get(place), a, cat, amount, tx, missing, face);
      } else if (placeKind === "territory") {
        if (!out.territories.has(place)) out.territories.set(place, empty());
        add(out.territories.get(place), a, cat, amount, tx, missing, face);
      } else add(placeKind === "foreign" ? out.foreign : out.unknown, a, cat, amount, tx, missing, face);
    }
    fillPlaces();
    state.cache.set(key, out);
    return out;
  }

  /* Years are only compared when the selection covers exactly the same categories in both,
     so a year with financial assistance is never compared with one without it. */
  function comparable(year) {
    const a = scopeCats(state.year), b = yearData(year) ? scopeCats(year) : null;
    return !!a && !!b && a.length === b.length && a.every((c) => b.includes(c));
  }
  function amountOf(code, year = state.year) {
    const agg = aggregate(year);
    return agg ? agg.places.get(code).amount : null;
  }
  function value(code, year = state.year) {
    const cents = amountOf(code, year);
    if (cents === null) return null;
    return state.measure === "per_resident" ? perResident(cents, yearData(year).states[code].population) : dollars(cents);
  }
  function hasPopulation(year = state.year) { return Object.values(yearData(year).states).some((s) => s.population); }

  /* ---------- Map ---------- */
  let paths, scale;
  function drawMap(topo) {
    const svg = d3.select("#map");
    const path = d3.geoPath();
    const states = topojson.feature(topo, topo.objects.states).features.filter((f) => FIPS[f.id]);
    paths = svg.append("g").selectAll("path").data(states).join("path")
      .attr("class", "state").attr("d", path).attr("tabindex", 0).attr("role", "button")
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
    return v < 0 ? RED : v === 0 ? NONE : scale(v);
  }
  function renderMap() {
    const codes = Object.keys(yearData().states);
    const values = codes.map((c) => value(c)).filter((v) => v !== null && v > 0);
    scale = d3.scaleQuantile().domain(values.length ? values : [0]).range(RAMP);
    paths.transition().duration(reduceMotion ? 0 : 450).attr("fill", (f) => colour(value(FIPS[f.id])));
    const agg = aggregate();
    paths.attr("aria-label", (f) => {
      const code = FIPS[f.id], s = yearData().states[code];
      return agg ? `${s.name}: ${label(code)}, ${rankText(agg.places.get(code))}` : `${s.name}: not loaded for this year`;
    }).classed("selected", (f) => FIPS[f.id] === state.selected);
    const chosen = state.geo.states.find((f) => FIPS[f.id] === state.selected);
    d3.select("#selected-outline").attr("d", chosen ? state.geo.path(chosen) : null);
    renderLegend(values);
  }
  function label(code) {
    const cents = amountOf(code);
    if (cents === null) return "not loaded";
    return state.measure === "per_resident"
      ? `${perText(perResident(cents, yearData().states[code].population))} per resident` : money(cents);
  }
  function renderLegend(values) {
    if (!aggregate()) { $("legend").innerHTML = ""; $("map-note").textContent = "This spending type is not loaded for this year, so the map is blank rather than zero."; return; }
    const cuts = scale.quantiles();
    const fmt = state.measure === "per_resident" ? (v) => `$${d3.format(",.0f")(v)}` : (v) => shortMoney(v * 100);
    const anyNegative = Object.keys(yearData().states).some((c) => (value(c) ?? 0) < 0);
    $("legend").innerHTML = `
      <div class="legend-swatches">${RAMP.map((c) => `<span style="background:${c}"></span>`).join("")}</div>
      <div class="legend-labels">${cuts.map((c, i) => `<span style="left:${((i + 1) / 7 * 100).toFixed(2)}%">${fmt(c)}</span>`).join("")}</div>
      ${anyNegative ? `<span class="legend-negative"><i></i> Net de-obligation (more taken back than committed)</span>` : ""}
      ${Object.keys(yearData().states).some((c) => value(c) === 0) ? `<span class="legend-negative"><i style="background:${NONE};outline:1px solid var(--rule)"></i> None in this selection</span>` : ""}`;
    $("map-note").textContent = `Shading groups states into seven equal-count bands${state.measure === "per_resident" ? " per resident" : ""}. Alaska and Hawaii are not to scale.`;
  }
  function showTip(event, code, fromFocus) {
    const s = yearData().states[code], agg = aggregate();
    if (!s) return;
    const tip = $("tip"), box = $("map").getBoundingClientRect(), wrap = $("map").parentElement.getBoundingClientRect();
    let x, y;
    if (fromFocus) { const b = event.target.getBoundingClientRect(); x = b.left + b.width / 2 - wrap.left; y = b.top - wrap.top; }
    else { x = event.clientX - wrap.left; y = event.clientY - wrap.top; }
    tip.innerHTML = `<strong>${esc(s.name)}</strong><br>${esc(label(code))}${agg ? `, ${rankText(agg.places.get(code))}` : ""}`;
    tip.style.left = `${Math.max(80, Math.min(x, box.width - 80))}px`;
    tip.style.top = `${y}px`;
    tip.hidden = false;
  }
  function hideTip() { $("tip").hidden = true; }

  /* ---------- Headline ---------- */
  function renderLede() {
    const d = yearData(), agg = aggregate();
    const who = state.agency ? `${esc(agencyName())}` : "Federal agencies";
    if (!agg) {
      $("lede").innerHTML = `${esc(scopeDef().label)} is not loaded for fiscal year ${d.fiscal_year}.`;
      $("lede-sub").textContent = "Choose another year or spending type. Years and types that are not loaded are listed under Coverage.";
    } else {
      $("lede").innerHTML = state.agency && !agg.agencyPresent
        ? `${who} has no awards in ${esc(scopeLabel())} in fiscal year ${d.fiscal_year}.`
        : `${who} committed a net <span class="figure">${money(agg.national.amount)}</span> in ${esc(scopeLabel())} in fiscal year ${d.fiscal_year}.`;
      const loans = scopeCats().includes("loans") ? " Loans count at their subsidy cost to the government, not their face value." : "";
      $("lede-sub").textContent = `Select a state to see which agencies committed the money and who received it.${loans}`;
    }
    document.title = `Money Trail: ${scopeDef().label.toLowerCase()}, fiscal year ${d.fiscal_year}`;
  }

  /* ---------- Panel ---------- */
  /* Positive and negative contributions are listed apart: a net reduction (an agency taking back
     more than it committed) lowers the total, so it is shown under its own heading in red. */
  function splitBars(items, limit, noun) {
    const pos = items.filter((i) => i.amount_cents > 0), neg = items.filter((i) => i.amount_cents < 0).sort((a, b) => a.amount_cents - b.amount_cents);
    const max = d3.max(items, (i) => Math.abs(i.amount_cents)) || 1;
    const more = pos.length - limit;
    let html = bars(pos.slice(0, limit), max) + (more > 0 ? `<p class="note">And ${count(more)} more ${noun}${more === 1 ? "" : "s"} adding to the total.</p>` : "");
    if (neg.length) html += `<p class="note sub">Net reductions: these took back more than they committed here, so they lower the total.</p>${bars(neg.slice(0, limit), max)}${neg.length > limit ? `<p class="note">And ${count(neg.length - limit)} more.</p>` : ""}`;
    return html;
  }
  function bars(items, scaleMax) {
    const max = scaleMax || d3.max(items, (i) => Math.abs(i.amount_cents)) || 1;
    return `<ul class="bars">${items.map((i) => `
      <li><span>${esc(titleCase(i.name))}</span>
        <span class="amount${i.amount_cents < 0 ? " negative" : ""}">${money(i.amount_cents)}</span>
        <span class="bar" aria-hidden="true"><span class="${i.amount_cents < 0 ? "negative" : ""}" style="width:${(100 * Math.abs(i.amount_cents) / max).toFixed(1)}%"></span></span>
      </li>`).join("")}</ul>`;
  }
  function agencyBars(t, limit = 8) {
    const d = yearData();
    const items = [...t.agencies.entries()].filter(([, v]) => v !== 0).sort((a, b) => b[1] - a[1])
      .map(([i, v]) => ({ name: d.agencies[i].name, amount_cents: v }));
    return splitBars(items, limit, "agency").replace(/agencys/g, "agencies");
  }
  function categoryBars(t) {
    const d = yearData();
    const items = [...t.cats.entries()].filter(([, v]) => v !== 0).sort((a, b) => b[1] - a[1])
      .map(([i, v]) => ({ name: d.categories[d.category_ids[i]].label, amount_cents: v }));
    return items.length > 1 ? `<h3>By type of spending</h3>${splitBars(items, 10, "type")}` : "";
  }
  function scopeNotes(t) {
    const notes = [];
    if (t.missing) notes.push(`${count(t.missing)} transaction${t.missing === 1 ? " has" : "s have"} no amount in the source and count as zero, as on USAspending.`);
    if (scopeCats().includes("loans") && t.face) notes.push(`Loans here count at their subsidy cost; their face value is ${money(t.face)}, which is not added to any total.`);
    return notes.map((n) => `<p class="note">${n}</p>`).join("");
  }
  function sparkline(code) {
    // code null = the national all-places total, always in dollars.
    const national = code === null;
    const points = state.years.filter(comparable)
      .map((y) => ({ y, v: national ? aggregate(y).national.amount : amountOf(code, y) }))
      .filter((p) => national || state.measure !== "per_resident" || yearData(p.y).states[code].population);  // no invented zeros
    if (points.length < 2) return "";
    const w = 260, h = 64, pad = 18;
    const x = d3.scalePoint().domain(points.map((p) => p.y)).range([pad, w - pad]);
    const yv = points.map((p) => !national && state.measure === "per_resident" ? perResident(p.v, yearData(p.y).states[code].population) : dollars(p.v));
    const y = d3.scaleLinear().domain([Math.min(0, d3.min(yv)), d3.max(yv)]).nice().range([h - 16, 6]);
    const line = d3.line().x((p, i) => x(points[i].y)).y((p, i) => y(yv[i]));
    const current = points.findIndex((p) => p.y === state.year);
    return `<svg class="spark" width="${w}" height="${h}" role="img" aria-label="${esc(scopeDef().label)} by fiscal year, ${points[0].y} to ${points[points.length - 1].y}">
      <path d="${line(points)}"></path>
      ${current >= 0 ? `<circle cx="${x(state.year)}" cy="${y(yv[current])}" r="3.5"></circle>` : ""}
      <text x="${x(points[0].y)}" y="${h - 2}" text-anchor="middle">${points[0].y}</text>
      <text x="${x(points[points.length - 1].y)}" y="${h - 2}" text-anchor="middle">${points[points.length - 1].y}</text>
    </svg><p class="note">${points.map((p, i) => `FY${p.y} ${!national && state.measure === "per_resident" ? perText(yv[i]) + " per resident" : money(p.v)}`).join("; ")}. Years loaded for this same selection, nominal dollars.</p>`;
  }
  function recipientList(code) {
    const def = scopeDef(), s = yearData().states[code];
    const kinds = def.id === "all" ? scopeKinds() : def.id === "contracts-all" ? ["contracts"] : def.id === "assistance-all" ? ["assistance"] : [];
    if (state.agency || !kinds.length) {
      return `<h3>Largest recipients</h3><p class="note">Recipient rankings are kept for all agencies and for contracts or financial assistance as a whole, so they are not shown for this selection.</p>`;
    }
    return kinds.map((kind) => {
      const k = s.by_kind[kind];
      if (!k) return "";
      const nr = k.recipients_not_ranked;
      return `<h3>Largest ${kind === "contracts" ? "contract" : "assistance"} recipients</h3>
        <ol class="recipients">${k.recipients.slice(0, 6).map((r) => `
          <li><div class="row"><span class="name">${esc(titleCase(r.name))}</span><span class="figure${r.amount_cents < 0 ? " negative" : ""}">${money(r.amount_cents)}</span></div>
            <div class="meta">${count(r.awards)} award${r.awards === 1 ? "" : "s"}${r.parent && r.parent !== r.name ? `; part of ${esc(titleCase(r.parent))}` : ""}${r.example ? `. Largest: ${esc(sentence(r.example))}` : ""}</div></li>`).join("")}</ol>
        ${nr && nr.transactions ? `<p class="note">Not ranked: ${money(nr.amount_cents)} in ${count(nr.transactions)} transactions that are not one identifiable recipient${nr.aggregate_records || nr.redacted_records ? ` (${[nr.aggregate_records ? `${count(nr.aggregate_records)} aggregate records` : "", nr.redacted_records ? `${count(nr.redacted_records)} redacted for privacy` : ""].filter(Boolean).join(", ")})` : ""}.</p>` : ""}`;
    }).join("") + `<p class="note">Recipients are grouped by their federal entity ID, never by name.</p>`;
  }
  function reconciliationLines(code) {
    const s = yearData().states[code], cats = scopeCats();
    const lines = cats.map((c) => {
      const r = s.reconciliation[c];
      const name = yearData().categories[c].label;
      if (!r) return `<li>${esc(name)}: no published state figure to compare with.</li>`;
      if (r.official_cents == null || r.ours_cents == null) return `<li>${esc(name)}: could not be compared; a figure is missing.</li>`;
      if (r.official_cents === 0) return `<li>${esc(name)}: USAspending publishes $0; this project finds ${money(r.ours_cents)}.</li>`;
      return `<li>${esc(name)}: ${money(r.ours_cents)} here, ${money(r.official_cents)} on USAspending (${d3.format("+.3f")(r.gap_pct)}%, ${Math.abs(r.gap_pct) <= 1 ? "within" : "outside"} 1%).</li>`;
    });
    return `<div class="check"><p>Compared with USAspending's published state figures, all agencies:</p><ul>${lines.join("")}</ul></div>`;
  }
  function changesBlock(code) {
    const s = yearData().states[code], kd = yearData().kinds.contracts, c = s.changes;
    // These counts are for all agencies and all contracts and IDVs, so they appear only for that selection or all spending.
    if (!c || !kd || !kd.loaded || !kd.changes || state.agency || !["all", "contracts-all"].includes(state.scope)) return "";
    const meta = kd.changes;
    return `<h3>Changed in Treasury's latest monthly contracts release (all agencies, contracts and IDVs)</h3>
      <p class="note">The ${esc(meta.snapshot_date || "latest")} release added or updated ${count(c.added_or_updated_rows)} dated contract transactions in this state and year${c.flagged_correction_rows ? `, ${count(c.flagged_correction_rows)} of them flagged as corrections` : ""}. ${c.deobligation_rows ? `${count(c.deobligation_rows)} of these take money back (${money(c.deobligation_cents)}).` : ""} The file cannot separate new transactions from updated ones.</p>`;
  }

  function renderPanel() {
    const d = yearData(), agg = aggregate(), panel = $("panel");
    if (!agg) { panel.innerHTML = `<h2>Not loaded</h2><p class="note">${esc(scopeDef().label)} is not loaded for fiscal year ${d.fiscal_year}.</p>`; return; }
    if (!state.selected) {
      const n = agg.national;
      panel.innerHTML = `
        <h2>United States</h2>
        <p class="state-figure figure${n.amount < 0 ? " negative" : ""}">${money(n.amount)}</p>
        <p class="facts">${count(n.tx)} transaction${n.tx === 1 ? "" : "s"}, fiscal year ${d.fiscal_year}, all places including those not on the map${state.agency ? `, ${esc(agencyName())} only` : ""}.</p>
        ${sparkline(null)}
        ${scopeNotes(n)}
        ${categoryBars(n)}
        ${state.agency ? "" : `<h3>Largest agencies nationally</h3>${agencyBars(n)}`}
        <p class="note hint">Select a state on the map or in the table for its agencies and recipients.</p>`;
      return;
    }
    const code = state.selected, s = d.states[code], t = agg.places.get(code);
    const prev = comparable(state.year - 1) ? amountOf(code, state.year - 1) : null;
    const per = perResident(t.amount, s.population);
    const change = prev ? (t.amount - prev) / Math.abs(prev) : null;
    let share = "";
    if (state.agency) {
      const whole = aggregate(state.year, null).places.get(code).amount;
      share = whole ? `<p class="note">That is ${pct(t.amount / whole)} of the ${money(whole)} all agencies committed here in this spending type.</p>` : "";
    }
    // Money taken back is recorded per kind of award, so it is shown only for whole kinds and all agencies.
    const wholeKinds = !state.agency && ["all", "contracts-all", "assistance-all"].includes(state.scope);
    const deob = scopeKinds().map((k) => s.by_kind[k]).filter(Boolean).reduce((sum, k) => sum + (k.deobligations_cents || 0), 0);
    panel.innerHTML = `
      <h2>${esc(s.name)}</h2>
      <p class="state-figure figure${t.amount < 0 ? " negative" : ""}">${money(t.amount)}</p>
      <p class="facts">${rankText(t)}${per === null || !t.tx ? "" : `, ${perText(per)} per resident`}${change === null ? "" : `, ${change >= 0 ? "up" : "down"} ${pct(Math.abs(change))} on ${state.year - 1}`}. ${count(t.tx)} transaction${t.tx === 1 ? "" : "s"}.</p>
      ${share}
      ${sparkline(code)}
      ${scopeNotes(t)}
      ${wholeKinds && deob ? `<p class="note">Includes ${money(Math.abs(deob))} taken back on earlier commitments.</p>` : ""}
      ${categoryBars(t)}
      ${state.agency ? "" : `<h3>Awarding agencies</h3>${agencyBars(t, 6)}`}
      ${recipientList(code)}
      ${changesBlock(code)}
      ${reconciliationLines(code)}
      <p class="note"><a href="https://www.usaspending.gov/state/${s.name.toLowerCase().replace(/ /g, "-")}/${state.year}">${esc(s.name)} on USAspending.gov</a></p>
      <button type="button" class="clear" id="clear">Back to the national view</button>`;
    $("clear").addEventListener("click", () => select(null));
  }

  /* ---------- Outside the map ---------- */
  function renderOutside() {
    const agg = aggregate();
    if (!agg) { $("outside").innerHTML = `<p class="note">Not loaded for this year.</p>`; return; }
    const terr = [...agg.territories.entries()].sort((a, b) => b[1].amount - a[1].amount)
      .slice(0, 4).map(([c, t]) => `${esc(TERRITORY_NAMES[c] || c)} ${money(t.amount)}`).join("; ");
    const territoryTotal = [...agg.territories.values()].reduce((s, t) => s + t.amount, 0);
    const items = [
      ["U.S. territories", territoryTotal, terr || "None recorded"],
      ["Outside the United States", agg.foreign.amount, "Work performed abroad, including Micronesia, the Marshall Islands and Palau (freely associated states, counted as foreign as Treasury does)"],
      ["Not assigned to a mapped place", agg.unknown.amount, "No usable place of performance in the record, or a state code that is not valid"],
    ];
    $("outside").innerHTML = items.map(([name, cents, detail]) => `
      <div class="item"><div class="label">${name}</div>
        <div class="value figure${cents < 0 ? " negative" : ""}">${money(cents)}</div>
        <div class="detail">${detail}</div></div>`).join("");
  }

  /* ---------- Table ---------- */
  function renderTable() {
    const d = yearData(), agg = aggregate();
    const body = $("state-table").querySelector("tbody");
    $("col-amount").textContent = state.agency ? `${agencyName()}` : "Amount";
    $("col-agency").textContent = state.agency ? "Share of state's total" : "Largest agency";
    if (!agg) { body.innerHTML = `<tr><td colspan="5">Not loaded for fiscal year ${d.fiscal_year}.</td></tr>`; return; }
    const whole = state.agency ? aggregate(state.year, null) : null;
    const prevOk = comparable(state.year - 1);
    const rows = Object.entries(d.states).map(([code, s]) => {
      const t = agg.places.get(code);
      const prev = prevOk ? amountOf(code, state.year - 1) : null;
      let top = "";
      if (state.agency) { const w = whole.places.get(code).amount; top = w ? pct(t.amount / w) : ""; }
      else { const best = [...t.agencies.entries()].sort((a, b) => b[1] - a[1])[0]; top = best ? titleCase(d.agencies[best[0]].name) : ""; }
      return { code, name: s.name, amount: t.amount, per: perResident(t.amount, s.population),
               change: prev ? (t.amount - prev) / Math.abs(prev) : null, agency: top };
    });
    document.querySelectorAll('#state-table [data-col="change"]').forEach((el) => { el.hidden = !prevOk; });
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
    body.innerHTML = rows.map((r) => `
      <tr data-code="${r.code}" class="${r.code === state.selected ? "selected" : ""}">
        <td><a href="${hashFor({ state: r.code })}">${esc(r.name)}</a></td>
        <td class="num${r.amount < 0 ? " negative" : ""}">${money(r.amount)}</td>
        <td class="num">${perText(r.per)}</td>
        <td class="num" data-col="change"${prevOk ? "" : " hidden"}>${r.change === null ? "" : `${r.change >= 0 ? "+" : "−"}${pct(Math.abs(r.change))}`}</td>
        <td>${esc(r.agency)}</td>
      </tr>`).join("");
  }

  /* ---------- Coverage and about ---------- */
  const STATUS = { passed: "passed", gaps: "did not pass", incomplete: "incomplete", "no official figure returned": "no official figure returned" };
  function renderCoverage() {
    const kindName = { contracts: "Contracts", assistance: "Financial assistance" };
    const rows = state.index.years.slice().reverse().map((y) => `
      <tr><th scope="row">${y.fiscal_year}</th>${["contracts", "assistance"].map((k) => {
        const v = y.kinds[k];
        if (!v || !v.loaded) return `<td>Not loaded</td>`;
        const statuses = v.reconciliation ? Object.entries(v.reconciliation).map(([c, st]) => `${esc(yearData(y.fiscal_year).categories[c].label)}: ${esc(STATUS[st] || st)}`).join("; ") : "not yet compared";
        return `<td><span class="figure">${money(v.amount_cents)}</span><br><span class="note">Snapshot ${esc(v.snapshot_date)}. Check: ${statuses}.</span></td>`;
      }).join("")}</tr>`).join("");
    $("coverage").innerHTML = `
      <table class="coverage"><thead><tr><th scope="col">Fiscal year</th><th scope="col">${kindName.contracts}</th><th scope="col">${kindName.assistance}</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="note">${esc(state.index.not_offered || "")} A year or type marked not loaded is left blank on the map, never shown as zero.</p>`;
  }
  function renderAbout() {
    const d = yearData(), parts = [];
    for (const kind of ["contracts", "assistance"]) {
      const k = d.kinds[kind];
      if (!k.loaded) { parts.push(`<li>${kind === "contracts" ? "Contracts" : "Financial assistance"}: not loaded for FY${d.fiscal_year}.</li>`); continue; }
      if (!k.reconciliation) { parts.push(`<li>${kind === "contracts" ? "Contracts" : "Financial assistance"}: not yet compared.</li>`); continue; }
      for (const [cat, c] of Object.entries(k.reconciliation.categories)) {
        const name = d.categories[cat].label;
        if (c.status === "no official figure returned") { parts.push(`<li>${esc(name)}: USAspending returned no state figures for this year, so there is nothing to compare; this project's ${money(c.total_ours_cents)} in the 51 places stands unchecked.</li>`); continue; }
        const compared = 51 - new Set([...(c.missing_ours || []), ...(c.missing_official || [])]).size;
        parts.push(`<li>${esc(name)}: check <strong>${STATUS[c.status] || esc(c.status)}</strong>. Over ${compared} of 51 places, ${money(c.total_ours_cents)} here against ${money(c.total_official_cents)} (${c.total_gap_pct === null ? "no percentage" : d3.format("+.3f")(c.total_gap_pct) + "%"}); ${c.states_within_1_pct} places within 1%.</li>`);
      }
    }
    $("reconciliation").innerHTML = `<p>Each type of spending is compared state by state with USAspending's own published figures for FY${d.fiscal_year}, using the same dates, place and award types. A check passes only if all 51 places are present, the national gap is within 0.1% and every place is within 1%. USAspending's figures are live, so records changed after the archive was made can open small gaps.</p><ul>${parts.join("")}</ul>`;
    for (const kind of ["contracts", "assistance"]) {
      const rec = d.kinds[kind].loaded && d.kinds[kind].reconciliation;
      if (!rec) continue;
      const kindName = kind === "contracts" ? "contracts" : "financial assistance";
      if (rec.agency_status === "unavailable") { $("reconciliation").insertAdjacentHTML("beforeend", `<p>No agency comparison for ${kindName}: ${esc(rec.agency_scope_note || "no official figures were returned")}.</p>`); continue; }
      const compared = (rec.agencies || []).filter((a) => a.gap_cents !== null);
      const off = compared.filter((a) => Math.abs(a.gap_cents) >= 100 * 1e6).sort((a, b) => Math.abs(b.gap_cents) - Math.abs(a.gap_cents));
      if (!compared.length) continue;
      const unanswered = Object.entries(rec.categories).filter(([, c]) => c.status === "no official figure returned").map(([c]) => d.categories[c].label);
      $("reconciliation").insertAdjacentHTML("beforeend", `<p>Agency by agency over the same 51 places (${kindName}${unanswered.length ? `, leaving out ${esc(unanswered.join(", ").toLowerCase())}, which has no published figure` : ""}): ${off.length ? `${compared.length - off.length} of the ${compared.length} agencies compared are within $1 million of USAspending. ${off.length > 4 ? "The largest differences are" : `The difference${off.length === 1 ? " is" : "s are"}`} in ${off.slice(0, 4).map((a) => `${esc(titleCase(a.agency))} (${money(a.gap_cents)})`).join(", ")}${off.length > 4 ? `, and ${off.length - 4} more agencies differ by $1 million or more` : ""}.` : `All ${compared.length} agencies compared are within $1 million of USAspending.`}</p>`);
    }
    for (const item of d.disclosures || []) {
      $("reconciliation").insertAdjacentHTML("beforeend", `<p><strong>${esc(d.categories[item.category].label)}:</strong> ${esc(item.text)} <span class="note">Finding of ${esc(item.observed)}; evidence: ${esc(item.evidence)}.</span></p>`);
    }
    if ((d.historical_disclosures || []).length) {
      $("reconciliation").insertAdjacentHTML("beforeend", `<p class="note">Earlier findings recorded for a different snapshot or comparison of FY${d.fiscal_year} are kept in the repository (pipeline/disclosures.json); they do not describe the figures shown.</p>`);
    }
    const ck = d.kinds.contracts;
    const batches = ck.loaded ? (ck.deleted_key_batches || []).filter((b) => b.match === "twin" && Math.abs(b.state_contract_cents) >= 100 * 1e6 * 100) : [];
    if (batches.length) {
      const fmtTime = (t) => t ? t.replace(/\+00$/, " UTC") : "no surviving copy";
      $("reconciliation").insertAdjacentHTML("beforeend", `<p>Treasury re-issued the keys of many Defense transactions in early November 2026, and the contracts archive holds both the old and the new copy of each. Grouped by when the new copy was written, the duplicated contract amounts in the 50 states and DC are: ${batches.map((b) => `${esc(fmtTime(b.twin_last_modified))}, ${money(b.state_contract_cents)} (${count(b.state_contract_rows)} rows)`).join("; ")}. The figures on this page still count both copies.</p>`);
    }
    const sources = [];
    for (const kind of ["contracts", "assistance"]) {
      const k = d.kinds[kind];
      if (!k.loaded) continue;
      const m = k.deleted_key_matches;
      const kept = m && m.rows && !m.applied ? ` It contains ${count(m.rows)} rows (${money(m.cents)}) whose keys the same date's delta file lists as deleted; they are counted, because removing them did not match USAspending's published totals either.` : "";
      const fips = k.unrecognised_state_fips && k.unrecognised_state_fips.length ? ` ${count(k.unrecognised_state_fips.reduce((s, f) => s + f.rows, 0))} rows carry a state code that is not a valid FIPS code and are counted as no location.` : "";
      const ch = k.changes;
      const undated = ch && ch.unknown_year_rows && ch.unknown_year_rows.length ? ` The ${esc(ch.source_file || "delta file")} has ${ch.unknown_year_rows.length} changed rows with no date or fiscal year: ${ch.unknown_year_rows.map((r) => `${esc(r.key)} (${esc(r.amount_raw || "no amount")}; ${esc(r.reason)}; ${esc(r.source_file)} row ${r.source_row})`).join("; ")}.` : "";
      const p = k.provenance, repo = "https://github.com/mintyfizz/money-trail";
      const links = p ? ` Original file preserved in <a href="${repo}/releases/tag/${encodeURIComponent(p.source_release)}">${esc(p.source_release)}</a>; typed data and results in <a href="${repo}/releases/tag/${encodeURIComponent(p.build_release)}">${esc(p.build_release)}</a>, built by commit <a href="${repo}/commit/${esc(p.code_commit)}">${esc(p.code_commit.slice(0, 7))}</a>${p.source_sha256 ? `; source SHA-256 ${esc(p.source_sha256)}` : ""}.` : " Release provenance not recorded for this file.";
      sources.push(`${kind === "contracts" ? "Contracts" : "Financial assistance"}: ${esc(k.source_zip)}, snapshot ${esc(k.snapshot_date)}, summarised ${esc(k.generated_at.slice(0, 10))}.${links}${kept}${fips}${undated}`);
    }
    $("source").innerHTML = `Source: Treasury's national award archives for fiscal year ${d.fiscal_year}. ${sources.join(" ")}`;
  }

  /* ---------- Controls ---------- */
  function renderYearButtons() {
    $("year-buttons").innerHTML = state.years.map((y) => `<button type="button" data-year="${y}" aria-pressed="${y === state.year}">${y}</button>`).join("");
    $("play").hidden = state.years.length < 2;
  }
  function renderScopeSelect() {
    const groups = {};
    const option = (s) => {
      const ok = scopeCats(state.year, s.id) !== null;
      return `<option value="${s.id}"${s.id === state.scope ? " selected" : ""}${ok ? "" : " disabled"}>${esc(s.label)}${ok ? "" : " (not loaded)"}</option>`;
    };
    const top = SCOPES.filter((s) => !s.group).map(option).join("");
    SCOPES.filter((s) => s.group).forEach((s) => { (groups[s.group] = groups[s.group] || []).push(option(s)); });
    $("scope").innerHTML = top + Object.entries(groups).map(([g, o]) => `<optgroup label="${esc(g)}">${o.join("")}</optgroup>`).join("");
  }
  function renderAgencySelect() {
    const d = yearData(), filter = ($("agency-search").value || "").trim().toLowerCase();
    const agg = aggregate(state.year, null);
    const totals = agg ? agg.national.agencies : new Map();
    const list = d.agencies.map((a, i) => ({ id: a.id, name: titleCase(a.name), amount: totals.get(i) }))
      .filter((a) => a.amount !== undefined)
      .sort((a, b) => b.amount - a.amount);
    if (state.agency && !list.some((a) => a.id === state.agency)) list.unshift({ id: state.agency, name: agencyName(), amount: null });
    const shown = list.filter((a) => !filter || a.name.toLowerCase().includes(filter) || a.id === state.agency);
    $("agency").innerHTML = `<option value="">All agencies</option>` + shown.map((a) =>
      `<option value="${esc(a.id)}"${a.id === state.agency ? " selected" : ""}>${esc(a.name)}${a.amount === null ? " (no awards in this selection)" : ` — ${shortMoney(a.amount)}`}</option>`).join("");
    $("agency-count").textContent = filter ? `${shown.length} of ${list.length} agencies match` : `${list.length} agencies`;
  }
  function measureControls() {
    const per = document.querySelector('input[name="measure"][value="per_resident"]');
    const ok = hasPopulation();
    per.disabled = !ok;
    per.parentElement.classList.toggle("disabled", !ok);
    per.parentElement.title = ok ? "" : "Population figures are not available for this year";
    if (!ok && state.measure === "per_resident") { state.measure = "total"; document.querySelector('input[name="measure"][value="total"]').checked = true; }
  }

  function hashFor(over = {}) {
    const v = { fy: state.year, scope: state.scope, agency: state.agency, state: state.selected, ...over };
    const p = new URLSearchParams();
    p.set("fy", v.fy);
    if (v.scope !== "all") p.set("scope", v.scope);
    if (v.agency) p.set("agency", v.agency);
    if (v.state) p.set("state", v.state);
    if (state.measure !== "total") p.set("measure", state.measure);
    return `#${p.toString()}`;
  }
  /* Intentional changes add a history entry; the first render, playback and back/forward replace it. */
  function navigate(change) {
    Object.assign(state, change);
    render("push");
  }
  function render(mode = "replace") {
    measureControls();
    renderYearButtons();
    renderScopeSelect();
    renderAgencySelect();
    renderLede();
    renderMap();
    renderPanel();
    renderOutside();
    renderTable();
    renderCoverage();
    renderAbout();
    const hash = hashFor();
    if (location.hash !== hash) {
      if (mode === "push") history.pushState(null, "", hash); else history.replaceState(null, "", hash);
    }
  }
  function select(code) {
    navigate({ selected: code });
    if (code && window.matchMedia("(max-width: 900px)").matches) $("panel").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
  }
  function setYear(year) { state.year = year; render(); }
  function stopPlay() { clearInterval(state.timer); state.timer = null; $("play").setAttribute("aria-pressed", "false"); $("play").textContent = "Play years"; }

  function bind() {
    // The skip link moves focus to the table without touching the address, so filters stay as they are.
    document.querySelector(".skip").addEventListener("click", (e) => {
      e.preventDefault();
      const target = $("table-title");
      target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      target.focus({ preventScroll: true });
    });
    $("year-buttons").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-year]"); if (!b) return;
      stopPlay(); navigate({ year: +b.dataset.year });
    });
    $("play").addEventListener("click", () => {
      if (state.timer) return stopPlay();
      $("play").setAttribute("aria-pressed", "true"); $("play").textContent = "Stop";
      let i = 0; setYear(state.years[0]);
      state.timer = setInterval(() => { i += 1; if (i >= state.years.length) return stopPlay(); setYear(state.years[i]); }, 1600);
    });
    $("scope").addEventListener("change", () => navigate({ scope: $("scope").value }));
    $("agency").addEventListener("change", () => navigate({ agency: $("agency").value || null }));
    $("agency-search").addEventListener("input", renderAgencySelect);
    document.querySelectorAll('input[name="measure"]').forEach((input) =>
      input.addEventListener("change", () => navigate({ measure: input.value })));
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
    state.year = state.years.includes(fy) ? fy : state.years[state.years.length - 1];
    const scope = params.get("scope");
    state.scope = scope && SCOPES.some((s) => s.id === scope) ? scope : "all";
    const agency = params.get("agency");
    // An agency that appears in no loaded year is not a selection; ignore it rather than show a fake zero.
    state.agency = agency && state.years.some((y) => yearData(y).agencies.some((a) => a.id === agency)) ? agency : null;
    state.selected = code && yearData().states[code] ? code : null;
    state.measure = params.get("measure") === "per_resident" ? "per_resident" : "total";
    const radio = document.querySelector(`input[name="measure"][value="${state.measure}"]`);
    if (radio) radio.checked = true;
  }

  async function start() {
    try {
      const [index, topo] = await Promise.all([d3.json("data/index.json"), d3.json("vendor/states-albers-10m.json")]);
      state.index = index;
      state.years = index.years.map((y) => y.fiscal_year).sort();
      const files = await Promise.all(state.years.map((y) => d3.json(`data/fy${y}.json`)));
      files.forEach((f) => { state.data[f.fiscal_year] = f; });
      state.year = state.years[state.years.length - 1];
      readHash();
      drawMap(topo);
      bind();
      render();
      // Links, the back button and edited addresses all go through the same path.
      const restore = () => {
        // Only an address that carries the page's own state changes it; a plain in-page anchor does not.
        if (!/(^#|&)fy=/.test(location.hash)) { history.replaceState(null, "", hashFor()); return; }
        stopPlay(); readHash(); render();
      };
      window.addEventListener("popstate", restore);
      window.addEventListener("hashchange", restore);
    } catch (err) {
      $("lede").innerHTML = `<span class="error">The data files did not load (${esc(err.message)}). If you opened this page from disk, serve the folder over HTTP instead.</span>`;
      console.error(err);
    }
  }
  start();
})();
