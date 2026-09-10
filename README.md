# Clinic Image Checker v2

This version supports forbidden images stored either as normal image files OR inside ZIP files.

## Folder structure

```text
clinic-image-checker/
│
├── checker.py
├── requirements.txt
├── config.example.json
│
├── forbidden-images/
│   ├── forbidden-001.zip
│   ├── forbidden-002.zip
│   ├── forbidden-003.zip
│   │
│   ├── another-folder/
│   │   ├── image.jpg
│   │   └── image.webp
│   │
│   └── ...
│
└── results/
```

You do NOT need to extract the ZIP files.

The script reads JPG/JPEG/PNG/WEBP files directly from ZIP archives in memory.

## Install

Open PowerShell in the `clinic-image-checker` folder:

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Copy `config.example.json` to `config.json` and set the shared Basic Auth
credentials once:

```json
{
  "auth": {
    "username": "testuser",
    "password": "change-me"
  },
  "max_pages": 1000
}
```

`config.json` is local-only and should not be committed. The same credentials
are used for every site run. Override the file with `--config path\to\file.json`
when needed.

You can also put multiple sites in `config.json` and run them all in order:

```json
{
  "sites": [
    "https://www.atsuko-dental-office.jp/",
    "https://www.moriguchi-tsuda-ortho.com/"
  ],
  "auth": {
    "username": "testuser",
    "password": "change-me"
  },
  "max_pages": 1000
}
```

Then run:

```powershell
python checker.py
```

Each site keeps its own `results/<domain>/` folder, report, and download
cache. A single site can still be run with `python checker.py https://site.example`.

## Run one project

```powershell
python checker.py https://example-clinic.jp
```

If the domain does not have `https://`, this also works:

```powershell
python checker.py example-clinic.jp
```

## Output

```text
results/
└── example-clinic.jp/
    ├── downloaded/
    │   ├── 0001_xxx.jpg
    │   └── ...
    │
    └── report.html
```

Open `report.html` in Chrome.

The report shows only MATCH and POSSIBLE results, so you don't have to look through every safe image.

## Forbidden-image cache

The first run creates `forbidden-images/.image-checker-cache.json`.
Unchanged image files and ZIP archives reuse their cached pHash and dHash
fingerprints on later runs,
so ZIP files do not need to be opened again. The cache is automatically
refreshed when a file's size or modified time changes.

## HTTP Basic Auth

The recommended method is the shared `config.json` above. Credentials can
still be provided per run with command-line options:

```powershell
python checker.py https://test.example.com `
  --auth-username testuser `
  --auth-password-file .\test-password.txt
```

For repeated use, environment variables avoid putting the password in shell
history:

```powershell
$env:IMAGE_CHECKER_USERNAME = "testuser"
$env:IMAGE_CHECKER_PASSWORD = "test-password"
python checker.py https://test.example.com
```

`--auth-username` and `--auth-password` also work directly. Both username and
password must be supplied together. Credentials are only sent through the
HTTP session and are not written to the HTML report.

## Similarity thresholds

The score combines pHash (overall visual layout) with dHash (edge structure).
Using both prevents simple, similarly bright images from being reported as a
possible match based on pHash alone.

Default:

```text
95%+       MATCH
88–94.9%   POSSIBLE
<88%       SAFE
```

Change them if needed:

```powershell
python checker.py https://example-clinic.jp --threshold 85 --match-threshold 93
```

## Site crawling and output reuse

The checker now follows same-domain links and collects image URLs from every
visited HTML page. `max_pages` defaults to 1000 to prevent accidental
infinite crawls; increase it in `config.json` for larger sites.
The console output and HTML report show how many pages were crawled. `www` and
non-`www` links for the same domain are treated as the same site.

Each domain always reuses `results/<domain>/`. The
`results/<domain>/.download-cache.json` manifest reuses already downloaded
images on later runs, so rerunning a project does not create duplicate image
files. The HTML report is regenerated with the current crawl results.

## Important

Also, visual similarity is a screening tool, not legal proof. Always manually verify MATCH/POSSIBLE results before reporting a copyright/image-use issue.

## What it currently detects

Website:
- `<img src>`
- `srcset`
- lazy-load image attributes
- `<picture><source>`
- inline `background-image`

Formats:
- JPG
- JPEG
- PNG
- WEBP

Forbidden source:
- normal image files
- ZIP archives containing JPG/JPEG/PNG/WEBP
- nested folders
