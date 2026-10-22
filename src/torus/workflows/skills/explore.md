---
name: explore
<!-- if code -->
description: Open the Torus visual graph explorer - an interactive map of companies, supplier/customer/competitor links, theses, events and alerts - in the user's browser. Use when the user runs /torus:explore or asks to see, visualise or map their research graph.
<!-- else -->
description: Open the Torus visual graph explorer - an interactive map of companies, supplier/customer/competitor links, theses, events and alerts - in the user's browser. Use when the user asks to see, visualise or map their research graph.
<!-- end -->
---

# Open the graph explorer

<!-- if code -->
1. Run `torus explore` with the Bash tool. To centre on companies the user named,
   pass them as arguments, e.g. `torus explore NVDA TSM --hops 2`. With no
   arguments it shows the whole graph.
2. The command writes an HTML file (it prints the path) and opens it in the
   default browser. If the user is on a remote or headless machine, add
   `--no-open` and give them the path instead.
<!-- else -->
1. Call `open_explorer`. To centre on companies the user named, pass them in
   `focus` (e.g. `["NVDA", "TSM"]`) with `hops: 2`. With no `focus` it shows the
   whole graph.
2. The tool writes a self-contained HTML file, opens it in the default browser,
   and returns the file `path` and node counts.
<!-- end -->
3. Tell the user how to use it, briefly:
   - click a company or thesis for its links, claims, sources, history and alerts
   - drag to move nodes, scroll to zoom, drag the background to pan, **Fit** to reset
   - search by name or ticker; the filters hide theses, events, competitor links
     or starter-pack links
   - red diamonds are events with open alerts ({{review}} handles them),
     and thesis colours show status (green active, amber at risk, red broken)
<!-- if code -->
4. If the graph is empty or thin, suggest `torus graph seed semis` (a curated
   semiconductor value chain) or researching a company first.

The file is a snapshot. Run the command again after new research to refresh it.
<!-- else -->
4. If the graph is empty or thin, offer `seed_starter_pack` (e.g. `semis`,
   `software`, `banks`, `energy` or `pharma`) or {{onboard}} first.

The file is a snapshot. Call `open_explorer` again after new research to refresh it.
<!-- end -->
