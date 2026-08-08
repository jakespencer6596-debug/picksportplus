"""Unit tests for app.scoring (Spec Sections 9 and 17).

Pure logic, no database and no network. Game ids are 1..N and the confidence stack is
handed out top down: game 1 gets N points, game N gets 1 point, unless a test says otherwise.

Every score_pick/score_week/weekly_winner_ids call below passes mode explicitly. The
functions themselves default to mode="standard" (see app/scoring.py), but the pool level
default the app actually runs under is "inverse" (Pool.scoring_mode), so being explicit
here keeps every test honest about which rule it is proving, standard or inverse, rather
than leaning on a default that says nothing about the real behavior.
"""

from __future__ import annotations

import pytest

from app.scoring import (
    TEAM_SIDES,
    GameOutcome,
    PickInput,
    SeasonEntryInput,
    SeasonTotals,
    WeekResult,
    is_countable,
    score_pick,
    score_week,
    season_totals,
    validate_picks,
    weekly_winner_ids,
)

FULL_SLATE = 14


def final(game_id: int, winner: str) -> GameOutcome:
    return GameOutcome(game_id=game_id, status="final", winner=winner)


def tie(game_id: int) -> GameOutcome:
    return GameOutcome(game_id=game_id, status="final", winner="tie")


def void(game_id: int) -> GameOutcome:
    return GameOutcome(game_id=game_id, status="void", winner=None)


def scheduled(game_id: int) -> GameOutcome:
    return GameOutcome(game_id=game_id, status="scheduled", winner=None)


def in_progress(game_id: int) -> GameOutcome:
    return GameOutcome(game_id=game_id, status="in_progress", winner=None)


def slate_outcomes(n: int = FULL_SLATE, winner: str = "home") -> list[GameOutcome]:
    """A slate of n final games, all won by the given side."""
    return [final(i, winner) for i in range(1, n + 1)]


def confidence_stack(n: int = FULL_SLATE) -> list[int]:
    """Game 1 is the most confident pick at n points, game n is the least at 1 point."""
    return list(range(n, 0, -1))


def picks_all(team: str, n: int = FULL_SLATE) -> list[PickInput]:
    """One pick per game, all on the same side, confidence n down to 1."""
    return [
        PickInput(game_id=i, picked_team=team, confidence=c)
        for i, c in zip(range(1, n + 1), confidence_stack(n), strict=False)
    ]


def max_penalty(n: int = FULL_SLATE) -> int:
    """The maximum possible inverse penalty for a picks_required of n: sum(1..n)."""
    return sum(range(1, n + 1))


# Public contract


def test_team_sides_is_the_exported_pair():
    assert TEAM_SIDES == ("home", "away")


def test_dataclass_field_order_is_stable_for_positional_callers():
    # Services construct these positionally, so the order is part of the contract.
    assert GameOutcome(1, "final", "home") == GameOutcome(game_id=1, status="final", winner="home")
    assert PickInput(1, "home", 14) == PickInput(game_id=1, picked_team="home", confidence=14)
    assert WeekResult(88, 11, 14) == WeekResult(points=88, correct=11, possible=14)
    assert WeekResult(88, 11, 14, False) == WeekResult(points=88, correct=11, possible=14)
    assert WeekResult(120, 0, 14, True) == WeekResult(
        points=120, correct=0, possible=14, did_not_submit=True
    )
    assert SeasonEntryInput(88, 11, 14, True) == SeasonEntryInput(
        points=88, correct=11, possible=14, is_winner=True
    )
    assert SeasonTotals(88, 11, 14, 1, 1) == SeasonTotals(
        points=88, correct=11, possible=14, weeks_played=1, weekly_wins=1
    )


def test_week_result_did_not_submit_defaults_false():
    # Every hand built WeekResult in this file that does not mention did_not_submit is
    # relying on this default, so it is pinned here as its own fact.
    assert WeekResult(points=10, correct=1, possible=1).did_not_submit is False


# Countability


def test_is_countable_final_home_or_away():
    assert is_countable(final(1, "home")) is True
    assert is_countable(final(2, "away")) is True


def test_is_countable_rejects_tie_void_and_unfinished():
    assert is_countable(tie(1)) is False
    assert is_countable(void(2)) is False
    assert is_countable(scheduled(3)) is False
    assert is_countable(in_progress(4)) is False
    assert is_countable(GameOutcome(game_id=5, status="final", winner=None)) is False


# Single pick scoring, standard mode: correct earns confidence, wrong earns nothing


def test_score_pick_standard_correct_earns_confidence():
    assert score_pick(PickInput(1, "home", 12), final(1, "home"), mode="standard") == 12


def test_score_pick_standard_wrong_earns_zero():
    assert score_pick(PickInput(1, "away", 12), final(1, "home"), mode="standard") == 0


