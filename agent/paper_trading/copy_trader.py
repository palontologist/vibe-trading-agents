"""
Copy Trader — fetches Polymarket user activity via Data API.
Port of TypeScript copy trader logic.
"""

import os
from dataclasses import dataclass
from typing import List, Optional, Union

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

POL_DATA_API = os.environ.get("POL_DATA_API", "https://data-api.polymarket.com")
ACTIVITY_DEFAULT_LIMIT = 100
ACTIVITY_MAX_LIMIT = 500

@dataclass
class Activity:
    proxyWallet: Optional[str] = None
    timestamp: int = 0
    conditionId: Optional[str] = None
    type: str = ""
    size: Optional[float] = None
    usdcSize: Optional[float] = None
    transactionHash: Optional[str] = None
    price: Optional[float] = None
    asset: Optional[str] = None
    side: Optional[str] = None
    outcomeIndex: Optional[int] = None
    title: Optional[str] = None
    slug: Optional[str] = None
    eventSlug: Optional[str] = None
    outcome: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Activity":
        return cls(
            proxyWallet=d.get("proxyWallet"),
            timestamp=d.get("timestamp", 0),
            conditionId=d.get("conditionId"),
            type=d.get("type", ""),
            size=d.get("size"),
            usdcSize=d.get("usdcSize"),
            transactionHash=d.get("transactionHash"),
            price=d.get("price"),
            asset=d.get("asset"),
            side=d.get("side"),
            outcomeIndex=d.get("outcomeIndex"),
            title=d.get("title"),
            slug=d.get("slug"),
            eventSlug=d.get("eventSlug"),
            outcome=d.get("outcome"),
        )

def build_activity_url(base: str, params: dict) -> str:
    from urllib.parse import urlencode, urljoin
    u = urljoin(base, "/activity")
    query = {"user": params["user"]}
    query["limit"] = str(min(ACTIVITY_MAX_LIMIT, params.get("limit", ACTIVITY_DEFAULT_LIMIT)))
    query["offset"] = str(max(0, params.get("offset", 0)))
    if params.get("type"):
        t = params["type"]
        if isinstance(t, list):
            query["type"] = ",".join(t)
        else:
            query["type"] = t
    if params.get("sortBy"):
        query["sortBy"] = params["sortBy"]
    if params.get("sortDirection"):
        query["sortDirection"] = params["sortDirection"]
    return f"{u}?{urlencode(query)}"

def get_activity(user: str, limit: int = 100, offset: int = 0, 
                 activity_type: Optional[Union[str, List[str]]] = None,
                 sort_by: str = "TIMESTAMP", sort_direction: str = "DESC") -> List[Activity]:
    params = {
        "user": user,
        "limit": limit,
        "offset": offset,
        "sortBy": sort_by,
        "sortDirection": sort_direction,
    }
    if activity_type:
        params["type"] = activity_type
    url = build_activity_url(POL_DATA_API, params)
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [Activity.from_dict(d) for d in data] if isinstance(data, list) else []

def trade_event_key(a: Activity) -> str:
    tx = a.transactionHash or ""
    asset = a.asset or ""
    side = a.side or ""
    ts = a.timestamp or 0
    if tx:
        return f"{tx}:{asset}:{side}"
    return f":{ts}:{asset}:{side}"

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python copy_trader.py <user_address> [limit]")
        sys.exit(1)
    user = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    activities = get_activity(user, limit=limit)
    for a in activities:
        print(f"[{a.timestamp}] {a.type} | {a.asset or '?'} | {a.side or '?'} | ${a.usdcSize or 0:.2f} | {a.outcome or '?'} | {a.title or ''[:50]}")