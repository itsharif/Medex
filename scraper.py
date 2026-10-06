import argparse
import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://medex.com.bd"
BRANDS_URL = f"{BASE_URL}/brands"

BATCH_SIZE = 500

REQUEST_DELAY = 0.30
MAX_RETRIES = 5
RETRY_DELAY = 2

TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


SECTION_HEADINGS = {
    "indications",
    "pharmacology",
    "dosage",
    "administration",
    "interaction",
    "contraindications",
    "side effects",
    "pregnancy & lactation",
    "precautions & warnings",
    "overdose effects",
    "therapeutic class",
    "storage conditions",
    "pack size & price",
}


DOSAGE_FORMS = {
    "tablet",
    "capsule",
    "soft gelatin capsule",
    "hard gelatin capsule",

    "syrup",
    "suspension",
    "solution",
    "oral solution",
    "oral suspension",

    "oral gel",
    "gel",

    "shampoo",

    "cream",
    "ointment",
    "lotion",

    "powder",
    "granules",

    "injection",
    "infusion",

    "eye drop",
    "eye drops",

    "ear drop",
    "ear drops",

    "nasal drop",
    "nasal drops",
    "nasal spray",

    "spray",

    "suppository",
    "pessary",

    "mouthwash",
    "mouth rinse",

    "inhaler",
    "respules",
    "nebules",

    "lozenge",

    "chewable tablet",
    "dispersible tablet",
    "effervescent tablet",

    "extended release tablet",
    "sustained release tablet",
    "modified release tablet",
    "enteric coated tablet",
    "film coated tablet",
    "controlled release tablet",

    "topical solution",
    "topical gel",
    "oral powder",

    "vaginal cream",
    "vaginal tablet",
}


def clean_text(value):
    if not value:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_url(url):
    if not url:
        return ""

    url = url.strip()

    if url.startswith("/"):
        return urljoin(BASE_URL, url)

    if url.startswith("http://"):
        url = "https://" + url[7:]

    return url


def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def get_page(session, url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                url,
                timeout=TIMEOUT,
                allow_redirects=True,
            )

            if response.status_code == 200:
                return response.text

            if response.status_code in (403, 429, 500, 502, 503, 504):
                wait = RETRY_DELAY * attempt
                print(
                    f"    HTTP {response.status_code}; "
                    f"retry {attempt}/{MAX_RETRIES} after {wait}s"
                )
                time.sleep(wait)
                continue

            print(f"    HTTP {response.status_code}")
            return None

        except requests.RequestException as exc:
            wait = RETRY_DELAY * attempt
            print(
                f"    Request error: {exc}; "
                f"retry {attempt}/{MAX_RETRIES} after {wait}s"
            )
            time.sleep(wait)

    return None


# ---------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------

def extract_medicine_links(html_text):
    soup = BeautifulSoup(html_text, "html.parser")

    links = set()

    for a in soup.find_all("a", href=True):
        href = clean_text(a.get("href", ""))

        if not href:
            continue

        # Convert relative URL to absolute URL
        url = normalize_url(href)

        if not url:
            continue

        # Get only the path portion
        path = urlparse(url).path

        # MedEx medicine URL pattern:
        # /brands/13717/3-bion-100-mg-tablet
        if re.match(r"^/brands/\d+/.+", path):
            links.add(url)

    return links


def discover_urls():
    all_urls = set()

    page = 1

    while True:
        if page == 1:
            url = BRANDS_URL
        else:
            url = f"{BRANDS_URL}?page={page}"

        print(f"[DISCOVERY] Page {page}: {url}")

        session = get_session()
        html_text = get_page(session, url)

        if not html_text:
            print("  Failed to load discovery page.")
            break

        links = extract_medicine_links(html_text)

        print(f"  Found {len(links)} medicine links")

        before = len(all_urls)
        all_urls.update(links)
        after = len(all_urls)

        print(f"  Total unique URLs: {after}")

        # Stop if page has no medicine links
        if not links:
            break

        # Stop if page is no longer adding anything
        if after == before:
            print("  No new URLs found. Discovery finished.")
            break

        page += 1

        time.sleep(0.5)

        # Safety limit
        if page > 5000:
            print("Safety limit reached.")
            break

    urls = sorted(all_urls)

    print()
    print("=" * 60)
    print(f"DISCOVERY COMPLETE")
    print(f"Total medicine URLs: {len(urls)}")
    print("=" * 60)

    return urls


