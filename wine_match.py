"""
Three-tier wine lookup logic.

Tier 1: specific wines you've personally tasted/rated
Tier 2: grape/style recognition + pairing rules (hardcoded common supermarket grapes)
Tier 3: LLM fallback for anything that misses both (logged for later cataloguing)
"""

import csv
import io
import json
import re
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
    resp.encoding = "utf-8"  # Google Sheets exports as UTF-8; don't let requests guess wrong
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def normalize(text):
    return text.strip().lower()


def find_tasted_match(producer, wine_name, tasted_wines):
    """Look for a Tasted Wines entry matching this producer + wine_name exactly.

    Shared by the /lookup enrichment path (comment fallback) and the
    food-pairing tier logic below -- both need to know whether a Shop
    Picks row is backed by an actual tasted record, and matching is
    exact on producer + wine_name (after normalize) in both cases.
    """
    p = normalize(producer)
    w = normalize(wine_name)
    for row in tasted_wines:
        if normalize(row.get("producer", "")) == p and normalize(row.get("wine_name", "")) == w:
            return row
    return None


def normalize_would_drink_again(raw):
    """Map whatever's typed in the Tasted Wines sheet to a canonical
    yes/neutral/no/trash, or None if blank/unrecognized. Unrecognized
    text is treated as unknown rather than raising -- a typo in the
    sheet shouldn't break ranking, it should just fall back to 'no
    opinion on file', the same as a blank cell.

    'trash' is a step below 'no' -- 'no' means "not for me", 'trash'
    means "actively bad, don't buy this regardless of context"."""
    if not raw:
        return None
    v = normalize(raw)
    if v in ("yes", "y"):
        return "yes"
    if v in ("neutral", "maybe"):
        return "neutral"
    if v in ("no", "n"):
        return "no"
    if v in ("trash", "avoid", "terrible"):
        return "trash"
    return None


def extract_price_number(text):
    """Pull a rough numeric price out of a free-text field like '~¥1000'
    or '1000-1500' (takes the first number found). Returns None if
    nothing parseable is there, rather than guessing."""
    if not text:
        return None
    match = re.search(r"[\d,]+", str(text))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


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


def _extract_json_object(raw_text):
    """Pull a JSON object out of raw model text, tolerating markdown fences
    and/or narration before/after it (common when the model has used a
    tool before its final answer)."""
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        return cleaned[brace_start:brace_end + 1]
    return cleaned


def _parse_tier3_fields(raw_text):
    """Parse a Tier 3 JSON response into the shared field set. Returns
    None if the text can't be parsed as JSON at all."""
    try:
        parsed = json.loads(_extract_json_object(raw_text))
    except (json.JSONDecodeError, ValueError):
        return None
    return {
        "producer": parsed.get("producer"),
        "wine_name": parsed.get("wine_name"),
        "grape": parsed.get("grape"),
        "country": parsed.get("country"),
        "style": parsed.get("style"),
        "pairing": parsed.get("pairing"),
        "drink_window": parsed.get("drink_window"),
        "notes": parsed.get("notes"),
    }


# Shared JSON-shape instruction for both Tier 3 variants -- keeping this
# in one place means the quick guess and the deep search always return
# fields the frontend can render identically.
_TIER3_JSON_INSTRUCTION = (
    "Respond with ONLY raw JSON, no markdown fences, no preamble. This "
    "applies even after you use a tool -- your final message must contain "
    "nothing but the JSON object itself, with no transition text like "
    "'Now I need to...' or 'Based on my search...' before it. "
    "Keys: producer (best guess or null), wine_name (best guess of the "
    "specific bottling or null), grape (or null), country (or null), "
    "style, pairing, drink_window, notes (1-2 sentences on anything "
    "distinctive worth knowing -- region reputation, winemaking style, "
    "etc, or null)."
)


