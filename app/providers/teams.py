"""Cross provider team identity (Spec 5d).

ESPN, The Odds API and CollegeFootballData all spell teams differently. ESPN
gives a `displayName` ("Ohio State Buckeyes") and a `location` ("Ohio State").
CFBD gives a `school` ("Ohio State"). The Odds API gives a full name that is
usually mascot inclusive ("Ohio State Buckeyes", "Green Bay Packers"). This
module folds all of those into one stable canonical key such as
"nfl:green-bay-packers" or "ncaaf:ohio-state", and matches a game against a
list of candidate games coming from a different provider.

The guiding rule: when a match is not confident, return None. A missing spread
is surfaced to the admin review screen. A wrong spread silently corrupts the
slate, so it is much worse than a missing one. Nothing here guesses.

Normalization summary
---------------------
`normalize_name` lowercases, strips accents (NFKD), deletes apostrophes and
periods, turns "&" into "and", turns every other punctuation mark into a space,
and collapses whitespace. It also strips AP poll rank prefixes ("#5 ", "(5) "),
drops a leading "university of" and a trailing "university", and expands "st".

The "st" heuristic
------------------
"st" is genuinely ambiguous: it means "state" in "Ohio St" and "saint" in
"St. John's". The rule is positional:

* leading token, so index 0, becomes "saint" ("St. John's" gives "saint johns")
* trailing token becomes "state" ("Ohio St" gives "ohio state")
* a middle token becomes "state" only when everything before it is a known
  state school stem ("Michigan St Spartans" gives "michigan state spartans")
* otherwise it is left as "st", because guessing is worse than not matching

Known caveat: "Miami University" is the legal name of Miami (OH), but the
trailing "university" rule reduces it to "miami", which resolves to the
Hurricanes. No provider emits that form. ESPN, CFBD and The Odds API all
qualify the RedHawks as "Miami (OH)", and the abbreviation hint plus the
explicit "Miami Univ" alias cover the rest.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "MatchCandidate",
    "NflTeam",
    "CollegeTeam",
    "NFL_TEAMS",
    "NFL_TEAMS_BY_KEY",
    "FBS_TEAMS",
    "FBS_TEAMS_BY_KEY",
    "UNKNOWN",
    "normalize_name",
    "canonical_key",
    "same_team",
    "match_by_teams_and_date",
    "display_name",
]

# Team part used when there is nothing to canonicalise. Two unknown keys never
# match each other, which is what keeps an unresolvable game out of the slate.
UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchCandidate:
    """One candidate game from another provider, already canonicalised."""

    home_key: str
    away_key: str
    kickoff: datetime
    payload: Any


@dataclass(frozen=True)
class NflTeam:
    key: str
    display_name: str
    location: str
    nickname: str
    espn_abbr: str
    odds_api_name: str
    alt_abbrs: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollegeTeam:
    key: str
    school: str
    mascot: str
    display_name: str
    espn_abbr: str = ""
    aliases: tuple[str, ...] = field(default=())


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# "#5 ", "#5", "(5) ", "( 5 )". Anchored, so "Miami (FL)" is untouched.
_RANK_PREFIX_RE = re.compile(r"^\s*(?:#\s*\d{1,2}|\(\s*\d{1,2}\s*\))\s*")

# Apostrophe family plus the Hawaiian okina. Deleted rather than spaced so
# "St. John's" gives "johns" and "Hawai'i" gives "hawaii".
_DELETE_CHARS = str.maketrans({c: "" for c in "'’‘ʻʼʽ`´."})

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_WS_RE = re.compile(r"\s+")

# Stems that are known "X State" schools, used only for the ambiguous middle
# "st" case. Deliberately a literal constant so normalize_name stays
# deterministic and independent of table build order.
_STATE_SCHOOL_STEMS = frozenset(
    {
        "alabama",
        "app",
        "appalachian",
        "arizona",
        "arkansas",
        "ball",
        "boise",
        "colorado",
        "delaware",
        "florida",
        "fresno",
        "georgia",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "jackson",
        "jacksonville",
        "kansas",
        "kennesaw",
        "kent",
        "michigan",
        "middle tennessee",
        "miss",
        "mississippi",
        "missouri",
        "montana",
        "murray",
        "nc",
        "new mexico",
        "north carolina",
        "north dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "penn",
        "portland",
        "sacramento",
        "sam houston",
        "san diego",
        "san jose",
        "south dakota",
        "tennessee",
        "texas",
        "utah",
        "washington",
        "weber",
        "wichita",
        "wright",
        "youngstown",
    }
)


def normalize_name(name: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not isinstance(name, str):
        return ""
    raw = name.strip()
    if not raw:
        return ""

    # AP poll rank prefixes can stack in odd feeds, so peel a couple.
    for _ in range(2):
        stripped = _RANK_PREFIX_RE.sub("", raw)
        if stripped == raw:
            break
        raw = stripped

    decomposed = unicodedata.normalize("NFKD", raw)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = without_accents.lower()
    lowered = lowered.translate(_DELETE_CHARS)
    lowered = lowered.replace("&", " and ")
    spaced = _NON_ALNUM_RE.sub(" ", lowered)
    collapsed = _WS_RE.sub(" ", spaced).strip()
    if not collapsed:
        return ""

    tokens = collapsed.split(" ")

    # Leading "university of" and trailing "university" both carry no identity.
    if len(tokens) > 2 and tokens[0] == "university" and tokens[1] == "of":
        tokens = tokens[2:]
    if len(tokens) > 1 and tokens[-1] == "university":
        tokens = tokens[:-1]

    tokens = _expand_st(tokens)
    return " ".join(tokens)


def _expand_st(tokens: list[str]) -> list[str]:
    """Apply the positional "st" heuristic documented in the module docstring."""
    if "st" not in tokens:
        return tokens
    last = len(tokens) - 1
    out: list[str] = []
    for index, token in enumerate(tokens):
        if token != "st":
            out.append(token)
        elif index == 0:
            out.append("saint")
        elif index == last:
            out.append("state")
        elif " ".join(tokens[:index]) in _STATE_SCHOOL_STEMS:
            out.append("state")
        else:
            out.append("st")
    return out


def _slug(normalized: str) -> str:
    return normalized.replace(" ", "-")


def _norm_abbr(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


# ---------------------------------------------------------------------------
# League normalization
# ---------------------------------------------------------------------------

_LEAGUE_ALIASES = {
    "nfl": "nfl",
    "americanfootball nfl": "nfl",
    "national football league": "nfl",
    "pro": "nfl",
    "ncaaf": "ncaaf",
    "americanfootball ncaaf": "ncaaf",
    "ncaa": "ncaaf",
    "ncaafb": "ncaaf",
    "cfb": "ncaaf",
    "college": "ncaaf",
    "college football": "ncaaf",
    "fbs": "ncaaf",
}


def _normalize_league(league: Any) -> str:
    if not isinstance(league, str):
        return UNKNOWN
    cleaned = _WS_RE.sub(" ", _NON_ALNUM_RE.sub(" ", league.lower())).strip()
    if not cleaned:
        return UNKNOWN
    return _LEAGUE_ALIASES.get(cleaned, _slug(cleaned))


# ---------------------------------------------------------------------------
# NFL table. All 32 teams.
# ---------------------------------------------------------------------------

# location, nickname, ESPN abbreviation, alternate abbreviations, extra aliases.
# The Odds API uses the full "location nickname" form, so odds_api_name is the
# display name. Bare "LA" and bare "NY" are deliberately absent from the
# abbreviation lists because each covers two teams.
_NFL_RAW: tuple[tuple[str, str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("Arizona", "Cardinals", "ARI", ("ARZ",), ()),
    ("Atlanta", "Falcons", "ATL", (), ()),
    ("Baltimore", "Ravens", "BAL", ("BLT",), ()),
    ("Buffalo", "Bills", "BUF", (), ()),
    ("Carolina", "Panthers", "CAR", (), ()),
    ("Chicago", "Bears", "CHI", (), ()),
    ("Cincinnati", "Bengals", "CIN", (), ()),
    ("Cleveland", "Browns", "CLE", ("CLV",), ()),
    ("Dallas", "Cowboys", "DAL", (), ()),
    ("Denver", "Broncos", "DEN", (), ()),
    ("Detroit", "Lions", "DET", (), ()),
    ("Green Bay", "Packers", "GB", ("GNB",), ("GB Packers",)),
    ("Houston", "Texans", "HOU", ("HST",), ()),
    ("Indianapolis", "Colts", "IND", (), ("Indy Colts",)),
    ("Jacksonville", "Jaguars", "JAX", ("JAC",), ("Jags",)),
    ("Kansas City", "Chiefs", "KC", ("KAN",), ("KC Chiefs",)),
    (
        "Las Vegas",
        "Raiders",
        "LV",
        ("LVR", "OAK"),
        ("Oakland Raiders", "LV Raiders", "Vegas Raiders"),
    ),
    (
        "Los Angeles",
        "Chargers",
        "LAC",
        ("SD", "SDG"),
        ("LA Chargers", "San Diego Chargers"),
    ),
    (
        "Los Angeles",
        "Rams",
        "LAR",
        ("STL",),
        ("LA Rams", "St. Louis Rams"),
    ),
    ("Miami", "Dolphins", "MIA", (), ("Fins",)),
    ("Minnesota", "Vikings", "MIN", (), ("Vikes",)),
    ("New England", "Patriots", "NE", ("NWE",), ("NE Patriots", "Pats")),
    ("New Orleans", "Saints", "NO", ("NOR",), ("NO Saints",)),
    ("New York", "Giants", "NYG", (), ("NY Giants", "N.Y. Giants")),
    ("New York", "Jets", "NYJ", (), ("NY Jets", "N.Y. Jets")),
    ("Philadelphia", "Eagles", "PHI", (), ("Philly Eagles",)),
    ("Pittsburgh", "Steelers", "PIT", (), ()),
    (
        "San Francisco",
        "49ers",
        "SF",
        ("SFO",),
        ("SF 49ers", "Niners", "San Francisco Forty Niners"),
    ),
    ("Seattle", "Seahawks", "SEA", (), ()),
    (
        "Tampa Bay",
        "Buccaneers",
        "TB",
        ("TAM",),
        ("TB Buccaneers", "Bucs", "Tampa Bay Bucs"),
    ),
    ("Tennessee", "Titans", "TEN", (), ()),
    (
        "Washington",
        "Commanders",
        "WSH",
        ("WAS", "WFT"),
        ("Washington Football Team", "Washington Redskins"),
    ),
)


def _build_nfl() -> tuple[NflTeam, ...]:
    teams: list[NflTeam] = []
    for location, nickname, abbr, alt_abbrs, aliases in _NFL_RAW:
        display = f"{location} {nickname}"
        teams.append(
            NflTeam(
                key=f"nfl:{_slug(normalize_name(display))}",
                display_name=display,
                location=location,
                nickname=nickname,
                espn_abbr=abbr,
                odds_api_name=display,
                alt_abbrs=tuple(alt_abbrs),
                aliases=tuple(aliases),
            )
        )
    return tuple(teams)


NFL_TEAMS: tuple[NflTeam, ...] = _build_nfl()
NFL_TEAMS_BY_KEY: dict[str, NflTeam] = {team.key: team for team in NFL_TEAMS}


# ---------------------------------------------------------------------------
# FBS table. The ~136 programs, with the naming variants the three feeds use.
# ---------------------------------------------------------------------------

# school, mascot, key override (blank means slug the school), ESPN abbreviation
# hint (blank means none), extra aliases.
#
# Two rules for the extra aliases. Never register a mascot on its own, because
# mascots repeat across programs (Tigers, Bulldogs, Wildcats). Never register an
# initialism that covers more than one FBS program (ASU, KSU, SDSU are all
# omitted for that reason).
_FBS_RAW: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    # Atlantic Coast Conference
    ("Boston College", "Eagles", "", "BC", ("BC Eagles",)),
    ("California", "Golden Bears", "", "CAL", ("Cal", "Cal Golden Bears", "UC Berkeley")),
    ("Clemson", "Tigers", "", "CLEM", ()),
    ("Duke", "Blue Devils", "", "DUKE", ()),
    ("Florida State", "Seminoles", "", "FSU", ("Florida St", "FSU")),
    ("Georgia Tech", "Yellow Jackets", "", "GT", ("Georgia Institute of Technology",)),
    ("Louisville", "Cardinals", "", "LOU", ()),
    (
        "Miami",
        "Hurricanes",
        "ncaaf:miami-fl",
        "MIA",
        (
            "Miami (FL)",
            "Miami FL",
            "Miami Florida",
            "Miami-Florida",
            "Miami Fla",
            "Miami (FL) Hurricanes",
            "Miami FL Hurricanes",
        ),
    ),
    (
        "NC State",
        "Wolfpack",
        "",
        "NCST",
        (
            "North Carolina State",
            "N.C. State",
            "NC St",
            "North Carolina St",
            "North Carolina State Wolfpack",
            "NC State Wolfpack",
        ),
    ),
    ("North Carolina", "Tar Heels", "", "UNC", ("UNC", "UNC Tar Heels")),
    ("Pittsburgh", "Panthers", "", "PITT", ("Pitt", "Pitt Panthers")),
    ("SMU", "Mustangs", "", "SMU", ("Southern Methodist", "Southern Methodist Mustangs")),
    ("Stanford", "Cardinal", "", "STAN", ()),
    ("Syracuse", "Orange", "", "SYR", ("Cuse",)),
    ("Virginia", "Cavaliers", "", "UVA", ("UVA",)),
    ("Virginia Tech", "Hokies", "", "VT", ("VT", "Va Tech")),
    ("Wake Forest", "Demon Deacons", "", "WAKE", ()),
    # Big Ten
    ("Illinois", "Fighting Illini", "", "ILL", ()),
    ("Indiana", "Hoosiers", "", "IND", ()),
    ("Iowa", "Hawkeyes", "", "IOWA", ()),
    ("Maryland", "Terrapins", "", "MD", ("Terps",)),
    ("Michigan", "Wolverines", "", "MICH", ()),
    ("Michigan State", "Spartans", "", "MSU", ("Michigan St", "MSU Spartans")),
    ("Minnesota", "Golden Gophers", "", "MINN", ("Minnesota Gophers",)),
    ("Nebraska", "Cornhuskers", "", "NEB", ()),
    ("Northwestern", "Wildcats", "", "NW", ()),
    (
        "Ohio State",
        "Buckeyes",
        "",
        "OSU",
        ("Ohio St", "Ohio State University", "The Ohio State University"),
    ),
    ("Oregon", "Ducks", "", "ORE", ()),
    ("Penn State", "Nittany Lions", "", "PSU", ("Penn St", "PSU")),
    ("Purdue", "Boilermakers", "", "PUR", ()),
    ("Rutgers", "Scarlet Knights", "", "RUTG", ()),
    ("UCLA", "Bruins", "", "UCLA", ()),
    (
        "USC",
        "Trojans",
        "",
        "USC",
        ("Southern California", "Southern Cal", "Southern California Trojans"),
    ),
    ("Washington", "Huskies", "", "WASH", ()),
    ("Wisconsin", "Badgers", "", "WIS", ()),
    # Big 12
    ("Arizona", "Wildcats", "", "ARIZ", ()),
    ("Arizona State", "Sun Devils", "", "ASU", ("Arizona St",)),
    ("Baylor", "Bears", "", "BAY", ()),
    ("BYU", "Cougars", "", "BYU", ("Brigham Young", "Brigham Young Cougars")),
    ("Cincinnati", "Bearcats", "", "CIN", ()),
    ("Colorado", "Buffaloes", "", "COLO", ("Colorado Buffs",)),
    ("Houston", "Cougars", "", "HOU", ()),
    ("Iowa State", "Cyclones", "", "ISU", ("Iowa St",)),
    ("Kansas", "Jayhawks", "", "KU", ("KU",)),
    ("Kansas State", "Wildcats", "", "KSU", ("Kansas St", "K-State", "K State")),
    ("Oklahoma", "Sooners", "", "OKLA", ()),
    ("Oklahoma State", "Cowboys", "", "OKST", ("Oklahoma St", "Okla State")),
    ("TCU", "Horned Frogs", "", "TCU", ("Texas Christian", "Texas Christian Horned Frogs")),
    ("Texas Tech", "Red Raiders", "", "TTU", ("TTU",)),
    ("UCF", "Knights", "", "UCF", ("Central Florida", "Central Florida Knights")),
    ("Utah", "Utes", "", "UTAH", ()),
    ("West Virginia", "Mountaineers", "", "WVU", ("WVU",)),
    # Southeastern Conference
    ("Alabama", "Crimson Tide", "", "ALA", ("Bama",)),
    ("Arkansas", "Razorbacks", "", "ARK", ()),
    ("Auburn", "Tigers", "", "AUB", ()),
    ("Florida", "Gators", "", "FLA", ()),
    ("Georgia", "Bulldogs", "", "UGA", ("UGA",)),
    ("Kentucky", "Wildcats", "", "UK", ()),
    ("LSU", "Tigers", "", "LSU", ("Louisiana State", "Louisiana State Tigers")),
    ("Mississippi State", "Bulldogs", "", "MSST", ("Mississippi St", "Miss State", "Miss St")),
    ("Missouri", "Tigers", "", "MIZ", ("Mizzou",)),
    ("Ole Miss", "Rebels", "", "MISS", ("Mississippi", "Mississippi Rebels")),
    ("South Carolina", "Gamecocks", "", "SC", ()),
    ("Tennessee", "Volunteers", "", "TENN", ("Tennessee Vols",)),
    ("Texas", "Longhorns", "", "TEX", ()),
    (
        "Texas A&M",
        "Aggies",
        "ncaaf:texas-am",
        "TAMU",
        ("Texas AM", "Texas A and M", "TAMU", "Texas A&M Aggies", "Texas AM Aggies"),
    ),
    ("Vanderbilt", "Commodores", "", "VAN", ("Vandy",)),
    # Pac-12
    ("Oregon State", "Beavers", "", "ORST", ("Oregon St",)),
    ("Washington State", "Cougars", "", "WSU", ("Washington St", "Wazzu", "WSU")),
    # American Athletic Conference
    ("Army", "Black Knights", "", "ARMY", ("Army West Point",)),
    ("Charlotte", "49ers", "", "CLT", ()),
    ("East Carolina", "Pirates", "", "ECU", ("ECU",)),
    ("Florida Atlantic", "Owls", "", "FAU", ("FAU", "FAU Owls")),
    ("Memphis", "Tigers", "", "MEM", ()),
    ("Navy", "Midshipmen", "", "NAVY", ()),
    ("North Texas", "Mean Green", "", "UNT", ("UNT",)),
    ("Rice", "Owls", "", "RICE", ()),
    ("South Florida", "Bulls", "", "USF", ("USF", "USF Bulls")),
    ("Temple", "Owls", "", "TEM", ()),
    ("Tulane", "Green Wave", "", "TULN", ()),
    ("Tulsa", "Golden Hurricane", "", "TLSA", ()),
    ("UAB", "Blazers", "", "UAB", ("Alabama-Birmingham", "Alabama Birmingham")),
    (
        "UTSA",
        "Roadrunners",
        "",
        "UTSA",
        ("Texas-San Antonio", "Texas San Antonio", "UT San Antonio"),
    ),
    # Conference USA
    ("Delaware", "Blue Hens", "", "DEL", ()),
    (
        "FIU",
        "Panthers",
        "",
        "FIU",
        ("Florida International", "Florida Intl", "Florida International Panthers"),
    ),
    ("Jacksonville State", "Gamecocks", "", "JVST", ("Jacksonville St", "Jax State")),
    ("Kennesaw State", "Owls", "", "KENN", ("Kennesaw St",)),
    ("Liberty", "Flames", "", "LIB", ()),
    ("Louisiana Tech", "Bulldogs", "", "LT", ("La Tech", "Louisiana-Tech")),
    (
        "Middle Tennessee",
        "Blue Raiders",
        "",
        "MTSU",
        ("Middle Tennessee State", "MTSU", "Middle Tenn State"),
    ),
    ("Missouri State", "Bears", "", "MOST", ("Missouri St",)),
    ("New Mexico State", "Aggies", "", "NMSU", ("New Mexico St", "NMSU")),
    ("Sam Houston", "Bearkats", "", "SHSU", ("Sam Houston State", "Sam Houston St")),
    ("UTEP", "Miners", "", "UTEP", ("Texas-El Paso", "Texas El Paso", "UT El Paso")),
    ("Western Kentucky", "Hilltoppers", "", "WKU", ("WKU",)),
    # Mid-American Conference
    ("Akron", "Zips", "", "AKR", ()),
    ("Ball State", "Cardinals", "", "BALL", ("Ball St",)),
    ("Bowling Green", "Falcons", "", "BGSU", ("Bowling Green State", "BGSU")),
    ("Buffalo", "Bulls", "", "BUFF", ()),
    ("Central Michigan", "Chippewas", "", "CMU", ("CMU",)),
    ("Eastern Michigan", "Eagles", "", "EMU", ("EMU",)),
    ("Kent State", "Golden Flashes", "", "KENT", ("Kent St", "Kent")),
    (
        "Miami (OH)",
        "RedHawks",
        "ncaaf:miami-oh",
        "M-OH",
        (
            "Miami OH",
            "Miami Ohio",
            "Miami-Ohio",
            "Miami of Ohio",
            "Miami Univ",
            "Miami (OH) RedHawks",
            "Miami OH RedHawks",
            "Miami Ohio RedHawks",
        ),
    ),
    ("Northern Illinois", "Huskies", "", "NIU", ("NIU",)),
    ("Ohio", "Bobcats", "", "OHIO", ()),
    ("Toledo", "Rockets", "", "TOL", ()),
    ("UMass", "Minutemen", "", "MASS", ("Massachusetts", "Massachusetts Minutemen")),
    ("Western Michigan", "Broncos", "", "WMU", ("WMU",)),
    # Mountain West
    ("Air Force", "Falcons", "", "AFA", ()),
    ("Boise State", "Broncos", "", "BSU", ("Boise St",)),
    ("Colorado State", "Rams", "", "CSU", ("Colorado St", "CSU")),
    ("Fresno State", "Bulldogs", "", "FRES", ("Fresno St",)),
    (
        "Hawai'i",
        "Rainbow Warriors",
        "ncaaf:hawaii",
        "HAW",
        ("Hawaii", "Hawaii Rainbow Warriors", "Hawaii Warriors", "Hawai'i Warriors"),
    ),
    ("Nevada", "Wolf Pack", "", "NEV", ()),
    ("New Mexico", "Lobos", "", "UNM", ()),
    ("San Diego State", "Aztecs", "", "SDSU", ("San Diego St",)),
    (
        "San Jose State",
        "Spartans",
        "",
        "SJSU",
        ("San Jose St", "San Jose State Spartans", "SJSU"),
    ),
    ("UNLV", "Rebels", "", "UNLV", ("Nevada-Las Vegas", "Nevada Las Vegas")),
    ("Utah State", "Aggies", "", "USU", ("Utah St",)),
    ("Wyoming", "Cowboys", "", "WYO", ()),
    # Sun Belt
    (
        "Appalachian State",
        "Mountaineers",
        "",
        "APP",
        ("App State", "Appalachian St", "App St", "App State Mountaineers"),
    ),
    ("Arkansas State", "Red Wolves", "", "ARST", ("Arkansas St", "A-State")),
    ("Coastal Carolina", "Chanticleers", "", "CCU", ("Coastal",)),
    ("Georgia Southern", "Eagles", "", "GASO", ()),
    ("Georgia State", "Panthers", "", "GAST", ("Georgia St",)),
    ("James Madison", "Dukes", "", "JMU", ("JMU",)),
    (
        "Louisiana",
        "Ragin' Cajuns",
        "",
        "UL",
        (
            "Louisiana-Lafayette",
            "Louisiana Lafayette",
            "UL Lafayette",
            "Louisiana Ragin Cajuns",
            "Louisiana-Lafayette Ragin' Cajuns",
            "ULL",
        ),
    ),
    (
        "Louisiana Monroe",
        "Warhawks",
        "",
        "ULM",
        ("Louisiana-Monroe", "UL Monroe", "UL-Monroe", "ULM", "Louisiana-Monroe Warhawks"),
    ),
    ("Marshall", "Thundering Herd", "", "MRSH", ()),
    ("Old Dominion", "Monarchs", "", "ODU", ("ODU",)),
    ("South Alabama", "Jaguars", "", "USA", ()),
    (
        "Southern Miss",
        "Golden Eagles",
        "",
        "USM",
        ("Southern Mississippi", "Southern Mississippi Golden Eagles", "USM"),
    ),
    ("Texas State", "Bobcats", "", "TXST", ("Texas St",)),
    ("Troy", "Trojans", "", "TROY", ()),
    # Independents
    ("Notre Dame", "Fighting Irish", "", "ND", ()),
    ("UConn", "Huskies", "", "CONN", ("Connecticut", "Connecticut Huskies")),
)


def _build_fbs() -> tuple[CollegeTeam, ...]:
    teams: list[CollegeTeam] = []
    for school, mascot, key_override, abbr, aliases in _FBS_RAW:
        key = key_override or f"ncaaf:{_slug(normalize_name(school))}"
        teams.append(
            CollegeTeam(
                key=key,
                school=school,
                mascot=mascot,
                display_name=f"{school} {mascot}",
                espn_abbr=abbr,
                aliases=tuple(aliases),
            )
        )
    return tuple(teams)


FBS_TEAMS: tuple[CollegeTeam, ...] = _build_fbs()
FBS_TEAMS_BY_KEY: dict[str, CollegeTeam] = {team.key: team for team in FBS_TEAMS}


# ---------------------------------------------------------------------------
# Alias tables
# ---------------------------------------------------------------------------


def _register(
    table: dict[str, str],
    ambiguous: set[str],
    alias: str,
    key: str,
) -> None:
    """Add one alias. A name claimed by two teams is dropped, not guessed."""
    normalized = normalize_name(alias)
    if not normalized:
        return
    if normalized in ambiguous:
        return
    existing = table.get(normalized)
    if existing is None:
        table[normalized] = key
    elif existing != key:
        del table[normalized]
        ambiguous.add(normalized)


def _register_abbr(table: dict[str, str], ambiguous: set[str], abbr: str, key: str) -> None:
    normalized = _norm_abbr(abbr)
    if not normalized:
        return
    if normalized in ambiguous:
        return
    existing = table.get(normalized)
    if existing is None:
        table[normalized] = key
    elif existing != key:
        del table[normalized]
        ambiguous.add(normalized)


def _build_nfl_tables() -> tuple[dict[str, str], dict[str, str]]:
    names: dict[str, str] = {}
    name_conflicts: set[str] = set()
    abbrs: dict[str, str] = {}
    abbr_conflicts: set[str] = set()
    for team in NFL_TEAMS:
        for alias in (
            team.display_name,
            team.odds_api_name,
            team.location,
            team.nickname,
            *team.aliases,
        ):
            _register(names, name_conflicts, alias, team.key)
        for abbr in (team.espn_abbr, *team.alt_abbrs):
            _register_abbr(abbrs, abbr_conflicts, abbr, team.key)
    return names, abbrs


def _build_fbs_tables() -> tuple[dict[str, str], dict[str, str]]:
    names: dict[str, str] = {}
    name_conflicts: set[str] = set()
    abbrs: dict[str, str] = {}
    abbr_conflicts: set[str] = set()
    for team in FBS_TEAMS:
        for alias in (team.school, team.display_name, *team.aliases):
            _register(names, name_conflicts, alias, team.key)
        _register_abbr(abbrs, abbr_conflicts, team.espn_abbr, team.key)
    return names, abbrs


_NFL_NAMES, _NFL_ABBRS = _build_nfl_tables()
_FBS_NAMES, _FBS_ABBRS = _build_fbs_tables()

_ALIAS_TABLES: dict[str, dict[str, str]] = {"nfl": _NFL_NAMES, "ncaaf": _FBS_NAMES}
_ABBR_TABLES: dict[str, dict[str, str]] = {"nfl": _NFL_ABBRS, "ncaaf": _FBS_ABBRS}


def _mascot_words(mascots: Sequence[str]) -> frozenset[str]:
    """Every word that appears in some mascot in this league.

    Membership here only makes a trailing word eligible for trimming off an
    unfamiliar spelling. The trim is then verified against the mascot of the
    team it actually landed on, so a word that doubles as part of a school name
    (for example "green", from Bowling Green) cannot drag a name onto the wrong
    team on its own.
    """
    words: set[str] = set()
    for mascot in mascots:
        words.update(normalize_name(mascot).split())
    return frozenset(words)


_MASCOT_TOKENS: dict[str, frozenset[str]] = {
    "nfl": _mascot_words([team.nickname for team in NFL_TEAMS]),
    "ncaaf": _mascot_words([team.mascot for team in FBS_TEAMS]),
}

# The mascot each canonical key actually answers to, used to verify a trim.
_MASCOT_WORDS_BY_KEY: dict[str, frozenset[str]] = {
    **{team.key: frozenset(normalize_name(team.nickname).split()) for team in NFL_TEAMS},
    **{team.key: frozenset(normalize_name(team.mascot).split()) for team in FBS_TEAMS},
}

# Trimming more than this is a sign the input is not a team name at all.
_MAX_MASCOT_TRIMS = 3


def _reduce_and_lookup(
    normalized: str,
    table: dict[str, str],
    mascot_tokens: frozenset[str],
) -> str | None:
    """Drop trailing mascot words one at a time, checking the table after each.

    A hit only counts when every word trimmed belongs to the mascot of the team
    that was hit. Without that check "Miami RedHawks" trims to "miami" and comes
    back as the Hurricanes, and "Michigan Spartans" comes back as Michigan.
    Those are the wrong team, which is the one outcome this module must not
    produce, so an inconsistent trim keeps going and then gives up.
    """
    tokens = normalized.split(" ")
    trimmed: set[str] = set()
    for _ in range(_MAX_MASCOT_TRIMS):
        if len(tokens) < 2 or tokens[-1] not in mascot_tokens:
            return None
        trimmed.add(tokens[-1])
        tokens = tokens[:-1]
        key = table.get(" ".join(tokens))
        if key and trimmed <= _MASCOT_WORDS_BY_KEY.get(key, frozenset()):
            return key
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _looks_like_abbreviation(name: Any) -> bool:
    """True for a short single token such as "GB", "WSH" or "M-OH"."""
    if not isinstance(name, str):
        return False
    stripped = name.strip()
    return 1 < len(stripped) <= 5 and not any(ch.isspace() for ch in stripped)


def canonical_key(
    name: str,
    league: str,
    abbreviation: str | None = None,
) -> str:
    """Return a stable canonical key such as "nfl:green-bay-packers" or "ncaaf:ohio-state".

    Deterministic. Never raises. Falls back to a slug of the normalized name
    when unknown. The abbreviation is a hint only, consulted after the name
    lookup fails, and never on its own when the name already resolved. A name
    that is itself a short single token (a caller passing "GB" or "M-OH") gets
    the same curated abbreviation lookup.
    """
    try:
        lg = _normalize_league(league)
        normalized = normalize_name(name)

        table = _ALIAS_TABLES.get(lg)
        if table is not None:
            abbr_table = _ABBR_TABLES[lg]
            if normalized:
                key = table.get(normalized)
                if key:
                    return key
                key = _reduce_and_lookup(normalized, table, _MASCOT_TOKENS[lg])
                if key:
                    return key
            if _looks_like_abbreviation(name):
                key = abbr_table.get(_norm_abbr(name))
                if key:
                    return key
            if abbreviation:
                key = abbr_table.get(_norm_abbr(abbreviation))
                if key:
                    return key

        if normalized:
            return f"{lg}:{_slug(normalized)}"
        fallback = normalize_name(abbreviation or "")
        if fallback:
            return f"{lg}:{_slug(fallback)}"
        return f"{lg}:{UNKNOWN}"
    except Exception:  # pragma: no cover, the contract says this never raises
        logger.exception("canonical_key failed for name=%r league=%r", name, league)
        return f"{UNKNOWN}:{UNKNOWN}"


def _resolve_key(key: Any) -> str:
    """Fold a key back through the alias tables so alternates compare equal."""
    if not isinstance(key, str):
        return ""
    cleaned = _WS_RE.sub(" ", key.strip().lower())
    if not cleaned:
        return ""
    if ":" not in cleaned:
        return cleaned
    lg, _, rest = cleaned.partition(":")
    lg = lg.strip()
    rest = rest.strip()
    if not rest or rest == UNKNOWN:
        return f"{lg or UNKNOWN}:{UNKNOWN}"
    return canonical_key(rest.replace("-", " "), lg)


def _is_unknown(resolved: str) -> bool:
    """True for anything that is not a confidently identified team.

    That covers an empty value, an unknown team, an unknown league, and any
    string that is not "league:team" shaped at all. A bare "miami" is not a key:
    it could be the Dolphins or the Hurricanes, so two of them must never be
    treated as the same team.
    """
    if not resolved:
        return True
    league, sep, team = resolved.partition(":")
    if not sep or not league or not team:
        return True
    return league == UNKNOWN or team == UNKNOWN


def same_team(a: str, b: str) -> bool:
    """True when two canonical keys refer to the same team.

    Unknown, empty and malformed keys never match, not even against an
    identical copy of themselves. That is what stops an unresolvable game from
    being paired with another one. A value with no league prefix, such as a
    bare "miami", is not a canonical key and is treated as unknown.
    """
    left = _resolve_key(a)
    right = _resolve_key(b)
    if _is_unknown(left) or _is_unknown(right):
        return False
    return left == right


def display_name(key: str) -> str | None:
    """Human readable name for a canonical key, or None when it is not a known team."""
    resolved = _resolve_key(key)
    team = NFL_TEAMS_BY_KEY.get(resolved)
    if team is not None:
        return team.display_name
    college = FBS_TEAMS_BY_KEY.get(resolved)
    if college is not None:
        return college.display_name
    return None


def _is_aware(value: Any) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def match_by_teams_and_date(
    home_key: str,
    away_key: str,
    kickoff: datetime,
    candidates: Sequence[MatchCandidate],
    tolerance_hours: float = 30.0,
) -> MatchCandidate | None:
    """Find the candidate whose canonical home/away keys match and whose kickoff is within
    tolerance. Returns None when the match is not confident. Also accepts a home/away swap
    only if nothing else matches, and in that case still returns None (never guess).

    Naive datetimes, unknown keys and ambiguous ties all resolve to None so the
    game lands on the admin review screen instead of getting a wrong spread.
    """
    if not candidates:
        return None
    if not _is_aware(kickoff):
        logger.debug("match_by_teams_and_date needs a timezone aware kickoff, got %r", kickoff)
        return None
    try:
        window = float(tolerance_hours) * 3600.0
    except (TypeError, ValueError):
        return None
    # NaN fails every comparison, so an unchecked NaN would wave through a
    # candidate kicking off in a different season.
    if math.isnan(window) or window < 0:
        return None

    wanted_home = _resolve_key(home_key)
    wanted_away = _resolve_key(away_key)
    if _is_unknown(wanted_home) or _is_unknown(wanted_away):
        return None
    if wanted_home == wanted_away:
        return None

    exact: list[tuple[float, int, MatchCandidate]] = []
    swapped: list[tuple[float, int, MatchCandidate]] = []

    for index, candidate in enumerate(candidates):
        candidate_kickoff = getattr(candidate, "kickoff", None)
        if not _is_aware(candidate_kickoff):
            continue
        delta = abs((candidate_kickoff - kickoff).total_seconds())
        if delta > window:
            continue
        candidate_home = _resolve_key(getattr(candidate, "home_key", ""))
        candidate_away = _resolve_key(getattr(candidate, "away_key", ""))
        if _is_unknown(candidate_home) or _is_unknown(candidate_away):
            continue
        if candidate_home == wanted_home and candidate_away == wanted_away:
            exact.append((delta, index, candidate))
        elif candidate_home == wanted_away and candidate_away == wanted_home:
            swapped.append((delta, index, candidate))

    if exact:
        exact.sort(key=lambda item: (item[0], item[1]))
        if len(exact) == 1 or exact[0][0] < exact[1][0]:
            return exact[0][2]
        logger.debug(
            "Ambiguous match for %s at %s: %d candidates tied on kickoff",
            f"{wanted_away} at {wanted_home}",
            kickoff.isoformat(),
            len(exact),
        )
        return None

    if swapped:
        logger.debug(
            "Only a home/away swap matched %s at %s. Refusing to guess.",
            f"{wanted_away} at {wanted_home}",
            kickoff.isoformat(),
        )
    return None
