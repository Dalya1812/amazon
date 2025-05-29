# app.py  ─ Dalya Deal Bot API & Frontend
import os, logging
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from clean_keyword_deal_bot import get_amazon_affiliate_links
from functools import lru_cache
import time

# ── 2. logging  ────────────────────────
log_level = "INFO"
logging.basicConfig(
    level=log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("dalya.dealbot.api")

# ── 3. Flask setup  ────────────────────
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # reverse-proxy friendly
FRONTEND_DIR = os.path.dirname(__file__)

# Cache configuration
CACHE_TIMEOUT = 3600  # 1 hour cache timeout

# Cache for search results
@lru_cache(maxsize=100)
def cached_search(keyword: str) -> dict:
    """Cached version of get_amazon_affiliate_links with timeout."""
    return get_amazon_affiliate_links(keyword)

# ── 4. Routes  ─────────────────────────
@app.route("/")
def index():
    """Serve the static index.html"""
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.get("/api/search")
def api_search():
    """Return Lego/Amazon deals as JSON"""
    kw = request.args.get("q", "").strip()
    if not kw:
        log.warning("query param 'q' missing or empty")
        return jsonify(error="Empty query"), 400

    try:
        # Use cached results
        result = cached_search(kw)
        log.debug("API response: %s", result)
        return jsonify(result)
    except Exception as e:
        log.error("Error in search: %s", str(e))
        return jsonify(error="Search failed"), 500

# ── 5. Run server (only if called directly) ─
if __name__ == "__main__":
    host = "0.0.0.0"
    port = 8080
    log.info("🚀 Starting Dalya Deal Bot on %s:%s (LOG_LEVEL=%s)", host, port, log_level)
    app.run(host=host, port=port, debug=True)
