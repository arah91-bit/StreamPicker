"""Holiday catalog windows. A holiday row appears the week leading up to each
holiday (plus 2 days after, so it doesn't vanish the morning of), except
Halloween (all of October) and Christmas (all of December). It is always the
FIRST catalog, its items are ranked by the user's own genre profile, and it
passes the same kid-age certification filter as everything else."""

import datetime


def _easter(year: int) -> datetime.date:
    """Anonymous Gregorian computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return datetime.date(year, month, day + 1)


def _thanksgiving(year: int) -> datetime.date:
    """4th Thursday of November."""
    d = datetime.date(year, 11, 1)
    offset = (3 - d.weekday()) % 7  # first Thursday
    return d + datetime.timedelta(days=offset + 21)


def _holidays(year: int) -> list[dict]:
    return [
        {"id": "new-years", "name": "New Year's Picks",
         "date": datetime.date(year, 1, 1),
         "keywords": ["new year's eve", "new year"]},
        {"id": "valentines", "name": "Valentine's Day Picks",
         "date": datetime.date(year, 2, 14),
         "keywords": ["valentine's day", "romantic comedy"]},
        {"id": "st-patricks", "name": "St. Patrick's Day Picks",
         "date": datetime.date(year, 3, 17),
         "keywords": ["st. patrick's day", "irish", "ireland"]},
        {"id": "easter", "name": "Easter Picks",
         "date": _easter(year),
         "keywords": ["easter", "easter bunny"]},
        {"id": "july4", "name": "4th of July Picks",
         "date": datetime.date(year, 7, 4),
         "keywords": ["fourth of july", "independence day", "patriotism", "americana"]},
        {"id": "halloween", "name": "Halloween Picks",
         "date": datetime.date(year, 10, 31),
         "window": (datetime.date(year, 10, 1), datetime.date(year, 10, 31)),
         "keywords": ["halloween", "haunted house", "trick or treat"]},
        {"id": "thanksgiving", "name": "Thanksgiving Picks",
         "date": _thanksgiving(year),
         "keywords": ["thanksgiving"]},
        {"id": "christmas", "name": "Christmas Picks",
         "date": datetime.date(year, 12, 25),
         "window": (datetime.date(year, 12, 1), datetime.date(year, 12, 31)),
         "keywords": ["christmas", "santa claus", "christmas eve"]},
    ]


def active_holiday(today: datetime.date | None = None) -> dict | None:
    today = today or datetime.date.today()
    for year in (today.year - 1, today.year, today.year + 1):
        for h in _holidays(year):
            start, end = h.get("window") or (
                h["date"] - datetime.timedelta(days=7),
                h["date"] + datetime.timedelta(days=2),
            )
            if start <= today <= end:
                return h
    return None
