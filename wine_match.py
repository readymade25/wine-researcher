"""
Three-tier wine lookup logic.

Tier 1: specific wines you've personally tasted/rated
Tier 2: grape/style recognition + pairing rules (hardcoded common supermarket grapes)
Tier 3: LLM fallback for anything that misses both (logged for later cataloguing)
"""

import csv
import io
import json
import requests

# --- Example data sources -----------------------------------------------
# In production these CSV URLs would be your published Google Sheets
# (File > Share > Publish to web > CSV), same pattern as your Tottori site.

TIER1_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTePDaX89kTpBqes9f2nTTt8yWpryyq20sbk2Xah02bwoCAYW5DHegoftoHyU-ztN4orUJmxaCdMTWW/pub?output=csv"
TIER2_RECOGNITION_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRcTkssOHFFWVeMKhwbYcxCDOVAkyRMWyRD5CCStXxOVe5Ey9O0yNdvmu468tLzBmFrOgangrDI7Pwt/pub?output=csv"
TIER2_PAIRING_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRssfhdcNwUEWAgokQFrCJzNoGqV5v6rGggF2xJ0ax7m9tHyFgrUNhwHd0SFNuR_830l9C6SBzqYxPj/pub?output=csv"
SHOP_PICKS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQIbZYk4yJtk9jrClYeSuI9_Nq99qfnhga4HbcDTIA9mQUlgOlZxuFcUiLKTM8SwlPMWBXVGcXS8lkU/pub?output=csv"
FOOD_SYNONYMS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQYFf78-Ia73AACfk8PFAxkgOF-N_WuiVfUG2LA9GrQrkX_T4g54RYY7JPXTdm7jRZd5B2FKmYxWVXx/pub?output=csv"

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
    """Match against your personally tasted wines.

    Checks the query against producer, wine_name, and the combined
    "producer + wine_name" string, so a search for just the producer
    ("Felsina") or just the specific wine ("Rancia") can both match.

    Returns a LIST of all matching wines, not just the first one --
    e.g. searching "Hilt" when you have both a Hilt Chardonnay and a
    Hilt Pinot Noir in your sheet should surface both, not silently
    pick whichever appears first.
    """
    q = normalize(query)
    matches = []

    for row in tasted_wines:
        producer = normalize(row.get("producer", ""))
        wine_name = normalize(row.get("wine_name", ""))
        combined = normalize(f"{row.get('producer', '')} {row.get('wine_name', '')}".strip())

        candidates = [producer, wine_name, combined]
        match = any(
            c and (q == c or q in c or c in q)
            for c in candidates
        )

        if match:
            matches.append({
                "tier": 1,
                "producer": row.get("producer"),
                "wine_name": row.get("wine_name"),
                "grape": row.get("grape"),
                "colour": row.get("colour"),
                "country": row.get("country"),
                "style": row.get("style"),
                "pairing": row.get("pairing"),
                "value_note": row.get("value_note"),
                "personal_take": row.get("personal_take"),
                "shop": row.get("shop"),
                "shop_price": row.get("shop_price"),
                "market_price_reference": row.get("market_price_reference"),
                "image_url": row.get("image_url"),
            })

    return matches if matches else None


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
                        "Respond with ONLY raw JSON, no markdown code fences, "
                        "no preamble, just the JSON object with keys: "
                        "style, pairing, drink_window."
                    ),
                }
            ],
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    raw_text = "".join(block.get("text", "") for block in data.get("content", []))

    # Strip markdown code fences if the model added them despite instructions
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        return {
            "tier": 3,
            "style": parsed.get("style"),
            "pairing": parsed.get("pairing"),
            "drink_window": parsed.get("drink_window"),
        }
    except (json.JSONDecodeError, ValueError):
        # If parsing fails for any reason, fall back to showing the raw text
        # rather than crashing the request.
        return {"tier": 3, "raw": raw_text}


# Keyword groups behind each broad food category. The synonym fallback
# checks pairings text against ALL keywords in the matched category,
# not just the literal category name -- e.g. "fish" should also catch
# pairing text that says "seafood" or "salmon", not just the word "fish".
CATEGORY_KEYWORDS = {
    "fish": ["fish", "seafood", "salmon", "squid", "shellfish", "sashimi"],
    "red meat": ["red meat", "beef", "steak", "yakiniku", "lamb", "braised meat", "stew"],
    "white meat": ["white meat", "chicken", "pork", "poultry", "yakitori"],
    "cheese": ["cheese", "aged cheese", "light bites"],
    "spicy": ["spicy", "curry", "chili", "asian"],
    "pasta": ["pasta", "tomato", "pizza"],
}


def filter_shop_picks(picks, colour=None, max_abv=None, food_term=None, food_synonyms=None, lang="en"):
    """
    Filter a shop's pick list by any combination of colour, ABV ceiling,
    and food term. All filters are optional and combine with AND logic.

    Food matching tries direct text match against pairings first; if no
    picks match directly, falls back to the food_synonyms list to find
    a broader category, then matches against ANY keyword in that
    category's keyword group (see CATEGORY_KEYWORDS) rather than just
    the literal category name -- e.g. "unagi" -> "fish" should also
    match pairing text that says "seafood" or "salmon".

    Returns: { "results": [...], "food_match_type": "direct" | "synonym" | None }
    """
    results = list(picks)
    food_match_type = None

    if colour:
        target = normalize(colour)
        results = [p for p in results if normalize(p.get("colour", "")) == target]

    if max_abv is not None:
        filtered = []
        for p in results:
            try:
                abv_value = float(p.get("abv", ""))
            except (ValueError, TypeError):
                continue  # skip rows with missing/invalid ABV rather than guessing
            if abv_value <= max_abv:
                filtered.append(p)
        results = filtered

    if food_term:
        pairings_key = f"pairings_{lang}"
        term = normalize(food_term)

        direct_matches = [p for p in results if term in normalize(p.get(pairings_key, ""))]

        if direct_matches:
            results = direct_matches
            food_match_type = "direct"
        else:
            # Fall back to the broader category via Food Synonyms
            broader_category = None
            for row in (food_synonyms or []):
                if normalize(row.get("food_term", "")) == term:
                    broader_category = normalize(row.get("broader_category", ""))
                    break

            if broader_category:
                keywords = CATEGORY_KEYWORDS.get(broader_category, [broader_category])
                synonym_matches = [
                    p for p in results
                    if any(kw in normalize(p.get(pairings_key, "")) for kw in keywords)
                ]
                if synonym_matches:
                    results = synonym_matches
                    food_match_type = "synonym"
                else:
                    results = []
            else:
                results = []

    return {"results": results, "food_match_type": food_match_type}


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
            "producer": "Bolla",
            "wine_name": "Soave Classico",
            "grape": "Garganega",
            "colour": "White",
            "country": "Italy",
            "style": "Light, crisp, dry white",
            "pairing": "Salmon, light seafood, easy drinking",
            "value_note": "Solid for the price, reliable supermarket pick",
            "personal_take": "Always a safe bet when nothing else stands out",
            "shop": "Kaldi",
            "shop_price": "998",
            "market_price_reference": "890",
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
