import requests
from bs4 import BeautifulSoup
import json
import hashlib
import re
import time
import os
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin


# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://medex.com.bd"
BRANDS_URL = f"{BASE_URL}/brands"

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

URL_FILE = OUTPUT_DIR / "medex_urls.json"

BATCH_DIR = OUTPUT_DIR / "batches"
BATCH_DIR.mkdir(exist_ok=True)

FINAL_FILE = OUTPUT_DIR / "medex_medicines.json"

FAILED_FILE = OUTPUT_DIR / "medex_failed.json"

# Parallel workers per GitHub job
WORKERS = 5

# Small delay between requests
REQUEST_DELAY = 0.25

MAX_RETRIES = 4
RETRY_DELAY = 2

# Number of medicines in each batch
BATCH_SIZE = 500


# =========================================================
# HTTP HEADERS
# =========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# =========================================================
# DOSAGE FORMS
# =========================================================

DOSAGE_FORMS = [
    "Dispersible Tablet",
    "Chewable Tablet",
    "Effervescent Tablet",
    "Enteric Coated Tablet",
    "Extended Release Tablet",
    "Sustained Release Tablet",
    "Film Coated Tablet",
    "Soft Gelatin Capsule",
    "Hard Capsule",
    "Oral Suspension",
    "Oral Solution",
    "IV Infusion",
    "IM Injection",
    "IV Injection",
    "Eye Drops",
    "Ear Drops",
    "Nasal Drops",
    "Nasal Spray",
    "Tablet",
    "Capsule",
    "Syrup",
    "Suspension",
    "Solution",
    "Injection",
    "Infusion",
    "Cream",
    "Ointment",
    "Gel",
    "Lotion",
    "Drops",
    "Spray",
    "Inhaler",
    "Powder",
    "Granules",
    "Suppository",
    "Pessary",
    "Mouthwash",
    "Shampoo",
    "Soap",
    "Paint",
    "Kit",
    "Patch",
    "Lozenge",
]


BAD_VALUES = {
    "indication",
    "indications",
    "dosage",
    "dosage & administration",
    "administration",
    "description",
    "pharmacology",
    "contraindications",
    "side effects",
    "precautions & warnings",
    "therapeutic class",
    "storage conditions",
    "pregnancy & lactation",
    "interaction",
}


# =========================================================
# UTILITIES
# =========================================================

def clean_text(value):

    if not value:
        return ""

    value = value.replace("\xa0", " ")

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_url(url):

    if not url:
        return None

    url = url.strip()

    if url.startswith("/"):
        url = urljoin(BASE_URL, url)

    url = url.split("?")[0]
    url = url.split("#")[0]

    return url.rstrip("/")


def is_medicine_url(url):

    if not url:
        return False

    return bool(
        re.match(
            r"^https://medex\.com\.bd/brands/\d+/.+",
            url
        )
    )


# =========================================================
# REQUEST
# =========================================================

