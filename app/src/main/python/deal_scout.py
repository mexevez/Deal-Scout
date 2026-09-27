#!/usr/bin/env python3
"""
Deal Scout: search a product and compare prices from major stores, side by side.

Run it:   python3 deal_scout.py      (Windows: py deal_scout.py)
It opens http://127.0.0.1:8765 in your browser. No account, key or install needed.

Deal Scout reads the same public search pages you'd see in your browser at
Amazon, Walmart, Best Buy, eBay and Newegg, and you can add any other store's
listing by pasting its link. Everything runs on your computer.
"""
import gzip
import html as htmllib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DEAL_SCOUT_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.environ.get("DEAL_SCOUT_DEMO") == "1"   # reads saved sample pages instead of the web
CACHE_SECONDS = 15 * 60
TIMEOUT = 15

_cache = {}
_lock = threading.Lock()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class Blocked(Exception):
    """The store refused an automated lookup (captcha, 403, 429...)."""


class FetchFailed(Exception):
    pass


def ssl_context():
    try:
        import certifi  # optional; helps on some macOS Python installs
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch(url, referer=None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl_context()) as resp:
            raw = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503, 412, 999):
            raise Blocked("The store turned away the lookup (HTTP %d)." % e.code)
        raise FetchFailed("The store returned an error (HTTP %d)." % e.code)
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "CERTIFICATE" in reason.upper():
            raise FetchFailed("Python couldn't verify secure connections. On a Mac, open "
                              "Applications > Python 3.x and run 'Install Certificates.command'.")
        if "timed out" in reason.lower():
            raise FetchFailed("The store took too long to answer.")
        raise FetchFailed("Couldn't reach the store. Check your internet connection.")
    except TimeoutError:
        raise FetchFailed("The store took too long to answer.")
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


BLOCK_MARKERS = ("captcha", "robot or human", "are you a human", "px-captcha",
                 "enter the characters you see below", "automated access",
                 "request unsuccessful. incapsula", "access denied", "pardon our interruption")


def looks_blocked(page):
    head = page[:60000].lower()
    return any(m in head for m in BLOCK_MARKERS)


# ---------------------------------------------------------------- helpers
def text_of(fragment):
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", htmllib.unescape(fragment)).strip()


