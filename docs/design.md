# Dockside — design plan

## Subject, audience, job
- **Subject:** inbound freight for one launch night: containers at a Hamburg terminal, air freight via Frankfurt, trucks from Poznań, a hard wall at Sunday 20:00.
- **Audience:** Raj Patel (logistics planner) at a desk; Katrin Jansen (site lead) on the dock floor with a phone; Dr. Maren Vogt (head of procurement) between meetings. A stressful Sunday, not a quiet analytics session.
- **Primary job:** get the right person to approve the right fix *in time*, and show why, in money and Collector's copies.

## Vernacular we borrow (and why)
- **Container stencil lettering.** Every box in a yard carries its ISO code (NHSU 447192 1) in stencil type. Shipments are named by these codes in the data, so the stencil face is the subject's own type, not decoration.
- **Hazard hatching.** Yellow/black diagonal hatching marks no-go zones on quays. We use it for exactly one thing: the German Sunday truck ban window (00:00–22:00), the real no-go zone of this launch.
- **The yard row.** Containers sit in rows and move toward the gate. The arrivals view is a yard row on a time axis that ends at a wall: Sunday 20:00.

## Tokens
Color (light, primary):
| name | hex | use |
|---|---|---|
| Harbour fog | `#E6EBEC` | page background (cool, grey-blue paper — not cream) |
| Quay white | `#F7F9F9` | raised surfaces (yard, decision list) |
| Bunker | `#13222C` | ink, the deadline wall |
| Steel | `#5A6A72` | secondary text, axis, hairlines at 25% |
| Dock yellow | `#F2B300` | hazard hatching, "waiting for you" state |
| Signal red | `#C8102E` | money at risk, "misses the wall" |
| Sea green | `#1C7A68` | "makes it", approved |

Dark (night shift, `prefers-color-scheme: dark` and `[data-theme=dark]`): background `#0F1C24` (night harbour, a deliberate navy), surfaces `#16262F`, ink `#E3EAEC`, steel `#8FA1A9`; yellow/red/green lifted for contrast.

Type:
- **Big Shoulders Stencil Display** (Google Fonts) — container codes, the money figure, the deadline wall label. Never for paragraphs.
- **Barlow** (Google Fonts; designed from highway/transit signage) — everything else, weights 400/500/600, tabular numerals (`font-variant-numeric: tabular-nums`) for quantities. Barlow Semi Condensed allowed for the yard row labels only.
- Scale (1.25, base 16px): 13 / 16 / 20 / 25 / 31 / 39 / 61 (hero money). Line length ≤ 72ch. Sentence case everywhere. No all-caps labels, no monospace labels, no middle-dot meta strings.

## Layout
```
┌ Dockside ─────────────────────── Soony, Grand Heist 6, DC Hamburg ─── 26 h 12 min to trucks out ┐
│                                                                                                  │
│  €708,000 in Collector's refunds depend on 3 decisions.        (hero sentence, money in stencil) │
│  5,900 copies short on art books. The cell has 2 hours of slack.                                 │
│                                                                                                  │
│  ┌ yard ─────────────────────────────────────────────────────────────────────────┐ ║ Sun 20:00   │
│  │ Sat 18      Sun 00 ////////////// Sunday truck ban ///////////// 22   cut-off │ ║  wall       │
│  │ [NHSU 447192 1]  at the terminal, no truck booked                              │ ║             │
│  │        ▭ PO 7K21M ──────●                                                      │ ║             │
│  │                     ▭ 020-48113256 ─────────────────────────────────────────── │→ Tue 24 Nov │
│  └────────────────────────────────────────────────────────────────────────────────┘             │
│                                                                                                  │
│  Decisions for you (2/3 width)                       │  Ask Dockside (1/3)                       │
│  ▌ Order 8,000 art books, send in vans  €16,800 → protects €708,000                              │
│  ▌ needs Paul Winter (buyer)   [Approve and send] [Decline]                                      │
│  ▌ ...                                               │  What Dockside noticed                    │
│                                                      │  Activity                                 │
│  Parts for the last pack-out (table)                 │                                           │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```
- Left-aligned throughout; the yard is the only full-bleed element.
- Decisions are a **list with a status edge** (4px left bar: yellow = needs you, red = escalated, green = approved), not identical cards. Cost vs. protected value is shown as one ratio line ("€16,800 protects €708,000"), the single most persuasive number per decision.
- Mobile (≤ 720px): yard scrolls horizontally inside its own frame with the wall pinned; decisions first, then ask, then the rest.

## Principles
1. **Boldness in one place: the yard row with the wall.** Everything else is quiet: no shadows except the yard, one radius scale (2px for chips, 6px for surfaces), hairlines in steel at 25%.
2. **Every structural mark carries data.** Hatching = Sunday ban. Left edge color = decision state. Wall = deadline. No decorative numbering or eyebrows.
3. **Words name what happens.** "Approve and send" → toast "Approved and sent to Karton Nowak". "Decline" → "Declined". Empty states tell you what to do next.
4. **One orchestrated motion:** on load, containers slide into their arrival slots once (respecting `prefers-reduced-motion`). Otherwise motion only answers actions (expanding messages, confirming approval).

## Review against the brief (what I changed)
- First draft was a big € KPI tile row + rounded cards — the SaaS-card default. Replaced by a **hero sentence** that reads as the situation ("€708,000 … depend on 3 decisions") and a yard row that *is* the data.
- First palette idea was near-black + safety orange (an industrial cliché close to the "dark + one acid accent" default). Switched to a cool harbour-fog light theme with yellow reserved for the ban hatching only.
- Dropped monospace for numbers (a generated-page tell); Barlow's tabular figures do the job and match signage.
