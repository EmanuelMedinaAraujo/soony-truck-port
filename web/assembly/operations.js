let queueKey = "";
let activityKey = "";
let nextStepsKey = "";
let busyRequestOpen = false;
const pendingDecisions = new Set();

const nextStepTones = {
  "Needs you": "alert", "Stuck": "alert", "Late": "alert", "Waiting for reply": "waiting",
  "Confirmed": "ready", "Booked": "ready", "Unloaded": "ready", "After launch": "waiting"
};

let calls = new Map();  // message id -> voice call (talk link, script, result)

async function refreshCalls() {
  try {
    const response = await fetch("/api/calls", {cache: "no-store"});
    if (response.ok) calls = new Map((await response.json()).map(call => [call.message_id, call]));
  } catch {
    // Without the call service the script is still shown.
  }
}

/* A step that needs a phone call: the voice agent's start link and the script it follows. */
function callControls(next) {
  const call = next?.call_id && calls.get(next.call_id);
  if (!call) return "";
  const result = call.result?.summary ? `<p class="call-result"><b>Call result:</b> ${html(call.result.summary)}</p>` : "";
  const start = call.talk_url && call.status !== "done"
    ? `<a class="call-button" href="${html(call.talk_url)}" target="_blank" rel="noreferrer">${call.status === "in_progress" ? "Call in progress" : "Start call"}</a>
       <button class="text-button call-copy" type="button" data-copy="${html(call.talk_url)}">Copy link</button>` : "";
  return `<span class="call-controls">${start}
    <details class="call-script"><summary>Call script</summary><p><b>${html(call.subject)}</b></p><p>${html(call.script)}</p></details>${result}</span>`;
}

document.addEventListener("click", async event => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = "Link copied"; }
  catch { button.textContent = "Copy failed"; }
  setTimeout(() => { button.textContent = "Copy link"; }, 2000);
});

function nextStepLine(next) {
  if (!next) return "";
  return `<span class="state-chip" data-tone="${nextStepTones[next.state] ?? "active"}">${html(next.state)}</span>
    <span>${html(next.text)}</span>${callControls(next)}`;
}

function renderNextSteps(snapshot) {
  const all = snapshot.now_next ?? [];
  const upcoming = all.filter(item => item.next_step?.state !== "Needs you");
  const yours = all.filter(item => item.next_step?.state === "Needs you");
  const waiting = (snapshot.decisions ?? []).filter(decision => decision.status !== "scheduled");
  const scheduled = (snapshot.decisions ?? []).filter(decision => decision.status === "scheduled");
  const key = JSON.stringify([all, [...calls.values()].map(call => [call.message_id, call.status, call.talk_url, call.result?.summary]), waiting.map(d => [d.id, d.title]), scheduled.map(d => [d.id, d.title, d.next_check_at])]);
  if (key === nextStepsKey) return;
  nextStepsKey = key;
  const count = waiting.length + yours.length;
  $("next-title").textContent = count ? `Action needed: ${count === 1 ? "1 thing" : `${number(count)} things`}` : "No action needed";
  $("next-strip").dataset.attention = count ? "true" : "false";
  const needs = $("needs-you");
  needs.innerHTML = [...yours.map(item => {
      const part = snapshot.components.find(component => component.id === item.component_id);
      return `<li><button class="next-part" type="button" data-next-part="${html(part.id)}">${html(nameOf(part))}</button>
        <span>${html(item.next_step.text)}</span>${callControls(item.next_step)}</li>`;
    }), ...waiting.map(decision => `<li><a href="#operations" data-jump="${html(decision.id)}">${html(decision.title)}</a>
      <span class="side-note">Waiting for ${html(decision.approver_name || "an approver")}</span></li>`),
    ...scheduled.map(decision => `<li><a href="#operations" data-jump="${html(decision.id)}">${html(decision.title)}</a>
      <span class="side-note">Goes ahead ${html(at(decision.next_check_at))} unless you stop it</span></li>`)].join("");
  needs.hidden = !count && !scheduled.length;
  const focusedPart = document.activeElement?.dataset.nextPart;
  $("next-list").innerHTML = upcoming.map(item => {
    const part = snapshot.components.find(component => component.id === item.component_id);
    return `<li><button class="next-part" type="button" data-next-part="${html(part.id)}" aria-controls="inspector">${html(nameOf(part))}</button>
      <p class="next-step-line">${nextStepLine(item.next_step)}</p></li>`;
  }).join("");
  $("handling-note").textContent = `Dockside is working on ${upcoming.length === 1 ? "1 thing" : `${number(upcoming.length)} things`}`;
  $("handling").hidden = !upcoming.length;
  if (focusedPart) $("next-list").querySelector(`[data-next-part="${CSS.escape(focusedPart)}"]`)?.focus({preventScroll: true});
  $("next-strip").hidden = false;
}

