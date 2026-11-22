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

let viewer = {person: null, source: null};  // who is signed in, from /api/me
let calls = new Map();  // message id -> voice call (talk link, script, result)

async function refreshViewer() {
  try {
    const response = await fetch("/api/me", {cache: "no-store"});
    if (response.ok) viewer = await response.json();
  } catch {
    // Without it the page shows questions read-only.
  }
}

async function sendAnswer(id, text, status) {
  status.textContent = "Sending…";
  const response = await fetch(`/api/questions/${encodeURIComponent(id)}/answer`, {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({text})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "Try again.");
  status.textContent = "Sent. Dockside reads it in its next pass and continues.";
}

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
  if (option.needsRevisedTerms && option.deliveryState?.revised_reason) return option.deliveryState.revised_reason;
  const from = option.origin ? ` ${option.origin}` : "";
  const asked = option.requestedBy && option.departForRequested ? `Ordered arrival ${at(option.requestedBy)}: leave${from} by ${at(option.departForRequested)}.` : "";
  return asked;
}

/* A supplier contradicted an earlier commitment: who acts, what conflicts (with its source), the clocks, what clears it. */
function conflictCard(part, item, snapshot) {
  const option = part.option;
  const conflict = option.deliveryState.conflict;
  const person = item.next_step.person ?? conflict.owner_reconcile;
  const logistics = String(conflict.owner_logistics ?? "").includes("@") ? `${conflict.who} (supplier logistics)` : conflict.owner_logistics;
  const unload = option.modeledUnloadAt ? Date.parse(option.modeledUnloadAt) : null;
  const cutoff = Date.parse(snapshot.cutoff);
  const late = option.modeledLateMinutes ?? (unload && unload > cutoff ? Math.round((unload - cutoff) / 60000) : 0);
  const anchor = option.modeledUnloadAnchorAt ?? conflict.at;
  const q = item.question;
  const task = item.next_step.task ?? q?.task;
  // Past the departure deadline only verified departure evidence keeps a Sunday recovery alive.
  const departure = option.departure ?? {state: "open"};  // the release gate's structured result, not the browser clock
  const departed = departure.state !== "passed";
  const answered = task && task.status && task.status !== "open"
    ? `<p class="task-state"><span class="state-chip" data-tone="waiting">Answered</span> ${html(task.answered_by ?? task.person)}${task.answered_by && task.answered_by !== task.person ? ` for ${html(task.person)}` : ""} at ${html(at(task.answered_at))}: “${html(task.answer)}”. Waiting for the supplier's written confirmation before anything is released.</p>` : "";
  return `<li class="decision-card"><button class="next-part" type="button" data-next-part="${html(part.id)}">${html(nameOf(part))}</button>
    ${departed ? "" : `<p class="verdict"><b>${html(at(departure.deadline))} departure deadline passed. No confirmed Sunday recovery.</b>
      ${option.copiesAdded ? `The +${number(option.copiesAdded)} copies stay a scenario until the books are received and inspected.` : ""}</p>`}
    <span class="decision-ask"><b>${html(person)}:</b> ${departed ? `reconcile the order and its dispatch instruction with ${html(conflict.who)}, then record the outcome.`
      : `reconcile the existing order with ${html(conflict.who)} in writing. Sunday delivery reopens only with proof that the books left ${html(option.origin ?? "the supplier")} by ${html(at(departure.deadline))}.`}${departed ? (option.copiesAdded ? ` The rescue (+${number(option.copiesAdded)} copies) is at risk.` : ` No copies depend on it right now: ${html(nameOf(snapshot.components.find(c => c.id === snapshot.focusComponentId) ?? part))} limits dispatch.`) : ""}</span>${item.alsoAsked?.length ? ` <span class="side-note">Escalated to ${html(item.alsoAsked.map(row => row.person).join(", "))} too.</span>` : ""}
    <p class="conflict-line"><span class="state-chip" data-tone="alert">Conflict</span> ${html(at(conflict.at))} ${html(conflict.who)}: “${html(conflict.text)}.”
      <a href="/api/replies/${encodeURIComponent(conflict.message_id)}" target="_blank" rel="noreferrer">Source</a></p>
    <p class="clock-row">${option.modeledUnloadAt ? `<span>Modeled arrival <b>${html(at(option.modeledUnloadAt))}</b> if loading had started at ${html(at(anchor))}${option.modeledUnloadReplyId ? ` (<a href="/api/replies/${encodeURIComponent(option.modeledUnloadReplyId)}" target="_blank" rel="noreferrer">latest reply</a>)` : ""}${late ? ` · <b>${late} min</b> after the cutoff` : ""}</span>` : ""}
      <span>Receiving cutoff <b>${html(at(snapshot.cutoff))}</b></span>
      ${departure.deadline ? `<span>Leave${option.origin ? ` ${html(option.origin)}` : ""} by <b>${html(at(departure.deadline))}</b>${departure.state === "passed" ? " (passed, no proof of departure)" : departure.state === "verified" ? ` (left ${html(at(departure.departed_at))})` : ""}</span>` : ""}</p>
    <details class="question" open><summary>What clears it</summary>
      <ol class="clears">
        <li>${html(person)} confirms the order reference, quantity, price and the dispatch instruction with ${html(conflict.who)}.</li>
        <li>${html(logistics)} confirms a carrier booking, a vehicle of the allowed size and an arrival time before the cutoff${departed ? "" : `, plus proof that the goods actually left ${html(option.origin ?? "the supplier")} in time (the ${html(at(option.departBy))} deadline has passed)`}.</li>
        <li>${html(siteName())} receiving confirms an unload slot for that arrival.</li></ol>
      <p class="side-note">Dockside's estimate: ${html(option.modeledUnloadBasis ?? "")}. Until all three hold, the release gate sends no amendment and the forecast counts none of these copies; usable stock rises only after receipt and inspection.</p></details>
    ${answered || (q ? questionBlock(part, {...q, person: q.person ?? person}) : "")}</li>`;
}

