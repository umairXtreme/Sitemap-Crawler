"""
crawler.py — Antigravity Sitemap Crawler engine
Includes: CrawlSettings, platform detection, Shopify fast-path,
URL categorisation, robots.txt support, 404 exclusion, pattern filters.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urljoin, urlparse
from xml.dom import minidom

import aiohttp
from bs4 import BeautifulSoup


# ─── Constants ────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

ALWAYS_SKIP_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".rar", ".tar", ".gz", ".mp3", ".mp4",
    ".avi", ".mov", ".mkv", ".xml", ".json", ".txt", ".csv",
}

PDF_EXT = ".pdf"

# Common non-content paths to skip by default
DEFAULT_EXCLUDES = [
    "/cart", "/checkout", "/account", "/login", "/logout",
    "/register", "/admin", "/wp-admin", "/wp-login",
    "?add-to-cart=", "?s=", "?search=", "__pycache__",
]

# URL-type patterns: order matters (more specific first)
URL_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("product",    ["/products/", "/product/", "/shop/", "/item/", "/items/", "/p/"]),
    ("collection", ["/collections/", "/collection/", "/category/", "/categories/",
                    "/cat/", "/department/", "/c/"]),
    ("blog",       ["/blog/", "/blogs/", "/news/", "/articles/", "/article/",
                    "/posts/", "/post/"]),
    ("tag",        ["/tag/", "/tags/", "/label/", "/topic/"]),
]


# ─── Data Structures ──────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE    = "done"
    FAILED  = "failed"


@dataclass
class CrawlSettings:
    """All user-configurable options for a crawl session."""
    # Filters
    exclude_404: bool = True          # only keep URLs returning HTTP 200
    include_pdfs: bool = False        # also include .pdf URLs
    respect_robots: bool = True       # honour robots.txt Disallow rules

    # Pattern filters (plain substrings / prefixes)
    exclude_patterns: list[str] = field(default_factory=list)
    include_only_patterns: list[str] = field(default_factory=list)

    # Content-type gates (empty list = all allowed)
    url_types: list[str] = field(default_factory=lambda: [
        "page", "product", "collection", "blog", "tag"
    ])

    # Platform override: "auto", "shopify", "wordpress", "generic"
    platform: str = "auto"
    
    # Advanced / Sitemap specific
    max_depth: int = 0               # 0 = unlimited
    last_modified: str = "today"     # "today" or "none"
    changefreq: str = "none"         # e.g., "weekly", "none"


@dataclass
class CrawlJob:
    job_id: str
    base_url: str
    max_urls: int
    settings: CrawlSettings = field(default_factory=CrawlSettings)
    status: JobStatus = JobStatus.PENDING

    # Live counters
    visited_count: int = 0
    queue_count:   int = 0
    total_found:   int = 0

    # Platform detected
    platform_detected: str = "generic"

    # URL-type breakdown  {"page": 0, "product": 0, ...}
    type_counts: dict[str, int] = field(default_factory=lambda: {
        "page": 0, "product": 0, "collection": 0, "blog": 0, "tag": 0
    })

    started_at:  float = 0.0
    finished_at: float = 0.0
    error: str = ""
    sitemap_xml: str = ""


# Global in-memory job store
JOBS: dict[str, CrawlJob] = {}


# ─── URL helpers ──────────────────────────────────────────────────────────────

def categorize_url(url: str) -> str:
    """Return the content-type category of a URL."""
    path = urlparse(url).path.lower()
    for category, patterns in URL_TYPE_PATTERNS:
        if any(p in path for p in patterns):
            return category
    return "page"


def is_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(PDF_EXT)


def passes_filters(url: str, domain: str, settings: CrawlSettings,
                   disallowed: set[str], visited: set[str]) -> bool:
    """True if the URL should be enqueued / kept."""
    parsed = urlparse(url)

    # Must be same domain
    if parsed.netloc != domain:
        return False

    path = parsed.path.lower()

    # PDFs
    if path.endswith(PDF_EXT):
        return settings.include_pdfs

    # Other static assets (always skip)
    if any(path.endswith(ext) for ext in ALWAYS_SKIP_EXTS):
        return False

    # Fragment-only links, empty
    if not url or url.startswith("#"):
        return False

    # robots.txt
    if settings.respect_robots:
        for dis in disallowed:
            if path.startswith(dis):
                return False

    # Default excludes
    for pat in DEFAULT_EXCLUDES:
        if pat in url:
            return False

    # User exclude patterns
    for pat in settings.exclude_patterns:
        pat = pat.strip()
        if pat and pat in url:
            return False

    # User include-only patterns
    if settings.include_only_patterns:
        if not any(pat.strip() in url for pat in settings.include_only_patterns if pat.strip()):
            return False

    # Content-type gate
    if settings.url_types:
        cat = categorize_url(url)
        if cat not in settings.url_types:
            return False

    return url not in visited


# ─── robots.txt ───────────────────────────────────────────────────────────────

async def fetch_robots(session: aiohttp.ClientSession, base_url: str) -> set[str]:
    """Parse robots.txt and return disallowed paths (for * user-agent)."""
    disallowed: set[str] = set()
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        async with session.get(robots_url, timeout=aiohttp.ClientTimeout(total=8),
                               headers=HEADERS, ssl=False) as r:
            if r.status == 200:
                text = await r.text(errors="replace")
                active = False
                for line in text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("user-agent:"):
                        agent = line.split(":", 1)[1].strip()
                        active = agent in ("*", "Googlebot")
                    elif active and line.lower().startswith("disallow:"):
                        path = line.split(":", 1)[1].strip()
                        if path:
                            disallowed.add(path.lower())
    except Exception:
        pass
    return disallowed


# ─── Platform Detection ───────────────────────────────────────────────────────

async def detect_platform(base_url: str, session: aiohttp.ClientSession) -> tuple[str, str]:
    """
    Returns (platform_name, final_url) after following any redirects.
    platform_name: 'shopify', 'wordpress', or 'generic'.
    Checks headers, meta tags, and known endpoints.
    """
    final_url = base_url
    try:
        async with session.get(base_url, timeout=aiohttp.ClientTimeout(total=10),
                               headers=HEADERS, ssl=False,
                               allow_redirects=True) as r:
            final_url = str(r.url)  # Capture the true URL after redirects
            
            # Shopify header
            if r.headers.get("X-ShopId") or r.headers.get("X-Shopify-Stage"):
                return "shopify", final_url
            html = await r.text(errors="replace")
            # Shopify meta tag
            if 'shopify' in html.lower() and ('Shopify.theme' in html or
                    'cdn.shopify.com' in html or 'myshopify.com' in html):
                return "shopify", final_url
            # WordPress indicators
            if "/wp-content/" in html or "/wp-includes/" in html or "wp-json" in html:
                return "wordpress", final_url
    except Exception:
        pass

    # Try WordPress REST API on the final_url
    try:
        async with session.get(urljoin(final_url, "/wp-json/"),
                               timeout=aiohttp.ClientTimeout(total=6),
                               headers=HEADERS, ssl=False) as r:
            if r.status == 200:
                return "wordpress", final_url
    except Exception:
        pass

    return "generic", final_url


# ─── HTTP fetch ───────────────────────────────────────────────────────────────

async def fetch_html(session: aiohttp.ClientSession, url: str,
                     exclude_404: bool = True) -> tuple[str | None, int]:
    """Returns (html_text_or_None, http_status)."""
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=12),
            headers=HEADERS,
            allow_redirects=True,
            ssl=False,
        ) as r:
            if r.status != 200 and exclude_404:
                return None, r.status
            ct = r.headers.get("Content-Type", "")
            if "text/html" in ct:
                return await r.text(errors="replace"), r.status
            return None, r.status
    except Exception:
        return None, 0


# ─── Shopify fast-path ────────────────────────────────────────────────────────

SHOPIFY_SITEMAP_TYPES = {
    "products":    "product",
    "collections": "collection",
    "pages":       "page",
    "blogs":       "blog",
    "articles":    "blog",
}


async def crawl_shopify(job: CrawlJob) -> None:
    """
    Parse Shopify's auto-generated sitemap index + child sitemaps.
    Much faster than HTML crawling for Shopify stores.
    """
    settings = job.settings
    base_url = job.base_url
    domain   = urlparse(base_url).netloc
    results: list[str] = []

    connector = aiohttp.TCPConnector(ssl=False, limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Fetch root sitemap index
        index_url = urljoin(base_url, "/sitemap.xml")
        try:
            async with session.get(index_url, timeout=aiohttp.ClientTimeout(total=10),
                                   headers=HEADERS, ssl=False) as r:
                if r.status != 200:
                    # Fallback to generic crawl
                    job.platform_detected = "generic"
                    await crawl_generic(job)
                    return
                xml_text = await r.text(errors="replace")
        except Exception:
            job.platform_detected = "generic"
            await crawl_generic(job)
            return

        # Find child sitemap URLs
        child_urls: list[tuple[str, str]] = []  # (url, category)
        try:
            root = ET.fromstring(xml_text)
            ns   = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for loc_el in root.findall(".//sm:loc", ns):
                child_url = (loc_el.text or "").strip()
                if not child_url:
                    continue
                # Detect category from URL slug
                cat = "page"
                slug = child_url.lower()
                for key, mapped in SHOPIFY_SITEMAP_TYPES.items():
                    if f"sitemap_{key}" in slug:
                        cat = mapped
                        break
                child_urls.append((child_url, cat))
        except ET.ParseError:
            job.platform_detected = "generic"
            await crawl_generic(job)
            return

        if not child_urls:
            # Single-level sitemap (non-standard Shopify)
            child_urls = [(index_url, "page")]

        # Fetch each child sitemap concurrently
        async def fetch_child(child_url: str, category: str):
            try:
                async with session.get(child_url, timeout=aiohttp.ClientTimeout(total=15),
                                       headers=HEADERS, ssl=False) as r:
                    if r.status != 200:
                        return
                    xml_body = await r.text(errors="replace")
                    root2 = ET.fromstring(xml_body)
                    ns2   = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                    for loc_el in root2.findall(".//sm:loc", ns2):
                        url = (loc_el.text or "").strip()
                        if not url:
                            continue
                        if urlparse(url).netloc != domain:
                            continue
                        # Content-type gate
                        if settings.url_types and category not in settings.url_types:
                            continue
                        # Exclude patterns
                        skip = False
                        for pat in settings.exclude_patterns:
                            if pat.strip() and pat.strip() in url:
                                skip = True
                                break
                        if skip:
                            continue
                        # Include-only patterns
                        if settings.include_only_patterns:
                            if not any(p.strip() in url for p in settings.include_only_patterns if p.strip()):
                                continue
                        results.append(url)
                        job.type_counts[category] = job.type_counts.get(category, 0) + 1
                        job.visited_count = len(results)
                        job.total_found   = len(results)
                        if len(results) >= job.max_urls:
                            return
            except Exception:
                pass  # child sitemap failed — continue

        tasks = [fetch_child(cu, ca) for cu, ca in child_urls]
        await asyncio.gather(*tasks)

    # Shopify doesn't have true depth, so we assign depth 1 to homepage, 2 to everything else
    url_depths: dict[str, int] = {base_url: 1}
    for u in results:
        if u != base_url:
            url_depths[u] = 2

    final_urls = sorted(set(results[:job.max_urls]))
    # ensure base_url is included if not already
    if base_url not in final_urls and len(final_urls) < job.max_urls:
        final_urls.insert(0, base_url)
        job.type_counts["page"] = job.type_counts.get("page", 0) + 1

    job.sitemap_xml    = generate_xml(final_urls, url_depths, settings)
    job.visited_count  = len(final_urls)
    job.queue_count    = 0
    job.status         = JobStatus.DONE


# ─── Generic BFS crawler ──────────────────────────────────────────────────────

async def crawl_generic(job: CrawlJob) -> None:
    """BFS HTML crawler with full settings support."""
    settings    = job.settings
    base_url    = job.base_url
    domain      = urlparse(base_url).netloc
    visited:  set[str] = set()
    to_visit: set[str] = {base_url}
    
    # Track depth for each URL to support max_depth
    url_depths: dict[str, int] = {base_url: 1}
    
    concurrency = 50

    connector = aiohttp.TCPConnector(ssl=False, limit=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Fetch robots.txt if requested
        disallowed: set[str] = set()
        if settings.respect_robots:
            disallowed = await fetch_robots(session, base_url)

        while to_visit and len(visited) < job.max_urls:
            batch = list(to_visit)[:concurrency]
            to_visit.difference_update(batch)

            tasks   = [fetch_html(session, url, settings.exclude_404) for url in batch]
            results = await asyncio.gather(*tasks)

            for url, (html, status) in zip(batch, results):
                if url in visited:
                    continue

                # 404 gate
                if settings.exclude_404 and status not in (200, 0):
                    continue

                visited.add(url)
                cat = categorize_url(url)
                job.type_counts[cat] = job.type_counts.get(cat, 0) + 1

                if html:
                    current_depth = url_depths.get(url, 1)
                    child_depth = current_depth + 1
                    
                    # Skip parsing links if we've hit the max depth
                    if settings.max_depth > 0 and current_depth >= settings.max_depth:
                        pass # We don't extract links from this page
                    else:
                        soup = BeautifulSoup(html, "html.parser")
                        for tag in soup.find_all("a", href=True):
                            href     = tag["href"].strip()
                            full_url = urljoin(url, href).split("#")[0].rstrip("?")
                            if passes_filters(full_url, domain, settings, disallowed, visited):
                                if full_url not in url_depths:
                                    url_depths[full_url] = child_depth
                                to_visit.add(full_url)

            job.visited_count = len(visited)
            job.queue_count   = len(to_visit)
            job.total_found   = job.visited_count + job.queue_count
            await asyncio.sleep(0)  # yield for polling

    job.sitemap_xml = generate_xml(sorted(visited), url_depths, settings)
    job.queue_count = 0
    job.status      = JobStatus.DONE


# ─── Entry point ──────────────────────────────────────────────────────────────

async def run_crawl(job: CrawlJob) -> None:
    """Detect platform and dispatch to the correct crawler."""
    job.status     = JobStatus.RUNNING
    job.started_at = time.time()

    try:
        connector = aiohttp.TCPConnector(ssl=False, limit=5)
        async with aiohttp.ClientSession(connector=connector) as probe:
            # Always detect platform to get the final redirected URL reliably
            detected_plat, final_url = await detect_platform(job.base_url, probe)
            
            # Update the job's base URL to the final resolved domain
            job.base_url = final_url
            
            if job.settings.platform == "auto":
                platform = detected_plat
            else:
                platform = job.settings.platform

        job.platform_detected = platform

        if platform == "shopify":
            await crawl_shopify(job)
        else:
            await crawl_generic(job)

    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error  = str(exc)
    finally:
        job.finished_at = time.time()


# ─── XML generator ────────────────────────────────────────────────────────────

def generate_xml(urls: list[str], url_depths: dict[str, int] = None, settings: CrawlSettings = None) -> str:
    if url_depths is None:
        url_depths = {}
        
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urlset = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

    for url in urls:
        path = urlparse(url).path
        url_el = ET.SubElement(urlset, "url")
        ET.SubElement(url_el, "loc").text        = url
        ET.SubElement(url_el, "lastmod").text    = now
        ET.SubElement(url_el, "changefreq").text = "weekly"
        ET.SubElement(url_el, "priority").text   = (
            "1.0" if path in ("", "/") else "0.8"
        )

    raw    = ET.tostring(urlset, encoding="utf-8")
    parsed = minidom.parseString(raw)
    return parsed.toprettyxml(indent="  ", encoding=None)
