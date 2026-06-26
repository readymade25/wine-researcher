"""
Three-tier wine lookup logic.

Tier 1: specific wines you've personally tasted/rated
Tier 2: grape/style recognition + pairing rules (hardcoded common supermarket grapes)
Tier 3: LLM fallback for anything that misses both (logged for later cataloguing)
"""

import csv
import io
import requests

# --- Example data sources -----------------------------------------------
# In production these CSV URLs would be your published Google Sheets
# (File > Share > Publish to web > CSV), same pattern as your Tottori site.

TIER1_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTePDaX89kTpBqes9f2nTTt8yWpryyq20sbk2Xah02bwoCAYW5DHegoftoHyU-ztN4orUJmxaCdMTWW/pub?output=csv"
TIER2_RECOGNITION_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRcTkssOHFFWVeMKhwbYcxCDOVAkyRMWyRD5CCStXxOVe5Ey9O0yNdvmu468tLzBmFrOgangrDI7Pwt/pub?output=csv"
TIER2_PAIRING_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRssfhdcNwUEWAgokQFrCJzNoGqV5v6rGggF2xJ0ax7m9tHyFgrUNhwHd0SFNuR_830l9C6SBzqYxPj/pub?output=csv"

LOG_FILE = "misses_log.csv"  # append-only log of Tier 3 fallbacks for later review


def fetch_csv(url):
    """Download a published Google Sheet as CSV and return list of dict rows."""
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def normalize(text):
    return text.strip().lower()


def tier1_lookup(query, tasted_wines):
    """Exact/substring match against your personally tasted wines."""
    q = normalize(query)
    for row in tasted_wines:
        name = normalize(row.get("name", ""))
        if q == name or q in name or name in q:
            return {
                "tier": 1,
                "name": row["name"],
                "style": row.get("style"),
                "pairing": row.get("pairing"),
                "value_note": row.get("value_note"),
                "personal_take": row.get("personal_take"),
                "shop": row.get("shop"),
                "shop_price": row.get("shop_price"),
                "market_price_reference": row.get("market_price_reference"),
            }
    return None


def tier2_lookup(query, recognition_rows, pairing_rows):
    """
    Step A: does the query contain a known keyword (e.g. 'soave', 'rioja')?
    Step B: use the mapped grape to pull pairing rules.
    """
    q = normalize(query)

    matched_grape = None
    for row in recognition_rows:
        keyword = normalize(row.get("keyword", ""))
        if keyword and keyword in q:
            matched_grape = row.get("grape")
            break  # first match wins; keep keyword list ordered specific->general

    if not matched_grape:
        return None

    for row in pairing_rows:
        if normalize(row.get("grape", "")) == normalize(matched_grape):
            return {
                "tier": 2,
                "matched_grape": matched_grape,
                "style": row.get("style"),
                "pairing": row.get("pairing"),
                "drink_window": row.get("drink_window"),
            }

    # Grape recognized but no pairing rule written yet for it
    return {
        "tier": 2,
        "matched_grape": matched_grape,
        "style": None,
        "pairing": None,
        "drink_window": None,
        "note": "Grape recognized but no pairing rule on file yet.",
    }


def tier3_llm_fallback(query, api_key):
    """Call Claude for anything Tier 1 and Tier 2 couldn't resolve."""
    log_miss(query)

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 300,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Wine: '{query}'. Give a short, plain-English answer "
                        "covering: typical style (light/full, dry/sweet), a "
                        "concrete food pairing, and whether it's drink-now or "
                        "can be held. No fluff, no marketing tone. "
                        "Respond as JSON with keys: style, pairing, drink_window."
                    ),
                }
            ],
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    text = "".join(block.get("text", "") for block in data.get("content", []))
    return {"tier": 3, "raw": text}


def log_miss(query):
    """Append unmatched queries to a CSV so they can be reviewed and promoted
    into Tier 1 or Tier 2 later."""
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([query])


def lookup_wine(query, tasted_wines, recognition_rows, pairing_rows, llm_api_key):
    """Main entry point: run the three tiers in order, return first hit."""
    result = tier1_lookup(query, tasted_wines)
    if result:
        return result

    result = tier2_lookup(query, recognition_rows, pairing_rows)
    if result and result.get("pairing"):
        return result

    return tier3_llm_fallback(query, llm_api_key)


# --- Example usage --------------------------------------------------------
if __name__ == "__main__":
    # In real use these would come from fetch_csv() calls against your
    # published Google Sheets, cached for some reasonable interval
    # (e.g. refresh every hour) rather than fetched on every request.
    tasted_wines = [
        {
            "name": "Bolla Soave Classico",
            "style": "Light, crisp, dry white",
            "pairing": "Salmon, light seafood, easy drinking",
            "value_note": "Solid for the price, reliable supermarket pick",
            "personal_take": "Always a safe bet when nothing else stands out",
        }
    ]

    recognition_rows = [
        {"keyword": "soave", "grape": "Garganega"},
        {"keyword": "chianti", "grape": "Sangiovese"},
        {"keyword": "rioja", "grape": "Tempranillo"},
        {"keyword": "sauvignon blanc", "grape": "Sauvignon Blanc"},
    ]

    pairing_rows = [
        {
            "grape": "Garganega",
            "style": "Light, high-acid, dry white",
            "pairing": "Delicate fish, light apps, not heavy sauces",
            "drink_window": "Drink young, within 1-2 years",
        },
        {
            "grape": "Sangiovese",
            "style": "Medium-bodied, high-acid red",
            "pairing": "Tomato-based pasta, pizza, grilled meats",
            "drink_window": "Drink now, basic Chianti doesn't age",
        },
    ]

    print(tier1_lookup("bolla soave", tasted_wines))
    print(tier2_lookup("Banfi Chianti Riserva", recognition_rows, pairing_rows))
    print(tier2_lookup("Some random Greek wine nobody has heard of", recognition_rows, pairing_rows))