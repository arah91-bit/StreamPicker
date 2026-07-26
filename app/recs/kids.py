"""Kid-profile age handling. Ages are anchored to a birthdate computed when
the age is set, so they advance in real time; at 18 the filter turns off."""

import datetime


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
