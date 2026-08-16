"""Unit tests for app.scenarios: placement odds, leverage, and build-your-own scenarios.

Pure logic only, no database, no network, exactly like test_scoring.py and test_slate.py.
Every hand-computed test below reasons through the actual scenarios in a comment or
docstring rather than asserting a number only ever produced by running the code once and
pasting the result, per the brief.
"""

from __future__ import annotations

import itertools
import math
import time

import pytest

from app.scenarios import (
    MONTE_CARLO_SAMPLES,
    PLACES,
    RemainingGame,
    _linearize_players,  # internal, tested directly
    _points_for_assignment,  # internal, tested directly
    build_custom_scenario,
    compute_scenario_report,
    implied_probability_from_moneyline,
    rank_players,
    win_probability,
)
from app.scoring import GameOutcome, PickInput, score_week

UTC_LEAGUE_SIGMA = {"nfl": 13.5, "ncaaf": 16.0}


def final(game_id: int, winner: str) -> GameOutcome:
    return GameOutcome(game_id=game_id, status="final", winner=winner)


def pick(game_id: int, side: str, confidence: int) -> PickInput:
    return PickInput(game_id=game_id, picked_team=side, confidence=confidence)


# rank_players -----------------------------------------------------------------


def test_rank_players_standard_highest_wins_no_ties():
    ranks = rank_players({1: 10, 2: 30, 3: 20}, mode="standard")
    assert ranks == {2: 1, 3: 2, 1: 3}


def test_rank_players_inverse_lowest_wins_no_ties():
    ranks = rank_players({1: 10, 2: 30, 3: 20}, mode="inverse")
    assert ranks == {1: 1, 3: 2, 2: 3}


def test_rank_players_competition_ranking_ties_skip_the_next_place():
    # Two players tied for 1st (both 50) push the next distinct value to 3rd, not 2nd.
    ranks = rank_players({1: 50, 2: 50, 3: 40}, mode="standard")
    assert ranks == {1: 1, 2: 1, 3: 3}


# Probability model --------------------------------------------------------------


def test_implied_probability_from_moneyline_favorite_and_underdog():
    # -150 favorite: 150 / (150 + 100) = 0.6
    assert implied_probability_from_moneyline(-150) == pytest.approx(0.6)
    # +130 underdog: 100 / (130 + 100) = 100 / 230
    assert implied_probability_from_moneyline(130) == pytest.approx(100 / 230)


def test_win_probability_devigs_moneyline_to_sum_to_one():
    game = RemainingGame(game_id=1, home_moneyline=-150, away_moneyline=130)
    p_home, fell_back = win_probability(game)
    assert not fell_back
    implied_home = 150 / 250  # 0.6
    implied_away = 100 / 230
    expected = implied_home / (implied_home + implied_away)
    assert p_home == pytest.approx(expected)
    # devigged, so the game's two sides always sum to exactly 1.0
    away_p, _ = win_probability(RemainingGame(game_id=1, home_moneyline=130, away_moneyline=-150))
    assert p_home + away_p == pytest.approx(1.0)


def test_win_probability_falls_back_to_spread_normal_cdf():
    # spread_home = -7 (home favoured by 7), NFL sigma 13.5.
    # p_home_win = Phi(7 / 13.5) = 0.5 * (1 + erf(7 / 13.5 / sqrt(2)))
    game = RemainingGame(game_id=1, spread_home=-7.0, league="nfl")
    p_home, fell_back = win_probability(game)
    assert not fell_back
    expected = 0.5 * (1 + math.erf((7.0 / 13.5) / math.sqrt(2)))
    assert p_home == pytest.approx(expected)


def test_win_probability_college_uses_wider_sigma_than_nfl():
    # The same spread is less decisive for college (wider sigma), so college's win
    # probability for the favourite sits closer to 0.5 than NFL's does.
    nfl_p, _ = win_probability(RemainingGame(game_id=1, spread_home=-7.0, league="nfl"))
    cfb_p, _ = win_probability(RemainingGame(game_id=1, spread_home=-7.0, league="ncaaf"))
    assert nfl_p > cfb_p > 0.5