/* The open question, its options with a recommendation, and the answer controls for the person it is assigned to. */
function questionParts(part, step) {
  const allowed = step.answerable_by ?? step.task?.answerable_by ?? [step.person];
  const mine = viewer.person && (allowed.includes(viewer.person) || viewer.source === "local demo");
  const day = value => value ? new Date(value).toLocaleDateString("en-GB", {day: "numeric", month: "short", timeZone: "UTC"}) : "";
  // Recommend the order line that matches this delivery's quantity; with none, a new emergency order.
  const wanted = part.option?.quantity;
  const match = (step.options ?? []).find(option => wanted && option.quantity === wanted);
  const advice = step.task ? "" : step.options?.length ? (match ? `Recommended: PO ${match.po} line ${match.line}, it matches the ${number(wanted)} ordered.`
    : `Recommended: a new emergency order reference for the ${number(wanted)} already accepted. No line named matches that quantity${(step.options ?? []).every(option => option.quantity > wanted) ? "; they are larger, later deliveries" : ""}.`) : "";
  const choices = (step.options ?? []).map(option => `<button type="button" class="secondary-button" data-quick-answer="${html(step.message_id)}"
      data-text="${html(option.answer)}">${option.label ? html(option.label) : `PO ${html(option.po)} line ${html(option.line)} · ${number(option.quantity)} · due ${html(day(option.requested))}`}</button>`).join("");
  const emergency = step.options?.length && !step.task ? `<button type="button" class="secondary-button" data-quick-answer="${html(step.message_id)}"
      data-text="Use a new emergency order reference for the accepted delivery (not an additional order); I will send the reference.">New emergency reference (same order)</button>` : "";
  const form = mine ? `<form class="answer-form" data-answer="${html(step.message_id)}">${choices ? `<div class="answer-choices">${choices}${emergency}</div>` : ""}
      <label class="visually-hidden" for="answer-${html(step.message_id)}">Answer for ${html(step.person)}</label>
      <textarea id="answer-${html(step.message_id)}" rows="2" placeholder="Or write your answer"></textarea>
      <button type="submit" class="action-button">Send answer</button><span class="side-note" role="status"></span></form>`
    : `<p class="side-note">${html(allowed[0])}${allowed.length > 1 ? ` (or ${html(allowed.slice(1).join(", "))})` : ""} answers here after company sign-in${viewer.person ? ` (you are ${html(viewer.person)})` : ""}, or with /answer in Discord. Dockside reminds and escalates until someone answers.</p>`;
  const answer = step.message_id ? `<div class="question" data-question="${html(step.message_id)}">
      ${choices && !step.task ? `<p><b>Options:</b> ${(step.options ?? []).map(option => `PO ${html(option.po)} line ${html(option.line)} (${number(option.quantity)}, due ${html(day(option.requested))})`).join(" · ")} · or a new emergency reference for the same order.</p>
      <p class="advice">${html(advice)}</p>` : ""}
      <details><summary>${step.task ? "What the supplier wrote, and what to record" : "Full question"}${step.task ? "" : step.since ? `, asked ${html(at(step.since))}` : ""}${step.answer_by ? ` (answer requested by ${html(at(step.answer_by))})` : ""}</summary><p>${html(step.question)}</p></details>${form}</div>` : "";
  return {html: answer, advice};
}

