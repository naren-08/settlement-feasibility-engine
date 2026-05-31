"""Candidate implementation goes here.

Implement ``evaluate_offer`` so that it satisfies the rules in ASSIGNMENT.md and
the example expectations in tests/test_cases.py. The dataclasses below define the
required OUTPUT shape (see ASSIGNMENT.md "Output"). You may add helpers, modules,
or rewrite internals freely, but keep ``evaluate_offer``'s signature and the
serialized shape of ``Result`` (so the runner and tests work).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from collections import defaultdict

from feasibility.models import (
    Client, CreditorRules, Offer, LedgerEntry,
    default_first_payment_date, monthly_payment_dates,
)


@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    # lump-sum only:
    date: date | None = None
    # monthly-increment only:
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:
            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_half_up(x: float) -> int:
    """Round half-up (0.5 rounds away from zero)."""
    return int(math.floor(x + 0.5))


def offer_total_cents(offer: Offer) -> int:
    return _round_half_up(offer.settlement_pct * offer.current_balance_cents)


def program_fee_cents(offer: Offer, rules: CreditorRules) -> int:
    return _round_half_up(rules.program_fee_pct * offer.original_balance_cents)


def _get_floor(position: int, rules: CreditorRules, token_pays_used: int) -> int:
    """Get the minimum payment for a given 1-based position."""
    floor = rules.min_payment_cents
    # Tier step-ups
    for from_pos, min_cents in rules.min_payment_tiers:
        if position >= from_pos:
            floor = max(floor, min_cents)
    # Token-pay rule: if we've used up max_token_pays at exactly min_payment_cents,
    # this payment must strictly exceed min_payment_cents
    if token_pays_used >= rules.max_token_pays:
        floor = max(floor, rules.min_payment_cents + 1)
    return floor


def _build_even_payments(k: int, total: int, rules: CreditorRules) -> list[int] | None:
    """Build k even payments summing to total. Remainder on last payments."""
    base = total // k
    remainder = total % k
    payments = [base] * (k - remainder) + [base + 1] * remainder
    # Validate floors
    token_pays = 0
    for i, p in enumerate(payments):
        pos = i + 1
        floor = _get_floor(pos, rules, token_pays)
        if p < floor:
            return None
        if p == rules.min_payment_cents:
            token_pays += 1
    return payments


def _build_balloon_payments(k: int, total: int, rules: CreditorRules) -> list[int] | None:
    """Build balloon: minimum payments for first k-1, remainder in final."""
    if k == 1:
        # Single payment = total
        floor = _get_floor(1, rules, 0)
        if total < floor:
            return None
        return [total]

    payments = []
    token_pays = 0
    for i in range(k - 1):
        pos = i + 1
        floor = _get_floor(pos, rules, token_pays)
        payments.append(floor)
        if floor == rules.min_payment_cents:
            token_pays += 1

    balloon = total - sum(payments)
    # Balloon must be >= last non-balloon payment (non-decreasing)
    if balloon < payments[-1]:
        return None
    # Balloon must respect its own floor
    floor_last = _get_floor(k, rules, token_pays)
    if balloon < floor_last:
        return None
    payments.append(balloon)
    return payments


def _build_staircase_payments(k: int, total: int, rules: CreditorRules) -> list[int] | None:
    """Build staircase: at most max_segments distinct levels, non-decreasing, front-load low."""
    max_seg = rules.max_segments

    # Compute floors for each position
    floors = []
    token_pays = 0
    for i in range(k):
        pos = i + 1
        floor = _get_floor(pos, rules, token_pays)
        floors.append(floor)
        # Assume we'll pay at floor for token counting in floor computation
        if floor == rules.min_payment_cents:
            token_pays += 1

    # For staircase with max_segments levels, we try to keep early payments at their floor
    # and step up later. We search for the best split.

    if max_seg == 1:
        # All payments must be the same level
        level = total // k
        remainder = total % k
        if remainder != 0:
            # Can't do exactly equal with 1 segment if not divisible
            # Actually with 1 segment all must be same value — but that means total must be divisible
            # Re-read: max_segments caps distinct levels. If 1, all payments same.
            # But remainder distribution makes them differ by 1 cent — that's 2 levels.
            # Treat "even-like" for max_segments=1: same as even logic
            return _build_even_payments(k, total, rules)
        for i, f in enumerate(floors):
            if level < f:
                return None
        return [level] * k

    # General staircase: try to maximize payments at the lowest possible level
    # Strategy: use floors as natural segment boundaries
    # Find distinct floor values in order
    distinct_floors = []
    for f in floors:
        if not distinct_floors or f != distinct_floors[-1]:
            distinct_floors.append(f)

    # Best approach: binary/greedy search
    # With max_segments levels, we want:
    #   - As many early payments as possible at the lowest floor
    #   - Remaining payments at a higher level that makes sum = total
    # Try splitting: first n payments at low_level, last (k-n) at high_level

    best = None

    if max_seg == 2:
        # Two levels: low and high
        # Try every split point n (how many at low level)
        for n in range(k - 1, 0, -1):  # maximize n (more at low)
            # Low level = floor for positions 1..n (must be same for all to be one segment)
            # Actually low_level must be >= max(floors[0..n-1]) to satisfy all floors in that range
            low_level = max(floors[:n])
            # High level must be >= max(floors[n:]) and >= low_level
            high_floor = max(floors[n:])
            high_floor = max(high_floor, low_level)

            remaining = total - low_level * n
            if remaining <= 0:
                continue
            high_count = k - n
            if remaining % high_count != 0:
                # high_level must be integer and all same
                high_level = remaining // high_count
                # Check if we can make it work with ceiling
                # All high payments same → remaining must be divisible
                # Actually for staircase, each segment is a single value
                # If not divisible, this split doesn't work cleanly
                # But we can try: high_level such that high_count * high_level = remaining
                # If not exact, skip this n
                continue
            high_level = remaining // high_count
            if high_level < high_floor:
                continue
            if high_level < low_level:
                continue
            # Check token-pay validity
            token_count = sum(1 for i in range(n) if low_level == rules.min_payment_cents)
            if token_count > rules.max_token_pays:
                # Payments beyond max_token_pays at min must exceed it
                # This low_level doesn't work
                continue
            # Also check high payments token validity
            if high_level == rules.min_payment_cents:
                token_count += high_count
                if token_count > rules.max_token_pays:
                    continue
            best = [low_level] * n + [high_level] * high_count
            break

        # If exact division didn't work, try with remainder distribution within segments
        if best is None:
            for n in range(k - 1, 0, -1):
                low_level = max(floors[:n])
                high_floor = max(max(floors[n:]), low_level)
                remaining = total - low_level * n
                if remaining <= 0:
                    continue
                high_count = k - n
                high_base = remaining // high_count
                high_rem = remaining % high_count
                if high_base < high_floor:
                    continue
                if high_rem == 0:
                    high_level = high_base
                    best = [low_level] * n + [high_level] * high_count
                    break
                else:
                    # Distribute remainder: last high_rem payments get +1
                    # This creates 2 distinct values in the high segment → 3 total segments
                    # Only valid if we treat base and base+1 as same segment (like even)
                    # Actually the spec says "distinct payment levels" — base and base+1 are different
                    # So this would be 3 segments with max_seg=2 → invalid
                    # Try: can we raise low_level to absorb?
                    # Or: try different n
                    continue

        # Last resort: try all payments at one level (1 segment)
        if best is None:
            if total % k == 0:
                level = total // k
                if all(level >= f for f in floors):
                    best = [level] * k

    else:
        # max_segments >= 3: more flexible
        # Strategy: greedily assign floor to each position, then distribute remainder
        # to the last segment
        payments = floors[:]
        remainder = total - sum(payments)
        if remainder < 0:
            return None

        # Add remainder to the last payments to minimize segments
        # Spread remainder evenly across the last group
        if remainder == 0:
            best = payments
        else:
            # Try: keep first positions at their floors, raise last group
            # Find how many segments we currently have
            # Add remainder to last payments
            for seg_count in range(1, max_seg + 1):
                # Try putting remainder into the last seg_count positions
                count_raise = min(seg_count, k)
                # Actually let's just raise the last group
                pass

            # Simpler: raise the last (k - split) payments by a uniform amount
            for n in range(k - 1, 0, -1):
                low_payments = floors[:n]
                high_count = k - n
                high_floor_val = max(floors[n:])
                high_floor_val = max(high_floor_val, max(low_payments))
                remaining = total - sum(low_payments)
                if remaining <= 0:
                    continue
                if remaining % high_count == 0:
                    high_level = remaining // high_count
                    if high_level >= high_floor_val:
                        candidate = low_payments + [high_level] * high_count
                        # Count distinct values
                        if len(set(candidate)) <= max_seg:
                            best = candidate
                            break

            if best is None:
                # Fallback: all at floor, add remainder to last payment
                payments = floors[:]
                payments[-1] += remainder
                if len(set(payments)) <= max_seg and payments == sorted(payments):
                    best = payments

    if best is None:
        return None

    # Final validation: non-decreasing and floors
    for i in range(1, len(best)):
        if best[i] < best[i - 1]:
            return None

    # Re-validate floors with actual token counting
    token_pays = 0
    for i, p in enumerate(best):
        pos = i + 1
        actual_floor = _get_floor(pos, rules, token_pays)
        if p < actual_floor:
            return None
        if p == rules.min_payment_cents:
            token_pays += 1

    if sum(best) != total:
        return None

    return best


def _determine_shape(rules: CreditorRules) -> str:
    if rules.even_pays:
        return "even"
    elif rules.is_ballooning_allowed:
        return "balloon"
    else:
        return "staircase"


def _build_payments(k: int, total: int, rules: CreditorRules, shape: str) -> list[int] | None:
    if shape == "even":
        return _build_even_payments(k, total, rules)
    elif shape == "balloon":
        return _build_balloon_payments(k, total, rules)
    else:
        return _build_staircase_payments(k, total, rules)


def _simulate(
    client: Client,
    payment_dates: list[date],
    payments: list[int],
    fee_allocation: list[int],
    bank_fee: int,
) -> list[ScheduleRow] | None:
    """Simulate the ledger. Returns schedule rows if feasible (balance >= 0 always), else None."""
    horizon = client.last_draft_date

    # Build a map of all events by date
    # Credits and debits from the ledger (entries after as_of_date)
    credits_by_date: dict[date, int] = defaultdict(int)
    debits_by_date: dict[date, int] = defaultdict(int)

    for entry in client.ledger:
        if entry.date > client.as_of_date:
            if entry.type == "credit":
                credits_by_date[entry.date] += entry.amount_cents
            else:
                debits_by_date[entry.date] += entry.amount_cents

    # Our scheduled debits
    scheduled_debits: dict[date, dict] = {}
    for i, d in enumerate(payment_dates):
        bank = bank_fee if payments[i] > 0 else 0
        scheduled_debits[d] = {
            "creditor": payments[i],
            "fee": fee_allocation[i],
            "bank": bank,
        }

    # Also handle fee-only dates (dates in payment_dates where creditor=0 but fee>0)
    # Actually fee_allocation aligns with payment_dates already

    # Collect all relevant dates
    all_dates = set()
    all_dates.update(credits_by_date.keys())
    all_dates.update(debits_by_date.keys())
    all_dates.update(payment_dates)

    all_dates = sorted(d for d in all_dates if d > client.as_of_date and d <= horizon)

    balance = client.current_balance_cents
    schedule_rows = []
    payment_date_set = set(payment_dates)

    for d in all_dates:
        # Credits first
        balance += credits_by_date.get(d, 0)
        # Then debits
        balance -= debits_by_date.get(d, 0)

        if d in scheduled_debits:
            sd = scheduled_debits[d]
            balance -= sd["creditor"]
            balance -= sd["bank"]
            balance -= sd["fee"]

        if balance < 0:
            return None

        if d in payment_date_set:
            sd = scheduled_debits[d]
            schedule_rows.append(ScheduleRow(
                date=d,
                creditor_payment_cents=sd["creditor"],
                program_fee_cents=sd["fee"],
                bank_fee_cents=sd["bank"],
                balance_cents=balance,
            ))

    return schedule_rows


def _front_load_fee(
    client: Client,
    payment_dates: list[date],
    payments: list[int],
    total_fee: int,
    bank_fee: int,
) -> list[int] | None:
    """Allocate program fee with look-ahead: take max fee that won't break future dates."""
    if total_fee == 0:
        return [0] * len(payment_dates)

    horizon = client.last_draft_date

    credits_by_date: dict[date, int] = defaultdict(int)
    debits_by_date: dict[date, int] = defaultdict(int)

    for entry in client.ledger:
        if entry.date > client.as_of_date:
            if entry.type == "credit":
                credits_by_date[entry.date] += entry.amount_cents
            else:
                debits_by_date[entry.date] += entry.amount_cents

    all_dates = set()
    all_dates.update(credits_by_date.keys())
    all_dates.update(debits_by_date.keys())
    all_dates.update(payment_dates)
    all_dates = sorted(d for d in all_dates if d > client.as_of_date and d <= horizon)

    payment_date_index = {d: i for i, d in enumerate(payment_dates)}

    # Pass 1: compute balance at each date WITHOUT any fee
    balance = client.current_balance_cents
    balance_at = []  # balance at each date in all_dates (no fee deducted)
    for d in all_dates:
        balance += credits_by_date.get(d, 0)
        balance -= debits_by_date.get(d, 0)
        if d in payment_date_index:
            idx = payment_date_index[d]
            b_fee = bank_fee if payments[idx] > 0 else 0
            balance -= payments[idx]
            balance -= b_fee
        balance_at.append(balance)

    # Check if even without fee the balance goes negative
    if any(b < 0 for b in balance_at):
        return None

    # Pass 2: compute suffix minimum (min future balance from each position onward)
    n = len(all_dates)
    suffix_min = [0] * n
    suffix_min[n - 1] = balance_at[n - 1]
    for i in range(n - 2, -1, -1):
        suffix_min[i] = min(balance_at[i], suffix_min[i + 1])

    # Pass 3: allocate fee greedily, limited by suffix minimum (look-ahead)
    fee_remaining = total_fee
    fee_allocation = [0] * len(payment_dates)
    total_fee_taken = 0  # cumulative fee taken so far

    for i, d in enumerate(all_dates):
        if d in payment_date_index and fee_remaining > 0:
            idx = payment_date_index[d]
            # Current effective balance = balance_at[i] - total_fee_taken
            current_bal = balance_at[i] - total_fee_taken
            # Future minimum effective balance = suffix_min[i] - total_fee_taken
            # If we take X more, all future balances drop by X
            # Need: (suffix_min from i onward) - total_fee_taken - X >= 0
            future_min = suffix_min[i] - total_fee_taken
            can_take = min(fee_remaining, current_bal, future_min)
            can_take = max(can_take, 0)
            fee_allocation[idx] = can_take
            fee_remaining -= can_take
            total_fee_taken += can_take

    if fee_remaining > 0:
        return None

    return fee_allocation