# ---------------------------------------------------------
# PARSING HELPERS
# ---------------------------------------------------------

def get_title_parts(soup):
    title = ""

    if soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))

    if not title:
        return []

    return [
        clean_text(part)
        for part in title.split("|")
        if clean_text(part)
    ]


def get_h1(soup):
    """
    Primary brand-name source.

    Example:
    <h1>A-Migel</h1>

    We deliberately do NOT derive the name from dosage text.
    """

    candidates = []

    for h1 in soup.find_all("h1"):
        text = clean_text(h1.get_text(" ", strip=True))

        if text:
            candidates.append(text)

    if not candidates:
        return ""

    # Prefer short h1 values that are not section headings.
    for value in candidates:
        if value.lower() not in SECTION_HEADINGS:
            return value

    return candidates[0]


def looks_like_section_heading(value):
    if not value:
        return True

    return value.strip().lower() in SECTION_HEADINGS


def looks_like_company(value):
    if not value:
        return False

    lower = value.lower()

    company_words = [
        "ltd.",
        "limited",
        "pharmaceutical",
        "pharmaceuticals",
        "laboratories",
        "lab.",
        "healthcare",
        "health care",
        "industries",
        "enterprise",
        "company",
        "plc",
        "pharma",
    ]

    return any(word in lower for word in company_words)


def looks_like_strength(value):
    if not value:
        return False

    value = clean_text(value)

    patterns = [
        r"\d+\s*mg\b",
        r"\d+\s*mcg\b",
        r"\d+\s*g\b",
        r"\d+\s*kg\b",
        r"\d+\s*iu\b",
        r"\d+\s*%\s*(w/w|w/v|v/v)?",
        r"\d+\s*(mg|mcg|g)\s*/\s*\d+\s*(ml|g|dose)",
        r"\d+\s*mg\s*/\s*\d+\s*ml",
        r"\d+\s*mg\s*\+\s*",
        r"\d+\s*mcg\s*\+\s*",
        r"\d+\s*mg\s*/\s*vial",
        r"\(\s*\d+",
    ]

    return any(re.search(pattern, value, re.I) for pattern in patterns)


def find_dosage_from_image(soup):
    """
    MedEx commonly exposes dosage form in image alt text.

    Example:
    Image: Oral Gel
    """

    for img in soup.find_all("img"):
        alt = clean_text(img.get("alt", ""))

        if not alt:
            continue

        alt_clean = re.sub(
            r"^(image|photo|picture)\s*:\s*",
            "",
            alt,
            flags=re.I,
        ).strip()

        if alt_clean.lower() in DOSAGE_FORMS:
            return alt_clean

        # Handle duplicated values such as:
        # "Oral Gel Oral Gel"
        for dosage in sorted(DOSAGE_FORMS, key=len, reverse=True):
            if alt_clean.lower() == f"{dosage} {dosage}":
                return dosage.title()

    return ""


def find_dosage_from_text(soup):
    """
    Controlled vocabulary fallback.
    """

    text = clean_text(soup.get_text(" ", strip=True))

    # Prefer longer dosage forms first.
    sorted_forms = sorted(DOSAGE_FORMS, key=len, reverse=True)

    for dosage in sorted_forms:
        pattern = r"\b" + re.escape(dosage) + r"\b"

        if re.search(pattern, text, re.I):
            return dosage.title()

    return ""