def test_score_pick_standard_tie_and_void_earn_zero_even_when_side_matches():
    assert score_pick(PickInput(1, "home", 14), tie(1), mode="standard") == 0
    assert score_pick(PickInput(1, "home", 14), void(1), mode="standard") == 0


def test_score_pick_standard_unfinished_game_earns_zero():
    assert score_pick(PickInput(1, "home", 9), scheduled(1), mode="standard") == 0
    assert score_pick(PickInput(1, "home", 9), in_progress(1), mode="standard") == 0


# Single pick scoring, inverse mode: wrong earns confidence AGAINST the player, correct
# earns nothing. A non countable outcome is still 0 in either mode.


def test_score_pick_inverse_wrong_earns_confidence_against_the_player():
    assert score_pick(PickInput(1, "away", 12), final(1, "home"), mode="inverse") == 12


def test_score_pick_inverse_correct_earns_zero():
    assert score_pick(PickInput(1, "home", 12), final(1, "home"), mode="inverse") == 0


def test_score_pick_inverse_tie_and_void_earn_zero_even_when_side_would_have_lost():
    # Under inverse a losing pick would normally count against the player, but a tie or a
    # void game is not countable at all, so nobody is charged for it either.
    assert score_pick(PickInput(1, "away", 14), tie(1), mode="inverse") == 0
    assert score_pick(PickInput(1, "away", 14), void(1), mode="inverse") == 0


def test_score_pick_inverse_unfinished_game_earns_zero():
    assert score_pick(PickInput(1, "away", 9), scheduled(1), mode="inverse") == 0
    assert score_pick(PickInput(1, "away", 9), in_progress(1), mode="inverse") == 0


# Week scoring, standard mode: every pre-Phase-2 behavior, unchanged, with mode named
# explicitly since the default the app runs under has moved to "inverse".


def test_standard_all_correct_full_slate():
    result = score_week(picks_all("home"), slate_outcomes(), mode="standard")
    assert result == WeekResult(points=105, correct=14, possible=14)
    assert result.points == sum(range(1, 15))
    assert result.did_not_submit is False


def test_standard_all_wrong_full_slate():
    result = score_week(picks_all("away"), slate_outcomes(winner="home"), mode="standard")
    assert result == WeekResult(points=0, correct=0, possible=14)


def test_standard_mixed_week_hand_computed():
    # Six game slate. Confidence runs 6, 5, 4, 3, 2, 1 across games 1 to 6.
    # Game 1: home wins, picked home. Correct, 6 points.
    # Game 2: away wins, picked home. Wrong, 0.
    # Game 3: away wins, picked away. Correct, 4 points.
    # Game 4: tie. Not countable, 0 points, not in correct or possible.
    # Game 5: home wins, picked home. Correct, 2 points.
    # Game 6: home wins, picked away. Wrong, 0.
    # Expected: points 6 + 4 + 2 = 12, correct 3, possible 5.
    picks = [
        PickInput(1, "home", 6),
        PickInput(2, "home", 5),
        PickInput(3, "away", 4),
        PickInput(4, "home", 3),
        PickInput(5, "home", 2),
        PickInput(6, "away", 1),
    ]
    outcomes = [
        final(1, "home"),
        final(2, "away"),
        final(3, "away"),
        tie(4),
        final(5, "home"),
        final(6, "home"),
    ]
    assert score_week(picks, outcomes, mode="standard") == WeekResult(
        points=12, correct=3, possible=5
    )


def test_standard_unsubmitted_player_scores_zero_and_possible_is_zero():
    # Phase 3: a no-show submitted no picks, so there is nothing of theirs to count, even
    # though the slate itself has 14 countable games for players who did pick.
    result = score_week([], slate_outcomes(), mode="standard")
    assert result == WeekResult(points=0, correct=0, possible=0, did_not_submit=True)


def test_standard_tie_game_is_excluded_from_correct_and_possible():
    outcomes = slate_outcomes()
    outcomes[3] = tie(4)  # game 4 ends level
    result = score_week(picks_all("home"), outcomes, mode="standard")
    # Game 4 staked 11 points (14 down to 1), so the winning total drops by 11.
    assert result == WeekResult(points=94, correct=13, possible=13)


def test_standard_voided_game_is_treated_like_a_tie():
    outcomes = slate_outcomes()
    outcomes[0] = void(1)  # commissioner voided the top game
    result = score_week(picks_all("home"), outcomes, mode="standard")
    assert result == WeekResult(points=91, correct=13, possible=13)


def test_standard_scheduled_and_in_progress_games_are_not_countable_yet():
    outcomes = slate_outcomes()
    outcomes[0] = scheduled(1)  # staked 14
    outcomes[1] = in_progress(2)  # staked 13
    result = score_week(picks_all("home"), outcomes, mode="standard")
    assert result == WeekResult(points=105 - 14 - 13, correct=12, possible=12)