def _try_schedule(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    k: int,
    shape: str,
    extra_lump: int = 0,
    lump_date: date | None = None,
    extra_monthly: int = 0,
) -> Result | None:
    """Try to build a feasible schedule with k payments. Returns Result or None."""
    total = offer_total_cents(offer)
    fee = program_fee_cents(offer, rules)
    first_pay = offer.first_payment_date or default_first_payment_date(client)
    horizon = client.last_draft_date

    dates = monthly_payment_dates(first_pay, k)
    if dates[-1] > horizon:
        return None

    payments = _build_payments(k, total, rules, shape)
    if payments is None:
        return None

    # Build a modified client if extra funds
    mod_client = client
    if extra_lump > 0 or extra_monthly > 0:
        from copy import deepcopy
        mod_client = deepcopy(client)
        if extra_lump > 0 and lump_date:
            mod_client.ledger.append(LedgerEntry(lump_date, extra_lump, "credit"))
        if extra_monthly > 0:
            new_entries = []
            for entry in mod_client.ledger:
                if entry.date > client.as_of_date and entry.type == "credit":
                    new_entries.append(LedgerEntry(entry.date, extra_monthly, "credit"))
            mod_client.ledger.extend(new_entries)

    # Pass 1: fee only on creditor-payment dates (best front-loading)
    fee_alloc = _front_load_fee(mod_client, dates, payments, fee, rules.bank_fee_cents)
    if fee_alloc is not None:
        rows = _simulate(mod_client, dates, payments, fee_alloc, rules.bank_fee_cents)
        if rows is not None:
            return Result(feasible=True, pay_shape_used=shape, schedule=rows)

    # Pass 2: add fee-only trailing dates after last creditor payment, up to horizon
    if fee > 0:
        # Generate extra monthly cadence dates after the last creditor date
        from feasibility.models import add_months, is_end_of_month, end_of_month
        last_cred_date = dates[-1]
        extra_dates = []
        i = 1
        while True:
            d = add_months(last_cred_date, i)
            if is_end_of_month(last_cred_date):
                d = end_of_month(d)
            if d > horizon:
                break
            extra_dates.append(d)
            i += 1

        if extra_dates:
            all_dates = dates + extra_dates
            all_payments = payments + [0] * len(extra_dates)
            fee_alloc = _front_load_fee(mod_client, all_dates, all_payments, fee, rules.bank_fee_cents)
            if fee_alloc is not None:
                rows = _simulate(mod_client, all_dates, all_payments, fee_alloc, rules.bank_fee_cents)
                if rows is not None:
                    return Result(feasible=True, pay_shape_used=shape, schedule=rows)

    return None


