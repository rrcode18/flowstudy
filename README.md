# FX spot-to-roll matching: signed amounts and roll trade IDs

## Run

Requires Python 3.10 or later:

```bash
pip install -r requirements.txt
python fx_matcher.py
python -m unittest -v test_fx_matcher.py
```

No command-line arguments are needed. Defaults read `data/spot_trades.csv` and
`data/roll_trades.csv` next to the Python script and write `results/` next to it.
Paths are resolved from `__file__`, so execution also works from a different working
directory or an IDE. Install requirements once before running.

To regenerate exactly the same sparse sample, run `python generate_sparse_sample.py`.

```bash
python fx_matcher.py --spot my_spots.csv --roll my_rolls.csv --out my_results
```

## Input schema and signed amounts

Spot CSV:

```text
spot_id,client_id,pair,trade_time_utc,value_date,base_amount
```

Roll CSV:

```text
roll_id,client_id,pair,trade_time_utc,start_date,end_date,base_amount,roll_trade_id
```

`account_id`, `direction`, `side`, `near_side` and `far_side` are unnecessary and
are absent from the supplied fixtures and generated output CSVs.

Amounts are signed units of the pair's first (base) currency:

- Spot +5,000,000 EURUSD: the customer bought EUR 5m.
- Spot -5,000,000 EURUSD: the customer sold EUR 5m.
- Roll +5,000,000 EURUSD: the customer sells EUR 5m near and buys EUR 5m far,
  carrying a LONG EUR position to the far date.
- Roll -5,000,000 EURUSD: the customer buys EUR 5m near and sells EUR 5m far,
  carrying a SHORT EUR position to the far date.

Roll sign always refers to the FAR-leg position. Convert near-leg-signed source
data before using it. The same base principal is assumed on both legs. Zero,
non-finite amounts, duplicate row IDs, invalid dates and non-UTC times are rejected.
Amounts are scaled to positive magnitudes in millions internally, and direction
is derived from the sign. LONG and SHORT remain separate matching pools. Output
amounts, residuals and bounds use the original signed base-currency units.
SHORT ranges are sorted numerically: for example [-5m, 0], not [0, -5m].

## roll_id versus roll_trade_id

- `roll_id`: unique execution-ticket / input-row identifier.
- `roll_trade_id`: business roll-trade identifier. Multiple split tickets may
  share it. Different values MUST NOT be aggregated even if dates and amounts match.

For a dataset without a parent identifier, use a unique identifier per roll ticket.
Do not invent a shared parent from matching fields. IDs in this demonstration are
synthetic parent IDs; their values are not used as evidence of spot provenance.

Spot grouping:
`client_id, pair, sign(base_amount), value_date`.

Roll grouping:
`client_id, pair, sign(base_amount), start_date, end_date, roll_trade_id`.

All groups retain input ticket IDs and execution-time ranges. A repeated parent
on different dates, clients or directions stays in separate groups.

## Matching network

Independent optimisation partitions remain `client_id, pair, sign(base_amount)`.
Within each partition, distinct roll groups are labelled by `roll_trade_id` and
retain their own observed capacities. Date-continuous rolls with different trade
IDs can be linked to support consecutive rolls. Splitting optimisation by trade
ID would break those chains and incorrectly let several networks reuse one source.

The transition graph uses exact settlement-date continuity and a conservative
execution rule: predecessor latest execution <= successor earliest execution.
No cross-client or cross-direction flow is allowed. Each source is separately
conserved, while all sources share each roll's capacity. Principal may exit
tracking at any stage. Unused roll capacity represents unknown other-origin
principal; the full external customer book is not reconstructed.

This execution rule needs confirmation for production. Pre-hedges and backdated
bookings may need a different policy. For overlapping execution batches, split
aggregated groups into time batches; the conservative max/min rule can otherwise
reject valid partial associations.

## Allocation and uncertainty

The default selected allocation is an explicit illustrative policy, not calibrated
matching evidence:

1. Maximise unique initial spot principal entering any roll.
2. Subject to 1, maximise principal-weighted time until tracking exits.

The second objective uses terminal dates, not number of hops. Both objectives can
over-attribute unrelated rolls. Source identity is not established merely by the
policy or by narrow policy-optimal ranges.

For each reachable source/roll pair the script calculates:

