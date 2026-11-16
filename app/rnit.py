import json
import re
from datetime import date
from decimal import Decimal
from http.client import HTTPException
from urllib.parse import urlencode
from urllib.request import Request, urlopen


HISTORY_URL = "https://www.rnit.rw/fileadmin/rnit/nav-public.php"
RECENT_NAV_LIMIT = 7


class RNITHistoryUnavailable(Exception):
    pass


def _fetch_history(year, today):
    query = urlencode({"history": "1", "year": year})
    request = Request(
        f"{HISTORY_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "PlotsAndProsper/1.0"},
    )
    try:
        with urlopen(request, timeout=4) as response:
            payload = json.load(response)

        entries = payload["entries"]
        if payload["ok"] is not True or not isinstance(entries, list):
            raise ValueError("Invalid RNIT NAV history response.")

        navs = {}
        for entry in entries:
            nav_date = date.fromisoformat(entry["nav_date"])
            value = entry["nav_value"]
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) or Decimal(value) <= 0:
                raise ValueError("Invalid RNIT NAV value.")
            if nav_date.year == year and nav_date <= today:
                navs[nav_date] = value
    except (OSError, HTTPException, KeyError, TypeError, ValueError) as error:
        raise RNITHistoryUnavailable from error

    return [
        {"as_of_date": nav_date.isoformat(), "nav_per_unit": navs[nav_date]}
        for nav_date in sorted(navs, reverse=True)[:RECENT_NAV_LIMIT]
    ]


def list_recent_published_navs(today):
    navs = _fetch_history(today.year, today)
    missing = RECENT_NAV_LIMIT - len(navs)
    if missing:
        navs.extend(_fetch_history(today.year - 1, today)[:missing])
    return navs
