---
name: scorecard
description: Track how the user's Torus theses have played out - returns against a benchmark in the direction of each thesis, drawdowns, right/wrong outcomes, and calibration (does their confidence match how often they're right). Use when the user runs /torus:scorecard, asks how their calls did, or wants to track a thesis against a stock.
---

# Thesis scorecard

The scorecard is a record of the user's own calls. It is not investment advice
and says nothing about what to do next.

1. **Show it.** Call `scorecard` with `refresh: true` to fetch the latest
   closing prices (Yahoo Finance) first. If there are no tracked theses, offer
   to track some (step 3).
2. **Summarise**, briefly:
   - per thesis: ticker, stance, return in the thesis's direction against the
     benchmark (`stance_return`), worst drawdown, days held, days to horizon,
     and the outcome if decided
   - calibration, once some theses are decided: right/decided, the Brier score
     (0 is perfect, 0.25 is what always saying 50% scores), and any band where
     confidence and hit rate disagree, e.g. "your 80%+ calls were right 50%
     of the time"
   - flag theses past their horizon that are still open: they need a verdict
     (`update_thesis` with `confirmed` or `broken`)
3. **Track a thesis** when the user asks: `track_thesis` with its id. The
   ticker defaults to the subject's, the entry to the close on the day the
   thesis was recorded, the benchmark to SPY. Pass `entry_price` /
   `entry_date` if the user entered at a different time.
4. **Outcomes.** Torus decides them automatically: confirmed or broken from the
   thesis status, and by price at the horizon. If the user disagrees ("that
   one was right, I was early"), call `set_thesis_outcome`. Their call always
   wins over the automatic one.

Price data comes from Yahoo Finance's public endpoint, which is unofficial. If
it fails, closes can be imported with `torus scorecard import SYMBOL file.csv`.
