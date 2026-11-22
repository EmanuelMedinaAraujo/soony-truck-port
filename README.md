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

Dockside classifies the action kind, subject, draft and translation for signs of an instruction, independent of the proposed cost. Approval email, dashboard and Discord show the same typed terms that Dockside uses to render the supplier email. The terms bind the supplier and recipient, part, quantity, unit and total prices, currency, Incoterm and delivery place, PO line or emergency reference, delivery deadline, expiry and conditions to a commitment ID. Each signature records the SHA-256 digest of those terms. The supplier email carries the same terms block, commitment ID and digest. A draft that conflicts on quantity, price or currency is held. If a positive price uses a currency other than EUR, Dockside routes the decision to the highest approval tier.

For a repeat, reference the original signed and sent action with `repeat_action_id`. Dockside reuses its commitment ID, recipient and terms, and marks the message as a repeat of the existing instruction. Historical outbound instructions without signed typed terms need a fresh review before they can be repeated. A second initial instruction for the same commitment is held when the first was sent or delivery is uncertain.

The cycle syncs portal files and polls the supplier mailbox before dispatch. Both checks must have succeeded within `release_max_age_minutes`. A failed refresh invalidates that source immediately. The gate checks the current supplier, offer, PO line and latest replies. A carrier or dock confirmation must come through a sent message linked to the same `delivery_id`. `GET /api/dispatch` exposes the signature digest, hold reason, evidence needed to clear it and the person or team responsible. Expired and superseded decisions have no approval controls.

Keep the dashboard bound to loopback behind Tailscale Serve. Map verified logins under `staff_accounts.value`, Discord user IDs under `staff_discord_accounts.value`, and separately controlled mailboxes under `staff_email.value`. A joint approval waits for every assigned signer. To enable local demo approval, set `demo_local_operator.value` to one assigned person's name. Paid commitments are delivered by email.

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
