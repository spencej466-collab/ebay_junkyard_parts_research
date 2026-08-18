import base64
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd
import requests
import streamlit as st
import truststore

# Use the Windows/system certificate store where available.
truststore.inject_into_ssl()

DB_PATH = "ebay_research.db"
SANDBOX = False
MARKETPLACE_ID = "EBAY_US"
API_ROOT = "https://api.sandbox.ebay.com" if SANDBOX else "https://api.ebay.com"
TOKEN_URL = f"{API_ROOT}/identity/v1/oauth2/token"
SEARCH_URL = f"{API_ROOT}/buy/browse/v1/item_summary/search"
SCOPES = "https://api.ebay.com/oauth/api_scope"
PAGE_SIZE = 200
MAX_LISTINGS = 1000
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 2
INITIAL_RETRY_DELAY = 1.0

# Words commonly found in automotive titles but not useful for identifying the
# underlying part. Used only for the title-based fallback grouping key.
TITLE_STOP_WORDS = {
    "OEM", "OE", "GENUINE", "GEN", "USED", "PREOWNED", "PRE-OWNED",
    "GOOD", "WORKING", "TESTED", "TEST", "COMPLETE", "ASSEMBLY",
    "ASSY", "SET", "PAIR", "LH", "RH", "LEFT", "RIGHT", "DRIVER",
    "PASSENGER", "FRONT", "REAR", "BLACK", "GRAY", "GREY", "BEIGE",
    "TAN", "CREAM", "WHITE", "SILVER", "CHROME", "CLEAR", "AUTO",
    "AUTOMATIC", "POWER", "MANUAL", "ELECTRIC", "FOR", "TOYOTA",
    "HONDA", "FORD", "CHEVROLET", "CHEVY", "NISSAN", "HYUNDAI",
    "KIA", "DODGE", "RAM", "JEEP", "SUBARU", "MAZDA", "LEXUS",
    "ACURA", "VOLKSWAGEN", "VW", "AUDI", "BMW", "MERCEDES", "BENZ",
    "INFINITI", "CADILLAC", "BUICK", "GMC", "LINCOLN", "VOLVO",
}

PART_NUMBER_ASPECT_NAMES = {
    "mpn", "manufacturer part number", "mfr part number", "part number",
    "part no", "part #", "mpn number", "manufacturer number",
}

OEM_ASPECT_NAMES = {
    "oem part number", "oe part number", "original equipment part number",
    "oem number", "oe number", "original part number", "oem",
}

INTERCHANGE_ASPECT_NAMES = {
    "interchange part number", "interchange number", "interchange",
    "cross reference", "cross-reference", "cross reference number",
    "superseded part number", "supersedes", "replacement part number",
    "alternate part number", "alternative part number",
}

IDENTIFIER_ASPECT_NAMES = (
    PART_NUMBER_ASPECT_NAMES
    | OEM_ASPECT_NAMES
    | INTERCHANGE_ASPECT_NAMES
    | {"gtin", "upc", "ean", "epid", "ebay product id"}
)


def get_secret(name: str) -> str:
    """Read a secret from Streamlit secrets first, then environment variables."""
    try:
        value = st.secrets.get(name)
        if value:
            return str(value).strip()
    except Exception:
        pass
    return os.getenv(name, "").strip()


def get_credentials() -> Tuple[str, str]:
    client_id = get_secret("EBAY_CLIENT_ID")
    client_secret = get_secret("EBAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "eBay credentials are not configured. Set EBAY_CLIENT_ID and "
            "EBAY_CLIENT_SECRET in Streamlit secrets or environment variables."
        )
    return client_id, client_secret


