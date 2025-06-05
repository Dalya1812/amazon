"""
Dalya Amazon Deal Bot  –  scrape Slickdeals RSS, return Amazon links with tag
---------------------------------------------------------------------------
* primary: deals from Slickdeals (store=amazon.com)
* fallback A: top Amazon product from Rainforest API (needs RAINFOREST_KEY)
* fallback B: plain Amazon search link
"""

from __future__ import annotations
import logging, re, os, html, requests, feedparser, threading
from urllib.parse import (
    urlparse, parse_qs, urlencode, urlunparse,
    quote_plus, unquote,
)
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from time import time

# ───────────────────────────────  settings  ────────────────────────────────
DEFAULT_TAG   = "2025050f-20"          # ←  תג-השותף שלך
MAX_RESULTS   = 5                      # ←  כמה דילים להחזיר ללקוח
HEADERS       = {"User-Agent": "DalyaDealBot/1.0 (+https://example.com)"}
RAINFOREST_KEY = os.getenv("RAINFOREST_KEY")   # ←  שימי במשתנה-סביבה / .env
PLACEHOLDER_IMG = "data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMTYwIiBoZWlnaHQ9IjE2MCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMTYwIiBoZWlnaHQ9IjE2MCIgZmlsbD0iI2Y1ZjVmNSIvPjx0ZXh0IHg9IjgwIiB5PSI4MCIgZm9udC1mYW1pbHk9IkFyaWFsIiBmb250LXNpemU9IjE0IiBmaWxsPSIjNjY2IiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBkb21pbmFudC1iYXNlbGluZT0ibWlkZGxlIj5ObyBJbWFnZTwvdGV4dD48L3N2Zz4="

# Cache for image URLs
IMAGE_CACHE = {}
IMAGE_CACHE_LOCK = threading.Lock()

# hosts שעוטפים לינק אמזון
REDIRECT_HOSTS = {"slickdeals.net", "go.skimresources.com"}

# לוגר בסיסי
log = logging.getLogger("clean_keyword_deal_bot")
if not log.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

def calculate_relevance(title: str, keyword: str) -> float:
    """Calculate how relevant a deal is to the search keyword."""
    # Convert to lowercase for better matching
    title_lower = title.lower()
    keyword_lower = keyword.lower()
    
    # Split keywords into words and remove common words
    stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'of', 'is', 'are', 'was', 'were', 'be', 'been', 'being'}
    keyword_words = set(word for word in keyword_lower.split() if word not in stop_words)
    title_words = set(word for word in title_lower.split() if word not in stop_words)
    
    # Calculate exact word match ratio
    exact_matches = len(keyword_words.intersection(title_words))
    word_match_ratio = exact_matches / len(keyword_words) if keyword_words else 0
    
    # Calculate sequence similarity for partial matches
    sequence_similarity = SequenceMatcher(None, title_lower, keyword_lower).ratio()
    
    # Calculate word order importance
    keyword_sequence = [word for word in keyword_lower.split() if word not in stop_words]
    title_sequence = [word for word in title_lower.split() if word not in stop_words]
    
    # Check if keywords appear in the same order
    order_score = 0
    if keyword_sequence:
        for i in range(len(title_sequence) - len(keyword_sequence) + 1):
            if title_sequence[i:i + len(keyword_sequence)] == keyword_sequence:
                order_score = 1
                break
    
    # Calculate word proximity score
    proximity_score = 0
    if len(keyword_words) > 1:
        keyword_positions = []
        for word in keyword_words:
            if word in title_lower:
                pos = title_lower.find(word)
                keyword_positions.append(pos)
        if len(keyword_positions) > 1:
            # Calculate average distance between keywords
            distances = [abs(keyword_positions[i] - keyword_positions[i-1]) for i in range(1, len(keyword_positions))]
            avg_distance = sum(distances) / len(distances)
            proximity_score = 1 / (1 + avg_distance/100)  # Normalize to 0-1 range
    
    # Combine scores with weights
    final_score = (
        word_match_ratio * 0.4 +      # Exact word matches
        sequence_similarity * 0.2 +   # Partial matches
        order_score * 0.2 +          # Word order importance
        proximity_score * 0.2        # Word proximity
    )
    
    return final_score

