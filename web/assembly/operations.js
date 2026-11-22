let queueKey = "";
let activityKey = "";
let nextStepsKey = "";
let callsKey = "";
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

function callFold(call) {
  const live = call.talk_url
    ? `<p><a href="${html(call.talk_url)}" target="_blank" rel="noreferrer">Try it live</a></p>` : "";
  const transcript = call.result?.transcript
    ? `<p><b>${call.result.simulated ? "Simulated transcript" : "Transcript"}</b></p>
       <p style="white-space: pre-wrap; overflow-wrap: anywhere">${html(call.result.transcript)}</p>` : "";
  return `<details class="call-script"><summary>${transcript ? "Transcript and call script" : "Call script"}</summary>
    ${transcript}<p><b>${html(call.subject)}</b></p><p>${html(call.script)}</p>${live}</details>`;
}

function callControls(next) {
  const call = next?.call_id && calls.get(next.call_id);
  return call ? `<span class="call-controls">${callFold(call)}</span>` : "";
}

function callStatus(call) {
  if (call.result) {
    const label = call.result.simulated ? " (simulated, no phone line yet)" : "";
    const summary = (call.result.summary || "See the transcript.").replace(/^Simulated call:\s*/, "");
    const sentence = summary.match(/^.*?[.!?](?=\s|$)/)?.[0] ?? summary;
    return `Dockside called ${call.to_name}${label}: ${sentence}`;
  }
  if (call.status === "failed") return `Dockside’s call to ${call.to_name} failed.`;
  if (call.status === "unavailable") return `Call to ${call.to_name} is unavailable.`;
  return `Dockside is calling ${call.to_name} now.`;
}

function renderCalls() {
  const rows = [...calls.values()].filter(call => call.status !== "unavailable");
  const key = JSON.stringify(rows);
  if (callsKey === key) return;
  callsKey = key;
  let section = $("calls-history");
  if (!section) {
    section = document.createElement("details");
    section.id = "calls-history";
    section.className = "operation-details";
    $("activity-summary").parentElement.after(section);
  }
  const open = new Set([...section.querySelectorAll("li:has(details[open])")].map(li => li.dataset.call));
  section.hidden = !rows.length;
  section.innerHTML = `<summary>Calls</summary><ul>${rows.map(call =>
    `<li data-call="${html(call.message_id)}"><p>${html(callStatus(call))}</p>${callFold(call)}</li>`).join("")}</ul>`;
  for (const item of section.querySelectorAll("li[data-call]")) {
    if (open.has(item.dataset.call)) item.querySelector("details").open = true;
  }
}

function nextStepLine(next) {
  if (!next) return "";
  return `<span class="state-chip" data-tone="${nextStepTones[next.state] ?? "active"}">${html(next.state)}</span>
    <span>${html(next.text)}</span>${callControls(next)}`;
}

/* The two clocks a buyer needs for a rush delivery: the arrival that was asked for, and the last departure that still makes the cutoff. */
function deliveryClock(option) {
  if (!option) return "";
  if (!option.releasedAt) return option.latestUsefulAt ? `Order must be released by ${at(option.latestUsefulAt)}.` : "";
  const from = option.origin ? ` ${option.origin}` : "";
  const missedAsk = option.departForRequested && Date.now() > Date.parse(option.departForRequested);
  const asked = option.requestedBy ? (missedAsk ? `Arrival asked for ${at(option.requestedBy)} is now unlikely (needed departure ${at(option.departForRequested)}). `
    : `Arrival asked for ${at(option.requestedBy)}: leave${from} by ${at(option.departForRequested)}. `) : "";
  return option.departBy ? `${asked}Last chance: leave${from} by ${at(option.departBy)} for the receiving cutoff.` : asked.trim();
}

