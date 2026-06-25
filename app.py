from flask import Flask, request, jsonify
import os
from difflib import get_close_matches

app = Flask(__name__)

# -------------------
# DATA
# -------------------

wines = {
    "Barbera d'Asti": {
        "region": "Piemonte",
        "acidity": "High",
        "body": "Medium",
        "descriptors": ["Cherry", "Plum", "Violet"]
    },

    "Fiano": {
        "region": "Campania",
        "acidity": "High",
        "body": "Medium",
        "descriptors": ["Pear", "Peach", "Hazelnut"]
    },

    "Soave": {
        "region": "Veneto",
        "acidity": "Medium-High",
        "body": "Light-Medium",
        "descriptors": ["Apple", "Pear", "Almond"]
    }
}

# -------------------
# ENGINE FUNCTIONS
# -------------------

def normalize(text):
    if not text:
        return ""
    return text.strip().lower().replace("’", "'")


def find_best_match(query):
    keys = list(wines.keys())
    match = get_close_matches(query, keys, n=1, cutoff=0.4)

    if match:
        return match[0]

    return None


def get_shelf_view(query):
    q = normalize(query)

    results = []

    for name, w in wines.items():
        if (
            q in normalize(name)
            or q in normalize(w["region"])
            or q in normalize(" ".join(w["descriptors"]))
        ):
            results.append({
                "name": name,
                **w
            })

    return results


# -------------------
# ROUTES
# -------------------

@app.route("/")
def home():
    return """
    <h1>WineResearcher</h1>
    <form action="/wine" method="get">
        <input name="name" placeholder="Enter wine name">
        <button type="submit">Search</button>
    </form>
    """


@app.route("/wine")
def wine():
    name = request.args.get("name", "")

    match = find_best_match(name)

    if not match:
        return {"error": "Wine not found"}

    w = wines[match]

    return {
        "name": match,
        "region": w["region"],
        "acidity": w["acidity"],
        "body": w["body"],
        "descriptors": w["descriptors"]
    }


@app.route("/supermarket")
def supermarket():
    query = request.args.get("query", "")

    if not query:
        return {
            "message": "Try: Bolla, Soave, Barbera, or anything on the shelf"
        }

    results = get_shelf_view(query)

    if not results:
        return {
            "query": query,
            "message": "No wines found"
        }

    # simple recommendation logic
    recommendation = "Pick the first result if unsure."

    if len(results) > 1:
        recommendation = "Lower acidity = smoother. Higher acidity = fresher taste."

    return {
        "query": query,
        "count": len(results),
        "shelf": results,
        "recommendation": recommendation
    }


# -------------------
# RUN SERVER
# -------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)