(async () => {
  if (window.self !== window.top) return;

  let links;
  try {
    const response = await fetch("/assembly/workspaces.json", {cache: "no-store"});
    if (!response.ok) return;
    links = await response.json();
  } catch {
    return;
  }

  const nav = document.createElement("nav");
  nav.className = "workspace-switch";
  nav.setAttribute("aria-label", "Dockside workspaces");
  const row = document.createElement("div");
  row.className = "shell";
  const brand = document.createElement("span");
  brand.className = "workspace-brand";
  brand.textContent = "Dockside";
  row.append(brand);
  for (const [id, label] of [["soony", "Soony"], ["discord", "Discord"], ["kestrel", "Kestrel Toys"]]) {
    if (!links[id]) continue;
    const url = new URL(links[id], location.href);
    if (!["https:", "http:"].includes(url.protocol)) continue;
    const tab = document.createElement("a");
    tab.className = "workspace-tab";
    tab.href = url.href;
    tab.textContent = label;
    if (id === links.active) tab.setAttribute("aria-current", "page");
    row.append(tab);
  }

  const phone = document.createElement("button");
  phone.type = "button";
  phone.className = "workspace-tab";
  phone.textContent = "Phone view";
  phone.setAttribute("aria-pressed", "false");
  row.append(phone);
  nav.append(row);
  document.body.prepend(nav);

  phone.addEventListener("click", () => {
    const dialog = document.createElement("dialog");
    dialog.className = "workspace-preview";
    dialog.setAttribute("aria-label", "Phone view");
    const toolbar = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = "Phone view";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "workspace-tab";
    close.textContent = "Close phone view";
    close.addEventListener("click", () => dialog.close());
    toolbar.append(title, close);
    const frame = document.createElement("iframe");
    frame.src = location.href;
    frame.title = "Dockside at phone width";
    frame.allow = "microphone";
    dialog.append(toolbar, frame);
    dialog.addEventListener("close", () => {
      dialog.remove();
      phone.setAttribute("aria-pressed", "false");
      phone.focus();
    });
    document.body.append(dialog);
    phone.setAttribute("aria-pressed", "true");
    dialog.showModal();
  });
})();
