"""入口点: uv run python -m web_app.server"""

from web_app.app import app

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)