def find_generic(soup):
    """
    Prefer links to /generics/.
    """

    candidates = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")

        if "/generics/" in href:
            text = clean_text(a.get_text(" ", strip=True))

            if text and not looks_like_section_heading(text):
                candidates.append(text)

    if candidates:
        # Usually the first matching generic is the product generic.
        return candidates[0]

    return ""


def find_company(soup, title_parts):
    """
    First try title structure:
    A-Migel | 2% w/w | Oral Gel | Bengali title | ACME Laboratories Ltd.
    
    But because title structures can vary, only accept a title part
    when it strongly looks like a company.
    """

    for part in title_parts:
        if looks_like_company(part):
            return part

    # Search links/text containing company-like wording.
    candidates = []

    for a in soup.find_all("a", href=True):
        text = clean_text(a.get_text(" ", strip=True))

        if looks_like_company(text):
            candidates.append(text)

    if candidates:
        return candidates[0]

    # Look around product summary.
    text_nodes = soup.find_all(string=True)

    for node in text_nodes:
        text = clean_text(node)

        if looks_like_company(text) and len(text) < 150:
            return text

    return ""


def find_strength(soup, title_parts):
    """
    Prefer title second part when it looks like strength.
    """

    for part in title_parts:
        if looks_like_strength(part):
            return part

    # Search visible short text nodes.
    for node in soup.find_all(string=True):
        text = clean_text(node)

        if 1 <= len(text) <= 100 and looks_like_strength(text):
            return text

    return ""


def remove_known_suffix_from_name(name):
    """
    Remove repeated dosage-form / strength suffixes from
    the medicine brand name.

    Examples:
        A-Migel Oral Gel Oral Gel -> A-Migel
        Nizoral Shampoo Shampoo   -> Nizoral
        Mycon Cream Cream         -> Mycon
        Arexel Tablet Tablet      -> Arexel
    """

    name = clean_text(name)

    if not name:
        return ""

    # Remove dosage suffix repeatedly.
    # Maximum 5 rounds prevents accidental infinite loops.
    for _ in range(5):

        old_name = name

        for dosage in sorted(
            DOSAGE_FORMS,
            key=len,
            reverse=True
        ):
            pattern = (
                r"\s+"
                + re.escape(dosage)
                + r"\s*$"
            )

            name = re.sub(
                pattern,
                "",
                name,
                flags=re.I
            ).strip()

        if name == old_name:
            break

    # Remove trailing strength if present.
    strength_pattern = (
        r"\s+"
        r"(?:"
        r"\d+(?:\.\d+)?\s*"
        r"(?:mg|mcg|g|kg|iu|%)"
        r"(?:\s*(?:w/w|w/v|v/v))?"
        r"(?:\s*/\s*\d+\s*(?:ml|g|dose|vial))?"
        r"|"
        r"\(\s*[^)]*"
        r"(?:mg|mcg|g|iu|%)"
        r"[^)]*\)"
        r")"
        r"\s*$"
    )

    name = re.sub(
        strength_pattern,
        "",
        name,
        flags=re.I
    ).strip()

    return clean_text(name)


def validate_record(record):
    reasons = []

    name = record.get("name", "")
    generic = record.get("generic_name", "")
    dosage = record.get("dosage", "")
    strength = record.get("strength", "")
    company = record.get("company", "")

    if not name:
        reasons.append("missing_name")

    if len(name) > 150:
        reasons.append("name_too_long")

    if looks_like_section_heading(name):
        reasons.append("name_is_section_heading")

    # Detect obvious contamination.
    for dosage_form in DOSAGE_FORMS:
        if re.search(
            r"\s+" + re.escape(dosage_form) + r"\s*$",
            name,
            re.I,
        ):
            reasons.append("name_contains_dosage")

    if generic and looks_like_section_heading(generic):
        reasons.append("generic_is_section_heading")

    if not generic:
        reasons.append("missing_generic")

    if not strength:
        reasons.append("missing_strength")

    if not dosage:
        reasons.append("missing_dosage")

    if not company:
        reasons.append("missing_company")

    if company and looks_like_section_heading(company):
        reasons.append("company_is_section_heading")

    return reasons


