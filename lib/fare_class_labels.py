"""
Friendly names for Southwest's fare product ids, confirmed against a live search response.

Separate module (rather than living in fare_checker.py or fare_watch.py) because both of those
need it and fare_watch.py already imports from fare_checker.py -- putting it in either one would
create a circular import the moment the other started using it.
"""

from __future__ import annotations

# Southwest's four current tiers, cheapest to most expensive: Basic, Choice, Choice Preferred,
# Choice Extra. Best-effort only: anything not listed here renders as its raw id, so an
# unrecognized id shows up as an ugly label rather than a fare class silently disappearing.
FARE_CLASS_LABELS = {
    "WGARED": "Basic",
    "PLURED": "Choice",
    "ANYRED": "Choice Preferred",
    "BUSRED": "Choice Extra",
}

# The one-line pitch Southwest itself uses for each tier. Same fallback rule as
# fare_class_label: an unrecognized id just gets no tagline.
FARE_CLASS_TAGLINES = {
    "WGARED": "Go for Less — seat assigned at check-in",
    "PLURED": "Top Pick — standard seat included",
    "ANYRED": "Earlier Access — preferred seat included",
    "BUSRED": "All In — extra legroom seat included",
}


def fare_class_label(fare_type: str) -> str:
    """The display name for a fare product id, falling back to the id itself."""
    return FARE_CLASS_LABELS.get(fare_type.upper(), fare_type)


def fare_class_tagline(fare_type: str) -> str:
    """The one-line description for a fare product id, or '' when there isn't one."""
    return FARE_CLASS_TAGLINES.get(fare_type.upper(), "")
