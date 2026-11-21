# Data findings — kickoff snapshot (313 files, Sat 21 Nov 2026 ~16:00)

Verified against portal listing: all 313 sha256 match the zip.

## Launch math (source: context/launch_status.xlsx, Sat 13:00 — newer than the PDFs' "kickoff" numbers)
| | Demand | Packed Sat 13:00 | Left | Rate/h | Hours needed / left |
|---|---|---|---|---|---|
| Standard | 640k | 554k | 86k | 6,000 | 14.3 / 23 |
| Collector's | 72k | 53.1k | 18.9k | 900 | 21 / 23 (≈2 h slack) |

Note: launch_status.xlsx stores numbers as German-formatted text ("640.000").

## Component gaps (agent shortfall.py, assumptions: no scrap)
| Component | Gap | What can close it |
|---|---|---|
| CMP-GAM-001 game card | −34.8k | SHP-04311 air ICN→FRA (48k). Still "booked" at ICN, ETD passed, ETA 1 d vs route 2 d → at risk |
| CMP-GAM-003 case | −32k | SHP-04276 container NHSU4471921, **discharged at CTA Hamburg since 19/20 Nov, nobody picked it up** |
| CMP-GAM-004 sleeve | **corrected:** stock 186k = 122k Karton Nowak rev C (usable, OA26837842) + 64k PackRight rev B (blocked by ECN-2026-118). Need 128k → −6k | SHP-04313 truck Poznań (20k rev C) due Sat evening → +14k. Not critical if the truck gets a dock slot |
| CMP-GAM-008 art book | −5.9k (GR 5000413065 booked 80,000 instead of 8,000) | **Karton Nowak has 8,000 in stock, loaded in 4 h, ~9 h drive** (Oferta_tryb_ekspresowy, valid 24 h) |
| CMP-GAM-010 statue | −3.4k (lot 5000413063 failed inspection) | nothing in time; concession needs vacant Category Lead |
| CMP-MAG-005 magnet | −100.8k on paper | probably BOM error (magnets integrated in CE box) — ask Nils/Maren |

## From supplier documents (order confirmations, notices, scans)
- **SHP-04311 is only 28,000 of 48,000 cards** (Shipping_advice_020-48113256): the airline offloaded 2 of 7 ULDs; the remaining 20k fly 23.11, reaching Hamburg 24.11 (too late). Game-card gap stays ≈ −6.8k even if the 28k make it, and they still need a FRA→HAM truck (Sunday ban!).
- **Boise 36k cards (PO 4500318769) packed but on hold at their dock**: the PO says DDP, the contract is EXW. They ship only once Soony confirms EXW in writing or names its own forwarder (OA26144214). An incoterm dispute is blocking the freight.
- Sleeve artwork split: PackRight prints rev B (all its POs + packing list), Karton Nowak prints rev C. SHP-04313 (20k, PO 775) is rev C and due DC-HAM **Sat evening**, so it needs a dock slot before 22:00.
- Karton Nowak downtime 21.11 14:00 – 23.11 06:00: offset and bindery stopped; the digital line can do max 15k sleeves per 36 h. The rush offer says 50k, so the two documents contradict each other.
- Statue lot 5000413063 rejected (AQL), but booked unrestricted.
- Unordered goods received: 30k PRM-GH6-PIN (Dongguan SN-261008-884).
- Truck plates appear only on Karton Nowak WZ notes ("PO 7K21M" = Poznań plate, not a PO). No container or seal numbers on any scan.
- Full OA table: docs/order_confirmations.csv (201 OAs, 291 lines; ~60 lines >7 d past contractual lead time).

## Logistics data traps
- `last_lat/last_lon` for sea shipments are a straight lat/lon line origin→Hamburg (ships "in" Ukraine, Kazakhstan, Tibet). Synthetic → never plot raw; project onto sea lane by progress.
- Tracker xlsx (v37_FINAL_final2) has only 14 sea rows; misses all air/road incl. both critical ones; statuses stale (04276 "in transit" though at port).
- SHP-04281 "in_delivery" since Thu 12:00 with no update (GH5 parts, not critical).
- Shipment reference "PO 7K21M" ≠ PO 4500318775.
- GR 5000413064 books 64k sleeves on PO 4500318738 on 17 Nov — before that PO's container reached port.
- Every row's updated_at ends ~Fri 20 Nov; staleness must be judged per carrier / relative, not absolute.

## Who decides (PP-07 + org_chart_v2)
- Dock slots at DC-HAM: Katrin Jansen (site lead), deputy Raj Patel.
- Unplanned air freight ≤ €200k/month/category: Category Lead; above → CPO (Jonas Lindqvist) same hour.
- Collector's Category Lead vacant until 23 Nov → CE exceptions go to Dr. Maren Vogt.
- Release/quality (sleeve rev C, statue concession): Clara Weiss / Daniel Okafor.
- Escalation for line-stopping risk: Maren Vogt, same hour (memo + plan notes). CE has priority over Standard.
