# Soony — Truck & Port agent (Lio Challenge, START Hack Munich 2026)

Agents that keep Grand Heist 6's inbound freight on track until Sunday 22 Nov 20:00.

- `sync/` — polls the Soony portal file listing (sha256), downloads only changed files, logs row-level change events.
- `docs/findings.md` — what we found in the data (contradictions, critical shipments, approval rules).

Set `SOONY_TOKEN` in `.env` and run `.venv/bin/uvicorn server.app:app --port 8765 --env-file .env` from the repository root. The server polls the portal and, when configured, the supplier mailbox. Open [Dockside](http://localhost:8765/) or [compare the four layouts](http://localhost:8765/v/).

The home view uses the artwork from Lio’s deep-dive presentation, slides 17–19. Select a component to trace it to dispatch and pick a shipment to see its route and last reported location. Decisions, recent activity and questions are in Actions on the same page. `/control-room` links there. The layouts share the stock-backed forecast and recovery scenarios from `GET /api/dispatch`. [Map coastlines by Natural Earth](https://www.naturalearthdata.com/downloads/110m-physical-vectors/110m-land/)

For a standalone portal sync, export `SOONY_TOKEN` and run `python3 sync/soony_sync.py --watch 90` with the web server stopped.
Customer data is never committed (`data/` is ignored).

## Supplier Chaser

Install Pixi, then run `scripts/update_chaser.sh` from the repo root. It follows the submodule’s `main` branch, prints the new commits, installs the locked environment, migrates its local PostgreSQL database, and restarts Chaser’s API and worker. Use `scripts/run_chaser.sh` to start Chaser again after a reboot. Logs are in `data/chaser/`.

Open [Supplier Chaser](http://127.0.0.1:8000/) and sign in with the `ADMIN_TOKEN` from its private `integrations/supplier-chaser/.env`. Set `CHASER_URL=http://127.0.0.1:8000` and `CHASER_TOKEN` to that token in Dockside’s private `.env`. Dockside displays the open case count and matches cases to the selected part’s purchase orders. Sign in to Chaser before opening a case link.

A fresh Chaser workspace uses its demo data. For the Soony feed, follow the submodule’s [portal setup](integrations/supplier-chaser/docs/integration-contracts.md#portal-setup) and review imported source mappings in Chaser. Use Dockside’s portal base URL and team token in Chaser’s private environment. The launcher uses Chaser’s fixture mailbox for local drafts and replies.
