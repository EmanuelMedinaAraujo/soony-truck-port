# Brief for Dockside UI prototypes

## Product
Dockside is an autonomous AI agent for a fictional games publisher, Soony. Its game "Grand Heist 6" launches Sunday 22 Nov 2026; every copy is
packed at the distribution center in Hamburg (DC-HAM) and the trucks to retail leave **Sunday 20:00**. Dockside watches containers, air freight and
trucks, prices every problem in Collector's Edition copies (a missing one is refunded at €120) and **acts on its own**:
- free actions (asking suppliers, confirming terms, reserving, flagging data errors, escalating) are done immediately → status `auto`;
- commitments within policy (orders, trucks, dock slots) are **scheduled** and executed after a 15-minute window unless the named person presses
  **Stop** (or **Do it now** to execute immediately) → status `scheduled`;
- only written releases (quality concessions) wait for a signature → status `proposed` ("Approve and send" / "Decline").
Humans mostly *watch and veto*. Users on a stressful Sunday: Raj Patel (logistics planner, desk), Katrin Jansen (site lead, dock floor, phone),
Dr. Maren Vogt (head of procurement, between meetings).

## API (server runs at http://localhost:8765; same origin, use relative URLs)
- `GET /api/state` → `{ now, deadline, cutoff_hours, priority, picture{FG-GH6-CE, FG-GH6-STD: packed_est_now, demand, todo_now, hours_left, slack_copies, rate},
  headline{ce_copies, eur, refund_per_copy, binding{component,name}}, coverage[{component, description, need_total, usable_now, gap, suspect_master_data, adjustments[{kind,cut,detail}]}],
  inbound[{shipment_id, mode sea|air|road, status, reference, material_names[], quantity, arrival_at_dc|null, makes_cutoff, flags[], origin, destination}], inbound_later,
  findings[{id, severity, title, detail, evidence[]}], actions[{id, title(=what), rationale(=why), kind, cost_eur, eur_protected, ce_copies, approver_name, approver_role,
  status proposed|scheduled|escalated|approved|sent|auto|rejected, next_check_at, history[{ts,entry}], payload{details}, messages[{channel, to_name, to_address, language, subject, body, body_en, status draft|outbox|sent|delivered}]}],
  events[{ts, kind, text}], system{busy, last_sync, files, docs_read, conflicts} }`. Run `curl -s localhost:8765/api/state | python3 -m json.tool | less` to see real data.
- `POST /api/actions/{id}/decide` body `{"decision":"approve"|"reject","by":"<name>"}` — approve = "Do it now"/"Approve", reject = "Stop"/"Decline".
  **Do not call it on real data while testing** (it changes shared state); stub `fetch` in the console to test the flow.
- `POST /api/chat` body `{"q": "..."}` → `{answer, steps[]}` (20–90 s; show progress). Test at most once.
- Poll `/api/state` every 15 s. `system.busy` is a live progress string ("agent planning: step 7, reading documents") — show it.

## Must contain (any form)
Countdown to Sunday 20:00 · money at risk and what runs out first · what Dockside will do next and who can stop it (scheduled, with time) ·
anything needing a signature · what Dockside already did on its own · which shipments make it before the trucks leave (receiving closes 16:00;
German Sunday truck ban 00:00–22:00 for trucks > 7.5 t) · ask Dockside a question · agent progress/live status.
Copy rules: plain words, no part codes (use material_names / plain names), sentence case, active voice, CTAs name what happens.

## Design brief (binding, from the client)
- A distinct visual identity grounded in the subject (logistics, ports, the launch); not templated. Spend boldness in one place.
- Hero = the most characteristic thing in the subject's world; avoid "big number + small label + stats + gradient".
- Typography carries personality; deliberate typefaces (not Inter/Roboto/system defaults), clear scale, line length < 80ch.
- Avoid: accenting one word in a headline; ALL-CAPS labels; unnecessary labels/eyebrows; numbered markers unless a real sequence; SaaS card kit
  (identical rounded cards, same soft shadow, gradient washes); middle-dot meta strings; 'WORD — fragment' labels; monospace for small data labels;
  '→' on buttons; near-black #111 stand-ins; cream + terracotta; near-black + one acid accent.
- One orchestrated motion moment at most; motion otherwise only answers user actions; respect prefers-reduced-motion.
- Quality floor: responsive to 375 px, visible keyboard focus, accessible contrast, light + dark (prefers-color-scheme + :root[data-theme]), explicit body background.
- Errors say what happened and how to fix it; empty states invite action.
- Process: write a short token plan (4–6 named hex colors, typefaces + roles, ASCII layout, principles) at the top of the file in an HTML comment,
  review it against the brief, then build. Critique with screenshots, iterate twice, then remove one accessory.
