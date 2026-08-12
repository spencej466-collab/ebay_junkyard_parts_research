# scraper.py

import base64
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import streamlit as st
import truststore

# Use the Windows system certificate store.
# This is especially useful on corporate-managed PCs.
truststore.inject_into_ssl()


# =============================================================================
# Configuration
# =============================================================================

DB_PATH = "ebay_research.db"

# Production
SANDBOX = False

# eBay US marketplace
MARKETPLACE_ID = "EBAY_US"

API_ROOT = (
    "https://api.sandbox.ebay.com"
    if SANDBOX
    else "https://api.ebay.com"
)

TOKEN_URL = f"{API_ROOT}/identity/v1/oauth2/token"
SEARCH_URL = f"{API_ROOT}/buy/browse/v1/item_summary/search"

SCOPES = "https://api.ebay.com/oauth/api_scope"

# Browse API page size.
# eBay can change the maximum page size, so the pagination logic below
# follows the response's "next" link instead of assuming a fixed size.
PAGE_SIZE = 200

# Application safety limit.
MAX_LISTINGS = 1000

# Transient HTTP status codes that are safe to retry.
RETRYABLE_STATUS_CODES = {
    429,  # Too Many Requests
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
}

# Maximum retry attempts after the initial request.
MAX_RETRIES = 2

# Initial delay between retries.
INITIAL_RETRY_DELAY = 1.0