def money(s):
    """First dollar amount in s -> float, or None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s) if s > 0 else None
    m = re.search(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)", str(s))
    return float(m.group(1).replace(",", "")) if m else None


def parse_shipping(text):
    """'Free delivery' -> 0.0, '+$5.99 shipping' -> 5.99, unknown -> None."""
    if not text:
        return None
    t = str(text).lower()
    if re.search(r"free\b[^,.;|]{0,40}\b(deliver|ship)", t) or re.search(r"(deliver\w*|shipping)\s+(is\s+)?free", t):
        return 0.0
    m = re.search(r"\+?\s?\$\s?([\d,]+(?:\.\d{1,2})?)\s*(?:\w+\s)?(?:shipping|delivery|ship)", t)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


CONDITION_WORDS = (
    (r"\b(renewed|refurbished|certified refurb|seller refurbished)\b", "Refurbished"),
    (r"\b(open[- ]box|new \(other\)|new other)\b", "Open box"),
    (r"\b(pre-owned|preowned|used)\b", "Used"),
)


def condition_from(*texts):
    for t in texts:
        low = (t or "").lower()
        for pat, label in CONDITION_WORDS:
            if re.search(pat, low):
                return label
    return "New"


def offer(store, title, price, **kw):
    price = money(price)
    title = text_of(title or "")
    if not price or not title:
        return None
    o = {"title": title[:300], "store": store, "price": price, "oldPrice": money(kw.get("oldPrice")),
         "shipping": kw.get("shipping"), "delivery": kw.get("delivery") or "",
         "condition": kw.get("condition") or condition_from(title), "rating": kw.get("rating"),
         "reviews": kw.get("reviews"), "thumbnail": kw.get("thumbnail") or "",
         "link": kw.get("link") or "", "tag": kw.get("tag") or "", "seller": kw.get("seller") or ""}
    if o["oldPrice"] and o["oldPrice"] <= o["price"]:
        o["oldPrice"] = None
    return o


def to_int(s):
    if s is None:
        return None
    m = re.search(r"\d[\d,]*", str(s))
    return int(m.group(0).replace(",", "")) if m else None


def to_float(s):
    if s is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(s))
    return float(m.group(0)) if m else None


def blocks(page, start_pattern, limit=80):
    """Slice page into item blocks, each starting at a match of start_pattern."""
    starts = [m.start() for m in re.finditer(start_pattern, page)]
    out = []
    for i, s in enumerate(starts[:limit]):
        e = starts[i + 1] if i + 1 < len(starts) else s + 20000
        out.append(page[s:min(e, s + 20000)])
    return out


def ld_json_offers(page, store, base_url):
    """Products described with schema.org JSON-LD (used by many stores)."""
    found = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = deque([data])
        while stack:
            d = stack.popleft()
            if isinstance(d, list):
                stack.extend(d)
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types:
                offers = d.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price") or offers.get("lowPrice")
                cond = str(offers.get("itemCondition") or "")
                agg = d.get("aggregateRating") or {}
                img = d.get("image")
                if isinstance(img, list):
                    img = img[0] if img else ""
                if isinstance(img, dict):
                    img = img.get("url", "")
                link = d.get("url") or offers.get("url") or base_url
                o = offer(store, d.get("name"), money("$%s" % price) if price not in (None, "") else None,
                          link=urllib.parse.urljoin(base_url, link) if link else base_url,
                          thumbnail=img or "",
                          rating=to_float(agg.get("ratingValue")), reviews=to_int(agg.get("reviewCount") or agg.get("ratingCount")),
                          condition=("Refurbished" if "Refurbished" in cond else "Used" if "Used" in cond
                                     else "Open box" if "Damaged" in cond else None))
                if o:
                    found.append(o)
            for v in d.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
    return found


# ---------------------------------------------------------------- stores
def parse_amazon(page):
    out = []
    for b in blocks(page, r'<div[^>]+data-component-type="s-search-result"'):
        asin = re.search(r'data-asin="([A-Z0-9]{10})"', b)
        if not asin:
            continue
        title = re.search(r'<h2[^>]*aria-label="([^"]+)"', b) or re.search(r"<h2\b[^>]*>(.*?)</h2>", b, re.S)
        # current price: first a-price that isn't a struck-through "List/Was" price
        price = None
        for pm in re.finditer(r'<span class="a-price(?P<cls>[^"]*)"(?P<attrs>[^>]*)>\s*<span class="a-offscreen">([^<]+)</span>', b):
            if "a-text-price" in pm.group("cls") or 'data-a-strike="true"' in pm.group("attrs"):
                continue
            price = pm.group(3)
            break
        old = re.search(r'<span class="a-price a-text-price"[^>]*data-a-strike="true"[^>]*>\s*<span class="a-offscreen">([^<]+)</span>', b)
        rating = re.search(r"([\d.]+) out of 5 stars", b)
        reviews = re.search(r'aria-label="([\d,]+) (?:ratings?|reviews?)"', b) or re.search(r's-underline-text">\(?([\d,.]+K?)\)?<', b)
        img = re.search(r'<img[^>]+class="s-image"[^>]+src="([^"]+)"', b) or re.search(r'<img[^>]+src="([^"]+)"[^>]+class="s-image"', b)
        txt = text_of(b)
        ship = 0.0 if re.search(r"free (delivery|shipping)", txt, re.I) else parse_shipping(txt)
        o = offer("Amazon", title.group(1) if title else "", price,
                  oldPrice=old.group(1) if old else None, shipping=ship,
                  rating=to_float(rating.group(1)) if rating else None,
                  reviews=to_int(reviews.group(1)) if reviews and "K" not in reviews.group(1) else None,
                  thumbnail=img.group(1) if img else "", link="https://www.amazon.com/dp/" + asin.group(1),
                  tag="Sponsored" if re.search(r">\s*Sponsored\s*<", b) else "",
                  condition=condition_from(title.group(1) if title else ""))
        if o:
            out.append(o)
    return out


def parse_walmart(page):
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out, seen, stack = [], set(), deque([data])
    while stack:
        d = stack.popleft()
        if isinstance(d, list):
            stack.extend(d)
            continue
        if not isinstance(d, dict):
            continue
        if d.get("canonicalUrl") and d.get("name") and ("price" in d or "priceInfo" in d):
            pid = d.get("usItemId") or d.get("id") or d.get("canonicalUrl")
            if pid in seen:
                continue
            seen.add(pid)
            pi = d.get("priceInfo") or {}
            cur = pi.get("currentPrice") or {}
            price = (d.get("price") if isinstance(d.get("price"), (int, float)) and d.get("price") > 0 else None) \
                or cur.get("price") or money(pi.get("linePrice")) or money(pi.get("itemPrice")) \
                or money(pi.get("linePriceDisplay"))
            was = pi.get("wasPrice")
            was = was.get("price") if isinstance(was, dict) else money(was)
            img = d.get("image") or (d.get("imageInfo") or {}).get("thumbnailUrl") or ""
            ship_txt = " ".join(str(x) for x in (d.get("fulfillmentBadge"), d.get("shippingMessage"),
                                                  (d.get("fulfillmentSummary") or [{}])[0].get("fulfillment") if isinstance(d.get("fulfillmentSummary"), list) and d.get("fulfillmentSummary") else "") if x)
            seller = d.get("sellerName") or ""
            cond = (d.get("conditionV2") or {}).get("name") if isinstance(d.get("conditionV2"), dict) else d.get("condition")
            o = offer("Walmart", d.get("name"), price, oldPrice=was,
                      shipping=parse_shipping(ship_txt), delivery=ship_txt,
                      rating=to_float(d.get("averageRating")), reviews=to_int(d.get("numberOfReviews")),
                      thumbnail=img, link=urllib.parse.urljoin("https://www.walmart.com", d["canonicalUrl"].split("?")[0]),
                      seller=seller if seller and seller.lower() not in ("walmart.com", "walmart") else "",
                      tag="Sponsored" if d.get("isSponsoredFlag") else "",
                      condition=condition_from(cond or "", d.get("name")))
            if o:
                out.append(o)
            continue
        for v in d.values():
            if isinstance(v, (dict, list)):
                stack.append(v)
    return out


def parse_ebay(page):
    out = []
    for b in blocks(page, r'<li[^>]*(?:class="[^"]*\bs-(?:item|card)\b[^"]*"|data-listingid=)'):
        link = re.search(r'href="(https://www\.ebay\.com/itm/\d+)', b)
        if not link:
            continue
        title = re.search(r'class="[^"]*\b(?:s-item__title|s-card__title)\b[^"]*"[^>]*>(.*?)</(?:div|h3)>', b, re.S)
        tt = text_of(title.group(1)) if title else ""
        tt = re.sub(r"^(New Listing|NEW LISTING)\s*", "", tt)
        tt = re.sub(r"\s*Opens in a new window or tab\s*$", "", tt)
        if not tt or tt.lower().startswith("shop on ebay"):
            continue
        price_m = re.search(r'class="[^"]*\b(?:s-item__price|s-card__price)\b[^"]*"[^>]*>(.*?)</span>', b, re.S)
        ptxt = text_of(price_m.group(1)) if price_m else ""
        if " to " in ptxt:   # price ranges (multiple variants) aren't comparable
            continue
        txt = text_of(b)
        ship = parse_shipping(txt)
        cond_m = re.search(r"\b(Brand New|New \(Other\)|Open Box|Pre-Owned|Used|Refurbished|Certified[- ]Refurbished|Seller refurbished|Excellent - Refurbished|Very Good - Refurbished|Good - Refurbished)\b", txt)
        cond = cond_m.group(1) if cond_m else ""
        cond = "New" if cond == "Brand New" else condition_from(cond, tt)
        img = re.search(r'<img[^>]+(?:src|data-src)="(https://i\.ebayimg\.com/[^"]+)"', b)
        o = offer("eBay", tt, ptxt, shipping=ship, condition=cond, link=link.group(1),
                  thumbnail=img.group(1) if img else "")
        if o:
            out.append(o)
    return out


def parse_newegg(page):
    out = []
    for b in blocks(page, r'<div[^>]+class="item-cell[^"]*"'):
        t = re.search(r'<a[^>]+class="item-title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S) \
            or re.search(r'<a[^>]+href="([^"]+)"[^>]*class="item-title"[^>]*>(.*?)</a>', b, re.S)
        p = re.search(r'class="price-current[^"]*"[^>]*>(.*?)</li>', b, re.S)
        if not t or not p:
            continue
        ptxt = text_of(p.group(1)).replace(" .", ".")
        ptxt = re.sub(r"(\d)\s+\.(\d)", r"\1.\2", ptxt)
        was = re.search(r'class="price-was-data"[^>]*>([^<]+)<', b)
        ship = re.search(r'class="price-ship"[^>]*>(.*?)</li>', b, re.S)
        rating = re.search(r'aria-label="rated ([\d.]+) out of 5"', b)
        reviews = re.search(r'class="item-rating-num"[^>]*>\(?([\d,]+)\)?<', b)
        img = re.search(r'<img[^>]+src="(https://c1\.neweggimages\.com/[^"]+)"', b)
        ship_txt = text_of(ship.group(1)) if ship else ""
        o = offer("Newegg", t.group(2), ptxt, oldPrice=was.group(1) if was else None,
                  shipping=0.0 if "free" in ship_txt.lower() else money(ship_txt), delivery=ship_txt,
                  rating=to_float(rating.group(1)) if rating else None,
                  reviews=to_int(reviews.group(1)) if reviews else None,
                  thumbnail=img.group(1) if img else "", link=htmllib.unescape(t.group(1)))
        if o:
            out.append(o)
    return out


def parse_bestbuy(page):
    out = []
    for b in blocks(page, r'<li[^>]+class="[^"]*\b(?:sku-item|product-list-item)\b[^"]*"'):
        t = re.search(r'<h4[^>]*class="[^"]*sku-title[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S) \
            or re.search(r'<a[^>]+class="[^"]*product-list-item-link[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S) \
            or re.search(r'<a[^>]+href="(/site/[^"]+\.p\?[^"]*)"[^>]*>(.*?)</a>', b, re.S)
        if not t:
            continue
        pr = re.search(r'class="[^"]*priceView-customer-price[^"]*"[^>]*>.*?(\$[\d,]+\.\d\d)', b, re.S) \
            or re.search(r'data-testid="customer-price"[^>]*>.*?(\$[\d,]+\.\d\d)', b, re.S) \
            or re.search(r'aria-hidden="true">(\$[\d,]+\.\d\d)<', b)
        was = re.search(r"Was (\$[\d,]+\.\d\d)", text_of(b))
        rating = re.search(r"Rating ([\d.]+) out of 5 stars with ([\d,]+) reviews", b)
        img = re.search(r'<img[^>]+class="[^"]*product-image[^"]*"[^>]+src="([^"]+)"', b) \
            or re.search(r'<img[^>]+src="(https://pisces\.bbystatic\.com/[^"]+)"', b)
        title = text_of(t.group(2))
        o = offer("Best Buy", title, pr.group(1) if pr else None, oldPrice=was.group(1) if was else None,
                  shipping=0.0 if re.search(r"free shipping", text_of(b), re.I) else None,
                  rating=to_float(rating.group(1)) if rating else None,
                  reviews=to_int(rating.group(2)) if rating else None,
                  thumbnail=img.group(1) if img else "",
                  link=urllib.parse.urljoin("https://www.bestbuy.com", htmllib.unescape(t.group(1))),
                  condition=condition_from(title))
        if o:
            out.append(o)
    return out


STORES = [
    {"name": "Amazon", "url": "https://www.amazon.com/s?k={q}", "parse": parse_amazon},
    {"name": "Walmart", "url": "https://www.walmart.com/search?q={q}", "parse": parse_walmart},
    {"name": "Best Buy", "url": "https://www.bestbuy.com/site/searchpage.jsp?st={q}&intl=nosplash", "parse": parse_bestbuy},
    {"name": "eBay", "url": "https://www.ebay.com/sch/i.html?_nkw={q}&LH_BIN=1&_sop=15", "parse": parse_ebay},
    {"name": "Newegg", "url": "https://www.newegg.com/p/pl?d={q}", "parse": parse_newegg},
]


def check_store(store, query):
    url = store["url"].format(q=urllib.parse.quote_plus(query))
    try:
        if DEMO:
            page = demo_page(store["name"])
        else:
            page = fetch(url)
        results = store["parse"](page)
        if not results:
            results = ld_json_offers(page, store["name"], url)
        if not results and looks_blocked(page):
            raise Blocked("The store asked for a robot check.")
        return {"store": store["name"], "status": "ok" if results else "none", "count": len(results),
                "message": "" if results else "No listings found.", "searchUrl": url}, results[:40]
    except Blocked as e:
        return {"store": store["name"], "status": "blocked", "count": 0, "message": str(e), "searchUrl": url}, []
    except FetchFailed as e:
        return {"store": store["name"], "status": "error", "count": 0, "message": str(e), "searchUrl": url}, []
    except Exception as e:  # a store changed its page layout
        return {"store": store["name"], "status": "error", "count": 0,
                "message": "Couldn't read this store's page (%s)." % type(e).__name__, "searchUrl": url}, []


def search(query):
    key = query.strip().lower()
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2], True
    with ThreadPoolExecutor(max_workers=len(STORES)) as pool:
        done = list(pool.map(lambda s: check_store(s, query), STORES))
    statuses = [d[0] for d in done]
    results = [r for d in done for r in d[1]]
    if any(s["status"] == "ok" for s in statuses):
        with _lock:
            _cache[key] = (time.time(), results, statuses)
    return results, statuses, False


def store_name_from(url):
    host = urllib.parse.urlparse(url).hostname or ""
    host = re.sub(r"^(www\d?|m|smile)\.", "", host)
    known = {"amazon.com": "Amazon", "walmart.com": "Walmart", "bestbuy.com": "Best Buy", "ebay.com": "eBay",
             "newegg.com": "Newegg", "target.com": "Target", "costco.com": "Costco", "homedepot.com": "Home Depot",
             "lowes.com": "Lowe's", "bhphotovideo.com": "B&H Photo", "samsclub.com": "Sam's Club",
             "microcenter.com": "Micro Center", "adorama.com": "Adorama", "kohls.com": "Kohl's",
             "macys.com": "Macy's", "wayfair.com": "Wayfair", "apple.com": "Apple", "backmarket.com": "Back Market"}
    return known.get(host, host.split(".")[0].capitalize() if host else "Store")


def read_listing(url):
    """Price from one product page, for stores Deal Scout doesn't search."""
    if not re.match(r"^https?://", url):
        raise FetchFailed("Paste a full link starting with https://")
    store = store_name_from(url)
    page = fetch(url)
    found = ld_json_offers(page, store, url)
    if not found:
        # Open Graph / microdata price tags
        price = (re.search(r'<meta[^>]+property="(?:product|og):price:amount"[^>]+content="([\d.,]+)"', page)
                 or re.search(r'itemprop="price"[^>]+content="([\d.,]+)"', page))
        title = (re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page)
                 or re.search(r"<title>(.*?)</title>", page, re.S))
        img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
        if price and title:
            o = offer(store, htmllib.unescape(title.group(1)), "$" + price.group(1).replace(",", ""),
                      link=url, thumbnail=img.group(1) if img else "")
            if o:
                found = [o]
    if not found:
        if looks_blocked(page):
            raise Blocked("%s asked for a robot check. Try again in a while." % store)
        raise FetchFailed("Couldn't find a price on that page. Some stores only show prices after the page loads in a browser.")
    best = found[0]
    best["link"] = url
    best["tag"] = "Added by link"
    return best