def test_win_probability_no_data_falls_back_to_even_and_flags_it():
    p_home, fell_back = win_probability(RemainingGame(game_id=1))
    assert p_home == 0.5
    assert fell_back is True


# The core hand-computed case: two players, two remaining games, four scenarios --------
#
# Player A picks game 101 = home @ 5, game 102 = away @ 3 (standard mode: correct picks
# earn their confidence).
# Player B picks game 101 = away @ 5, game 102 = home @ 3.
#
# The four scenarios (game 101, game 102), worked out by hand:
#   (home, home): A scores 5 (101 right) + 0 (102 wrong)  = 5.  B scores 0 + 3      = 3.
#                 A > B, so A is 1st, B is 2nd.
#   (home, away): A scores 5 + 3 = 8.  B scores 0 + 0 = 0.  A is 1st, B is 2nd.
#   (away, home): A scores 0 + 0 = 0.  B scores 5 + 3 = 8.  B is 1st, A is 2nd.
#   (away, away): A scores 0 + 3 = 3.  B scores 5 + 0 = 5.  B is 1st, A is 2nd.
#
# So A is 1st in exactly 2 of the 4 scenarios (both where game 101 lands on home) and 2nd
# in the other 2 (both where it lands on away); B is the mirror image. Each of A's two 1st
# place scenarios has game 101 = home in BOTH of them (2 of 2 = 100%) and game 102 split
# evenly (1 of 2 = 50%), so game 101 is what actually matters to A's win and game 102 does
# not move the needle at all.


def _two_player_two_game_picks():
    picks = {
        1: [pick(101, "home", 5), pick(102, "away", 3)],
        2: [pick(101, "away", 5), pick(102, "home", 3)],
    }
    remaining = [RemainingGame(game_id=101), RemainingGame(game_id=102)]
    return picks, remaining


def test_two_player_two_game_hand_computed_counts_and_percentages():
    picks, remaining = _two_player_two_game_picks()
    report = compute_scenario_report(
        picks, [], remaining, mode="standard", picks_required=2, representative_for=[1, 2]
    )
    assert report.method == "exhaustive"
    assert not report.is_estimate
    assert report.scenario_count == 4
    # Under "even", each remaining game's weight is p_home=p_away=0.5, so every scenario's
    # weight is the product 0.5 * 0.5 = 0.25 and the four scenarios' weights sum to 1.0,
    # not to the scenario count: total_weight is a probability mass, not a tally.
    assert report.total_weight == pytest.approx(1.0)

    # Two of the four scenarios each weigh 0.25 (0.5 * 0.5 per game under "even"), so the
    # weighted count at each place is 2 * 0.25 = 0.5, and dividing by total_weight (1.0)
    # recovers the same 0.5 either way.
    a, b = report.players[1], report.players[2]
    assert a.scenarios_at_place == {
        1: pytest.approx(0.5),
        2: pytest.approx(0.5),
        3: pytest.approx(0.0),
    }
    assert a.pct_at_place == {1: pytest.approx(0.5), 2: pytest.approx(0.5), 3: pytest.approx(0.0)}
    assert b.scenarios_at_place == {
        1: pytest.approx(0.5),
        2: pytest.approx(0.5),
        3: pytest.approx(0.0),
    }
    assert b.pct_at_place == {1: pytest.approx(0.5), 2: pytest.approx(0.5), 3: pytest.approx(0.0)}


def test_two_player_two_game_leverage_matches_hand_computed_answer():
    picks, remaining = _two_player_two_game_picks()
    report = compute_scenario_report(
        picks, [], remaining, mode="standard", picks_required=2, representative_for=[1, 2]
    )
    a = report.players[1]
    # Both of A's winning scenarios have game 101 = home (100%), and split evenly on
    # game 102 (50/50): game 101 is what A needs, game 102 does not matter to A.
    assert a.leverage[101] == {"home": pytest.approx(1.0), "away": pytest.approx(0.0)}
    assert a.leverage[102] == {"home": pytest.approx(0.5), "away": pytest.approx(0.5)}

    b = report.players[2]
    assert b.leverage[101] == {"home": pytest.approx(0.0), "away": pytest.approx(1.0)}
    assert b.leverage[102] == {"home": pytest.approx(0.5), "away": pytest.approx(0.5)}


