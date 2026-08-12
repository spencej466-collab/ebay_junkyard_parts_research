import base64
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import streamlit as st
import truststore

# Use the host/system certificate store where available.
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


def get_secret(name: str) -> str:
    """Read a credential from Streamlit secrets first, then environment variables."""
    try:
        value = st.secrets.get(name)
        if value:
            return str(value).strip()
    except Exception:
        pass
    return os.getenv(name, "").strip()


def get_credentials() -> tuple[str, str]:
    client_id = get_secret("EBAY_CLIENT_ID")
    client_secret = get_secret("EBAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "eBay credentials are not configured. Set EBAY_CLIENT_ID and "
            "EBAY_CLIENT_SECRET in Streamlit secrets or environment variables."
        )
    return client_id, client_secret


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

    # Lightweight schema migration for earlier local prototype databases.
    cur.execute("PRAGMA table_info(active_items)")
    existing = {row[1] for row in cur.fetchall()}
    migrations = {
        "shipping_cost": "ALTER TABLE active_items ADD COLUMN shipping_cost REAL",
        "total_price": "ALTER TABLE active_items ADD COLUMN total_price REAL",
        "item_location": "ALTER TABLE active_items ADD COLUMN item_location TEXT",
        "buying_options": "ALTER TABLE active_items ADD COLUMN buying_options TEXT",
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

    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": SCOPES},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if "access_token" not in payload:
        raise RuntimeError(f"Unexpected eBay token response: {payload}")
    return payload["access_token"]


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
    offset = 0
    total_available = 0

    while len(all_items) < max_listings:
        remaining = max_listings - len(all_items)
        current_limit = min(PAGE_SIZE, remaining)
        params: Dict[str, Any] = {
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

        if offset == 0:
            total_available = int(payload.get("total", 0) or 0)
        if not page_items:
            break

        all_items.extend(page_items)
        offset += len(page_items)
        if offset >= total_available or len(page_items) < current_limit:
            break

    return {"itemSummaries": all_items, "total": total_available or len(all_items)}


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


def flatten_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
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
            }
        )
    return rows


def save_search_and_items(query: str, rows: List[Dict[str, Any]]) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    created_at = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "INSERT INTO searches (query_text, marketplace_id, created_at) VALUES (?, ?, ?)",
        (query, MARKETPLACE_ID, created_at),
    )
    search_id = cur.lastrowid

    for row in rows:
        cur.execute(
            """
            INSERT INTO active_items (
                search_id, item_id, title, price, shipping_cost, total_price,
                currency, condition_text, item_location, buying_options,
                item_web_url, seller_username, last_seen, raw_json
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
        JOIN searches s ON s.id = a.search_id
        ORDER BY a.id DESC
        """,
        conn,
    )
    conn.close()
    return df


def summarize_results(df: pd.DataFrame) -> Dict[str, Optional[float]]:
    if df.empty:
        return {"count": 0, "median_price": None, "mean_price": None, "max_price": None}
    prices = pd.to_numeric(df["price"], errors="coerce").dropna()
    return {
        "count": int(len(df)),
        "median_price": float(prices.median()) if not prices.empty else None,
        "mean_price": float(prices.mean()) if not prices.empty else None,
        "max_price": float(prices.max()) if not prices.empty else None,
    }


def run_active_search(year: str, make: str, model: str, part: str, used_only: bool, max_listings: int) -> None:
    query = build_search_query(year, make, model, part)
    if not query:
        st.error("Enter at least one vehicle or part search field.")
        return

    try:
        with st.spinner(f"Searching eBay for '{query}'..."):
            payload = search_active_listings(
                query=query,
                max_listings=max_listings,
                used_only=used_only,
            )
            rows = flatten_items(payload)
            if not rows:
                st.warning("No active listings were returned.")
                return

            save_search_and_items(query, rows)
            st.session_state["latest_df"] = pd.DataFrame(rows)
            st.session_state["latest_query"] = query
            st.session_state["latest_total"] = payload.get("total", len(rows))
            st.success(f"Retrieved {len(rows):,} active listings.")
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response is not None else ""
        st.error(f"eBay API error: {exc}\n\n{body}")
    except Exception as exc:
        st.error(f"Search failed: {exc}")


def main() -> None:
    st.set_page_config(page_title="Junkyard Parts Research", page_icon="🚗", layout="wide")
    init_db()

    st.title("Junkyard Parts Research")
    st.caption(
        "Research live eBay listings for used automotive parts. "
        "Marketplace Insights integration is reserved for the approved API path."
    )

    with st.sidebar:
        st.header("Vehicle")
        year = st.text_input("Year", value="2011")
        make = st.text_input("Make", value="Toyota")
        model = st.text_input("Model", value="Camry")
        part = st.text_input("Part", value="", placeholder="Leave blank to search the vehicle broadly")

        st.divider()
        st.header("Active Listing Search")
        used_only = st.checkbox("Used listings only", value=True)
        max_listings = st.slider("Maximum active listings", 50, 1000, 200, 50)

        if st.button("Search eBay", type="primary", width="stretch"):
            run_active_search(year, make, model, part, used_only, max_listings)

    active_df = st.session_state.get("latest_df")
    if active_df is None or active_df.empty:
        active_df = load_recent_results()

    if active_df is None or active_df.empty:
        st.info("Enter a vehicle/part search and click Search eBay to begin.")
        return

    latest_query = st.session_state.get("latest_query", "")
    total_available = st.session_state.get("latest_total", len(active_df))
    if latest_query:
        st.info(
            f"Search: **{latest_query}** | eBay reports approximately "
            f"**{total_available:,}** active matches. Retrieved **{len(active_df):,}**."
        )

    summary = summarize_results(active_df)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Active Listings", f"{summary['count']:,}")
    c2.metric("Median Price", f"${summary['median_price']:.2f}" if summary["median_price"] is not None else "n/a")
    c3.metric("Average Price", f"${summary['mean_price']:.2f}" if summary["mean_price"] is not None else "n/a")
    c4.metric("Max Price", f"${summary['max_price']:.2f}" if summary["max_price"] is not None else "n/a")

    st.subheader("Current eBay Market")
    show_cols = [
        "title", "price", "shipping_cost", "total_price", "currency",
        "condition_text", "item_location", "seller_username", "item_web_url",
    ]
    display_df = active_df[show_cols].rename(
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
            "Price": st.column_config.NumberColumn("Price", format="$%.2f"),
            "Shipping": st.column_config.NumberColumn("Shipping", format="$%.2f"),
            "Total": st.column_config.NumberColumn("Total", format="$%.2f"),
            "eBay URL": st.column_config.LinkColumn("eBay Listing"),
        },
    )

    csv_data = active_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download Active Listings CSV",
        data=csv_data,
        file_name="ebay_active_listings.csv",
        mime="text/csv",
        width="content",
    )

    st.divider()
    st.subheader("Marketplace Insights")
    st.info(
        "This prototype is intentionally built around the live Browse API today. "
        "The next integration point is eBay's restricted Marketplace Insights API "
        "(`item_sales/search`) so sold-history metrics can be retrieved directly."
    )


if __name__ == "__main__":
    main()
