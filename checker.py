#!/usr/bin/env python3
"""
Clinic Image Checker v2

Checks ONE published project at a time.

Forbidden images can be:
  - normal JPG/JPEG/PNG/WEBP files
  - files inside ZIP archives
  - nested inside folders

The script:
  1. Downloads images from the supplied published page.
  2. Reads forbidden images, including images INSIDE ZIP files without
     extracting the ZIP to disk.
  3. Creates two complementary perceptual hashes (pHash + dHash).
  4. Compares the published images against forbidden images.
  5. Generates an HTML report with side-by-side images.

Usage:
    python checker.py https://example.com

Thresholds:
    MATCH    >= 95%
    POSSIBLE >= 88%
    SAFE     < 88%

You can change them:
    python checker.py https://example.com --threshold 85 --match-threshold 93
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urldefrag, urlunparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError
import imagehash


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)


# ---------------------------------------------------------
# URL / HTML
# ---------------------------------------------------------

def clean_url(url, base_url):
    if not url:
        return None

    url = url.strip()

    if url.startswith(("data:", "blob:", "javascript:", "#")):
        return None

    return urldefrag(urljoin(base_url, url))[0]


def is_image_url(url):
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in IMAGE_EXTS)


def add_url(urls, url, base_url):
    url = clean_url(url, base_url)

    if url:
        urls.add(url)


def parse_srcset(value, urls, base_url):
    if not value:
        return

    for part in value.split(","):
        part = part.strip()

        if not part:
            continue

        candidate = part.split()[0]
        add_url(urls, candidate, base_url)


def extract_image_urls(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    urls = set()

    # <img>
    for tag in soup.find_all("img"):
        for attr in (
            "src",
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-lazy",
            "data-url",
            "data-image",
        ):
            add_url(urls, tag.get(attr), page_url)

        parse_srcset(tag.get("srcset"), urls, page_url)
        parse_srcset(tag.get("data-srcset"), urls, page_url)

    # <picture><source>
    for tag in soup.find_all("source"):
        parse_srcset(tag.get("srcset"), urls, page_url)

    # Inline style background-image
    for tag in soup.find_all(style=True):
        matches = re.findall(
            r'url\(["\']?([^)"\']+)["\']?\)',
            tag["style"]
        )

        for match in matches:
            add_url(urls, match, page_url)

    return sorted(urls)


def extract_page_links(html, page_url, site_host):
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    for tag in soup.find_all("a", href=True):
        link = clean_url(tag["href"], page_url)
        if not link:
            continue

        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https"):
            continue
        link_host = parsed.netloc.lower()
        if link_host != site_host and link_host.removeprefix(
            "www."
        ) != site_host.removeprefix("www."):
            continue

        links.add(link)

    return sorted(links)


def crawl_site(session, site_url, max_pages=1000):
    site_host = urlparse(site_url).netloc.lower()
    pending = deque([site_url])
    visited = set()
    image_urls = set()

    while pending and len(visited) < max_pages:
        page_url = pending.popleft()
        page_url = urldefrag(page_url)[0]
        if page_url in visited:
            continue

        visited.add(page_url)
        print(
            f"      Page [{len(visited)}] "
            f"{page_url}"
        )

        try:
            response = session.get(page_url, timeout=25)
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"[WARN] Could not open page {page_url}: {e}")
            continue

        final_url = urldefrag(response.url)[0]
        if final_url != page_url:
            visited.add(final_url)

        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and "html" not in content_type:
            continue

        image_urls.update(
            extract_image_urls(response.text, page_url)
        )

        for link in extract_page_links(
            response.text,
            page_url,
            site_host,
        ):
            if link not in visited:
                pending.append(link)

    if pending:
        print(
            f"      Page limit reached ({max_pages}); "
            f"{len(pending)} pages not visited."
        )

    return sorted(image_urls), len(visited)


# ---------------------------------------------------------
# IMAGE HASH
# ---------------------------------------------------------

def make_hashes(image_bytes):
    """Return complementary fingerprints for an image.

    pHash is good at finding resized/re-encoded copies, but it can give a
    high score to unrelated images with a similarly simple brightness layout.
    dHash captures edge structure instead, so using both substantially reduces
    those false positives.
    """
    with Image.open(io.BytesIO(image_bytes)) as im:
        im = im.convert("RGB")
        return {
            "phash": imagehash.phash(im),
            "dhash": imagehash.dhash(im),
        }


def hash_similarity(hash_a, hash_b):
    """
    pHash is normally 64 bits.
    Similarity is converted to 0-100%.
    """
    distance = hash_a - hash_b
    return max(0.0, 100.0 * (1.0 - distance / 64.0))


def similarity(image_hashes, forbidden_hashes):
    """Score a candidate using both tonal and structural image information."""
    phash_score = hash_similarity(
        image_hashes["phash"], forbidden_hashes["phash"]
    )
    dhash_score = hash_similarity(
        image_hashes["dhash"], forbidden_hashes["dhash"]
    )

    # pHash remains slightly more important for tolerant duplicate detection,
    # while dHash verifies that the image's visible structure also agrees.
    return 0.55 * phash_score + 0.45 * dhash_score


# ---------------------------------------------------------
# FORBIDDEN IMAGE SCANNER
# ---------------------------------------------------------

def image_name_from_zip(zip_path, member):
    """
    Display name used in the report.
    Example:
        forbidden-images/clinic-a.zip
          -> folder/photos/doctor.jpg
    """
    return f"{zip_path} -> {member}"


def file_signature(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def load_forbidden_cache(cache_path):
    if not cache_path.exists():
        return {}

    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("version") != 2:
            return {}
        return data.get("files", {})
    except (OSError, json.JSONDecodeError):
        print(f"[WARN] Could not read cache: {cache_path}")
        return {}


def save_forbidden_cache(cache_path, cache):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(
            {"version": 2, "files": cache},
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(cache_path)


def scan_forbidden(folder):
    """
    Scan:
      - normal image files
      - ZIP files containing images

    ZIP images are processed directly from memory.
    They are NOT extracted to disk.
    """

    folder = Path(folder)
    cache_path = folder / ".image-checker-cache.json"
    old_cache = load_forbidden_cache(cache_path)
    new_cache = {}
    records = []

    for path in folder.rglob("*"):

        if not path.is_file():
            continue

        suffix = path.suffix.lower()
        relative_name = str(path.relative_to(folder))

        try:
            signature = file_signature(path)
        except OSError as e:
            print(f"[WARN] Could not inspect file: {path} ({e})")
            continue

        cached = old_cache.get(relative_name)
        if cached and cached.get("signature") == signature:
            for cached_record in cached.get("records", []):
                records.append({
                    "name": (
                        str(path)
                        if not cached_record.get("member")
                        else image_name_from_zip(
                            path,
                            cached_record["member"],
                        )
                    ),
                    "hashes": {
                        "phash": imagehash.hex_to_hash(
                            cached_record["phash"]
                        ),
                        "dhash": imagehash.hex_to_hash(
                            cached_record["dhash"]
                        ),
                    },
                    "source": cached_record["source"],
                })
            new_cache[relative_name] = cached
            continue

        cached_records = []

        # -----------------------------------------
        # Normal image file
        # -----------------------------------------
        if suffix in IMAGE_EXTS:
            try:
                data = path.read_bytes()
                hashes = make_hashes(data)

                records.append({
                    "name": str(path),
                    "hashes": hashes,
                    "source": "file",
                })
                cached_records.append({
                    "member": None,
                    "phash": str(hashes["phash"]),
                    "dhash": str(hashes["dhash"]),
                    "source": "file",
                })

            except Exception as e:
                print(
                    f"[WARN] Could not process image: "
                    f"{path} ({e})"
                )

        # -----------------------------------------
        # ZIP archive
        # -----------------------------------------
        elif suffix == ".zip":
            print(f"      Reading ZIP: {path}")

            try:
                with zipfile.ZipFile(path, "r") as z:

                    for member in z.infolist():

                        if member.is_dir():
                            continue

                        member_suffix = Path(
                            member.filename
                        ).suffix.lower()

                        if member_suffix not in IMAGE_EXTS:
                            continue

                        try:
                            # Read directly from ZIP.
                            # Nothing is extracted to disk.
                            data = z.read(member)

                            hashes = make_hashes(data)

                            records.append({
                                "name": image_name_from_zip(
                                    path,
                                    member.filename
                                ),
                                "hashes": hashes,
                                "source": "zip",
                            })
                            cached_records.append({
                                "member": member.filename,
                                "phash": str(hashes["phash"]),
                                "dhash": str(hashes["dhash"]),
                                "source": "zip",
                            })

                        except Exception as e:
                            print(
                                f"[WARN] Could not process "
                                f"{member.filename}: {e}"
                            )

            except zipfile.BadZipFile:
                print(f"[WARN] Invalid ZIP: {path}")

            except Exception as e:
                print(
                    f"[WARN] Could not open ZIP "
                    f"{path}: {e}"
                )

        if cached_records:
            new_cache[relative_name] = {
                "signature": signature,
                "records": cached_records,
            }

    save_forbidden_cache(cache_path, new_cache)
    return records


# ---------------------------------------------------------
# DOWNLOAD
# ---------------------------------------------------------

def download_image(session, url, timeout=20):

    try:
        response = session.get(
            url,
            timeout=timeout,
            stream=True
        )

        response.raise_for_status()

        data = response.content

        if len(data) < 100:
            return None

        # Validate actual image content.
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()

        except (
            UnidentifiedImageError,
            OSError
        ):
            return None

        return data

    except requests.RequestException:
        return None


# ---------------------------------------------------------
# SAVE DOWNLOADED IMAGE
# ---------------------------------------------------------

def save_download(data, out_dir, url, index):

    parsed = urlparse(url)

    original_name = (
        Path(parsed.path).name
        or f"image_{index}.jpg"
    )

    stem = Path(original_name).stem
    ext = Path(original_name).suffix.lower()

    if ext not in IMAGE_EXTS:
        ext = ".jpg"

    digest = hashlib.sha1(
        url.encode("utf-8")
    ).hexdigest()[:10]

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        stem
    )[:80]

    filename = (
        f"{index:04d}_"
        f"{safe_name}_"
        f"{digest}"
        f"{ext}"
    )

    path = out_dir / filename
    path.write_bytes(data)

    return path


def load_json_file(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not read JSON file {path}: {e}") from e


def save_json_file(path, data):
    path.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------
# HTML REPORT
# ---------------------------------------------------------

def esc(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def file_uri(path):
    return path.resolve().as_uri()


def generate_report(
    report_path,
    site_url,
    results,
    stats,
    threshold,
    match_threshold
):

    rows = []

    # Only show POSSIBLE / MATCH.
    # SAFE images would make the report unnecessarily huge.
    for item in results:

        if item["status"] == "SAFE":
            continue

        status_class = item["status"].lower()

        rows.append(
            f"""
            <tr>
              <td>
                <img
                  src="{esc(file_uri(item["downloaded"]))}"
                  class="preview"
                >
                <div class="url">
                  {esc(item["url"])}
                </div>
              </td>

              <td>
                <div class="forbidden-name">
                  {esc(item["forbidden"])}
                </div>
              </td>

              <td class="score {status_class}">
                {item["score"]:.1f}%
              </td>

              <td>
                <b>{item["status"]}</b>
              </td>
            </tr>
            """
        )

    if not rows:
        rows.append(
            """
            <tr>
              <td colspan="4" class="safe-message">
                No possible forbidden-image matches were found.
              </td>
            </tr>
            """
        )

    html = f"""<!doctype html>