# =============================================================================
# Database
# =============================================================================

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
            last_seen TEXT NOT NULL,
            raw_json TEXT,
            FOREIGN KEY(search_id) REFERENCES searches(id)
        )
        """
    )

    # -------------------------------------------------------------------------
    # Upgrade older versions of the database.
    # -------------------------------------------------------------------------

    cur.execute("PRAGMA table_info(active_items)")

    existing_columns = {
        row[1]
        for row in cur.fetchall()
    }

    migrations = {
        "shipping_cost":
            "ALTER TABLE active_items ADD COLUMN shipping_cost REAL",

        "total_price":
            "ALTER TABLE active_items ADD COLUMN total_price REAL",

        "item_location":
            "ALTER TABLE active_items ADD COLUMN item_location TEXT",

        "buying_options":
            "ALTER TABLE active_items ADD COLUMN buying_options TEXT",
    }

    for column_name, sql in migrations.items():

        if column_name not in existing_columns:
            cur.execute(sql)

    conn.commit()
    conn.close()


# =============================================================================
# Credentials
# =============================================================================

def get_secret(name: str) -> str:
    """
    Read a secret from Streamlit Secrets first, then fall back to
    environment variables for local development.
    """

    value = ""

    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""

    if value:
        return str(value).strip()

    return os.getenv(name, "").strip()


def get_credentials() -> tuple[str, str]:

    client_id = get_secret("EBAY_CLIENT_ID")
    client_secret = get_secret("EBAY_CLIENT_SECRET")

    if not client_id or not client_secret:

        raise RuntimeError(
            "eBay credentials are not configured. "
            "Add EBAY_CLIENT_ID and EBAY_CLIENT_SECRET "
            "to Streamlit Secrets."
        )

    return client_id, client_secret


# =============================================================================
# HTTP Helpers
# =============================================================================

def format_error_response(
    response: requests.Response,
) -> str:
    """
    Extract a useful, user-readable error message from an eBay response.
    """

    status = response.status_code

    try:
        payload = response.json()

        errors = payload.get("errors")

        if errors:

            messages = []

            for error in errors:

                error_id = error.get("errorId", "")
                message = error.get("message", "")
                long_message = error.get("longMessage", "")

                parts = [
                    str(x).strip()
                    for x in [
                        error_id,
                        message,
                        long_message,
                    ]
                    if x
                ]

                if parts:
                    messages.append(" - ".join(parts))

            if messages:
                return (
                    f"HTTP {status}: "
                    + " | ".join(messages)
                )

        if "message" in payload:
            return f"HTTP {status}: {payload['message']}"

        return f"HTTP {status}: {json.dumps(payload)}"

    except Exception:

        text = response.text.strip()

        if text:
            return f"HTTP {status}: {text[:1000]}"

        return f"HTTP {status}"


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> requests.Response:
    """
    Execute an HTTP request with up to MAX_RETRIES retries for transient
    eBay/service errors.

    Retry sequence:
        initial attempt
        retry 1
        retry 2

    Uses exponential backoff and honors Retry-After when supplied.
    """

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

            delay = INITIAL_RETRY_DELAY * (2 ** attempt)
            time.sleep(delay)

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

    raise RuntimeError(
        "HTTP request failed without receiving a response."
    )


# =============================================================================
# OAuth
# =============================================================================

@st.cache_data(ttl=3500, show_spinner=False)
def get_app_token() -> str:

    client_id, client_secret = get_credentials()

    auth = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("utf-8")

    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {
        "grant_type": "client_credentials",
        "scope": SCOPES,
    }

    response = request_with_retry(
        "POST",
        TOKEN_URL,
        headers=headers,
        data=data,
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

        raise RuntimeError(
            "eBay returned a successful response but no access_token."
        )

    return token


# =============================================================================
# Search Construction
# =============================================================================

def build_search_query(
    year: str,
    make: str,
    model: str,
    part: str,
) -> str:

    pieces = []

    if year.strip():
        pieces.append(year.strip())

    if make.strip():
        pieces.append(make.strip())

    if model.strip():
        pieces.append(model.strip())

    if part.strip():
        pieces.append(part.strip())

    return " ".join(pieces)


# =============================================================================
# Browse API
# =============================================================================

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

            raw_total = payload.get("total", 0)

            try:
                total_available = int(raw_total)
            except (TypeError, ValueError):
                total_available = 0

        page_items = payload.get(
            "itemSummaries",
            [],
        ) or []

        if not page_items:
            break

        remaining = max_listings - len(all_items)

        all_items.extend(
            page_items[:remaining]
        )

        # ---------------------------------------------------------------------
        # eBay provides a "next" link when another page exists.
        #
        # We deliberately follow it instead of calculating pagination
        # ourselves from "total". eBay specifically recommends this because
        # the page size and result counts can change.
        # ---------------------------------------------------------------------

        next_url = payload.get("next")

        # Once we follow a fully constructed "next" URL, we must not send
        # the original search parameters again.
        params = None

    return {
        "itemSummaries": all_items,
        "total": total_available or len(all_items),
    }


# =============================================================================
# Active Listing Normalization
# =============================================================================

def extract_shipping_cost(
    item: Dict[str, Any],
) -> float:

    shipping_options = item.get(
        "shippingOptions"
    ) or []

    if not shipping_options:
        return 0.0

    first_option = shipping_options[0] or {}

    shipping_cost = first_option.get(
        "shippingCost"
    ) or {}

    try:

        return float(
            shipping_cost.get(
                "value",
                0,
            ) or 0
        )

    except (TypeError, ValueError):

        return 0.0


def extract_location(
    item: Dict[str, Any],
) -> str:

    location = item.get(
        "itemLocation"
    ) or {}

    city = location.get(
        "city",
        "",
    )

    state = location.get(
        "stateOrProvince",
        "",
    )

    postal_code = location.get(
        "postalCode",
        "",
    )

    pieces = [
        str(city).strip(),
        str(state).strip(),
        str(postal_code).strip(),
    ]

    return ", ".join(
        x for x in pieces if x
    )


def flatten_items(
    payload: Dict[str, Any],
) -> List[Dict[str, Any]]:

    items = payload.get(
        "itemSummaries",
        [],
    ) or []

    rows: List[Dict[str, Any]] = []

    for item in items:

        price = item.get(
            "price"
        ) or {}

        seller = item.get(
            "seller"
        ) or {}

        condition = item.get(
            "condition",
            "",
        )

        try:

            item_price = float(
                price.get(
                    "value",
                    0,
                ) or 0
            )

        except (TypeError, ValueError):

            item_price = 0.0

        shipping_cost = extract_shipping_cost(
            item
        )

        total_price = (
            item_price +
            shipping_cost
        )

        buying_options = item.get(
            "buyingOptions"
        ) or []

        rows.append(
            {
                "item_id": item.get(
                    "itemId"
                ),
                "title": item.get(
                    "title"
                ),
                "price": item_price,
                "shipping_cost": shipping_cost,
                "total_price": total_price,
                "currency": price.get(
                    "currency"
                ),
                "condition_text": condition,
                "item_location": extract_location(
                    item
                ),
                "buying_options": ", ".join(
                    buying_options
                ),
                "item_web_url": item.get(
                    "itemWebUrl"
                ),
                "seller_username": seller.get(
                    "username"
                ),
                "raw_json": json.dumps(
                    item
                ),
            }
        )

    return rows


# =============================================================================
# Database Storage
# =============================================================================

def save_search_and_items(
    query: str,
    rows: List[Dict[str, Any]],
) -> int:

    conn = sqlite3.connect(
        DB_PATH
    )

    cur = conn.cursor()

    created_at = datetime.now(
        timezone.utc
    ).isoformat()

    cur.execute(
        """
        INSERT INTO searches (
            query_text,
            marketplace_id,
            created_at
        )
        VALUES (?, ?, ?)
        """,
        (
            query,
            MARKETPLACE_ID,
            created_at,
        ),
    )

    search_id = cur.lastrowid

    for row in rows:

        cur.execute(
            """
            INSERT INTO active_items (
                search_id,
                item_id,
                title,
                price,
                shipping_cost,
                total_price,
                currency,
                condition_text,
                item_location,
                buying_options,
                item_web_url,
                seller_username,
                last_seen,
                raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                search_id,
                row["item_id"],
                row["title"],
                row["price"],
                row["shipping_cost"],
                row["total_price"],
                row["currency"],
                row["condition_text"],
                row["item_location"],
                row["buying_options"],
                row["item_web_url"],
                row["seller_username"],
                created_at,
                row["raw_json"],
            ),
        )

    conn.commit()
    conn.close()

    return int(search_id)