def test_standard_partial_week_possible_counts_only_final_games():
    # Only the first three games are final. Nothing else has kicked off.
    outcomes = [final(1, "home"), final(2, "away"), final(3, "home")]
    outcomes += [scheduled(i) for i in range(4, 15)]
    result = score_week(picks_all("home"), outcomes, mode="standard")
    assert result == WeekResult(points=14 + 12, correct=2, possible=3)


def test_standard_short_slate_of_ten_games():
    result = score_week(picks_all("home", 10), slate_outcomes(10), mode="standard")
    assert result == WeekResult(points=55, correct=10, possible=10)


def test_standard_short_slate_mixed_result():
    outcomes = slate_outcomes(10)
    outcomes[2] = final(3, "away")  # game 3 staked 8
    outcomes[9] = final(10, "away")  # game 10 staked 1
    result = score_week(picks_all("home", 10), outcomes, mode="standard")
    assert result == WeekResult(points=55 - 8 - 1, correct=8, possible=10)


def test_standard_pick_for_a_game_that_left_the_slate_is_ignored():
    picks = picks_all("home", 3) + [PickInput(99, "home", 50)]
    result = score_week(picks, slate_outcomes(3), mode="standard")
    assert result == WeekResult(points=6, correct=3, possible=3)


def test_standard_possible_excludes_slate_games_the_player_skipped():
    # Phase 3: possible is scoped to the player's own submitted picks, not the whole slate.
    # This player only picked game 1 of a 3 game slate, so possible is 1, not 3, even though
    # games 2 and 3 are countable for whoever did pick them.
    picks = [PickInput(1, "home", 3)]
    result = score_week(picks, slate_outcomes(3), mode="standard")
    assert result == WeekResult(points=3, correct=1, possible=1)


def test_standard_empty_slate_scores_nothing():
    # No games at all is not the same shape as "no picks submitted for a real slate": the
    # slate itself is empty, so there is nothing to have been a no-show for.
    assert score_week([], [], mode="standard") == WeekResult(
        points=0, correct=0, possible=0, did_not_submit=True
    )


def test_standard_correct_pick_staked_at_zero_still_counts_as_correct():
    # A stored confidence of 0 is invalid input, but it must not make a right pick
    # disappear from the correct count. Points and correct are separate facts.
    outcomes = [final(1, "home"), final(2, "home")]
    picks = [PickInput(1, "home", 0), PickInput(2, "home", 1)]
    result = score_week(picks, outcomes, mode="standard")
    assert result == WeekResult(points=1, correct=2, possible=2)


def test_standard_wrong_pick_staked_at_zero_is_not_correct():
    outcomes = [final(1, "home")]
    result = score_week([PickInput(1, "away", 0)], outcomes, mode="standard")
    assert result == WeekResult(points=0, correct=0, possible=1)


def test_standard_duplicate_outcome_rows_do_not_inflate_possible():
    # score_week keys outcomes by game_id, so a repeated row is still one game.
    outcomes = [final(1, "home"), final(1, "home"), final(2, "away")]
    picks = [PickInput(1, "home", 2), PickInput(2, "away", 1)]
    result = score_week(picks, outcomes, mode="standard")
    assert result == WeekResult(points=3, correct=2, possible=2)


# Week scoring, inverse mode: wrong picks contribute points, correct picks do not.
# "correct" counts right picks exactly the same way as standard, in every case below.


def test_inverse_all_correct_full_slate_scores_zero_but_correct_is_full():
    result = score_week(picks_all("home"), slate_outcomes(), mode="inverse")
    assert result == WeekResult(points=0, correct=14, possible=14)
    assert result.correct == result.possible


def test_inverse_all_wrong_full_slate_equals_the_maximum_penalty():
    result = score_week(picks_all("away"), slate_outcomes(winner="home"), mode="inverse")
    assert result == WeekResult(points=max_penalty(), correct=0, possible=14)
    assert result.points == sum(range(1, 15))


def test_inverse_correct_and_points_move_in_opposite_directions():
    # The brief's exact contract: correct and points are independent facts under inverse.
    all_correct = score_week(picks_all("home"), slate_outcomes(), mode="inverse")
    all_wrong = score_week(picks_all("away"), slate_outcomes(winner="home"), mode="inverse")
    assert all_correct.correct == all_correct.possible
    assert all_correct.points == 0
    assert all_wrong.correct == 0
    assert all_wrong.points == max_penalty()


