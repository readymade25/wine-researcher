from flask import Flask, request
import os

app = Flask(__name__)

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
    name = request.args.get("name")

    if name in wines:
        w = wines[name]

        return f"""
        <h2>{name}</h2>
        <p><b>Region:</b> {w['region']}</p>
        <p><b>Acidity:</b> {w['acidity']}</p>
        <p><b>Body:</b> {w['body']}</p>
        <p><b>Descriptors:</b> {', '.join(w['descriptors'])}</p>
        """

    return "<h2>Wine not found</h2>"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)