<html lang="en">

<head>

<meta charset="utf-8">

<title>Clinic Image Check Report</title>

<style>

body {{
    font-family: Arial, sans-serif;
    margin: 30px;
    background: #f6f6f6;
    color: #222;
}}

h1 {{
    margin-bottom: 5px;
}}

.summary {{
    margin: 15px 0 25px;
    padding: 15px;
    background: white;
    border-radius: 8px;
    line-height: 1.8;
}}

table {{
    width: 100%;
    border-collapse: collapse;
    background: white;
}}

th,
td {{
    padding: 14px;
    border: 1px solid #ddd;
    vertical-align: top;
    text-align: left;
}}

th {{
    background: #eee;
}}

.preview {{
    width: 260px;
    height: 190px;
    object-fit: contain;
    background: #fafafa;
    border: 1px solid #ddd;
}}

.url {{
    max-width: 280px;
    overflow-wrap: anywhere;
    margin-top: 8px;
    font-size: 12px;
    color: #555;
}}

.forbidden-name {{
    max-width: 450px;
    overflow-wrap: anywhere;
    font-family: monospace;
    font-size: 13px;
}}

.score {{
    font-size: 20px;
    font-weight: bold;
}}

.match {{
    color: #b00020;
}}

.possible {{
    color: #a15c00;
}}

