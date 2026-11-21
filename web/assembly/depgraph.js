/* Component dependencies within the part inspector. */
window.PartDependencyGraph = (() => {
  const namespace = "http://www.w3.org/2000/svg";
  const order = ["assembly", "part", "subpart", "supplier", "risk"];
  const columnNames = {assembly: "Part of", part: "Part", subpart: "Contains", supplier: "Made by", risk: "Risks and deliveries"};
  const kinds = {assembly: "Assembly", part: "Selected part", subpart: "Sub-part", supplier: "Supplier",
    risk: "Supply risk", shipment: "Delivery"};
  const statuses = {ok: "OK", at_risk: "At risk", blocked: "Blocked", info: "No effect on launch"};
  const observers = new WeakMap();
  const expandedByPart = new Map();
  const cardHeight = 61;
  const detailLine = 17;
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

  function detailText(node) {
    const lines = [];
    // The status line already shows the meta text; don't repeat it as the first sentence.
    const meta = String(node.meta ?? node.subtitle ?? "").trim().replace(/\.$/, "");
    let detail = String(node.detail ?? "").trim();
    if (meta && detail.toLowerCase().startsWith(meta.toLowerCase() + ".")) detail = detail.slice(meta.length + 1).trim();
    if (node.doing) lines.push(node.doing);
    if (detail) lines.push(detail);
    if (node.quote) lines.push(`“${node.quote}”`);
    const sources = node.sources ?? (node.evidence ? [node.evidence.split("/").at(-1)] : []);
    if (sources.length) lines.push(`Source: ${sources.join(", ")}`);
    if (node.reference) lines.push(`Reference ${node.reference}`);
    return lines;
  }

  function isSourceLine(text) {
    return text.startsWith("Source:") || text.startsWith("Reference");
  }

  function columnOf(node) {
    return node.kind === "shipment" ? "risk" : node.kind;
  }

  function layout(graph, width, heights) {
    const layers = order.map(kind => graph.nodes.filter(node => columnOf(node) === kind)).filter(layer => layer.length);
    const vertical = width < Math.max(700, layers.length * 160 + (layers.length - 1) * 56 + 32);
    const positions = new Map();
    const heightOf = node => heights.get(node.id) ?? cardHeight;
    layers.forEach((layer, column) => layer.forEach(node => positions.set(node.id, {node, column, height: heightOf(node)})));
    const connections = graph.edges.filter(edge => positions.has(edge.source) && positions.has(edge.target)).map(edge => {
      const source = positions.get(edge.source);
      const target = positions.get(edge.target);
      const reversed = source.column > target.column;
      return {edge, from: reversed ? target : source, to: reversed ? source : target, reversed};
    });

    if (vertical) {
      const cardWidth = Math.min(width - 32, 360);
      let y = 4;
      const headers = [];
      layers.forEach(layer => {
        headers.push({x: (width - cardWidth) / 2, y: y + 12, text: columnNames[columnOf(layer[0])]});
        y += 24;
        for (const node of layer) {
          const position = positions.get(node.id);
          Object.assign(position, {x: (width - cardWidth) / 2, y, width: cardWidth});
          y += position.height + 10;
        }
        y += 8;
      });
      return {positions, connections, height: y, vertical, headers};
    }

    const margin = 4;
    const available = width - margin * 2;
    const gap = 56;
    const cardWidth = Math.min(230, Math.floor((available - (layers.length - 1) * gap) / layers.length));
    const step = (available - cardWidth) / Math.max(layers.length - 1, 1);
    const top = 30;
    const rowStep = cardHeight + 14;
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
      const x = layers.length === 1 ? (width - cardWidth) / 2 : margin + column * step;
      headers.push({x, y: 14, text: columnNames[columnOf(layer[0])]});
      let extra = 0;
      layer.forEach((node, index) => {
        const position = positions.get(node.id);
        const y = top + (index + (rows - layer.length) / 2) * rowStep + extra;
        extra += position.height - cardHeight;
        Object.assign(position, {x, y, width: cardWidth});
        bottom = Math.max(bottom, y + position.height);
      });
    }
    return {positions, connections, height: bottom + 6, vertical, headers};
  }

  function wire(connection, vertical) {
    const {from, to} = connection;
    if (vertical) {
      const x1 = from.x + 10;
      const x2 = to.x + 10;
      const rail = from.x - 10;
      return `M ${x1} ${from.y + 30} H ${rail} V ${to.y + 30} H ${x2 - 10}`;
    }
    const x1 = from.x + from.width;
    const y1 = from.y + cardHeight / 2;
    const y2 = to.y + cardHeight / 2;
    const x = (x1 + to.x) / 2;
    return `M ${x1} ${y1} C ${x} ${y1} ${x} ${y2} ${to.x} ${y2}`;
  }

  function render(svg, width, graph, section, activate, expanded) {
    const focusId = svg.contains(document.activeElement) ? document.activeElement.dataset.node : null;
    const nameFont = `700 14px ${getComputedStyle(section).fontFamily}`;
    const detailFont = `400 12.5px ${getComputedStyle(section).fontFamily}`;
    const context = document.createElement("canvas").getContext("2d");

    // Expanded cards grow downwards; measure them before placing anything.
    const provisional = layout(graph, width, new Map());
    const heights = new Map();
    const detailLines = new Map();
    for (const id of expanded) {
      const position = provisional.positions.get(id);
      if (!position) continue;
      context.font = detailFont;
      const lines = detailText(position.node).flatMap(text =>
        wrap(text, position.width - 22, context, isSourceLine(text) ? 1 : 9).map(line => ({line, doing: text === position.node.doing})));
      if (!lines.length) continue;
      detailLines.set(id, lines);
      heights.set(id, cardHeight + 8 + lines.length * detailLine);
    }
    const {positions, connections, height, vertical, headers} = layout(graph, width, heights);
    svg.dataset.direction = vertical ? "vertical" : "horizontal";
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    context.font = nameFont;

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
    for (const connection of connections) {
      const {edge, reversed} = connection;
      const source = positions.get(edge.source).node;
      const target = positions.get(edge.target).node;
      const description = `${source.label} ${edge.label} ${target.label}`;
      for (const node of [source, target]) {
        if (!relations.has(node.id)) relations.set(node.id, []);
        relations.get(node.id).push(description);
      }
      const attributes = {d: wire(connection, vertical)};
      if (!vertical) attributes[reversed ? "marker-start" : "marker-end"] = `url(#${markerId})`;
      lines.append(svgElement("path", attributes));
    }
    svg.append(lines);

    const ordered = [...positions.values()].sort((a, b) => expanded.has(a.node.id) - expanded.has(b.node.id));
    for (const {node, x, y, width: cardWidth, height: nodeHeight} of ordered) {
      const isOpen = detailLines.has(node.id);
      const meta = node.meta ?? node.subtitle ?? kinds[node.kind];
      const status = statuses[node.status] ?? "";
      const card = svgElement("g", {class: "depgraph-node", "data-node": node.id, "data-kind": node.kind,
        "data-status": node.status, "data-selected": node.selected ? "true" : "false", "data-open": isOpen ? "true" : "false",
        transform: `translate(${x},${y})`, tabindex: "0", role: "button", "aria-expanded": String(isOpen),
        "aria-label": `${node.label}. ${meta}. ${node.kind === "part" ? "" : status}`,
        "aria-description": [isOpen ? detailText(node).join(". ") : "", relations.get(node.id)?.join(". ")].filter(Boolean).join(". ")});
      card.append(svgElement("rect", {class: "depgraph-card", width: cardWidth, height: nodeHeight, rx: 9}));
      context.font = nameFont;
      const nameLines = wrap(node.label, cardWidth - 22 - (node.selected ? 44 : 0), context);
      for (const [i, line] of nameLines.entries()) {
        card.append(svgElement("text", {x: 11, y: (nameLines.length === 1 ? 25 : 19) + 16 * i, class: "depgraph-name"}, line));
      }
      if (node.selected) card.append(svgElement("text", {x: cardWidth - 10, y: 19, "text-anchor": "end", class: "depgraph-flag"}, "On map"));
      if (node.kind !== "part") card.append(svgElement("circle", {cx: 14, cy: 48, r: 3.4}));
      context.font = "700 11.5px sans-serif";
      card.append(svgElement("text", {x: node.kind === "part" ? 11 : 22, y: 52, class: "depgraph-meta"},
        wrap(meta, cardWidth - 34, context, 1)[0]));
      if (isOpen) {
        card.append(svgElement("line", {class: "depgraph-rule", x1: 11, x2: cardWidth - 11, y1: cardHeight + 1, y2: cardHeight + 1}));
        detailLines.get(node.id).forEach(({line, doing}, i) => card.append(svgElement("text",
          {x: 11, y: cardHeight + 18 + i * detailLine, class: doing ? "depgraph-detail depgraph-doing" : isSourceLine(line) ? "depgraph-source" : "depgraph-detail"}, line)));
      } else if (detailText(node).length) {
        card.append(svgElement("text", {x: cardWidth - 12, y: 52, "text-anchor": "end", class: "depgraph-more", "aria-hidden": "true"}, "+"));
      }
      card.addEventListener("click", () => activate(node));
      card.addEventListener("keydown", event => {
        if (event.key === "Escape" && isOpen) { event.preventDefault(); activate(node, "close"); }
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        activate(node);
      });
      svg.append(card);
      if (focusId === node.id) card.focus({preventScroll: true});
    }
  }

  async function mount(inspector, componentId, nameOf, selectPart, selectedShipment = null, selectShipment = null) {
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
    const viewport = document.createElement("div");
    viewport.className = "depgraph-viewport";
    const feedback = document.createElement("p");
    feedback.className = "depgraph-feedback";
    feedback.setAttribute("role", "status");
    feedback.textContent = "Loading dependencies…";
    section.append(heading, verdict, viewport, feedback);
    inspector.querySelector(".inspector-head").after(section);

    try {
      const graph = structuredClone(await load(componentId));
      if (!section.isConnected) return;
      for (const node of graph.nodes) {
        if (["assembly", "part", "subpart"].includes(node.kind)) node.label = nameOf(node.id) ?? node.label;
        if (node.id === selectedShipment) node.selected = true;
      }
      if (!graph.short && graph.verdict) verdict.textContent = graph.verdict;
      verdict.hidden = !verdict.textContent;

      const expanded = expandedByPart.get(componentId) ?? new Set();
      expandedByPart.set(componentId, expanded);
      const svg = svgElement("svg", {role: "group", "aria-labelledby": heading.id});
      let lastWidth = 0;
      const redraw = () => render(svg, lastWidth, graph, section, activate, expanded);
      function activate(node, intent) {
        if (["assembly", "subpart"].includes(node.kind) && nameOf(node.id) && !intent) {
          selectPart(node.id);
          return;
        }
        const opening = intent !== "close" && !expanded.has(node.id);
        if (opening) expanded.add(node.id); else expanded.delete(node.id);
        if (node.kind === "shipment" && opening && selectShipment && selectShipment(node.id)) return;  // re-renders the inspector
        redraw();
      }
      viewport.append(svg);
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
    } catch {
      feedback.textContent = "Can’t load part dependencies.";
    }
  }

  return {mount, prefetch};
})();
