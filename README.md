# Soony — Truck & Port agent (Lio Challenge, START Hack Munich 2026)

Agents that keep Grand Heist 6's inbound freight on track until Sunday 22 Nov 20:00.

- `sync/` — polls the Soony portal file listing (sha256), downloads only changed files, logs row-level change events.
- `docs/findings.md` — what we found in the data (contradictions, critical shipments, approval rules).

Setup: `cp .env.example .env` and set `SOONY_TOKEN`, then `python3 sync/soony_sync.py --watch 90`.
Customer data is never committed (`data/` is ignored).
