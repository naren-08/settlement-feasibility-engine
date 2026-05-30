# Settlement Feasibility & Fee Engine — Take-home

Welcome, and thanks for taking the time. The full problem is in
[`ASSIGNMENT.md`](./ASSIGNMENT.md). This README is just orientation.

## The task in one line

Given a client's escrow account, a settlement offer, and a creditor's rules,
decide whether the offer is affordable (and schedule it, collecting our fee as
early as allowed) or — if not — compute the minimum extra funding needed.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Layout

```
hiring_takehome/
├── ASSIGNMENT.md            # full specification — read this
├── feasibility/
│   ├── models.py            # data models, JSON loaders, date/EOM helpers (provided)
│   └── engine.py            # >>> implement evaluate_offer here <<< (+ Result shape)
├── cases/                   # four example cases (client.json / offer.json / creditor_rules.json)
│   ├── case1_feasible_even
│   ├── case2_infeasible_minima
│   ├── case3_balloon
│   └── case4_tiers
├── tests/
│   ├── test_smoke.py        # scaffolding sanity tests (pass out of the box)
│   └── test_cases.py        # example expectations — make these pass, then add your own
├── run.py                   # python run.py cases/<case>
└── requirements.txt
```

## Run

```bash
# evaluate a single case (prints the Result as JSON)
python run.py cases/case1_feasible_even

# tests
pytest -q
```

Out of the box, `tests/test_smoke.py` passes and `tests/test_cases.py` fails —
the latter is your target. Go beyond those four cases with your own tests.

## What to submit

Your implementation, your tests, and a short README section describing:
- your approach and the alternatives you considered,
- **your interpretation of the payment shapes** (even / staircase / balloon — we
  left these loosely defined on purpose),
- assumptions you made, and known edge cases / limitations.

Budget ~5–6 hours. Prefer a correct, well-tested core over breadth. When in
doubt, write down your assumption and keep going.

---

## Approach

### Algorithm

The solver iterates `k` (number of payments) from `max_k` down to 1. For each `k`:

1. Generate cadence dates; skip if last date exceeds the horizon.
2. Build payment amounts according to the shape (even/balloon/staircase).
3. Greedily front-load the program fee: simulate forward, and on each payment date collect as much fee as the account can afford after creditor payment + bank fee.
4. Run a full date-by-date simulation (credits before debits on same day). If balance stays ≥ 0 everywhere → feasible.

Trying larger `k` first means smaller per-payment amounts, which leaves more room for early fee collection — aligning with the objective.

When infeasible, binary search finds the minimum lump sum and minimum monthly increment independently.

### Alternatives considered

- **LP/constraint solver (e.g. ortools):** Would give optimal fee allocation but adds a dependency and complexity for a problem solvable with greedy + enumeration.
- **Bottom-up (try k=1 first):** Rejected because smaller k means larger payments that crowd out early fee collection.
- **Exact fee optimization per k:** The greedy approach (take max fee each date) is provably optimal for front-loading since fee has no ordering constraint beyond "not before first payment."

### Payment shape interpretation

**Even (`even_pays=true`):**
All payments are `offer_total // k`. The remainder `offer_total % k` cents are distributed +1 to the **last** payments, keeping the sequence non-decreasing.

**Balloon (`is_ballooning_allowed=true`):**
Payments 1 through k-1 sit at their respective floors (respecting tiers and token-pay limits). The final payment absorbs the remainder (`offer_total - sum(first k-1)`). This maximizes fee front-loading by minimizing early outflows.

**Staircase (neither flag):**
At most `max_segments` distinct payment levels, non-decreasing. The solver maximizes the count of early payments at the lowest valid floor, then sets the later group to a uniform higher level that makes the sum exact. With `max_segments=2`, this produces a clean two-step shape (e.g. 6 payments at $25 then 4 at $50). Tier boundaries naturally align with segment boundaries.

### Token-pay / tier interaction with balloon

In balloon mode, the first k-1 payments respect both the token-pay cap and tier step-ups. If `max_token_pays=3` and `min_payment_tiers=[[4, 5000]]`, then positions 1-3 may sit at `min_payment_cents`, position 4+ must be ≥ 5000, and the balloon absorbs the rest. The balloon itself must also be ≥ its positional floor and ≥ the previous payment.

### Assumptions

- `max_terms` and `max_payments` bind identically (as stated in the spec): `k ≤ min(both)`.
- "Distinct payment levels" for `max_segments` counts exact cent values — $83 and $84 are two distinct levels.
- For `max_segments=1`, the even-pay logic is reused (remainder distribution creates at most +1 cent difference, treated as valid for a single-segment constraint).
- The lump sum is injected on the earliest future credit date (first draft after `as_of_date`), since earlier cash is weakly more useful.
- The monthly increment is added to every ledger credit entry after `as_of_date`.
- Rounding uses explicit round-half-up (`math.floor(x + 0.5)`) as required by the spec, not Python's default banker's rounding.

### Fee-only trailing dates

The solver uses a two-pass approach for fee allocation:
1. **Pass 1:** Try to collect the full program fee on creditor-payment dates only (maximum front-loading).
2. **Pass 2 (fallback):** If fee can't fit, extend with fee-only trailing cadence dates after the last creditor payment, up to the horizon. These dates carry only fee (no creditor payment, no bank fee).

This ensures the fee is front-loaded as aggressively as possible, while avoiding false "infeasible" verdicts when the fee simply needs one or two extra months to be collected.

### Known edge cases / limitations

- If `offer_total` cannot be split into `k` payments that all meet their floors AND sum exactly AND use ≤ `max_segments` levels with exact divisibility, some valid splits may be missed. The solver handles the common cases (2-segment with exact division, tier-aligned splits) but does not exhaustively search all possible multi-segment configurations.
- Binary search for minimum funds assumes the feasibility function is monotonic (more money → still feasible), which holds given the problem structure.
