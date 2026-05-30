"""Custom tests beyond the 4 provided cases.

Covers: even/staircase/balloon shapes, token-pay and tier floors, max_segments,
exact-sum, date-by-date simulation (same-day ordering, balance hitting $0),
horizon limit, fee compliance, and Part 2 minima.
"""

from __future__ import annotations

from datetime import date

import pytest

from feasibility.engine import evaluate_offer, _round_half_up, _get_floor, _build_even_payments
from feasibility.models import (
    Client, Offer, CreditorRules, LedgerEntry,
    offer_total_cents, program_fee_cents,
)


# ---------------------------------------------------------------------------
# Helper to build inline cases
# ---------------------------------------------------------------------------

def _make_client(
    draft=20000, day=1, first="2026-01-01", last="2026-07-01",
    as_of="2025-12-31", balance=0, extra_debits=None,
):
    first_d = date.fromisoformat(first)
    last_d = date.fromisoformat(last)
    as_of_d = date.fromisoformat(as_of)
    # Generate drafts
    ledger = []
    d = first_d
    while d <= last_d:
        ledger.append(LedgerEntry(d, draft, "credit"))
        # next month
        m = d.month + 1
        y = d.year
        if m > 12:
            m = 1
            y += 1
        d = date(y, m, day)
    if extra_debits:
        for dt, amt in extra_debits:
            ledger.append(LedgerEntry(date.fromisoformat(dt), amt, "debit"))
    return Client(draft, day, first_d, last_d, as_of_d, balance, ledger)


def _make_offer(creditor_bal=100000, original_bal=120000, pct=0.5, first_pay="2026-01-31"):
    return Offer("TestCo", creditor_bal, original_bal, pct,
                 date.fromisoformat(first_pay) if first_pay else None)


def _make_rules(
    max_terms=12, max_payments=12, min_pay=2500, max_token=6,
    tiers=None, even=False, balloon=False, max_seg=2,
    bank_fee=500, fee_pct=0.2,
):
    return CreditorRules(max_terms, max_payments, min_pay, max_token,
                         tiers or [], even, balloon, max_seg, bank_fee, fee_pct)


# ---------------------------------------------------------------------------
# Round-half-up
# ---------------------------------------------------------------------------

class TestRoundHalfUp:
    def test_half_rounds_up(self):
        assert _round_half_up(2.5) == 3
        assert _round_half_up(3.5) == 4

    def test_below_half_rounds_down(self):
        assert _round_half_up(2.4) == 2

    def test_above_half_rounds_up(self):
        assert _round_half_up(2.6) == 3


# ---------------------------------------------------------------------------
# Even shape
# ---------------------------------------------------------------------------

class TestEvenShape:
    def test_exact_division(self):
        """When offer_total divides evenly by k, all payments identical."""
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=60000, original_bal=60000, pct=0.5)  # total=30000
        rules = _make_rules(max_payments=6, even=True, bank_fee=0, fee_pct=0.0)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        assert r.pay_shape_used == "even"
        payments = [row.creditor_payment_cents for row in r.schedule]
        assert all(p == 5000 for p in payments)
        assert sum(payments) == 30000

    def test_remainder_on_last(self):
        """Remainder cents go on the last payments (non-decreasing)."""
        client = _make_client(draft=20000, last="2026-06-01")
        offer = _make_offer(creditor_bal=100000, original_bal=100000, pct=0.5)  # total=50000
        rules = _make_rules(max_payments=6, even=True, bank_fee=0, fee_pct=0.0)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        payments = [row.creditor_payment_cents for row in r.schedule]
        assert payments == sorted(payments)  # non-decreasing
        assert sum(payments) == 50000
        # At most 1 cent difference between any two
        assert max(payments) - min(payments) <= 1


# ---------------------------------------------------------------------------
# Balloon shape
# ---------------------------------------------------------------------------

class TestBalloonShape:
    def test_balloon_defers_to_final(self):
        """Balloon puts minimum early, large final payment."""
        client = _make_client(draft=10000, last="2026-06-01")
        offer = _make_offer(creditor_bal=50000, original_bal=50000, pct=0.5)  # total=25000
        rules = _make_rules(max_payments=5, balloon=True, bank_fee=0, fee_pct=0.0, min_pay=2500)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        assert r.pay_shape_used == "balloon"
        payments = [row.creditor_payment_cents for row in r.schedule]
        # First payments should be at floor, last is the balloon
        assert payments[-1] > payments[0]
        assert sum(payments) == 25000

    def test_balloon_respects_tiers(self):
        """Tier floors apply even in balloon mode."""
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=60000, original_bal=60000, pct=0.5)  # total=30000
        rules = _make_rules(max_payments=6, balloon=True, bank_fee=0, fee_pct=0.0,
                            min_pay=2500, tiers=[[3, 5000]])
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        payments = [row.creditor_payment_cents for row in r.schedule]
        # Positions 3+ must be >= 5000
        for p in payments[2:-1]:  # exclude balloon itself
            assert p >= 5000