def test_inverse_mixed_week_only_wrong_picks_contribute_points():
    # Same six game slate as the standard mixed test. This time the wrong picks (games 2
    # and 6, staked 5 and 1) are what count, and the right ones (games 1, 3, 5) do not.
    picks = [
        PickInput(1, "home", 6),
        PickInput(2, "home", 5),
        PickInput(3, "away", 4),
        PickInput(4, "home", 3),
        PickInput(5, "home", 2),
        PickInput(6, "away", 1),
    ]
    outcomes = [
        final(1, "home"),
        final(2, "away"),
        final(3, "away"),
        tie(4),
        final(5, "home"),
        final(6, "home"),
    ]
    result = score_week(picks, outcomes, mode="inverse")
    assert result == WeekResult(points=5 + 1, correct=3, possible=5)


def test_inverse_void_and_tie_score_zero_and_are_excluded_from_possible():
    outcomes = slate_outcomes()
    outcomes[0] = void(1)  # staked 14
    outcomes[3] = tie(4)  # staked 11
    result = score_week(picks_all("home"), outcomes, mode="inverse")
    # Every remaining pick is correct, so inverse points are still 0.
    assert result == WeekResult(points=0, correct=12, possible=12)


def test_inverse_short_slate_of_ten_games_all_correct_scores_zero():
    result = score_week(picks_all("home", 10), slate_outcomes(10), mode="inverse")
    assert result == WeekResult(points=0, correct=10, possible=10)


def test_inverse_short_slate_mixed_result():
    outcomes = slate_outcomes(10)
    outcomes[2] = final(3, "away")  # game 3 staked 8, now wrong
    outcomes[9] = final(10, "away")  # game 10 staked 1, now wrong
    result = score_week(picks_all("home", 10), outcomes, mode="inverse")
    assert result == WeekResult(points=8 + 1, correct=8, possible=10)


def test_inverse_correct_pick_staked_at_zero_still_counts_as_correct():
    outcomes = [final(1, "home"), final(2, "home")]
    picks = [PickInput(1, "home", 0), PickInput(2, "away", 1)]
    result = score_week(picks, outcomes, mode="inverse")
    # Game 1 correct (0 staked anyway), game 2 wrong, 1 point against.
    assert result == WeekResult(points=1, correct=1, possible=2)


# No-shows: did_not_submit and the maximum penalty


def test_standard_no_show_scores_zero_and_is_flagged():
    result = score_week([], slate_outcomes(), mode="standard")
    assert result.did_not_submit is True
    assert result.points == 0
    assert result.correct == 0
    assert result.possible == 0


def test_inverse_no_show_takes_the_maximum_penalty_and_is_flagged():
    # picks_required defaults to len(outcomes), 14 games here, so the penalty is
    # sum(1..14) = 105, the same total an all-correct week would have paid out under
    # standard. Nobody submitting is the worst outcome in the pool under inverse.
    result = score_week([], slate_outcomes(), mode="inverse")
    assert result.did_not_submit is True
    assert result.points == max_penalty(14) == 105
    assert result.correct == 0
    assert result.possible == 0


def test_inverse_no_show_penalty_uses_explicit_picks_required_not_slate_size():
    # A real pool today has num_games_per_week=20 but, per the brief's own confirmed
    # defaults (DECISIONS.md), a picks_required of 15 (120 max penalty). picks_required is
    # never inferred as the slate size when the caller passes it explicitly, only when it
    # is omitted. possible is 0 regardless of slate size: a no-show submitted nothing.
    outcomes = slate_outcomes(20)
    result = score_week([], outcomes, mode="inverse", picks_required=15)
    assert result.points == sum(range(1, 16)) == 120
    assert result.possible == 0


def test_no_show_possible_is_zero_not_the_slate_size():
    # The Phase 3 refinement, named directly: possible is scoped to a player's own picks,
    # so a no-show (zero submitted picks) has possible=0 in both modes, even against a
    # large countable slate. Before this phase a no-show's possible came from the whole
    # slate; see DECISIONS.md, Phase 3, for why this changed.
    outcomes = slate_outcomes(20)
    assert score_week([], outcomes, mode="standard").possible == 0
    assert score_week([], outcomes, mode="inverse", picks_required=15).possible == 0


def test_inverse_no_show_default_picks_required_matches_the_slate_size():
    outcomes = slate_outcomes(9)
    result = score_week([], outcomes, mode="inverse")
    assert result.points == sum(range(1, 10)) == 45


def test_empty_slate_no_picks_is_still_a_no_show_in_both_modes():
    # An empty slate (nothing built yet) still marks did_not_submit; possible is just 0.
    assert score_week([], [], mode="inverse") == WeekResult(
        points=0, correct=0, possible=0, did_not_submit=True
    )
    assert score_week([], [], mode="standard") == WeekResult(
        points=0, correct=0, possible=0, did_not_submit=True
    )


