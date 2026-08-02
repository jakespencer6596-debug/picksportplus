"""Unit tests for the cross provider team matcher. Pure logic, no network.

The point of this file is the integration risk called out in Spec 5d: ESPN, The
Odds API and CollegeFootballData spell teams differently, and a mismatch either
loses a spread (acceptable, the admin sees it) or attaches the wrong spread to a
game (not acceptable). So the tests below check three things in order of
importance: trap pairs never collapse into one key, the same team spelled three
ways always collapses into one key, and anything less than a confident match
returns None.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re

import pytest

from app.providers.teams import (
    FBS_TEAMS,
    NFL_TEAMS,
    MatchCandidate,
    canonical_key,
    display_name,
    match_by_teams_and_date,
    normalize_name,
    same_team,
)

UTC = dt.timezone.utc
EASTERN = dt.timezone(dt.timedelta(hours=-5))

KEY_RE = re.compile(r"^[a-z0-9]+:[a-z0-9]+(?:-[a-z0-9]+)*$")


def at(day: int, hour: int = 13, minute: int = 0, tz: dt.tzinfo = UTC) -> dt.datetime:
    """A kickoff in a fixed September week, so the tests read like a schedule."""
    return dt.datetime(2025, 9, day, hour, minute, tzinfo=tz)


def candidate(home: str, away: str, kickoff: dt.datetime, payload: object = None) -> MatchCandidate:
    return MatchCandidate(
        home_key=home,
        away_key=away,
        kickoff=kickoff,
        payload=payload if payload is not None else f"{away} at {home}",
    )


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


def test_normalize_lowercases_and_collapses_whitespace():
    assert normalize_name("  GREEN   Bay\tPackers \n") == "green bay packers"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("San José State", "san jose state"),
        ("Hawaiʻi", "hawaii"),
        ("Hawai'i Rainbow Warriors", "hawaii rainbow warriors"),
        ("München Tech", "munchen tech"),
    ],
)
def test_normalize_strips_accents_and_apostrophes(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Periods vanish without splitting the token, so N.C. becomes nc.
        ("N.C. State", "nc state"),
        ("U.S.C.", "usc"),
        ("St. John's", "saint johns"),
        # Everything else becomes a space.
        ("Miami (FL)", "miami fl"),
        ("Miami (OH)", "miami oh"),
        ("Louisiana-Monroe", "louisiana monroe"),
        ("K-State", "k state"),
        ("Louisiana Ragin' Cajuns", "louisiana ragin cajuns"),
    ],
)
def test_normalize_punctuation(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Texas A&M", "texas a and m"),
        ("Texas A&M Aggies", "texas a and m aggies"),
        ("A&M", "a and m"),
    ],
)
def test_normalize_expands_ampersand(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#5 Ohio State", "ohio state"),
        ("#25 Boise State", "boise state"),
        ("(5) Ohio State", "ohio state"),
        ("(12) Texas", "texas"),
        ("# 3 Georgia", "georgia"),
        ("(1) #2 Alabama", "alabama"),
    ],
)
def test_normalize_strips_rank_prefixes(raw, expected):
    assert normalize_name(raw) == expected


def test_normalize_does_not_treat_a_trailing_paren_as_a_rank():
    # Miami (FL) must keep its qualifier. Only a leading numeral is a rank.
    assert normalize_name("Miami (FL)") == "miami fl"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("University of Miami", "miami"),
        ("University of Notre Dame", "notre dame"),
        ("Rice University", "rice"),
        ("Ohio State University", "ohio state"),
        # Nothing to drop, and never reduce the string to nothing.
        ("University", "university"),
        ("University of", "university of"),
    ],
)
def test_normalize_university_rules(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Trailing token means state.
        ("Ohio St", "ohio state"),
        ("Ohio St.", "ohio state"),
        ("Appalachian St", "appalachian state"),
        ("Iowa St", "iowa state"),
        # Leading token means saint.
        ("St. John's", "saint johns"),
        ("St Bonaventure", "saint bonaventure"),
        ("St. Louis Rams", "saint louis rams"),
        # Middle token, known state school stem, means state.
        ("Michigan St Spartans", "michigan state spartans"),
        ("Penn St Nittany Lions", "penn state nittany lions"),
        ("Fresno St Bulldogs", "fresno state bulldogs"),
        # Middle token with no evidence is left alone rather than guessed at.
        ("Acme St Widgets", "acme st widgets"),
    ],
)
def test_normalize_st_heuristic(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, 17, object()])
def test_normalize_handles_junk_input(raw):
    assert normalize_name(raw) == ""


def test_normalize_keeps_digits():
    assert normalize_name("San Francisco 49ers") == "san francisco 49ers"


@pytest.mark.parametrize(
    "raw",
    [
        "St. John's",
        "Ohio St",
        "#5 Texas A&M Aggies",
        "University of Miami",
        "Hawaiʻi",
        "Miami (OH)",
        "Louisiana-Monroe",
    ],
)
def test_normalize_is_idempotent(raw):
    once = normalize_name(raw)
    assert normalize_name(once) == once


# ---------------------------------------------------------------------------
# canonical_key, general behaviour
# ---------------------------------------------------------------------------


def test_every_table_key_has_the_documented_shape():
    for team in (*NFL_TEAMS, *FBS_TEAMS):
        assert KEY_RE.match(team.key), team.key


def test_documented_example_keys():
    assert canonical_key("Green Bay Packers", "nfl") == "nfl:green-bay-packers"
    assert canonical_key("Ohio State", "ncaaf") == "ncaaf:ohio-state"


def test_table_keys_are_unique_within_a_league():
    nfl_keys = [team.key for team in NFL_TEAMS]
    fbs_keys = [team.key for team in FBS_TEAMS]
    assert len(set(nfl_keys)) == len(nfl_keys) == 32
    assert len(set(fbs_keys)) == len(fbs_keys)


def test_fbs_table_covers_the_full_membership():
    assert len(FBS_TEAMS) >= 136


def test_canonical_key_is_deterministic():
    for _ in range(3):
        assert canonical_key("Ohio State Buckeyes", "ncaaf") == "ncaaf:ohio-state"


@pytest.mark.parametrize(
    ("name", "league"),
    [
        (None, "nfl"),
        ("", "ncaaf"),
        ("   ", "ncaaf"),
        ("Ohio State", None),
        ("Ohio State", ""),
        (17, "nfl"),
        ("ʻʻʻ", "ncaaf"),
        (object(), object()),
    ],
)
def test_canonical_key_never_raises(name, league):
    key = canonical_key(name, league)
    assert isinstance(key, str)
    assert key


def test_unknown_school_falls_back_to_a_slug():
    assert canonical_key("Nowhere Tech", "ncaaf") == "ncaaf:nowhere-tech"
    assert canonical_key("Nowhere St", "ncaaf") == "ncaaf:nowhere-state"


def test_empty_name_gives_an_unknown_key_that_never_matches_itself():
    key = canonical_key("", "ncaaf")
    assert key == "ncaaf:unknown"
    assert same_team(key, key) is False


@pytest.mark.parametrize(
    "league",
    ["ncaaf", "NCAAF", "college-football", "americanfootball_ncaaf", "CFB", "college football"],
)
def test_league_spellings_agree(league):
    assert canonical_key("Ohio State", league) == "ncaaf:ohio-state"


@pytest.mark.parametrize("league", ["nfl", "NFL", "americanfootball_nfl", " nfl "])
def test_nfl_league_spellings_agree(league):
    assert canonical_key("Green Bay Packers", league) == "nfl:green-bay-packers"


def test_keys_are_scoped_by_league():
    # Miami is a Dolphin in one league and a Hurricane in the other.
    assert canonical_key("Miami", "nfl") != canonical_key("Miami", "ncaaf")
    assert canonical_key("Miami", "nfl") == "nfl:miami-dolphins"
    assert canonical_key("Miami", "ncaaf") == "ncaaf:miami-fl"


def test_key_round_trips_through_canonical_key():
    # Storing a key on a game row and re-canonicalising it must be a no-op,
    # otherwise same_team would drift as rows age.
    for team in (*NFL_TEAMS, *FBS_TEAMS):
        league, _, rest = team.key.partition(":")
        assert canonical_key(rest.replace("-", " "), league) == team.key


# ---------------------------------------------------------------------------
# NFL coverage
# ---------------------------------------------------------------------------


def test_nfl_table_is_complete_and_populated():
    assert len(NFL_TEAMS) == 32
    for team in NFL_TEAMS:
        assert team.display_name == f"{team.location} {team.nickname}"
        assert team.odds_api_name == team.display_name
        assert team.espn_abbr


def test_every_nfl_spelling_resolves_to_its_own_key():
    shared_cities = {"New York", "Los Angeles"}
    for team in NFL_TEAMS:
        spellings = [team.display_name, team.odds_api_name, team.nickname, *team.aliases]
        if team.location not in shared_cities:
            spellings.append(team.location)
        for spelling in spellings:
            assert canonical_key(spelling, "nfl") == team.key, spelling
        for abbr in (team.espn_abbr, *team.alt_abbrs):
            assert canonical_key("Not A Real Team", "nfl", abbr) == team.key, abbr


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Washington Commanders", "Washington Football Team"),
        ("Washington Commanders", "Washington Redskins"),
        ("Washington Commanders", "Washington"),
        ("Los Angeles Rams", "LA Rams"),
        ("Los Angeles Rams", "St. Louis Rams"),
        ("Los Angeles Chargers", "LA Chargers"),
        ("Los Angeles Chargers", "San Diego Chargers"),
        ("New York Giants", "NY Giants"),
        ("New York Giants", "N.Y. Giants"),
        ("New York Jets", "NY Jets"),
        ("Las Vegas Raiders", "Oakland Raiders"),
        ("San Francisco 49ers", "Niners"),
        ("Tampa Bay Buccaneers", "Bucs"),
        ("New England Patriots", "Pats"),
        ("Green Bay Packers", "Packers"),
        ("Green Bay Packers", "Green Bay"),
    ],
)
def test_nfl_common_alternates_agree(left, right):
    assert canonical_key(left, "nfl") == canonical_key(right, "nfl")


@pytest.mark.parametrize(
    ("ambiguous", "first", "second"),
    [
        ("New York", "New York Giants", "New York Jets"),
        ("Los Angeles", "Los Angeles Rams", "Los Angeles Chargers"),
    ],
)
def test_shared_nfl_cities_do_not_resolve_to_either_team(ambiguous, first, second):
    key = canonical_key(ambiguous, "nfl")
    assert key != canonical_key(first, "nfl")
    assert key != canonical_key(second, "nfl")


def test_nfl_teams_are_all_distinct():
    keys = {canonical_key(team.display_name, "nfl") for team in NFL_TEAMS}
    assert len(keys) == 32


# ---------------------------------------------------------------------------
# FBS coverage
# ---------------------------------------------------------------------------


def test_every_fbs_spelling_resolves_to_its_own_key():
    for team in FBS_TEAMS:
        for spelling in (team.school, team.display_name, *team.aliases):
            assert canonical_key(spelling, "ncaaf") == team.key, spelling


def test_every_fbs_abbreviation_hint_resolves_to_its_own_key():
    for team in FBS_TEAMS:
        assert canonical_key("Not A Real Team", "ncaaf", team.espn_abbr) == team.key, team.espn_abbr


def test_mascot_inclusive_and_mascot_free_forms_agree():
    for team in FBS_TEAMS:
        assert canonical_key(team.school, "ncaaf") == canonical_key(team.display_name, "ncaaf")


# The traps. Every name inside a group must produce a distinct key.
TRAP_GROUPS = [
    ("Miami", "Miami (OH)"),
    ("Miami (FL)", "Miami (OH)", "Ohio", "Ohio State"),
    ("USC", "South Carolina"),
    ("Texas", "Texas State", "Texas Tech", "UTSA", "UTEP", "Texas A&M", "North Texas"),
    ("Ole Miss", "Mississippi State", "Southern Miss"),
    ("Louisiana", "Louisiana Monroe", "Louisiana Tech", "LSU"),
    ("San Jose State", "San Diego State"),
    ("NC State", "North Carolina"),
    ("FIU", "FAU"),
    (
        "UCF",
        "South Florida",
        "Florida",
        "Florida State",
        "Florida Atlantic",
        "Florida International",
    ),
    ("Michigan", "Michigan State"),
    ("Washington", "Washington State"),
    ("Oregon", "Oregon State"),
    ("Arizona", "Arizona State"),
    ("Kansas", "Kansas State"),
    ("Colorado", "Colorado State"),
    ("Utah", "Utah State"),
    ("Georgia", "Georgia State", "Georgia Southern", "Georgia Tech"),
    ("Iowa", "Iowa State"),
    ("Oklahoma", "Oklahoma State"),
    ("Penn State", "Kent State"),
    ("Appalachian State", "Arkansas State"),
    ("UConn", "UMass"),
    ("Pittsburgh", "Penn State"),
    ("Boise State", "Ball State"),
    ("Central Michigan", "Eastern Michigan", "Western Michigan", "Michigan"),
    ("Buffalo", "Charlotte"),
    ("Sam Houston", "Houston"),
    ("Middle Tennessee", "Tennessee"),
    ("Jacksonville State", "Kennesaw State"),
    ("New Mexico", "New Mexico State"),
    ("Missouri", "Missouri State"),
    ("Delaware", "Duke"),
]


@pytest.mark.parametrize("group", TRAP_GROUPS, ids=lambda group: "-vs-".join(group))
def test_trap_groups_resolve_to_different_keys(group):
    keys = [canonical_key(name, "ncaaf") for name in group]
    assert len(set(keys)) == len(group), dict(zip(group, keys))
    for left in range(len(group)):
        for right in range(left + 1, len(group)):
            assert same_team(keys[left], keys[right]) is False


# The same program spelled the way each feed spells it. ESPN gives displayName
# and location, CFBD gives school, The Odds API gives a mascot inclusive name.
SAME_TEAM_GROUPS = [
    ("Ohio State Buckeyes", "Ohio State", "Ohio St", "#5 Ohio State", "(3) Ohio State Buckeyes"),
    ("Miami Hurricanes", "Miami", "Miami (FL)", "Miami (FL) Hurricanes", "University of Miami"),
    ("Miami (OH) RedHawks", "Miami (OH)", "Miami OH", "Miami Ohio", "Miami of Ohio"),
    ("USC Trojans", "USC", "Southern California", "Southern Cal"),
    ("Texas A&M Aggies", "Texas A&M", "Texas AM", "Texas A and M", "TAMU"),
    ("Ole Miss Rebels", "Ole Miss", "Mississippi", "Mississippi Rebels"),
    ("Pittsburgh Panthers", "Pittsburgh", "Pitt", "Pitt Panthers"),
    ("UConn Huskies", "UConn", "Connecticut", "Connecticut Huskies"),
    ("UMass Minutemen", "UMass", "Massachusetts", "Massachusetts Minutemen"),
    (
        "Appalachian State Mountaineers",
        "Appalachian State",
        "App State",
        "App St",
        "Appalachian St",
    ),
    ("Southern Miss Golden Eagles", "Southern Miss", "Southern Mississippi"),
    (
        "Louisiana Ragin' Cajuns",
        "Louisiana",
        "Louisiana-Lafayette",
        "Louisiana Lafayette",
        "UL Lafayette",
    ),
    ("Louisiana Monroe Warhawks", "Louisiana-Monroe", "UL Monroe", "ULM"),
    (
        "San Jose State Spartans",
        "San Jose State",
        "San José State",
        "San Jose St",
        "SJSU",
    ),
    (
        "Hawaiʻi Rainbow Warriors",
        "Hawaii",
        "Hawai'i",
        "Hawaii Rainbow Warriors",
        "Hawaii Warriors",
    ),
    ("NC State Wolfpack", "NC State", "North Carolina State", "N.C. State", "NC St"),
    ("North Carolina Tar Heels", "North Carolina", "UNC"),
    ("FIU Panthers", "FIU", "Florida International"),
    ("Florida Atlantic Owls", "Florida Atlantic", "FAU"),
    ("UCF Knights", "UCF", "Central Florida"),
    ("SMU Mustangs", "SMU", "Southern Methodist"),
    ("TCU Horned Frogs", "TCU", "Texas Christian"),
    ("BYU Cougars", "BYU", "Brigham Young"),
    ("LSU Tigers", "LSU", "Louisiana State"),
    ("Michigan State Spartans", "Michigan State", "Michigan St", "Michigan St Spartans"),
    ("Texas State Bobcats", "Texas State", "Texas St"),
    ("UTSA Roadrunners", "UTSA", "Texas-San Antonio", "UT San Antonio"),
    ("UTEP Miners", "UTEP", "Texas-El Paso", "UT El Paso"),
]


@pytest.mark.parametrize("group", SAME_TEAM_GROUPS, ids=lambda group: group[0])
def test_provider_spellings_agree(group):
    keys = [canonical_key(name, "ncaaf") for name in group]
    assert len(set(keys)) == 1, dict(zip(group, keys))
    for other in keys[1:]:
        assert same_team(keys[0], other) is True


def test_espn_abbreviation_disambiguates_the_two_miamis():
    assert canonical_key("Miami", "ncaaf", "MIA") == "ncaaf:miami-fl"
    assert canonical_key("Miami (OH)", "ncaaf", "M-OH") == "ncaaf:miami-oh"
    # A name that already resolved wins over the abbreviation hint, so a stale
    # or wrong abbreviation cannot drag a good name to the wrong team.
    assert canonical_key("Miami (OH)", "ncaaf", "MIA") == "ncaaf:miami-oh"


def test_unfamiliar_mascot_suffix_is_trimmed_safely():
    # Not a spelling in any table, but the trailing words are known mascots.
    assert canonical_key("Louisiana Lafayette Ragin Cajuns", "ncaaf") == canonical_key(
        "Louisiana", "ncaaf"
    )
    # Trimming must never reach across a qualifier and turn a RedHawk into a
    # Hurricane, because "oh" is not a mascot word.
    assert canonical_key("Miami OH RedHawks", "ncaaf") == "ncaaf:miami-oh"


# The shorthand a human writes when a real feed would qualify the school. The
# mascot belongs to the in state rival, so trimming it and keeping the bare
# location would attach the rival's spread to the game. Every one of these must
# stay unresolved rather than resolve to the team named on the left.
WRONG_MASCOT_PAIRS = [
    ("Miami RedHawks", "Miami"),
    ("Miami Redhawks", "Miami"),
    ("Ohio Buckeyes", "Ohio"),
    ("Michigan Spartans", "Michigan"),
    ("Washington Cougars", "Washington"),
    ("Texas Aggies", "Texas"),
    ("Louisiana Bulldogs", "Louisiana"),
    ("Mississippi Bulldogs", "Mississippi"),
    ("Arkansas Red Wolves", "Arkansas"),
    ("Kansas Wildcats", "Kansas"),
]


@pytest.mark.parametrize(("spelling", "other_school"), WRONG_MASCOT_PAIRS, ids=lambda v: v)
def test_mascot_trimming_never_lands_on_a_team_with_a_different_mascot(spelling, other_school):
    key = canonical_key(spelling, "ncaaf")
    assert key != canonical_key(other_school, "ncaaf"), spelling
    assert display_name(key) is None, spelling
    assert same_team(key, canonical_key(other_school, "ncaaf")) is False


# Alias plus mascot, the form The Odds API tends to publish. None of these are
# listed verbatim in the table, so they all reach the matcher through trimming.
ALIAS_PLUS_MASCOT = [
    ("UL Monroe Warhawks", "ncaaf:louisiana-monroe"),
    ("Southern Mississippi Golden Eagles", "ncaaf:southern-miss"),
    ("Massachusetts Minutemen", "ncaaf:umass"),
    ("Connecticut Huskies", "ncaaf:uconn"),
    ("Central Florida Knights", "ncaaf:ucf"),
    ("Southern California Trojans", "ncaaf:usc"),
    ("Brigham Young Cougars", "ncaaf:byu"),
    ("Louisiana State Tigers", "ncaaf:lsu"),
    ("Texas Christian Horned Frogs", "ncaaf:tcu"),
    ("Southern Methodist Mustangs", "ncaaf:smu"),
    ("Florida International Panthers", "ncaaf:fiu"),
    ("Middle Tennessee State Blue Raiders", "ncaaf:middle-tennessee"),
    ("Sam Houston State Bearkats", "ncaaf:sam-houston"),
    ("Texas-San Antonio Roadrunners", "ncaaf:utsa"),
    ("Texas-El Paso Miners", "ncaaf:utep"),
    ("Alabama-Birmingham Blazers", "ncaaf:uab"),
    ("Nevada-Las Vegas Rebels", "ncaaf:unlv"),
    ("Bowling Green State Falcons", "ncaaf:bowling-green"),
    ("Mississippi Rebels", "ncaaf:ole-miss"),
    # "green" is a mascot word and part of a school name. It is still trimmable,
    # because the trim is checked against the mascot of the team it lands on.
    ("UNT Mean Green", "ncaaf:north-texas"),
    # The middle "st" resolves to "state" off a known stem, then the mascot goes.
    ("Miss St Bulldogs", "ncaaf:mississippi-state"),
    ("App St Mountaineers", "ncaaf:appalachian-state"),
]


@pytest.mark.parametrize(("spelling", "expected"), ALIAS_PLUS_MASCOT, ids=lambda v: v)
def test_alias_plus_mascot_resolves(spelling, expected):
    assert canonical_key(spelling, "ncaaf") == expected


def test_every_alias_plus_its_own_mascot_resolves():
    # "University" aliases are excluded: the trailing word is dropped only when
    # it ends the string, so "Ohio State University Buckeyes" is out of scope.
    for team in FBS_TEAMS:
        for alias in team.aliases:
            if "university" in alias.lower():
                continue
            combo = f"{alias} {team.mascot}"
            assert canonical_key(combo, "ncaaf") == team.key, combo


# ---------------------------------------------------------------------------
# same_team
# ---------------------------------------------------------------------------


def test_same_team_matches_identical_keys():
    assert same_team("ncaaf:ohio-state", "ncaaf:ohio-state") is True


def test_same_team_tolerates_case_and_padding():
    assert same_team("  NCAAF:Ohio-State ", "ncaaf:ohio-state") is True


def test_same_team_resolves_alternate_keys():
    assert same_team("nfl:la-rams", "nfl:los-angeles-rams") is True
    assert same_team("ncaaf:pitt", "ncaaf:pittsburgh") is True
    assert same_team("ncaaf:connecticut", "ncaaf:uconn") is True


def test_same_team_rejects_different_teams():
    assert same_team("ncaaf:ohio-state", "ncaaf:ohio") is False
    assert same_team("ncaaf:miami-fl", "ncaaf:miami-oh") is False
    assert same_team("nfl:new-york-jets", "nfl:new-york-giants") is False


def test_same_team_never_matches_across_leagues():
    assert same_team("nfl:miami-dolphins", "ncaaf:miami-fl") is False


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("", ""),
        ("ncaaf:unknown", "ncaaf:unknown"),
        ("nfl:unknown", "nfl:unknown"),
        ("ncaaf:unknown", "ncaaf:ohio-state"),
        ("", "ncaaf:ohio-state"),
        (None, "ncaaf:ohio-state"),
        ("ncaaf:ohio-state", None),
        ("unknown:unknown", "unknown:unknown"),
        # An unknown league is as unresolved as an unknown team, otherwise two
        # games from different sports could be paired.
        ("unknown:ohio-state", "unknown:ohio-state"),
        (":ohio-state", ":ohio-state"),
    ],
)
def test_same_team_treats_unknown_as_never_equal(left, right):
    assert same_team(left, right) is False


@pytest.mark.parametrize(
    "value",
    [
        "garbage",
        "hello world",
        # A bare team name is not a key. It could be the Dolphins or the
        # Hurricanes, so two of them are not evidence of the same team.
        "miami",
        "ohio-state",
        "nfl",
    ],
)
def test_same_team_rejects_a_value_that_is_not_a_canonical_key(value):
    assert same_team(value, value) is False


def test_same_team_when_the_league_could_not_be_read():
    key = canonical_key("Ohio State", None)
    assert same_team(key, key) is False


def test_same_team_does_not_fold_an_unresolved_slug_onto_a_real_team():
    # The slug a mascot mismatch falls back to must stay distinct from the team
    # whose name it contains, including after a round trip through the tables.
    slug = canonical_key("Miami RedHawks", "ncaaf")
    assert same_team(slug, "ncaaf:miami-fl") is False
    assert same_team(slug, "ncaaf:miami-oh") is False
    assert same_team(slug, slug) is True


# ---------------------------------------------------------------------------
# display_name
# ---------------------------------------------------------------------------


def test_display_name_reads_back_from_a_key():
    assert display_name("nfl:green-bay-packers") == "Green Bay Packers"
    assert display_name("ncaaf:ohio-state") == "Ohio State Buckeyes"
    assert display_name("ncaaf:miami-oh") == "Miami (OH) RedHawks"


def test_display_name_is_none_for_an_unknown_key():
    assert display_name("ncaaf:nowhere-tech") is None
    assert display_name("ncaaf:unknown") is None
    assert display_name("") is None


# ---------------------------------------------------------------------------
# MatchCandidate
# ---------------------------------------------------------------------------


def test_match_candidate_is_frozen():
    entry = candidate("ncaaf:ohio-state", "ncaaf:michigan", at(6))
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.home_key = "ncaaf:michigan"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# match_by_teams_and_date
# ---------------------------------------------------------------------------

HOME = "ncaaf:ohio-state"
AWAY = "ncaaf:michigan"


def test_match_returns_the_candidate_when_teams_and_date_line_up():
    wanted = candidate(HOME, AWAY, at(6, 12), payload={"spread": -3.5})
    noise = candidate("ncaaf:texas", "ncaaf:oklahoma", at(6, 12))
    found = match_by_teams_and_date(HOME, AWAY, at(6, 12), [noise, wanted])
    assert found is wanted
    assert found.payload == {"spread": -3.5}


def test_match_accepts_a_different_provider_spelling_of_the_same_key():
    # The candidate list was canonicalised from The Odds API, which writes the
    # mascot inclusive name, while the ESPN side used the location.
    odds_side = candidate(
        canonical_key("Ohio State Buckeyes", "ncaaf"),
        canonical_key("Michigan Wolverines", "ncaaf"),
        at(6, 12),
    )
    espn_home = canonical_key("Ohio State", "ncaaf")
    espn_away = canonical_key("Michigan", "ncaaf")
    assert match_by_teams_and_date(espn_home, espn_away, at(6, 12), [odds_side]) is odds_side


def test_match_works_across_timezones_for_the_same_instant():
    entry = candidate(HOME, AWAY, dt.datetime(2025, 9, 6, 17, 0, tzinfo=UTC))
    kickoff = dt.datetime(2025, 9, 6, 12, 0, tzinfo=EASTERN)
    assert match_by_teams_and_date(HOME, AWAY, kickoff, [entry]) is entry


def test_match_allows_a_kickoff_inside_the_tolerance():
    entry = candidate(HOME, AWAY, at(7, 12))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [entry], tolerance_hours=30.0) is entry


def test_match_tolerance_boundary_is_inclusive():
    entry = candidate(HOME, AWAY, at(6, 12) + dt.timedelta(hours=30))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [entry], tolerance_hours=30.0) is entry


def test_match_rejects_a_kickoff_outside_the_tolerance():
    entry = candidate(HOME, AWAY, at(6, 12) + dt.timedelta(hours=30, seconds=1))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [entry], tolerance_hours=30.0) is None


def test_match_honours_a_tighter_tolerance():
    entry = candidate(HOME, AWAY, at(6, 18))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [entry], tolerance_hours=3.0) is None
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [entry], tolerance_hours=8.0) is entry


def test_match_with_zero_tolerance_needs_an_exact_instant():
    exact = candidate(HOME, AWAY, at(6, 12))
    late = candidate(HOME, AWAY, at(6, 13))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [exact], tolerance_hours=0) is exact
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [late], tolerance_hours=0) is None


def test_match_prefers_the_closest_kickoff_when_several_fit():
    near = candidate(HOME, AWAY, at(6, 14), payload="near")
    far = candidate(HOME, AWAY, at(7, 2), payload="far")
    found = match_by_teams_and_date(HOME, AWAY, at(6, 12), [far, near])
    assert found is near


# --- the never guess rules -------------------------------------------------


def test_match_returns_none_for_a_home_away_swap():
    swapped = candidate(AWAY, HOME, at(6, 12))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [swapped]) is None


def test_match_prefers_the_correct_orientation_over_a_swap():
    swapped = candidate(AWAY, HOME, at(6, 12), payload="swapped")
    correct = candidate(HOME, AWAY, at(6, 13), payload="correct")
    found = match_by_teams_and_date(HOME, AWAY, at(6, 12), [swapped, correct])
    assert found is correct


def test_match_returns_none_when_only_one_side_agrees():
    half = candidate(HOME, "ncaaf:penn-state", at(6, 12))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [half]) is None


def test_match_returns_none_when_two_candidates_tie():
    first = candidate(HOME, AWAY, at(6, 12), payload="first")
    second = candidate(HOME, AWAY, at(6, 12), payload="second")
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [first, second]) is None


def test_match_returns_none_for_an_empty_candidate_list():
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), []) is None


def test_match_returns_none_when_a_requested_key_is_unknown():
    entry = candidate(HOME, "ncaaf:unknown", at(6, 12))
    assert match_by_teams_and_date(HOME, "ncaaf:unknown", at(6, 12), [entry]) is None
    assert match_by_teams_and_date("", AWAY, at(6, 12), [entry]) is None


def test_match_skips_candidates_with_unknown_keys():
    junk = candidate("ncaaf:unknown", "ncaaf:unknown", at(6, 12))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [junk]) is None


def test_match_returns_none_when_both_requested_keys_are_the_same_team():
    entry = candidate(HOME, HOME, at(6, 12))
    assert match_by_teams_and_date(HOME, HOME, at(6, 12), [entry]) is None


def test_match_returns_none_for_a_naive_kickoff():
    entry = candidate(HOME, AWAY, at(6, 12))
    naive = dt.datetime(2025, 9, 6, 12, 0)
    assert match_by_teams_and_date(HOME, AWAY, naive, [entry]) is None


def test_match_skips_candidates_with_a_naive_kickoff():
    naive_entry = candidate(HOME, AWAY, dt.datetime(2025, 9, 6, 12, 0))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [naive_entry]) is None


def test_match_returns_none_for_a_negative_tolerance():
    entry = candidate(HOME, AWAY, at(6, 12))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [entry], tolerance_hours=-1) is None


def test_match_returns_none_for_a_nonsense_tolerance():
    entry = candidate(HOME, AWAY, at(6, 12))
    assert match_by_teams_and_date(HOME, AWAY, at(6, 12), [entry], tolerance_hours="soon") is None


def test_match_returns_none_for_a_nan_tolerance():
    # NaN loses every comparison, so an unguarded NaN window accepts a kickoff
    # in a different season.
    far_off = candidate(HOME, AWAY, dt.datetime(2030, 1, 1, tzinfo=UTC))
    result = match_by_teams_and_date(HOME, AWAY, at(6, 12), [far_off], tolerance_hours=float("nan"))
    assert result is None


def test_match_ignores_candidates_whose_keys_are_not_canonical():
    # A provider adapter that stored a bare team name instead of a key must not
    # match, even when both sides carry the same bare name.
    junk = candidate("miami", "ohio", at(6, 12))
    assert match_by_teams_and_date("miami", "ohio", at(6, 12), [junk]) is None


def test_match_does_not_pair_the_two_miamis():
    # The classic wrong spread: Miami (OH) at Ohio, matched against Miami (FL).
    hurricanes = canonical_key("Miami Hurricanes", "ncaaf")
    redhawks = canonical_key("Miami (OH) RedHawks", "ncaaf")
    ohio = canonical_key("Ohio Bobcats", "ncaaf")
    entry = candidate(ohio, redhawks, at(6, 12))
    assert match_by_teams_and_date(ohio, hurricanes, at(6, 12), [entry]) is None
    assert match_by_teams_and_date(ohio, redhawks, at(6, 12), [entry]) is entry


def test_match_does_not_pair_usc_with_south_carolina():
    usc = canonical_key("USC Trojans", "ncaaf")
    gamecocks = canonical_key("South Carolina Gamecocks", "ncaaf")
    ucla = canonical_key("UCLA Bruins", "ncaaf")
    entry = candidate(ucla, gamecocks, at(6, 12))
    assert match_by_teams_and_date(ucla, usc, at(6, 12), [entry]) is None


def test_match_does_not_pair_an_unresolved_school_with_another_one():
    # Two different schools the tables do not know. Neither should ever be
    # treated as the same team just because both fell back to a slug.
    left = canonical_key("Nowhere Tech", "ncaaf")
    right = canonical_key("Somewhere Tech", "ncaaf")
    entry = candidate(left, "ncaaf:texas", at(6, 12))
    assert match_by_teams_and_date(right, "ncaaf:texas", at(6, 12), [entry]) is None
