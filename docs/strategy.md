# Dockside and the Sunday dispatch

**The supplier can find 8,000 art books. How many more games will actually leave on Sunday?** Dockside follows the offer through legal transport, dock release and the packing line. It recalculates how many finished copies that offer could add, then works through the open steps and shows the buyer any decision that needs them.

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

Test a €5,000 pilot for one launch at one site with buyers who have a hard retail commitment. This is a pricing hypothesis. Discovery should test willingness to pay against avoided losses and buyer time saved. A seasonal toy launch is a next prospect with the same multipart packing, shared parts and receiving constraints. The forecast already takes product, component, priority, price and timing inputs. A second live customer would still need site-specific connectors, approval policy and operating data.

## The platform: compile an operation once, then let the agent run it

We pitch a horizontal platform: an onboarding agent for any deadline-driven supply chain. Soony is its first compiled customer. The launch cockpit is one instance of the platform, not the product.

| Layer | Today in Dockside | Generic? |
|---|---|---|
| Change detection (file hashes, versions, row events) | `sync/` | Yes, any file source |
| Raw ingest of every CSV, XLSX and JSON | `raw_*` tables | Yes |
| Reading documents in any language into facts | `core/extract_docs.py` | Yes |
| Operating rules (who signs what, deadline, cost per missing unit, autonomy) | `config/business.json`, every value with its source | The structure is generic; the content is per customer |
| Detectors (stuck at port, ETA against route time, revision mismatch, over-receipt, qualification expiry, Sunday truck ban) | `core/rules.py` | Mostly, once parameterized |
| Typed loaders (BOM, POs, shipments, editions) | `core/ingest.py` | No: becomes a generated mapping |
| Cockpit | `web/assembly` | No: becomes a generated view spec |

**Compile once.** From a data dump, policy documents and a few interviews, the onboarding agent writes three reviewable files:
- `mapping.yaml`: raw tables mapped to a canonical model (part, bill of materials, supplier, purchase order, shipment, stock, site, deadline, role). The agent asks only about matches it is unsure of.
- `policy.json`: what `business.json` is today, including the value function. For Soony that is €120 refunded per missing Collector's copy. For a carmaker it would be the cost of a minute of line stop; for pharma, the value of a spoiled batch.
- `views.json`: the cockpit, composed from a fixed set of about 12 components, never free-form HTML.

**Fixed runtime.** Deterministic code runs the mapping, the detectors and the renderer. The model never computes the numbers. It writes specs and proposes actions, and each action is logged, scheduled within a stop window, or sent to the person who must sign.

**Then evolve.** The same action log that makes Dockside's emails, calls and bookings auditable also records view changes. The agent can pin an answer as a widget or make a view for a role, and anyone can undo the change.

**Why it compounds.** Every new customer adds to a cross-customer record of supplier reliability, for example how fast a supplier answers and how far its ETAs slip. Every customer's file history also supports a backtest: replay their last launch and show which problems Dockside would have flagged earlier, and what that would have saved. That backtest is the sales argument and the basis for pricing as a share of avoided losses.

For Sunday we show the platform through what already runs generically: hash-based change detection, multilingual document reading, a policy with cited sources, autonomous follow-ups, Supplier Chaser and voice calls. The cockpit stays hand-polished.

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