def test_inverse_perfect_week_beats_a_no_shows_max_penalty():
    """The exact trap the brief calls out: a no-show must never come out ahead of a player
    who played a perfect week. A no-show pays the maximum penalty (120 at picks_required
    15) and a perfect week pays 0, so the perfect week is the sole weekly winner.
    """
    perfect_user_id = 1
    no_show_user_id = 2

    perfect = score_week(
        picks_all("home", 15),
        slate_outcomes(15),
        mode="inverse",
        picks_required=15,
    )
    no_show = score_week([], slate_outcomes(15), mode="inverse", picks_required=15)

    assert perfect.points == 0
    assert no_show.points == 120
    assert no_show.did_not_submit is True

    results = {perfect_user_id: perfect, no_show_user_id: no_show}
    assert weekly_winner_ids(results, mode="inverse") == {perfect_user_id}


# Phase 3: possible is scoped to the player's own submitted picks, not the whole slate.
# This is what lets one player's slate cover a different subset of the 20 game slate than
# another player's 15 picks without either one affecting the other's possible count.


def test_possible_only_counts_the_players_own_picks_on_a_bigger_slate():
    # 20 game slate, this player only picked 15 of them (games 1..15). Only their own 15
    # picks can ever show up in their possible, no matter how many games are on the slate.
    picks = picks_all("home", 15)
    result = score_week(picks, slate_outcomes(20), mode="standard", picks_required=15)
    assert result.possible == 15


def test_a_voided_pick_scores_zero_and_only_reduces_that_players_own_possible():
    # The player submitted a full 15 picks. One of their picked games is voided by the
    # commissioner after the fact; the rest of the slate (including their other 14 picks)
    # goes final. Their possible drops to 14, the void pick itself earns nothing and is not
    # counted as correct, and every other pick's points/correct are unaffected.
    picks = picks_all("home", 15)
    outcomes = slate_outcomes(20)
    outcomes[0] = void(1)  # the game this player staked their top pick (15) on
    result = score_week(picks, outcomes, mode="standard", picks_required=15)
    # Games 2..15 are all correct (home won, picked home), staked 14 down to 1.
    assert result.possible == 14
    assert result.correct == 14
    assert result.points == sum(range(1, 15))


def test_a_voided_pick_under_inverse_also_only_reduces_that_players_own_possible():
    picks = picks_all("away", 15)  # every pick wrong except the voided one
    outcomes = slate_outcomes(20, winner="home")
    outcomes[0] = void(1)
    result = score_week(picks, outcomes, mode="inverse", picks_required=15)
    assert result.possible == 14
    assert result.correct == 0
    # Games 2..15 are all wrong under inverse, staked 14 down to 1, all counted against.
    assert result.points == sum(range(1, 15))


# Validation (unchanged by Phase 2, mode has no bearing on what a valid submission looks
# like)


def test_validate_picks_valid_submission_returns_no_errors():
    slate = list(range(1, 15))
    assert validate_picks(picks_all("home", 14), slate) == []


def test_validate_picks_accepts_any_confidence_order():
    slate = [1, 2, 3]
    picks = [
        PickInput(1, "away", 2),
        PickInput(2, "home", 3),
        PickInput(3, "away", 1),
    ]
    assert validate_picks(picks, slate) == []


def test_validate_picks_extra_game_not_on_slate():
    slate = [1, 2, 3, 4, 5]
    picks = picks_all("home", 5) + [PickInput(99, "home", 5)]
    # picks_required is set to the submitted count so this stays a test of the off slate
    # check alone; the Phase 3 count check gets its own tests below.
    assert validate_picks(picks, slate, picks_required=6) == [
        "Game 99 is not on this week's slate."
    ]


def test_validate_picks_same_game_picked_twice():
    slate = [1, 2, 3]
    picks = [
        PickInput(1, "home", 3),
        PickInput(1, "away", 2),
        PickInput(2, "home", 1),
    ]
    # Phase 3 removed the old per-game "missing a winner" check (an unpicked slate game is
    # legal now), so a duplicate no longer implies a second, separate "missing" message,
    # just the duplicate itself.
    assert validate_picks(picks, slate) == ["Game 1 has more than one pick."]


def test_validate_picks_duplicate_confidence_value():
    slate = [1, 2, 3, 4]
    picks = [
        PickInput(1, "home", 4),
        PickInput(2, "away", 3),
        PickInput(3, "home", 3),
        PickInput(4, "away", 1),
    ]
    errors = validate_picks(picks, slate)
    assert "Confidence value 3 is used twice." in errors
    assert "Confidence value 2 is not used." in errors
    assert len(errors) == 2


def test_validate_picks_confidence_used_three_times():
    slate = [1, 2, 3, 4]
    picks = [
        PickInput(1, "home", 2),
        PickInput(2, "away", 2),
        PickInput(3, "home", 2),
        PickInput(4, "away", 1),
    ]
    errors = validate_picks(picks, slate)
    assert "Confidence value 2 is used three times." in errors