def tier3_quick_guess(query, api_key):
    """Fast, cheap Tier 3 guess -- the model's own knowledge only, no web
    search. This is the default Tier 3 path so an ordinary miss doesn't
    incur a search fee; it's good enough for anything reasonably well
    known. Returned dict includes guess_type='quick' so the frontend can
    offer the deeper search as an opt-in next step.
    """
    log_miss(query)

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5",
            "max_tokens": 500,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Wine: '{query}'. Give the most informative answer you can "
                        "from what you already know. If you recognize the specific "
                        "producer and/or wine, name it. Give real substance, not "
                        "generic filler: what actually makes this wine or region "
                        "distinctive, a concrete food pairing (a specific dish, not "
                        "just 'red meat'), and typical price range if you know it. "
                        "If you don't recognize it specifically, say so honestly via "
                        "null fields rather than inventing detail. No marketing "
                        "tone.\n\n" + _TIER3_JSON_INSTRUCTION
                    ),
                }
            ],
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    raw_text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )

    fields = _parse_tier3_fields(raw_text)
    if fields is None:
        return {"tier": 3, "guess_type": "quick", "raw": raw_text}
    return {"tier": 3, "guess_type": "quick", **fields}


def tier3_deep_search(query, api_key):
    """Slower, costlier Tier 3 lookup that uses live web search for
    grounded specifics. Not called automatically -- only when a person
    explicitly asks for more than the quick guess gave them, since web
    search carries a per-call cost on top of tokens.
    """
    log_miss(query)  # still worth logging -- repeat deep-search queries are strong "add to Tasted Wines" candidates

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5",
            "max_tokens": 1024,
            "tools": [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
            ],
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Someone searched for the wine '{query}' and wants a deeper, "
                        "web-grounded answer beyond a quick guess. Use web search to "
                        "confirm specifics (producer, region, grape, typical style, "
                        "price) rather than relying on memory alone -- this matters "
                        "most for smaller or less famous producers. If you can "
                        "identify the specific producer and/or wine, name it. Give "
                        "real substance, not generic filler: what actually makes "
                        "this wine or region distinctive, a concrete food pairing "
                        "(a specific dish, not just a food category), and typical "
                        "price range if known. No marketing tone.\n\n"
                        + _TIER3_JSON_INSTRUCTION
                    ),
                }
            ],
        },
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    raw_text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )

    fields = _parse_tier3_fields(raw_text)
    if fields is None:
        return {"tier": 3, "guess_type": "search", "raw": raw_text}
    return {"tier": 3, "guess_type": "search", **fields}


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


def get_broader_category(term, food_synonyms):
    """Look up the broader food category for a term via the Food Synonyms
    sheet (e.g. 'unagi' -> 'fish'). Returns None if the term isn't a
    known synonym."""
    for row in (food_synonyms or []):
        if normalize(row.get("food_term", "")) == term:
            return normalize(row.get("broader_category", ""))
    return None


def compute_food_match(pick, food_term, food_synonyms, lang, tasted_wines):
    """
    Determine whether a Shop Picks row matches a food term, and if so,
    at what confidence tier. Three tiers, strongest first:

    Tier A -- Personally paired: the pick links to a Tasted Wines row
    (same producer + wine_name), and that row's OWN pairing text
    mentions the food term or its broader category. This means the
    pairing claim is backed by an actual tasting, not just a note
    written while curating the shop list.

    Tier B -- Shop pairing note: the pick's own pairings_{lang} text
    directly mentions the food term. No tasted backing, but it's a
    direct textual match rather than a category guess.

    Tier C -- Closest match: only found via the food_synonyms category
    fallback against the pick's own pairing text (e.g. "unagi" not
    mentioned directly, but the text says "salmon" and both map to
    the "fish" category).

    Returns (matches: bool, tier: 'A' | 'B' | 'C' | None).
    """
    if not food_term:
        return (True, None)

    term = normalize(food_term)
    pairings_key = f"pairings_{lang}"
    pick_text = normalize(pick.get(pairings_key, ""))
    broader_category = get_broader_category(term, food_synonyms)
    keywords = CATEGORY_KEYWORDS.get(broader_category, [broader_category]) if broader_category else []

    tasted_match = find_tasted_match(pick.get("producer_en", ""), pick.get("wine_name_en", ""), tasted_wines) if tasted_wines else None
    tasted_pairing_text = normalize(tasted_match.get("pairing", "")) if tasted_match else ""

    if tasted_match:
        if term in tasted_pairing_text:
            return (True, "A")
        if broader_category and any(kw in tasted_pairing_text for kw in keywords):
            return (True, "A")

    if term in pick_text:
        return (True, "B")

    if broader_category and any(kw in pick_text for kw in keywords):
        return (True, "C")

    return (False, None)


