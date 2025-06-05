# app.py  ─ Dalya Deal Bot API & Frontend
import os, logging
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from clean_keyword_deal_bot import get_amazon_affiliate_links
from functools import lru_cache
import time
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timedelta

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
TOP_DEALS_CACHE = {}
TOP_DEALS_LOCK = threading.Lock()
TOP_DEALS_LAST_UPDATE = 0
TOP_DEALS_UPDATE_INTERVAL = 300  # Update every 5 minutes

def update_top_deals_cache():
    """Update top deals cache in background."""
    global TOP_DEALS_CACHE, TOP_DEALS_LAST_UPDATE
    current_time = time.time()
    
    with TOP_DEALS_LOCK:
        if current_time - TOP_DEALS_LAST_UPDATE < TOP_DEALS_UPDATE_INTERVAL:
            return TOP_DEALS_CACHE
        
        categories = ['electronics', 'home', 'fashion']
        all_deals = []
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_category = {
                executor.submit(get_amazon_affiliate_links, category, max_results=3): category
                for category in categories
            }
            
            for future in future_to_category:
                try:
                    deals = future.result()
                    if deals and 'deals' in deals:
                        for deal in deals['deals']:
                            deal['category'] = future_to_category[future]
                            all_deals.append(deal)
                except Exception as e:
                    log.error(f"Error fetching deals for {future_to_category[future]}: {e}")
        
        # Sort by relevance score and take top 3
        all_deals.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
        TOP_DEALS_CACHE = {"deals": all_deals[:3]}
        TOP_DEALS_LAST_UPDATE = current_time
        return TOP_DEALS_CACHE

# Start background update of top deals
def start_background_updates():
    def update_loop():
        while True:
            try:
                update_top_deals_cache()
                time.sleep(TOP_DEALS_UPDATE_INTERVAL)
            except Exception as e:
                log.error(f"Error in background update: {e}")
                time.sleep(60)  # Wait a minute before retrying

    thread = threading.Thread(target=update_loop, daemon=True)
    thread.start()

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

    result = get_amazon_affiliate_links(kw)
    log.debug("API response: %s", result)
    return jsonify(result)

@app.route('/api/top-deals')
def api_top_deals():
    try:
        # Get cached top deals or update if needed
        deals = update_top_deals_cache()
        return jsonify(deals)
    except Exception as e:
        log.error(f"Top deals error: {e}")
        return jsonify({"error": str(e)}), 500

# Start background updates when the app starts
start_background_updates()

# ── 5. Run server (only if called directly) ─
if __name__ == "__main__":
    host = "0.0.0.0"
    port = 8090
    log.info("🚀 Starting Dalya Deal Bot on %s:%s (LOG_LEVEL=%s)", host, port, log_level)
    app.run(host=host, port=port, debug=True)