def test_validate_picks_confidence_zero_is_out_of_range():
    slate = [1, 2, 3]
    picks = [
        PickInput(1, "home", 0),
        PickInput(2, "away", 2),
        PickInput(3, "home", 3),
    ]
    errors = validate_picks(picks, slate)
    assert "Confidence value 0 is outside the range 1 to 3." in errors
    assert "Confidence value 1 is not used." in errors


def test_validate_picks_confidence_above_n_is_out_of_range():
    slate = [1, 2, 3]
    picks = [
        PickInput(1, "home", 4),
        PickInput(2, "away", 2),
        PickInput(3, "home", 1),
    ]
    errors = validate_picks(picks, slate)
    assert "Confidence value 4 is outside the range 1 to 3." in errors
    assert "Confidence value 3 is not used." in errors


def test_validate_picks_bad_picked_team_value():
    slate = [1, 2, 3]
    picks = [
        PickInput(1, "tie", 3),
        PickInput(2, "away", 2),
        PickInput(3, "home", 1),
    ]
    errors = validate_picks(picks, slate)
    assert 'Game 1 has an invalid pick of "tie". Pick home or away.' in errors
    assert len(errors) == 1


def test_validate_picks_empty_picked_team_value():
    slate = [1, 2]
    picks = [PickInput(1, "", 2), PickInput(2, "home", 1)]
    errors = validate_picks(picks, slate)
    assert 'Game 1 has an invalid pick of "". Pick home or away.' in errors


def test_validate_picks_repeated_slate_id_counts_as_one_game():
    # A repeated id in the slate list is the same game, so N is 3 and 4 is out of range.
    slate = [1, 2, 2, 3]
    picks = [
        PickInput(1, "home", 4),
        PickInput(2, "home", 3),
        PickInput(3, "home", 2),
    ]
    errors = validate_picks(picks, slate)
    assert "Confidence value 4 is outside the range 1 to 3." in errors


def test_validate_picks_repeated_slate_id_still_accepts_a_clean_submission():
    slate = [1, 2, 2, 3]
    picks = [
        PickInput(1, "home", 3),
        PickInput(2, "home", 2),
        PickInput(3, "home", 1),
    ]
    assert validate_picks(picks, slate) == []


def test_validate_picks_off_slate_confidence_is_not_range_checked():
    # The off slate game gets one clear error. Its confidence is not the player's problem.
    slate = [1, 2, 3]
    picks = [
        PickInput(1, "home", 3),
        PickInput(2, "home", 2),
        PickInput(3, "home", 1),
        PickInput(9, "home", 99),
    ]
    assert validate_picks(picks, slate, picks_required=4) == ["Game 9 is not on this week's slate."]


def test_validate_picks_negative_confidence_is_out_of_range():
    slate = [1, 2]
    picks = [PickInput(1, "home", -1), PickInput(2, "home", 2)]
    errors = validate_picks(picks, slate)
    assert "Confidence value -1 is outside the range 1 to 2." in errors
    assert "Confidence value 1 is not used." in errors


def test_validate_picks_completely_empty_submission():
    assert validate_picks([], list(range(1, 15))) == ["No picks were submitted."]


def test_validate_picks_empty_slate_and_empty_submission_is_valid():
    assert validate_picks([], []) == []


def test_validate_picks_reports_several_problems_at_once():
    slate = [1, 2, 3, 4]
    picks = [
        PickInput(1, "left", 4),
        PickInput(2, "home", 4),
        PickInput(3, "away", 1),
        PickInput(4, "home", 2),
        PickInput(77, "home", 3),
    ]
    errors = validate_picks(picks, slate)
    assert "Game 77 is not on this week's slate." in errors
    assert 'Game 1 has an invalid pick of "left". Pick home or away.' in errors
    assert "Confidence value 4 is used twice." in errors
    assert "Confidence value 3 is not used." in errors


@pytest.mark.parametrize("size", [10, 12, 14, 16])
def test_validate_picks_valid_across_configurable_slate_sizes(size):
    assert validate_picks(picks_all("home", size), list(range(1, size + 1))) == []


def test_validate_error_strings_follow_the_copy_rules():
    slate = [1, 2, 3, 4]
    picks = [
        PickInput(1, "left", 0),
        PickInput(2, "home", 0),
        PickInput(9, "away", 4),
    ]
    # The long dashes are written as escapes so this file itself stays clean of them.
    em_dash = chr(0x2014)
    en_dash = chr(0x2013)
    for message in validate_picks(picks, slate):
        assert em_dash not in message
        assert en_dash not in message
        assert message.endswith(".")
        assert message == message.strip()


# Validation, Phase 3: picks_required can be smaller than the slate, the pool's real rule
# is 15 picks out of a 20 game slate. Every call below passes picks_required explicitly,
# since that is what real call sites (app/routers/picks.py) always do.


