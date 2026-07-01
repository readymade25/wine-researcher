"""
Flask API for the three-tier wine lookup tool.

This is the deployable version: it takes the matching logic from
wine_match.py and exposes it as an HTTP endpoint that Render can run,
and that your frontend (WordPress, static page, whatever) can call.

Run locally with:   python app.py
Deploy to Render with a start command like:  gunicorn app:app
"""

import os
import time
from flask import Flask, request, jsonify
from flask_cors import CORS

from wine_match import (
    fetch_csv,
    tier1_lookup,
    tier2_lookup,
    tier3_llm_fallback,
    log_miss,
    normalize,
    filter_shop_picks,
    find_tasted_match,
    TIER1_CSV_URL,
    TIER2_RECOGNITION_CSV_URL,
    TIER2_PAIRING_CSV_URL,
    SHOP_PICKS_CSV_URL,
    FOOD_SYNONYMS_CSV_URL,
)

app = Flask(__name__)
CORS(app)  # allows your WordPress/static frontend (different domain) to call this API

LLM_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # set this in Render's dashboard, never hardcode it

# --- Simple in-memory cache for the Sheets data ---------------------------
# Avoids re-downloading the CSVs on every single request.
# Refreshes automatically once the cache is older than CACHE_SECONDS.

CACHE_SECONDS = 60 * 60  # 1 hour
_cache = {
    "loaded_at": 0,
    "tasted_wines": [],
    "recognition_rows": [],
    "pairing_rows": [],
    "shop_picks": [],
    "food_synonyms": [],
}


def get_data():
    age = time.time() - _cache["loaded_at"]
    if age > CACHE_SECONDS or not _cache["loaded_at"]:
        if os.environ.get("USE_TEST_DATA"):
            # Local testing only — bypasses real network calls to Google Sheets.
            _cache["tasted_wines"] = [{
                "producer": "Bolla",
                "wine_name": "Soave Classico",
                "grape": "Garganega",
                "colour": "White",
                "country": "Italy",
                "style": "Light, crisp, dry white",
                "pairing": "Salmon, light seafood, easy drinking",
                "value_note": "Solid for the price, reliable supermarket pick",
                "personal_take": "Always a safe bet when nothing else stands out",
                "would_drink_again": "Yes",
                "shop": "Kaldi",
                "shop_price": "998",
                "market_price_reference": "890",
                "image_url": "",
            }, {
                "producer": "Allegrini",
                "wine_name": "Amarone",
                "grape": "Corvina blend",
                "colour": "Red",
                "country": "Italy",
                "style": "Full-bodied, dried-fruit red",
                "pairing": "Aged cheese, braised meats, rich stews",
                "value_note": "Pricey for what it is",
                "personal_take": "Too heavy for most occasions",
                "would_drink_again": "No",
                "shop": "Kaldi",
                "shop_price": "2500",
                "market_price_reference": "2400",
                "image_url": "",
            }]
            _cache["recognition_rows"] = [
                {"keyword": "soave", "grape": "Garganega"},
                {"keyword": "chianti", "grape": "Sangiovese"},
                {"keyword": "rioja", "grape": "Tempranillo"},
            ]
            _cache["pairing_rows"] = [
                {"grape": "Garganega", "style": "Light, high-acid, dry white",
                 "pairing": "Delicate fish, light apps", "drink_window": "Drink young"},
                {"grape": "Sangiovese", "style": "Medium-bodied, high-acid red",
                 "pairing": "Tomato-based pasta, pizza, grilled meats", "drink_window": "Drink now"},
            ]
            _cache["shop_picks"] = [
                {"shop": "Kaldi", "producer_en": "Bolla", "producer_jp": "ボッラ",
                 "wine_name_en": "Soave Classico", "wine_name_jp": "ソアーヴェ",
                 "colour": "White", "grape": "Garganega", "country": "Italy",
                 "abv": "12.5", "sweetness": "Dry",
                 "pairings_en": "Salmon, light seafood, easy drinking",
                 "pairings_jp": "サーモン、軽い魚介類",
                 "comment_en": "", "comment_jp": "",
                 "confidence": "tasted", "price_range": "~¥1000"},
                {"shop": "Kaldi", "producer_en": "Allegrini", "producer_jp": "アッレグリーニ",
                 "wine_name_en": "Amarone", "wine_name_jp": "アマローネ",
                 "colour": "Red", "grape": "Corvina blend", "country": "Italy",
                 "abv": "16.5", "sweetness": "Dry",
                 "pairings_en": "Aged cheese, braised meats, rich stews",
                 "pairings_jp": "熟成チーズ、煮込み料理",
                 "comment_en": "", "comment_jp": "",
                 "confidence": "tasted", "price_range": "~¥2500"},
                {"shop": "Kaldi", "producer_en": "Pieropan", "producer_jp": "ピエロパン",
                 "wine_name_en": "Soave Classico", "wine_name_jp": "ソアーヴェ",
                 "colour": "White", "grape": "Garganega", "country": "Italy",
                 "abv": "12", "sweetness": "Dry",
                 "pairings_en": "Great with light seafood and salads",
                 "pairings_jp": "軽い魚介類やサラダに合う",
                 "comment_en": "Never tasted, just a shop note", "comment_jp": "",
                 "confidence": "", "price_range": "~¥1400"},
            ]
            _cache["food_synonyms"] = [
                {"food_term": "unagi", "broader_category": "fish"},
                {"food_term": "sukiyaki", "broader_category": "red meat"},
            ]
        else:
            _cache["tasted_wines"] = fetch_csv(TIER1_CSV_URL)
            _cache["recognition_rows"] = fetch_csv(TIER2_RECOGNITION_CSV_URL)
            _cache["pairing_rows"] = fetch_csv(TIER2_PAIRING_CSV_URL)
            _cache["shop_picks"] = fetch_csv(SHOP_PICKS_CSV_URL)
            _cache["food_synonyms"] = fetch_csv(FOOD_SYNONYMS_CSV_URL)
        _cache["loaded_at"] = time.time()
    return (
        _cache["tasted_wines"],
        _cache["recognition_rows"],
        _cache["pairing_rows"],
        _cache["shop_picks"],
        _cache["food_synonyms"],
    )


