let queueKey = "";
let activityKey = "";
const pendingDecisions = new Set();

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
  $("attention").textContent = decisions.length ? `${number(decisions.length)} ${decisions.length === 1 ? "open action" : "open actions"}` : "No decisions needed right now.";
  const next = JSON.stringify(decisions);
  if (next !== queueKey && pendingDecisions.size === 0) {
    queueKey = next;
    $("decision-list").innerHTML = decisions.map(decisionItem).join("");
  }
  const activity = JSON.stringify(snapshot.activity);
  if (activity !== activityKey) {
    activityKey = activity;
    $("activity-list").innerHTML = snapshot.activity.length ? snapshot.activity.map(event =>
      `<li><time datetime="${html(event.ts)}">${html(at(event.ts))}</time> ${html(event.text)}</li>`).join("") : "<li>No activity recorded yet.</li>";
  }
}

function initOperations(reload) {
  const decisionResult = $("operation-result");
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