def test_validate_picks_valid_submission_of_15_of_20():
    slate = list(range(1, 21))
    assert validate_picks(picks_all("home", 15), slate, picks_required=15) == []


def test_validate_picks_one_pick_short_of_required():
    slate = list(range(1, 21))
    picks = picks_all("home", 14)
    assert validate_picks(picks, slate, picks_required=15) == ["You have picked 14 games. Pick 15."]


def test_validate_picks_one_pick_over_required():
    slate = list(range(1, 21))
    picks = picks_all("home", 16)
    errors = validate_picks(picks, slate, picks_required=15)
    assert "You have picked 16 games. Pick 15." in errors


def test_validate_picks_duplicated_game_still_errors_with_picks_required():
    slate = list(range(1, 21))
    # 14 distinct games plus a second pick on game 1: 15 picks total, matching
    # picks_required, so the only remaining problem is the duplicate itself.
    picks = picks_all("home", 14) + [PickInput(1, "away", 15)]
    assert validate_picks(picks, slate, picks_required=15) == ["Game 1 has more than one pick."]


def test_validate_picks_off_slate_game_still_errors_with_picks_required():
    slate = list(range(1, 21))
    # 14 on slate picks plus one off slate pick: 15 total, matching picks_required, so the
    # only remaining problem is the off slate game.
    picks = picks_all("home", 14) + [PickInput(99, "home", 15)]
    assert validate_picks(picks, slate, picks_required=15) == [
        "Game 99 is not on this week's slate."
    ]


def test_validate_picks_duplicate_confidence_still_errors_with_picks_required():
    slate = [1, 2, 3, 4, 5]
    picks = [
        PickInput(1, "home", 3),
        PickInput(2, "home", 3),
        PickInput(3, "home", 2),
    ]
    errors = validate_picks(picks, slate, picks_required=3)
    assert "Confidence value 3 is used twice." in errors


def test_validate_picks_confidence_16_is_out_of_range_when_15_are_required():
    slate = list(range(1, 21))
    picks = list(picks_all("home", 15))
    picks[0] = PickInput(picks[0].game_id, picks[0].picked_team, 16)
    errors = validate_picks(picks, slate, picks_required=15)
    assert "Confidence value 16 is outside the range 1 to 15." in errors


def test_validate_picks_20_of_20_still_validates_cleanly():
    # Proves picks_required is a real, honored config value: a pool that wants the whole
    # slate just sets picks_required equal to num_games_per_week, nothing is hard coded.
    slate = list(range(1, 21))
    assert validate_picks(picks_all("home", 20), slate, picks_required=20) == []


# Weekly winners, standard mode: highest points wins, unchanged from before Phase 2


def test_weekly_winner_ids_standard_single_winner():
    results = {
        1: WeekResult(88, 11, 14),
        2: WeekResult(74, 9, 14),
        3: WeekResult(51, 7, 14),
    }
    assert weekly_winner_ids(results, mode="standard") == {1}


def test_weekly_winner_ids_standard_two_way_tie_shares_the_win():
    results = {
        1: WeekResult(88, 11, 14),
        2: WeekResult(88, 10, 14),
        3: WeekResult(51, 7, 14),
    }
    assert weekly_winner_ids(results, mode="standard") == {1, 2}


def test_weekly_winner_ids_empty_mapping_has_no_winner_in_either_mode():
    assert weekly_winner_ids({}, mode="standard") == set()
    assert weekly_winner_ids({}, mode="inverse") == set()


def test_weekly_winner_ids_standard_all_zero_week_has_no_winner():
    results = {
        1: WeekResult(0, 0, 14),
        2: WeekResult(0, 0, 14),
    }
    assert weekly_winner_ids(results, mode="standard") == set()


def test_weekly_winner_ids_standard_ignores_correct_count_as_a_tiebreak():
    results = {
        1: WeekResult(60, 5, 14),
        2: WeekResult(60, 12, 14),
    }
    assert weekly_winner_ids(results, mode="standard") == {1, 2}


def test_weekly_winner_ids_standard_excludes_a_no_show_even_at_zero():
    # A no-show already scores 0 under standard, same as before this flag existed. This
    # pins that a no-show still cannot win, now via the explicit flag rather than by
    # accident of the 0-point value.
    results = {
        1: WeekResult(0, 0, 14, did_not_submit=True),
        2: WeekResult(0, 0, 14),
    }
    # Player 2 actually played a scoreless week (0 correct out of 14); nobody submitted a
    # positive score, so nobody wins, exactly like the all-zero-week case above.
    assert weekly_winner_ids(results, mode="standard") == set()


# Weekly winners, inverse mode: lowest points wins, no-shows are always excluded, 0 is a
# valid and winnable score (no <= 0 guard).


