"""Unit tests for app.slate, per league closest spread selection (spec Section 6).

Pure logic only. No database, no network, no fixtures.

The product rule under test: take the closest target_nfl NFL games and the
closest target_ncaaf college games, 8 and 12 by default for a total of 20, and
fill across leagues when one league is short.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import random
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app import slate as slate_module
from app.slate import (
    LEAGUE_LABELS,
    LEAGUES,
    Candidate,
    Selected,
    SlateResult,
    closeness_of,
    compute_lock_at,
    select_slate_by_targets,
)

# A fixed Sunday so every test reads the same way. Kickoffs are offsets from it.
BASE = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
NOW = BASE - timedelta(hours=5)

DEFAULT_TARGETS = {"nfl": 8, "ncaaf": 12}
DEFAULT_TOTAL = 20


def game(
    key: str,
    spread: float | None = 0.0,
    league: str = "nfl",
    hours: float = 0.0,
) -> Candidate:
    """Build a Candidate with readable defaults."""
    return Candidate(
        key=key,
        league=league,
        kickoff=BASE + timedelta(hours=hours),
        spread_home=spread,
    )


def pool(prefix: str, league: str, spreads: list[float]) -> list[Candidate]:
    """A league's worth of games, keyed prefix01, prefix02 and so on.

    Kickoffs step an hour apart so the spread, not the clock, decides the order.
    """
    return [
        game(f"{prefix}{index:02d}", spread=value, league=league, hours=float(index))
        for index, value in enumerate(spreads, start=1)
    ]


def steps(count: int, first: float, step: float = 1.0) -> list[float]:
    """A run of spreads, closest first."""
    return [first + step * index for index in range(count)]


def keys(result: SlateResult) -> list[str]:
    return [item.key for item in result.selected]


# The default shape: 8 NFL and 12 college out of a rich pool


def rich_pool() -> list[Candidate]:
    """12 NFL games at 1.0 to 12.0 and 16 college games at 0.5 to 15.5.

    Whole numbers for the NFL and halves for college, so nothing ties and the
    expected order can be written out by hand.
    """
    return pool("nfl", "nfl", steps(12, 1.0)) + pool("cfb", "ncaaf", steps(16, 0.5))


def test_default_shape_takes_eight_nfl_and_twelve_college():
    result = select_slate_by_targets(rich_pool(), DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    assert keys(result) == [
        "cfb01",
        "nfl01",
        "cfb02",
        "nfl02",
        "cfb03",
        "nfl03",
        "cfb04",
        "nfl04",
        "cfb05",
        "nfl05",
        "cfb06",
        "nfl06",
        "cfb07",
        "nfl07",
        "cfb08",
        "nfl08",
        "cfb09",
        "cfb10",
        "cfb11",
        "cfb12",
    ]
    assert [item.slate_rank for item in result.selected] == list(range(1, 21))
    assert result.per_league == {"nfl": 8, "ncaaf": 12}
    assert result.shortfalls == {}
    assert result.filled_from_other == 0
    assert result.notes == []


def test_default_shape_ranks_by_closeness_not_by_league():
    result = select_slate_by_targets(rich_pool(), DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    assert [item.closeness for item in result.selected[:4]] == [0.5, 1.0, 1.5, 2.0]
    assert result.selected[-1].closeness == 11.5


def test_the_farthest_games_in_each_league_are_left_off():
    result = select_slate_by_targets(rich_pool(), DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)
    chosen = set(keys(result))

    # NFL 9 through 12 (spreads 9.0 to 12.0) and college 13 through 16
    # (spreads 12.5 to 15.5) are the ones each league leaves behind.
    assert chosen.isdisjoint({"nfl09", "nfl10", "nfl11", "nfl12"})
    assert chosen.isdisjoint({"cfb13", "cfb14", "cfb15", "cfb16"})


def test_within_each_league_the_closest_games_are_taken():
    candidates = [
        game("nfl_far", spread=-9.5, league="nfl", hours=1),
        game("nfl_close", spread=1.0, league="nfl", hours=2),
        game("nfl_mid", spread=-4.0, league="nfl", hours=3),
        game("cfb_far", spread=21.0, league="ncaaf", hours=4),
        game("cfb_close", spread=-0.5, league="ncaaf", hours=5),
        game("cfb_mid", spread=6.0, league="ncaaf", hours=6),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 2, "ncaaf": 2}, total=4, now=NOW)

    assert keys(result) == ["cfb_close", "nfl_close", "nfl_mid", "cfb_mid"]
    assert result.per_league == {"nfl": 2, "ncaaf": 2}
    assert result.shortfalls == {}
    assert result.filled_from_other == 0


def test_a_far_nfl_game_beats_a_closer_college_game_until_the_target_is_met():
    # This is the whole point of per league targets. A pure global top 20 would
    # be 20 college games and no NFL at all.
    candidates = pool("cfb", "ncaaf", steps(20, 0.5, 0.5)) + pool("nfl", "nfl", steps(8, 14.0))
    result = select_slate_by_targets(candidates, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)
    chosen = set(keys(result))

    assert result.per_league == {"nfl": 8, "ncaaf": 12}
    # nfl08 is a 21 point spread and still makes the slate.
    assert "nfl08" in chosen
    # cfb13 is a 6.5 point spread, closer than every NFL game, and does not.
    assert "cfb13" not in chosen
    assert chosen.isdisjoint({f"cfb{index}" for index in range(13, 21)})
    # The NFL games are the farthest on the slate, so they rank last.
    assert keys(result)[12:] == [f"nfl{index:02d}" for index in range(1, 9)]


# A short league fills from the other one


def test_a_short_nfl_week_is_filled_with_college_games():
    candidates = (
        pool("nfl", "nfl", steps(5, 3.0))
        + [
            game("nfl_no_line_a", spread=None, league="nfl", hours=7),
            game("nfl_no_line_b", spread=None, league="nfl", hours=8),
        ]
        + pool("cfb", "ncaaf", steps(20, 0.5))
    )
    result = select_slate_by_targets(candidates, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    assert len(result.selected) == 20
    assert result.per_league == {"nfl": 5, "ncaaf": 15}
    assert result.shortfalls == {"nfl": 3}
    assert result.filled_from_other == 3
    assert result.notes == [
        "Only 5 NFL games had a resolvable spread, 3 short of the target of 8.",
        "The gap was filled with the next closest college games.",
    ]
    # The three extra college games are the next closest ones, 13 through 15.
    assert {"cfb13", "cfb14", "cfb15"} <= set(keys(result))
    assert "cfb16" not in keys(result)


def test_a_short_nfl_week_fills_in_exact_closeness_order():
    # Identity, not just counts. A fill that walked the pool from the far end
    # would still return 20 games and still report nfl 5 and ncaaf 15.
    candidates = (
        pool("nfl", "nfl", steps(5, 3.0))
        + [
            game("nfl_no_line_a", spread=None, league="nfl", hours=7),
            game("nfl_no_line_b", spread=None, league="nfl", hours=8),
        ]
        + pool("cfb", "ncaaf", steps(20, 0.5))
    )
    result = select_slate_by_targets(candidates, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    assert keys(result) == [
        "cfb01",
        "cfb02",
        "cfb03",
        "nfl01",
        "cfb04",
        "nfl02",
        "cfb05",
        "nfl03",
        "cfb06",
        "nfl04",
        "cfb07",
        "nfl05",
        "cfb08",
        "cfb09",
        "cfb10",
        "cfb11",
        "cfb12",
        "cfb13",
        "cfb14",
        "cfb15",
    ]
    # The three fill games are the closest ones left, 12.5, 13.5 and 14.5, not
    # the farthest, 17.5, 18.5 and 19.5.
    assert [item.closeness for item in result.selected[-3:]] == [12.5, 13.5, 14.5]


def test_a_short_college_week_is_filled_with_nfl_games():
    candidates = pool("nfl", "nfl", steps(14, 1.0)) + pool("cfb", "ncaaf", steps(9, 0.5))
    result = select_slate_by_targets(candidates, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    assert len(result.selected) == 20
    assert result.per_league == {"nfl": 11, "ncaaf": 9}
    assert result.shortfalls == {"ncaaf": 3}
    assert result.filled_from_other == 3
    assert "The gap was filled with the next closest NFL games." in result.notes


def test_both_leagues_short_returns_fewer_than_the_total_and_says_so():
    candidates = pool("nfl", "nfl", steps(3, 1.0)) + pool("cfb", "ncaaf", steps(4, 0.5))
    result = select_slate_by_targets(candidates, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    assert len(result.selected) == 7
    assert result.per_league == {"nfl": 3, "ncaaf": 4}
    assert result.shortfalls == {"nfl": 5, "ncaaf": 8}
    assert result.filled_from_other == 0
    assert result.notes == [
        "Only 3 NFL games had a resolvable spread, 5 short of the target of 8.",
        "Only 4 college games had a resolvable spread, 8 short of the target of 12.",
        "The slate came up 13 games short of 20 because only 7 games had a " "resolvable spread.",
    ]


def test_a_single_missing_game_reads_in_the_singular():
    candidates = [
        game("nfl01", spread=1.0, league="nfl", hours=1),
        game("nfl02", spread=2.0, league="nfl", hours=2),
        game("cfb01", spread=0.5, league="ncaaf", hours=3),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 2, "ncaaf": 2}, total=4, now=NOW)

    assert result.shortfalls == {"ncaaf": 1}
    assert result.notes == [
        "Only 1 college game had a resolvable spread, 1 short of the target of 2.",
        "The slate came up 1 game short of 4 because only 3 games had a " "resolvable spread.",
    ]


def test_a_league_short_because_its_games_kicked_off_does_not_blame_the_spread():
    # Every NFL spread here is resolved. Three of the four games have simply
    # started, which is the normal state of a Sunday afternoon rebuild. Saying
    # the spread was missing would send the commissioner chasing a line that is
    # sitting right there.
    candidates = (
        [game(f"nfl_done{index}", spread=float(index), hours=-20 - index) for index in (1, 2, 3)]
        + [game("nfl_next", spread=6.0, hours=4)]
        + pool("cfb", "ncaaf", steps(3, 0.5))
    )
    result = select_slate_by_targets(candidates, {"nfl": 3, "ncaaf": 3}, total=6, now=NOW)

    assert keys(result) == ["cfb01", "cfb02", "cfb03", "nfl_next"]
    assert result.shortfalls == {"nfl": 2}
    assert result.notes == [
        "Only 1 NFL game had a spread and had not kicked off, 2 short of the " "target of 3.",
        "The slate came up 2 games short of 6 because only 4 games had a spread "
        "and had not kicked off.",
    ]


def test_keeping_started_games_keeps_the_spread_wording():
    # Nothing was taken by the clock here, so the note names the only cause left.
    candidates = [
        game("started", spread=0.0, hours=-8),
        game("no_line", spread=None, hours=4),
    ]
    result = select_slate_by_targets(
        candidates, {"nfl": 2}, total=2, now=NOW, exclude_started=False
    )

    assert keys(result) == ["started"]
    assert result.notes == [
        "Only 1 NFL game had a resolvable spread, 1 short of the target of 2.",
        "The slate came up 1 game short of 2 because only 1 game had a " "resolvable spread.",
    ]


# Targets that do not add up to the total


def test_targets_above_the_total_drop_the_farthest_games():
    candidates = pool("nfl", "nfl", steps(12, 1.0)) + pool("cfb", "ncaaf", steps(12, 0.5))
    result = select_slate_by_targets(candidates, {"nfl": 12, "ncaaf": 12}, total=20, now=NOW)
    chosen = set(keys(result))

    assert len(result.selected) == 20
    assert result.per_league == {"nfl": 10, "ncaaf": 10}
    assert result.shortfalls == {}
    assert result.filled_from_other == 0
    # 12.0, 11.5, 11.0 and 10.5 are the four farthest of the 24 first pass games.
    assert chosen.isdisjoint({"nfl11", "nfl12", "cfb11", "cfb12"})
    assert result.notes == [
        "The league targets add up to 24, which is more than the total of 20, "
        "so the 4 farthest games were dropped."
    ]


def test_the_trim_keeps_the_closest_games_and_drops_the_farthest():
    # Identity of the survivors, spelled out. An inverted trim would keep the
    # 20 worst games of the 24 and still report 20 games and nfl 10, ncaaf 10.
    candidates = pool("nfl", "nfl", steps(12, 1.0)) + pool("cfb", "ncaaf", steps(12, 0.5))
    result = select_slate_by_targets(candidates, {"nfl": 12, "ncaaf": 12}, total=20, now=NOW)

    assert keys(result) == [
        "cfb01",
        "nfl01",
        "cfb02",
        "nfl02",
        "cfb03",
        "nfl03",
        "cfb04",
        "nfl04",
        "cfb05",
        "nfl05",
        "cfb06",
        "nfl06",
        "cfb07",
        "nfl07",
        "cfb08",
        "nfl08",
        "cfb09",
        "nfl09",
        "cfb10",
        "nfl10",
    ]
    # Every survivor is closer than every game the trim dropped.
    assert result.selected[-1].closeness == 10.0
    assert max(item.closeness for item in result.selected) < 10.5


def test_a_shortfall_is_measured_on_the_first_pass_not_on_the_trimmed_slate():
    # The NFL supply is 10 against a target of 12, so the shortfall is 2. The
    # trim then cuts the 22 first pass games down to 15 and leaves 7 NFL games
    # on the slate. Reading the shortfall off the final slate would say 5, and
    # would blame missing spreads for a cut the commissioner's own total made.
    candidates = pool("nfl", "nfl", steps(10, 1.0)) + pool("cfb", "ncaaf", steps(12, 0.5))
    result = select_slate_by_targets(candidates, {"nfl": 12, "ncaaf": 12}, total=15, now=NOW)

    assert len(result.selected) == 15
    assert result.per_league == {"nfl": 7, "ncaaf": 8}
    assert result.shortfalls == {"nfl": 2}
    assert result.notes == [
        "Only 10 NFL games had a resolvable spread, 2 short of the target of 12.",
        "The league targets add up to 24, which is more than the total of 15, "
        "so the 7 farthest games were dropped.",
    ]


def test_a_league_cut_below_its_target_by_the_trim_is_not_a_shortfall():
    # Both leagues have all 12 games. Neither came up short of anything, the
    # total simply cannot hold 24, so shortfalls must stay empty.
    candidates = pool("nfl", "nfl", steps(12, 1.0)) + pool("cfb", "ncaaf", steps(12, 0.5))
    result = select_slate_by_targets(candidates, {"nfl": 12, "ncaaf": 12}, total=20, now=NOW)

    assert result.per_league == {"nfl": 10, "ncaaf": 10}
    assert result.shortfalls == {}


def test_targets_below_the_total_fill_the_rest_globally_by_closeness():
    candidates = pool("nfl", "nfl", steps(12, 1.0)) + pool("cfb", "ncaaf", steps(12, 0.5))
    result = select_slate_by_targets(candidates, {"nfl": 5, "ncaaf": 5}, total=20, now=NOW)
    chosen = set(keys(result))

    assert len(result.selected) == 20
    assert result.per_league == {"nfl": 10, "ncaaf": 10}
    assert result.shortfalls == {}
    assert result.filled_from_other == 10
    assert chosen.isdisjoint({"nfl11", "nfl12", "cfb11", "cfb12"})
    assert result.notes == [
        "The league targets add up to 10, which is less than the total of 20, "
        "so the 10 next closest games were added."
    ]


def test_dropping_exactly_one_game_reads_in_the_singular():
    candidates = pool("nfl", "nfl", steps(2, 1.0)) + pool("cfb", "ncaaf", steps(2, 0.5))
    result = select_slate_by_targets(candidates, {"nfl": 2, "ncaaf": 2}, total=3, now=NOW)

    assert keys(result) == ["cfb01", "nfl01", "cfb02"]
    assert result.notes == [
        "The league targets add up to 4, which is more than the total of 3, "
        "so the 1 farthest game was dropped."
    ]


def test_targets_that_match_the_total_exactly_produce_no_notes():
    candidates = pool("nfl", "nfl", steps(6, 1.0)) + pool("cfb", "ncaaf", steps(6, 0.5))
    result = select_slate_by_targets(candidates, {"nfl": 3, "ncaaf": 3}, total=6, now=NOW)

    assert result.per_league == {"nfl": 3, "ncaaf": 3}
    assert result.notes == []


# Leagues with no target of their own


def test_a_league_absent_from_targets_still_joins_the_fill():
    candidates = pool("nfl", "nfl", steps(3, 5.0)) + pool("cfb", "ncaaf", steps(3, 0.5))
    result = select_slate_by_targets(candidates, {"nfl": 2}, total=4, now=NOW)

    assert keys(result) == ["cfb01", "cfb02", "nfl01", "nfl02"]
    assert result.per_league == {"nfl": 2, "ncaaf": 2}
    assert result.shortfalls == {}
    assert result.filled_from_other == 2


def test_an_unknown_league_is_selectable_through_the_fill():
    candidates = [
        game("xfl1", spread=0.0, league="xfl", hours=1),
        game("nfl1", spread=8.0, league="nfl", hours=2),
        game("nfl2", spread=9.0, league="nfl", hours=3),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 1}, total=2, now=NOW)

    assert keys(result) == ["xfl1", "nfl1"]
    assert result.per_league == {"nfl": 1, "xfl": 1}
    assert result.filled_from_other == 1


def test_empty_targets_fall_back_to_a_plain_closest_first_slate():
    candidates = [
        game("a", spread=-7.0, league="nfl", hours=1),
        game("b", spread=1.5, league="ncaaf", hours=2),
        game("c", spread=-3.0, league="nfl", hours=3),
        game("d", spread=0.5, league="ncaaf", hours=4),
        game("e", spread=14.0, league="ncaaf", hours=5),
    ]
    result = select_slate_by_targets(candidates, {}, total=3, now=NOW)

    assert keys(result) == ["d", "b", "c"]
    assert result.per_league == {"ncaaf": 2, "nfl": 1}
    assert result.shortfalls == {}
    assert result.filled_from_other == 3
    assert result.notes == []


def test_targets_of_none_behave_like_an_empty_mapping():
    # The annotation allows None, so pin what it does rather than leaving the
    # caller to find out. A pool with no leagues enabled hands over nothing.
    candidates = [
        game("a", spread=-7.0, league="nfl", hours=1),
        game("b", spread=1.5, league="ncaaf", hours=2),
        game("c", spread=-3.0, league="nfl", hours=3),
        game("d", spread=0.5, league="ncaaf", hours=4),
        game("e", spread=14.0, league="ncaaf", hours=5),
    ]
    empty = select_slate_by_targets(candidates, {}, total=3, now=NOW)

    assert repr(select_slate_by_targets(candidates, None, total=3, now=NOW)) == repr(empty)


def test_a_target_of_zero_leaves_a_league_to_the_fill_only():
    candidates = [
        game("nfl1", spread=0.0, league="nfl", hours=1),
        game("cfb1", spread=4.0, league="ncaaf", hours=2),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 0, "ncaaf": 1}, total=2, now=NOW)

    assert keys(result) == ["nfl1", "cfb1"]
    assert result.shortfalls == {}
    assert result.filled_from_other == 1


# Closeness and the spread values that cannot be used


def test_closeness_of_handles_sign_and_none():
    assert closeness_of(0.0) == 0.0
    assert closeness_of(-2.5) == 2.5
    assert closeness_of(2.5) == 2.5
    assert closeness_of(None) is None


def test_closeness_of_negative_zero_is_zero():
    assert closeness_of(-0.0) == 0.0


def test_closeness_of_accepts_an_int_spread_and_returns_a_float():
    result = closeness_of(-3)

    assert result == 3.0
    assert isinstance(result, float)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_closeness_of_treats_non_finite_spreads_as_unresolved(value):
    assert closeness_of(value) is None


def test_a_pick_em_ranks_first():
    candidates = [
        game("blowout", spread=-21.0, league="nfl", hours=1),
        game("pickem", spread=0.0, league="ncaaf", hours=48),
        game("close", spread=-1.0, league="nfl", hours=2),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 2, "ncaaf": 1}, total=3, now=NOW)

    assert keys(result)[0] == "pickem"
    assert result.selected[0].slate_rank == 1
    assert result.selected[0].closeness == 0.0


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf")])
def test_a_candidate_without_a_usable_spread_is_excluded(value):
    candidates = [
        game("bad", spread=value, league="nfl", hours=1),
        game("good", spread=-6.5, league="nfl", hours=2),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 2}, total=2, now=NOW)

    assert keys(result) == ["good"]
    assert result.shortfalls == {"nfl": 1}
    assert "Only 1 NFL game had a resolvable spread, 1 short of the target of 2." in result.notes


def test_a_non_finite_spread_cannot_reorder_the_slate():
    # A NaN compares false against everything, so a NaN left in the sort would
    # make the slate depend on the order of the input list.
    real = [
        game("a", spread=0.0, league="nfl", hours=1),
        game("b", spread=1.0, league="nfl", hours=2),
        game("c", spread=2.0, league="nfl", hours=3),
    ]
    nan_game = game("nan", spread=float("nan"), league="nfl", hours=1)
    expected = select_slate_by_targets(real, {"nfl": 2}, total=2, now=NOW)

    for position in range(len(real) + 1):
        polluted = real[:position] + [nan_game] + real[position:]
        assert select_slate_by_targets(polluted, {"nfl": 2}, total=2, now=NOW) == expected


def test_selected_closeness_is_always_a_float():
    result = select_slate_by_targets(
        [game("a", spread=-3, league="nfl")], {"nfl": 1}, total=1, now=NOW
    )

    assert isinstance(result.selected[0].closeness, float)


# Ties


def test_ties_break_by_kickoff_then_key():
    candidates = [
        game("zulu", spread=2.0, league="nfl", hours=3),
        game("alpha", spread=-2.0, league="nfl", hours=3),
        game("mike", spread=2.0, league="nfl", hours=3),
        game("early", spread=-2.0, league="nfl", hours=1),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 4}, total=4, now=NOW)

    assert keys(result) == ["early", "alpha", "mike", "zulu"]
    assert [item.closeness for item in result.selected] == [2.0, 2.0, 2.0, 2.0]


def test_the_key_tiebreak_decides_which_game_makes_a_full_slate():
    candidates = [
        game("zulu", spread=-1.0, league="nfl", hours=3),
        game("alpha", spread=1.0, league="nfl", hours=3),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 1}, total=1, now=NOW)

    assert keys(result) == ["alpha"]


def test_a_tie_across_leagues_does_not_disturb_the_targets():
    candidates = [
        game("cfb_tie", spread=3.0, league="ncaaf", hours=2),
        game("nfl_tie", spread=-3.0, league="nfl", hours=2),
        game("cfb_close", spread=0.5, league="ncaaf", hours=1),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 1, "ncaaf": 1}, total=2, now=NOW)

    assert keys(result) == ["cfb_close", "nfl_tie"]


# Started games and the clock


def test_exclude_started_true_drops_games_already_kicked_off():
    candidates = [
        game("started", spread=0.0, league="nfl", hours=-8),
        game("kicking_now", spread=0.0, league="nfl", hours=-5),  # exactly at now
        game("upcoming", spread=-9.0, league="nfl", hours=4),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 3}, total=3, now=NOW, exclude_started=True)

    assert keys(result) == ["upcoming"]


def test_exclude_started_false_keeps_games_already_kicked_off():
    candidates = [
        game("started", spread=0.0, league="nfl", hours=-8),
        game("kicking_now", spread=0.0, league="nfl", hours=-5),
        game("upcoming", spread=-9.0, league="nfl", hours=4),
    ]
    result = select_slate_by_targets(
        candidates, {"nfl": 3}, total=3, now=NOW, exclude_started=False
    )

    assert keys(result) == ["started", "kicking_now", "upcoming"]


def test_now_defaults_to_the_current_utc_time():
    real_now = datetime.now(UTC)
    candidates = [
        Candidate("past", "nfl", real_now - timedelta(hours=3), 0.0),
        Candidate("future", "nfl", real_now + timedelta(hours=3), 10.0),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 2}, total=2)

    assert keys(result) == ["future"]


def test_non_utc_aware_kickoffs_compare_correctly():
    eastern = timezone(timedelta(hours=-4))
    candidates = [
        Candidate("utc", "nfl", BASE + timedelta(hours=2), 1.0),
        Candidate("eastern", "nfl", (BASE + timedelta(hours=1)).astimezone(eastern), 1.0),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 2}, total=2, now=NOW)

    assert keys(result) == ["eastern", "utc"]


# Input shape


def test_a_generator_of_candidates_is_not_silently_dropped():
    # The awareness pass iterates the input, so an un-materialized generator
    # would otherwise come back empty and the cron would publish a blank slate.
    candidates = [
        game("a", spread=0.0, league="nfl", hours=1),
        game("b", spread=1.0, league="nfl", hours=2),
        game("c", spread=2.0, league="nfl", hours=3),
    ]
    result = select_slate_by_targets(
        (candidate for candidate in candidates), {"nfl": 2}, total=2, now=NOW
    )

    assert keys(result) == ["a", "b"]


def test_duplicate_keys_collapse_to_the_closest_entry():
    candidates = [
        game("dup", spread=9.0, league="nfl", hours=1),
        game("dup", spread=0.0, league="nfl", hours=1),
        game("other", spread=1.0, league="nfl", hours=2),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 5}, total=5, now=NOW)

    assert keys(result) == ["dup", "other"]
    assert result.selected[0].closeness == 0.0
    assert result.per_league == {"nfl": 2}


def test_selection_does_not_mutate_the_input():
    candidates = [
        game("b", spread=4.0, league="nfl", hours=2),
        game("a", spread=1.0, league="nfl", hours=1),
    ]
    before = list(candidates)
    select_slate_by_targets(candidates, {"nfl": 1}, total=1, now=NOW)

    assert candidates == before


def test_empty_candidates_return_an_empty_result():
    result = select_slate_by_targets([], {}, total=20, now=NOW)

    assert result.selected == []
    assert result.per_league == {}
    assert result.shortfalls == {}
    assert result.filled_from_other == 0
    assert result.notes == [
        "The slate came up 20 games short of 20 because only 0 games had a " "resolvable spread."
    ]


def test_empty_candidates_report_every_target_as_a_shortfall():
    result = select_slate_by_targets([], DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    assert result.selected == []
    assert result.per_league == {}
    assert result.shortfalls == {"nfl": 8, "ncaaf": 12}
    assert result.filled_from_other == 0
    assert result.notes[0] == (
        "Only 0 NFL games had a resolvable spread, 8 short of the target of 8."
    )


def test_never_returns_more_than_the_total():
    candidates = pool("nfl", "nfl", steps(30, 1.0)) + pool("cfb", "ncaaf", steps(30, 0.5))
    result = select_slate_by_targets(candidates, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    assert len(result.selected) == 20


# Errors


@pytest.mark.parametrize("total", [0, -1, -20])
def test_total_below_one_raises(total):
    with pytest.raises(ValueError, match="total"):
        select_slate_by_targets([game("a")], DEFAULT_TARGETS, total=total, now=NOW)


def test_total_below_one_raises_even_with_no_candidates():
    with pytest.raises(ValueError, match="total"):
        select_slate_by_targets([], {}, total=0, now=NOW)


@pytest.mark.parametrize("total", ["20", 20.0, None, True])
def test_a_non_integer_total_raises(total):
    with pytest.raises(ValueError, match="total"):
        select_slate_by_targets([game("a")], {}, total=total, now=NOW)


@pytest.mark.parametrize("targets", [{"nfl": -1}, {"nfl": 8, "ncaaf": -12}])
def test_a_negative_target_raises(targets):
    with pytest.raises(ValueError, match="targets"):
        select_slate_by_targets([game("a")], targets, total=20, now=NOW)


@pytest.mark.parametrize("targets", [{"nfl": 1.5}, {"nfl": "8"}, {"nfl": None}, {"nfl": True}])
def test_a_non_integer_target_raises(targets):
    with pytest.raises(ValueError, match="targets"):
        select_slate_by_targets([game("a")], targets, total=20, now=NOW)


def test_a_naive_kickoff_raises():
    naive = Candidate("naive", "nfl", datetime(2026, 9, 13, 17, 0), 0.0)
    with pytest.raises(ValueError, match="timezone aware"):
        select_slate_by_targets([naive], {"nfl": 1}, total=1, now=NOW)


def test_a_naive_kickoff_raises_even_when_started_games_are_kept():
    naive = Candidate("naive", "nfl", datetime(2026, 9, 13, 17, 0), 0.0)
    with pytest.raises(ValueError, match="timezone aware"):
        select_slate_by_targets([naive], {"nfl": 1}, total=1, now=NOW, exclude_started=False)


def test_a_naive_now_raises():
    with pytest.raises(ValueError, match="timezone aware"):
        select_slate_by_targets([game("a")], {"nfl": 1}, total=1, now=datetime(2026, 9, 13, 12, 0))


# Determinism


def shuffle_pool() -> list[Candidate]:
    """A pool with ties, a missing spread, and targets that overflow the total."""
    return [
        game("cfb1", spread=0.0, league="ncaaf", hours=1),
        game("cfb2", spread=-3.0, league="ncaaf", hours=1),
        game("cfb3", spread=3.0, league="ncaaf", hours=1),
        game("cfb4", spread=7.5, league="ncaaf", hours=2),
        game("nfl1", spread=-3.0, league="nfl", hours=1),
        game("nfl2", spread=6.0, league="nfl", hours=3),
        game("nfl3", spread=None, league="nfl", hours=3),
        game("nfl4", spread=-1.5, league="nfl", hours=4),
    ]


def test_shuffling_the_input_gives_an_identical_result():
    candidates = shuffle_pool()
    targets = {"nfl": 3, "ncaaf": 4}
    expected = select_slate_by_targets(candidates, targets, total=6, now=NOW)

    assert keys(expected) == ["cfb1", "nfl4", "cfb2", "cfb3", "nfl1", "nfl2"]

    rng = random.Random(20260913)
    for _ in range(50):
        shuffled = candidates[:]
        rng.shuffle(shuffled)
        result = select_slate_by_targets(shuffled, targets, total=6, now=NOW)

        assert result == expected
        # Byte identical, so the dict orders match too, not only their contents.
        assert repr(result) == repr(expected)


def test_shuffling_a_short_league_pool_gives_an_identical_result():
    candidates = pool("nfl", "nfl", steps(4, 2.0)) + pool("cfb", "ncaaf", steps(20, 0.5))
    expected = select_slate_by_targets(candidates, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    rng = random.Random(7)
    for _ in range(50):
        shuffled = candidates[:]
        rng.shuffle(shuffled)
        result = select_slate_by_targets(shuffled, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

        assert repr(result) == repr(expected)


def tie_heavy_pool() -> list[Candidate]:
    """Three leagues where almost everything ties on closeness and on kickoff.

    Only the key tiebreak separates most of these games, which is exactly the
    condition under which an unstable sort would show up.
    """
    games = [
        game(f"nfl{index:02d}", spread=3.0 if index % 2 else -3.0, hours=2) for index in range(1, 7)
    ]
    games += [
        game(f"cfb{index:02d}", spread=3.0 if index % 2 else -3.0, league="ncaaf", hours=2)
        for index in range(1, 9)
    ]
    games += [
        game(f"xfl{index:02d}", spread=1.0 if index % 2 else -1.0, league="xfl", hours=1)
        for index in range(1, 5)
    ]
    games.append(game("late", spread=3.0, league="ncaaf", hours=9))
    games.append(game("noline", spread=None, hours=2))
    return games


def test_shuffling_a_tie_heavy_three_league_pool_gives_an_identical_result():
    targets = {"nfl": 8, "ncaaf": 5}
    expected = select_slate_by_targets(tie_heavy_pool(), targets, total=12, now=NOW)

    # Pinned by hand: the four xfl games all sit at 1.0 and tie on kickoff, so
    # only xfl01 is taken, and every 3.0 game ties with every -3.0 game.
    assert keys(expected) == [
        "xfl01",
        "cfb01",
        "cfb02",
        "cfb03",
        "cfb04",
        "cfb05",
        "nfl01",
        "nfl02",
        "nfl03",
        "nfl04",
        "nfl05",
        "nfl06",
    ]
    assert expected.per_league == {"nfl": 6, "ncaaf": 5, "xfl": 1}
    assert expected.shortfalls == {"nfl": 2}
    # Targeted leagues in the order the settings named them, then the fill only
    # league. Neither alphabetical nor closeness order, so this pins the rule
    # that makes the whole result reproducible and not merely equal.
    assert list(expected.per_league) == ["nfl", "ncaaf", "xfl"]

    rng = random.Random(31337)
    for _ in range(100):
        shuffled = tie_heavy_pool()
        rng.shuffle(shuffled)
        result = select_slate_by_targets(shuffled, targets, total=12, now=NOW)

        assert repr(result) == repr(expected)


def test_random_pools_keep_the_ordering_invariants():
    # A wider net than the hand written cases: whatever the pool, the slate is
    # in global closeness order, never longer than the total, and within one
    # league it is always a prefix of that league's own closeness order. That
    # last one is what an inverted trim or an inverted fill would break.
    rng = random.Random(90210)
    spreads = [0.0, 0.5, 1.0, 1.0, 2.5, 3.0, 3.0, 7.5, 10.0, 14.0]
    leagues = ["nfl", "ncaaf", "xfl"]

    for _ in range(200):
        candidates = [
            game(
                f"g{index:02d}",
                spread=rng.choice(spreads),
                league=rng.choice(leagues),
                hours=float(rng.choice([1, 2, 2, 5, 20])),
            )
            for index in range(rng.randint(0, 20))
        ]
        targets = {league: rng.randint(0, 6) for league in leagues if rng.random() < 0.8}
        total = rng.randint(1, 16)
        result = select_slate_by_targets(candidates, targets, total=total, now=NOW)

        chosen = keys(result)
        by_key = {candidate.key: candidate for candidate in candidates}
        order = [
            candidate.key
            for candidate in sorted(
                candidates, key=lambda c: (abs(c.spread_home), c.kickoff, c.key)
            )
        ]

        assert len(chosen) <= total
        assert len(set(chosen)) == len(chosen)
        # In global order, and short only when the pool itself ran out.
        assert [order.index(key) for key in chosen] == sorted(order.index(key) for key in chosen)
        assert len(chosen) == min(total, len(candidates))
        for league in leagues:
            league_order = [key for key in order if by_key[key].league == league]
            taken = [key for key in league_order if key in set(chosen)]
            assert taken == league_order[: len(taken)]


def test_repeated_calls_return_equal_values():
    candidates = rich_pool()
    first = select_slate_by_targets(candidates, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)
    second = select_slate_by_targets(candidates, DEFAULT_TARGETS, total=DEFAULT_TOTAL, now=NOW)

    assert first == second


# compute_lock_at


def test_compute_lock_at_returns_the_earliest_kickoff():
    kickoffs = [
        BASE + timedelta(hours=6),
        BASE + timedelta(hours=1),
        BASE + timedelta(days=1),
    ]

    assert compute_lock_at(kickoffs) == BASE + timedelta(hours=1)


def test_compute_lock_at_returns_none_for_empty():
    assert compute_lock_at([]) is None


def test_compute_lock_at_with_one_kickoff():
    assert compute_lock_at([BASE]) == BASE


def test_compute_lock_at_across_timezones():
    eastern = timezone(timedelta(hours=-4))
    earliest = BASE + timedelta(hours=1)
    kickoffs = [
        BASE + timedelta(hours=3),
        earliest.astimezone(eastern),
    ]

    assert compute_lock_at(kickoffs) == earliest


def test_compute_lock_at_rejects_mixed_awareness():
    kickoffs = [BASE, datetime(2026, 9, 13, 12, 0)]
    with pytest.raises(ValueError):
        compute_lock_at(kickoffs)


def test_compute_lock_at_accepts_an_all_naive_sequence():
    # Lenient by contract: only a mix of naive and aware is rejected.
    kickoffs = [datetime(2026, 9, 13, 20, 0), datetime(2026, 9, 13, 17, 0)]

    assert compute_lock_at(kickoffs) == datetime(2026, 9, 13, 17, 0)


def test_compute_lock_at_rejects_a_non_datetime_with_a_useful_message():
    with pytest.raises(ValueError, match="datetimes"):
        compute_lock_at(["2026-09-13T17:00:00Z", BASE])


def test_compute_lock_at_accepts_a_tuple():
    assert compute_lock_at((BASE + timedelta(hours=2), BASE)) == BASE


def test_compute_lock_at_pairs_with_the_selection():
    candidates = [
        game("late_close", spread=0.0, league="nfl", hours=30),
        game("early_close", spread=1.0, league="nfl", hours=2),
        game("early_blowout", spread=28.0, league="nfl", hours=1),
    ]
    result = select_slate_by_targets(candidates, {"nfl": 2}, total=2, now=NOW)
    by_key = {candidate.key: candidate for candidate in candidates}
    lock_at = compute_lock_at([by_key[item.key].kickoff for item in result.selected])

    # The blowout is off the slate, so the lock follows the earliest slate game.
    assert lock_at == BASE + timedelta(hours=2)


# Public contract. Other modules are written against these names, so a rename
# is a breaking change and should fail here first.


def test_module_exports_the_agreed_surface():
    assert set(slate_module.__all__) == {
        "LEAGUES",
        "LEAGUE_LABELS",
        "Candidate",
        "Selected",
        "SlateResult",
        "closeness_of",
        "select_slate_by_targets",
        "compute_lock_at",
    }
    assert LEAGUES == ("nfl", "ncaaf")
    assert LEAGUE_LABELS == {"nfl": "NFL", "ncaaf": "college"}


def test_the_old_league_mix_surface_is_gone():
    for name in ("select_slate", "LEAGUE_MIX_KEYS", "_parse_league_mix", "_mix_value"):
        assert not hasattr(slate_module, name)
    assert "league_mix" not in inspect.getsource(slate_module)


def test_candidate_field_names_and_order():
    fields = [field.name for field in dataclasses.fields(Candidate)]

    assert fields == ["key", "league", "kickoff", "spread_home"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        game("a").key = "b"


def test_selected_field_names_and_order():
    fields = [field.name for field in dataclasses.fields(Selected)]

    assert fields == ["key", "slate_rank", "closeness"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Selected(key="a", slate_rank=1, closeness=0.0).slate_rank = 2


def test_slate_result_field_names_and_order():
    fields = [field.name for field in dataclasses.fields(SlateResult)]

    assert fields == [
        "selected",
        "per_league",
        "shortfalls",
        "filled_from_other",
        "notes",
    ]
    empty = SlateResult(selected=[], per_league={}, shortfalls={}, filled_from_other=0, notes=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        empty.filled_from_other = 1


def test_select_slate_by_targets_signature_and_defaults():
    parameters = inspect.signature(select_slate_by_targets).parameters

    assert list(parameters) == [
        "candidates",
        "targets",
        "total",
        "now",
        "exclude_started",
    ]
    assert parameters["targets"].default is inspect.Parameter.empty
    assert parameters["total"].default is inspect.Parameter.empty
    assert parameters["now"].default is None
    assert parameters["exclude_started"].default is True


def test_module_stays_pure():
    # No database, no network. Read the import statements, not the prose, so
    # the module docstring can still name what it deliberately does not import.
    tree = ast.parse(inspect.getsource(slate_module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    # collections.abc is where the typing ABCs moved to. Still pure stdlib, still no
    # database and no network, which is the only thing this test is guarding.
    assert imported <= {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "math",
        "typing",
    }


def test_no_em_dashes_or_emoji_in_the_module():
    # Spec 3h, enforced in the codebase and not only in the UI copy. Written
    # with escapes so this file does not contain the characters it forbids.
    forbidden = (chr(0x2014), chr(0x2013))
    with open(__file__, encoding="utf-8") as handle:
        sources = (inspect.getsource(slate_module), handle.read())
    for source in sources:
        for character in forbidden:
            assert character not in source
        assert all(ord(character) < 127 for character in source)
