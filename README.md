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
