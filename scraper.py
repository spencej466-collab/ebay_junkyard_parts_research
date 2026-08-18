# app.py

import base64
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import pandas as pd
import requests
import streamlit as st
import truststore

# Use the Windows system certificate store.
truststore.inject_into_ssl()


# =============================================================================
# Configuration
# =============================================================================

DB_PATH = "ebay_research.db"

# Production
SANDBOX = False

MARKETPLACE_ID = "EBAY_US"

API_ROOT = (
    "https://api.sandbox.ebay.com"
    if SANDBOX
    else "https://api.ebay.com"
)

TOKEN_URL = f"{API_ROOT}/identity/v1/oauth2/token"
SEARCH_URL = f"{API_ROOT}/buy/browse/v1/item_summary/search"

SCOPES = "https://api.ebay.com/oauth/api_scope"

PAGE_SIZE = 200
MAX_LISTINGS = 1000


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

    conn.commit()
    conn.close()


# =============================================================================
# eBay Authentication
# =============================================================================

def get_credentials() -> tuple[str, str]:
    client_id = os.getenv("EBAY_CLIENT_ID", "").strip()
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise RuntimeError(
            "Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET "
            "in your PowerShell environment."
        )

    return client_id, client_secret


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

    response = requests.post(
        TOKEN_URL,
        headers=headers,
        data=data,
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()

    if "access_token" not in payload:
        raise RuntimeError(
            f"eBay token response did not contain an access_token: {payload}"
        )

    return payload["access_token"]


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
# eBay Browse API
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

    offset = 0
    total_available = None

    while len(all_items) < max_listings:

        remaining = max_listings - len(all_items)
        current_limit = min(PAGE_SIZE, remaining)

        params = {
            "q": query,
            "limit": current_limit,
            "offset": offset,
        }

        if used_only:
            params["filter"] = "conditionIds:{3000}"

        response = requests.get(
            SEARCH_URL,
            headers=headers,
            params=params,
            timeout=30,
        )

        response.raise_for_status()

        payload = response.json()

        page_items = payload.get("itemSummaries", []) or []

        if total_available is None:
            total_available = payload.get("total", 0)

        if not page_items:
            break

        all_items.extend(page_items)

        offset += len(page_items)

        if offset >= total_available:
            break

        if len(page_items) < current_limit:
            break

    return {
        "itemSummaries": all_items,
        "total": total_available or len(all_items),
    }


# =============================================================================
# Normalize Active Listings
# =============================================================================

def extract_shipping_cost(item: Dict[str, Any]) -> float:

    shipping_options = item.get("shippingOptions") or []

    if not shipping_options:
        return 0.0

    first_option = shipping_options[0] or {}
    shipping_cost = first_option.get("shippingCost") or {}

    try:
        return float(shipping_cost.get("value", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def extract_location(item: Dict[str, Any]) -> str:

    location = item.get("itemLocation") or {}

    city = location.get("city", "")
    state = location.get("stateOrProvince", "")
    postal_code = location.get("postalCode", "")

    pieces = [
        str(city).strip(),
        str(state).strip(),
        str(postal_code).strip(),
    ]

    return ", ".join(x for x in pieces if x)


def flatten_items(
    payload: Dict[str, Any]
) -> List[Dict[str, Any]]:

    items = payload.get("itemSummaries", []) or []

    rows: List[Dict[str, Any]] = []

    for item in items:

        price = item.get("price") or {}
        seller = item.get("seller") or {}

        condition = item.get("condition") or ""

        try:
            item_price = float(price.get("value", 0) or 0)
        except (TypeError, ValueError):
            item_price = 0.0

        shipping_cost = extract_shipping_cost(item)

        total_price = item_price + shipping_cost

        buying_options = item.get("buyingOptions") or []

        rows.append(
            {
                "item_id": item.get("itemId"),
                "title": item.get("title"),
                "price": item_price,
                "shipping_cost": shipping_cost,
                "total_price": total_price,
                "currency": price.get("currency"),
                "condition_text": condition,
                "item_location": extract_location(item),
                "buying_options": ", ".join(buying_options),
                "item_web_url": item.get("itemWebUrl"),
                "seller_username": seller.get("username"),
                "raw_json": json.dumps(item),
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

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    created_at = datetime.now(timezone.utc).isoformat()

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
# Load Active Results
# =============================================================================

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
# Sold CSV Processing
# =============================================================================

def guess_column(
    columns: List[str],
    keywords: List[str],
) -> str:

    lowered = {
        column: column.lower().strip()
        for column in columns
    }

    for keyword in keywords:

        for original, lower in lowered.items():

            if keyword in lower:
                return original

    return columns[0] if columns else ""


def process_sold_csv(
    uploaded_file,
) -> pd.DataFrame:

    try:
        sold_df = pd.read_csv(uploaded_file)
    except Exception as ex:
        raise RuntimeError(
            f"Could not read the CSV file: {ex}"
        )

    if sold_df.empty:
        raise RuntimeError(
            "The uploaded CSV contains no rows."
        )

    columns = sold_df.columns.tolist()

    st.write("Detected columns:")
    st.write(columns)

    st.markdown("### Map Sold Data Columns")

    price_guess = guess_column(
        columns,
        [
            "sold price",
            "sale price",
            "price",
            "sold",
            "amount",
        ],
    )

    date_guess = guess_column(
        columns,
        [
            "sold date",
            "sale date",
            "date sold",
            "date",
        ],
    )

    title_guess = guess_column(
        columns,
        [
            "title",
            "item title",
            "listing title",
            "item",
        ],
    )

    price_column = st.selectbox(
        "Sold price column",
        options=columns,
        index=columns.index(price_guess),
    )

    date_column = st.selectbox(
        "Sold date column",
        options=columns,
        index=columns.index(date_guess),
    )

    title_column = st.selectbox(
        "Title column",
        options=columns,
        index=columns.index(title_guess),
    )

    sold_df["sold_price"] = (
        sold_df[price_column]
        .astype(str)
        .str.replace("$", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.strip()
    )

    sold_df["sold_price"] = pd.to_numeric(
        sold_df["sold_price"],
        errors="coerce",
    )

    sold_df["sold_date"] = pd.to_datetime(
        sold_df[date_column],
        errors="coerce",
    )

    sold_df["sold_title"] = (
        sold_df[title_column]
        .astype(str)
        .str.strip()
    )

    sold_df = sold_df.dropna(
        subset=["sold_price"]
    )

    # -------------------------------------------------------------------------
    # 90-day filter
    # -------------------------------------------------------------------------

    today = pd.Timestamp.now().normalize()
    cutoff = today - pd.Timedelta(days=90)

    sold_df = sold_df[
        sold_df["sold_date"].isna()
        | (sold_df["sold_date"] >= cutoff)
    ].copy()

    # Remove obvious zero/negative prices.
    sold_df = sold_df[
        sold_df["sold_price"] > 0
    ]

    return sold_df


# =============================================================================
# Sold Metrics
# =============================================================================

def calculate_sold_metrics(
    sold_df: pd.DataFrame,
) -> Dict[str, Any]:

    if sold_df is None or sold_df.empty:

        return {
            "sold_count": 0,
            "median_sold_price": None,
            "average_sold_price": None,
            "min_sold_price": None,
            "max_sold_price": None,
        }

    prices = pd.to_numeric(
        sold_df["sold_price"],
        errors="coerce",
    ).dropna()

    if prices.empty:

        return {
            "sold_count": 0,
            "median_sold_price": None,
            "average_sold_price": None,
            "min_sold_price": None,
            "max_sold_price": None,
        }

    return {
        "sold_count": int(len(prices)),
        "median_sold_price": float(prices.median()),
        "average_sold_price": float(prices.mean()),
        "min_sold_price": float(prices.min()),
        "max_sold_price": float(prices.max()),
    }


# =============================================================================
# Combined Metrics
# =============================================================================

def calculate_market_metrics(
    active_df: pd.DataFrame,
    sold_df: pd.DataFrame,
) -> Dict[str, Any]:

    active_count = (
        len(active_df)
        if active_df is not None
        else 0
    )

    sold_metrics = calculate_sold_metrics(
        sold_df
    )

    sold_count = sold_metrics["sold_count"]

    # This is an estimate using 90-day sold volume
    # against current active inventory.
    #
    # It is NOT intended to reproduce eBay's internal
    # Terapeak methodology exactly.
    if sold_count + active_count > 0:

        estimated_str = (
            sold_count /
            (sold_count + active_count)
        )

    else:
        estimated_str = 0.0

    return {
        "active_count": active_count,
        "sold_count": sold_count,
        "estimated_str": estimated_str,
        **sold_metrics,
    }


# =============================================================================
# Simple Pick Score
# =============================================================================

def calculate_pick_score(
    metrics: Dict[str, Any],
) -> float:

    active_count = metrics["active_count"]
    sold_count = metrics["sold_count"]
    median_price = metrics["median_sold_price"]
    estimated_str = metrics["estimated_str"]

    if sold_count == 0 or median_price is None:
        return 0.0

    # Demand component
    demand_score = min(
        sold_count * 2,
        50,
    )

    # Sell-through component
    str_score = estimated_str * 30

    # Value component
    value_score = min(
        median_price / 10,
        20,
    )

    score = (
        demand_score +
        str_score +
        value_score
    )

    return round(
        min(score, 100),
        1,
    )


# =============================================================================
# Streamlit App
# =============================================================================

def main() -> None:

    st.set_page_config(
        page_title="eBay Parts Research",
        layout="wide",
    )

    init_db()

    st.title("eBay Parts Research")

    st.caption(
        "Research active and recently sold eBay listings "
        "for junkyard parts sourcing."
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

        st.header("Active Listing Search")

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
    # Search eBay
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
                "Enter at least one vehicle or part search field."
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

                    rows = flatten_items(payload)

                    if not rows:

                        st.warning(
                            "No active listings were returned."
                        )

                    else:

                        save_search_and_items(
                            query=query,
                            rows=rows,
                        )

                        df = pd.DataFrame(rows)

                        st.session_state["latest_df"] = df
                        st.session_state["latest_query"] = query
                        st.session_state["latest_total"] = payload.get(
                            "total",
                            len(rows),
                        )

                        st.success(
                            f"Retrieved {len(rows):,} "
                            "active listings."
                        )

            except requests.HTTPError as ex:

                response_text = ""

                if ex.response is not None:
                    response_text = ex.response.text

                st.error(
                    f"eBay API error: {ex}\n\n"
                    f"{response_text}"
                )

            except Exception as ex:

                st.error(
                    f"Search failed: {ex}"
                )

    # =========================================================================
    # Active Data
    # =========================================================================

    active_df = st.session_state.get(
        "latest_df"
    )

    if active_df is None or active_df.empty:

        active_df = load_recent_results()

    # =========================================================================
    # Sold Data Upload
    # =========================================================================

    st.sidebar.divider()

    st.sidebar.header("Sold Data")

    sold_file = st.sidebar.file_uploader(
        "Upload sold listings CSV",
        type=["csv"],
        help=(
            "Upload a CSV containing sold listings for the "
            "same vehicle/part search."
        ),
    )

    sold_df = None

    if sold_file is not None:

        try:

            sold_df = process_sold_csv(
                sold_file
            )

            st.session_state["sold_df"] = sold_df

            st.sidebar.success(
                f"{len(sold_df):,} sold records loaded."
            )

        except Exception as ex:

            st.sidebar.error(
                f"Sold data error: {ex}"
            )

    elif "sold_df" in st.session_state:

        sold_df = st.session_state["sold_df"]

    # =========================================================================
    # Combined Metrics
    # =========================================================================

    if (
        active_df is not None
        and not active_df.empty
    ):

        metrics = calculate_market_metrics(
            active_df,
            sold_df,
        )

        pick_score = calculate_pick_score(
            metrics
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
                len(active_df),
            )

            st.info(
                f"Search: **{latest_query}** | "
                f"eBay reports approximately "
                f"**{total_available:,}** active matches. "
                f"We retrieved **{len(active_df):,}**."
            )

        # ---------------------------------------------------------------------
        # Market Metrics
        # ---------------------------------------------------------------------

        st.subheader("Market Metrics")

        c1, c2, c3, c4, c5, c6 = st.columns(6)

        c1.metric(
            "Active",
            f"{metrics['active_count']:,}",
        )

        c2.metric(
            "Sold 90d",
            f"{metrics['sold_count']:,}",
        )

        c3.metric(
            "Estimated STR",
            f"{metrics['estimated_str']:.1%}",
        )

        c4.metric(
            "Median Sold",
            (
                f"${metrics['median_sold_price']:.2f}"
                if metrics["median_sold_price"] is not None
                else "n/a"
            ),
        )

        c5.metric(
            "Average Sold",
            (
                f"${metrics['average_sold_price']:.2f}"
                if metrics["average_sold_price"] is not None
                else "n/a"
            ),
        )

        c6.metric(
            "Pick Score",
            f"{pick_score:.1f}",
        )

        # ---------------------------------------------------------------------
        # Sold Summary
        # ---------------------------------------------------------------------

        if sold_df is not None and not sold_df.empty:

            st.subheader("Sold Listings")

            sold_display = sold_df[
                [
                    "sold_title",
                    "sold_price",
                    "sold_date",
                ]
            ].copy()

            sold_display = sold_display.rename(
                columns={
                    "sold_title": "Title",
                    "sold_price": "Sold Price",
                    "sold_date": "Sold Date",
                }
            )

            st.dataframe(
                sold_display,
                width="stretch",
                hide_index=True,
                column_config={
                    "Sold Price": st.column_config.NumberColumn(
                        "Sold Price",
                        format="$%.2f",
                    ),
                    "Sold Date": st.column_config.DatetimeColumn(
                        "Sold Date",
                    ),
                },
            )

        else:

            st.warning(
                "No sold data loaded. Upload a CSV in the sidebar "
                "to calculate sold metrics."
            )

        # ---------------------------------------------------------------------
        # Active Listings
        # ---------------------------------------------------------------------

        st.subheader("Active Listings")

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

        display_df = active_df[
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
        # Active Export
        # ---------------------------------------------------------------------

        csv_data = active_df.to_csv(
            index=False
        ).encode("utf-8")

        st.download_button(
            label="Download Active Listings CSV",
            data=csv_data,
            file_name="ebay_active_listings.csv",
            mime="text/csv",
            width="stretch",
        )

        # ---------------------------------------------------------------------
        # Combined Metrics Export
        # ---------------------------------------------------------------------

        summary_df = pd.DataFrame(
            [
                {
                    "query": latest_query,
                    "active_listings": metrics["active_count"],
                    "sold_90d": metrics["sold_count"],
                    "estimated_str": metrics["estimated_str"],
                    "median_sold_price": metrics[
                        "median_sold_price"
                    ],
                    "average_sold_price": metrics[
                        "average_sold_price"
                    ],
                    "pick_score": pick_score,
                }
            ]
        )

        summary_csv = summary_df.to_csv(
            index=False
        ).encode("utf-8")

        st.download_button(
            label="Download Market Metrics CSV",
            data=summary_csv,
            file_name="ebay_market_metrics.csv",
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