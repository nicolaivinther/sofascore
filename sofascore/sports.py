"""The sports this collector handles.

``code`` is the value of every ``sofascore_*.sport`` column and must match the realgm_db sport
registry (``realgm_db/src/config/sports.py``), which reads these tables later.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Sport:
    """Constants for one sport."""

    code: str
    # Sofascore's slug: ``event.tournament.category.sport.slug``, ``/sport/<slug>/...``.
    sofascore_sport: str
    # The fast run first asks for a game's result this long after its start.
    result_after_minutes: int


BASKETBALL = Sport(code="basketball", sofascore_sport="basketball", result_after_minutes=100)
# Three 20-minute periods plus stoppages, overtime and a shootout run longer.
HOCKEY = Sport(code="hockey", sofascore_sport="ice-hockey", result_after_minutes=130)

SPORTS: tuple[Sport, ...] = (BASKETBALL, HOCKEY)
SPORT_CODES: tuple[str, ...] = tuple(s.code for s in SPORTS)


def get_sport(code: str) -> Sport:
    """The entry for ``code``; raises ``ValueError`` for an unknown sport."""
    for sport in SPORTS:
        if sport.code == code:
            return sport
    raise ValueError(f"unknown sport {code!r} (known: {', '.join(SPORT_CODES)})")