# ---------------------------------------------------------
# PRODUCT PARSER
# ---------------------------------------------------------

def parse_medicine_page(url, html_text):
    soup = BeautifulSoup(html_text, "html.parser")

    title_parts = get_title_parts(soup)

    # -----------------------------------------------------
    # NAME
    # -----------------------------------------------------

    name = get_h1(soup)

if not name and title_parts:
    name = clean_text(title_parts[0])
else:
    name = clean_text(name)

    # -----------------------------------------------------
    # GENERIC
    # -----------------------------------------------------

    generic = find_generic(soup)

    if not generic:
        for part in title_parts:
            part = clean_text(part)

            if not part:
                continue

            if part.lower() == name.lower():
                continue

            if looks_like_strength(part):
                continue

            if looks_like_company(part):
                continue

            if part.lower() in {
                x.lower() for x in DOSAGE_FORMS
            }:
                continue

            if looks_like_section_heading(part):
                continue

            if len(part) < 200 and re.search(r"[A-Za-z]", part):
                generic = part
                break

    # -----------------------------------------------------
    # STRENGTH
    # -----------------------------------------------------

    strength = find_strength(
        soup,
        title_parts
    )

    # -----------------------------------------------------
    # DOSAGE
    # -----------------------------------------------------

    dosage = find_dosage_from_image(soup)

if not dosage:
    for part in title_parts:
        part_lower = part.lower()

        if part_lower in {
            x.lower() for x in DOSAGE_FORMS
        }:
            dosage = part
            break

if not dosage:
    dosage = find_dosage_from_text(soup)

    # Clean medicine name AFTER dosage is known.
name = remove_known_suffix_from_name(name)

    # -----------------------------------------------------
    # COMPANY
    # -----------------------------------------------------

    company = find_company(
        soup,
        title_parts
    )

    # -----------------------------------------------------
    # BUILD RECORD
    # -----------------------------------------------------

    record = {
        "id": "",
        "name": name,
        "generic_name": generic,
        "strength": strength,
        "dosage": dosage,
        "company": company,
        "source_url": url,
        "needs_review": False,
        "review_reasons": [],
    }

    # -----------------------------------------------------
    # VALIDATION
    # -----------------------------------------------------

    reasons = validate_record(record)

    record["needs_review"] = bool(reasons)
    record["review_reasons"] = reasons

    # -----------------------------------------------------
    # DETERMINISTIC ID
    # -----------------------------------------------------

    identity = "|".join([
        clean_text(name).lower(),
        clean_text(generic).lower(),
        clean_text(strength).lower(),
        clean_text(dosage).lower(),
        clean_text(company).lower(),
    ])

    record["id"] = hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:24]

    return record


# ---------------------------------------------------------
# BATCH SCRAPING
# ---------------------------------------------------------