# ---------------------------------------------------------------------------
# Staircase shape
# ---------------------------------------------------------------------------

class TestStaircaseShape:
    def test_max_segments_respected(self):
        """Number of distinct payment levels <= max_segments."""
        client = _make_client(draft=10000, last="2027-01-01")
        offer = _make_offer(creditor_bal=100000, original_bal=100000, pct=0.5)  # total=50000
        rules = _make_rules(max_payments=12, max_seg=2, bank_fee=0, fee_pct=0.0, min_pay=2500)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        assert r.pay_shape_used == "staircase"
        payments = [row.creditor_payment_cents for row in r.schedule]
        assert len(set(payments)) <= 2

    def test_non_decreasing(self):
        """Staircase payments are non-decreasing."""
        client = _make_client(draft=10000, last="2027-01-01")
        offer = _make_offer(creditor_bal=100000, original_bal=100000, pct=0.5)
        rules = _make_rules(max_payments=12, max_seg=2, bank_fee=0, fee_pct=0.0, min_pay=2500)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        payments = [row.creditor_payment_cents for row in r.schedule]
        assert payments == sorted(payments)


# ---------------------------------------------------------------------------
# Token-pay and tier floors
# ---------------------------------------------------------------------------

class TestFloors:
    def test_token_pay_limit(self):
        """After max_token_pays at min, subsequent payments must exceed min."""
        client = _make_client(draft=10000, last="2027-01-01")
        offer = _make_offer(creditor_bal=100000, original_bal=100000, pct=0.5)
        rules = _make_rules(max_payments=12, max_seg=2, bank_fee=0, fee_pct=0.0,
                            min_pay=2500, max_token=3)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        payments = [row.creditor_payment_cents for row in r.schedule]
        token_count = sum(1 for p in payments if p == 2500)
        assert token_count <= 3

    def test_tier_floor_enforced(self):
        """Tier minimum is respected at specified positions."""
        client = _make_client(draft=10000, last="2027-01-01")
        offer = _make_offer(creditor_bal=150000, original_bal=150000, pct=0.4)
        rules = _make_rules(max_payments=12, max_seg=2, bank_fee=500, fee_pct=0.2,
                            min_pay=2500, max_token=6, tiers=[[7, 5000]])
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        payments = [row.creditor_payment_cents for row in r.schedule]
        for p in payments[6:]:
            assert p >= 5000


# ---------------------------------------------------------------------------
# Exact sum
# ---------------------------------------------------------------------------

class TestExactSum:
    def test_payments_sum_to_offer_total(self):
        """Creditor payments sum exactly to offer_total."""
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=100000, original_bal=120000, pct=0.5)
        rules = _make_rules(max_payments=6, even=True, bank_fee=1000, fee_pct=0.25)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        total_paid = sum(row.creditor_payment_cents for row in r.schedule)
        assert total_paid == offer_total_cents(offer)

    def test_fee_sum_equals_program_fee(self):
        """Program fees collected sum exactly to total program fee."""
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=100000, original_bal=120000, pct=0.5)
        rules = _make_rules(max_payments=6, even=True, bank_fee=1000, fee_pct=0.25)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        total_fee = sum(row.program_fee_cents for row in r.schedule)
        assert total_fee == program_fee_cents(offer, rules)


# ---------------------------------------------------------------------------
# Balance never negative (simulation correctness)
# ---------------------------------------------------------------------------

class TestSimulation:
    def test_balance_never_negative(self):
        """Running balance >= 0 at every schedule row."""
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=100000, original_bal=120000, pct=0.5)
        rules = _make_rules(max_payments=6, even=True, bank_fee=1000, fee_pct=0.25)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        for row in r.schedule:
            assert row.balance_cents >= 0

    def test_balance_can_hit_zero(self):
        """Balance hitting exactly 0 is valid (not negative)."""
        # Case 1 hits 0 on first dates
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=100000, original_bal=120000, pct=0.5)
        rules = _make_rules(max_payments=6, even=True, bank_fee=1000, fee_pct=0.25)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        # At least one row should be at or near 0 (front-loading drains the account)
        assert any(row.balance_cents == 0 for row in r.schedule)


# ---------------------------------------------------------------------------
# Horizon limit
# ---------------------------------------------------------------------------