# ---------------------------------------------------------------- sample pages (for testing only)
def demo_page(store):
    path = os.path.join(HERE, "samples", store.lower().replace(" ", "") + ".html")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        raise FetchFailed("No sample page for %s." % store)


# ---------------------------------------------------------------- web server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            return self._send(200, PAGE, "text/html")
        if url.path == "/api/status":
            return self._json(200, {"demo": DEMO, "stores": [s["name"] for s in STORES]})
        if url.path == "/api/search":
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                return self._json(400, {"error": "Type a product to search for."})
            try:
                results, stores, cached = search(q[:200])
            except Exception as e:  # keep the app alive on anything unexpected
                return self._json(500, {"error": "Something went wrong: %s" % e})
            return self._json(200, {"query": q, "results": results, "stores": stores, "cached": cached,
                                    "demo": DEMO, "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
        if url.path == "/api/link":
            link = (qs.get("url") or [""])[0].strip()
            try:
                if DEMO:
                    return self._json(200, {"offer": offer(store_name_from(link), "Sample listing from " + store_name_from(link),
                                                           "$309.99", link=link, shipping=0.0, tag="Added by link")})
                return self._json(200, {"offer": read_listing(link)})
            except (Blocked, FetchFailed) as e:
                return self._json(502, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": "Couldn't read that page (%s)." % type(e).__name__})
        self._send(404, "Not found", "text/plain")


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deal Scout</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,800&family=Public+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>
:root{--bg:#EDF1EE;--surface:#FFFFFF;--ink:#15201B;--muted:#5A6A62;--line:#D3DDD7;--accent:#0E7A4F;--tag:#FFD23F;--tag-ink:#1E1A06;--warn:#B4531A;--good-bg:#E1F3EA;--warn-bg:#FBEBDD;
--display:"Bricolage Grotesque",ui-sans-serif,system-ui,sans-serif;--body:"Public Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#101613;--surface:#18211C;--ink:#E6EEE9;--muted:#95A69D;--line:#2A3730;--accent:#3DBE83;--tag:#F2C832;--warn:#F0A06A;--good-bg:#153526;--warn-bg:#3A2616}}
*{box-sizing:border-box}html{background:var(--bg)}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--body);padding:28px 16px 56px}
.wrap{max-width:1000px;margin:0 auto;display:flex;flex-direction:column;gap:24px}
h1,h2{font-family:var(--display);font-weight:800;margin:0;line-height:1.1;text-wrap:balance}
h1{font-size:clamp(32px,6vw,50px);letter-spacing:-.02em}h2{font-size:clamp(22px,3.4vw,28px)}
p{margin:0}.note{font-size:13px;color:var(--muted)}
.eyebrow{font:600 11px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.tag{align-self:flex-start;background:var(--tag);color:var(--tag-ink);font:600 12px/1 var(--mono);padding:6px 10px 6px 14px;border-radius:3px;position:relative;letter-spacing:.04em}
.tag::before{content:"";position:absolute;left:5px;top:50%;width:4px;height:4px;margin-top:-2px;border-radius:50%;background:var(--bg)}
header{display:flex;flex-direction:column;gap:14px}
form.search{display:flex;gap:8px;flex-wrap:wrap}
input,select{font:inherit;color:var(--ink);background:var(--surface);border:1.5px solid var(--line);border-radius:8px;padding:10px 12px;min-width:0}
#q{flex:1 1 300px;font-size:17px;padding:13px 14px}
button{font:600 14px/1 var(--body);border-radius:8px;padding:12px 16px;cursor:pointer;border:1.5px solid var(--ink);background:var(--ink);color:var(--bg)}
button.ghost{background:transparent;color:var(--ink);border-color:var(--line)}button:disabled{opacity:.5;cursor:default}
:focus-visible{outline:2.5px solid var(--accent);outline-offset:2px}
.recent{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.recent button{background:var(--surface);color:var(--ink);border:1.5px solid var(--line);border-radius:99px;padding:7px 11px;font-weight:500;font-size:13px}
.card{background:var(--surface);border:1.5px solid var(--line);border-radius:10px;padding:18px;display:flex;flex-direction:column;gap:12px}
.banner{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:12px 14px;font-size:14px}
.demo{background:var(--tag);color:var(--tag-ink);border-radius:8px;padding:10px 14px;font:600 13px var(--mono)}
.filters{display:flex;gap:14px 20px;flex-wrap:wrap;align-items:center;font-size:14px}
.filters label{display:flex;gap:8px;align-items:center}
.filters input[type=number]{width:96px;font-family:var(--mono);padding:8px 10px}
.filters select{padding:8px 10px}
.verdict{display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:center;padding:18px 20px;background:var(--ink);color:var(--bg);border-radius:10px}
.verdict .big{font:800 clamp(30px,5vw,42px)/1 var(--display);color:var(--tag)}.verdict .sub{opacity:.75;font-size:14px;margin-top:4px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.chip{font:600 11px/1 var(--mono);padding:5px 8px;border-radius:99px;letter-spacing:.04em}
.chip.hit{background:var(--good-bg);color:var(--accent)}.chip.miss{background:var(--warn-bg);color:var(--warn)}
.picks{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.pick{background:var(--surface);border:1.5px solid var(--line);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:4px}
.pick .v{font:600 20px var(--mono)}.pick .s{font-size:13px;color:var(--muted)}
.cmp{position:relative}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:12px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.cmp-row{display:grid;grid-template-columns:minmax(120px,200px) 1fr auto;gap:12px;align-items:center;padding:4px 0;border-radius:6px}
.cmp-name{display:flex;flex-direction:column;line-height:1.25;min-width:0}.cmp-name b{font-weight:600;overflow-wrap:anywhere}.cmp-name span{font-size:12px;color:var(--muted)}
.track{position:relative;height:22px;border-left:1.5px solid var(--line)}
.bar{position:absolute;left:0;top:3px;bottom:3px;background:var(--muted);opacity:.45;border-radius:0 4px 4px 0}
.cmp-row.best .bar{background:var(--accent);opacity:1}.cmp-row:hover .bar{opacity:.75}.cmp-row.best:hover .bar{opacity:.85}
.tgt{position:absolute;top:-4px;bottom:-4px;border-left:2px dashed var(--warn)}
.cmp-val{text-align:right;font-family:var(--mono);white-space:nowrap;display:flex;flex-direction:column;line-height:1.25}
.cmp-val span{font-size:12px;color:var(--muted)}.cmp-row.best .cmp-val span{color:var(--accent);font-weight:600}
.tip{position:absolute;z-index:3;pointer-events:none;background:var(--ink);color:var(--bg);font:12px/1.5 var(--mono);padding:8px 10px;border-radius:6px;min-width:200px;max-width:280px;box-shadow:0 4px 14px rgba(0,0,0,.18)}
.tip div{display:flex;justify-content:space-between;gap:14px}.tip .t{border-top:1px solid rgba(128,128,128,.4);margin-top:4px;padding-top:4px;font-weight:600}
.tbl-wrap{overflow-x:auto;background:var(--surface);border:1.5px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:820px;font-variant-numeric:tabular-nums}
th{font:600 11px/1.2 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:12px 10px;border-bottom:1.5px solid var(--line);white-space:nowrap}
th.r,td.r{text-align:right}td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
td.m{font-family:var(--mono);white-space:nowrap}tr.best td{background:var(--good-bg)}
td img{width:44px;height:44px;object-fit:contain;border-radius:6px;background:#fff}
td a{color:var(--accent);font-weight:600;text-decoration:none;white-space:nowrap}td a:hover{text-decoration:underline}
.sm{font-size:12px;color:var(--muted);display:block}.strike{text-decoration:line-through}
.ttl{max-width:340px}
.more{align-self:flex-start}
.stores{display:flex;gap:8px;flex-wrap:wrap}
.st{display:flex;align-items:center;gap:7px;background:var(--surface);border:1.5px solid var(--line);border-radius:99px;padding:6px 11px;font-size:13px}
.st i{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
.st.ok i{background:var(--accent)}.st.blocked i,.st.error i{background:var(--warn)}
.st a{color:var(--accent);font-weight:600;text-decoration:none}
.st small{color:var(--muted)}
.linkform{gap:10px}
.x{background:none;border:0;color:var(--muted);padding:4px 6px;font-size:16px;cursor:pointer}.x:hover{color:var(--warn)}
.loading{display:flex;gap:10px;align-items:center;color:var(--muted)}
.spin{width:16px;height:16px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:s .8s linear infinite}@keyframes s{to{transform:rotate(360deg)}}
@media (max-width:560px){.verdict{grid-template-columns:1fr}.cmp-row{grid-template-columns:1fr auto}.cmp-row .track{grid-column:1/-1;grid-row:2}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
</style></head><body>
<div class="wrap">
  <header>
    <span class="tag">DEAL SCOUT</span>
    <h1>Every store's price, side by side.</h1>
    <p class="note" style="font-size:15px;max-width:62ch">Search any product and see current prices from Amazon, Walmart, Best Buy, eBay and Newegg, ranked by what you'd actually pay. Shopping somewhere else? Paste the listing link and it joins the comparison.</p>
    <form class="search" id="searchForm"><input id="q" type="search" placeholder="e.g. Dyson V15 Detect, Ninja Creami, LG C4 65 inch" aria-label="Product to search" autocomplete="off"><button id="go" type="submit">Find prices</button></form>
    <div class="recent" id="recent"></div>
    <div class="demo" id="demo" hidden>SAMPLE DATA MODE: these are made-up prices for testing, not real ones.</div>
  </header>


  <div class="banner" id="err" hidden></div>
  <div class="loading" id="loading" hidden><span class="spin"></span><span>Checking Amazon, Walmart, Best Buy, eBay and Newegg…</span></div>

  <section id="res" hidden style="display:flex;flex-direction:column;gap:14px">
    <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:baseline"><h2 id="resHead"></h2><span class="note" id="meta"></span></div>
    <div class="filters">
      <label><input type="checkbox" id="exact" checked> Exact product only</label>
      <label>Condition <select id="cond"><option value="new">New only</option><option value="all">New, used &amp; refurbished</option></select></label>
      <label><input type="checkbox" id="perStore"> Cheapest per store</label>
      <label>Sales tax % <input type="number" id="tax" min="0" step="0.1"></label>
      <label>Target $ <input type="number" id="target" min="0" step="0.01" placeholder="optional"></label>
    </div>
    <div class="stores" id="stores" aria-label="What each store returned"></div>
    <p class="note" id="filterNote"></p>
    <div class="verdict" id="verdict"></div>
    <div class="picks" id="picks"></div>
    <div class="card cmp" id="cmp"><div class="legend" id="legend"></div><div id="bars"></div><button type="button" class="ghost more" id="moreBars" hidden></button><div class="tip" id="tip" hidden></div></div>
    <form class="card linkform" id="linkForm">
      <div><b>Add a price from any other store</b><p class="note">Paste a product link from Target, Costco, Home Depot, B&amp;H or any shop. Deal Scout reads its price and adds it to this comparison.</p></div>
      <div class="search"><input id="link" type="url" placeholder="https://www.target.com/p/…" aria-label="Product link" style="flex:1 1 300px"><button type="submit" id="linkBtn">Add price</button></div>
      <p class="note" id="linkMsg"></p>
    </form>
    <span class="eyebrow">All prices found</span>
    <div class="tbl-wrap"><table><thead><tr><th>#</th><th></th><th>Product</th><th>Store</th><th>Condition</th><th class="r">Price</th><th class="r">Shipping</th><th class="r">You pay</th><th>Rating</th><th></th></tr></thead><tbody id="rows"></tbody></table></div>
    <p class="note">Prices are read from each store's search page and can change. Tax is estimated from your rate. "Shipping not listed" means the page didn't say; check before buying. Member-only and in-cart prices may not show.</p>
  </section>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money=n=>"$"+Number(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const num=v=>{const n=parseFloat(v);return isFinite(n)?n:0};
const safeUrl=u=>/^https?:\/\//i.test(u||"")?u:"";
const squash=s=>String(s).toLowerCase().replace(/[^a-z0-9]+/g,"");
function store(k,v){try{v===undefined?null:localStorage.setItem("ds."+k,JSON.stringify(v))}catch(e){}}
function recall(k,d){try{const v=localStorage.getItem("ds."+k);return v?JSON.parse(v):d}catch(e){return d}}
let prefs=recall("prefs",{tax:8,exact:true,cond:"new",perStore:false,targets:{}});
let data=null,showAll=false;

$("tax").value=prefs.tax;$("exact").checked=prefs.exact;$("cond").value=prefs.cond;$("perStore").checked=prefs.perStore;
function savePrefs(){store("prefs",prefs)}
["exact","perStore"].forEach(id=>$(id).addEventListener("change",e=>{prefs[id]=e.target.checked;savePrefs();render()}));
$("cond").addEventListener("change",e=>{prefs.cond=e.target.value;savePrefs();render()});
$("tax").addEventListener("input",e=>{prefs.tax=e.target.value;savePrefs();render()});
$("target").addEventListener("input",e=>{if(!data)return;prefs.targets[squash(data.query)]=e.target.value;savePrefs();render()});

function renderRecent(){
  const r=recall("recent",[]);
  $("recent").innerHTML=r.length?'<span class="eyebrow">Recent</span>'+r.map(q=>`<button type="button" data-q="${esc(q)}">${esc(q)}</button>`).join(""):"";
}
$("recent").addEventListener("click",e=>{const b=e.target.closest("button[data-q]");if(b){$("q").value=b.dataset.q;run(b.dataset.q)}});

async function status(){
  try{const s=await (await fetch("/api/status")).json();$("demo").hidden=!s.demo;return s}
  catch(e){showErr("Deal Scout isn't running. Start it again from your terminal.");return null}
}

function showErr(m){$("err").hidden=!m;$("err").textContent=m||""}

async function run(q){
  q=q.trim();if(!q)return;
  showErr("");$("loading").hidden=false;$("go").disabled=true;
  try{
    const r=await fetch("/api/search?q="+encodeURIComponent(q));const j=await r.json();
    if(!r.ok)throw new Error(j.error||"Search failed.");
    data=j;showAll=false;
    const rec=[q,...recall("recent",[]).filter(x=>x.toLowerCase()!==q.toLowerCase())].slice(0,8);store("recent",rec);renderRecent();
    render();
  }catch(e){showErr(e.message==="Failed to fetch"?"Deal Scout isn't running. Start it again from your terminal.":e.message)}
  finally{$("loading").hidden=true;$("go").disabled=false}
}
$("searchForm").addEventListener("submit",e=>{e.preventDefault();run($("q").value)});

const cost=o=>o.price*(1+num(prefs.tax)/100)+(o.shipping||0);

function filtered(){
  const extra=linksFor();
  let list=data.results.slice(),notes=[];
  const tokens=data.query.split(/\s+/).map(squash).filter(t=>t.length>1);
  if(prefs.exact&&tokens.length){
    const m=list.filter(o=>{const t=squash(o.title);return tokens.every(k=>t.includes(k))});
    if(m.length)list=m;else notes.push("No listing matched every word of your search, so all results are shown.");
    const ACC=/\b(case|cases|cover|covers|cable|cables|charger|charging|replacement|pads?|cushions?|earpads?|stand|mount|holder|protector|skin|skins|decal|adapter|strap|bag|pouch|sleeve|remote|battery|batteries|filter|filters|parts?|compatible)\b/i;
    const ql=data.query.toLowerCase();
    const noAcc=list.filter(o=>{const w=(o.title.match(ACC)||[])[0];return !w||ql.includes(w.toLowerCase())});
    if(noAcc.length)list=noAcc;
    const ps=list.filter(o=>o.tag!=="Added by link").map(o=>o.price).sort((a,b)=>a-b);
    if(ps.length>=3){const med=ps[Math.floor(ps.length/2)];list=list.filter(o=>o.tag==="Added by link"||o.price>=med*0.4)}
  }
  if(prefs.cond==="new"){const n=list.filter(o=>o.condition==="New");if(n.length)list=n;else notes.push("No new-condition listings found, so used and refurbished are shown.")}
  list=list.concat(extra);
  list.sort((a,b)=>cost(a)-cost(b));
  if(prefs.perStore){const seen=new Set();list=list.filter(o=>{const k=o.store.toLowerCase();if(seen.has(k))return false;seen.add(k);return true})}
  const hidden=data.results.length+extra.length-list.length;
  if(hidden>0)notes.push(`${hidden} other listing${hidden>1?"s":""} hidden by your filters.`);
  return {list,notes};
}

function render(){
  if(!data)return;
  $("res").hidden=false;
  $("resHead").textContent=data.query;
  $("meta").textContent=(data.cached?"Saved from a search in the last 15 min":"Checked "+new Date(data.fetchedAt).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"}));
  const okStores=data.stores.filter(x=>x.status==="ok").length;
  $("stores").innerHTML=data.stores.map(x=>`<span class="st ${x.status}" title="${esc(x.message)}"><i></i><b>${esc(x.store)}</b> ${x.status==="ok"?`<small>${x.count} listing${x.count>1?"s":""}</small>`:x.status==="none"?"<small>no matches</small>":`<small>${x.status==="blocked"?"blocked lookup":"didn't load"}</small> <a href="${esc(x.searchUrl)}" target="_blank" rel="noopener">open ↗</a>`}</span>`).join("")+(linksFor().length?`<span class="st ok"><i></i><b>Your links</b> <small>${linksFor().length}</small></span>`:"");
  $("target").value=prefs.targets[squash(data.query)]??"";
  const {list,notes}=filtered();$("filterNote").textContent=notes.join(" ");
  const tgt=num(prefs.targets[squash(data.query)]);
  if(!list.length){$("verdict").innerHTML=`<div class="big">—</div><div><p>${data.stores.some(x=>x.status==="ok")?"No listings matched. Try the brand plus model number, or untick “Exact product only”.":"None of the stores returned prices this time. They sometimes turn away automated lookups; try again in a few minutes, or paste a listing link below."}</p></div>`;["picks","bars","rows","legend"].forEach(id=>$(id).innerHTML="");$("moreBars").hidden=true;return}
  const best=list[0],worst=list[list.length-1];
  const chips=[];
  if(best.condition!=="New")chips.push(`<span class="chip miss">${esc(best.condition)}</span>`);
  if(best.shipping==null)chips.push(`<span class="chip miss">Shipping not listed</span>`);
  if(tgt>0)chips.push(cost(best)<=tgt?`<span class="chip hit">At or under your target</span>`:`<span class="chip miss">${money(cost(best)-tgt)} over your target</span>`);
  $("verdict").innerHTML=`<div class="big">${money(cost(best))}</div><div><p><b>${esc(best.store)}</b> has the best deal of ${list.length} price${list.length>1?"s":""}.</p>
    <p class="sub">${list.length>1?`Saves ${money(cost(worst)-cost(best))} versus the priciest (${esc(worst.store)}).`:"Only one listing matched."}</p>${chips.length?`<div class="chips">${chips.join("")}</div>`:""}</div>`;
  // picks
  const cards=[{l:"Best deal",o:best,s:"Lowest total with shipping and estimated tax"}];
  const newB=list.find(o=>o.condition==="New");if(newB&&newB!==best)cards.push({l:"Cheapest brand-new",o:newB,s:`${money(cost(newB)-cost(best))} more than the best, but new`});
  const sticker=list.reduce((a,b)=>b.price<a.price?b:a);if(sticker!==best)cards.push({l:"Lowest sticker price",o:sticker,s:`Costs ${money(cost(sticker)-cost(best))} more once shipping is added`});
  const rated=list.filter(o=>o.rating&&o.reviews>=20).sort((a,b)=>b.rating-a.rating||cost(a)-cost(b))[0];if(rated&&rated!==best)cards.push({l:"Top rated",o:rated,s:`${rated.rating}★ from ${Number(rated.reviews).toLocaleString()} reviews`});
  if(list.length>1&&cards.length<4&&!cards.some(k=>k.o===worst))cards.push({l:"Priciest option",o:worst,s:`${money(cost(worst)-cost(best))} more than the best deal`});
  $("picks").innerHTML=cards.map(k=>`<div class="pick"><span class="eyebrow">${k.l}</span><span class="v">${money(cost(k.o))}</span><b>${esc(k.o.store)}</b><span class="s">${k.s}</span></div>`).join("");
  // bars
  const LIMIT=12,shown=showAll?list:list.slice(0,LIMIT);
  const max=Math.max(cost(worst),tgt)*1.02;
  $("legend").innerHTML=`<span><i style="background:var(--accent)"></i>Best deal</span><span><i style="background:var(--muted);opacity:.45"></i>Other listings</span>${tgt>0?`<span><i style="border-left:2px dashed var(--warn);width:0;height:12px;border-radius:0"></i>Your target ${money(tgt)}</span>`:""}<span>Total you pay · hover for the breakdown</span>`;
  $("bars").innerHTML=shown.map((o,r)=>`<div class="cmp-row${r===0?" best":""}" data-i="${r}" tabindex="0">
    <div class="cmp-name"><b>${r+1}. ${esc(o.store)}</b><span>${esc(o.condition)}${o.shipping==null?" · shipping not listed":""}</span></div>
    <div class="track"><div class="bar" style="width:${(cost(o)/max*100).toFixed(2)}%"></div>${tgt>0?`<div class="tgt" style="left:${(tgt/max*100).toFixed(2)}%"></div>`:""}</div>
    <div class="cmp-val"><b>${money(cost(o))}</b><span>${r===0?"Best deal":"+"+money(cost(o)-cost(best))}</span></div></div>`).join("");
  $("moreBars").hidden=list.length<=LIMIT;$("moreBars").textContent=showAll?"Show top 12":`Show all ${list.length}`;
  currentList=list;
  // table
  $("rows").innerHTML=list.map((o,r)=>{const u=safeUrl(o.link),img=safeUrl(o.thumbnail);return `<tr class="${r===0?"best":""}">
    <td class="m">${r+1}</td><td>${img?`<img src="${esc(img)}" alt="" loading="lazy">`:""}</td>
    <td class="ttl">${esc(o.title)}${o.tag?`<span class="sm">${esc(o.tag)}</span>`:""}</td><td><b>${esc(o.store)}</b>${o.seller?`<span class="sm">Sold by ${esc(o.seller)}</span>`:""}</td><td>${esc(o.condition)}</td>
    <td class="m r">${money(o.price)}${o.oldPrice?`<span class="sm strike">${money(o.oldPrice)}</span>`:""}</td>
    <td class="m r">${o.shipping==null?`<span class="sm">Not listed</span>`:o.shipping===0?"Free":money(o.shipping)}</td>
    <td class="m r"><b>${money(cost(o))}</b></td>
    <td class="m">${o.rating?`${o.rating}★<span class="sm">${o.reviews?Number(o.reviews).toLocaleString()+" reviews":""}</span>`:"—"}</td>
    <td>${u?`<a href="${esc(u)}" target="_blank" rel="noopener">View ↗</a>`:""}${o.tag==="Added by link"?` <button type="button" class="x" data-rm="${esc(o.link)}" aria-label="Remove this price">×</button>`:""}</td></tr>`}).join("");
}
let currentList=[];
function linkKey(){return "links."+squash(data?data.query:"")}
function linksFor(){return data?recall(linkKey(),[]):[]}
$("rows").addEventListener("click",e=>{const b=e.target.closest("[data-rm]");if(!b)return;store(linkKey(),linksFor().filter(o=>o.link!==b.dataset.rm));render()});
$("linkForm").addEventListener("submit",async e=>{
  e.preventDefault();const url=$("link").value.trim();if(!url||!data)return;
  $("linkBtn").disabled=true;$("linkMsg").textContent="Reading that page…";
  try{
    const r=await fetch("/api/link?url="+encodeURIComponent(url));const j=await r.json();
    if(!r.ok)throw new Error(j.error||"Couldn't read that page.");
    store(linkKey(),[...linksFor().filter(o=>o.link!==j.offer.link),j.offer]);
    $("link").value="";$("linkMsg").textContent=`Added ${j.offer.store} at ${money(j.offer.price)}.`;render();
  }catch(err){$("linkMsg").textContent=err.message==="Failed to fetch"?"Deal Scout isn't running.":err.message}
  finally{$("linkBtn").disabled=false}
});
$("moreBars").onclick=()=>{showAll=!showAll;render()};

(function(){
  const cmp=$("cmp"),tip=$("tip");
  function show(row,x,y){
    const o=currentList[+row.dataset.i];if(!o)return;
    tip.innerHTML=`<div style="display:block;margin-bottom:4px;white-space:normal">${esc(o.title)}</div>${o.oldPrice?`<div><span>Was</span><span>${money(o.oldPrice)}</span></div>`:""}
      <div><span>Price</span><span>${money(o.price)}</span></div><div><span>Est. tax</span><span>${money(o.price*num(prefs.tax)/100)}</span></div>
      <div><span>Shipping</span><span>${o.shipping==null?"Not listed":o.shipping===0?"Free":money(o.shipping)}</span></div><div class="t"><span>You pay</span><span>${money(cost(o))}</span></div>`;
    tip.hidden=false;const b=cmp.getBoundingClientRect(),tw=tip.offsetWidth;
    let left=x-b.left+14;if(left+tw>b.width-8)left=x-b.left-tw-14;if(left<8)left=8;
    tip.style.left=left+"px";tip.style.top=(y-b.top+14)+"px";
  }
  cmp.addEventListener("mousemove",e=>{const r=e.target.closest(".cmp-row");r?show(r,e.clientX,e.clientY):tip.hidden=true});
  cmp.addEventListener("mouseleave",()=>tip.hidden=true);
  cmp.addEventListener("focusin",e=>{const r=e.target.closest(".cmp-row");if(r){const rb=r.getBoundingClientRect();show(r,rb.left+rb.width/2,rb.bottom-8)}});
  cmp.addEventListener("focusout",()=>tip.hidden=true);
})();

renderRecent();status();$("q").focus();
</script></body></html>'''


def main():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("Port %d is busy. Deal Scout may already be running: open http://127.0.0.1:%d" % (PORT, PORT))
        print("Or pick another port:  DEAL_SCOUT_PORT=8766 python3 deal_scout.py")
        sys.exit(1)
    url = "http://127.0.0.1:%d" % PORT
    print("Deal Scout is running at " + url + ("  (sample data mode)" if DEMO else ""))
    print("Keep this window open while you use it. Press Ctrl+C to stop.")
    if os.environ.get("DEAL_SCOUT_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
