---
name: research-protocol
description: How to research companies, stocks, sectors or macro topics with the Torus research memory. Use whenever the user asks for investment or company research, a thesis, a stock analysis, or an update on earlier research.
---

# Researching with Torus

Torus is a research memory that persists across sessions. It stores entities
(companies, people, products, sectors), **evidence** (sources), **claims**
(atomic findings that cite evidence) and **theses** (conclusions that rest on
claims), with full history. Use it so that research compounds instead of
starting from zero each time.

The capture mode for this installation is **${user_config.capture_mode}**.

## 1. Start from what is already known

- If the prompt already contains a "Torus: prior research" block, read it first.
- Otherwise call the `recall` tool with the entities (tickers, names) or the
  question. Pass `as_of` to see what was believed at an earlier date.
- Build on existing theses: confirm, weaken or break them explicitly instead of
  writing a new, unrelated report. Don't re-explore listed rejected hypotheses
  unless there is new evidence.

## 2. Record while you research

What you record depends on the capture mode:

| Mode | You record | A curator records later |
|---|---|---|
| `live` | evidence, every claim, relations, theses, rejected hypotheses | anything you missed |
| `balanced` | theses, the key claims they rest on (with evidence), rejected hypotheses | supporting claims, extra sources |
| `curator` | theses and rejected hypotheses only | claims, evidence, relations |

Rules that apply in every mode:

- **Claims are atomic.** One checkable statement per claim, with entities named.
  "TSMC CoWoS capacity limits NVIDIA data-center shipments through 2026", not a
  paragraph.
- **Facts and metrics cite evidence.** Call `record_evidence` (URL, title, a short
  verbatim quote) and pass its id. Inferences cite evidence or `derived_from`
  claim ids. Opinions and forecasts need neither, but say whose they are.
- **Use honest confidence.** 0.9+ only for primary sources such as filings. A
  single news report is ~0.6. Your own inference is usually 0.4–0.7.
- **Set validity dates** (`valid_from` / `valid_to`) for time-bound facts such as
  guidance, prices, rates and contracts.
- **Theses link their assumptions.** `record_thesis` with `assumption_claim_ids`,
  a horizon, and `triggers` saying what would invalidate it. This is what lets
  Torus flag the thesis when news breaks later. Write triggers in concrete
  words a headline would use ("TSMC CoWoS capacity slips into 2027"), with the
  company they watch: Torus matches event wording against them.
- **Horizons are dates.** Use forms Torus can read: "12m", "18 months",
  "FY2026", "Q3 2026", "2027-06-30". The audit flags theses past their horizon,
  and the scorecard decides tracked theses there. Offer `track_thesis` for a
  thesis about a listed company.
- **Correct, don't duplicate.** Use `supersede_claim` when a number changes and
  `retract_claim` when something was wrong. Use `update_thesis` to change a
  thesis's status or confidence, with a reason.
- **Record dead ends.** `record_rejected_hypothesis` for ideas you checked and
  dropped, with the reason.
- **Cite URLs in your answer** so the curator can link sources you didn't record.

## 3. Relations

Use `record_relation` for durable links such as `SUPPLIES`, `CUSTOMER_OF`,
`COMPETES_WITH`, `EXPOSED_TO` and `SUBSIDIARY_OF`, backed by a claim where
possible. Events touching one company reach theses about its suppliers and
customers through these links.

Look around the graph before concluding:

- `neighbors` shows a company's suppliers, customers and competitors (1–2 hops),
  with the theses attached to them.
- `find_paths` shows how two companies are connected, e.g. how an ASML export
  rule could reach an AMD thesis.

Links whose source is `starter:…` come from a curated pack. Confirm or correct
them as you research. Re-recording a link marks it as yours.

## 4. Read primary sources

`filing_text` reads a company's SEC filings by section (`business`,
`risk_factors`, `mdna`), and `filing_diff` shows what changed since the
previous filing. Prefer them to secondary write-ups, cite the filing `url` as
evidence of kind `filing`, and quote it verbatim.

## 5. Finish

End with a short summary of what changed in the graph: new theses, theses whose
status or confidence moved, and claims you superseded.

Torus is a research memory, not financial advice.