@app.route("/lookup", methods=["GET"])
def lookup():
    """
    Example request:  GET /lookup?wine=Bolla%20Soave

    Returns JSON like:
    { "tier": 1, "name": "...", "style": "...", "pairing": "...", ... }
    """
    query = request.args.get("wine", "").strip()
    if not query:
        return jsonify({"error": "Missing 'wine' parameter"}), 400

    tasted_wines, recognition_rows, pairing_rows, _, _ = get_data()

    tier1_matches = tier1_lookup(query, tasted_wines)
    if tier1_matches:
        if len(tier1_matches) == 1:
            return jsonify(tier1_matches[0])
        return jsonify({"tier": 1, "matches": tier1_matches})

    result = tier2_lookup(query, recognition_rows, pairing_rows)
    if result and result.get("pairing"):
        return jsonify(result)

    if not LLM_API_KEY:
        log_miss(query)
        return jsonify({
            "tier": None,
            "error": "No match found, and LLM fallback is not configured.",
        }), 404

    result = tier3_llm_fallback(query, LLM_API_KEY)
    return jsonify(result)


def truncate_take(text, max_chars=80):
    if not text:
        return None
    text = text.strip()
    if len(text) <= max_chars:
        return text
    # Cut at the last full word before the limit, add an ellipsis
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut + "…"


@app.route("/shop", methods=["GET"])
def shop():
    """
    Example request:
      GET /shop?name=Kaldi&lang=en&colour=Red&max_abv=14&food=sukiyaki

    All filters (colour, max_abv, food) are optional and combine.
    lang defaults to "en". Returns the curated list of recommended
    wines for that shop -- a personal shortlist, NOT a full inventory.

    Results are ranked (not just filtered):
      1. Food-match confidence tier, when a food filter is given --
         "personal" (tasted and the pairing is backed by that tasting)
         beats "shop_note" (only the shop-list text says so) beats
         "closest" (matched via a broader category, not directly).
      2. would_drink_again, pulled from a linked Tasted Wines row --
         yes > neutral > unknown > no. A "no" wine is never hidden,
         even if it's the only match -- it just sinks to the bottom
         and is flagged rather than silently ranked to the top on
         confidence alone.
      3. Price ascending, as the final tiebreak between otherwise
         equally-ranked picks.

    Each pick's "comment" field is either: the hand-written comment_en/jp
    if present, or a truncated version of the matching Tasted Wines
    personal_take if one exists, or null if neither is available.
    """
    shop_name = request.args.get("name", "").strip()
    if not shop_name:
        return jsonify({"error": "Missing 'name' parameter"}), 400

    lang = request.args.get("lang", "en").strip().lower()
    if lang not in ("en", "jp"):
        lang = "en"

    colour = request.args.get("colour", "").strip() or None
    food_term = request.args.get("food", "").strip() or None

    max_abv_raw = request.args.get("max_abv", "").strip()
    max_abv = None
    if max_abv_raw:
        try:
            max_abv = float(max_abv_raw)
        except ValueError:
            return jsonify({"error": "max_abv must be a number"}), 400

    tasted_wines, _, _, shop_picks, food_synonyms = get_data()

    shop_rows = [row for row in shop_picks if normalize(row.get("shop", "")) == normalize(shop_name)]

    filtered = filter_shop_picks(
        shop_rows,
        colour=colour,
        max_abv=max_abv,
        food_term=food_term,
        food_synonyms=food_synonyms,
        lang=lang,
        tasted_wines=tasted_wines,
    )

    picks_out = []
    for row in filtered["results"]:
        # Producer/wine name fall back to English if the _jp version is
        # blank -- unlike comment, a missing name would break the list
        # entirely, so showing the English name is better than nothing
        # while Japanese translations are still being filled in.
        producer = row.get(f"producer_{lang}") or row.get("producer_en")
        wine_name = row.get(f"wine_name_{lang}") or row.get("wine_name_en")

        comment = row.get(f"comment_{lang}")
        comment_source = "shop" if comment else None
        tasted_match = None
        if not comment and lang == "en":
            tasted_match = find_tasted_match(
                row.get("producer_en", ""), row.get("wine_name_en", ""), tasted_wines
            )
            if tasted_match:
                comment = truncate_take(tasted_match.get("personal_take"))
                comment_source = "tasted"
        # Note: Tasted Wines isn't bilingual yet, so for lang=jp there's no
        # Japanese personal_take to fall back to. If comment_jp is blank,
        # we simply show no comment rather than silently showing English
        # text under a Japanese-language response.

        picks_out.append({
            "producer": producer,
            "wine_name": wine_name,
            "colour": row.get("colour"),
            "grape": row.get("grape"),
            "country": row.get("country"),
            "abv": row.get("abv"),
            "sweetness": row.get("sweetness"),
            "comment": comment,
            # comment_source tells the frontend where "comment" came from:
            # 'shop' = your own comment_en/jp written for this shop listing,
            # 'tasted' = no shop comment existed, this is a fallback from
            # your Tasted Wines personal_take instead. This matters because
            # when a wine also has a full Tasted Wines match, the detail
            # view shows the richer Tasted Wines card instead of this one --
            # a 'shop' comment needs to be surfaced there too, since it
            # won't otherwise appear anywhere (a 'tasted' comment doesn't,
            # since the richer card already shows the full personal_take).
            "comment_source": comment_source,
            "confidence": row.get("confidence"),
            "price_range": row.get("price_range"),
            # food_match_tier is only meaningful when a food filter was
            # applied ('personal' = tasted wine's own pairing backs this
            # claim, 'shop_note' = only the shop-list text says so,
            # 'closest' = matched via broader category, not directly).
            # None means no food filter was active.
            "food_match_tier": {"A": "personal", "B": "shop_note", "C": "closest"}.get(row.get("food_match_tier")),
            # would_drink_again reflects a linked Tasted Wines opinion --
            # 'yes' / 'neutral' / 'no', or None if this pick has never
            # been personally tasted (no opinion on file, not a rating).
            "would_drink_again": row.get("would_drink_again"),
        })

    if not picks_out:
        return jsonify({
            "shop": shop_name,
            "picks": [],
            "food_match_type": filtered["food_match_type"],
            "note": "No picks match these filters yet.",
        })

    return jsonify({
        "shop": shop_name,
        "picks": picks_out,
        "food_match_type": filtered["food_match_type"],
    })