async function refreshBusy() {
  if (busyRequestOpen) return;
  busyRequestOpen = true;
  const progress = $("busy-progress");
  try {
    const response = await fetch("/api/state", {cache: "no-store"});
    if (!response.ok) throw new Error(response.status);
    const system = (await response.json()).system ?? {};
    const busy = system.busy;
    const label = busy ? busy[0].toUpperCase() + busy.slice(1) : "";
    if (progress.textContent !== label) progress.textContent = label;
    const llm = system.llm ?? {ok: true};
    const banner = $("llm-banner");
    banner.hidden = llm.ok !== false;
    if (llm.ok === false) {
      const text = `Agent paused. ${llm.message}`;
      if (banner.textContent !== text) banner.textContent = text;
      banner.dataset.kind = llm.kind;
    }
  } catch {
    progress.textContent = "";
  } finally {
    busyRequestOpen = false;
  }
}

function decisionItem(decision) {
  const scheduled = decision.status === "scheduled";
  const due = scheduled ? decision.next_check_at : decision.due_at;
  const actionLine = scheduled ? `Goes ahead ${at(due)} unless stopped.` :
    `Waiting for ${decision.approver_name || "an approver"}.${due ? ` Due ${at(due)}.` : ""}`;
  const reviewed = decision.messages?.map(message =>
    `<li><strong>${html(message.subject)}</strong> to ${html(message.to_name)}<p>${html(message.body_en || message.body)}</p></li>`).join("") ?? "";
  return `<article class="decision" data-action="${html(decision.id)}"><h3>${html(decision.title)}</h3>
    <p>${html(decision.rationale)}</p><p class="side-note">${html(actionLine)}${decision.cost > 0 ? ` Estimated cost ${money(decision.cost)}.` : ""}</p>
    ${decision.details || reviewed ? `<details class="detail-block"><summary>Review action</summary>
       ${decision.details ? `<p>${html(decision.details)}</p>` : ""}${reviewed ? `<ul>${reviewed}</ul>` : ""}</details>` : ""}
    <div class="decision-buttons"><button type="button" class="action-button" data-decision="approve">${scheduled ? "Do it now" : "Approve"}</button>
      <button type="button" class="secondary-button" data-decision="reject">${scheduled ? "Stop" : "Decline"}</button></div></article>`;
}

function renderOperations(snapshot) {
  const decisions = snapshot.decisions;
  $("attention").textContent = decisions.length ? `${number(decisions.length)} ${decisions.length === 1 ? "open action" : "open actions"}` : "Nothing waiting for your approval.";
  const next = JSON.stringify(decisions);
  if (next !== queueKey && pendingDecisions.size === 0) {
    queueKey = next;
    $("decision-list").innerHTML = decisions.map(decisionItem).join("");
  }
  const activity = JSON.stringify(snapshot.activity);
  if (activity !== activityKey) {
    activityKey = activity;
    const {summary, items} = snapshot.activity;
    $("activity-summary").textContent = `Recent activity · ${summary}`;
    $("activity-list").innerHTML = items.length ? items.map(event =>
      `<li><time datetime="${html(event.ts)}">${html(at(event.ts))}</time> ${html(event.text)}</li>`).join("") : "<li>No activity recorded yet.</li>";
  }
}

function initOperations(reload) {
  const decisionResult = $("operation-result");
  $("needs-you").addEventListener("click", event => {
    const link = event.target.closest("[data-jump]");
    const card = link && $("decision-list").querySelector(`[data-action="${CSS.escape(link.dataset.jump)}"]`);
    if (!card) return;
    event.preventDefault();
    card.scrollIntoView({block: "center"});
    card.querySelector("button")?.focus({preventScroll: true});
  });
  $("decision-list").addEventListener("click", async event => {
    const button = event.target.closest("[data-decision]");
    if (!button) return;
    const card = button.closest("[data-action]");
    const id = card.dataset.action;
    if (pendingDecisions.has(id)) return;
    const decision = button.dataset.decision;
    let by = "web";
    try { by = localStorage.getItem("dockside.me") || by; } catch {}
    pendingDecisions.add(id);
    card.querySelectorAll("button").forEach(control => control.disabled = true);
    decisionResult.textContent = "Recording decision…";
    try {
      const response = await fetch(`/api/actions/${encodeURIComponent(id)}/decide`, {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({decision, by})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Try again after refreshing the page.");
      const queued = result.messages?.some(message => ["draft", "outbox"].includes(message.status));
      const unknown = result.messages?.some(message => ["sending", "delivery_unknown"].includes(message.status));
      decisionResult.textContent = decision === "reject" ? "Decision recorded." : unknown ? "Approved. Delivery needs confirmation." :
        queued ? "Approved. A message is queued." : "Approved.";
      card.remove();
    } catch (error) {
      decisionResult.textContent = `Couldn’t record this decision. ${error.message}`;
      card.querySelectorAll("button").forEach(control => control.disabled = false);
    } finally {
      pendingDecisions.delete(id);
      await reload();
    }
  });
  $("ask-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = event.target.querySelector("button");
    const answer = $("answer");
    const question = $("question").value.trim();
    if (button.disabled || !question) return;
    button.disabled = true;
    answer.textContent = "Checking shipment and packing records…";
    try {
      const response = await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({q: question})});
      if (!response.ok) throw new Error();
      answer.textContent = (await response.json()).answer;
    } catch {
      answer.textContent = "Dockside couldn’t answer. Try again in a moment.";
    } finally {
      button.disabled = false;
    }
  });
}