def get_page(url):

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=30
            )

            if response.status_code == 200:
                return response.text

            print(
                f"[HTTP {response.status_code}] {url}",
                flush=True
            )

        except Exception as e:

            print(
                f"[ERROR] {url} -> {e}",
                flush=True
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    return None


# =========================================================
# JSON
# =========================================================

def save_json(path, data):

    path = Path(path)

    temp = Path(
        str(path) + ".tmp"
    )

    with open(
        temp,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    temp.replace(path)


def load_json(path, default):

    path = Path(path)

    if not path.exists():
        return default

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return default


# =========================================================
# DISCOVERY
# =========================================================

def extract_medicine_links(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    urls = set()

    for a in soup.find_all(
        "a",
        href=True
    ):

        url = normalize_url(
            a.get("href")
        )

        if is_medicine_url(url):
            urls.add(url)

    return urls


def discover_urls():

    print(
        "\n======================================"
    )
    print(
        "MEDEx URL DISCOVERY"
    )
    print(
        "======================================\n"
    )

    urls = set()

    page = 1

    while True:

        if page == 1:
            url = BRANDS_URL
        else:
            url = f"{BRANDS_URL}?page={page}"

        print(
            f"[DISCOVERY] Page {page}",
            flush=True
        )

        html = get_page(url)

        if not html:

            print(
                "[STOP] Page unavailable",
                flush=True
            )

            break

        found = extract_medicine_links(
            html
        )

        print(
            f"  Found: {len(found)}",
            flush=True
        )

        if not found:

            print(
                "[STOP] No more medicine links",
                flush=True
            )

            break

        old = len(urls)

        urls.update(found)

        new = len(urls) - old

        print(
            f"  New: {new}",
            flush=True
        )

        print(
            f"  Total: {len(urls)}",
            flush=True
        )

        page += 1

        time.sleep(REQUEST_DELAY)

    urls = sorted(urls)

    save_json(
        URL_FILE,
        urls
    )

    print(
        f"\nTotal medicine URLs: {len(urls)}"
    )

    return urls


# =========================================================
# FIELD EXTRACTION
# =========================================================

def extract_name(soup):

    h1 = soup.find("h1")

    if not h1:
        return ""

    name = clean_text(
        h1.get_text(
            " ",
            strip=True
        )
    )

    if name.lower() in BAD_VALUES:
        return ""

    return name


def extract_generic(soup):

    # Strong method:
    # find links pointing to generic pages

    for a in soup.find_all(
        "a",
        href=True
    ):

        href = a.get("href", "")

        if "/generics/" in href:

            value = clean_text(
                a.get_text(
                    " ",
                    strip=True
                )
            )

            if (
                value
                and value.lower() not in BAD_VALUES
                and len(value) < 150
            ):

                return value

    return ""


def extract_company(soup):

    for a in soup.find_all(
        "a",
        href=True
    ):

        href = a.get("href", "")

        if "/companies/" in href:

            value = clean_text(
                a.get_text(
                    " ",
                    strip=True
                )
            )

            if value:
                return value

    return ""


def looks_like_strength(value):

    if not value:
        return False

    return bool(
        re.search(
            r"\d+(?:\.\d+)?\s*"
            r"(?:mg|mcg|g|kg|ml|l|iu|%)",
            value,
            flags=re.I
        )
    )


def extract_strength_from_text(text):

    if not text:
        return ""

    pattern = r"""
        \d+(?:\.\d+)?\s*
        (?:mg|mcg|g|kg|ml|l|iu|%)
        (?:
            \s*/\s*
            \d+(?:\.\d+)?\s*
            (?:mg|mcg|g|kg|ml|l|iu|%)
        )*
    """

    match = re.search(
        pattern,
        text,
        flags=re.I | re.X
    )

    if match:

        return clean_text(
            match.group(0)
        )

    return ""


def extract_strength(soup):

    # First check H1 parent
    h1 = soup.find("h1")

    if h1:

        parent = h1.parent

        if parent:

            text = clean_text(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            value = extract_strength_from_text(
                text
            )

            if value:
                return value

    # Page title
    title = soup.find("title")

    if title:

        text = clean_text(
            title.get_text(
                " ",
                strip=True
            )
        )

        value = extract_strength_from_text(
            text
        )

        if value:
            return value

    return ""


def detect_dosage_form(text):

    if not text:
        return ""

    text = clean_text(text)

    for form in sorted(
        DOSAGE_FORMS,
        key=len,
        reverse=True
    ):

        if re.search(
            rf"\b{re.escape(form)}\b",
            text,
            flags=re.I
        ):

            return form

    return ""


def extract_dosage(soup, url):

    # Look around H1
    h1 = soup.find("h1")

    if h1:

        parent = h1.parent

        if parent:

            value = detect_dosage_form(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            if value:
                return value

    # URL fallback
    slug = url.split("/")[-1]
    slug = slug.replace("-", " ")

    return detect_dosage_form(slug)


# =========================================================
# NAME CLEANUP
# =========================================================

def clean_medicine_name(
    name,
    dosage
):

    name = clean_text(name)

    if not name:
        return ""

    if dosage:

        # Remove:
        # Tablet
        # Tablet Tablet
        # Capsule Capsule
        # etc.

        pattern = (
            rf"(?:\s+{re.escape(dosage)})+$"
        )

        name = re.sub(
            pattern,
            "",
            name,
            flags=re.I
        ).strip()

    return name


# =========================================================
# URL FALLBACK
# =========================================================

def fallback_name_from_url(url):

    slug = url.split("/")[-1]

    slug = slug.replace(
        "-",
        " "
    )

    return clean_text(slug)


def fallback_strength_from_url(url):

    slug = url.split("/")[-1]

    slug = slug.replace(
        "-",
        " "
    )

    return extract_strength_from_text(
        slug
    )


# =========================================================
# VALIDATION
# =========================================================

def validate(record):

    reasons = []

    if not record["name"]:
        reasons.append(
            "missing_name"
        )

    if not record["generic_name"]:
        reasons.append(
            "missing_generic"
        )

    if record["generic_name"].lower() in BAD_VALUES:
        reasons.append(
            "invalid_generic"
        )

    if len(
        record["generic_name"]
    ) > 150:

        reasons.append(
            "generic_too_long"
        )

    if record["strength"]:

        if not looks_like_strength(
            record["strength"]
        ):

            reasons.append(
                "invalid_strength"
            )

    if not record["unit_dosage"]:

        reasons.append(
            "missing_dosage"
        )

    if not record["company"]:

        reasons.append(
            "missing_company"
        )

    return reasons


# =========================================================
# ID
# =========================================================

def create_id(record):

    raw = "|".join([
        record["name"].lower(),
        record["strength"].lower(),
        record["generic_name"].lower(),
        record["unit_dosage"].lower(),
        record["company"].lower()
    ])

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:24]


# =========================================================
# PARSE
# =========================================================

def parse_medicine(url):

    html = get_page(url)

    if not html:
        return None

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    name = extract_name(
        soup
    )

    generic = extract_generic(
        soup
    )

    strength = extract_strength(
        soup
    )

    dosage = extract_dosage(
        soup,
        url
    )

    company = extract_company(
        soup
    )

    # Fallbacks

    if not name:

        name = fallback_name_from_url(
            url
        )

    if not strength:

        strength = fallback_strength_from_url(
            url
        )

    # IMPORTANT:
    # Remove duplicate dosage from name

    name = clean_medicine_name(
        name,
        dosage
    )

    record = {
        "id": "",
        "name": name,
        "strength": strength,
        "generic_name": generic,
        "unit_dosage": dosage,
        "company": company,
        "source_url": url,
        "needs_review": False,
        "review_reasons": []
    }

    reasons = validate(
        record
    )

    if reasons:

        record["needs_review"] = True
        record["review_reasons"] = reasons

    record["id"] = create_id(
        record
    )

    return record


# =========================================================
# BATCH
# =========================================================

def get_batch_number():

    if len(sys.argv) < 2:

        print(
            "Usage: python scraper.py batch 1"
        )

        sys.exit(1)

    try:

        return int(
            sys.argv[2]
        )

    except:

        print(
            "Invalid batch number"
        )

        sys.exit(1)


def scrape_batch(
    urls,
    batch_number
):

    total = len(urls)

    start = (
        batch_number - 1
    ) * BATCH_SIZE

    end = min(
        start + BATCH_SIZE,
        total
    )

    batch_urls = urls[
        start:end
    ]

    print(
        "\n======================================"
    )

    print(
        f"BATCH {batch_number}"
    )

    print(
        f"URLs {start + 1} - {end}"
    )

    print(
        f"Total in batch: {len(batch_urls)}"
    )

    print(
        "======================================\n"
    )

    if not batch_urls:

        print(
            "No URLs in this batch."
        )

        return

    results = []

    failed = []

    # Thread worker
    def worker(url):

        time.sleep(
            REQUEST_DELAY
        )

        return (
            url,
            parse_medicine(url)
        )

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {
            executor.submit(
                worker,
                url
            ): url

            for url in batch_urls
        }

        completed = 0

        for future in as_completed(
            futures
        ):

            url = futures[
                future
            ]

            completed += 1

            try:

                url, record = future.result()

                if record:

                    results.append(
                        record
                    )

                    print(
                        f"[{completed}/{len(batch_urls)}] "
                        f"[OK] "
                        f"{record['name']}",
                        flush=True
                    )

                else:

                    failed.append(
                        url
                    )

                    print(
                        f"[{completed}/{len(batch_urls)}] "
                        f"[FAILED] "
                        f"{url}",
                        flush=True
                    )

            except Exception as e:

                failed.append(
                    url
                )

                print(
                    f"[{completed}/{len(batch_urls)}] "
                    f"[ERROR] {url} -> {e}",
                    flush=True
                )

    # =====================================================
    # SAVE BATCH
    # =====================================================

    batch_file = (
        BATCH_DIR
        / f"batch_{batch_number:03d}.json"
    )

    save_json(
        batch_file,
        results
    )

    failed_file = (
        BATCH_DIR
        / f"failed_{batch_number:03d}.json"
    )

    save_json(
        failed_file,
        failed
    )

    print(
        "\n======================================"
    )

    print(
        f"BATCH {batch_number} COMPLETE"
    )

    print(
        f"Success: {len(results)}"
    )

    print(
        f"Failed : {len(failed)}"
    )

    print(
        f"Saved  : {batch_file}"
    )

    print(
        "======================================"
    )


# =========================================================
# MAIN
# =========================================================

def main():

    command = (
        sys.argv[1]
        if len(sys.argv) > 1
        else ""
    )

    # ---------------------------------------------
    # DISCOVERY
    # ---------------------------------------------

    if command == "discover":

        discover_urls()

        return

    # ---------------------------------------------
    # BATCH
    # ---------------------------------------------

    if command == "batch":

        batch_number = get_batch_number()

        urls = load_json(
            URL_FILE,
            []
        )

        if not urls:

            print(
                "URL list not found."
            )

            print(
                "Run: python scraper.py discover"
            )

            sys.exit(1)

        scrape_batch(
            urls,
            batch_number
        )

        return

    # ---------------------------------------------
    # MERGE
    # ---------------------------------------------

    if command == "merge":

        merge_batches()

        return

    print(
        """
Commands:

python scraper.py discover
python scraper.py batch 1
python scraper.py batch 2
python scraper.py merge
"""
    )


# =========================================================
# MERGE FUNCTION
# =========================================================

def merge_batches():

    print(
        "\n======================================"
    )

    print(
        "MERGING BATCHES"
    )

    print(
        "======================================\n"
    )

    all_records = []
    seen_ids = set()

    for file in sorted(
        BATCH_DIR.glob(
            "batch_*.json"
        )
    ):

        records = load_json(
            file,
            []
        )

        print(
            f"{file.name}: "
            f"{len(records)}"
        )

        for record in records:

            record_id = record.get(
                "id"
            )

            if (
                record_id
                and record_id not in seen_ids
            ):

                seen_ids.add(
                    record_id
                )

                all_records.append(
                    record
                )

    save_json(
        FINAL_FILE,
        all_records
    )

    # Merge failed URLs

    failed = []

    for file in sorted(
        BATCH_DIR.glob(
            "failed_*.json"
        )
    ):

        data = load_json(
            file,
            []
        )

        failed.extend(
            data
        )

    failed = sorted(
        set(failed)
    )

    save_json(
        FAILED_FILE,
        failed
    )

    print(
        "\n======================================"
    )

    print(
        "MERGE COMPLETE"
    )

    print(
        f"Total medicines: "
        f"{len(all_records)}"
    )

    print(
        f"Failed URLs: "
        f"{len(failed)}"
    )

    print(
        f"Final file: "
        f"{FINAL_FILE}"
    )

    print(
        "======================================"
    )


if __name__ == "__main__":
    main()