def test_two_player_two_game_representative_scenarios_are_real_winning_scenarios():
    picks, remaining = _two_player_two_game_picks()
    report = compute_scenario_report(
        picks, [], remaining, mode="standard", picks_required=2, representative_for=[1]
    )
    reps = report.players[1].representative_scenarios
    assert 1 <= len(reps) <= 2  # A only has 2 distinct winning scenarios to choose from
    for rep in reps:
        sides = dict(rep.assignment)
        assert sides[101] == "home"  # every one of A's winning scenarios needs 101 = home


# Clinched and eliminated ---------------------------------------------------------
#
# Player X picked a final game correctly for 10 points (standard mode) and picked none of
# the two remaining games, so X's score is a constant 10 no matter how they land. Player Y
# picked only the two remaining games, at confidence 1 and 2, so Y's best possible score
# (both correct) is 1 + 2 = 3. Since 10 > 3 always, X is 1st in literally every one of the
# 4 scenarios (clinched) and Y can never be 1st (eliminated at 1st); with only two players,
# the reverse holds for 2nd place.


def test_clinched_and_eliminated():
    final_outcomes = [final(1, "home")]
    picks = {
        10: [pick(1, "home", 10)],
        20: [pick(201, "home", 1), pick(202, "home", 2)],
    }
    remaining = [RemainingGame(game_id=201), RemainingGame(game_id=202)]
    report = compute_scenario_report(
        picks, final_outcomes, remaining, mode="standard", picks_required=2
    )

    x, y = report.players[10], report.players[20]
    assert x.clinched[1] is True
    assert x.eliminated[1] is False
    assert x.eliminated[2] is True  # X is never anywhere but 1st
    assert x.pct_at_place[1] == pytest.approx(1.0)

    assert y.eliminated[1] is True  # Y can never catch X
    assert y.clinched[1] is False
    assert y.clinched[2] is True  # Y is always 2nd, since there are only two players
    assert y.pct_at_place[2] == pytest.approx(1.0)


# Ties share a place, and percentages sum correctly --------------------------------
#
# One final game (won by home). Player A and Player B both pick ONLY the one remaining
# game, both on home, both at confidence 5, so A and B always score identically to each
# other on the remaining game (5 if home wins it, 0 if away wins it) -- they are a matched
# pair. Player C instead picks the final game correctly for a constant 1 point, and never
# picks the remaining game.
#
# Two scenarios for the remaining game, worked out by hand (standard mode, highest wins):
#   home: A = 5, B = 5, C = 1.        A and B tie for 1st (rank 1 each); C is 3rd, since
#                                     two players already sit ahead of C (competition
#                                     ranking skips rank 2 entirely here).
#   away: A = 0, B = 0, C = 1.        C is 1st alone; A and B tie for 2nd (rank 2 each).
#
# So, weighting each scenario equally (weight 1, total_weight 2 under the "even" model):
#   at place 1: the "home" scenario credits BOTH A and B (2 credits), the "away" scenario
#     credits only C (1 credit). Total credits across players = 3, so summed over players,
#     pct_at_place[1] adds up to 3 / 2 = 1.5, not 1.0: A and B each contribute 0.5 (1 of 2
#     scenarios) and C contributes 0.5 (1 of 2 scenarios), 0.5 + 0.5 + 0.5 = 1.5. A ties
#     scenario legitimately credits two players at once, so this is not a bug, it is what
#     "ties share a place" has to mean once you add the percentages back up.
#   at place 2: only the "away" scenario has anyone at 2nd (A and B, tied), so total
#     credits = 2, summed pct_at_place[2] = 2 / 2 = 1.0 (A: 0.5, B: 0.5, C: 0).
#   at place 3: only the "home" scenario has anyone at 3rd (C alone), so total credits = 1,
#     summed pct_at_place[3] = 1 / 2 = 0.5 (A: 0, B: 0, C: 0.5).


