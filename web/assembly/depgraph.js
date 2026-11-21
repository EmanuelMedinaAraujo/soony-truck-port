/* Component dependencies within the part inspector. */
window.PartDependencyGraph = (() => {
  const namespace = "http://www.w3.org/2000/svg";
  const order = ["product", "assembly", "part", "subpart", "supplier", "risk"];
  const kinds = {product: "Finished product", assembly: "Assembly", part: "Selected part",
    subpart: "Sub-part", supplier: "Supplier", risk: "Supply risk", shipment: "Inbound shipment"};
  const statuses = {ok: "OK", at_risk: "At risk", blocked: "Blocked"};
  const observers = new WeakMap();
  let nextId = 0;

  function svgElement(tag, attributes = {}, content) {
    const element = document.createElementNS(namespace, tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
    if (content != null) element.textContent = content;
    return element;
  }

  function wrap(label, width, context) {
    const lines = [""];
    for (const word of label.split(/\s+/)) {
      const line = lines.at(-1);
      const combined = line ? `${line} ${word}` : word;
      if (line && context.measureText(combined).width > width && lines.length < 2) lines.push(word);
      else lines[lines.length - 1] = combined;
    }
    return lines.map(line => {
      if (context.measureText(line).width <= width) return line;
      while (line && context.measureText(line + "…").width > width) line = line.slice(0, -1);
      return line.trimEnd() + "…";
    });
  }

  function layout(graph, width) {
    const layers = order.map(kind => graph.nodes.filter(node =>
      node.kind === kind || (kind === "risk" && node.kind === "shipment"))).filter(layer => layer.length);
    const vertical = width < Math.max(740, layers.length * 135 + (layers.length - 1) * 78 + 32);
    const positions = new Map();
    const cardHeight = 61;
    layers.forEach((layer, column) => layer.forEach(node => positions.set(node.id, {node, column})));
    const connections = graph.edges.map(edge => {
      const source = positions.get(edge.source);
      const target = positions.get(edge.target);
      const reversed = source.column > target.column;
      return {edge, from: reversed ? target : source, to: reversed ? source : target, reversed};
    });

    if (vertical) {
      const incoming = new Map();
      for (const connection of connections) {
        const count = incoming.get(connection.to) ?? 0;
        connection.slot = count;
        incoming.set(connection.to, count + 1);
      }
      const cardWidth = Math.min(width - 72, 320);
      let y = 14;
      [...positions.values()].forEach((position, index) => {
        y += (incoming.get(position) ?? 0) * 23;
        Object.assign(position, {x: (width - cardWidth) / 2, y, index, width: cardWidth});
        y += cardHeight + 13;
      });
      return {positions, connections, cardHeight, height: y, vertical};
    }

    const margin = 16;
    const available = width - margin * 2;
    const cardWidth = Math.min(210, Math.floor((available - (layers.length - 1) * 78) / layers.length));
    const step = (available - cardWidth) / Math.max(layers.length - 1, 1);
    let crossing = 0;
    for (const connection of connections) {
      if (connection.to.column - connection.from.column > 1) connection.lane = crossing++;
    }
    const top = 18 + crossing * 24;
    const rowStep = cardHeight + 15;
    const rows = Math.max(...layers.map(layer => layer.length));
    for (const [column, layer] of layers.entries()) {
      layer.forEach((node, index) => {
        const x = layers.length === 1 ? (width - cardWidth) / 2 : margin + column * step;
        const y = top + (index + (rows - layer.length) / 2) * rowStep;
        Object.assign(positions.get(node.id), {x, y, width: cardWidth});
      });
    }
    return {positions, connections, cardHeight, height: top * 2 - crossing * 24 + rows * rowStep - 15, vertical};
  }

  function wire(connection, cardHeight, vertical) {
    const {from, to, lane, slot} = connection;
    if (vertical) {
      const x = to.x + to.width / 2;
      const y = to.y - 11 - slot * 23;
      if (to.index === from.index + 1 && slot === 0) {
        return {path: `M ${x} ${from.y + cardHeight} V ${to.y}`, x: x + 8, y, anchor: "start"};
      }
      const rail = from.x - 8 - (from.index % 5) * 5;
      const entry = to.x + 12 + slot * Math.min(15, (to.width - 24) / (slot + 1));
      return {path: `M ${from.x} ${from.y + cardHeight - 18} H ${rail} V ${y} H ${entry} V ${to.y}`,
        x: entry + 6, y: y - 3, anchor: "start"};
    }
    const x1 = from.x + from.width;
    const y1 = from.y + cardHeight / 2;
    const y2 = to.y + cardHeight / 2;
    const x = (x1 + to.x) / 2;
    if (lane != null) {
      const y = 13 + lane * 24;
      return {path: `M ${x1} ${y1} H ${x1 + 12} V ${y} H ${to.x - 12} V ${y2} H ${to.x}`, x, y: y - 3};
    }
    return {path: `M ${x1} ${y1} C ${x} ${y1} ${x} ${y2} ${to.x} ${y2}`,
      x, y: (y1 + y2) / 2 - 5};
  }

  function render(svg, width, graph, section, activate) {
    const focusId = svg.contains(document.activeElement) ? document.activeElement.dataset.node : null;
    const {positions, connections, cardHeight, height, vertical} = layout(graph, width);
    svg.dataset.direction = vertical ? "vertical" : "horizontal";
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const context = document.createElement("canvas").getContext("2d");
    context.font = `700 14px ${getComputedStyle(section).fontFamily}`;

    const markerId = `${section.id}-arrow`;
    const defs = svgElement("defs");
    const marker = svgElement("marker", {id: markerId, viewBox: "0 0 7 8", refX: 7, refY: 4,
      markerWidth: 7, markerHeight: 8, markerUnits: "userSpaceOnUse", orient: "auto-start-reverse"});
    marker.append(svgElement("path", {d: "M 0 0 L 7 4 L 0 8 Z"}));
    defs.append(marker);
    svg.replaceChildren(defs);

    const lines = svgElement("g", {class: "depgraph-wires", "aria-hidden": "true"});
    const labels = svgElement("g", {class: "depgraph-edge-labels", "aria-hidden": "true"});
    const relations = new Map();
    for (const connection of connections) {
      const {edge, reversed} = connection;
      const source = positions.get(edge.source).node;
      const target = positions.get(edge.target).node;
      const description = `${source.label} → ${edge.label} → ${target.label}`;
      for (const node of [source, target]) {
        if (!relations.has(node.id)) relations.set(node.id, []);
        relations.get(node.id).push(description);
      }
      const {path, x, y, anchor = "middle"} = wire(connection, cardHeight, vertical);
      const line = svgElement("path", {d: path, [reversed ? "marker-start" : "marker-end"]: `url(#${markerId})`});
      const label = svgElement("text", {x, y, "text-anchor": anchor}, edge.label);
      label.append(svgElement("title", {}, [edge.label, edge.detail].filter(Boolean).join(". ")));
      lines.append(line);
      labels.append(label);
    }
    svg.append(lines, labels);

    for (const {node, x, y, width: cardWidth} of positions.values()) {
      const type = node.subtitle ?? kinds[node.kind];
      const status = statuses[node.status];
      const card = svgElement("g", {class: "depgraph-node", "data-node": node.id, "data-kind": node.kind,
        "data-status": node.status, transform: `translate(${x},${y})`, tabindex: "0", role: "button",
        "aria-label": `${node.label}, ${type}, ${status}`, "aria-description": relations.get(node.id)?.join(". ") ?? ""});
      card.append(svgElement("title", {}, [node.label, node.code, node.reference, node.detail].filter(Boolean).join("\n")));
      card.append(svgElement("rect", {class: "depgraph-card", width: cardWidth, height: cardHeight, rx: 9}));
      const nameLines = wrap(node.label, cardWidth - 22, context);
      for (const [i, line] of nameLines.entries()) {
        card.append(svgElement("text", {x: 10, y: (nameLines.length === 1 ? 25 : 19) + 16 * i,
          class: "depgraph-name"}, line));
      }
      card.append(svgElement("circle", {cx: 13, cy: 48, r: 3.4}));
      card.append(svgElement("text", {x: 21, y: 52, class: "depgraph-meta"}, `${type}, ${status}`));
      card.addEventListener("click", () => activate(node));
      card.addEventListener("keydown", event => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        activate(node);
      });
      svg.append(card);
      if (focusId === node.id) card.focus({preventScroll: true});
    }
  }

  async function mount(inspector, componentId, nameOf, selectPart) {
    observers.get(inspector)?.disconnect();
    const section = document.createElement("section");
    section.className = "depgraph";
    section.id = `part-graph-${++nextId}`;
    const heading = document.createElement("h3");
    heading.id = `${section.id}-heading`;
    heading.textContent = "Part dependencies";
    section.setAttribute("aria-labelledby", heading.id);
    const viewport = document.createElement("div");
    viewport.className = "depgraph-viewport";
    const feedback = document.createElement("p");
    feedback.className = "depgraph-feedback";
    feedback.setAttribute("role", "status");
    feedback.textContent = "Loading dependencies…";
    section.append(heading, viewport, feedback);
    inspector.querySelector(".inspector-head").after(section);

    try {
      const response = await fetch(`/api/parts/${encodeURIComponent(componentId)}/graph`, {cache: "no-store"});
      if (!response.ok) throw new Error(`Graph request failed (${response.status})`);
      const graph = await response.json();
      if (!section.isConnected) return;
      for (const node of graph.nodes) {
        if (["assembly", "part", "subpart"].includes(node.kind)) node.label = nameOf(node.id) ?? node.label;
      }

      const svg = svgElement("svg", {role: "group", "aria-labelledby": heading.id});
      const activate = node => {
        if (["assembly", "subpart"].includes(node.kind) && nameOf(node.id)) {
          selectPart(node.id);
          return;
        }
        const supply = graph.edges.filter(edge => edge.target === node.id && edge.label === "made by" && edge.detail);
        const details = [node.label, node.subtitle, statuses[node.status], node.detail, ...supply.map(edge => edge.detail)];
        feedback.textContent = [...new Set(details.filter(Boolean))].join(". ");
      };
      viewport.append(svg);
      feedback.textContent = "";
      let lastWidth = 0;
      const draw = () => {
        const width = Math.floor(viewport.clientWidth);
        if (!width || width === lastWidth) return;
        lastWidth = width;
        render(svg, width, graph, section, activate);
      };
      const observer = new ResizeObserver(draw);
      observers.set(inspector, observer);
      observer.observe(viewport);
      draw();
    } catch {
      feedback.textContent = "Can’t load part dependencies.";
    }
  }

  return {mount};
})();
