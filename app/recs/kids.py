"""Kid-profile age handling. Ages are anchored to a birthdate computed when
the age is set, so they advance in real time; at 18 the filter turns off."""

import datetime

# The developmental bands the recommender actually scores against, ascending;
# `max_age` is inclusive and the last band catches anything above it. The
# boundaries live here rather than in tmdb because the catalog builder labels
# its age slider from this same table: a page that names a band the ranker
# does not use is a lie an operator has no way to catch. `effect` says what
# moving into the band changes, and is the operator-facing half of
# tmdb.kid_age_appeal plus tmdb.cert_allowed.
AGE_BANDS = (
    {
        "id": "preschool",
        "max_age": 5,
        "label": "Preschool",
        "effect": "G, U and TV-Y only — plus PG animation and family films, "
                  "because Frozen and Toy Story are rated PG. Kids, Family "
                  "and Animation rank highest; adult-skewing genres and "
                  "pre-1990 titles without a child anchor are pushed down.",
    },
    {
        "id": "early-childhood",
        "max_age": 8,
        "label": "Early childhood",
        "effect": "PG and TV-PG open up at 6, TV-Y7 at 7. Kids, Family and "
                  "Animation still rank highest, with adventure, comedy, "
                  "fantasy and music close behind. From 7, explicit "
                  "preschool framing starts being demoted.",
    },
    {
        "id": "school-age",
        "max_age": 12,
        "label": "School age",
        "effect": "Certificates stay at PG and under until 13. Action, "
                  "adventure, mystery and science fiction rank up, a general "
                  "Kids label counts for much less, and preschool framing is "
                  "pushed well down.",
    },
    {
        "id": "teen",
        "max_age": 17,
        "label": "Teen",
        "effect": "PG-13 opens up at 13 and TV-14 at 14 (R and TV-MA only at "
                  "17). Coming-of-age and teen stories rank up; a general "
                  "Kids label is demoted.",
    },
)

MIN_AGE = 2
MAX_AGE = AGE_BANDS[-1]["max_age"]
# Used when kid mode is switched on without an age being chosen.
DEFAULT_AGE = 8


def band_for_age(kid_age: int | None) -> dict | None:
    """The band an age falls in, or None for a viewer with no kid age."""
    if kid_age is None:
        return None
    for band in AGE_BANDS:
        if kid_age <= band["max_age"]:
            return band
    return AGE_BANDS[-1]


def clamp_age(value: int | None) -> int:
    """Pull a requested age into the range the profile filters support."""
    if value is None:
        return DEFAULT_AGE
    return max(MIN_AGE, min(MAX_AGE, value))


def birthdate_from_age(age: int) -> str:
    today = datetime.date.today()
    try:
        born = today.replace(year=today.year - age)
    except ValueError:  # Feb 29
        born = today.replace(year=today.year - age, day=28)
    return born.isoformat()


def effective_kid_age(user: dict) -> int | None:
    """Current age for an active kid profile, None if the user is not a kid
    (or has aged out at 18+)."""
    if not user.get("is_kid"):
        return None
    birthdate = user.get("kid_birthdate")
    if not birthdate:
        return user.get("kid_age")
    b = datetime.date.fromisoformat(birthdate)
    t = datetime.date.today()
    age = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    return age if age < 18 else None
