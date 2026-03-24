import time
import uuid
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks
from fastapi.responses import Response, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from crawler import CrawlJob, CrawlSettings, JobStatus, JOBS, run_crawl

app = FastAPI(title="Antigravity Sitemap Generator")
templates = Jinja2Templates(directory="templates")


# ─── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ─── Start crawl ──────────────────────────────────────────────────────────────

@app.post("/start-crawl")
async def start_crawl(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    max_urls: int = Form(5000),
    platform: str = Form("auto"),
    exclude_404: str = Form("false"),
    include_pdfs: str = Form("false"),
    respect_robots: str = Form("false"),
    exclude_patterns: str = Form(""),
    include_only_patterns: str = Form(""),
    url_types: str = Form("page,product,collection,blog,tag"),
    # New options:
    max_depth: int = Form(0),
    last_modified: str = Form("today"),
    changefreq: str = Form("none")
):
    """Start a new crawl job in the background."""
    print(f"DEBUG: Start crawl called for {url}")
    parsed_url = urlparse(url)
    if not parsed_url.scheme or not parsed_url.netloc:
        return JSONResponse({"error": "Invalid URL"}, status_code=400)

    if not (1 <= max_urls <= 50000):
        raise HTTPException(status_code=400, detail="max_urls must be 1–50,000")

    settings = CrawlSettings(
        exclude_404=exclude_404.lower() == "true",
        include_pdfs=include_pdfs.lower() == "true",
        respect_robots=respect_robots.lower() == "true",
        exclude_patterns=[p.strip() for p in exclude_patterns.splitlines() if p.strip()],
        include_only_patterns=[p.strip() for p in include_only_patterns.splitlines() if p.strip()],
        url_types=[t.strip() for t in url_types.split(",") if t.strip()],
        platform=platform,
        max_depth=max_depth,
        last_modified=last_modified,
        changefreq=changefreq
    )

    job_id = str(uuid.uuid4())
    job    = CrawlJob(job_id=job_id, base_url=url, max_urls=max_urls, settings=settings)
    JOBS[job_id] = job

    background_tasks.add_task(run_crawl, job)
    return JSONResponse({"job_id": job_id})


# ─── Live progress ────────────────────────────────────────────────────────────

@app.get("/progress/{job_id}")
async def get_progress(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    now = time.time()
    elapsed = (
        round(job.finished_at - job.started_at, 1) if job.finished_at
        else round(now - job.started_at, 1) if job.started_at
        else 0
    )

    return JSONResponse({
        "status":            job.status,
        "platform_detected": job.platform_detected,
        "visited":           job.visited_count,
        "queue":             job.queue_count,
        "total_found":       job.total_found,
        "elapsed_s":         elapsed,
        "max_urls":          job.max_urls,
        "type_counts":       job.type_counts,
        "error":             job.error,
    })


# ─── Download ─────────────────────────────────────────────────────────────────

@app.get("/download/{job_id}")
async def download_sitemap(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.DONE:
        raise HTTPException(status_code=202, detail="Crawl not finished yet")

    xml_content = job.sitemap_xml
    url_count   = job.visited_count
    del JOBS[job_id]

    return Response(
        content=xml_content,
        media_type="application/xml",
        headers={
            "Content-Disposition": "attachment; filename=sitemap.xml",
            "X-URL-Count": str(url_count),
        },
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