def load_urls(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        urls = data.get("urls", [])
    else:
        urls = data

    return [
        normalize_url(url)
        for url in urls
        if normalize_url(url)
    ]


def scrape_batch(urls, batch_number, output_dir):
    start = (batch_number - 1) * BATCH_SIZE
    end = start + BATCH_SIZE

    batch_urls = urls[start:end]

    print()
    print("=" * 70)
    print(f"BATCH {batch_number}")
    print(f"URL range: {start + 1} - {min(end, len(urls))}")
    print(f"Batch size: {len(batch_urls)}")
    print("=" * 70)

    if not batch_urls:
        print("No URLs in this batch.")
        return {
            "batch": batch_number,
            "success": 0,
            "failed": 0,
            "records": [],
        }

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    session = get_session()

    records = []
    failed = []

    for index, url in enumerate(batch_urls, start=1):

        print(
            f"[{index}/{len(batch_urls)}] ",
            end="",
            flush=True,
        )

        html_text = get_page(session, url)

        if not html_text:
            print("[FAILED]")
            failed.append(url)
            continue

        try:
            record = parse_medicine_page(
                url,
                html_text,
            )

            records.append(record)

            status = "REVIEW" if record["needs_review"] else "OK"

            print(
                f"[{status}] {record['name']}"
            )

            if index <= 15:
                print(
                    f"       Strength : {record['strength']}"
                )
                print(
                    f"       Generic  : {record['generic_name']}"
                )
                print(
                    f"       Dosage   : {record['dosage']}"
                )
                print(
                    f"       Company  : {record['company']}"
                )

        except Exception as exc:
            print(f"[PARSE ERROR] {exc}")
            failed.append(url)

        time.sleep(REQUEST_DELAY)

    batch_data = {
        "batch": batch_number,
        "total_urls": len(batch_urls),
        "success": len(records),
        "failed": len(failed),
        "failed_urls": failed,
        "records": records,
    }

    output_file = (
        output_dir /
        f"batch_{batch_number:03d}.json"
    )

    with open(
        output_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            batch_data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("=" * 70)
    print(f"BATCH {batch_number} COMPLETE")
    print(f"Success: {len(records)}")
    print(f"Failed : {len(failed)}")
    print(f"Review : {sum(1 for x in records if x['needs_review'])}")
    print(f"Saved  : {output_file}")
    print("=" * 70)

    return batch_data


# ---------------------------------------------------------
# MERGE
# ---------------------------------------------------------

def merge_batches(batch_dir, output_file):
    batch_dir = Path(batch_dir)

    files = sorted(
        batch_dir.glob("batch_*.json")
    )

    print(f"Found {len(files)} batch files.")

    all_records = []
    failed_urls = []

    seen_ids = set()
    duplicate_count = 0

    for file in files:
        try:
            with open(
                file,
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            for record in data.get("records", []):

                record_id = record.get("id")

                if record_id in seen_ids:
                    duplicate_count += 1
                    continue

                seen_ids.add(record_id)
                all_records.append(record)

            failed_urls.extend(
                data.get("failed_urls", [])
            )

        except Exception as exc:
            print(
                f"Could not read {file}: {exc}"
            )

    final = {
        "source": "MedEx Bangladesh",
        "total_records": len(all_records),
        "total_failed": len(failed_urls),
        "duplicate_records_removed": duplicate_count,
        "records_needing_review": sum(
            1
            for record in all_records
            if record.get("needs_review")
        ),
        "failed_urls": sorted(set(failed_urls)),
        "records": all_records,
    }

    output_file = Path(output_file)
    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        output_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            final,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("=" * 70)
    print("MERGE COMPLETE")
    print(f"Records : {len(all_records)}")
    print(f"Failed  : {len(set(failed_urls))}")
    print(f"Duplicates removed: {duplicate_count}")
    print(
        "Needs review:",
        final["records_needing_review"],
    )
    print(f"Saved: {output_file}")
    print("=" * 70)


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--discover",
        action="store_true",
    )

    parser.add_argument(
        "--urls-file",
        default="medex_urls.json",
    )

    parser.add_argument(
        "--batch",
        type=int,
    )

    parser.add_argument(
        "--output-dir",
        default="output/batches",
    )

    parser.add_argument(
        "--merge",
        action="store_true",
    )

    parser.add_argument(
        "--batch-dir",
        default="output/batches",
    )

    parser.add_argument(
        "--output-file",
        default="output/medex_medicines.json",
    )

    args = parser.parse_args()

    # DISCOVERY
    if args.discover:
        urls = discover_urls()

        with open(
            args.urls_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                {
                    "total": len(urls),
                    "urls": urls,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

        print(
            f"Saved URL list: {args.urls_file}"
        )

        return

    # BATCH
    if args.batch is not None:

        urls = load_urls(
            args.urls_file
        )

        scrape_batch(
            urls,
            args.batch,
            args.output_dir,
        )

        return

    # MERGE
    if args.merge:

        merge_batches(
            args.batch_dir,
            args.output_file,
        )

        return

    parser.print_help()


if __name__ == "__main__":
    main()