def extract_price(text: str) -> float:
    """Extract price from text."""
    price_pattern = r'\$(\d+(?:\.\d{2})?)'
    match = re.search(price_pattern, text)
    if match:
        return float(match.group(1))
    return 0.0

def is_good_deal(title: str, price: float) -> bool:
    """Determine if a deal is worth showing based on price and title."""
    # Check for common deal indicators
    deal_indicators = [
        'deal', 'sale', 'discount', 'off', 'save', 'clearance',
        'limited', 'special', 'offer', 'bargain', 'steal',
        'best', 'top', 'amazing', 'incredible', 'unbelievable'
    ]
    
    title_lower = title.lower()
    has_deal_indicator = any(indicator in title_lower for indicator in deal_indicators)
    
    # Price threshold
    is_good_price = price > 0 and price < 1000  # Reasonable price threshold
    
    return has_deal_indicator or is_good_price

def _extract_embedded_url(url: str) -> str:
    """מחזיר את ערך הפרמטר url= או u= אם host הוא slickdeals / skimlinks."""
    parts = urlparse(url)
    host  = parts.netloc.lower()
    if any(h in host for h in REDIRECT_HOSTS):
        qs = parse_qs(parts.query)
        for key in ("url", "u"):
            if key in qs:
                return unquote(qs[key][0])
    return url

def _normalize_amazon_link(raw_url: str, tag: str = DEFAULT_TAG) -> str:
    """מנקה קישור אמזון: פותר רידיירקטים, מוחק פרמטרים, מוסיף tag."""
    url = _extract_embedded_url(raw_url)

    # amzn.to / bit.ly
    try:
        url = requests.head(url, allow_redirects=True, timeout=5).url
    except requests.RequestException:
        pass

    host = urlparse(url).netloc.lower()
    if not (".amazon." in host or host.endswith(".a.co")):
        raise ValueError(f"Not an Amazon link ({host})")

    parts = list(urlparse(url))
    qs    = parse_qs(parts[4], keep_blank_values=False)
    for junk in ("ascsubtag", "ref", "psc", "crid", "qid", "sr"):
        qs.pop(junk, None)
    qs["tag"] = [tag]
    parts[4]  = urlencode(qs, doseq=True)
    clean     = re.sub(r"[&?]+$", "", urlunparse(parts))
    return clean

def _build_amazon_search_link(title_or_kw: str, tag: str) -> str:
    """יוצר לינק-חיפוש באמזון מהכותרת/המילה עם tag."""
    q = quote_plus(title_or_kw.strip())
    return f"https://www.amazon.com/s?k={q}&tag={tag}"