def test_ties_share_a_place_and_percentages_sum_correctly():
    final_outcomes = [final(1, "home")]
    picks = {
        1: [pick(301, "home", 5)],
        2: [pick(301, "home", 5)],
        3: [pick(1, "home", 1)],
    }
    remaining = [RemainingGame(game_id=301)]
    report = compute_scenario_report(
        picks, final_outcomes, remaining, mode="standard", picks_required=1
    )

    a, b, c = report.players[1], report.players[2], report.players[3]
    assert a.pct_at_place == {1: pytest.approx(0.5), 2: pytest.approx(0.5), 3: pytest.approx(0.0)}
    assert b.pct_at_place == {1: pytest.approx(0.5), 2: pytest.approx(0.5), 3: pytest.approx(0.0)}
    assert c.pct_at_place == {1: pytest.approx(0.5), 2: pytest.approx(0.0), 3: pytest.approx(0.5)}

    # The exact identity from the hand computation above: ties mean this sum is not 1.0.
    assert sum(o.pct_at_place[1] for o in (a, b, c)) == pytest.approx(1.5)
    assert sum(o.pct_at_place[2] for o in (a, b, c)) == pytest.approx(1.0)
    assert sum(o.pct_at_place[3] for o in (a, b, c)) == pytest.approx(0.5)


# Inverse vs standard: a different player wins under identical inputs ---------------
#
# Player P picks four final games: three correct at high confidence (10, 9, 8) and one
# wrong at a very high confidence (20). Player Q picks a single final game, wrong, at a
# tiny confidence (1).
#   standard (correct earns points, highest wins): P = 10 + 9 + 8 + 0 = 27, Q = 0.
#     P wins (27 > 0).
#   inverse (wrong earns points AGAINST you, lowest wins): P = 0 + 0 + 0 + 20 = 20, Q = 1.
#     Q wins (1 < 20): P's one big miss costs more than Q's one small one, even though P
#     was right the other three times and Q was never right at all.
# Same picks, same outcomes, opposite winner: the direction is genuinely honored, not just
# a sign flip of the same ranking.


def test_inverse_vs_standard_produce_different_winners():
    final_outcomes = [final(g, "home") for g in (1, 2, 3, 4)]
    picks = {
        10: [pick(1, "home", 10), pick(2, "home", 9), pick(3, "home", 8), pick(4, "away", 20)],
        20: [pick(1, "away", 1)],
    }
    standard = compute_scenario_report(picks, final_outcomes, [], mode="standard")
    inverse = compute_scenario_report(picks, final_outcomes, [], mode="inverse")

    standard_winner = [uid for uid, o in standard.players.items() if o.clinched[1]]
    inverse_winner = [uid for uid, o in inverse.players.items() if o.clinched[1]]
    assert standard_winner == [10]
    assert inverse_winner == [20]
    assert standard_winner != inverse_winner


# Moneyline weighting shifts percentages toward the favorite ------------------------


def test_moneyline_weighting_shifts_toward_the_favorite_versus_even():
    # Home is a big favorite (-300 versus +250 away). Player X needs home to win to take
    # 1st, player Y needs away.
    remaining = [RemainingGame(game_id=1, home_moneyline=-300, away_moneyline=250, league="nfl")]
    picks = {
        1: [pick(1, "home", 5)],
        2: [pick(1, "away", 5)],
    }
    even = compute_scenario_report(picks, [], remaining, mode="standard", probability_model="even")
    moneyline = compute_scenario_report(
        picks, [], remaining, mode="standard", probability_model="moneyline"
    )

    assert even.players[1].pct_at_place[1] == pytest.approx(0.5)
    p_home, _ = win_probability(remaining[0])
    assert moneyline.players[1].pct_at_place[1] == pytest.approx(p_home)
    # The favorite's own backer gains probability mass versus the even baseline, and the
    # underdog's backer loses exactly the same amount.
    assert moneyline.players[1].pct_at_place[1] > even.players[1].pct_at_place[1]
    assert moneyline.players[2].pct_at_place[1] < even.players[2].pct_at_place[1]
    assert moneyline.probability_model == "moneyline"