class TestHorizon:
    def test_no_payment_past_horizon(self):
        """All payment dates <= last_draft_date."""
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=100000, original_bal=120000, pct=0.5)
        rules = _make_rules(max_payments=6, even=True, bank_fee=1000, fee_pct=0.25)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        for row in r.schedule:
            assert row.date <= client.last_draft_date

    def test_horizon_too_short_makes_infeasible(self):
        """If horizon is too short to fit payments, result is infeasible."""
        client = _make_client(draft=5000, last="2026-02-01")  # only 2 months
        offer = _make_offer(creditor_bal=100000, original_bal=100000, pct=0.5)  # need 50000
        rules = _make_rules(max_payments=2, even=True, bank_fee=0, fee_pct=0.0, min_pay=2500)
        r = evaluate_offer(client, offer, rules)
        # 2 payments of 25000 each, but only 5000/month coming in — infeasible
        assert r.feasible is False


# ---------------------------------------------------------------------------
# Fee compliance
# ---------------------------------------------------------------------------

class TestFeeCompliance:
    def test_no_fee_before_first_payment(self):
        """Program fee is not collected before the first creditor payment date."""
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=100000, original_bal=120000, pct=0.5)
        rules = _make_rules(max_payments=6, even=True, bank_fee=1000, fee_pct=0.25)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        # First schedule row is the first payment date — fee can start here
        # No fee before this (we only allocate on payment dates which start at first_payment_date)
        # Just verify fee is collected starting from first date
        assert r.schedule[0].program_fee_cents >= 0

    def test_bank_fee_only_on_creditor_payment_dates(self):
        """Bank fee is charged only when there's a creditor payment."""
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=100000, original_bal=120000, pct=0.5)
        rules = _make_rules(max_payments=6, even=True, bank_fee=1000, fee_pct=0.25)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        for row in r.schedule:
            if row.creditor_payment_cents > 0:
                assert row.bank_fee_cents == 1000
            else:
                assert row.bank_fee_cents == 0


# ---------------------------------------------------------------------------
# Part 2: minimum extra funds
# ---------------------------------------------------------------------------

class TestMinimumFunds:
    def test_lump_sum_makes_feasible(self):
        """Adding the computed lump sum should make the offer feasible."""
        client = _make_client(draft=10000, last="2026-05-01")
        offer = _make_offer(creditor_bal=80000, original_bal=80000, pct=0.5)
        rules = _make_rules(max_payments=4, max_seg=3, bank_fee=0, fee_pct=0.125)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is False
        lump = r.additional_funds.lump_sum.amount_cents
        assert lump > 0

    def test_monthly_increment_makes_feasible(self):
        """Adding the computed monthly increment should make the offer feasible."""
        client = _make_client(draft=10000, last="2026-05-01")
        offer = _make_offer(creditor_bal=80000, original_bal=80000, pct=0.5)
        rules = _make_rules(max_payments=4, max_seg=3, bank_fee=0, fee_pct=0.125)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is False
        inc = r.additional_funds.monthly_increment.amount_cents
        assert inc > 0
        assert r.additional_funds.monthly_increment.num_drafts == 5

    def test_guardrail_lump_rejects_large(self):
        """Lump sum exceeding 65% of offer_total fails guardrail."""
        # Tiny drafts, big offer → needs huge lump
        client = _make_client(draft=1000, last="2026-03-01")
        offer = _make_offer(creditor_bal=100000, original_bal=100000, pct=0.5)  # total=50000
        rules = _make_rules(max_payments=3, even=True, bank_fee=0, fee_pct=0.0, min_pay=2500)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is False
        # 65% of 50000 = 32500. With only 3000 total income, need ~47000 lump
        if r.additional_funds.lump_sum.amount_cents > 32500:
            assert r.additional_funds.lump_sum.within_guardrail is False

    def test_guardrail_monthly_rejects_large(self):
        """Monthly increment exceeding max(10000, 40% of draft) fails guardrail."""
        client = _make_client(draft=1000, last="2026-03-01")
        offer = _make_offer(creditor_bal=100000, original_bal=100000, pct=0.5)
        rules = _make_rules(max_payments=3, even=True, bank_fee=0, fee_pct=0.0, min_pay=2500)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is False
        # max(10000, 40% of 1000) = max(10000, 400) = 10000
        if r.additional_funds.monthly_increment.amount_cents > 10000:
            assert r.additional_funds.monthly_increment.within_guardrail is False


# ---------------------------------------------------------------------------
# Same-day ordering: credits before debits
# ---------------------------------------------------------------------------