def _extract_asin_from_url(url: str) -> str | None:
    """Extract ASIN from Amazon URL."""
    patterns = [
        r'/dp/([A-Z0-9]{10})',
        r'/gp/product/([A-Z0-9]{10})',
        r'/product/([A-Z0-9]{10})',
        r'/ASIN/([A-Z0-9]{10})',
        r'asin=([A-Z0-9]{10})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

def _get_amazon_product_image(asin: str) -> str | None:
    """Get product image from Amazon using ASIN."""
    # Check cache first
    with IMAGE_CACHE_LOCK:
        if asin in IMAGE_CACHE:
            return IMAGE_CACHE[asin]
    
    try:
        amazon_url = f"https://www.amazon.com/dp/{asin}"
        response = requests.get(amazon_url, headers=HEADERS, timeout=3)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Look for main product image
            img = soup.select_one('#landingImage, #imgBlkFront, .a-dynamic-image')
            if img:
                src = img.get('src') or img.get('data-src')
                if src and not src.startswith('data:'):
                    # Cache the result
                    with IMAGE_CACHE_LOCK:
                        IMAGE_CACHE[asin] = src
                    return src
    except Exception as e:
        log.warning(f"Failed to get Amazon product image for ASIN {asin}: {e}")
    return None

def _process_deal(entry, tag: str, keyword: str) -> dict | None:
    """Process a single deal entry and return deal data or None if failed."""
    try:
        log.info(f"Processing deal: {entry.title}")
        start_time = time()
        
        # Calculate relevance score first
        relevance_score = calculate_relevance(entry.title, keyword)
        
        # Skip deals with very low relevance
        if relevance_score < 0.2:  # Lowered threshold to catch more potential matches
            return None
            
        # Extract price
        price = extract_price(entry.title)
        
        # Check if it's a good deal
        if not is_good_deal(entry.title, price):
            return None

        # Find Amazon link
        href = None
        try:
            art = requests.get(entry.link, headers=HEADERS, timeout=3)
            soup = BeautifulSoup(art.content, "html.parser")
            
            amazon_selectors = [
                'a[href*="amazon.com"]',
                'a[href*="amzn.to"]',
                'a[data-href*="amazon.com"]',
                'button[data-url*="amazon.com"]'
            ]
            for selector in amazon_selectors:
                link_elem = soup.select_one(selector)
                if link_elem:
                    href = link_elem.get('href') or link_elem.get('data-href') or link_elem.get('data-url')
                    if href:
                        break
        except Exception as exc:
            log.warning(f"Timeout or error fetching {entry.link}: {exc}")
            return None

        if not href:
            return None

        try:
            clean = _normalize_amazon_link(href, tag)
            asin = _extract_asin_from_url(clean)
            img_url = _get_amazon_product_image(asin) if asin else PLACEHOLDER_IMG
            
            # Adjust relevance score based on additional factors
            if price > 0:
                # Boost relevance for deals with prices
                relevance_score *= 1.2
            if img_url != PLACEHOLDER_IMG:
                # Boost relevance for deals with images
                relevance_score *= 1.1
                
            # Cap relevance score at 1.0
            relevance_score = min(relevance_score, 1.0)
            
            elapsed = time() - start_time
            if elapsed > 2:
                log.warning(f"Slow deal processing: {entry.title} took {elapsed:.2f}s")
                
            return {
                "title": entry.title,
                "amazon_link": clean,
                "image": img_url,
                "relevance_score": relevance_score,
                "price": price
            }
        except Exception as e:
            log.warning(f"Amazon link normalization failed: {e}")
            return None
            
    except Exception as exc:
        log.warning("⚠️ fetch failed for %s: %s", entry.link, exc)
        return None

def get_amazon_affiliate_links(
    keyword: str,
    tag: str = DEFAULT_TAG,
    max_results: int = MAX_RESULTS,
) -> dict:
    """Return {'deals': [...]} always (never empty list)."""
    log.info("🔎 keyword=%s  tag=%s", keyword, tag)

    # 1) RSS feed filtered to Amazon store
    rss = (
        "https://slickdeals.net/newsearch.php?src=SearchBarV2"
        f"&q={quote_plus(keyword)}"
        "&searcharea=deals&searchin=first&store=amazon.com"
        "&rss=1"
    )
    feed = feedparser.parse(rss)
    log.info("📥 RSS entries: %d", len(feed.entries))

    # Process items with improved filtering
    deals = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_entry = {
            executor.submit(_process_deal, entry, tag, keyword): entry
            for entry in feed.entries[:10]  # Increased from 5 to 10 for better filtering
        }
        for future in as_completed(future_to_entry):
            deal = future.result()
            if deal:
                deals.append(deal)
                log.info("✅ deal collected: %s (score: %.2f)", deal["title"], deal["relevance_score"])

    # Sort deals by multiple criteria
    def sort_key(deal):
        relevance = deal.get('relevance_score', 0)
        price = deal.get('price', 0)
        has_image = deal.get('image') != PLACEHOLDER_IMG
        
        # Combine factors for sorting
        return (
            relevance,  # Primary sort by relevance
            has_image,  # Secondary sort by image presence
            -price if price > 0 else 0  # Tertiary sort by price (higher prices first)
        )

    # Sort and limit results
    deals.sort(key=sort_key, reverse=True)
    deals = deals[:max_results]

    # 2) Fallbacks
    if not deals:
        search_link = _build_amazon_search_link(keyword, tag)
        log.info("🔁 Fallback to plain Amazon search")
        deals = [{
            "title": f"Amazon search for '{keyword}'",
            "amazon_link": search_link,
            "image": PLACEHOLDER_IMG,
            "relevance_score": 0,
            "price": 0
        }]
    return {"deals": deals}

# ─────────────────────────────── quick test ────────────────────────────────
if __name__ == "__main__":
    import json, sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "ring"
    print(json.dumps(get_amazon_affiliate_links(kw), indent=2))