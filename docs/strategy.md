# Dockside and the Sunday dispatch

## The story in 30 seconds

*"We find out at the dock,"* Soony's Head of Procurement told us. On Saturday Dockside found an 8,000-book receipt booked as **80,000**: 5,900 Collector's copies short, €708,000 in refunds. It ordered 8,000 replacements, never counted them as stock, and tracked the supplier. At 02:07 the supplier accepted; at 04:17 it wrote it had no dispatch instruction. Dockside held the amendment and gave the buyer one task: settle it with the supplier.

*(The figures above are the Saturday replay; the live board recalculates them from every new file, receipt and reply.)*

Everything it did is in the action log. It hands a person only what needs a person: spending, signed on the exact terms, and facts only Soony can give.

**The product:** an agent that keeps a launch's supply on track. It reads every file, scan and email in any language, finds what will stop boxes from leaving, and works the problem itself: it asks suppliers in their language, chases confirmations, requests unload slots from the site, prepares and runs calls (simulated until a phone line is connected), and escalates to the right person when only a person can help. Anything that costs money waits for the assigned approver to sign the exact terms.

## The case for paying

| Decision input | Soony launch scenario |
|---|---:|
| Collector’s copies at risk in the stock-backed forecast | 5,900 |
| Extra Collector’s copies if 8,000 art books arrive usable in time | 2,500 |
| Refunds those 2,500 copies could avoid | €300,000 |
| Cost of the rush offer | €16,800 |
| Refunds avoided less purchase cost, **only if** the books arrive usable before the cutoff | €283,200 (conditional) |
| Collector’s gap that still remains because of statues | 3,400 |

The result is a conditional upside, not a forecast. The supplier's 02:07 acceptance is disputed by its own 04:17 reply; the dispatch instruction, the carrier booking, the arrival time and the unload slot are open. The stock-backed view takes no credit for the offer until the books are received and counted.

