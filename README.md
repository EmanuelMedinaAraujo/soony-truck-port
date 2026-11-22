# Soony — Truck & Port agent (Lio Challenge, START Hack Munich 2026)

Agents that keep Grand Heist 6's inbound freight on track until Sunday 22 Nov 20:00.

- `sync/` — polls the Soony portal file listing (sha256), downloads only changed files, logs row-level change events.
- `docs/findings.md` — what we found in the data (contradictions, critical shipments, approval rules).

Set `SOONY_TOKEN` in `.env` and run `.venv/bin/uvicorn server.app:app --port 8765 --env-file .env` from the repository root. The server polls the portal and, when configured, the supplier mailbox. Open [Dockside](http://localhost:8765/) or [compare the four layouts](http://localhost:8765/v/).

The home view uses the artwork from Lio’s deep-dive presentation, slides 17–19. Select a component to trace it to dispatch and pick a shipment to see its route and last reported location. Decisions, recent activity and questions are in Actions on the same page. `/control-room` links there. The layouts share the stock-backed forecast and recovery scenarios from `GET /api/dispatch`. [Map coastlines by Natural Earth](https://www.naturalearthdata.com/downloads/110m-physical-vectors/110m-land/)

For a standalone portal sync, export `SOONY_TOKEN` and run `python3 sync/soony_sync.py --watch 90` with the web server stopped.
Customer data is never committed (`data/` is ignored).

## Supplier Chaser

Dockside's background loop checks launch-relevant purchase orders for missing, partial, late or conflicting confirmations. It shares the agent's outbound action log, reuses recent supplier inquiries for the same PO and sends at most one reminder on a thread it starts. Dockside reads the shared mailbox with read-only IMAP and checks replies locally. Escalations go to procurement through Discord unless an address is configured in `chase.value.internal_emails` using the names in `people`.

Preview the cases and exact outgoing messages with `.venv/bin/python -m core.chase --dry-run`. The preview uses the local snapshots and stored replies. The live loop runs in the Dockside service. Manage it with `chase.value.enabled` in `config/business.json`.

The native port is based on `integrations/supplier-chaser` at `cc0ecad`. The reference update to `0954ca4` adds Linux ARM64 packaging support.

## Voice calls

Released call messages run automatically. Dockside uses ElevenLabs to rehearse the call with a simulated counterpart based on the shipment records. If the simulator is unavailable, a local mock reports the known status and leaves unanswered questions open. Both appear in Actions → Calls with their transcript and script. Simulated replies can prompt follow-up and are excluded from shipment and stock evidence.

Set `ELEVENLABS_API_KEY` in `.env` to use the provider and enable **Try it live**. An optional telephone demo uses an imported ElevenLabs SIP number and requires both `voice.value.dial_out` and `voice.value.demo_number` in `config/business.json`. Dockside dials only that destination.

- `GET /api/calls` lists calls. Pass `?action_id=ACT-…` to filter them.
- `GET /api/calls/{message_id}` returns status, script, `talk_url` and the result with transcript, summary, fields and simulation label.
- `POST /api/calls/{message_id}/prepare` prepares or reuses the optional web-call agent.

## Spending approvals

Reversible inquiries can run automatically. Supplier purchase, dispatch and freight instructions require explicit approval, including zero-cost instructions. Dockside emails each assigned approver individually. Each link opens a confirmation page showing the instruction before recording a decision. Links expire after seven days or when the instruction is no longer useful. The existing stop window applies only to eligible actions without a supplier instruction.

Set `PUBLIC_URL` and the SMTP settings in `.env`. Add known recipients under `staff_email.value` in `config/business.json`. Set `demo_approval_aliases.value` to use Gmail plus-addresses for single-signer demos. Successful sends appear in action history and recent activity.

## Release gate

Dockside classifies the action kind, subject, draft and translation for signs of an instruction, independent of the proposed cost. Approval email, dashboard and Discord show the same typed terms that Dockside uses to render the supplier email. The terms bind the supplier and recipient, part, quantity, unit and total prices, currency, Incoterm and delivery place, PO line or emergency reference, delivery deadline, departure deadline when known, expiry and conditions to a commitment ID. Each signature records the SHA-256 digest of those terms. The supplier email carries the same terms block, commitment ID and digest. A draft that conflicts on quantity, price or currency is held. If a positive price uses a currency other than EUR, Dockside routes the decision to the highest approval tier.

For a repeat, reference the original signed and sent action with `repeat_action_id`. Dockside reuses its commitment ID, recipient and terms, and marks the message as a repeat of the existing instruction. Historical outbound instructions without signed typed terms need a fresh review before they can be repeated. A second initial instruction for the same commitment is held when the first was sent or delivery is uncertain.

The cycle syncs portal files and polls the supplier mailbox before dispatch. Both checks must have succeeded within `release_max_age_minutes`. A failed refresh or rebuild invalidates that source immediately. An ingest issue tied to the supplier, part, PO or delivery also makes the evidence stale. The gate checks the current supplier, offer, PO line and latest replies.

Send confirmation inquiries with the signed `delivery_id` and the relevant `confirmation_kind` (`book_truck` or `dock_slot`). The carrier or supplier logistics reply must confirm a booking reference and an ETA within the signed deadline. If the signed terms include `vehicle_limit_t`, the same reply must confirm the vehicle's gross weight rating, for example `Gross vehicle weight is 7.5 t.` The site lead or deputy must confirm a dock window that covers that ETA. Use an ISO date or weekday and 24-hour times in replies, for example `Carrier confirmed booking WT-123, ETA Sun 10:30` and `Dock slot confirmed for Sun 07:00–12:00`. A clock-only ETA uses the signed delivery date. A clock-only slot uses the carrier's ETA date. Times without a zone use the configured site timezone.

Set `release_carrier_contacts.value` to a map of delivery IDs to verified carrier email addresses. Use `release_supplier_logistics_contacts.value` to map supplier IDs to verified logistics addresses that can confirm that supplier's deliveries. Each map accepts one address or a list for each key. Dockside takes site contacts from the site lead and deputy roles in `people.value`. It uses their verified addresses in `staff_email.value` or the configured single-signer Gmail demo routes. The Warta Trans master record contains no email, so the live demo uses Agnieszka Nowak at Karton Nowak for logistics confirmations.

The rush option in `GET /api/dispatch` exposes `deliveryState` and `needsRevisedTerms`. The follow-up agent schedules the intervention by `act_by`, the last departure time. It chooses an amended arrival after the original deadline, at or after the verified ETA and by the receiving cutoff. The original deadline stays in effect until the release gate has the buyer's signature on the exact amendment and a fresh unload slot covering the ETA.

Dockside derives `depart_by` from the arrival deadline and the drive duration in the supplier offer or `release_drive_hours.value`, a map from delivery ID to drive hours. For an amendment to a historical order, the arrival limit for this calculation is the receiving cutoff. The amendment records the departure deadline in the terms the buyer signs. After that deadline, proposal, signature and every send attempt require timely departure evidence and a current ETA within the arrival window and confirmed dock slot. An authorized carrier or supplier logistics contact can reply on the linked transport thread with “Truck departed at 06:40. ETA Sun 14:30.” An in-transit carrier position linked to the booking or delivery can also establish departure if it was observed by the deadline.

Keep the dashboard bound to loopback behind Tailscale Serve. Map verified logins under `staff_accounts.value`, Discord user IDs under `staff_discord_accounts.value`, and separately controlled mailboxes under `staff_email.value`. A joint approval waits for every assigned signer. To enable local demo approval, set `demo_local_operator.value` to one assigned person's name. Paid commitments are delivered by email.

## Release gate replay

```sh
.venv/bin/python examples/replay_gate.py
```

This offline replay reconstructs the historical rush offer and the 02:07 supplier acceptance in temporary SQLite databases. It uses example email addresses, a fixed launch clock and an in-memory SMTP capture. The buyer signatures, carrier booking and receiving reply are replay inputs. The second sequence runs the actual follow-up with a mocked model. It uses the 14:30 ETA to propose a 15:00 arrival to the assigned buyer and requests a new slot. The 07:01 retry holds even with a slot. A linked carrier reply verifying departure at 06:40 and a current ETA of 14:30 clears the hold. The order keeps its historical emergency reference while the buyer's PO number is outstanding.

```text
ACT-F74B32 · Karton Nowak · 8,000 art books · €16,800 DAP Hamburg

Dockside commitment
{
  "commitment_id": "COM-B14FD3555F7513A5C757",
  "conditions": [
    "dock_slot_confirmed",
    "transport_confirmed"
  ],
  "currency": "EUR",
  "deliver_by": "2026-11-22T12:00:00+01:00",
  "delivery_id": "ART-BOOKS-20261122",
  "delivery_place": "Hamburg",
  "depart_by": "2026-11-22T03:00:00+01:00",
  "emergency_reference": "ACT-F74B32",
  "incoterm": "DAP",
  "instruction": "order",
  "language": "en",
  "latest_useful_at": "2026-11-22T03:00:00+01:00",
  "part": "CMP-GAM-008",
  "po": null,
  "po_line": null,
  "quantity": 8000,
  "recipient": "orders@karton-nowak.example",
  "supplier_id": "SUP-0028",
  "total_price": 16800.0,
  "unit_price": 2.1,
  "valid_until": "2026-11-22",
  "version": 1
}
Approval digest: d9776b74eacb26735f8ee2ac3c1b1719c29129114426e6b15b639837be3cd143

00:14 HELD — verified approval for these exact terms is missing
  Carrier booking and ETA: no linked reply from an authorized contact
  Dock slot: no linked reply from an authorized contact

02:07 HISTORICAL EVIDENCE — Karton Nowak accepted the written €16,800 order and released the art books to its logistics team.
HELD — newer supplier reply needs review of quantity, price and PO
  Carrier booking and ETA: no linked reply from an authorized contact
  Dock slot: no linked reply from an authorized contact

02:08 BUYER REVIEW AND SIGNATURE — HELD — required condition missing: transport confirmed (no linked reply from an authorized contact)
  Carrier booking and ETA: no linked reply from an authorized contact
  Dock slot: no linked reply from an authorized contact

02:09 RELEASED WITH SYNTHETIC EVIDENCE
  Carrier confirmed booking WT-123, ETA Sun 10:30
  Slot 07:00–12:00 confirmed by Katrin Jansen
X-Dockside-Terms-SHA256: d9776b74eacb26735f8ee2ac3c1b1719c29129114426e6b15b639837be3cd143
Exact outbound body
Purchase and dispatch according to these terms.

Dockside commitment
{
  "commitment_id": "COM-B14FD3555F7513A5C757",
  "conditions": [
    "dock_slot_confirmed",
    "transport_confirmed"
  ],
  "currency": "EUR",
  "deliver_by": "2026-11-22T12:00:00+01:00",
  "delivery_id": "ART-BOOKS-20261122",
  "delivery_place": "Hamburg",
  "depart_by": "2026-11-22T03:00:00+01:00",
  "emergency_reference": "ACT-F74B32",
  "incoterm": "DAP",
  "instruction": "order",
  "language": "en",
  "latest_useful_at": "2026-11-22T03:00:00+01:00",
  "part": "CMP-GAM-008",
  "po": null,
  "po_line": null,
  "quantity": 8000,
  "recipient": "orders@karton-nowak.example",
  "supplier_id": "SUP-0028",
  "total_price": 16800.0,
  "unit_price": 2.1,
  "valid_until": "2026-11-22",
  "version": 1
}

02:09 TAMPERED DRAFT (80,000 art books) — HELD — draft quantity disagrees with the commitment

03:20 HISTORICAL ORDER BEFORE CARRIER REPLY
The ordered 12:00 arrival can no longer be met. A later arrival (latest 16:00 receiving cutoff, leave Poznań by 07:00) needs revised terms with Karton Nowak, a carrier ETA and a new unload slot.
  Carrier booking [waiting] Awaiting a booking reference from Warta Trans or the supplier's authorized logistics contact.
  Arrival time [blocked] Awaiting a verified carrier ETA. Ordered arrival Sun 12:00. The ordered 12:00 arrival can no longer be met. A later arrival (latest 16:00 receiving cutoff, leave Poznań by 07:00) needs revised terms with Karton Nowak, a carrier ETA and a new unload slot.
  Unload slot [waiting] Sun 07:00–12:00 confirmed by Katrin Jansen · Sun 00:14. Slot window is awaiting a confirmed carrier ETA.
  Vehicle [waiting] Awaiting carrier confirmation that the vehicle is rated ≤7.5 t gross.
  needsRevisedTerms=true

03:21 AUTHORIZED CARRIER REPLY
  Carrier booking [confirmed] Booking WT-123 confirmed by bookings@warta-trans.example · Sun 03:21.
  Arrival time [blocked] ETA Sun 14:30 from bookings@warta-trans.example · Sun 03:21. Ordered arrival Sun 12:00. Verified ETA Sun 14:30 is after the ordered 12:00 arrival. A later arrival (latest 16:00 receiving cutoff, leave Poznań by 07:00) needs revised terms with Karton Nowak, a carrier ETA and a new unload slot.
  Unload slot [blocked] Sun 07:00–12:00 confirmed by Katrin Jansen · Sun 00:14. Slot window does not cover the confirmed carrier ETA.
  Vehicle [confirmed] Rated 7.5 t gross, confirmed by bookings@warta-trans.example · Sun 03:21. Order limit ≤7.5 t.
  needsRevisedTerms=true
03:21 FOLLOW-UP PROPOSED — act by 07:00, arrive by 15:00, receiving cutoff 16:00
03:21 AMENDMENT SIGNED — HELD — required condition missing: dock slot confirmed (no linked reply from an authorized contact)

07:01 RETRY WITH CONFIRMED SLOT — HELD — departure deadline passed
Verify the truck departed by the deadline and provide a current ETA within the delivery window and confirmed dock slot.

07:01 REVISED TERMS RELEASED — verified departure Sun 06:40, ETA Sun 14:30
  Carrier booking [confirmed] Booking WT-123 confirmed by bookings@warta-trans.example · Sun 07:01.
  Arrival time [confirmed] ETA Sun 14:30 from bookings@warta-trans.example · Sun 07:01. Ordered arrival Sun 15:00.
  Unload slot [confirmed] Sun 14:00–15:00 confirmed by Katrin Jansen · Sun 07:01.
  Vehicle [confirmed] Rated 7.5 t gross, confirmed by bookings@warta-trans.example · Sun 07:01. Order limit ≤7.5 t.
  needsRevisedTerms=false
```

## Second customer in a day

Run the same typed ingest, detectors and dispatch forecast for Kestrel Toys' Robot Builder Set before its Rotterdam departure on December 12 at 06:00 local time.

```sh
.venv/bin/python examples/run_customer.py examples/kestrel
```

Each run uses a temporary working tree and SQLite database with a fixed replay clock. The runner reads the checked-in document facts and performs no network calls. Pass `--through 2` to stop after the second drop.

To set up another replay, copy the structure of [Kestrel's mapping](examples/kestrel/business.json). Review the site, deadline, shifts, priority edition and cost per missing unit against the customer's operating policy. In `sources.value.paths`, map each source to its file path relative to `raw/` or to the glob for a set of files. Supplier profile and catalog paths are relative to each supplier's folder. In `sources.value.columns`, map canonical field names to the headers in the exports. Unmapped headers use their canonical names. The launch-status input accepts CSV or XLSX and matches edition IDs against the product catalog. Put the initial exports in `raw/`, reviewed document facts in `doc_facts/` and each subsequent set of changed files in `drops/<number>/`, using the original relative paths. The replay applies each drop on top of the prior one.

- **Drop 1** finds a receipt booked as 500 motors against an order for 50. It excludes 450 from usable stock. Deluxe is 30 short and Classic is 100 short.
- **Drop 2** adds a verified receipt of 100 motors and brings the remaining truck ETA forward. Deluxe is fully covered and Classic is 30 short. The on-time truck's 30 motors are still in transit.
- **Drop 3** adds an independent carrier manifest for the same booking. It reports 12 motors against the truck board's 30. Dockside shows both claims and holds the shipment for review. The confirmed forecast stays at 0 Deluxe and 30 Classic short.