# =============================================================================
# Load Recent Results
# =============================================================================

def load_recent_results() -> pd.DataFrame:

    conn = sqlite3.connect(
        DB_PATH
    )

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
            a.seller_username
        FROM active_items a
        JOIN searches s
            ON s.id = a.search_id
        ORDER BY a.id DESC
        """,
        conn,
    )

    conn.close()

    return df


# =============================================================================
# Analytics
# =============================================================================

def summarize_results(
    df: pd.DataFrame,
) -> Dict[str, Any]:

    if df.empty:

        return {
            "count": 0,
            "median_price": None,
            "mean_price": None,
            "max_price": None,
            "median_total": None,
        }

    prices = pd.to_numeric(
        df["price"],
        errors="coerce",
    ).dropna()

    totals = pd.to_numeric(
        df["total_price"],
        errors="coerce",
    ).dropna()

    return {
        "count": int(len(df)),
        "median_price": (
            float(prices.median())
            if not prices.empty
            else None
        ),
        "mean_price": (
            float(prices.mean())
            if not prices.empty
            else None
        ),
        "max_price": (
            float(prices.max())
            if not prices.empty
            else None
        ),
        "median_total": (
            float(totals.median())
            if not totals.empty
            else None
        ),
    }


# =============================================================================
# Streamlit UI
# =============================================================================

def main() -> None:

    st.set_page_config(
        page_title="eBay Parts Research",
        layout="wide",
    )

    init_db()

    st.title(
        "eBay Parts Research"
    )

    st.caption(
        "Research active used eBay listings "
        "for automotive salvage sourcing."
    )

    # =========================================================================
    # Sidebar
    # =========================================================================

    with st.sidebar:

        st.header("Vehicle")

        year = st.text_input(
            "Year",
            value="2011",
        )

        make = st.text_input(
            "Make",
            value="Toyota",
        )

        model = st.text_input(
            "Model",
            value="Camry",
        )

        part = st.text_input(
            "Part",
            value="",
            placeholder="Leave blank for all parts",
        )

        st.divider()

        st.header(
            "Active Listing Search"
        )

        used_only = st.checkbox(
            "Used listings only",
            value=True,
        )

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

    # =========================================================================
    # Search
    # =========================================================================

    if search_button:

        query = build_search_query(
            year=year,
            make=make,
            model=model,
            part=part,
        )

        if not query:

            st.error(
                "Enter at least one vehicle or part "
                "search field."
            )

        else:

            try:

                with st.spinner(
                    f"Searching eBay for '{query}'..."
                ):

                    payload = search_active_listings(
                        query=query,
                        max_listings=max_listings,
                        used_only=used_only,
                    )

                    rows = flatten_items(
                        payload
                    )

                    if not rows:

                        st.warning(
                            "No active listings "
                            "were returned."
                        )

                    else:

                        save_search_and_items(
                            query=query,
                            rows=rows,
                        )

                        df = pd.DataFrame(
                            rows
                        )

                        st.session_state[
                            "latest_df"
                        ] = df

                        st.session_state[
                            "latest_query"
                        ] = query

                        st.session_state[
                            "latest_total"
                        ] = payload.get(
                            "total",
                            len(rows),
                        )

                        st.success(
                            f"Retrieved {len(rows):,} "
                            "active listings."
                        )

            except RuntimeError as ex:

                st.error(
                    str(ex)
                )

            except requests.RequestException as ex:

                st.error(
                    "Unable to reach eBay. "
                    "Please try again.\n\n"
                    f"Technical detail: {ex}"
                )

            except Exception as ex:

                st.error(
                    f"Unexpected application error: {ex}"
                )

    # =========================================================================
    # Results
    # =========================================================================

    df = st.session_state.get(
        "latest_df"
    )

    if df is None or df.empty:

        df = load_recent_results()

    if df is not None and not df.empty:

        summary = summarize_results(
            df
        )

        # ---------------------------------------------------------------------
        # Search Summary
        # ---------------------------------------------------------------------

        latest_query = st.session_state.get(
            "latest_query",
            "",
        )

        if latest_query:

            total_available = st.session_state.get(
                "latest_total",
                len(df),
            )

            st.info(
                f"Search: **{latest_query}** | "
                f"eBay reports approximately "
                f"**{total_available:,}** "
                f"matching active listings. "
                f"Retrieved **{len(df):,}**."
            )

        # ---------------------------------------------------------------------
        # Metrics
        # ---------------------------------------------------------------------

        c1, c2, c3, c4, c5 = st.columns(5)

        c1.metric(
            "Listings",
            f"{summary['count']:,}",
        )

        c2.metric(
            "Median Price",
            (
                f"${summary['median_price']:.2f}"
                if summary["median_price"] is not None
                else "n/a"
            ),
        )

        c3.metric(
            "Average Price",
            (
                f"${summary['mean_price']:.2f}"
                if summary["mean_price"] is not None
                else "n/a"
            ),
        )

        c4.metric(
            "Max Price",
            (
                f"${summary['max_price']:.2f}"
                if summary["max_price"] is not None
                else "n/a"
            ),
        )

        c5.metric(
            "Median + Shipping",
            (
                f"${summary['median_total']:.2f}"
                if summary["median_total"] is not None
                else "n/a"
            ),
        )

        # ---------------------------------------------------------------------
        # Results
        # ---------------------------------------------------------------------

        st.subheader(
            "Active Listings"
        )

        show_cols = [
            "title",
            "price",
            "shipping_cost",
            "total_price",
            "currency",
            "condition_text",
            "item_location",
            "seller_username",
            "item_web_url",
        ]

        display_df = df[
            show_cols
        ].copy()

        display_df = display_df.rename(
            columns={
                "title": "Title",
                "price": "Price",
                "shipping_cost": "Shipping",
                "total_price": "Total",
                "currency": "Currency",
                "condition_text": "Condition",
                "item_location": "Location",
                "seller_username": "Seller",
                "item_web_url": "eBay URL",
            }
        )

        st.dataframe(
            display_df,
            width="stretch",
            hide_index=True,
            column_config={
                "Price": st.column_config.NumberColumn(
                    "Price",
                    format="$%.2f",
                ),
                "Shipping": st.column_config.NumberColumn(
                    "Shipping",
                    format="$%.2f",
                ),
                "Total": st.column_config.NumberColumn(
                    "Total",
                    format="$%.2f",
                ),
                "eBay URL": st.column_config.LinkColumn(
                    "eBay Listing",
                ),
            },
        )

        # ---------------------------------------------------------------------
        # CSV Export
        # ---------------------------------------------------------------------

        csv_data = df.to_csv(
            index=False
        ).encode("utf-8")

        st.download_button(
            label="Download Results CSV",
            data=csv_data,
            file_name="ebay_active_listings.csv",
            mime="text/csv",
            width="stretch",
        )

    else:

        st.info(
            "Enter a vehicle/part search and click "
            "**Search eBay** to begin."
        )


if __name__ == "__main__":
    main()