def test_moneyline_model_notes_a_game_with_no_moneyline_or_spread():
    remaining = [RemainingGame(game_id=1)]  # neither moneyline nor spread
    picks = {1: [pick(1, "home", 1)], 2: [pick(1, "away", 1)]}
    report = compute_scenario_report(
        picks, [], remaining, mode="standard", probability_model="moneyline"
    )
    assert any("Game 1" in note and "50/50" in note for note in report.probability_notes)


# Zero remaining games ------------------------------------------------------------


def test_zero_remaining_games_is_clean_and_final():
    final_outcomes = [final(1, "home"), final(2, "away")]
    picks = {
        1: [pick(1, "home", 10), pick(2, "away", 5)],  # both correct, 15
        2: [pick(1, "away", 10), pick(2, "home", 5)],  # both wrong, 0
        3: [pick(1, "home", 3), pick(2, "home", 2)],  # one correct, 3
    }
    report = compute_scenario_report(picks, final_outcomes, [], mode="standard")
    assert report.scenario_count == 1
    assert not report.is_estimate
    assert report.method == "exhaustive"
    assert report.total_weight == pytest.approx(1.0)

    # 1: 15, 3: 3, 2: 0. Highest wins: player 1 is 1st, player 3 2nd, player 2 3rd.
    assert report.players[1].clinched[1] is True
    assert report.players[1].pct_at_place[1] == pytest.approx(1.0)
    assert report.players[3].clinched[2] is True
    assert report.players[2].clinched[3] is True
    for uid, place in ((1, 1), (3, 2), (2, 3)):
        for other_place in PLACES:
            if other_place != place:
                assert report.players[uid].eliminated[other_place] is True


def test_zero_remaining_games_no_error_when_nobody_has_picked():
    report = compute_scenario_report({1: [], 2: []}, [], [], mode="inverse", picks_required=3)
    assert report.scenario_count == 1
    # Both no-shows take the same maximum penalty (sum 1..3 = 6) under inverse, so they tie
    # for 1st (lowest wins, and there is nothing lower on offer).
    assert report.players[1].clinched[1] is True
    assert report.players[2].clinched[1] is True


# Monte Carlo: convergence and reproducibility --------------------------------------
#
# Only remaining game 1 matters to either player (each stakes a pick on it and nothing
# else); every other remaining game is a "decoy" neither player picked, so it cannot move
# either score. The true answer is therefore exactly 0.5 / 0.5 regardless of how many
# decoys are added, which gives an exact reference to converge toward at any R, small
# (exhaustive) or large (Monte Carlo).


def _decoy_picks_and_games(decoys: int):
    picks = {1: [pick(1, "home", 5)], 2: [pick(1, "away", 5)]}
    remaining = [RemainingGame(game_id=1)] + [RemainingGame(game_id=100 + i) for i in range(decoys)]
    return picks, remaining


def test_monte_carlo_r24_converges_to_the_exact_small_case_ratio():
    exact_picks, exact_remaining = _decoy_picks_and_games(8)  # R = 9, still exhaustive
    exact = compute_scenario_report(exact_picks, [], exact_remaining, mode="standard")
    assert exact.method == "exhaustive"
    assert exact.players[1].pct_at_place[1] == pytest.approx(0.5)

    mc_picks, mc_remaining = _decoy_picks_and_games(23)  # R = 24, forces Monte Carlo
    mc = compute_scenario_report(mc_picks, [], mc_remaining, mode="standard", seed=1)
    assert mc.method == "monte_carlo"
    assert mc.is_estimate is True
    assert mc.players[1].pct_at_place[1] == pytest.approx(0.5, abs=0.05)
    assert mc.players[2].pct_at_place[1] == pytest.approx(0.5, abs=0.05)


