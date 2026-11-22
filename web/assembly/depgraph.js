/* Component dependencies within the part inspector. */
window.PartDependencyGraph = (() => {
  const namespace = "http://www.w3.org/2000/svg";
  const order = ["assembly", "part", "stock", "subpart", "supplier", "risk", "shipment"];
  const columnNames = {assembly: "Part of", part: "Part", stock: "On hand", subpart: "Contains", supplier: "Made by",
    risk: "Risks", shipment: "Deliveries"};
  const kinds = {assembly: "Assembly", part: "Selected part", stock: "Warehouse stock", subpart: "Sub-part", supplier: "Supplier",
    risk: "Supply risk", shipment: "Delivery"};
  const icons = {assembly: "pack.png", part: "pack.png", stock: "receive.png", subpart: "pack.png",
    supplier: "order.png", risk: "confirm.png", shipment: "ship.png"};
  const statuses = {ok: "OK", at_risk: "At risk", blocked: "Blocked", verify: "Check record", info: "No effect on launch"};
  const observers = new WeakMap();
  const openByPart = new Map();
  const scrollByPart = new Map();
  const cardHeight = 53;
  let nextId = 0;
  const cache = new Map();  // componentId -> Promise<graph>, warmed in the background on page load

  function load(componentId, fresh = false) {
    if (!fresh && cache.has(componentId)) return cache.get(componentId);
    const request = fetch(`/api/parts/${encodeURIComponent(componentId)}/graph`, {cache: "no-store"}).then(response => {
      if (!response.ok) throw new Error(`Graph request failed (${response.status})`);
      return response.json();
    });
    const previous = cache.get(componentId);
    // Keep showing the last good graph while a refresh is in flight or if it fails.
    cache.set(componentId, previous ? request.catch(() => previous) : request);
    request.catch(() => { if (!previous) cache.delete(componentId); });
    return cache.get(componentId);
  }

  function prefetch(componentIds) {
    for (const id of componentIds) load(id, true).catch(() => {});
  }

  function svgElement(tag, attributes = {}, content) {
    const element = document.createElementNS(namespace, tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
    if (content != null) element.textContent = content;
    return element;
  }

  function wrap(text, width, context, maxLines = 2) {
    const lines = [""];
    for (const word of String(text).split(/\s+/)) {
      const line = lines.at(-1);
      const combined = line ? `${line} ${word}` : word;
      if (line && context.measureText(combined).width > width && lines.length < maxLines) lines.push(word);
      else lines[lines.length - 1] = combined;
    }
    return lines.map(line => {
      if (context.measureText(line).width <= width) return line;
      while (line && context.measureText(line + "…").width > width) line = line.slice(0, -1);
      return line.trimEnd() + "…";
    });
  }

  function detailRows(node) {
    // The status line already shows the meta text; don't repeat it as the first sentence.
    const meta = String(node.meta ?? node.subtitle ?? "").trim().replace(/\.$/, "");
    let detail = String(node.detail ?? "").trim();
    if (meta && detail.toLowerCase().startsWith(meta.toLowerCase() + ".")) detail = detail.slice(meta.length + 1).trim();
    const sources = node.sources ?? (node.evidence ? [node.evidence.split("/").at(-1)] : []);
    return [
      ["Work in progress", node.doing],
      ["Contact", node.contact],
      ["Details", detail],
      ["Supplier quote", node.quote && `“${node.quote}”`],
      ["Source", sources.join(", ")],
      ["Reference", node.reference],
    ].filter(([, value]) => value);
  }

  function layout(graph, viewportWidth) {
    const layers = order.map(kind => graph.nodes.filter(node => node.kind === kind)).filter(layer => layer.length);
    const positions = new Map();
    layers.forEach((layer, column) => layer.forEach(node => positions.set(node.id, {node, column})));
    const connections = graph.edges.filter(edge => positions.has(edge.source) && positions.has(edge.target)).map(edge => {
      const source = positions.get(edge.source);
      const target = positions.get(edge.target);
      const reversed = source.column > target.column;
      return {edge, from: reversed ? target : source, to: reversed ? source : target, reversed};
    });

    const margin = 5;
    const gap = 24;
    const width = Math.max(viewportWidth, layers.length * 146 + (layers.length - 1) * gap + margin * 2);
    const cardWidth = Math.min(252, Math.floor((width - margin * 2 - (layers.length - 1) * gap) / layers.length));
    const step = cardWidth + gap;
    const top = 28;
    const rowStep = cardHeight + 7;
    const rows = Math.max(...layers.map(layer => layer.length));
    const rowOf = new Map();
    layers.forEach((layer, column) => {
      if (column > 0) {
        const score = node => {
          const near = connections.filter(c => (c.to.node === node && c.from.column < column) || (c.from.node === node && c.to.column < column))
            .map(c => rowOf.get((c.to.node === node ? c.from : c.to).node.id)).filter(v => v != null);
          return near.length ? near.reduce((a, b) => a + b, 0) / near.length : Infinity;
        };
        const scored = layer.map((node, i) => ({node, i, s: score(node)}));
        scored.sort((a, b) => a.s - b.s || a.i - b.i);
        layer.splice(0, layer.length, ...scored.map(x => x.node));
      }
      layer.forEach((node, index) => rowOf.set(node.id, index + (rows - layer.length) / 2));
    });
    let bottom = 0;
    const headers = [];
    for (const [column, layer] of layers.entries()) {
      const x = margin + column * step;
      headers.push({x, y: 13, text: columnNames[layer[0].kind]});
      layer.forEach((node, index) => {
        const position = positions.get(node.id);
        const y = top + (index + (rows - layer.length) / 2) * rowStep;
        Object.assign(position, {x, y, width: cardWidth});
        bottom = Math.max(bottom, y + cardHeight);
      });
    }
    return {positions, connections, width, height: bottom + 5, headers};
  }

  function wire(connection, index) {
    const {from, to} = connection;
    const x1 = from.x + from.width;
    const y1 = from.y + cardHeight / 2;
    const y2 = to.y + cardHeight / 2;
    if (to.column - from.column > 1) {
      const rail = 19 + (index % 3) * 3;
      return `M ${x1} ${y1} H ${x1 + 8} V ${rail} H ${to.x - 8} V ${y2} H ${to.x}`;
    }
    const x = (x1 + to.x) / 2;
    return `M ${x1} ${y1} C ${x} ${y1} ${x} ${y2} ${to.x} ${y2}`;
  }

  function render(svg, section, activate, openId, geometry) {
    const focusId = svg.contains(document.activeElement) ? document.activeElement.dataset.node : null;
    const font = getComputedStyle(section).fontFamily;
    const nameFont = `700 13.5px ${font}`;
    const metaFont = `700 11px ${font}`;
    const context = document.createElement("canvas").getContext("2d");
    const {positions, connections, width, height, headers} = geometry;
    svg.dataset.direction = "horizontal";
    svg.style.width = `${width}px`;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

    const markerId = `${section.id}-arrow`;
    const defs = svgElement("defs");
    const marker = svgElement("marker", {id: markerId, viewBox: "0 0 7 8", refX: 7, refY: 4,
      markerWidth: 7, markerHeight: 8, markerUnits: "userSpaceOnUse", orient: "auto-start-reverse"});
    marker.append(svgElement("path", {d: "M 0 0 L 7 4 L 0 8 Z"}));
    defs.append(marker);
    svg.replaceChildren(defs);

    const headings = svgElement("g", {class: "depgraph-columns", "aria-hidden": "true"});
    for (const header of headers) headings.append(svgElement("text", {x: header.x + 2, y: header.y}, header.text));
    svg.append(headings);

    const lines = svgElement("g", {class: "depgraph-wires", "aria-hidden": "true"});
    const relations = new Map();
    for (const [i, connection] of connections.entries()) {
      const {edge, reversed} = connection;
      const source = positions.get(edge.source).node;
      const target = positions.get(edge.target).node;
      const description = `${source.label} ${edge.label} ${target.label}`;
      for (const node of [source, target]) {
        if (!relations.has(node.id)) relations.set(node.id, []);
        relations.get(node.id).push(description);
      }
      const attributes = {d: wire(connection, i), [reversed ? "marker-start" : "marker-end"]: `url(#${markerId})`};
      if (connection.to.column - connection.from.column > 1) attributes.class = "depgraph-long-wire";
      lines.append(svgElement("path", attributes));
    }
    svg.append(lines);

    for (const {node, x, y, width: cardWidth} of positions.values()) {
      const isOpen = openId === node.id;
      const meta = node.meta ?? node.subtitle ?? kinds[node.kind];
      const status = statuses[node.status] ?? "";
      const hasDetails = node.rows.length > 0;
      const followsPart = ["assembly", "subpart"].includes(node.kind);
      const clickable = hasDetails || node.mapAvailable || followsPart;
      const controls = hasDetails && !followsPart ? {
        "aria-controls": `${section.id}-evidence`, "aria-expanded": String(isOpen),
      } : {};
      const card = svgElement("g", {class: "depgraph-node", "data-node": node.id, "data-kind": node.kind,
        "data-status": node.status, "data-selected": node.selected ? "true" : "false", "data-open": String(isOpen),
        transform: `translate(${x},${y})`, ...(clickable ? {tabindex: "0", role: "button", ...controls} : {role: "group"}),
        "aria-label": [node.label, meta, !["part", "stock"].includes(node.kind) && status,
          node.mapAvailable && (node.selected ? "Hide map" : "Show map")].filter(Boolean).join(". "),
        "aria-description": relations.get(node.id)?.join(". ") ?? ""});
      card.append(
        svgElement("title", {}, `${node.label} · ${meta}`),
        svgElement("rect", {class: "depgraph-card", width: cardWidth, height: cardHeight, rx: 7}),
        svgElement("image", {href: `/assembly/art/${node.image}`, x: 8, y: 12, width: 27, height: 29, "aria-hidden": "true"}),
      );

      context.font = nameFont;
      const nameLines = wrap(node.label, cardWidth - 53, context);
      for (const [i, line] of nameLines.entries()) {
        card.append(svgElement("text", {x: 42, y: (nameLines.length === 1 ? 22 : 15) + 14 * i, class: "depgraph-name"}, line));
      }
      context.font = metaFont;
      card.append(svgElement("text", {x: 42, y: 44, class: "depgraph-meta"},
        wrap(meta, cardWidth - (clickable ? 61 : 50), context, 1)[0]));
      if (clickable) {
        const indicator = isOpen || node.selected ? "−" : followsPart ? "›" : "+";
        card.append(svgElement("text", {x: cardWidth - 8, y: 44, "text-anchor": "end", class: "depgraph-more", "aria-hidden": "true"}, indicator));
        card.addEventListener("click", () => activate(node));
        card.addEventListener("keydown", event => {
          if (event.key === "Escape" && (isOpen || node.selected)) {
            event.preventDefault();
            event.stopPropagation();
            activate(node, "close");
          }
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          activate(node);
        });
      }
      svg.append(card);
      if (focusId === node.id) card.focus({preventScroll: true});
    }
  }

  function renderEvidence(panel, node, close) {
    panel.hidden = !node?.rows.length;
    panel.replaceChildren();
    if (panel.hidden) return;

    const image = document.createElement("img");
    image.src = `/assembly/art/${node.image}`;
    image.alt = "";
    const heading = document.createElement("h4");
    heading.id = `${panel.id}-heading`;
    heading.textContent = node.label;
    const meta = document.createElement("p");
    meta.className = "depgraph-evidence-meta";
    const description = node.meta ?? node.subtitle ?? kinds[node.kind];
    meta.textContent = node.selected ? `${description} · On map` : description;
    const name = document.createElement("div");
    name.append(heading, meta);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "text-button";
    button.textContent = "Close evidence";
    button.addEventListener("click", close);
    const title = document.createElement("div");
    title.className = "depgraph-evidence-title";
    title.append(image, name, button);
    const rows = document.createElement("dl");
    for (const [label, value] of node.rows) {
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = value;
      rows.append(term, description);
    }
    panel.setAttribute("aria-labelledby", heading.id);
    panel.append(title, rows);
  }

  async function mount(inspector, componentId, nameOf, selectPart, {
    trackedShipments = new Set(), selectedShipment, selectShipment, focusShipment, partImage,
  } = {}) {
    observers.get(inspector)?.disconnect();
    const section = document.createElement("section");
    section.className = "depgraph";
    section.id = `part-graph-${++nextId}`;
    const heading = document.createElement("h3");
    heading.id = `${section.id}-heading`;
    heading.textContent = "What this part depends on";
    section.setAttribute("aria-labelledby", heading.id);
    const verdict = document.createElement("p");
    verdict.className = "depgraph-verdict";
    const title = document.createElement("div");
    title.className = "depgraph-heading";
    title.append(heading, verdict);
    const viewport = document.createElement("div");
    viewport.className = "depgraph-viewport";
    viewport.setAttribute("aria-label", "Scrollable dependency graph");
    const scrollHint = document.createElement("p");
    scrollHint.className = "depgraph-scroll-hint";
    scrollHint.textContent = "Scroll sideways for more";
    scrollHint.hidden = true;
    const evidence = document.createElement("section");
    evidence.className = "depgraph-evidence";
    evidence.id = `${section.id}-evidence`;
    evidence.hidden = true;
    const feedback = document.createElement("p");
    feedback.className = "depgraph-feedback";
    feedback.setAttribute("role", "status");
    feedback.textContent = "Loading dependencies…";
    section.append(title, viewport, scrollHint, evidence, feedback);
    inspector.querySelector(".inspector-head").after(section);

    try {
      const graph = structuredClone(await load(componentId));
      if (!section.isConnected) return;
      for (const node of graph.nodes) {
        const isPart = ["assembly", "part", "subpart"].includes(node.kind);
        if (isPart) node.label = nameOf(node.id) ?? node.label;
        const artwork = isPart && partImage?.(node.id);
        node.image = artwork ? `${artwork}.webp` : icons[node.kind];
        node.rows = detailRows(node);
        node.mapAvailable = node.kind === "shipment" && trackedShipments.has(node.id);
        node.selected = node.mapAvailable && node.id === selectedShipment;
      }
      verdict.textContent = graph.verdict ?? "";
      if (graph.stock_status) verdict.dataset.status = graph.stock_status;
      verdict.hidden = !verdict.textContent;

      const svg = svgElement("svg", {role: "group", "aria-labelledby": heading.id});
      let openId = openByPart.get(componentId);
      let lastWidth = 0;
      let restoredScroll = false;
      const nodeControl = id => svg.querySelector(`[data-node="${CSS.escape(id)}"]`);
      const redraw = () => {
        const geometry = layout(graph, lastWidth);
        render(svg, section, activate, openId, geometry);
        const overflows = geometry.width > lastWidth;
        scrollHint.hidden = !overflows;
        if (overflows) viewport.tabIndex = 0;
        else viewport.removeAttribute("tabindex");
        if (!restoredScroll) {
          viewport.scrollLeft = scrollByPart.get(componentId) ?? 0;
          restoredScroll = true;
        }
        if (focusShipment && document.activeElement === document.body) {
          nodeControl(focusShipment)?.focus({preventScroll: true});
        }
        focusShipment = null;
      };
      const showEvidence = () => {
        const node = graph.nodes.find(item => item.id === openId);
        renderEvidence(evidence, node, () => {
          activate(node, "close");
          nodeControl(node.id)?.focus({preventScroll: true});
        });
      };
      function activate(node, intent) {
        if (["assembly", "subpart"].includes(node.kind) && nameOf(node.id) && !intent) {
          selectPart(node.id);
          return;
        }
        const opening = intent !== "close" && (node.mapAvailable ? !node.selected : openId !== node.id);
        openId = opening && node.rows.length ? node.id : null;
        if (openId) openByPart.set(componentId, openId);
        else openByPart.delete(componentId);
        scrollByPart.set(componentId, viewport.scrollLeft);
        if (node.kind === "shipment" && selectShipment?.(node.id, opening)) return;
        redraw();
        showEvidence();
      }
      viewport.append(svg);
      viewport.addEventListener("scroll", () => scrollByPart.set(componentId, viewport.scrollLeft), {passive: true});
      feedback.textContent = "";
      const draw = () => {
        const width = Math.floor(viewport.clientWidth);
        if (!width || width === lastWidth) return;
        lastWidth = width;
        redraw();
      };
      const observer = new ResizeObserver(draw);
      observers.set(inspector, observer);
      observer.observe(viewport);
      draw();
      showEvidence();
    } catch {
      feedback.textContent = "Can’t load part dependencies.";
    }
  }

  return {mount, prefetch};
})();
