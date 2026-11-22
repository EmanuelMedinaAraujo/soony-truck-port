# Dockside · Three-minute stage pitch

**Business case 1:30** · 0:00–0:27 and 1:57–3:00

**Technical proof 1:30** · 0:27–1:57

Read the quoted lines. Use the stage directions to time the clicks. The deck includes a still of each live screen.

## 0:00–0:27 · The problem

*Slide 1. Pause after Maren’s quote.*

> “We find out at the dock.” That’s Soony’s head of procurement, Maren Vogt. Her buyers were calling suppliers every day. The trucks had to leave Hamburg at eight on Sunday. One extra zero in an art-book receipt hid a 5,900-copy shortfall. That’s €708,000 in projected refunds. We built Dockside to give the buyer time to act.

## 0:27–0:58 · Live product and supplier action

*Open the walkthrough dashboard. Expand **Dockside is working on…**, select **Game card**, then select the air shipment to show the map. Use **Open supplier reply** and close it in place. Slide 2 is your backup.*

> Dockside’s agents watch incoming files and replies, check the packing plan and chase missing confirmations. Both editions need this game card. Open its shipment to see the route. Open the supplier reply to see what they actually said. For the art books, Dockside corrected the receipt in its forecast, asked for replacements in Polish and followed up about transport.

## 0:58–1:29 · Live buyer decision

*Return to the top of the walkthrough. Open **Choose delivery or cancellation for Art book** and click **Keep the order for Monday delivery** on cue. Leave the approval panel open. Slide 3 is your backup.*

> Now replay Sunday. The supplier accepts the order, then says it has no dispatch instruction. At seven, there’s no proof the truck left. Dockside gives the buyer one decision. Keep the order for Monday or request cancellation. I’ll choose Monday. Dockside queues a new approval for the existing order. Sunday is still 5,900 copies short.

## 1:29–1:57 · The technical proof

*Return to slide 4. Point to the held and released outcomes, then the Kestrel chart.*

> Ask Dockside an ordinary question on the web, speak it on a phone, or use Discord. Each reads the same forecast. The release gate blocks changed terms and requires departure proof after seven. At Kestrel Toys, the same engine reads different files. A verified receipt moves Deluxe dispatch from seventy to a hundred. A contradictory manifest goes to review.

## 1:57–2:22 · Value to the customer

*Slide 5. Give the three numbers one beat each.*

> At €120 a copy, 2,500 extra copies could avoid €300,000 in refunds. The rush order cost €16,800, leaving €283,200 if the books arrived usable and on time. Statues would still leave 3,400 copies short. Earlier warning gives procurement time to make that tradeoff.

## 2:22–2:43 · Price and proof

*Slide 6. Point to 182.*

> We’d offer Soony a €5,000 pilot for one launch at one site. At their refund rate, 182 extra dispatched copies would cover the pilot and the rush purchase. We’d measure time to alert, buyer time spent chasing and extra copies traced to verified receipts.

## 2:43–3:00 · Where we go next

*Slide 7. End after the last sentence.*

> We’d start with tabletop publishers like Kosmos, Pegasus and Asmodee Germany. We’d replay their last fair or Christmas launch and sell a pilot for the next one. Dockside helps buyers act before the dock.

## Stage setup

Run the walkthrough from the repository root.

```sh
.venv/bin/python examples/demo_stage.py --mode sunday --include-recorded-activity --enable-questions --port 8768
```

Open `http://127.0.0.1:8768/` before walking on stage. The launcher copies saved stock and Saturday messages into temporary SQLite databases, then adds the Sunday scenario. The Monday choice runs the real decision endpoint. Restart the command before each rehearsal to reset it. Supplier email, portal polling and Discord delivery are disabled during the replay.

For a live question, use the floating chat icon. Type a question or choose **Speak a question** on a phone that can reach the secured deployment. Review the transcript before sending. In the team’s Discord channel, `/status` reports the shortfall and `/ask` accepts plain-language questions against the same dispatch data.

To show customer portability in the website, start `.venv/bin/python examples/demo_stage.py --mode kestrel --port 8769` and open `http://127.0.0.1:8769/demo/kestrel`. Load the next file drop to show the verified receipt, then the independent carrier manifest. Choose **Drive motor** to inspect the hold.

Download the [pitch deck](https://github.com/EmanuelMedinaAraujo/soony-truck-port/releases/download/hackathon-demo/Dockside-pitch.pdf) and [demo video](https://github.com/EmanuelMedinaAraujo/soony-truck-port/releases/download/hackathon-demo/Dockside-demo.mp4) for an immediate fallback.

## Likely jury questions

| Question | Answer |
| --- | --- |
| What did it actually do? | The action log contains the Polish reservation request, the receiving inquiry and the follow-up to check legal transport. The Sunday replay shows a real click producing a new approval record. |
| How do you stop a bad draft from spending money? | The signature binds exact terms by digest. Dispatch checks those terms against current supplier, carrier and dock evidence. The offline test holds a changed quantity and a late departure, then releases when the authorized carrier verifies a timely departure and the other conditions hold. |
| How much did you save? | The €300,000 is modeled potential for timely, usable delivery. Pilot attribution requires an agreed no-intervention baseline, a verified receipt and finished copies that actually dispatched. |
| What is required to add another customer? | Review the source paths, column mapping, bill of materials, shifts and policy. Kestrel used its own reviewed mapping and three file drops with the same ingest, detectors and forecast. |
| How does the team check in away from a desk? | Ask Dockside in the mobile browser by typing or speaking a question. Use `/ask` or `/status` in Discord. The tools read the same forecast in each channel. |
| Why does this need an agent? | A buyer can see the shortage in the board. The agent also opens the supplier thread, asks targeted questions, chases the missing confirmation and routes the decision to the person authorized to make it. |

```sh
.venv/bin/python examples/replay_gate.py
.venv/bin/python examples/run_customer.py examples/kestrel
```
