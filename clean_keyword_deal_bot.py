"""
Dalya Amazon Deal Bot  –  scrape Slickdeals RSS, return Amazon links with tag
---------------------------------------------------------------------------
* primary: deals from Slickdeals (store=amazon.com)
* fallback A: top Amazon product from Rainforest API (needs RAINFOREST_KEY)
* fallback B: plain Amazon search link
"""

from __future__ import annotations
import logging, re, os, html, requests, feedparser
from urllib.parse import (
    urlparse, parse_qs, urlencode, urlunparse,
    quote_plus, unquote,
)
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

# ───────────────────────────────  settings  ────────────────────────────────
DEFAULT_TAG   = "2025050f-20"          # ←  תג-השותף שלך
MAX_RESULTS   = 5                      # ←  כמה דילים להחזיר ללקוח
HEADERS       = {"User-Agent": "DalyaDealBot/1.0 (+https://example.com)"}
RAINFOREST_KEY = os.getenv("RAINFOREST_KEY")   # ←  שימי במשתנה-סביבה / .env
PLACEHOLDER_IMG = "data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMTYwIiBoZWlnaHQ9IjE2MCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMTYwIiBoZWlnaHQ9IjE2MCIgZmlsbD0iI2Y1ZjVmNSIvPjx0ZXh0IHg9IjgwIiB5PSI4MCIgZm9udC1mYW1pbHk9IkFyaWFsIiBmb250LXNpemU9IjE0IiBmaWxsPSIjNjY2IiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBkb21pbmFudC1iYXNlbGluZT0ibWlkZGxlIj5ObyBJbWFnZTwvdGV4dD48L3N2Zz4="

# hosts שעוטפים לינק אמזון
REDIRECT_HOSTS = {"slickdeals.net", "go.skimresources.com"}

# לוגר בסיסי
log = logging.getLogger("clean_keyword_deal_bot")
if not log.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

# ───────────────────── helpers: redirects  &  link cleaning ────────────────
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
        url = requests.head(url, allow_redirects=True, timeout=8).url
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


# ───────────────────── helpers: build search / alt product links ───────────
def _build_amazon_search_link(title_or_kw: str, tag: str) -> str:
    """יוצר לינק-חיפוש באמזון מהכותרת/המילה עם tag."""
    q = quote_plus(title_or_kw.strip())
    return f"https://www.amazon.com/s?k={q}&tag={tag}"