.safe-message {{
    text-align: center;
    padding: 30px;
    color: #187a2f;
}}

</style>

</head>

<body>

<h1>Clinic Image Check Report</h1>

<div class="summary">

<b>Site:</b>
{esc(site_url)}
<br>

<b>Pages crawled:</b>
{stats["pages"]}
<br>

<b>Website images found:</b>
{stats["found"]}
<br>

<b>Downloaded successfully:</b>
{stats["downloaded"]}
<br>

<b>Reused from previous run:</b>
{stats["reused"]}
<br>

<b>Forbidden images indexed:</b>
{stats["forbidden"]}
<br>

<b>MATCH:</b>
{stats["match"]}
<br>

<b>POSSIBLE:</b>
{stats["possible"]}
<br>

<b>SAFE:</b>
{stats["safe"]}
<br>

<b>MATCH threshold:</b>
{match_threshold}%
<br>

<b>POSSIBLE threshold:</b>
{threshold}%

</div>

<table>

<thead>

<tr>
    <th>Published website image</th>
    <th>Forbidden image</th>
    <th>Similarity</th>
    <th>Result</th>
</tr>

</thead>

<tbody>

{''.join(rows)}

</tbody>

</table>

</body>

</html>
"""

    report_path.write_text(
        html,
        encoding="utf-8"
    )


def project_summary(site_url, output_dir, stats, results):
    """Build the compact, persistent data used by the all-project dashboard."""
    issues = []
    for item in results:
        if item["status"] == "SAFE":
            continue
        issues.append({
            "published_url": item["url"],
            "forbidden": item["forbidden"],
            "score": round(item["score"], 1),
            "status": item["status"],
        })

    return {
        "version": 1,
        "site_url": site_url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_folder": output_dir.name,
        "stats": stats,
        "issues": issues,
    }


def load_existing_project_summary(project_dir):
    """Load a project summary; make a best-effort summary for old reports too."""
    summary_path = project_dir / "summary.json"
    try:
        summary = load_json_file(summary_path, None)
        if isinstance(summary, dict) and summary.get("version") == 1:
            return summary
    except ValueError:
        pass

    # Reports created before the dashboard have no JSON summary. Include them
    # so existing checked projects do not disappear; a recheck replaces this
    # fallback with a complete, up-to-date summary.
    report_path = project_dir / "report.html"
    if not report_path.is_file():
        return None
    try:
        soup = BeautifulSoup(report_path.read_text(encoding="utf-8"), "html.parser")
    except OSError:
        return None

    summary_text = soup.select_one(".summary")
    text_value = summary_text.get_text(" ", strip=True) if summary_text else ""

    def number_after(label):
        match = re.search(rf"{re.escape(label)}:\s*(\d+)", text_value)
        return int(match.group(1)) if match else 0

    site_match = re.search(r"Site:\s*(https?://\S+)", text_value)
    issues = []
    for row in soup.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) != 4:
            continue
        status = cells[3].get_text(" ", strip=True)
        if status not in {"MATCH", "POSSIBLE"}:
            continue
        score_match = re.search(r"[\d.]+", cells[2].get_text(" ", strip=True))
        issues.append({
            "published_url": cells[0].get_text(" ", strip=True),
            "forbidden": cells[1].get_text(" ", strip=True),
            "score": float(score_match.group()) if score_match else 0.0,
            "status": status,
        })

    return {
        "version": 1,
        "site_url": site_match.group(1) if site_match else project_dir.name,
        "checked_at": datetime.fromtimestamp(
            report_path.stat().st_mtime, timezone.utc
        ).isoformat(),
        "project_folder": project_dir.name,
        "stats": {
            "pages": number_after("Pages crawled"),
            "found": number_after("Website images found"),
            "downloaded": number_after("Downloaded successfully"),
            "reused": number_after("Reused from previous run"),
            "forbidden": number_after("Forbidden images indexed"),
            "match": number_after("MATCH"),
            "possible": number_after("POSSIBLE"),
            "safe": number_after("SAFE"),
        },
        "issues": issues,
    }


def generate_all_projects_report(results_root):
    """Create one clear dashboard for every project already checked."""
    results_root = Path(results_root)
    projects = []
    for project_dir in results_root.iterdir() if results_root.exists() else []:
        if project_dir.is_dir():
            summary = load_existing_project_summary(project_dir)
            if summary:
                projects.append(summary)

    projects.sort(
        key=lambda project: (
            -project["stats"].get("match", 0),
            -project["stats"].get("possible", 0),
            project["site_url"],
        )
    )

    total_match = sum(p["stats"].get("match", 0) for p in projects)
    total_possible = sum(p["stats"].get("possible", 0) for p in projects)
    total_images = sum(p["stats"].get("found", 0) for p in projects)
    project_rows = []
    issue_rows = []

    for project in projects:
        stats = project["stats"]
        folder = project["project_folder"]
        report_link = f"{folder}/report.html"
        match_count = stats.get("match", 0)
        possible_count = stats.get("possible", 0)
        state = "Needs review" if match_count or possible_count else "Clear"
        state_class = "needs-review" if match_count or possible_count else "clear"
        checked_at = project.get("checked_at", "").replace("T", " ").replace("+00:00", " UTC")
        project_rows.append(f"""
            <tr>
              <td><a href=\"{esc(report_link)}\">{esc(project["site_url"])}</a></td>
              <td>{stats.get("found", 0)}</td>
              <td class=\"match\">{match_count}</td>
              <td class=\"possible\">{possible_count}</td>
              <td><span class=\"badge {state_class}\">{state}</span></td>
              <td class=\"checked\">{esc(checked_at)}</td>
              <td><a class=\"button\" href=\"{esc(report_link)}\">Open report</a></td>
            </tr>
        """)

        for issue in project.get("issues", []):
            issue_rows.append(f"""
                <tr>
                  <td><a href=\"{esc(report_link)}\">{esc(project["site_url"])}</a></td>
                  <td><b class=\"{esc(issue["status"].lower())}\">{esc(issue["status"])}</b></td>
                  <td class=\"score {esc(issue["status"].lower())}\">{issue["score"]:.1f}%</td>
                  <td class=\"wrap\">{esc(issue["published_url"])}</td>
                  <td class=\"wrap mono\">{esc(issue["forbidden"])}</td>
                  <td><a class=\"button\" href=\"{esc(report_link)}\">Details</a></td>
                </tr>
            """)

    if not projects:
        project_rows.append('<tr><td colspan="7" class="empty">No projects have been checked yet.</td></tr>')
    if not issue_rows:
        issue_rows.append('<tr><td colspan="6" class="empty clear-text">No MATCH or POSSIBLE images found.</td></tr>')

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>All Projects - Clinic Image Check</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 30px; background: #f5f7fa; color: #1f2937; }}
h1 {{ margin-bottom: 6px; }} .subtitle {{ color: #596579; margin-top: 0; }}
.cards {{ display: flex; gap: 15px; flex-wrap: wrap; margin: 22px 0; }}
.card {{ min-width: 170px; padding: 18px; border-radius: 10px; background: white; box-shadow: 0 1px 3px #0002; }}
.card .number {{ display: block; font-size: 30px; font-weight: bold; margin-top: 5px; }}
.card.match-card .number, .match {{ color: #b42318; }} .card.possible-card .number, .possible {{ color: #a15c00; }}
h2 {{ margin-top: 34px; }} table {{ width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 3px #0001; }}
th, td {{ padding: 12px; border: 1px solid #d9dee7; text-align: left; vertical-align: top; }} th {{ background: #eaf0f7; }}
a {{ color: #1557a0; }} .button {{ display: inline-block; padding: 6px 10px; color: white; background: #1557a0; border-radius: 5px; text-decoration: none; white-space: nowrap; }}
.badge {{ padding: 4px 8px; border-radius: 999px; font-weight: bold; white-space: nowrap; }} .needs-review {{ color: #9a3412; background: #ffedd5; }} .clear {{ color: #166534; background: #dcfce7; }}
.score {{ font-weight: bold; }} .wrap {{ max-width: 330px; overflow-wrap: anywhere; }} .mono {{ font-family: monospace; font-size: 12px; }} .checked {{ white-space: nowrap; font-size: 12px; color: #596579; }}
.empty {{ text-align: center; padding: 28px; color: #596579; }} .clear-text {{ color: #166534; }}
</style></head><body>
<h1>All Projects — Image Check Summary</h1>
<p class="subtitle">Latest result for each checked project. Rechecking a project automatically refreshes this page.</p>
<div class="cards">
  <div class="card"><span>Projects checked</span><span class="number">{len(projects)}</span></div>
  <div class="card"><span>Website images checked</span><span class="number">{total_images}</span></div>
  <div class="card match-card"><span>MATCH images</span><span class="number">{total_match}</span></div>
  <div class="card possible-card"><span>POSSIBLE images</span><span class="number">{total_possible}</span></div>
</div>
<h2>Projects</h2><table><thead><tr><th>Project</th><th>Images checked</th><th>MATCH</th><th>POSSIBLE</th><th>Status</th><th>Last checked</th><th>Details</th></tr></thead><tbody>{''.join(project_rows)}</tbody></table>
<h2>Images requiring review</h2><table><thead><tr><th>Project</th><th>Result</th><th>Score</th><th>Website image</th><th>Forbidden image</th><th>Details</th></tr></thead><tbody>{''.join(issue_rows)}</tbody></table>
</body></html>"""
    (results_root / "index.html").write_text(html, encoding="utf-8")