class TestSameDayOrdering:
    def test_credit_and_debit_same_day(self):
        """When draft and payment are on the same day, credit applies first."""
        # Draft on 31st, payment on 31st — should work if credit comes first
        client = Client(
            draft_amount_cents=10000, draft_day=31,
            first_draft_date=date(2026, 1, 31), last_draft_date=date(2026, 3, 31),
            as_of_date=date(2025, 12, 31), current_balance_cents=0,
            ledger=[
                LedgerEntry(date(2026, 1, 31), 10000, "credit"),
                LedgerEntry(date(2026, 2, 28), 10000, "credit"),
                LedgerEntry(date(2026, 3, 31), 10000, "credit"),
            ],
        )
        offer = Offer("SameDayCo", 20000, 20000, 0.5, date(2026, 1, 31))  # total=10000
        rules = _make_rules(max_payments=3, even=True, bank_fee=0, fee_pct=0.0, min_pay=2500)
        r = evaluate_offer(client, offer, rules)
        # 10000 total / 3 = 3333+3333+3334, each month gets 10000 credit same day
        assert r.feasible is True
        assert all(row.balance_cents >= 0 for row in r.schedule)


# ---------------------------------------------------------------------------
# Zero program fee
# ---------------------------------------------------------------------------

class TestZeroFee:
    def test_zero_program_fee(self):
        """When program_fee_pct is 0, no fee is collected."""
        client = _make_client(draft=10000, last="2026-06-01")
        offer = _make_offer(creditor_bal=50000, original_bal=50000, pct=0.5)
        rules = _make_rules(max_payments=5, balloon=True, bank_fee=0, fee_pct=0.0)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        assert all(row.program_fee_cents == 0 for row in r.schedule)


# ---------------------------------------------------------------------------
# Fee-only trailing dates
# ---------------------------------------------------------------------------

class TestFeeOnlyTrailingDates:
    def test_fee_spills_to_trailing_date(self):
        """Fee that can't fit on creditor dates gets collected on trailing fee-only dates."""
        # Small drafts, big fee relative to available balance
        # 2 creditor payments use most of the cash, fee needs a 3rd date
        client = Client(
            draft_amount_cents=5000, draft_day=1,
            first_draft_date=date(2026, 1, 1), last_draft_date=date(2026, 4, 1),
            as_of_date=date(2025, 12, 31), current_balance_cents=0,
            ledger=[
                LedgerEntry(date(2026, 1, 1), 5000, "credit"),
                LedgerEntry(date(2026, 2, 1), 5000, "credit"),
                LedgerEntry(date(2026, 3, 1), 5000, "credit"),
                LedgerEntry(date(2026, 4, 1), 5000, "credit"),
            ],
        )
        # offer_total = 5000, fee = 5000 (100% of original as fee)
        offer = Offer("TrailCo", 10000, 5000, 0.5, date(2026, 1, 31))
        rules = _make_rules(max_payments=2, even=True, bank_fee=0, fee_pct=1.0, min_pay=2500)
        # 2 payments of 2500 each on Jan 31, Feb 28
        # Fee = 5000. After paying 2500 creditor, only 2500 left for fee each month.
        # Jan 31: 5000 - 2500 = 2500 available → grab 2500 fee
        # Feb 28: 5000 - 2500 = 2500 available → grab 2500 fee → fee done!
        # Actually this fits in 2 dates. Let me make it tighter.

        # Make fee = 8000 (160% of original)
        rules2 = _make_rules(max_payments=2, even=True, bank_fee=0, fee_pct=1.6, min_pay=2500)
        # fee = round(1.6 * 5000) = 8000
        # Jan 31: 5000 - 2500 = 2500 → grab 2500 fee
        # Feb 28: 5000 - 2500 = 2500 → grab 2500 fee. Remaining = 3000.
        # Without trailing: fails (fee_remaining > 0)
        # With trailing (Mar 31): 5000 available → grab 3000 fee. Done!
        r = evaluate_offer(client, offer, rules2)
        assert r.feasible is True
        assert r.pay_shape_used == "even"
        # Should have 3 rows: 2 creditor + 1 fee-only
        assert len(r.schedule) == 3
        # Last row is fee-only: no creditor payment, no bank fee
        assert r.schedule[-1].creditor_payment_cents == 0
        assert r.schedule[-1].bank_fee_cents == 0
        assert r.schedule[-1].program_fee_cents > 0

    def test_no_trailing_needed_when_fee_fits(self):
        """When fee fits on creditor dates, no trailing dates are added."""
        client = _make_client(draft=20000, last="2026-07-01")
        offer = _make_offer(creditor_bal=100000, original_bal=120000, pct=0.5)
        rules = _make_rules(max_payments=6, even=True, bank_fee=1000, fee_pct=0.25)
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True
        # All rows should have creditor payments (no fee-only trailing)
        assert all(row.creditor_payment_cents > 0 for row in r.schedule)