_TIER_RANK = {"A": 0, "B": 1, "C": 2, None: 3}
_RECOMMEND_RANK = {"yes": 0, "neutral": 1, None: 2, "no": 3, "trash": 4}


def filter_shop_picks(picks, colour=None, max_abv=None, food_term=None, food_synonyms=None, lang="en", tasted_wines=None):
    """
    Filter a shop's pick list by any combination of colour, ABV ceiling,
    and food term, then rank what's left by:

      1. Food-match confidence tier (A/B/C -- see compute_food_match).
         No-op if food_term isn't given, since there's no tier to rank by.
      2. would_drink_again pulled from a linked Tasted Wines row
         (yes > neutral > unknown > no). A "no" wine is never hidden --
         if it's the only match, it still shows -- it just sinks to the
         bottom and gets flagged rather than silently ranked to the top
         on confidence alone.
      3. Price ascending, parsed from price_range where possible, as
         the final tiebreak between otherwise-equal picks.

    Each returned pick is annotated with two extra keys:
      food_match_tier: 'A' | 'B' | 'C' | None
      would_drink_again: 'yes' | 'neutral' | 'no' | None

    Returns: { "results": [...], "food_match_type": "direct" | "synonym" | None }
    """
    results = list(picks)

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

    annotated = []
    for p in results:
        matches, tier = compute_food_match(p, food_term, food_synonyms, lang, tasted_wines)
        if not matches:
            continue
        row = dict(p)  # copy -- these rows are cached across requests, never mutate in place
        row["food_match_tier"] = tier

        tasted_match = find_tasted_match(p.get("producer_en", ""), p.get("wine_name_en", ""), tasted_wines) if tasted_wines else None
        row["would_drink_again"] = normalize_would_drink_again(tasted_match.get("would_drink_again")) if tasted_match else None

        price_source = p.get("price_range")
        row["_price_sort"] = extract_price_number(price_source)
        if row["_price_sort"] is None and tasted_match:
            # Fall back to the tasted wine's own shop_price only if this
            # pick's own price_range didn't give us a usable number.
            row["_price_sort"] = extract_price_number(tasted_match.get("shop_price"))

        annotated.append(row)

    results = annotated

    food_match_type = None
    if food_term:
        if any(p["food_match_tier"] in ("A", "B") for p in results):
            food_match_type = "direct"
        elif any(p["food_match_tier"] == "C" for p in results):
            food_match_type = "synonym"

    results.sort(key=lambda p: (
        _TIER_RANK.get(p.get("food_match_tier"), 3),
        _RECOMMEND_RANK.get(p.get("would_drink_again"), 2),
        p["_price_sort"] if p["_price_sort"] is not None else float("inf"),
    ))

    for p in results:
        p.pop("_price_sort", None)

    return {"results": results, "food_match_type": food_match_type}


def log_miss(query):
    """Append unmatched queries to a CSV so they can be reviewed and promoted
    into Tier 1 or Tier 2 later."""
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([query])


def lookup_wine(query, tasted_wines, recognition_rows, pairing_rows, llm_api_key):
    """Main entry point: run the three tiers in order, return first hit.

    Not used by app.py (which inlines this logic per-route so it can
    also expose /lookup/deep) -- kept as a standalone convenience
    function for scripts or a REPL. Uses the cheap quick guess, not the
    web-search deep search, to match /lookup's default behavior.
    """
    result = tier1_lookup(query, tasted_wines)
    if result:
        return result

    result = tier2_lookup(query, recognition_rows, pairing_rows)
    if result and result.get("pairing"):
        return result

    return tier3_quick_guess(query, llm_api_key)


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