def format_error_response(response: requests.Response) -> str:
    try:
        payload = response.json()
        errors = payload.get("errors")
        if errors:
            messages = []
            for error in errors:
                parts = [
                    str(x).strip()
                    for x in (
                        error.get("errorId", ""),
                        error.get("message", ""),
                        error.get("longMessage", ""),
                    )
                    if x
                ]
                if parts:
                    messages.append(" - ".join(parts))
            if messages:
                return f"HTTP {response.status_code}: " + " | ".join(messages)
        if "message" in payload:
            return f"HTTP {response.status_code}: {payload['message']}"
        return f"HTTP {response.status_code}: {json.dumps(payload)}"
    except Exception:
        text = response.text.strip()
        return f"HTTP {response.status_code}: {text[:1000]}" if text else f"HTTP {response.status_code}"


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> requests.Response:
    last_response: Optional[requests.Response] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                data=data,
                timeout=timeout,
            )
        except requests.RequestException:
            if attempt >= MAX_RETRIES:
                raise
            time.sleep(INITIAL_RETRY_DELAY * (2 ** attempt))
            continue

        last_response = response
        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response
        if attempt >= MAX_RETRIES:
            return response

        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = INITIAL_RETRY_DELAY * (2 ** attempt)
        time.sleep(delay)

    if last_response is not None:
        return last_response
    raise RuntimeError("HTTP request failed without receiving a response.")


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text TEXT NOT NULL,
            marketplace_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS active_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_id INTEGER NOT NULL,
            item_id TEXT,
            title TEXT,
            price REAL,
            shipping_cost REAL,
            total_price REAL,
            currency TEXT,
            condition_text TEXT,
            item_location TEXT,
            buying_options TEXT,
            item_web_url TEXT,
            seller_username TEXT,
            brand TEXT,
            mpn TEXT,
            epid TEXT,
            gtin TEXT,
            part_number TEXT,
            interchange_numbers TEXT,
            group_key TEXT,
            group_method TEXT,
            duplicate_key TEXT,
            last_seen TEXT NOT NULL,
            raw_json TEXT,
            FOREIGN KEY(search_id) REFERENCES searches(id)
        )
        """
    )

    cur.execute("PRAGMA table_info(active_items)")
    existing = {row[1] for row in cur.fetchall()}
    migrations = {
        "shipping_cost": "ALTER TABLE active_items ADD COLUMN shipping_cost REAL",
        "total_price": "ALTER TABLE active_items ADD COLUMN total_price REAL",
        "item_location": "ALTER TABLE active_items ADD COLUMN item_location TEXT",
        "buying_options": "ALTER TABLE active_items ADD COLUMN buying_options TEXT",
        "brand": "ALTER TABLE active_items ADD COLUMN brand TEXT",
        "mpn": "ALTER TABLE active_items ADD COLUMN mpn TEXT",
        "epid": "ALTER TABLE active_items ADD COLUMN epid TEXT",
        "gtin": "ALTER TABLE active_items ADD COLUMN gtin TEXT",
        "part_number": "ALTER TABLE active_items ADD COLUMN part_number TEXT",
        "interchange_numbers": "ALTER TABLE active_items ADD COLUMN interchange_numbers TEXT",
        "group_key": "ALTER TABLE active_items ADD COLUMN group_key TEXT",
        "group_method": "ALTER TABLE active_items ADD COLUMN group_method TEXT",
        "duplicate_key": "ALTER TABLE active_items ADD COLUMN duplicate_key TEXT",
    }
    for column, sql in migrations.items():
        if column not in existing:
            cur.execute(sql)

    conn.commit()
    conn.close()


@st.cache_data(ttl=3500, show_spinner=False)
def get_app_token() -> str:
    client_id, client_secret = get_credentials()
    auth = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("utf-8")
    response = request_with_retry(
        "POST",
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": SCOPES},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            "Unable to obtain an eBay application access token.\n\n"
            + format_error_response(response)
        )
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("eBay returned no access_token.")
    return token


def build_search_query(year: str, make: str, model: str, part: str) -> str:
    return " ".join(
        piece.strip()
        for piece in (year, make, model, part)
        if piece and piece.strip()
    )


def search_active_listings(
    query: str,
    max_listings: int = MAX_LISTINGS,
    used_only: bool = True,
) -> Dict[str, Any]:
    token = get_app_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
    }

    all_items: List[Dict[str, Any]] = []
    next_url: Optional[str] = SEARCH_URL
    params: Optional[Dict[str, Any]] = {
        "q": query,
        "limit": min(PAGE_SIZE, max_listings),
        "offset": 0,
    }
    if used_only:
        params["filter"] = "conditionIds:{3000}"

    total_available: Optional[int] = None

    while next_url and len(all_items) < max_listings:
        response = request_with_retry(
            "GET",
            next_url,
            headers=headers,
            params=params,
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                "eBay Browse API request failed.\n\n"
                + format_error_response(response)
            )
        payload = response.json()
        if total_available is None:
            try:
                total_available = int(payload.get("total", 0) or 0)
            except (TypeError, ValueError):
                total_available = 0

        page_items = payload.get("itemSummaries", []) or []
        if not page_items:
            break

        remaining = max_listings - len(all_items)
        all_items.extend(page_items[:remaining])
        next_url = payload.get("next")
        params = None

    return {
        "itemSummaries": all_items,
        "total": total_available or len(all_items),
    }


def normalize_text(value: Any) -> str:
    text = str(value or "").upper().strip()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_identifier(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def split_identifier_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_values: List[str] = []
        for item in value:
            raw_values.extend(split_identifier_values(item))
        return raw_values
    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"[,;/|]\s*|\s{2,}", text)
    return [part.strip() for part in parts if part.strip()]


def aspect_dict(item: Dict[str, Any]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = defaultdict(list)
    aspects = item.get("localizedAspects") or []
    for aspect in aspects:
        if not isinstance(aspect, dict):
            continue
        name = normalize_text(aspect.get("name") or aspect.get("localizedName"))
        values = aspect.get("value")
        if values is None:
            values = aspect.get("localizedValue", "")
        if isinstance(values, list):
            result[name].extend(str(v) for v in values if v is not None)
        elif values is not None:
            result[name].append(str(values))
    return result


def find_aspect_values(aspects: Dict[str, List[str]], names: Set[str]) -> List[str]:
    values: List[str] = []
    normalized_names = {normalize_text(name) for name in names}
    for name, vals in aspects.items():
        if name in normalized_names:
            values.extend(vals)
    return values


def extract_title_identifier_values(title: str, keywords: Sequence[str]) -> List[str]:
    text = str(title or "")
    results: List[str] = []
    escaped_keywords = "|".join(re.escape(k) for k in keywords)
    pattern = re.compile(
        rf"(?:{escaped_keywords})\s*(?:PART\s*)?(?:NUMBER|NO\.?|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-./ ]{{2,24}})",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        candidate = normalize_identifier(match.group(1))
        if 3 <= len(candidate) <= 24:
            results.append(candidate)
    return sorted(set(results))


def extract_part_identifiers(item: Dict[str, Any]) -> Dict[str, Any]:
    title = item.get("title") or ""
    aspects = aspect_dict(item)

    brand = str(item.get("brand") or "").strip()
    mpn = str(item.get("mpn") or "").strip()
    epid = str(item.get("epid") or "").strip()
    gtin = str(item.get("gtin") or "").strip()

    if not brand:
        brand_values = find_aspect_values(aspects, {"brand"})
        brand = brand_values[0].strip() if brand_values else ""

    if not mpn:
        mpn_values = find_aspect_values(aspects, PART_NUMBER_ASPECT_NAMES)
        mpn = mpn_values[0].strip() if mpn_values else ""

    oem_values = find_aspect_values(aspects, OEM_ASPECT_NAMES)
    interchange_values = find_aspect_values(aspects, INTERCHANGE_ASPECT_NAMES)

    if not oem_values:
        oem_values = extract_title_identifier_values(
            title,
            ["OEM", "OE", "OEM PART", "OE PART"],
        )

    if not interchange_values:
        interchange_values = extract_title_identifier_values(
            title,
            ["INTERCHANGE", "CROSS REFERENCE", "CROSS-REFERENCE", "SUPERCEDES", "SUPERSEDES"],
        )

    part_number = normalize_identifier(mpn)
    oem_numbers = sorted({normalize_identifier(v) for v in oem_values if normalize_identifier(v)})
    interchange_numbers = sorted({normalize_identifier(v) for v in interchange_values if normalize_identifier(v)})

    explicit_identifier_set: Set[str] = set()
    if epid:
        explicit_identifier_set.add(f"EPID:{normalize_identifier(epid)}")
    if gtin:
        explicit_identifier_set.add(f"GTIN:{normalize_identifier(gtin)}")
    if part_number:
        explicit_identifier_set.add(f"MPN:{normalize_identifier(part_number)}")
    for number in oem_numbers:
        explicit_identifier_set.add(f"OEM:{number}")
    for number in interchange_numbers:
        explicit_identifier_set.add(f"XREF:{number}")

    return {
        "brand": brand,
        "mpn": mpn,
        "epid": epid,
        "gtin": gtin,
        "part_number": part_number,
        "oem_numbers": oem_numbers,
        "interchange_numbers": interchange_numbers,
        "identifier_set": explicit_identifier_set,
    }


def extract_shipping_cost(item: Dict[str, Any]) -> float:
    options = item.get("shippingOptions") or []
    if not options:
        return 0.0
    shipping_cost = (options[0] or {}).get("shippingCost") or {}
    try:
        return float(shipping_cost.get("value", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def extract_location(item: Dict[str, Any]) -> str:
    location = item.get("itemLocation") or {}
    pieces = [
        str(location.get("city", "")).strip(),
        str(location.get("stateOrProvince", "")).strip(),
        str(location.get("postalCode", "")).strip(),
    ]
    return ", ".join(value for value in pieces if value)


def title_fingerprint(title: str, year: str, make: str, model: str) -> str:
    tokens = normalize_text(title).split()
    vehicle_tokens = set(normalize_text(" ".join((year, make, model))).split())
    filtered = []
    for token in tokens:
        if token in vehicle_tokens:
            continue
        if token in TITLE_STOP_WORDS:
            continue
        if len(token) <= 1:
            continue
        filtered.append(token)
    # A sorted fingerprint catches modest seller-title reordering while keeping
    # the number of title-only groups manageable.
    return " ".join(sorted(set(filtered))[:14])


def build_duplicate_key(row: Dict[str, Any]) -> str:
    seller = normalize_text(row.get("seller_username"))
    title = normalize_text(row.get("title"))
    price = f"{float(row.get('price', 0) or 0):.2f}"
    shipping = f"{float(row.get('shipping_cost', 0) or 0):.2f}"
    identifiers = "|".join(sorted(row.get("identifier_set", set())))
    return "::".join((seller, title, price, shipping, identifiers))


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a == root_b:
            return
        if self.rank[root_a] < self.rank[root_b]:
            root_a, root_b = root_b, root_a
        self.parent[root_b] = root_a
        if self.rank[root_a] == self.rank[root_b]:
            self.rank[root_a] += 1


def group_active_listings(
    rows: List[Dict[str, Any]],
    year: str,
    make: str,
    model: str,
) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    working = pd.DataFrame(rows).copy()

    # Make sure identifier metadata exists even if a future API response omits
    # one or more fields.
    metadata: List[Dict[str, Any]] = []
    for row in rows:
        metadata.append(extract_part_identifiers(row.get("raw_item", {}) or {}))

    working["brand"] = [m["brand"] for m in metadata]
    working["mpn"] = [m["mpn"] for m in metadata]
    working["epid"] = [m["epid"] for m in metadata]
    working["gtin"] = [m["gtin"] for m in metadata]
    working["part_number"] = [m["part_number"] for m in metadata]
    working["oem_numbers"] = [m["oem_numbers"] for m in metadata]
    working["interchange_numbers_list"] = [m["interchange_numbers"] for m in metadata]
    working["identifier_set"] = [m["identifier_set"] for m in metadata]

    working["title_fingerprint"] = [
        title_fingerprint(str(row.get("title", "")), year, make, model)
        for row in rows
    ]
    working["duplicate_key"] = [
        build_duplicate_key({**row, "identifier_set": metadata[idx]["identifier_set"]})
        for idx, row in enumerate(rows)
    ]

    # Exact duplicate detection: same seller + title + price + shipping and
    # same known identifiers. Keep one representative listing per exact dup.
    working["duplicate_rank"] = working.groupby("duplicate_key").cumcount()
    working["duplicate_count"] = working.groupby("duplicate_key")["item_id"].transform("count")

    # Union listings into groups when they share any strong identifier.
    uf = UnionFind(len(working))
    identifier_owner: Dict[str, int] = {}
    for idx, identifiers in enumerate(working["identifier_set"]):
        for identifier in identifiers:
            if identifier in identifier_owner:
                uf.union(idx, identifier_owner[identifier])
            else:
                identifier_owner[identifier] = idx

    # Title-only grouping is only used where an item has no strong identifier.
    # This is deliberately conservative to reduce false merges.
    fingerprint_owner: Dict[str, int] = {}
    for idx, fingerprint in enumerate(working["title_fingerprint"]):
        if not fingerprint or working.at[idx, "identifier_set"]:
            continue
        if fingerprint in fingerprint_owner:
            uf.union(idx, fingerprint_owner[fingerprint])
        else:
            fingerprint_owner[fingerprint] = idx

    roots = [uf.find(i) for i in range(len(working))]
    root_to_group: Dict[int, str] = {}
    for idx, root in enumerate(roots):
        if root not in root_to_group:
            root_to_group[root] = f"P{len(root_to_group) + 1:04d}"

    working["group_id"] = [root_to_group[root] for root in roots]

    def group_method_for(identifier_set: Set[str], fingerprint: str) -> str:
        if any(x.startswith("EPID:") for x in identifier_set):
            return "ePID"
        if any(x.startswith("MPN:") for x in identifier_set):
            return "Brand + MPN"
        if any(x.startswith("OEM:") for x in identifier_set):
            return "OEM part number"
        if any(x.startswith("XREF:") for x in identifier_set):
            return "Interchange number"
        if any(x.startswith("GTIN:") for x in identifier_set):
            return "GTIN"
        if fingerprint:
            return "Title fingerprint"
        return "Unresolved"

    working["group_method"] = [
        group_method_for(set(identifiers), fingerprint)
        for identifiers, fingerprint in zip(
            working["identifier_set"],
            working["title_fingerprint"],
        )
    ]

    def id_display(row: pd.Series) -> str:
        values = []
        if row["mpn"]:
            values.append(f"MPN: {row['mpn']}")
        for number in row["oem_numbers"]:
            values.append(f"OEM: {number}")
        for number in row["interchange_numbers_list"]:
            values.append(f"XREF: {number}")
        if row["epid"]:
            values.append(f"ePID: {row['epid']}")
        if row["gtin"]:
            values.append(f"GTIN: {row['gtin']}")
        return "; ".join(dict.fromkeys(values))

    working["identifier_display"] = working.apply(id_display, axis=1)

    # Group label: use a cleaned representative title, but prefer explicit
    # identifiers when available.
    group_label_map: Dict[str, str] = {}
    for group_id, group in working.groupby("group_id", sort=False):
        identifier_rows = group[group["identifier_display"] != ""]
        if not identifier_rows.empty:
            best = identifier_rows.iloc[0]
            label = str(best["title"])
        else:
            label = str(group.iloc[0]["title"])
        group_label_map[group_id] = label

    working["group_label"] = working["group_id"].map(group_label_map)

    # Representative listing after exact de-duplication.
    unique_working = working[working["duplicate_rank"] == 0].copy()
    unique_working["price"] = pd.to_numeric(unique_working["price"], errors="coerce")
    unique_working["total_price"] = pd.to_numeric(unique_working["total_price"], errors="coerce")

    group_rows: List[Dict[str, Any]] = []
    for group_id, group in unique_working.groupby("group_id", sort=False):
        prices = group["price"].dropna()
        total_prices = group["total_price"].dropna()
        sellers = group["seller_username"].dropna().astype(str).str.strip()
        identifier_values = [
            value
            for value in group["identifier_display"].tolist()
            if value
        ]
        representative = group.iloc[0]

        group_rows.append(
            {
                "group_id": group_id,
                "part_group": representative["group_label"],
                "listings": int(len(group)),
                "duplicates_removed": int(group["duplicate_count"].sum() - len(group)),
                "unique_sellers": int(sellers.nunique()),
                "group_method": representative["group_method"],
                "part_identifiers": "; ".join(dict.fromkeys(identifier_values)),
                "median_price": float(prices.median()) if not prices.empty else None,
                "average_price": float(prices.mean()) if not prices.empty else None,
                "min_price": float(prices.min()) if not prices.empty else None,
                "max_price": float(prices.max()) if not prices.empty else None,
                "median_total": float(total_prices.median()) if not total_prices.empty else None,
            }
        )

    groups_df = pd.DataFrame(group_rows)
    if groups_df.empty:
        return working

    groups_df = groups_df.sort_values(
        ["listings", "median_price"],
        ascending=[False, False],
        na_position="last",
    ).reset_index(drop=True)

    # Put group summary columns first in the detailed dataframe as well.
    return working.merge(
        groups_df[["group_id", "part_group"]],
        on=["group_id", "part_group"],
        how="left",
        suffixes=("", "_summary"),
    )


def prepare_items_for_grouping(
    payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in payload.get("itemSummaries", []) or []:
        price = item.get("price") or {}
        seller = item.get("seller") or {}
        try:
            item_price = float(price.get("value", 0) or 0)
        except (TypeError, ValueError):
            item_price = 0.0
        shipping_cost = extract_shipping_cost(item)
        rows.append(
            {
                "item_id": item.get("itemId"),
                "title": item.get("title"),
                "price": item_price,
                "shipping_cost": shipping_cost,
                "total_price": item_price + shipping_cost,
                "currency": price.get("currency"),
                "condition_text": item.get("condition") or "",
                "item_location": extract_location(item),
                "buying_options": ", ".join(item.get("buyingOptions") or []),
                "item_web_url": item.get("itemWebUrl"),
                "seller_username": seller.get("username"),
                "raw_json": json.dumps(item),
                "raw_item": item,
            }
        )
    return rows


def dedupe_and_group(
    rows: List[Dict[str, Any]],
    year: str,
    make: str,
    model: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    detail_df = group_active_listings(rows, year, make, model)

    # Exact duplicate rows are hidden from the listing table, but their count
    # remains visible in the group summary.
    unique_detail_df = detail_df[detail_df["duplicate_rank"] == 0].copy()

    group_rows: List[Dict[str, Any]] = []
    for group_id, group in unique_detail_df.groupby("group_id", sort=False):
        prices = pd.to_numeric(group["price"], errors="coerce").dropna()
        totals = pd.to_numeric(group["total_price"], errors="coerce").dropna()
        sellers = group["seller_username"].fillna("").astype(str).str.strip()
        identifiers = [x for x in group["identifier_display"] if x]
        rep = group.iloc[0]
        group_rows.append(
            {
                "group_id": group_id,
                "part_group": rep["group_label"],
                "listings": len(group),
                "duplicates_removed": int(group["duplicate_count"].sum() - len(group)),
                "unique_sellers": int(sellers[sellers != ""].nunique()),
                "group_method": rep["group_method"],
                "part_identifiers": "; ".join(dict.fromkeys(identifiers)),
                "median_price": float(prices.median()) if not prices.empty else None,
                "average_price": float(prices.mean()) if not prices.empty else None,
                "min_price": float(prices.min()) if not prices.empty else None,
                "max_price": float(prices.max()) if not prices.empty else None,
                "median_total": float(totals.median()) if not totals.empty else None,
            }
        )

    groups_df = pd.DataFrame(group_rows)
    if not groups_df.empty:
        groups_df = groups_df.sort_values(
            ["listings", "median_price"],
            ascending=[False, False],
            na_position="last",
        ).reset_index(drop=True)

    return unique_detail_df, groups_df


def save_search_and_items(
    query: str,
    rows: List[Dict[str, Any]],
) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    created_at = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "INSERT INTO searches (query_text, marketplace_id, created_at) VALUES (?, ?, ?)",
        (query, MARKETPLACE_ID, created_at),
    )
    search_id = cur.lastrowid

    for row in rows:
        identifiers = extract_part_identifiers(row.get("raw_item", {}) or {})
        cur.execute(
            """
            INSERT INTO active_items (
                search_id, item_id, title, price, shipping_cost, total_price,
                currency, condition_text, item_location, buying_options,
                item_web_url, seller_username, brand, mpn, epid, gtin,
                part_number, interchange_numbers, group_key, group_method,
                duplicate_key, last_seen, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                search_id,
                row.get("item_id"),
                row.get("title"),
                row.get("price"),
                row.get("shipping_cost"),
                row.get("total_price"),
                row.get("currency"),
                row.get("condition_text"),
                row.get("item_location"),
                row.get("buying_options"),
                row.get("item_web_url"),
                row.get("seller_username"),
                identifiers["brand"],
                identifiers["mpn"],
                identifiers["epid"],
                identifiers["gtin"],
                identifiers["part_number"],
                "; ".join(identifiers["interchange_numbers"]),
                None,
                None,
                None,
                created_at,
                row.get("raw_json"),
            ),
        )

    conn.commit()
    conn.close()


