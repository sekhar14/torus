---
name: onboard
description: Start covering a list of companies with Torus - add them, watch their SEC filings, seed matching starter packs, and have a Haiku subagent map their suppliers, customers and competitors from their annual reports. Use when the user runs /torus:onboard, shares a watchlist or portfolio, or asks Torus to start following some companies.
---

# Onboard a coverage list

Torus is most useful once the graph knows how the user's companies connect.
Onboarding builds that structure in a few minutes.

1. **Get the list.** Use the tickers or names the user gave. If they pointed at
   a CSV file (a portfolio or watchlist export), read it and take the ticker or
   symbol column. Confirm the list back in one line if it came from a file.
2. **Add the companies.** Call `onboard` with the tickers. It adds each company,
   watches its SEC filings, queues a mapping job for it, and suggests starter
   packs. Tell the user about anything in `unresolved`.
3. **Starter packs.** For each suggested pack, say what it covers and how many
   of their companies it includes, and offer to add it. Call
   `seed_starter_pack` for the ones they accept. Packs give impact analysis a
   curated web of links on day one; research and the mapper confirm or correct
   them later.
4. **Map the companies.** Delegate to the `torus:company-mapper` subagent with
   the Agent tool, using this prompt: "Process the Torus mapping queue." It
   reads each company's latest 10-K or 20-F and records suppliers, customers,
   competitors and customer concentration, citing the filing. It handles 5
   companies per run, so run it again (in the background if the user wants to
   keep working) until it reports an empty queue.
   - If a tool says there is no SEC contact email, ask the user to run
     `torus config set sec_contact you@example.com` and try again.
5. **Report.** Summarise in a few lines: companies added, packs seeded, links
   the mapper recorded, and anything it couldn't map. Suggest next steps:
   `/torus:explore` to see the map, and researching a company to record a first
   thesis. Mention that `torus schedule install` keeps the filing watchers
   running.

Don't map companies yourself in the main conversation. The mapper runs on a
cheaper model and cites the filings.