function questionBlock(part, step) {
  return questionParts(part, step).html;
}

/* One line for the binding part: the open check (a simulated call is evidence of nothing), and when Dockside looks again. */
function blockerText(step) {
  const followUp = step.text.match(/Dockside (?:follows up at [^.]+|is following up now)\.?/)?.[0] ?? "";
  if (step.call_id) return `Departure and arrival are not confirmed; a real confirmation from the carrier is still missing (the call so far was simulated). ${followUp}`.trim();
  return step.text;
}

/* Lead with what the person must do now, in one sentence. */
function instruction(step, advice) {
  if (!step.person) return step.text;
  const ask = (step.detail ?? step.text.split(": ").slice(1).join(": ")).replace(/\s*Asked [A-Z][a-z]{2} \d\d:\d\d\.?$/, "");
  return `${step.person}: ${ask.charAt(0).toLowerCase() + ask.slice(1)}`;
}

function renderNextSteps(snapshot) {
  const all = snapshot.now_next ?? [];
  const yours = all.filter(item => item.next_step?.state === "Needs you");
  // Questions to Soony staff: a part's next step, or an open question row on its current delivery (newest per person).
  const team = all.filter(item => item.next_step?.person && item.next_step?.state !== "Needs you");
  for (const part of snapshot.components ?? []) {
    const route = part.routes?.find(candidate => candidate.id === part.default_route_id);
    for (const row of route?.steps ?? []) {
      if (!row.message_id || team.some(item => item.next_step.message_id === row.message_id || item.next_step.person === row.person)) continue;
      const card = yours.find(item => item.component_id === part.id);
      if (card) {  // questions on a part with a decision card belong on that card: its owner's question, or an escalation
        if (card.next_step.person === row.person) card.question ??= row;
        else (card.alsoAsked ??= []).push(row);
        continue;
      }
      const detail = row.detail.charAt(0).toLowerCase() + row.detail.slice(1);
      const first = team.find(item => item.component_id === part.id);
      if (first) (first.alsoAsked ??= []).push(row);  // the same decision escalated to someone else
      else team.push({component_id: part.id, next_step: {...row, state: "Waiting for reply", text: `Waiting for ${row.person}: ${detail}`}});
    }
  }
  const upcoming = all.filter(item => !yours.includes(item) && !team.includes(item));
  // A decision already shown as a conflict card (its order is disputed) is not listed a second time.
  const carded = new Set((snapshot.components ?? []).map(part => part.option?.deliveryState?.conflict && part.option.deliveryState.action_id).filter(Boolean));
  const waiting = (snapshot.decisions ?? []).filter(decision => decision.status !== "scheduled" && !carded.has(decision.id));
  const scheduled = (snapshot.decisions ?? []).filter(decision => decision.status === "scheduled");
  const key = JSON.stringify([all, [...calls.values()].map(call => [call.message_id, call.status, call.talk_url, call.result?.summary]), waiting.map(d => [d.id, d.title]), scheduled.map(d => [d.id, d.title, d.next_check_at])]);
  if (key === nextStepsKey) return;
  nextStepsKey = key;
  const count = waiting.length + yours.length;
  $("next-title").textContent = count ? `Action needed: ${count === 1 ? "1 thing" : `${number(count)} things`}` :
    team.length ? `Waiting on your team: ${team.length === 1 ? "1 decision" : `${number(team.length)} decisions`}` : "No spending approvals pending";
  $("next-strip").dataset.attention = count ? "true" : team.length ? "waiting" : "false";
  const needs = $("needs-you");
  const drafts = new Map([...needs.querySelectorAll("form[data-answer]")].map(form => [form.dataset.answer, form.querySelector("textarea").value]));
  const opened = new Set([...needs.querySelectorAll("[data-question] details[open]")].map(node => node.closest("[data-question]").dataset.question));
  // The part that limits dispatch comes first: what Dockside is checking, by when, and a link to its evidence.
  const binding = snapshot.components.find(component => component.id === snapshot.focusComponentId);
  const bindingStep = binding?.next_step;
  const blocker = binding && bindingStep && !yours.some(item => item.component_id === binding.id) ? [`<li class="blocker-line">
      <span class="state-chip" data-tone="alert">Limits dispatch</span>
      <button class="next-part" type="button" data-next-part="${html(binding.id)}">${html(nameOf(binding))}</button>
      <span>${html(blockerText(bindingStep))}</span></li>`] : [];
  needs.innerHTML = [...blocker, ...yours.map(item => {
      const part = snapshot.components.find(component => component.id === item.component_id);
      const conflict = part.option?.deliveryState?.conflict;
      if (conflict) return conflictCard(part, item, snapshot);
      return `<li><button class="next-part" type="button" data-next-part="${html(part.id)}">${html(nameOf(part))}</button>
        <span>${html(item.next_step.text)}</span>${callControls(item.next_step)}</li>`;
    }), ...waiting.map(decision => `<li><a href="#operations" data-jump="${html(decision.id)}">${html(decision.title)}</a>
      <span class="side-note">Waiting for ${html(decision.approver_name || "an approver")}</span></li>`),
    ...team.map(item => {
      const part = snapshot.components.find(component => component.id === item.component_id);
      const option = part.option;
      const clock = item === team.find(entry => entry.component_id === item.component_id) ? deliveryClock(option) : "";
      const step = item.next_step;
      const overdue = step.answer_by && Date.now() > Date.parse(step.answer_by);
      // The deadline that matters now: before the goods must leave; the time asked for in the message is history.
      const blockBy = option?.departBy && Date.now() < Date.parse(option.departBy) ? option.departBy : null;
      const due = blockBy ? ` <span class="clock-note">Needed before the goods leave${option.origin ? ` ${html(option.origin)}` : ""}: by ${html(at(blockBy))}.</span>`
        : step.answer_by && !overdue ? ` <span class="clock-note">Answer by ${html(at(step.answer_by))}.</span>` : "";
      const {html: answer, advice} = questionParts(part, step);
      return `<li><button class="next-part" type="button" data-next-part="${html(part.id)}">${html(nameOf(part))}</button>
        <span>${html(instruction(step, advice))}</span>${due}${item.alsoAsked?.length ? ` <span class="side-note">Escalated to ${html(item.alsoAsked.map(row => row.person).join(", "))} too.</span>` : ""}${clock ? ` <span class="clock-note">${html(clock)}</span>` : ""}${answer}</li>`;
    }),
    ...scheduled.map(decision => `<li><a href="#operations" data-jump="${html(decision.id)}">${html(decision.title)}</a>
      <span class="side-note">Goes ahead ${html(at(decision.next_check_at))} unless you stop it</span></li>`)].join("");
  needs.hidden = !count && !scheduled.length && !team.length && !blocker.length;
  for (const node of needs.querySelectorAll("[data-question]")) if (opened.has(node.dataset.question)) node.querySelector("details").open = true;
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
  const carded = new Set((snapshot.components ?? []).map(part => part.option?.deliveryState?.conflict && part.option.deliveryState.action_id).filter(Boolean));
  const decisions = (snapshot.decisions ?? []).filter(decision => !carded.has(decision.id));
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

if (typeof document !== "undefined") document.addEventListener("click", async event => {
  const choice = event.target.closest("[data-quick-answer]");
  if (!choice) return;
  const status = choice.closest("form").querySelector("[role=status]");
  choice.disabled = true;
  try { await sendAnswer(choice.dataset.quickAnswer, choice.dataset.text, status); }
  catch (error) { status.textContent = `Not sent. ${error.message}`; choice.disabled = false; }
});

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