def load_recent_results() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT
            s.query_text,
            s.created_at,
            a.item_id,
            a.title,
            a.price,
            a.shipping_cost,
            a.total_price,
            a.currency,
            a.condition_text,
            a.item_location,
            a.buying_options,
            a.item_web_url,
            a.seller_username,
            a.brand,
            a.mpn,
            a.epid,
            a.gtin,
            a.part_number,
            a.interchange_numbers
        FROM active_items a
        JOIN searches s ON s.id = a.search_id
        ORDER BY a.id DESC
        """,
        conn,
    )
    conn.close()
    return df


def summarize_results(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty:
        return {
            "count": 0,
            "median_price": None,
            "mean_price": None,
            "max_price": None,
            "median_total": None,
        }
    prices = pd.to_numeric(df["price"], errors="coerce").dropna()
    totals = pd.to_numeric(df["total_price"], errors="coerce").dropna()
    return {
        "count": int(len(df)),
        "median_price": float(prices.median()) if not prices.empty else None,
        "mean_price": float(prices.mean()) if not prices.empty else None,
        "max_price": float(prices.max()) if not prices.empty else None,
        "median_total": float(totals.median()) if not totals.empty else None,
    }


def main() -> None:
    st.set_page_config(
        page_title="eBay Parts Research",
        layout="wide",
    )

    init_db()

    st.title("eBay Parts Research")
    st.caption(
        "Research active used eBay listings for automotive salvage sourcing."
    )

    with st.sidebar:
        st.header("Vehicle")
        year = st.text_input("Year", value="2011")
        make = st.text_input("Make", value="Toyota")
        model = st.text_input("Model", value="Camry")
        part = st.text_input(
            "Part",
            value="",
            placeholder="Leave blank for all parts",
        )

        st.divider()
        st.header("Active Listing Search")
        used_only = st.checkbox("Used listings only", value=True)
        max_listings = st.slider(
            "Maximum active listings",
            min_value=50,
            max_value=1000,
            value=200,
            step=50,
        )
        search_button = st.button(
            "Search eBay",
            type="primary",
            width="stretch",
        )

    if search_button:
        query = build_search_query(year, make, model, part)
        if not query:
            st.error("Enter at least one vehicle or part search field.")
        else:
            try:
                with st.spinner(f"Searching eBay for '{query}'..."):
                    payload = search_active_listings(
                        query=query,
                        max_listings=max_listings,
                        used_only=used_only,
                    )
                    rows = prepare_items_for_grouping(payload)
                    if not rows:
                        st.warning("No active listings were returned.")
                    else:
                        unique_df, groups_df = dedupe_and_group(
                            rows, year, make, model
                        )
                        save_search_and_items(query, rows)
                        st.session_state["latest_rows"] = rows
                        st.session_state["latest_df"] = unique_df
                        st.session_state["latest_groups_df"] = groups_df
                        st.session_state["latest_query"] = query
                        st.session_state["latest_total"] = payload.get("total", len(rows))
                        st.success(
                            f"Retrieved {len(rows):,} active listings, "
                            f"reduced to {len(unique_df):,} unique listings "
                            f"across {len(groups_df):,} part groups."
                        )
            except RuntimeError as ex:
                st.error(str(ex))
            except requests.RequestException as ex:
                st.error(
                    "Unable to reach eBay. Please try again.\n\n"
                    f"Technical detail: {ex}"
                )
            except Exception as ex:
                st.error(f"Unexpected application error: {ex}")

    df = st.session_state.get("latest_df")
    groups_df = st.session_state.get("latest_groups_df")
    latest_rows = st.session_state.get("latest_rows")

    if df is None or df.empty:
        df = load_recent_results()

    if df is not None and not df.empty:
        summary = summarize_results(df)
        latest_query = st.session_state.get("latest_query", "")
        if latest_query:
            total_available = st.session_state.get("latest_total", len(df))
            st.info(
                f"Search: **{latest_query}** | eBay reports approximately "
                f"**{total_available:,}** matching active listings. "
                f"Retrieved **{len(latest_rows) if latest_rows else len(df):,}** and "
                f"showing **{len(df):,}** unique listings."
            )

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Unique Listings", f"{len(df):,}")
        c2.metric("Part Groups", f"{len(groups_df):,}" if groups_df is not None else "0")
        c3.metric(
            "Median Price",
            f"${summary['median_price']:.2f}" if summary["median_price"] is not None else "n/a",
        )
        c4.metric(
            "Average Price",
            f"${summary['mean_price']:.2f}" if summary["mean_price"] is not None else "n/a",
        )
        c5.metric(
            "Median + Shipping",
            f"${summary['median_total']:.2f}" if summary["median_total"] is not None else "n/a",
        )

        tab_groups, tab_listings = st.tabs(["Part Groups", "Listings"])

        with tab_groups:
            if groups_df is None or groups_df.empty:
                st.info("No part groups could be formed from this search.")
            else:
                group_display = groups_df.rename(
                    columns={
                        "group_id": "Group",
                        "part_group": "Part Group",
                        "listings": "Listings",
                        "duplicates_removed": "Duplicates Removed",
                        "unique_sellers": "Sellers",
                        "group_method": "Grouping Method",
                        "part_identifiers": "Part / Interchange Numbers",
                        "median_price": "Median Price",
                        "average_price": "Average Price",
                        "min_price": "Min Price",
                        "max_price": "Max Price",
                        "median_total": "Median + Shipping",
                    }
                )
                st.dataframe(
                    group_display,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Median Price": st.column_config.NumberColumn(format="$%.2f"),
                        "Average Price": st.column_config.NumberColumn(format="$%.2f"),
                        "Min Price": st.column_config.NumberColumn(format="$%.2f"),
                        "Max Price": st.column_config.NumberColumn(format="$%.2f"),
                        "Median + Shipping": st.column_config.NumberColumn(format="$%.2f"),
                    },
                )

                st.subheader("Group Details")
                selected_group = st.selectbox(
                    "Select a part group",
                    groups_df["group_id"].tolist(),
                    format_func=lambda gid: (
                        f"{gid} - "
                        f"{groups_df.loc[groups_df['group_id'] == gid, 'part_group'].iloc[0]}"
                    ),
                )
                group_detail = df[df["group_id"] == selected_group].copy()
                detail_cols = [
                    "title", "price", "shipping_cost", "total_price",
                    "condition_text", "seller_username", "brand", "mpn",
                    "epid", "gtin", "interchange_numbers", "group_method",
                    "item_web_url",
                ]
                detail_cols = [c for c in detail_cols if c in group_detail.columns]
                detail_display = group_detail[detail_cols].rename(
                    columns={
                        "title": "Title",
                        "price": "Price",
                        "shipping_cost": "Shipping",
                        "total_price": "Total",
                        "condition_text": "Condition",
                        "seller_username": "Seller",
                        "brand": "Brand",
                        "mpn": "MPN",
                        "epid": "ePID",
                        "gtin": "GTIN",
                        "interchange_numbers": "Interchange",
                        "group_method": "Grouping Method",
                        "item_web_url": "eBay URL",
                    }
                )
                st.dataframe(
                    detail_display,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Price": st.column_config.NumberColumn(format="$%.2f"),
                        "Shipping": st.column_config.NumberColumn(format="$%.2f"),
                        "Total": st.column_config.NumberColumn(format="$%.2f"),
                        "eBay URL": st.column_config.LinkColumn("eBay Listing"),
                    },
                )

        with tab_listings:
            display_df = df[
                [
                    "title",
                    "price",
                    "shipping_cost",
                    "total_price",
                    "currency",
                    "condition_text",
                    "part_number",
                    "interchange_numbers_list",
                    "group_id",
                    "group_method",
                    "seller_username",
                    "item_web_url",
                ]
            ].copy()
            display_df["interchange_numbers"] = display_df[
                "interchange_numbers_list"
            ].apply(lambda x: "; ".join(x) if isinstance(x, list) else "")
            display_df = display_df.drop(columns=["interchange_numbers_list"])
            display_df = display_df.rename(
                columns={
                    "title": "Title",
                    "price": "Price",
                    "shipping_cost": "Shipping",
                    "total_price": "Total",
                    "currency": "Currency",
                    "condition_text": "Condition",
                    "part_number": "MPN",
                    "interchange_numbers": "Interchange",
                    "group_id": "Group",
                    "group_method": "Grouping Method",
                    "seller_username": "Seller",
                    "item_web_url": "eBay URL",
                }
            )
            st.dataframe(
                display_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "Price": st.column_config.NumberColumn(format="$%.2f"),
                    "Shipping": st.column_config.NumberColumn(format="$%.2f"),
                    "Total": st.column_config.NumberColumn(format="$%.2f"),
                    "eBay URL": st.column_config.LinkColumn("eBay Listing"),
                },
            )

        csv_data = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download Grouped Listings CSV",
            data=csv_data,
            file_name="ebay_grouped_listings.csv",
            mime="text/csv",
            width="stretch",
        )

    else:
        st.info("Enter a vehicle/part search and click **Search eBay** to begin.")


if __name__ == "__main__":
    main()
