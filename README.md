# Advanced Sitemap Crawler

A fast local sitemap generator built with FastAPI, asyncio, and BeautifulSoup.

This tool crawls websites and generates clean XML sitemap output with support for Shopify detection, WordPress detection, smart URL filtering, and live crawl progress.

## Highlights

- Async crawler for high-speed URL discovery
- Automatic platform detection: Shopify, WordPress, generic sites
- Shopify fast-path using native `sitemap.xml` indexes
- Live crawl status via polling endpoint
- Download sitemap as XML when crawl is complete
- Optional `robots.txt` support
- Optional exclusion of non-200 pages
- URL type filters: page, product, collection, blog, tag
- Include-only and exclude patterns
- Configurable crawl depth
- PDF inclusion toggle
- Beautiful local web UI (no cloud dependency)

## Tech Stack

- FastAPI
- Uvicorn
- aiohttp
- BeautifulSoup4
- Jinja2
- Tailwind CSS (CDN)

## Project Structure

```text
.
|-- crawler.py
|-- main.py
|-- requirements.txt
|-- templates/
|   `-- index.html
`-- README.md
```

## Quick Start

### 1) Clone

```bash
git clone https://github.com/<your-username>/sitemap-crawler.git
cd sitemap-crawler
```

### 2) Create virtual environment

```bash
python -m venv .venv
```

Windows (PowerShell):

```powershell
.\.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
source .venv/bin/activate
```

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

### 4) Run app

```bash
python main.py
```

App URL:

- http://127.0.0.1:8000

## How It Works

1. Submit a target URL and crawl options from the web UI.
2. A background crawl job is created.
3. The frontend polls the progress endpoint.
4. When done, the XML sitemap is available for download.

## Configuration Options

- `max_urls`: hard cap for discovered URLs
- `platform`: `auto`, `shopify`, `wordpress`, `generic`
- `exclude_404`: include only valid pages
- `respect_robots`: honor `robots.txt` disallow rules
- `include_pdfs`: include `.pdf` URLs
- `exclude_patterns`: remove URLs containing custom patterns
- `include_only_patterns`: keep only matching URL patterns
- `url_types`: filter by URL category
- `max_depth`: crawl depth limit (`0` means unlimited)
- `last_modified`: sitemap lastmod strategy
- `changefreq`: sitemap change frequency

## API Endpoints

- `GET /` - UI page
- `POST /start-crawl` - create crawl job
- `GET /progress/{job_id}` - fetch live status
- `GET /download/{job_id}` - download sitemap XML

## Example Workflow

1. Open the app in your browser.
2. Enter target website (for example: `https://example.com`).
3. Set crawl options.
4. Start crawl.
5. Monitor progress.
6. Download `sitemap.xml`.

## Notes

- This project is designed for local usage.
- Large websites can take time depending on network speed and crawl limits.
- Some sites may block aggressive crawling; adjust settings responsibly.

## License

This project is licensed under the MIT License. See `LICENSE` for details.