def test_monte_carlo_is_reproducible_under_a_fixed_seed():
    picks, remaining = _decoy_picks_and_games(23)  # R = 24
    first = compute_scenario_report(
        picks, [], remaining, mode="inverse", seed=123, representative_for=[1]
    )
    second = compute_scenario_report(
        picks, [], remaining, mode="inverse", seed=123, representative_for=[1]
    )
    assert first.method == "monte_carlo"
    assert first.scenario_count == second.scenario_count
    assert first.players[1].scenarios_at_place == second.players[1].scenarios_at_place
    assert first.players[1].pct_at_place == second.players[1].pct_at_place
    assert first.players[1].leverage == second.players[1].leverage

    # A different seed is not asserted to differ (that would be flaky), only recorded that
    # it is allowed to.
    third = compute_scenario_report(picks, [], remaining, mode="inverse", seed=999)
    assert third.method == "monte_carlo"


def test_monte_carlo_never_exceeds_the_hard_cap():
    picks, remaining = _decoy_picks_and_games(23)  # R = 24
    start = time.perf_counter()
    report = compute_scenario_report(picks, [], remaining, mode="inverse", seed=5)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    assert report.elapsed_seconds < 2.0


def test_monte_carlo_after_an_aborted_exhaustive_attempt_still_respects_the_hard_cap():
    """Regression test for a real bug: when R <= MAX_EXHAUSTIVE_REMAINING but too slow to
    finish exhaustively (a large pool at R around 20, a very plausible mid-week shape), the
    Monte Carlo fallback used to size its own sample budget off a fixed fraction of the
    ORIGINAL time_budget_seconds, oblivious to how much the exhaustive attempt above it had
    already spent before aborting. Since the exhaustive abort fraction and the Monte Carlo
    stop fraction were both 0.75 of that same original total, the fallback routinely
    committed to more samples than the time actually left could finish, overshooting the
    hard cap (reproduced directly on real hardware: 2.0-2.1s against a 2.0s budget, every
    run, not a rare timing fluke). 40 players at R = 20, the largest R exhaustive is ever
    attempted for, reliably makes the exhaustive attempt too slow to finish inside the
    default 2 second budget, forcing this exact fallback path rather than the R = 24 tests
    above, which skip the exhaustive attempt entirely and never exercise it.
    """
    final_outcomes = [final(g, "home") for g in range(1, 3)]
    remaining = [RemainingGame(game_id=g, spread_home=1.5, league="nfl") for g in range(3, 23)]
    picks = {
        p: [pick(g, "home" if (g + p) % 2 == 0 else "away", 22 - g) for g in range(1, 23)]
        for p in range(1, 41)
    }

    start = time.perf_counter()
    report = compute_scenario_report(
        picks, final_outcomes, remaining, mode="inverse", picks_required=20
    )
    elapsed = time.perf_counter() - start

    assert report.method == "monte_carlo"
    assert report.is_estimate
    # A small margin over the 2.0s hard cap covers ordinary measurement/scheduling jitter,
    # not the ~100ms+ overshoot the bug produced every single run.
    assert elapsed < 2.15
    assert report.elapsed_seconds < 2.15


def test_monte_carlo_sample_count_respects_the_requested_target():
    picks, remaining = _decoy_picks_and_games(23)
    report = compute_scenario_report(
        picks, [], remaining, mode="inverse", seed=5, monte_carlo_samples=1_000
    )
    assert report.scenario_count <= 1_000
    assert report.scenario_count <= MONTE_CARLO_SAMPLES


# The R = 15, 16 player timing test (the brief's hard requirement) ------------------


def test_exhaustive_r15_16_players_completes_well_under_the_two_second_cap():
    final_outcomes = [final(g, "home") for g in range(1, 6)]
    remaining = [RemainingGame(game_id=g, spread_home=1.5, league="nfl") for g in range(6, 21)]
    picks = {
        p: [pick(g, "home" if (g + p) % 2 == 0 else "away", 21 - g) for g in range(1, 21)][:15]
        for p in range(1, 17)
    }

    start = time.perf_counter()
    report = compute_scenario_report(
        picks, final_outcomes, remaining, mode="inverse", picks_required=15, representative_for=[1]
    )
    elapsed = time.perf_counter() - start

    assert report.method == "exhaustive"
    assert not report.is_estimate
    assert report.scenario_count == 2**15
    # A generous bound, not a rubber stamp: measured well under 1 second on the build
    # machine (see DECISIONS.md, Phase 8), a wide margin under the 2 second hard cap.
    assert elapsed < 1.5
    assert report.elapsed_seconds < 1.5


