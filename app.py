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
    TIER1_CSV_URL,
    TIER2_RECOGNITION_CSV_URL,
    TIER2_PAIRING_CSV_URL,
    SHOP_PICKS_CSV_URL,
)

app = Flask(__name__)
CORS(app)  # allows your WordPress/static frontend (different domain) to call this API

LLM_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # set this in Render's dashboard, never hardcode it

# --- Simple in-memory cache for the Sheets data ---------------------------
# Avoids re-downloading the CSVs on every single request.
# Refreshes automatically once the cache is older than CACHE_SECONDS.

CACHE_SECONDS = 60 * 60  # 1 hour
_cache = {"loaded_at": 0, "tasted_wines": [], "recognition_rows": [], "pairing_rows": [], "shop_picks": []}


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
                "shop": "Kaldi",
                "shop_price": "998",
                "market_price_reference": "890",
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
                {"shop": "Kaldi", "producer": "Bolla", "wine_name": "Soave",
                 "why_pick_this": "Safe, easy white, always reliable here", "price_range": "~¥1000"},
            ]
        else:
            _cache["tasted_wines"] = fetch_csv(TIER1_CSV_URL)
            _cache["recognition_rows"] = fetch_csv(TIER2_RECOGNITION_CSV_URL)
            _cache["pairing_rows"] = fetch_csv(TIER2_PAIRING_CSV_URL)
            _cache["shop_picks"] = fetch_csv(SHOP_PICKS_CSV_URL)
        _cache["loaded_at"] = time.time()
    return _cache["tasted_wines"], _cache["recognition_rows"], _cache["pairing_rows"], _cache["shop_picks"]


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

    tasted_wines, recognition_rows, pairing_rows, _ = get_data()

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


@app.route("/shop", methods=["GET"])
def shop():
    """
    Example request:  GET /shop?name=Kaldi

    Returns the curated list of recommended wines for that shop.
    This is a personal shortlist, NOT a full inventory -- only wines
    you've specifically chosen to recommend show up here.

    Returns JSON like:
    { "shop": "Kaldi", "picks": [ { "producer": "...", "wine_name": "...",
      "why_pick_this": "...", "price_range": "..." }, ... ] }
    """
    shop_name = request.args.get("name", "").strip()
    if not shop_name:
        return jsonify({"error": "Missing 'name' parameter"}), 400

    _, _, _, shop_picks = get_data()

    matches = [
        {
            "producer": row.get("producer"),
            "wine_name": row.get("wine_name"),
            "why_pick_this": row.get("why_pick_this"),
            "price_range": row.get("price_range"),
        }
        for row in shop_picks
        if normalize(row.get("shop", "")) == normalize(shop_name)
    ]

    if not matches:
        return jsonify({
            "shop": shop_name,
            "picks": [],
            "note": "No picks catalogued for this shop yet.",
        })

    return jsonify({"shop": shop_name, "picks": matches})


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

    tasted_wines, _, _, _ = get_data()

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
