# Soony — Truck & Port agent (Lio Challenge, START Hack Munich 2026)

Agents that keep Grand Heist 6's inbound freight on track until Sunday 22 Nov 20:00.

- `sync/` — polls the Soony portal file listing (sha256), downloads only changed files, logs row-level change events.
- `docs/findings.md` — what we found in the data (contradictions, critical shipments, approval rules).

Set `SOONY_TOKEN` in `.env` and run `.venv/bin/uvicorn server.app:app --port 8765 --env-file .env` from the repository root. The server polls the portal and, when configured, the supplier mailbox. Open [Dockside](http://localhost:8765/) or [compare the four layouts](http://localhost:8765/v/).

The home view uses the artwork from Lio’s deep-dive presentation, slides 17–19. Select a component to trace it to dispatch and pick a shipment to see its route and last reported location. Decisions, recent activity and questions are in Actions on the same page. `/control-room` links there. The layouts share the stock-backed forecast and recovery scenarios from `GET /api/dispatch`. [Map coastlines by Natural Earth](https://www.naturalearthdata.com/downloads/110m-physical-vectors/110m-land/)

For a standalone portal sync, export `SOONY_TOKEN` and run `python3 sync/soony_sync.py --watch 90` with the web server stopped.
Customer data is never committed (`data/` is ignored).

## Supplier Chaser

Dockside's background loop reads confirmation evidence and tracks supplier inquiries in its action log. Preview cases and mail with `.venv/bin/python -m core.chase --dry-run`. Live chasing is disabled by default. Enable it in `config/business.json` and configure `chase.value.internal_emails` using the names in `people` to route escalation emails.

The chase loop is ported from `integrations/supplier-chaser` (commit `cc0ecad761f5c02f40be0afa46360d270c57038b`). To pull teammate changes, update the submodule and port the diff.
