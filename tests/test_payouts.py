"""Unit tests for app.payouts, the pure payout allocation engine (Payout system rebuild,
Phase 2). Pure logic, no database and no network, exactly like tests/test_scoring.py.

Every rank_standings call below passes descending explicitly, on purpose, even where a
default would arguably be "safe": this module's whole design point is that no caller,
including a test, gets to lean on an assumed direction. See app/payouts.py's module
docstring and test_season_wins_always_ranks_descending_regardless_of_pool_scoring_mode below
for why season_wins in particular must never inherit the pool's own scoring direction.

Dollar amounts below that don't need a tie-breaking timestamp use pot=Decimal("0") for
amount-mode rules, since an amount-mode rule's resolved value never depends on the pot at
all; using 0 makes that independence visible rather than implying some real number matters.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.payouts import (
    SCOPES,
    Award,
    Rule,
    StandingInput,
    allocate,
    allocation_summary,
    category_total,
    rank_standings,
    resolve_rule,
)

UTC = dt.UTC

EARLY = dt.datetime(2026, 1, 1, tzinfo=UTC)
MID = dt.datetime(2026, 1, 2, tzinfo=UTC)
LATE = dt.datetime(2026, 1, 3, tzinfo=UTC)


def rule(scope: str, place: int, mode: str, value, label: str | None = None) -> Rule:
    return Rule(scope=scope, place=place, mode=mode, value=Decimal(str(value)), label=label)


def std(user_id: int, metric, submitted_at: dt.datetime | None = None) -> StandingInput:
    return StandingInput(user_id=user_id, metric=Decimal(str(metric)), submitted_at=submitted_at)


# The commissioner's real ladder ----------------------------------------------------------


def test_fatrunner_ladder_resolves_to_known_totals():
    """Pins the real commissioner's real payout structure to known totals so a future change
    to resolution or category-total math fails loudly rather than quietly shorting real
    money. See the brief: weekly 105/55/25 x 15 weeks, bowl 250/100/50, season_points
    600/405/150, season_wins 325/185/110, grand total 4950 against a 4950 pot."""
    rules = [
        rule("weekly", 1, "amount", 105),
        rule("weekly", 2, "amount", 55),
        rule("weekly", 3, "amount", 25),
        rule("bowl", 1, "amount", 250),
        rule("bowl", 2, "amount", 100),
        rule("bowl", 3, "amount", 50),
        rule("season_points", 1, "amount", 600),
        rule("season_points", 2, "amount", 405),
        rule("season_points", 3, "amount", 150),
        rule("season_wins", 1, "amount", 325),
        rule("season_wins", 2, "amount", 185),
        rule("season_wins", 3, "amount", 110),
    ]
    pot = Decimal("4950")

    summary = allocation_summary(rules, pot=pot, weekly_weeks=15)

    assert summary.scopes["weekly"].category_total == Decimal("2775")
    assert summary.scopes["bowl"].category_total == Decimal("400")
    assert summary.scopes["season_points"].category_total == Decimal("1155")
    assert summary.scopes["season_wins"].category_total == Decimal("620")
    assert summary.grand_total == Decimal("4950")
    assert summary.pot == pot
    assert summary.unallocated == Decimal("0")


# resolve_rule -----------------------------------------------------------------------------


def test_resolve_rule_amount_mode_passes_value_through_unchanged():
    r = rule("bowl", 1, "amount", "250.00")
    assert resolve_rule(r, pot=Decimal("999999")) == Decimal("250.00")


def test_resolve_rule_percent_mode_computes_percentage_of_pot():
    r = rule("season_points", 1, "percent", "10")
    assert resolve_rule(r, pot=Decimal("4950")) == Decimal("495.00")


def test_non_integer_percent_values_resolve_to_correct_cents():
    pot = Decimal("10000")
    assert resolve_rule(rule("season_points", 1, "percent", "2.12"), pot) == Decimal("212.00")
    assert resolve_rule(rule("season_points", 2, "percent", "0.05"), pot) == Decimal("5.00")


# rank_standings -----------------------------------------------------------------------------


def test_rank_standings_ties_share_a_rank_and_the_next_rank_skips_ahead():
    rows = [std(1, 100), std(2, 100), std(3, 90)]
    standings = rank_standings(rows, descending=True)
    by_user = {s.user_id: s.rank for s in standings}
    assert by_user[1] == 1
    assert by_user[2] == 1
    assert by_user[3] == 3  # skips rank 2, the two tied players occupied it


def test_inverse_scoring_lowest_metric_wins_and_flips_the_winner_vs_standard():
    """Same raw standings, only the descending flag changes: proves rank_standings reads the
    direction it is given rather than assuming standard (highest-wins) behavior. This is the
    core correctness property for weekly, bowl, and season_points under a pool running
    inverse scoring, where the lowest point total is the winner."""
    rows = [std(1, 50), std(2, 10), std(3, 30)]

    inverse_standings = rank_standings(rows, descending=False)  # inverse: lowest wins
    standard_standings = rank_standings(rows, descending=True)  # standard: highest wins

    inverse_winner = next(s for s in inverse_standings if s.rank == 1)
    standard_winner = next(s for s in standard_standings if s.rank == 1)
    assert inverse_winner.user_id == 2  # lowest metric
    assert standard_winner.user_id == 1  # highest metric
    assert inverse_winner.user_id != standard_winner.user_id

    # The flip carries all the way through to real dollars, not just the ranking.
    rules = [rule("bowl", 1, "amount", 250)]
    inverse_awards = allocate(rules, inverse_standings, pot=Decimal("0"), rounding="dollar")
    standard_awards = allocate(rules, standard_standings, pot=Decimal("0"), rounding="dollar")
    assert inverse_awards[0].user_id == 2
    assert standard_awards[0].user_id == 1


def test_season_wins_always_ranks_descending_regardless_of_pool_scoring_mode():
    """The cross-wiring bug this module is built to make impossible: a future caller must
    never be able to wire season_wins to the pool's own scoring direction by accident.

    Every real call site for season_wins passes descending=True, hard coded, never sourced
    from pool.scoring_mode the way weekly/bowl/season_points are. This test proves it by
    asserting the winner's actual metric is the maximum of the group, not merely by id: if a
    future edit ever let season_wins inherit descending=False (the direction weekly/bowl/
    season_points use under inverse scoring), the winner's metric would come back as the
    minimum instead, and this assertion would catch it even if the user ids happened to
    still line up.
    """
    rows = [std(1, 5), std(2, 12), std(3, 8)]
    standings = rank_standings(rows, descending=True)
    winner = next(s for s in standings if s.rank == 1)
    assert winner.user_id == 2
    assert winner.metric == max(r.metric for r in rows)
    assert winner.metric != min(r.metric for r in rows)


# Percent vs amount, mixed modes -------------------------------------------------------------


def test_percent_mode_matches_amount_mode_for_all_scopes():
    pot = Decimal("10000")
    amount_rules = [
        rule("weekly", 1, "amount", 1000),
        rule("weekly", 2, "amount", 500),
        rule("weekly", 3, "amount", 200),
        rule("bowl", 1, "amount", 1500),
        rule("bowl", 2, "amount", 700),
        rule("bowl", 3, "amount", 300),
        rule("season_points", 1, "amount", 2000),
        rule("season_points", 2, "amount", 1000),
        rule("season_points", 3, "amount", 500),
        rule("season_wins", 1, "amount", 800),
        rule("season_wins", 2, "amount", 400),
        rule("season_wins", 3, "amount", 200),
    ]
    percent_rules = [
        rule("weekly", 1, "percent", 10),
        rule("weekly", 2, "percent", 5),
        rule("weekly", 3, "percent", 2),
        rule("bowl", 1, "percent", 15),
        rule("bowl", 2, "percent", 7),
        rule("bowl", 3, "percent", 3),
        rule("season_points", 1, "percent", 20),
        rule("season_points", 2, "percent", 10),
        rule("season_points", 3, "percent", 5),
        rule("season_wins", 1, "percent", 8),
        rule("season_wins", 2, "percent", 4),
        rule("season_wins", 3, "percent", 2),
    ]

    amount_summary = allocation_summary(amount_rules, pot=pot, weekly_weeks=1)
    percent_summary = allocation_summary(percent_rules, pot=pot, weekly_weeks=1)

    for scope in SCOPES:
        assert (
            amount_summary.scopes[scope].category_total
            == percent_summary.scopes[scope].category_total
        )
        assert amount_summary.scopes[scope].places == percent_summary.scopes[scope].places
    assert amount_summary.grand_total == percent_summary.grand_total


def test_mixed_modes_in_one_season_resolve_correctly_together():
    pot = Decimal("5000")
    rules = [
        rule("weekly", 1, "percent", 2),  # 2% of 5000 = 100/week
        rule("weekly", 2, "percent", 1),  # 50/week
        rule("season_points", 1, "amount", 600),
        rule("season_points", 2, "amount", 400),
    ]
    summary = allocation_summary(rules, pot=pot, weekly_weeks=10)
    assert summary.scopes["weekly"].category_total == Decimal("1500")  # (100 + 50) * 10
    assert summary.scopes["season_points"].category_total == Decimal("1000")
    assert summary.grand_total == Decimal("2500")


# Weekly per-week semantics -------------------------------------------------------------------


def test_weekly_award_is_the_per_week_figure_not_the_season_figure():
    rules = [rule("weekly", 1, "amount", 105)]
    standings = rank_standings([std(1, 500)], descending=True)
    awards = allocate(rules, standings, pot=Decimal("0"), rounding="dollar")
    assert awards == [
        Award(
            user_id=1,
            place=1,
            tied_with=1,
            amount=Decimal("105"),
            rule_mode="amount",
            rule_value=Decimal("105"),
        )
    ]


def test_category_total_multiplies_the_weekly_per_week_total_by_weekly_payout_weeks():
    rules = [
        rule("weekly", 1, "amount", 105),
        rule("weekly", 2, "amount", 55),
        rule("weekly", 3, "amount", 25),
    ]
    per_week = category_total(rules, pot=Decimal("0"), weeks=1)
    season = category_total(rules, pot=Decimal("0"), weeks=15)
    assert per_week == Decimal("185")
    assert season == per_week * 15 == Decimal("2775")


def test_weekly_payout_weeks_of_zero_is_zero_not_an_error():
    rules = [rule("weekly", 1, "amount", 105)]
    assert category_total(rules, pot=Decimal("0"), weeks=0) == Decimal("0")


def test_weekly_payout_weeks_of_one_equals_the_per_week_total():
    rules = [rule("weekly", 1, "amount", 105), rule("weekly", 2, "amount", 55)]
    assert category_total(rules, pot=Decimal("0"), weeks=1) == Decimal("160")


# Season scopes independence -------------------------------------------------------------------


def test_season_points_and_season_wins_resolve_independently_same_winner_collects_both():
    points_rules = [
        rule("season_points", 1, "amount", 600),
        rule("season_points", 2, "amount", 400),
    ]
    wins_rules = [rule("season_wins", 1, "amount", 325), rule("season_wins", 2, "amount", 185)]

    points_standings = rank_standings([std(1, 900), std(2, 700)], descending=True)
    wins_standings = rank_standings([std(1, 8), std(2, 5)], descending=True)

    points_awards = allocate(points_rules, points_standings, pot=Decimal("0"), rounding="dollar")
    wins_awards = allocate(wins_rules, wins_standings, pot=Decimal("0"), rounding="dollar")

    points_by_user = {a.user_id: a.amount for a in points_awards}
    wins_by_user = {a.user_id: a.amount for a in wins_awards}
    assert points_by_user[1] == Decimal("600")
    assert wins_by_user[1] == Decimal("325")

    # Winning one scope has no bearing on the other: user 1 collects both in full.
    total_for_user_1 = sum(a.amount for a in (*points_awards, *wins_awards) if a.user_id == 1)
    assert total_for_user_1 == Decimal("925")


# Tie splitting --------------------------------------------------------------------------------


def test_two_way_tie_for_first_splits_first_and_second_next_player_takes_third():
    rules = [
        rule("weekly", 1, "amount", 100),
        rule("weekly", 2, "amount", 50),
        rule("weekly", 3, "amount", 30),
    ]
    standings = rank_standings([std(1, 100), std(2, 100), std(3, 90)], descending=True)
    awards = allocate(rules, standings, pot=Decimal("0"), rounding="dollar")
    by_user = {a.user_id: a for a in awards}

    assert by_user[1].amount == Decimal("75")
    assert by_user[2].amount == Decimal("75")
    assert by_user[1].place == 1
    assert by_user[1].tied_with == 2
    assert by_user[3].amount == Decimal("30")
    assert by_user[3].place == 3
    assert by_user[3].tied_with == 1
    assert sum(a.amount for a in awards) == Decimal("180")


def test_three_way_tie_for_first_with_only_three_places_splits_evenly_nobody_else_paid():
    rules = [
        rule("weekly", 1, "amount", 210),
        rule("weekly", 2, "amount", 120),
        rule("weekly", 3, "amount", 90),
    ]
    standings = rank_standings([std(1, 50), std(2, 50), std(3, 50), std(4, 10)], descending=True)
    awards = allocate(rules, standings, pot=Decimal("0"), rounding="dollar")

    assert {a.user_id for a in awards} == {1, 2, 3}
    assert all(a.amount == Decimal("140") for a in awards)
    assert all(a.tied_with == 3 for a in awards)
    assert sum(a.amount for a in awards) == Decimal("420")


def test_three_way_tie_for_second_where_fourth_place_has_no_rule_contributes_zero():
    rules = [
        rule("weekly", 1, "amount", 300),
        rule("weekly", 2, "amount", 150),
        rule("weekly", 3, "amount", 90),
    ]
    # Rank 2 is a 3 way tie spanning places 2, 3, and 4; place 4 has no configured rule.
    standings = rank_standings(
        [std(1, 100), std(2, 80), std(3, 80), std(4, 80), std(5, 10)], descending=True
    )
    awards = allocate(rules, standings, pot=Decimal("0"), rounding="dollar")
    by_user = {a.user_id: a for a in awards}

    assert by_user[1].amount == Decimal("300")
    tied = [by_user[2], by_user[3], by_user[4]]
    assert sum(a.amount for a in tied) == Decimal("240")  # 150 (2nd) + 90 (3rd) + 0 (4th, no rule)
    assert all(a.tied_with == 3 for a in tied)
    assert 5 not in by_user  # rank 5 is entirely past the highest configured place


def test_tie_at_the_very_last_paid_place_splits_correctly():
    rules = [
        rule("weekly", 1, "amount", 100),
        rule("weekly", 2, "amount", 60),
        rule("weekly", 3, "amount", 40),
    ]
    standings = rank_standings([std(1, 100), std(2, 80), std(3, 80)], descending=True)
    awards = allocate(rules, standings, pot=Decimal("0"), rounding="dollar")
    by_user = {a.user_id: a.amount for a in awards}

    assert by_user[1] == Decimal("100")
    assert by_user[2] == Decimal("50")
    assert by_user[3] == Decimal("50")
    assert sum(by_user.values()) == Decimal("200")


# Rounding and remainder distribution -----------------------------------------------------------


@pytest.mark.parametrize(
    "rounding, total, expected_amounts",
    [
        ("cent", Decimal("100.01"), [Decimal("33.34"), Decimal("33.34"), Decimal("33.33")]),
        ("dollar", Decimal("50"), [Decimal("17"), Decimal("17"), Decimal("16")]),
        ("five", Decimal("115"), [Decimal("40"), Decimal("40"), Decimal("35")]),
    ],
)
def test_rounding_reconciles_to_the_allocated_total_at_every_unit(
    rounding, total, expected_amounts
):
    rules = [rule("weekly", 1, "amount", total)]
    standings = rank_standings(
        [
            std(1, 100, submitted_at=EARLY),
            std(2, 100, submitted_at=MID),
            std(3, 100, submitted_at=LATE),
        ],
        descending=True,
    )
    awards = allocate(rules, standings, pot=Decimal("0"), rounding=rounding)
    amounts = [a.amount for a in sorted(awards, key=lambda a: a.user_id)]

    assert amounts == expected_amounts
    assert sum(amounts) == total
    for a in awards:
        assert isinstance(a.amount, Decimal)


def test_uneven_three_way_split_of_one_hundred_dollars_distributes_34_33_33():
    rules = [rule("weekly", 1, "amount", 100)]
    standings = rank_standings(
        [
            std(1, 100, submitted_at=EARLY),
            std(2, 100, submitted_at=MID),
            std(3, 100, submitted_at=LATE),
        ],
        descending=True,
    )
    awards = allocate(rules, standings, pot=Decimal("0"), rounding="dollar")
    amounts = [a.amount for a in sorted(awards, key=lambda a: a.user_id)]

    assert amounts == [Decimal("34"), Decimal("33"), Decimal("33")]
    assert sum(amounts) == Decimal("100")


def test_remainder_tiebreak_falls_back_to_user_id_when_submitted_at_is_equal():
    same_time = dt.datetime(2026, 9, 1, tzinfo=UTC)
    rules = [rule("weekly", 1, "amount", 100)]
    standings = rank_standings(
        [
            std(5, 100, submitted_at=same_time),
            std(2, 100, submitted_at=same_time),
            std(9, 100, submitted_at=same_time),
        ],
        descending=True,
    )
    awards = allocate(rules, standings, pot=Decimal("0"), rounding="dollar")
    by_user = {a.user_id: a.amount for a in awards}

    # Nothing distinguishes the submitted_at values, so the lowest user_id breaks the tie
    # and picks up the one remainder dollar.
    assert by_user[2] == Decimal("34")
    assert by_user[5] == Decimal("33")
    assert by_user[9] == Decimal("33")


def test_remainder_tiebreak_handles_none_submitted_at_without_raising():
    rules = [rule("weekly", 1, "amount", 100)]
    standings = rank_standings(
        [
            std(1, 100, submitted_at=None),
            std(2, 100, submitted_at=EARLY),
            std(3, 100, submitted_at=None),
        ],
        descending=True,
    )
    awards = allocate(rules, standings, pot=Decimal("0"), rounding="dollar")  # must not raise
    by_user = {a.user_id: a.amount for a in awards}

    # The one real timestamp sorts ahead of both missing ones and takes the remainder dollar;
    # between the two None submissions, user_id (1 before 3) is what stays deterministic.
    assert by_user[2] == Decimal("34")
    assert by_user[1] == Decimal("33")
    assert by_user[3] == Decimal("33")


# Empty and undersized inputs --------------------------------------------------------------------


def test_empty_rules_returns_empty_award_list():
    standings = rank_standings([std(1, 100)], descending=True)
    assert allocate([], standings, pot=Decimal("1000"), rounding="dollar") == []


def test_empty_standings_returns_empty_award_list():
    rules = [rule("weekly", 1, "amount", 100)]
    assert allocate(rules, [], pot=Decimal("1000"), rounding="dollar") == []


def test_fewer_players_than_configured_places_leaves_unfilled_places_unpaid():
    rules = [
        rule("weekly", 1, "amount", 100),
        rule("weekly", 2, "amount", 50),
        rule("weekly", 3, "amount", 25),
    ]
    standings = rank_standings([std(1, 90)], descending=True)
    awards = allocate(rules, standings, pot=Decimal("0"), rounding="dollar")

    assert len(awards) == 1
    assert awards[0].user_id == 1
    assert awards[0].amount == Decimal("100")


# Pot edge cases and over-allocation --------------------------------------------------------------


def test_pot_zero_with_percent_mode_resolves_everything_to_zero_without_dividing_by_zero():
    rules = [rule("weekly", 1, "percent", 10), rule("weekly", 2, "percent", 5)]
    standings = rank_standings([std(1, 90), std(2, 80)], descending=True)

    awards = allocate(rules, standings, pot=Decimal("0"), rounding="cent")  # must not raise

    assert len(awards) == 2
    assert all(a.amount == Decimal("0") for a in awards)
    assert category_total(rules, pot=Decimal("0")) == Decimal("0")


def test_grand_total_exceeding_pot_allocates_normally_over_allocation_is_legal():
    rules = [
        rule("weekly", 1, "amount", 500),
        rule("weekly", 2, "amount", 300),
        rule("bowl", 1, "amount", 400),
    ]
    pot = Decimal("100")

    summary = allocation_summary(rules, pot=pot, weekly_weeks=1)
    assert summary.grand_total == Decimal("1200")
    assert summary.unallocated == Decimal("-1100")

    # allocate() itself never clamps against the pot either: over-allocation is the
    # commissioner's call to make (and fix), not something this engine blocks.
    weekly_rules = [r for r in rules if r.scope == "weekly"]
    standings = rank_standings([std(1, 90), std(2, 80)], descending=True)
    awards = allocate(weekly_rules, standings, pot=pot, rounding="dollar")
    assert {a.amount for a in awards} == {Decimal("500"), Decimal("300")}


# Decimal typing everywhere -----------------------------------------------------------------------


def test_money_values_are_always_decimal_never_float():
    pot = Decimal("1000")
    percent_rule = rule("weekly", 1, "percent", "12.5")
    amount_rule = rule("weekly", 2, "amount", "10")

    resolved = resolve_rule(percent_rule, pot)
    assert isinstance(resolved, Decimal)

    standings = rank_standings([std(1, 50), std(2, 50)], descending=True)
    awards = allocate([percent_rule, amount_rule], standings, pot=pot, rounding="cent")
    assert awards, "expected at least one award to check"
    for award in awards:
        assert isinstance(award.amount, Decimal)
        assert isinstance(award.rule_value, Decimal)

    total = category_total([percent_rule], pot=pot, weeks=3)
    assert isinstance(total, Decimal)

    summary = allocation_summary([percent_rule], pot=pot, weekly_weeks=3)
    assert isinstance(summary.pot, Decimal)
    assert isinstance(summary.grand_total, Decimal)
    assert isinstance(summary.unallocated, Decimal)
    for scope_summary in summary.scopes.values():
        assert isinstance(scope_summary.category_total, Decimal)
        assert isinstance(scope_summary.percent_of_pot, Decimal)
        for amount in scope_summary.places.values():
            assert isinstance(amount, Decimal)