def _find_feasible(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    shape: str,
    extra_lump: int = 0,
    lump_date: date | None = None,
    extra_monthly: int = 0,
) -> Result | None:
    """Try all valid k values, return first feasible result."""
    max_k = min(rules.max_payments, rules.max_terms)
    # Try from largest k down (more payments = smaller each = more room for fee)
    for k in range(max_k, 0, -1):
        result = _try_schedule(client, offer, rules, k, shape, extra_lump, lump_date, extra_monthly)
        if result is not None:
            return result
    return None


def _is_feasible_with_lump(client: Client, offer: Offer, rules: CreditorRules, shape: str, amount: int, lump_date: date) -> bool:
    return _find_feasible(client, offer, rules, shape, extra_lump=amount, lump_date=lump_date) is not None


def _is_feasible_with_monthly(client: Client, offer: Offer, rules: CreditorRules, shape: str, amount: int) -> bool:
    return _find_feasible(client, offer, rules, shape, extra_monthly=amount) is not None


def _best_lump_date(client: Client) -> date:
    """Earliest future date for lump sum injection."""
    # Use the first future ledger credit date (first draft after as_of_date)
    future_credits = sorted(
        e.date for e in client.ledger if e.date > client.as_of_date and e.type == "credit"
    )
    if future_credits:
        return future_credits[0]
    return client.first_draft_date