def _top_amazon_product(keyword: str, tag: str) -> dict | None:
    """
    מביא את התוצאה האורגנית הראשונה באמזון באמצעות Rainforest API
    ומחזיר מילון  {title, amazon_link, image}.  מחזיר None אם יש כשל.
    """
    if not RAINFOREST_KEY:
        return None                       # אין מפתח? דולג לפולבאק הבא.

    try:
        params = {
            "api_key": RAINFOREST_KEY,
            "type": "search",
            "amazon_domain": "amazon.com",
            "search_term": keyword,
            "sort_by": "featured"         # או "salesrank"
        }
        r = requests.get(
            "https://api.rainforestapi.com/request",
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        data  = r.json()
        first = data["search_results"][0]          # התוצאה הכi בולטת
        asin  = first["asin"]
        title = first["title"]
        img   = first["image"]                     # ← שורת התמונה
        link  = f"https://www.amazon.com/dp/{asin}?tag={tag}"
        return {"title": title, "amazon_link": link, "image": img}

    except Exception as exc:
        log.warning("Rainforest fallback failed: %s", exc)
        return None


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
    """Get product image from Amazon using ASIN via direct Amazon page scraping."""
    try:
        amazon_url = f"https://www.amazon.com/dp/{asin}"
        response = requests.get(amazon_url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Look for main product image
            img_selectors = [
                '#landingImage',
                '#imgBlkFront',
                '.a-dynamic-image',
                '[data-a-image-name="landingImage"]',
                '#main-image-container img'
            ]
            
            for selector in img_selectors:
                img = soup.select_one(selector)
                if img:
                    src = img.get('src') or img.get('data-src')
                    if src and not src.startswith('data:'):
                        log.info(f"Found Amazon product image: {src}")
                        return src
                        
    except Exception as e:
        log.warning(f"Failed to get Amazon product image for ASIN {asin}: {e}")
    return None


def _make_absolute_slickdeals_url(url: str) -> str:
    if url and url.startswith('/'):
        return 'https://slickdeals.net' + url
    return url


def _extract_image_from_slickdeals(soup: BeautifulSoup, entry_title: str) -> str | None:
    """Enhanced image extraction from Slickdeals page."""
    img_url = None
    
    # Strategy 1: Look for main deal image in various containers
    deal_containers = [
        soup.find('div', class_='dealContent'),
        soup.find('div', class_='dealImage'),
        soup.find('div', class_='threadContent'),
        soup.find('div', class_='cept-post-content'),
        soup.find('article'),
        soup.find('main')
    ]
    
    for container in deal_containers:
        if not container:
            continue
            
        # Look for images with specific classes first
        priority_selectors = [
            'img.dealImage',
            'img[class*="product"]',
            'img[class*="main"]',
            'img[class*="primary"]',
            'img[data-src*="amazon"]',
            'img[src*="amazon"]'
        ]
        
        for selector in priority_selectors:
            img = container.select_one(selector)
            if img:
                src = img.get('src') or img.get('data-src')
                if src and _is_valid_product_image(src):
                    log.info(f"Found priority image: {src}")
                    return _make_absolute_slickdeals_url(src)
        
        # Look for any reasonably sized images
        for img in container.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if src and _is_valid_product_image(src):
                # Check image dimensions if available
                width = img.get('width')
                height = img.get('height')
                if width and height:
                    try:
                        w, h = int(width), int(height)
                        if w >= 100 and h >= 100:  # Reasonable product image size
                            log.info(f"Found sized product image: {src}")
                            return _make_absolute_slickdeals_url(src)
                    except ValueError:
                        pass
                
                # If no size info, accept it if it passes validity check
                log.info(f"Found valid image: {src}")
                return _make_absolute_slickdeals_url(src)
    
    # Strategy 2: Check meta tags
    meta_selectors = [
        'meta[property="og:image"]',
        'meta[name="twitter:image"]',
        'meta[property="product:image"]'
    ]
    
    for selector in meta_selectors:
        meta = soup.select_one(selector)
        if meta:
            content = meta.get('content')
            if content and _is_valid_product_image(content):
                log.info(f"Found meta image: {content}")
                return _make_absolute_slickdeals_url(content)
    
    # Strategy 3: Look in JSON-LD structured data
    try:
        scripts = soup.find_all('script', type='application/ld+json')
        for script in scripts:
            import json
            data = json.loads(script.string)
            if isinstance(data, dict):
                image = data.get('image')
                if image:
                    if isinstance(image, list):
                        image = image[0]
                    if isinstance(image, dict):
                        image = image.get('url')
                    if image and _is_valid_product_image(str(image)):
                        log.info(f"Found JSON-LD image: {image}")
                        return _make_absolute_slickdeals_url(str(image))
    except:
        pass
    
    return None


def _is_valid_product_image(url: str) -> bool:
    """Check if URL looks like a valid product image."""
    if not url or url.startswith('data:'):
        return False
    
    # Skip obvious non-product images
    skip_patterns = [
        'icon', 'avatar', 'logo', 'spinner', 'loading', 
        'placeholder', 'blank', 'spacer', 'pixel.gif',
        'facebook', 'twitter', 'social', 'badge'
    ]
    
    url_lower = url.lower()
    if any(pattern in url_lower for pattern in skip_patterns):
        return False
    
    # Prefer Amazon images
    if 'amazon' in url_lower or 'ssl-images-amazon' in url_lower:
        return True
    
    # Check for reasonable image extensions
    if any(ext in url_lower for ext in ['.jpg', '.jpeg', '.png', '.webp']):
        return True
    
    return True


def _process_deal(entry, tag: str) -> dict | None:
    """Process a single deal entry and return deal data or None if failed."""
    try:
        log.info(f"Processing deal: {entry.title}")
        art = requests.get(entry.link, headers=HEADERS, timeout=10)
        print("==== HTML for debugging ====")
        print(art.text[:2000])  # Print the first 2000 characters for inspection
        print("===========================")
        soup = BeautifulSoup(art.content, "html.parser")

        # Enhanced image extraction
        img_url = _extract_image_from_slickdeals(soup, entry.title)

        # Find Amazon link
        href = None
        amazon_selectors = [
            'a[href*="amazon.com"]',
            'a[href*="amzn.to"]',
            'a[data-href*="amazon.com"]',
            'button[data-url*="amazon.com"]'
        ]
        
        # Try direct Amazon link selectors first
        for selector in amazon_selectors:
            link_elem = soup.select_one(selector)
            if link_elem:
                href = link_elem.get('href') or link_elem.get('data-href') or link_elem.get('data-url')
                if href:
                    log.info(f"Found direct Amazon link: {href}")
                    break
        
        # Fallback to any link that might redirect to Amazon
        if not href:
            for tag_elem in soup.find_all(["a", "button"]):
                href = (
                    tag_elem.get("href")
                    or tag_elem.get("data-href")
                    or tag_elem.get("data-url")
                )
                if href and ('amazon' in href.lower() or 'amzn' in href.lower() or 'skimresources' in href.lower()):
                    log.info(f"Found potential Amazon link: {href}")
                    break

        if href:
            try:
                clean = _normalize_amazon_link(href, tag)
                log.info(f"Normalized Amazon link: {clean}")
                
                # If we didn't find an image from Slickdeals, try to get it from Amazon
                if not img_url:
                    asin = _extract_asin_from_url(clean)
                    if asin:
                        img_url = _get_amazon_product_image(asin)
                
                if not img_url:
                    img_url = PLACEHOLDER_IMG
                    log.info("Using placeholder image")

                # Log the final deal data
                deal_data = {
                    "title": entry.title,
                    "amazon_link": clean,
                    "image": img_url
                }
                log.info(f"Final deal data: {deal_data}")
                return deal_data

            except ValueError:
                clean = _build_amazon_search_link(entry.title, tag)
                log.info(f"Created search link: {clean}")
                return {
                    "title": entry.title,
                    "amazon_link": clean,
                    "image": img_url or PLACEHOLDER_IMG
                }

    except Exception as exc:
        log.warning("⚠️ fetch failed for %s: %s", entry.link, exc)
    
    return None


# ──────────────────────────────── main API ─────────────────────────────────
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

    # Filter entries by keyword
    matching_entries = [
        entry for entry in feed.entries
        if keyword.lower() in entry.title.lower()
    ][:max_results]

    # Process deals in parallel
    deals = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_entry = {
            executor.submit(_process_deal, entry, tag): entry
            for entry in matching_entries
        }
        
        for future in as_completed(future_to_entry):
            deal = future.result()
            if deal:
                deals.append(deal)
                log.info("✅ deal collected: %s", deal["title"])

    # 2) Fallbacks
    if not deals:
        alt = _top_amazon_product(keyword, tag)
        if alt:
            log.info("🌟 using top-product fallback")
            return {"deals": [alt]}

        search_link = _build_amazon_search_link(keyword, tag)
        log.info("🔁 Fallback to plain Amazon search")
        deals = [{
            "title": f"Amazon search for '{keyword}'",
            "amazon_link": search_link,
            "image": PLACEHOLDER_IMG
        }]

    return {"deals": deals}


# ─────────────────────────────── quick test ────────────────────────────────
if __name__ == "__main__":
    import json, sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "ring"
    print(json.dumps(get_amazon_affiliate_links(kw), indent=2))