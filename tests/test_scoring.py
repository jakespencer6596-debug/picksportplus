"""Unit tests for app.scoring (Spec Sections 9 and 17).

Pure logic, no database and no network. Game ids are 1..N and the confidence stack is
handed out top down: game 1 gets N points, game N gets 1 point, unless a test says otherwise.
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


# Public contract


def test_team_sides_is_the_exported_pair():
    assert TEAM_SIDES == ("home", "away")


def test_dataclass_field_order_is_stable_for_positional_callers():
    # Services construct these positionally, so the order is part of the contract.
    assert GameOutcome(1, "final", "home") == GameOutcome(game_id=1, status="final", winner="home")
    assert PickInput(1, "home", 14) == PickInput(game_id=1, picked_team="home", confidence=14)
    assert WeekResult(88, 11, 14) == WeekResult(points=88, correct=11, possible=14)
    assert SeasonEntryInput(88, 11, 14, True) == SeasonEntryInput(
        points=88, correct=11, possible=14, is_winner=True
    )
    assert SeasonTotals(88, 11, 14, 1, 1) == SeasonTotals(
        points=88, correct=11, possible=14, weeks_played=1, weekly_wins=1
    )


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


# Single pick scoring


def test_score_pick_correct_earns_confidence():
    assert score_pick(PickInput(1, "home", 12), final(1, "home")) == 12


def test_score_pick_wrong_earns_zero():
    assert score_pick(PickInput(1, "away", 12), final(1, "home")) == 0


def test_score_pick_tie_and_void_earn_zero_even_when_side_matches():
    assert score_pick(PickInput(1, "home", 14), tie(1)) == 0
    assert score_pick(PickInput(1, "home", 14), void(1)) == 0


def test_score_pick_unfinished_game_earns_zero():
    assert score_pick(PickInput(1, "home", 9), scheduled(1)) == 0
    assert score_pick(PickInput(1, "home", 9), in_progress(1)) == 0


# Week scoring


def test_all_correct_full_slate():
    result = score_week(picks_all("home"), slate_outcomes())
    assert result == WeekResult(points=105, correct=14, possible=14)
    assert result.points == sum(range(1, 15))


def test_all_wrong_full_slate():
    result = score_week(picks_all("away"), slate_outcomes(winner="home"))
    assert result == WeekResult(points=0, correct=0, possible=14)


def test_mixed_week_hand_computed():
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
    assert score_week(picks, outcomes) == WeekResult(points=12, correct=3, possible=5)


def test_unsubmitted_player_scores_zero_against_full_possible():
    assert score_week([], slate_outcomes()) == WeekResult(points=0, correct=0, possible=14)


def test_tie_game_is_excluded_from_correct_and_possible():
    outcomes = slate_outcomes()
    outcomes[3] = tie(4)  # game 4 ends level
    result = score_week(picks_all("home"), outcomes)
    # Game 4 staked 11 points (14 down to 1), so the winning total drops by 11.
    assert result == WeekResult(points=94, correct=13, possible=13)


def test_voided_game_is_treated_like_a_tie():
    outcomes = slate_outcomes()
    outcomes[0] = void(1)  # commissioner voided the top game
    result = score_week(picks_all("home"), outcomes)
    assert result == WeekResult(points=91, correct=13, possible=13)


def test_scheduled_and_in_progress_games_are_not_countable_yet():
    outcomes = slate_outcomes()
    outcomes[0] = scheduled(1)  # staked 14
    outcomes[1] = in_progress(2)  # staked 13
    result = score_week(picks_all("home"), outcomes)
    assert result == WeekResult(points=105 - 14 - 13, correct=12, possible=12)


def test_partial_week_possible_counts_only_final_games():
    # Only the first three games are final. Nothing else has kicked off.
    outcomes = [final(1, "home"), final(2, "away"), final(3, "home")]
    outcomes += [scheduled(i) for i in range(4, 15)]
    result = score_week(picks_all("home"), outcomes)
    assert result == WeekResult(points=14 + 12, correct=2, possible=3)


def test_short_slate_of_ten_games():
    result = score_week(picks_all("home", 10), slate_outcomes(10))
    assert result == WeekResult(points=55, correct=10, possible=10)


def test_short_slate_mixed_result():
    outcomes = slate_outcomes(10)
    outcomes[2] = final(3, "away")  # game 3 staked 8
    outcomes[9] = final(10, "away")  # game 10 staked 1
    result = score_week(picks_all("home", 10), outcomes)
    assert result == WeekResult(points=55 - 8 - 1, correct=8, possible=10)


def test_pick_for_a_game_that_left_the_slate_is_ignored():
    picks = picks_all("home", 3) + [PickInput(99, "home", 50)]
    assert score_week(picks, slate_outcomes(3)) == WeekResult(points=6, correct=3, possible=3)


def test_possible_counts_slate_games_the_player_skipped():
    picks = [PickInput(1, "home", 3)]
    assert score_week(picks, slate_outcomes(3)) == WeekResult(points=3, correct=1, possible=3)


def test_empty_slate_scores_nothing():
    assert score_week([], []) == WeekResult(points=0, correct=0, possible=0)


def test_correct_pick_staked_at_zero_still_counts_as_correct():
    # A stored confidence of 0 is invalid input, but it must not make a right pick
    # disappear from the correct count. Points and correct are separate facts.
    outcomes = [final(1, "home"), final(2, "home")]
    picks = [PickInput(1, "home", 0), PickInput(2, "home", 1)]
    assert score_week(picks, outcomes) == WeekResult(points=1, correct=2, possible=2)


def test_wrong_pick_staked_at_zero_is_not_correct():
    outcomes = [final(1, "home")]
    assert score_week([PickInput(1, "away", 0)], outcomes) == WeekResult(
        points=0, correct=0, possible=1
    )


def test_duplicate_outcome_rows_do_not_inflate_possible():
    # score_week keys outcomes by game_id, so a repeated row is still one game.
    outcomes = [final(1, "home"), final(1, "home"), final(2, "away")]
    picks = [PickInput(1, "home", 2), PickInput(2, "away", 1)]
    assert score_week(picks, outcomes) == WeekResult(points=3, correct=2, possible=2)


# Validation


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


def test_validate_picks_missing_one_game():
    slate = [1, 2, 3, 4, 5]
    picks = [
        PickInput(1, "home", 4),
        PickInput(2, "home", 3),
        PickInput(3, "away", 2),
        PickInput(4, "home", 1),
    ]
    assert validate_picks(picks, slate) == ["One game is missing a winner."]


def test_validate_picks_missing_two_games():
    slate = [1, 2, 3, 4, 5]
    picks = [
        PickInput(1, "home", 5),
        PickInput(2, "home", 4),
        PickInput(3, "away", 3),
    ]
    assert validate_picks(picks, slate) == ["Two games are missing a winner."]


def test_validate_picks_extra_game_not_on_slate():
    slate = [1, 2, 3, 4, 5]
    picks = picks_all("home", 5) + [PickInput(99, "home", 5)]
    assert validate_picks(picks, slate) == ["Game 99 is not on this week's slate."]


def test_validate_picks_same_game_picked_twice():
    slate = [1, 2, 3]
    picks = [
        PickInput(1, "home", 3),
        PickInput(1, "away", 2),
        PickInput(2, "home", 1),
    ]
    errors = validate_picks(picks, slate)
    assert "Game 1 has more than one pick." in errors
    assert "One game is missing a winner." in errors


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
    assert validate_picks(picks, slate) == ["Game 9 is not on this week's slate."]


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


# Weekly winners


def test_weekly_winner_ids_single_winner():
    results = {
        1: WeekResult(88, 11, 14),
        2: WeekResult(74, 9, 14),
        3: WeekResult(51, 7, 14),
    }
    assert weekly_winner_ids(results) == {1}


def test_weekly_winner_ids_two_way_tie_shares_the_win():
    results = {
        1: WeekResult(88, 11, 14),
        2: WeekResult(88, 10, 14),
        3: WeekResult(51, 7, 14),
    }
    assert weekly_winner_ids(results) == {1, 2}


def test_weekly_winner_ids_empty_mapping():
    assert weekly_winner_ids({}) == set()


def test_weekly_winner_ids_all_zero_week_has_no_winner():
    results = {
        1: WeekResult(0, 0, 14),
        2: WeekResult(0, 0, 14),
    }
    assert weekly_winner_ids(results) == set()


def test_weekly_winner_ids_ignores_correct_count_as_a_tiebreak():
    results = {
        1: WeekResult(60, 5, 14),
        2: WeekResult(60, 12, 14),
    }
    assert weekly_winner_ids(results) == {1, 2}


# Season aggregation


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


def test_season_totals_from_scored_weeks_end_to_end():
    week_one = score_week(picks_all("home"), slate_outcomes())
    week_two = score_week(picks_all("away"), slate_outcomes())
    entries = [
        SeasonEntryInput(week_one.points, week_one.correct, week_one.possible, True),
        SeasonEntryInput(week_two.points, week_two.correct, week_two.possible, False),
    ]
    assert season_totals(entries) == SeasonTotals(
        points=105, correct=14, possible=28, weeks_played=2, weekly_wins=1
    )
