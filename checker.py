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
  3. Creates perceptual hashes (pHash).
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
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

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

    if url and is_image_url(url):
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


# ---------------------------------------------------------
# IMAGE HASH
# ---------------------------------------------------------

def make_hash(image_bytes):
    with Image.open(io.BytesIO(image_bytes)) as im:
        im = im.convert("RGB")
        return imagehash.phash(im)


def similarity(hash_a, hash_b):
    """
    pHash is normally 64 bits.
    Similarity is converted to 0-100%.
    """
    distance = hash_a - hash_b
    return max(0.0, 100.0 * (1.0 - distance / 64.0))


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


def scan_forbidden(folder):
    """
    Scan:
      - normal image files
      - ZIP files containing images

    ZIP images are processed directly from memory.
    They are NOT extracted to disk.
    """

    records = []
    folder = Path(folder)

    for path in folder.rglob("*"):

        if not path.is_file():
            continue

        suffix = path.suffix.lower()

        # -----------------------------------------
        # Normal image file
        # -----------------------------------------
        if suffix in IMAGE_EXTS:
            try:
                data = path.read_bytes()
                h = make_hash(data)

                records.append({
                    "name": str(path),
                    "hash": h,
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

                            h = make_hash(data)

                            records.append({
                                "name": image_name_from_zip(
                                    path,
                                    member.filename
                                ),
                                "hash": h,
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

<b>Website images found:</b>
{stats["found"]}
<br>

<b>Downloaded successfully:</b>
{stats["downloaded"]}
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

    args = parser.parse_args()

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

    # -----------------------------------------
    # 2. Website
    # -----------------------------------------

    print()
    print("[2/4] Crawling project page...")

    try:

        response = session.get(
            site_url,
            timeout=25
        )

        response.raise_for_status()

    except requests.RequestException as e:

        print(
            f"[ERROR] Could not open website: {e}"
        )

        sys.exit(1)

    image_urls = extract_image_urls(
        response.text,
        site_url
    )

    print(
        f"      Found {len(image_urls)} "
        f"image URLs."
    )

    # -----------------------------------------
    # 3. Download + compare
    # -----------------------------------------

    print()
    print("[3/4] Downloading and comparing...")

    results = []

    stats = {
        "found": len(image_urls),
        "downloaded": 0,
        "forbidden": len(forbidden),
        "match": 0,
        "possible": 0,
        "safe": 0,
    }

    for i, image_url in enumerate(
        image_urls,
        1
    ):

        print(
            f"      [{i}/{len(image_urls)}] "
            f"{image_url}"
        )

        data = download_image(
            session,
            image_url
        )

        if not data:
            continue

        stats["downloaded"] += 1

        downloaded = save_download(
            data,
            downloads_dir,
            image_url,
            i
        )

        try:

            current_hash = make_hash(
                data
            )

        except Exception:
            continue

        best_score = -1
        best_forbidden = None

        for forbidden_item in forbidden:

            score = similarity(
                current_hash,
                forbidden_item["hash"]
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

    print()
    print("========================================")
    print("DONE")
    print("========================================")
    print(
        f"Website images:     {stats['found']}"
    )
    print(
        f"Downloaded:         {stats['downloaded']}"
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
    print("========================================")


if __name__ == "__main__":
    main()