def _count_future_drafts(client: Client) -> int:
    return sum(1 for e in client.ledger if e.date > client.as_of_date and e.type == "credit")


def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer. See ASSIGNMENT.md for the full specification."""
    shape = _determine_shape(rules)

    # Try to find feasible schedule
    result = _find_feasible(client, offer, rules, shape)
    if result is not None:
        return result

    # Infeasible — compute minimum extra funds
    total = offer_total_cents(offer)
    lump_date = _best_lump_date(client)
    num_drafts = _count_future_drafts(client)

    # Binary search for minimum lump sum
    lo, hi = 0, total * 2
    while lo < hi:
        mid = (lo + hi) // 2
        if _is_feasible_with_lump(client, offer, rules, shape, mid, lump_date):
            hi = mid
        else:
            lo = mid + 1
    lump_amount = lo

    # Binary search for minimum monthly increment
    lo, hi = 0, total
    while lo < hi:
        mid = (lo + hi) // 2
        if _is_feasible_with_monthly(client, offer, rules, shape, mid):
            hi = mid
        else:
            lo = mid + 1
    monthly_amount = lo

    # Guardrails
    lump_guardrail_limit = _round_half_up(0.65 * total)
    lump_ok = lump_amount <= lump_guardrail_limit
    lump_reason = "" if lump_ok else f"Lump sum {lump_amount} exceeds 65% of offer total ({lump_guardrail_limit})"

    monthly_guardrail_limit = max(10000, _round_half_up(0.40 * client.draft_amount_cents))
    monthly_ok = monthly_amount <= monthly_guardrail_limit
    monthly_reason = "" if monthly_ok else f"Monthly increment {monthly_amount} exceeds guardrail ({monthly_guardrail_limit})"

    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=AdditionalFunds(
            lump_sum=FundsOption(
                amount_cents=lump_amount,
                within_guardrail=lump_ok,
                reason=lump_reason,
                date=lump_date,
            ),
            monthly_increment=FundsOption(
                amount_cents=monthly_amount,
                within_guardrail=monthly_ok,
                reason=monthly_reason,
                num_drafts=num_drafts,
            ),
        ),
    )