def generate_github_pages(results_root, publish_root):
    """Export reports as a small static site suitable for GitHub Pages.

    The scanner's downloaded-image cache is intentionally not published. Only
    previews that already appear in MATCH/POSSIBLE rows are copied.
    """
    results_root = Path(results_root)
    publish_root = Path(publish_root)
    staging_root = publish_root.with_name(publish_root.name + "-staging")

    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True)

    dashboard_path = results_root / "index.html"
    if dashboard_path.is_file():
        shutil.copy2(dashboard_path, staging_root / "index.html")

    exported_reports = 0
    exported_previews = 0
    for project_dir in results_root.iterdir() if results_root.exists() else []:
        if not project_dir.is_dir():
            continue
        report_path = project_dir / "report.html"
        if not report_path.is_file():
            continue

        destination_dir = staging_root / project_dir.name
        destination_dir.mkdir()
        soup = BeautifulSoup(report_path.read_text(encoding="utf-8"), "html.parser")
        download_dir = (project_dir / "downloaded").resolve()

        for image in soup.select("img.preview"):
            source_url = image.get("src", "")
            parsed = urlparse(source_url)
            if parsed.scheme != "file":
                image.decompose()
                continue
            source_path = Path(unquote(parsed.path.lstrip("/"))).resolve()
            if not source_path.is_file() or not source_path.is_relative_to(download_dir):
                image.decompose()
                continue

            assets_dir = destination_dir / "assets"
            assets_dir.mkdir(exist_ok=True)
            asset_name = (
                hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:12]
                + source_path.suffix.lower()
            )
            destination_image = assets_dir / asset_name
            shutil.copy2(source_path, destination_image)
            image["src"] = f"assets/{asset_name}"
            exported_previews += 1

        (destination_dir / "report.html").write_text(
            str(soup), encoding="utf-8"
        )
        exported_reports += 1

    (staging_root / ".nojekyll").write_text("", encoding="utf-8")
    if publish_root.exists():
        shutil.rmtree(publish_root)
    staging_root.replace(publish_root)
    return exported_reports, exported_previews


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description="Check one published website "
                    "against forbidden images."
    )

    parser.add_argument(
        "url",
        nargs="?",
        help="Published project URL"
    )

    parser.add_argument(
        "--forbidden",
        default="forbidden-images",
        help="Forbidden image folder"
    )

    parser.add_argument(
        "--output",
        default="results",
        help="Output folder"
    )

    parser.add_argument(
        "--publish-dir",
        default="docs",
        help="GitHub Pages export folder (set to an empty string to skip)"
    )

    parser.add_argument(
        "--threshold",
        type=float,
        default=88.0,
        help="Possible-match threshold"
    )

    parser.add_argument(
        "--match-threshold",
        type=float,
        default=95.0,
        help="High-confidence match threshold"
    )

    parser.add_argument(
        "--auth-username",
        default=None,
        help="HTTP Basic Auth username (or IMAGE_CHECKER_USERNAME)"
    )

    parser.add_argument(
        "--auth-password",
        default=None,
        help=(
            "HTTP Basic Auth password (or IMAGE_CHECKER_PASSWORD); "
            "prefer the environment variable"
        )
    )

    parser.add_argument(
        "--auth-password-file",
        default=None,
        help="Read the HTTP Basic Auth password from a local file"
    )

    parser.add_argument(
        "--config",
        default="config.json",
        help="JSON config file for shared settings"
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum same-site pages to crawl (default: config or 1000)"
    )

    args = parser.parse_args()

    try:
        config = load_json_file(Path(args.config), {})
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    if not isinstance(config, dict):
        print(f"[ERROR] Config file must contain a JSON object: {args.config}")
        sys.exit(1)

    configured_sites = config.get("sites", [])
    if configured_sites and not args.url:
        if (
            not isinstance(configured_sites, list)
            or not all(isinstance(site, str) and site.strip()
                       for site in configured_sites)
        ):
            print("[ERROR] Config 'sites' must be a list of non-empty URLs.")
            sys.exit(1)

        command_base = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--config",
            args.config,
            "--forbidden",
            args.forbidden,
            "--output",
            args.output,
            "--publish-dir",
            args.publish_dir,
            "--threshold",
            str(args.threshold),
            "--match-threshold",
            str(args.match_threshold),
        ]

        failed = False
        for site in configured_sites:
            print()
            print(f"========== Checking {site} ==========")
            result = subprocess.run(command_base + [site])
            if result.returncode != 0:
                failed = True

        sys.exit(1 if failed else 0)

    if not args.url:
        print(
            "[ERROR] Provide a URL or add site URLs to config 'sites'."
        )
        sys.exit(1)

    config_auth = config.get("auth", {})
    if not isinstance(config_auth, dict):
        print("[ERROR] Config 'auth' must be a JSON object.")
        sys.exit(1)

    site_url = args.url

    if not site_url.startswith(
        ("http://", "https://")
    ):
        site_url = "https://" + site_url

    forbidden_dir = Path(args.forbidden)

    if not forbidden_dir.exists():
        print(
            f"[ERROR] Forbidden folder not found: "
            f"{forbidden_dir}"
        )
        sys.exit(1)

    site_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        urlparse(site_url).netloc
    )

    output_dir = (
        Path(args.output)
        / site_name
    )

    downloads_dir = (
        output_dir
        / "downloaded"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    downloads_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    # -----------------------------------------
    # 1. Forbidden images
    # -----------------------------------------

    print()
    print("[1/4] Indexing forbidden images...")

    forbidden = scan_forbidden(
        forbidden_dir
    )

    print(
        f"      {len(forbidden)} "
        f"forbidden images indexed."
    )

    # -----------------------------------------
    # HTTP session
    # -----------------------------------------

    session = requests.Session()

    session.headers.update({
        "User-Agent": USER_AGENT
    })

    auth_username = (
        args.auth_username
        or config_auth.get("username")
        or os.environ.get("IMAGE_CHECKER_USERNAME")
    )
    auth_password = (
        args.auth_password
        or config_auth.get("password")
        or os.environ.get("IMAGE_CHECKER_PASSWORD")
    )

    if args.auth_password_file:
        try:
            auth_password = Path(
                args.auth_password_file
            ).read_text(encoding="utf-8").strip()
        except OSError as e:
            print(
                f"[ERROR] Could not read auth password file: {e}"
            )
            sys.exit(1)

    if bool(auth_username) != bool(auth_password):
        print(
            "[ERROR] Both auth username and auth password are required."
        )
        sys.exit(1)

    if auth_username and auth_password:
        session.auth = (auth_username, auth_password)
        print("      HTTP Basic Auth enabled.")

    # -----------------------------------------
    # 2. Website
    # -----------------------------------------

    print()
    print("[2/4] Crawling site pages...")

    configured_max_pages = config.get("max_pages", 1000)
    max_pages = args.max_pages or configured_max_pages
    if not isinstance(max_pages, int) or max_pages < 1:
        print("[ERROR] max_pages must be a positive integer.")
        sys.exit(1)

    image_urls, pages_crawled = crawl_site(
        session,
        site_url,
        max_pages,
    )

    print(
        f"      Crawled {pages_crawled} pages; found "
        f"{len(image_urls)} image URLs."
    )

    # -----------------------------------------
    # 3. Download + compare
    # -----------------------------------------

    print()
    print("[3/4] Downloading and comparing...")

    results = []

    stats = {
        "pages": pages_crawled,
        "found": len(image_urls),
        "downloaded": 0,
        "reused": 0,
        "forbidden": len(forbidden),
        "match": 0,
        "possible": 0,
        "safe": 0,
    }

    download_cache_path = output_dir / ".download-cache.json"
    try:
        download_cache = load_json_file(download_cache_path, {})
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
    if not isinstance(download_cache, dict):
        print(
            f"[ERROR] Download cache must be a JSON object: "
            f"{download_cache_path}"
        )
        sys.exit(1)

    for i, image_url in enumerate(
        image_urls,
        1
    ):

        print(
            f"      [{i}/{len(image_urls)}] "
            f"{image_url}"
        )

        cached_name = download_cache.get(image_url)
        downloaded = None
        data = None
        if isinstance(cached_name, str):
            cached_path = downloads_dir / cached_name
            if cached_path.is_file():
                try:
                    data = cached_path.read_bytes()
                    make_hashes(data)
                    downloaded = cached_path
                    stats["reused"] += 1
                except (OSError, UnidentifiedImageError):
                    data = None

        if data is None:
            data = download_image(session, image_url)

        if not data:
            continue

        stats["downloaded"] += 1

        if downloaded is None:
            downloaded = save_download(
                data,
                downloads_dir,
                image_url,
                i
            )
            download_cache[image_url] = downloaded.name

        try:

            current_hashes = make_hashes(
                data
            )

        except Exception:
            continue

        best_score = -1
        best_forbidden = None

        for forbidden_item in forbidden:

            score = similarity(
                current_hashes,
                forbidden_item["hashes"]
            )

            if score > best_score:

                best_score = score

                best_forbidden = (
                    forbidden_item["name"]
                )

        if best_score >= args.match_threshold:

            status = "MATCH"
            stats["match"] += 1

        elif best_score >= args.threshold:

            status = "POSSIBLE"
            stats["possible"] += 1

        else:

            status = "SAFE"
            stats["safe"] += 1

        results.append({
            "url": image_url,
            "downloaded": downloaded,
            "forbidden": best_forbidden,
            "score": best_score,
            "status": status,
        })

    save_json_file(download_cache_path, download_cache)

    # -----------------------------------------
    # 4. Report
    # -----------------------------------------

    print()
    print("[4/4] Creating HTML report...")

    report_path = (
        output_dir
        / "report.html"
    )

    generate_report(
        report_path,
        site_url,
        results,
        stats,
        args.threshold,
        args.match_threshold
    )

    save_json_file(
        output_dir / "summary.json",
        project_summary(site_url, output_dir, stats, results),
    )
    generate_all_projects_report(Path(args.output))
    published_reports = published_previews = 0
    if args.publish_dir:
        published_reports, published_previews = generate_github_pages(
            Path(args.output), Path(args.publish_dir)
        )

    print()
    print("========================================")
    print("DONE")
    print("========================================")
    print(
        f"Pages crawled:       {stats['pages']}"
    )
    print(
        f"Website images:     {stats['found']}"
    )
    print(
        f"Downloaded:         {stats['downloaded']}"
    )
    print(
        f"Reused:             {stats['reused']}"
    )
    print(
        f"Forbidden indexed:  {stats['forbidden']}"
    )
    print(
        f"MATCH:              {stats['match']}"
    )
    print(
        f"POSSIBLE:           {stats['possible']}"
    )
    print(
        f"SAFE:               {stats['safe']}"
    )
    print()
    print(
        f"Report: {report_path.resolve()}"
    )
    print(
        f"All-project summary: {(Path(args.output) / 'index.html').resolve()}"
    )
    if args.publish_dir:
        print(
            f"GitHub Pages export: {Path(args.publish_dir).resolve()} "
            f"({published_reports} reports, {published_previews} previews)"
        )
    print("========================================")


if __name__ == "__main__":
    main()
