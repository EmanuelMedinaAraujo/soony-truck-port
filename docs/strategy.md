# How we win — Truck & Port agent ("Dockside", working name)

## The one-sentence pitch
**"We find out at the dock" → the dock finds out from us.** An agent that watches every container, plane and truck heading
to Hamburg, prices each one in Collector's Editions and euros, and gets the *right person* to approve the *right fix*
in one tap — before the CE cell runs dry.

## Why this wins on each judging criterion
| Criterion | What we show |
|---|---|
| Pitch that sticks | A real moment: *"350,000 cases have been sitting at the CTA terminal since Thursday. Nobody booked a truck. The spreadsheet still says 'in transit'."* Then: *"every hour the CE cell waits costs €108,000"* (900 copies/h × €120 refund — from our interview). |
| Business case | €708k of CE refunds at risk this weekend alone (5,900 art books). Our fix costs €16.8k + a van → buyer-level signature. ROI > 40×. Beyond Soony: every hard-deadline launch (toys before Christmas, consumer electronics, pharma, automotive JIT where a line stop costs €20k+/min). |
| Technical depth | sha256 change feed → deterministic rebuild of a provenance-tracked master DB → rules engine (numbers) → LLM agents (read multilingual docs, decide, draft, route approval by signature ladder, follow up, escalate). Nothing hardcoded: interview facts live in `config/business.json` with sources. Contradictions are kept and surfaced, never silently overwritten. |
| Taste | Not dashboard #41. One screen: **€ at risk meter + 3 decisions + arrival timeline vs. Sunday cut-off**. Push to the person's phone (Discord) with Approve / Reject. |

## Demo storyline (3 min)
1. **Problem (30 s)** — 72k numbered CEs, €120 refund each, CE cell has ~2 h slack. Truth lives in ERP + a spreadsheet called `v37_FINAL_final2` + PDFs in Korean and Polish, and they disagree.
2. **Live (2 min)**
   - Open Dockside: *"5,900 CE copies at risk — €708k"*.
   - Card 1: **Art books** — the ERP says 91k in stock; the agent read the Polish delivery note: someone typed 80.000, the dock corrected it to 8 000 by hand. Karton Nowak has 8,000 in stock (Polish offer, translated). Sunday truck ban → van ≤ 7.5 t or leave Saturday. Cost €16.8k → **Buyer Mei Lin can sign** (CE category lead is vacant — the agent knows). Tap *Approve* in Discord → agent emails the supplier (teammate's supplier channel) → follows up.
   - Card 2: **Container NHSU4471921 at CTA** — book drayage, request dock slot from Katrin Jansen. Port-to-DC is exempt from the Sunday ban.
   - Card 3: **Air game cards**: only 28k of 48k flew (Korean shipping advice). The agent recomputes: CE is still covered because CE gets cards first; Standard short — *not worth air-freight money* (Standard sells on Tuesday). Judgment, not just alerts.
   - **Live drop**: a file changes on the portal → within 90 s the agent re-plans and pings the owner.
3. **Business (30 s)** — pricing per launch / per site, path to the next 10 customers.

## Product decisions
- **Dashboard + Discord, same brain.** The web "control room" is for the planner (Raj); Discord is where approvals and
  "what if" questions happen on a stressful Sunday (Katrin on the dock, Maren on her phone). Every action, approval and
  message is in one action log.
- **Follow-up loop = real agent work.** Each action has owner, deadline and next check. No reply or no data change →
  re-ping → escalate one tier up the signature ladder (vacancy-aware).
- **What-if via chat**: "What if the Polish truck is 3 h late?" → agent runs the rules engine with a modified ETA and answers in copies and euros.

## Architecture
```
portal ──sha256 poll──▶ sync (blobs, change events)
                          │
                          ▼
           ingest → master SQLite (provenance, observations, conflicts)
                          │                 ▲
   doc reader agent ──────┘ (facts from PDFs/scans, cached by sha256)
                          ▼
                rules engine (coverage, arrival vs cut-off, € impact, approver)
                          ▼
     action agent (LLM + tools: query, what-if, draft, request approval, send, follow-up)
           │                         │
     web control room          Discord bot (push, approve, chat)
```

## Open questions for office hours (Sun 09:00, Felix Brunner)
- How many dock doors / slots at DC-HAM on Sat evening & Sunday? Who books drayage from CTA?
- Do you use vans or permits for Sunday deliveries? (Sunday truck ban)
- How do carriers tell you about delays today — portal, email, phone?
- Approval ladder: interview said CEO + CPO above €5M, PP-07 says CPO + CFO. Which is right?

## Known data traps (also a pitch asset: "how we handle messy data")
See `findings.md`. Key: synthetic ship positions on land; tracker misses all air/road; 80.000 vs 8 000; sleeve revisions
differ per supplier (our first analysis got this wrong — the per-batch provenance model caught it).
