# Clinic Image Checker v2

This version supports forbidden images stored either as normal image files OR inside ZIP files.

## Folder structure

```text
clinic-image-checker/
│
├── checker.py
├── requirements.txt
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

## Similarity thresholds

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

## Important

This first test version crawls the supplied page only.

It does NOT yet recursively visit every internal page.

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
