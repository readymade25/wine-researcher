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
    TIER1_CSV_URL,
    TIER2_RECOGNITION_CSV_URL,
    TIER2_PAIRING_CSV_URL,
)

app = Flask(__name__)
CORS(app)  # allows your WordPress/static frontend (different domain) to call this API

LLM_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # set this in Render's dashboard, never hardcode it

# --- Simple in-memory cache for the Sheets data ---------------------------
# Avoids re-downloading the CSVs on every single request.
# Refreshes automatically once the cache is older than CACHE_SECONDS.

CACHE_SECONDS = 60 * 60  # 1 hour
_cache = {"loaded_at": 0, "tasted_wines": [], "recognition_rows": [], "pairing_rows": []}


def get_data():
    age = time.time() - _cache["loaded_at"]
    if age > CACHE_SECONDS or not _cache["loaded_at"]:
        if os.environ.get("USE_TEST_DATA"):
            # Local testing only — bypasses real network calls to Google Sheets.
            _cache["tasted_wines"] = [{
                "name": "Bolla Soave Classico",
                "style": "Light, crisp, dry white",
                "pairing": "Salmon, light seafood, easy drinking",
                "value_note": "Solid for the price, reliable supermarket pick",
                "personal_take": "Always a safe bet when nothing else stands out",
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
        else:
            _cache["tasted_wines"] = fetch_csv(TIER1_CSV_URL)
            _cache["recognition_rows"] = fetch_csv(TIER2_RECOGNITION_CSV_URL)
            _cache["pairing_rows"] = fetch_csv(TIER2_PAIRING_CSV_URL)
        _cache["loaded_at"] = time.time()
    return _cache["tasted_wines"], _cache["recognition_rows"], _cache["pairing_rows"]


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

    tasted_wines, recognition_rows, pairing_rows = get_data()

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


@app.route("/health", methods=["GET"])
def health():
    """Simple endpoint to confirm the API is alive — useful for Render's health checks."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Local testing only. Render will use gunicorn instead of this.
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