- Selected signed attribution.
- Full feasible lower and upper bounds, generally including zero.
- Policy-optimal lower and upper bounds.
- Ambiguity flags based on the ranges.

Bounds are computed separately and need not be jointly attainable. Optimisation
uses a 1e-7 million-unit-day tolerance for the terminal objective. Tiny discrepancies
can reflect numerical tolerance. Ties need not be resolved identically by different
solver versions.

To add validated evidence costs, use `FlowModel.solve(c)` with one cost per
transfer/exit variable and recompute ranges under the same objective constraint.
Costs and exit penalties must be calibrated jointly. No probability model is supplied.

## Outputs

- `allocations.csv`: client_id, pair, value_date, roll_trade_id, source/roll group
  IDs, roll start/end dates, signed allocation, bounds and ticket-ID lists.
  **value_date here is the original source spot settlement date**, including for
  later continuation rolls. Use roll_start_date for the current roll near date.
- `matching_network.csv`: one row per observed roll group, including client_id,
  pair, value_date, roll_trade_id, signed observed/attributed/unattributed amounts.
  **value_date here is the roll near date.** This includes rolls with no candidates.
- `roll_residuals.csv`: same roll-level reconciliation view.
- `source_summary.csv`: source settlement date and signed unique initial principal,
  rolled principal and unrolled principal.
- `transfers.csv`: source-labelled path transfers and EXIT rows. roll_trade_id is
  the destination trade ID; it is blank on EXIT rows.
- `spot_groups.csv`, `roll_groups.csv`: group definitions and underlying ticket IDs.
- `allocation_ranges.png`: full feasible range, policy-optimal range and selected point.
- `flow_Cxxx.png`: client networks with LONG/SHORT panel labels and roll_trade_id on
  edges. Labels show signed selected / observed principal, in millions.
- `run_info.json`: configuration and counts.

Position direction is derived from signed amounts rather than exported as a
redundant CSV field. It is explicitly shown as LONG/SHORT in network plots.
Different base currencies must never be summed without conversion. Repeated roll
volume is not unique initial principal. Ticket-level provenance is not inferred
within a pooled spot group.

## Sparse sample and expected results

The supplied fixture has 200 spot execution tickets and 60 roll execution tickets,
aggregating into 30 roll trades. Exactly 10 spot tickets (5%) have any feasible
candidate; those candidates involve only two roll trades (RT001 and RT002).
The other 190 spot tickets settle on dates that cannot feed any observed roll.
Other roll trades contain overlapping clients and pairs but no compatible complete
path from an observed spot source.

- S0001-S0005: C001 EURUSD long, 20m settling 5 October. RT001 rolls 6m to 12 October.
- S0006-S0010: C002 GBPUSD short, -10m settling 6 October. RT002 rolls -4m to 13 October.
- All other spot tickets have no candidate roll. All other roll trades are external.

Five percent refers to eligible source tickets, not proof that each ticket was rolled
or that 5% of total notional was rolled. Matching is at pooled-position level.
The example purposely has no continuation path; continuation remains supported and
is covered by tests.

`data/ground_truth.csv` is evaluation-only and is never read by the matcher.
It records source ticket pools rather than claiming exact ticket-level provenance.

The output `roll_aggregation.csv` is the roll aggregation table requested by the user
(and is also available as `roll_groups.csv` for compatibility). The script takes raw
`roll_trades.csv`, builds this table, and matches its groups against spot sources.
`match_coverage.json` and `match_coverage.png` report eligible spot ticket and roll
counts. Network PNGs are written for up to ten clients with candidates.

## Limits and verification

The implementation supports partial, continuous principal. It does not support
whole-ticket constraints, unequal-principal legs, automatic netting, or cross-currency
notional conversion. Currency-pair orientation must be normalised upstream with the
correct principal conversion. Mixed reversed orientations are rejected.

Sparse LPs and client/pair/direction partitions reduce computation. This is not a
production-scale benchmark: generating paths and solving four ranges per candidate
can be expensive. Large deployments should split connected components and calculate
bounds selectively. No external market data or simulated ground truth is used.

Nine tests cover shared source capacities, continuation limits, execution order,
competing branches, unmatched sources, signed inputs, distinct parent IDs and
separation of opposite amounts with the same parent ID, plus the exact 5% fixture coverage. Every run also checks
nonnegative internal flows, source conservation and observed roll capacities.