# Linearization is exact, not an approximation: cross check against brute force score_week


def test_linearization_matches_brute_force_score_week_on_every_scenario():
    final_outcomes = [final(1, "home")]
    remaining = [RemainingGame(game_id=g) for g in (2, 3, 4)]  # R = 3, 8 scenarios
    picks = {
        1: [pick(1, "home", 4), pick(2, "home", 3), pick(3, "away", 2), pick(4, "home", 1)],
        2: [pick(1, "away", 1), pick(3, "home", 2)],  # skips game 2 and 4 entirely
        3: [],  # a no-show
    }
    linear_by_player = _linearize_players(picks, final_outcomes, remaining, "inverse", 4)

    for sides in itertools.product(("home", "away"), repeat=3):
        combined = final_outcomes + [
            GameOutcome(game_id=g.game_id, status="final", winner=side)
            for g, side in zip(remaining, sides, strict=True)
        ]
        for user_id, linear in linear_by_player.items():
            brute = score_week(picks[user_id], combined, mode="inverse", picks_required=4)
            assert _points_for_assignment(linear, sides) == brute.points


# Build your own scenario ----------------------------------------------------------


def test_build_custom_scenario_undecided_game_is_simply_omitted():
    final_outcomes = [final(1, "home")]
    picks = {
        1: [pick(1, "home", 5), pick(2, "home", 3), pick(3, "away", 2)],
        2: [pick(1, "away", 5), pick(2, "away", 3), pick(3, "home", 2)],
    }
    # Game 2 fixed home, game 3 left undecided (None).
    fixed = {2: "home", 3: None}
    rows = build_custom_scenario(picks, final_outcomes, fixed, mode="standard")

    by_user = {r.user_id: r for r in rows}
    # Player 1: game1 (5, correct) + game2 (3, correct) = 8. Game 3 not counted (undecided).
    assert by_user[1].points == 8
    assert by_user[1].possible == 2
    # Player 2: game1 wrong (0) + game2 wrong (0) = 0. Game 3 not counted.
    assert by_user[2].points == 0
    assert by_user[2].possible == 2
    assert by_user[1].place == 1
    assert by_user[2].place == 2

    # Matches score_week directly over the same combined, explicit outcome list.
    combined = final_outcomes + [GameOutcome(game_id=2, status="final", winner="home")]
    expected = score_week(picks[1], combined, mode="standard")
    assert by_user[1].points == expected.points
    assert by_user[1].correct == expected.correct


def test_build_custom_scenario_all_undecided_matches_final_outcomes_only():
    final_outcomes = [final(1, "home")]
    picks = {1: [pick(1, "home", 5), pick(2, "home", 3)]}
    rows = build_custom_scenario(picks, final_outcomes, {2: None}, mode="standard")
    assert rows[0].points == 5
    assert rows[0].possible == 1


def test_build_custom_scenario_home_and_away_both_fixed():
    final_outcomes: list[GameOutcome] = []
    picks = {
        1: [pick(1, "home", 5), pick(2, "away", 3)],
        2: [pick(1, "away", 5), pick(2, "home", 3)],
    }
    rows = build_custom_scenario(picks, final_outcomes, {1: "home", 2: "away"}, mode="standard")
    by_user = {r.user_id: r for r in rows}
    assert by_user[1].points == 8  # both of player 1's picks correct
    assert by_user[2].points == 0  # both of player 2's picks wrong
    assert by_user[1].place == 1
    assert by_user[2].place == 2


def test_build_custom_scenario_did_not_submit_flows_through():
    rows = build_custom_scenario({1: []}, [], {}, mode="inverse", picks_required=3)
    assert rows[0].did_not_submit is True
    assert rows[0].points == 6  # sum(1..3)