@app.route("/compare", methods=["GET"])
def compare():
    """
    Example request:
      GET /compare?wines=Bolla|Soave,Felsina|Rancia Chianti Classico Riserva

    Each wine is given as "producer|wine_name", multiple wines separated by commas.
    This is meant for comparing specific known wines (e.g. from a Shop Mode
    list), not free-text search -- so matching is exact on producer + wine_name
    rather than the loose substring matching /lookup uses.

    Returns JSON like:
    {
      "results": [
        { "producer": "...", "wine_name": "...", "style": "...", ... },
        { "producer": "...", "wine_name": "...", "error": "Not found" }
      ]
    }
    """
    raw = request.args.get("wines", "").strip()
    if not raw:
        return jsonify({"error": "Missing 'wines' parameter"}), 400

    requested = [w.strip() for w in raw.split(",") if w.strip()]

    MAX_COMPARE = 5
    if len(requested) > MAX_COMPARE:
        return jsonify({
            "error": f"Too many wines requested. Maximum is {MAX_COMPARE}."
        }), 400

    tasted_wines, _, _, _, _ = get_data()

    results = []
    for item in requested:
        if "|" not in item:
            results.append({"input": item, "error": "Expected format: producer|wine_name"})
            continue

        producer_query, wine_name_query = item.split("|", 1)
        producer_query = normalize(producer_query.strip())
        wine_name_query = normalize(wine_name_query.strip())

        found = None
        for row in tasted_wines:
            if (normalize(row.get("producer", "")) == producer_query
                    and normalize(row.get("wine_name", "")) == wine_name_query):
                found = row
                break

        if found:
            results.append({
                "producer": found.get("producer"),
                "wine_name": found.get("wine_name"),
                "grape": found.get("grape"),
                "colour": found.get("colour"),
                "country": found.get("country"),
                "style": found.get("style"),
                "pairing": found.get("pairing"),
                "value_note": found.get("value_note"),
                "personal_take": found.get("personal_take"),
                "shop": found.get("shop"),
                "shop_price": found.get("shop_price"),
                "market_price_reference": found.get("market_price_reference"),
                "image_url": found.get("image_url"),
            })
        else:
            results.append({
                "producer": producer_query,
                "wine_name": wine_name_query,
                "error": "Not found in tasted wines",
            })

    return jsonify({"results": results})


@app.route("/health", methods=["GET"])
def health():
    """Simple endpoint to confirm the API is alive — useful for Render's health checks."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Local testing only. Render will use gunicorn instead of this.
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