function renderNextSteps(snapshot) {
  const all = snapshot.now_next ?? [];
  const yours = all.filter(item => item.next_step?.state === "Needs you");
  const team = all.filter(item => item.next_step?.person && item.next_step?.state !== "Needs you");
  const upcoming = all.filter(item => !yours.includes(item) && !team.includes(item));
  const waiting = (snapshot.decisions ?? []).filter(decision => decision.status !== "scheduled");
  const scheduled = (snapshot.decisions ?? []).filter(decision => decision.status === "scheduled");
  const key = JSON.stringify([all, [...calls.values()].map(call => [call.message_id, call.status, call.talk_url, call.result?.summary]), waiting.map(d => [d.id, d.title]), scheduled.map(d => [d.id, d.title, d.next_check_at])]);
  if (key === nextStepsKey) return;
  nextStepsKey = key;
  const count = waiting.length + yours.length;
  $("next-title").textContent = count ? `Action needed: ${count === 1 ? "1 thing" : `${number(count)} things`}` :
    team.length ? `Waiting on your team: ${team.length === 1 ? "1 answer" : `${number(team.length)} answers`}` : "No spending approvals pending";
  $("next-strip").dataset.attention = count ? "true" : team.length ? "waiting" : "false";
  const needs = $("needs-you");
  const drafts = new Map([...needs.querySelectorAll("form[data-answer]")].map(form => [form.dataset.answer, form.querySelector("textarea").value]));
  const opened = new Set([...needs.querySelectorAll("details[data-question][open]")].map(node => node.dataset.question));
  needs.innerHTML = [...yours.map(item => {
      const part = snapshot.components.find(component => component.id === item.component_id);
      return `<li><button class="next-part" type="button" data-next-part="${html(part.id)}">${html(nameOf(part))}</button>
        <span>${html(item.next_step.text)}</span>${callControls(item.next_step)}</li>`;
    }), ...waiting.map(decision => `<li><a href="#operations" data-jump="${html(decision.id)}">${html(decision.title)}</a>
      <span class="side-note">Waiting for ${html(decision.approver_name || "an approver")}</span></li>`),
    ...team.map(item => {
      const part = snapshot.components.find(component => component.id === item.component_id);
      const option = part.option;
      const clock = deliveryClock(option);
      const step = item.next_step;
      const overdue = step.answer_by && Date.now() > Date.parse(step.answer_by);
      const due = step.answer_by ? ` <span class="clock-note">${overdue ? `Answer was due ${html(at(step.answer_by))}.` : `Answer by ${html(at(step.answer_by))}.`}</span>` : "";
      const answer = step.message_id ? `<details class="question" data-question="${html(step.message_id)}"><summary>Read the question and answer it here</summary>
          <p>${html(step.question)}</p>
          <form class="answer-form" data-answer="${html(step.message_id)}"><label class="visually-hidden" for="answer-${html(step.message_id)}">Answer for ${html(step.person)}</label>
          <textarea id="answer-${html(step.message_id)}" rows="2" required placeholder="Answer as ${html(step.person)}"></textarea>
          <button type="submit" class="action-button">Send answer</button><span class="side-note" role="status"></span></form></details>` : "";
      return `<li><button class="next-part" type="button" data-next-part="${html(part.id)}">${html(nameOf(part))}</button>
        <span>${html(step.text)}</span>${due}${clock ? ` <span class="clock-note">${html(clock)}</span>` : ""}${answer}</li>`;
    }),
    ...scheduled.map(decision => `<li><a href="#operations" data-jump="${html(decision.id)}">${html(decision.title)}</a>
      <span class="side-note">Goes ahead ${html(at(decision.next_check_at))} unless you stop it</span></li>`)].join("");
  needs.hidden = !count && !scheduled.length && !team.length;
  for (const node of needs.querySelectorAll("details[data-question]")) if (opened.has(node.dataset.question)) node.open = true;
  for (const form of needs.querySelectorAll("form[data-answer]")) form.querySelector("textarea").value = drafts.get(form.dataset.answer) ?? "";
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
  const signed = Object.keys(decision.signatures ?? {});
  const actionLine = decision.terminal || (decision.held_reason ?
    [`On hold — ${decision.held_reason}`, decision.hold_owner && `${decision.hold_owner} · ${decision.hold_clears}`].filter(Boolean).join(". ") :
    scheduled ? `Goes ahead ${at(due)} unless stopped.` :
    `Waiting for ${decision.approver_name || "an approver"}.${signed.length ? ` Signed so far: ${signed.join(", ")}.` : ""}${due ? ` Due ${at(due)}.` : ""}`);
  const reviewed = decision.instruction ? "" : (decision.messages ?? []).map(message =>
    `<li><strong>${html(message.subject)}</strong> to ${html(message.to_name)}<p>${html(message.body_en || message.body)}</p></li>`).join("");
  return `<article class="decision" data-action="${html(decision.id)}" data-commitment-digest="${html(decision.commitment_digest || "")}"><h3>${html(decision.title)}</h3>
    <p>${html(decision.rationale)}</p><p class="side-note">${html(actionLine)}${decision.cost > 0 ? ` Estimated cost ${money(decision.cost)}.` : ""}</p>
    ${decision.instruction ? `<details class="detail-block" open><summary>Shipment instruction</summary><pre style="white-space: pre-wrap">${html(decision.instruction)}</pre></details>` : ""}
    ${decision.details || reviewed ? `<details class="detail-block"><summary>Review action</summary>
       ${decision.details ? `<p>${html(decision.details)}</p>` : ""}${reviewed ? `<ul>${reviewed}</ul>` : ""}</details>` : ""}
    ${decision.can_approve ? `<div class="decision-buttons"><button type="button" class="action-button" data-decision="approve">${scheduled ? "Do it now" : "Approve"}</button>
      <button type="button" class="secondary-button" data-decision="reject">${scheduled ? "Stop" : "Decline"}</button></div>` : ""}</article>`;
}

function renderOperations(snapshot) {
  renderCalls();
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
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({decision, by, commitment_digest: card.dataset.commitmentDigest || null})});
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

if (typeof document !== "undefined") document.addEventListener("submit", async event => {
  const form = event.target.closest("form[data-answer]");
  if (!form) return;
  event.preventDefault();
  const status = form.querySelector("[role=status]");
  const button = form.querySelector("button");
  button.disabled = true;
  status.textContent = "Sending…";
  try {
    const response = await fetch(`/api/questions/${encodeURIComponent(form.dataset.answer)}/answer`, {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: form.querySelector("textarea").value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Try again.");
    form.querySelector("textarea").value = "";
    status.textContent = "Sent. Dockside reads it in its next pass and continues.";
  } catch (error) {
    status.textContent = `Not sent. ${error.message}`;
  } finally {
    button.disabled = false;
  }
});