def test_weekly_winner_ids_inverse_lowest_wins():
    results = {
        1: WeekResult(12, 10, 14),
        2: WeekResult(40, 6, 14),
        3: WeekResult(75, 2, 14),
    }
    assert weekly_winner_ids(results, mode="inverse") == {1}


def test_weekly_winner_ids_inverse_two_way_tie_shares_the_win():
    results = {
        1: WeekResult(12, 10, 14),
        2: WeekResult(12, 4, 14),
        3: WeekResult(75, 2, 14),
    }
    assert weekly_winner_ids(results, mode="inverse") == {1, 2}


def test_weekly_winner_ids_inverse_zero_is_a_valid_winning_score():
    # Standard has a "best <= 0 means nobody wins" guard. Inverse must not apply it: a
    # perfect week is 0 points and must be able to win outright.
    results = {
        1: WeekResult(0, 14, 14),
        2: WeekResult(30, 9, 14),
    }
    assert weekly_winner_ids(results, mode="inverse") == {1}


def test_weekly_winner_ids_inverse_excludes_no_shows_from_the_eligible_pool():
    results = {
        1: WeekResult(20, 8, 14),
        2: WeekResult(105, 0, 14, did_not_submit=True),
        3: WeekResult(35, 6, 14),
    }
    # Player 2's 105 is not just excluded from winning, it must not even be visible to the
    # min() comparison: if it leaked in it would still lose to 20 anyway, so this also
    # covers the case with a mix of eligible players around it.
    assert weekly_winner_ids(results, mode="inverse") == {1}


def test_weekly_winner_ids_inverse_all_no_show_returns_empty_set():
    results = {
        1: WeekResult(105, 0, 14, did_not_submit=True),
        2: WeekResult(105, 0, 14, did_not_submit=True),
    }
    assert weekly_winner_ids(results, mode="inverse") == set()


# Season aggregation. season_totals is a plain sum and does not change between modes: the
# "lower is better" of inverse is a presentation concern handled at read time by
# app/services/standings.py sort direction, not something season_totals needs to know
# about. is_winner already reflects the correct mode-aware result from weekly_winner_ids
# by the time it reaches here, so summing it is mode agnostic by construction.


def test_season_totals_aggregates_points_correct_possible_and_wins():
    entries = [
        SeasonEntryInput(points=88, correct=11, possible=14, is_winner=True),
        SeasonEntryInput(points=61, correct=9, possible=14, is_winner=False),
        SeasonEntryInput(points=73, correct=10, possible=13, is_winner=True),
        SeasonEntryInput(points=0, correct=0, possible=14, is_winner=False),
    ]
    assert season_totals(entries) == SeasonTotals(
        points=222,
        correct=30,
        possible=55,
        weeks_played=4,
        weekly_wins=2,
    )


def test_season_totals_no_entries():
    assert season_totals([]) == SeasonTotals(
        points=0, correct=0, possible=0, weeks_played=0, weekly_wins=0
    )


def test_season_totals_accepts_any_iterable():
    entries = (
        SeasonEntryInput(points=10, correct=2, possible=14, is_winner=False) for _ in range(3)
    )
    assert season_totals(entries) == SeasonTotals(
        points=30, correct=6, possible=42, weeks_played=3, weekly_wins=0
    )


def test_season_totals_from_scored_weeks_end_to_end_standard():
    week_one = score_week(picks_all("home"), slate_outcomes(), mode="standard")
    week_two = score_week(picks_all("away"), slate_outcomes(), mode="standard")
    entries = [
        SeasonEntryInput(week_one.points, week_one.correct, week_one.possible, True),
        SeasonEntryInput(week_two.points, week_two.correct, week_two.possible, False),
    ]
    assert season_totals(entries) == SeasonTotals(
        points=105, correct=14, possible=28, weeks_played=2, weekly_wins=1
    )


def test_season_totals_from_scored_weeks_end_to_end_inverse():
    # Same two weeks, inverse mode: the all-correct week now pays 0, the all-wrong week
    # now pays the full 105, so the season total is the same number (105) but it is now
    # entirely attributable to the bad week instead of the good one, and a season total
    # by itself does not encode which. That is exactly why sort direction, not the sum,
    # is what has to change for inverse (see tests/test_standings.py).
    week_one = score_week(picks_all("home"), slate_outcomes(), mode="inverse")
    week_two = score_week(picks_all("away"), slate_outcomes(), mode="inverse")
    entries = [
        SeasonEntryInput(week_one.points, week_one.correct, week_one.possible, True),
        SeasonEntryInput(week_two.points, week_two.correct, week_two.possible, False),
    ]
    assert season_totals(entries) == SeasonTotals(
        points=105, correct=14, possible=28, weeks_played=2, weekly_wins=1
    )
