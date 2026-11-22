# Dockside and the Sunday dispatch

## The story in 30 seconds

At the kickoff, Soony's Head of Procurement told us: *"We find out at the dock."* Hamburg books what is on the paper, and *"half of what we know is in a PDF somebody scanned."*

That is what happened. A delivery note for 8,000 art books was booked as **80,000**, so the stock list looked full. In reality **5,900 numbered Collector's copies** could not be packed. Soony refunds €120 for each one, so **€708,000** was at stake, and nobody saw it until Saturday.

Dockside found the extra zero by matching the receipt against the purchase order and the scanned note. It then found a Polish supplier offer for 8,000 ready art books, worked out that they could add **2,500 copies** before statues become the next limit, sent the formal order, and asked Hamburg for an unload slot. Everything it did is in the action log. The only decisions it hands to a person are the ones that cost money.

**The product:** an agent that keeps a launch's supply on track. It reads every file, scan and email in any language, finds what will stop boxes from leaving, and works the problem itself: it asks suppliers, chases confirmations, places calls, books unload slots, and escalates when only a person can help.

## The case for paying

| Decision input | Soony launch scenario |
|---|---:|
| Collector’s copies at risk in the stock-backed forecast | 5,900 |
| Extra Collector’s copies if 8,000 art books arrive usable in time | 2,500 |
| Refunds those 2,500 copies could avoid | €300,000 |
| Cost of the rush offer | €16,800 |
| Refunds avoided less purchase cost if the plan lands in time | €283,200 |
| Collector’s gap that still remains because of statues | 3,400 |

The result is a conditional forecast. The supplier is holding stock while it checks transport. A receiving slot and formal order are still open. The stock-backed view takes no credit for the offer while these steps are open.