The illustrated trace can treat confirmation as a scenario input. A future handoff would pass a verified, matching supplier claim keyed by purchase order, line and supplier, with committed quantities, delivery time and evidence. Dockside can then follow the carrier, dock slot and receipt against the same commitment. [Negotiator’s confirmation model](https://github.com/ManuelLerchner/HACKLeague2026-lio/blob/main/src/supplier_chaser/domain/models.py)

Dockside shows the clocks that decide the outcome next to the person who has to act: the latest useful release of the order (Sun 03:00, met at 00:14), the arrival the order asked for (Sun 12:00, now unlikely), and the last departure from Poznań that still makes the 16:00 receiving cutoff (Sun 07:00).

**Who pays and how much.** The buyer is the Head of Procurement, Dr. Maren Vogt's role. The users are the buyers and the site lead in Hamburg. A pilot is **€5,000 for one launch at one site**, under 1% of the €708,000 at stake in this launch. After the pilot comes a fee per critical period (launch, ramp-up, peak season) plus a share of the losses avoided, which the backtest below measures. This is a hypothesis to test with Soony. We have not yet seen a price reaction.

**Proof before the pitch: the backtest.** Dockside keeps every file version and event, so it can replay a launch. For a prospect we replay their last launch and report when Dockside would have flagged each problem, how many buyer hours of chasing it would have replaced, and the value of the actions that were still possible at that moment. For Soony, the rush offer stayed useful only until Sunday 03:00, so every hour of earlier warning counted.

**Backtest on Soony, from the timestamps in the data and the action log.**

| When | What the evidence shows |
|---|---|
| Wed 18 Nov | Goods receipt 5000413065 books **80,000** art books against an order line of **8,000** ("over delivery accepted"). From here the stock list overstates art books by 72,000. |
| Sat 21 Nov, about 16:00 | Soony's files are published on the portal. Dockside's service starts its first pass at 17:57. |
| Sat 17:59 | **Two minutes into its first pass**, Dockside flags the receipt as impossible against the order and the delivery note, and corrects usable stock in its forecast (the ERP record stays as booked). In the same pass it asks Karton Nowak to reserve the 8,000 ready art books at no cost and proposes the order. |
| Sun 00:14 | The formal order goes out when its 15-minute stop window elapses, under the policy in force that night (no signature for paid actions in the buyer limit), before the **Sun 03:00** limit. Today's release gate would have held it until Paul Winter signed the exact terms and the carrier and site had confirmed; `examples/replay_gate.py` replays exactly this order under the current rules. |
| Sun 00:18 | Karton Nowak acknowledges the order and asks for the correct PO reference, because the one quoted was already delivered. Dockside asks Paul Winter. |
| Sun 02:07 | Karton Nowak accepts the written order and hands the books to its own logistics team (not yet to a carrier). Still open: the carrier booking and arrival time, the unload slot, and the PO reference Soony's buyer has to give. Dockside keeps chasing each of them; the goods must leave Poznań by 07:00. |
| Sun 04:17 | The same supplier now writes that its records show **no dispatch instruction and no Warta Trans booking**, and that loading takes 4 hours from a written instruction. Dockside treats this as a conflict with the 02:07 acceptance, models the earliest unload at about 17:17, after the 16:00 cutoff, and asks the buyer to reconcile the order and the carrier to confirm a real ETA before anything else is signed. The rescue is shown as at risk, not as on track. |
| Without Dockside | The gap shows only when the art book shelf runs empty during packing ("we find out at the dock"). The 4,900 usable books last about five and a half hours at 900 Collector's boxes an hour; then packing stops, too late for a rush order. |

**The next ten customers.** They share three traits: a date retail will not move, products assembled from many parts that share supply, and suppliers who report by PDF and email in several languages. We start with one segment where all three are extreme: **tabletop and board game publishers in the German-speaking market**. Each has hundreds of components per box, Asian and Polish print and plastics suppliers, and two immovable dates a year (the SPIEL fair in Essen in October and the Christmas retail cutoff).

Target list: Kosmos, Pegasus Spiele, Ravensburger, Schmidt Spiele, HABA, Asmodee Germany, Hutter Trade, Feuerland Spiele, Frosted Games and Queen Games. The offer to each is the same: send us the files from your last fair or Christmas launch, and within a day we replay it and show which problems Dockside would have caught, when, and what was still possible at that moment. The Kestrel example shows that replay offline on a reviewed sample customer. The first buyer we ask to pay is Soony's Head of Procurement, Dr. Maren Vogt, for the next launch. After that comes the €5,000 pilot on the next launch.

The first three prospects, as a sales plan:

| Prospect | Buyer role we approach | Route in | Replay → pilot decision |
|---|---|---|---|
| Kosmos | Head of supply chain / operations | Warm intro via the SPIEL Essen exhibitor network, then an email with the Soony replay | Replay of last October's fair launch; pilot if it shows at least one stock-out caught a day earlier |
| Pegasus Spiele | Procurement lead | Direct outreach with the Kestrel replay as a sample | Same replay offer; pilot decided by their procurement lead |
| Asmodee Germany | Logistics manager for the DACH warehouse | Through their contract packer, who serves several publishers | Replay of one Christmas launch; pilot priced per warehouse instead of per brand |

**The comparison forecast.** Before the pilot starts, the buyer approves a frozen baseline: Dockside's stock-backed forecast plus every receipt already expected and every recovery action already under way without Dockside (for Soony on Saturday: the open shipments in the ERP and the tracker, at their booked arrival times). Only copies above that baseline count.

*Worked example.* Saturday's ERP lists an air shipment of 28,000 game cards booked to arrive Wednesday, with no confirmed departure. In the baseline it counts at its booked time, Wednesday, so it adds nothing to Sunday's launch. If Dockside's chasing gets it confirmed on a Saturday flight and the cards are received, counted and packed into boxes that ship Sunday, those boxes are Dockside's: the baseline did not have them, and the evidence chain (Dockside's request, the carrier's confirmation, the goods receipt, the dispatch record) shows why. If the supplier had confirmed the Saturday flight on its own before Dockside asked, the baseline would be updated on the spot and the copies would not count.

*Pricing across launches (a hypothesis to test).* The first launch is the €5,000 pilot. After that, a fee per critical period (each launch, ramp-up or peak season), priced at a fraction of the attributable value the replay shows for that customer's previous period.

**When does the pilot pay for itself?** At Soony's €120 refund per Collector's copy, **182 attributable copies** cover the €5,000 pilot and the €16,800 rush purchase (182 × €120 = €21,840). Attributable means: absent from the frozen no-intervention forecast (which already counts expected receipts and recovery actions that were under way before Dockside), and traced from a Dockside action to a verified receipt and a dispatched box.

Adjacent segments follow with the same product: video game physical editions and their packing partners, toy kits before Christmas, and contract packers serving several brands, where one onboarding serves many launches.

**What the pilot must prove (acceptance criteria).**
1. Time from a file or email becoming available to an actionable alert: under 15 minutes. Measured as two intervals: availability to Dockside's pass (at Soony about two hours, because the service started then) and pass to alert (two minutes).
2. Buyer minutes spent chasing suppliers per open issue: at least halved, measured against a one-week baseline the buyers log before the pilot.
3. At least 1,000 copies dispatched that would otherwise have been short, counted from verified receipts linked to Dockside's actions **and** the dispatch records of the finished boxes.
4. Zero paid commitments sent without the assigned approver's signature on the exact terms.

**How we measure it (proposed for the buyer's sign-off before the pilot starts).** On day one we freeze the comparison forecast defined above (stock on hand, plus receipts already expected and recovery actions already under way, at their booked times) and store it with the action log, dated and with the quantities and arrival times it assumed. Every copy counted as recovered must trace from that frozen gap through a Dockside action, a verified receipt and a finished-box dispatch record. Copies that would have shipped anyway stay out. For the next prospect we also time the onboarding itself: hours from receiving their files to the first replayed alert.

**Still to validate with Soony.** The hours buyers spend chasing today (the onboarding notes say "every day, by phone") and their reaction to the €5,000 price.

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

Drop 2 changes three files and the verified receipt adds 100 motors to the forecast. Drop 3 changes one file and changes the next step from “On the way” to “Needs you”. Both source claims stay visible and the unsettled quantity stays out of the plan. Every drop ingests with zero issues. The original Soony rebuild still matches the saved typed rows and the offline forecast of 5,900 Collector's and 34,800 Standard copies short (the live service, which also applies the corrections in its action log, shows 32,000 Standard), with the rush offer adding at most 2,500 Collector's copies and avoiding €300,000 in refunds.

## The three-minute demo

1. **The pain (30 s).** Maren's quote, then the receipt booked as 80,000 against an 8,000 note, and the art book trace: 5,900 Collector's copies short on Saturday.
2. **The reversal (60 s).** The order row: sent 00:14, accepted 02:07, disputed 04:17, each with its source email. The decision card: one task for Paul, the modeled arrival against the 16:00 cutoff, what clears it. Nothing is counted or sent while the conflict is open.
3. **Another customer, same runtime (30 s).** The Kestrel replay: a toy maker's files, its own SKUs, site and deadline. A verified receipt moves the forecast; a carrier manifest that contradicts the truck board (30 vs 12) is held for review instead of counted.
4. **Why it's safe (30 s).** The release-gate replay: unsigned or changed terms are held, 07:01 without proof of departure is held, the exact signed terms are what leaves.
5. **The business (30 s).** €5,000 pilot, 182 attributable copies break even, Kestrel shows the same runtime on another customer, the first three publishers and how we reach them.

## What the jury can verify

| Criterion | Live proof |
|---|---|
| A pitch that sticks | Follow the art book from a corrected receipt to the supplier's reversal, and watch Dockside stop counting it and move the recommendation to the new bottleneck. |
| A real business case | Compare verified offer cost with marginal refund exposure and show the effect of the next bottleneck. Apply the same calculation to a seasonal retail launch. |
| Technical depth | Pull source changes by hash, retain evidence for receipt and inspection corrections, show real multilingual supplier replies and real outbound activity. Recompute forecasts from the shared bill of materials and the current packing clock. |
| Taste | Keep the overview quiet. Select a part to open the exact path, evidence and decision. Keep the work Dockside is doing visible without asking the buyer to manage it. |

The judging guide calls for a live prototype, repo, deck and backup video of up to three minutes by Sunday at 12:00. The stage slot is three minutes plus two minutes of questions.