The illustrated trace can treat confirmation as a scenario input. A future handoff would pass a verified, matching supplier claim keyed by purchase order, line and supplier, with committed quantities, delivery time and evidence. Dockside can then follow the carrier, dock slot and receipt against the same commitment. [Negotiator’s confirmation model](https://github.com/ManuelLerchner/HACKLeague2026-lio/blob/main/src/supplier_chaser/domain/models.py)

An illustrative €3,000 standby van would break even if it improved the chance of getting those copies onto the trucks by at least **one percentage point**. Both options still need a dock slot, an authorized order and usable receipt. The buyer supplies a real premium or sees the threshold for the example. Dockside shows the latest viable release time from the offer lead times and displays the evidence the estimate uses.

**Who pays and how much.** The buyer is the Head of Procurement, Dr. Maren Vogt's role. The users are the buyers and the site lead in Hamburg. A pilot is **€5,000 for one launch at one site**: less than one hour of the exposure above, priced below the level that needs a tender. After the pilot comes a fee per critical period (launch, ramp-up, peak season) plus a share of the losses avoided, which the backtest below measures. This is a hypothesis to test with Soony. We have not yet seen a price reaction.

**Proof before the pitch: the backtest.** Dockside keeps every file version and event, so it can replay a launch. For a prospect we replay their last launch and report when Dockside would have flagged each problem, how many buyer hours of chasing it would have replaced, and the value of the actions that were still possible at that moment. For Soony, the rush offer stayed useful only until Sunday 03:00, so every hour of earlier warning counted.

**The next ten customers** share three traits: a fixed date retail will not move, products assembled from many parts that share supply, and suppliers who report by PDF and email in several languages.
- Game publishers and their packing partners: special and collector's editions every quarter.
- Toy makers before Christmas: multi-part kits with a fixed shelf date.
- Consumer electronics launches: accessories and packaging from many suppliers.
- Trading cards and collectibles: numbered runs with refunds per missing unit.
- Contract packers and fulfilment centres: one site serving many brands, so the onboarding cost is paid once.

## The platform: compile an operation once, then let the agent run it

We pitch a horizontal platform: an onboarding agent for any deadline-driven supply chain. Soony is its first compiled customer. The launch cockpit is one instance of the platform, not the product.

| Layer | Today in Dockside | Generic? |
|---|---|---|
| Change detection (file hashes, versions, row events) | `sync/` | Yes, any file source |
| Raw ingest of every CSV, XLSX and JSON | `raw_*` tables | Yes |
| Reading documents in any language into facts | `core/extract_docs.py` | Yes |
| Operating rules (who signs what, deadline, cost per missing unit, autonomy) | `config/business.json`, every value with its source | The structure is generic; the content is per customer |
| Detectors (stuck at port, ETA against route time, revision mismatch, over-receipt, qualification expiry, conflicting shipment quantity, truck restrictions) | `core/rules.py` | Yes, with customer policy and source evidence |
| Typed loaders (BOM, POs, shipments, editions) | `core/ingest.py` + `sources` in the policy file | Yes, for the mapped export formats |
| Cockpit | `web/assembly` | No: becomes a generated view spec |

**Compile once.** From a data dump, policy documents and a few interviews, the onboarding agent writes three reviewable files:
- `mapping.yaml`: raw tables mapped to a canonical model (part, bill of materials, supplier, purchase order, shipment, stock, site, deadline, role). The agent asks only about matches it is unsure of.
- `policy.json`: what `business.json` is today, including the value function. For Soony that is €120 refunded per missing Collector's copy. For a carmaker it would be the cost of a minute of line stop; for pharma, the value of a spoiled batch.
- `views.json`: the cockpit, composed from a fixed set of about 12 components, never free-form HTML.

**Fixed runtime.** Deterministic code runs the mapping, the detectors and the renderer. The model never computes the numbers. It writes specs and proposes actions, and each action is logged, scheduled within a stop window, or sent to the person who must sign.

**Then evolve.** The same action log that makes Dockside's emails, calls and bookings auditable also records view changes. The agent can pin an answer as a widget or make a view for a role, and anyone can undo the change.

**Why it compounds.** Every new customer adds to a cross-customer record of supplier reliability, for example how fast a supplier answers and how far its ETAs slip. Every customer's file history also supports a backtest: replay their last launch and show which problems Dockside would have flagged earlier, and what that would have saved. That backtest is the sales argument and the basis for pricing as a share of avoided losses.

For Sunday we show the platform through what already runs generically: hash-based change detection, multilingual document reading, a policy with cited sources, autonomous follow-ups, Supplier Chaser and voice calls. The cockpit stays hand-polished.

## Proof: a second customer

Kestrel Toys packs the Robot Builder Set in two editions at Rotterdam. The same ingest, detectors and dispatch forecast read its own SKU codes, packing schedule, source paths and column names from a [reviewed mapping](../examples/kestrel/business.json). The deadline is December 12, 2026 at 06:00 local time.

| Replay | Deluxe projected | Classic projected | Refunds at risk | Biggest lever | Inbound booking RD-201 |
|---|---:|---:|---:|---|---|
| Drop 1 | 70/100, 30 short | 40/140, 100 short | €2,460 | 50/180 drive motors usable | 30 motors after cutoff |
| Drop 2 | 100/100, 0 short | 110/140, 30 short | €450 | 150/180 drive motors usable | 30 motors by cutoff |
| Drop 3 | 100/100, 0 short | 110/140, 30 short | €450 | 150/180 drive motors usable | Held for review. Truck board says 30, carrier manifest says 12. |

Drop 2 changes three files and the verified receipt adds 100 motors to the forecast. Drop 3 changes one file and changes the next step from “On the way” to “Needs you”. Both source claims stay visible and the unsettled quantity stays out of the plan. Every drop ingests with zero issues. The original Soony rebuild still matches the saved typed rows and the dispatch forecast of 5,900 Collector's and 34,800 Standard copies short, with the rush offer adding at most 2,500 Collector's copies and avoiding €300,000 in refunds.

## The three-minute demo

1. Open the dependency overview and select Art Book. The buyer sees the active path from supplier stock through transport and receiving into Collector’s dispatch. The shared game card appears once across both editions.
2. Open the evidence and show the corrected goods receipt, the real supplier reply and the checks still open. Then compare the rush offer and standby premium. The art books unlock at most 2,500 copies under the current model because statues become the next constraint.
3. Show the action already taken and the live decision queue. An email records that Dockside asked. A reply records what the supplier actually confirmed. A verified receiving record changes the supply picture. When a real approval is required, show who can act and by when.

## What the jury can verify

| Criterion | Live proof |
|---|---|
| A pitch that sticks | Follow a single art book from a corrected receipt to one more finished game on the Sunday truck. |
| A real business case | Compare verified offer cost with marginal refund exposure and show the effect of the next bottleneck. Apply the same calculation to a seasonal retail launch. |
| Technical depth | Pull source changes by hash, retain evidence for receipt and inspection corrections, show real multilingual supplier replies and real outbound activity. Recompute forecasts from the shared bill of materials and the current packing clock. |
| Taste | Keep the overview quiet. Select a part to open the exact path, evidence and decision. Keep the work Dockside is doing visible without asking the buyer to manage it. |

The judging guide calls for a live prototype, repo, deck and backup video of up to three minutes by Sunday at 12:00. The stage slot is three minutes plus two minutes of questions.
