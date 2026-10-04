"""
Disney Cruise Line scraper - Relu Consultancy hiring challenge (Objective 1)

What this script does (follows the steps in the challenge document):
  Step 1  Open https://disneycruise.disney.go.com/en-in/ (and accept a consent
          banner if one is shown).
  Step 2  Click "View Dates" (the full, unfiltered search).
  Step 3  Wait for the cruise cards to load.
  Step 4  Scroll to the end of the page until no new cards load, then read
          every card.
  Step 5  Save the raw data to raw_cards.csv (temporary file) and clean it:
          trim text, remove rows with empty fields (locations must be present),
          remove duplicates.
  Step 6  Save the cleaned data to results.csv.

To answer the five questions it also runs three filtered searches
(Pacific Coast, Halloween on the High Seas, Very Merrytime) and prints and
saves the answers (answers.txt).

How to run:
  pip install playwright pandas
  playwright install chromium
  python disney_cruise_scraper.py

Run headless (no browser window), for example in Google Colab:
  HEADLESS=1 python disney_cruise_scraper.py
"""

import os
import re
import sys

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
URL = "https://disneycruise.disney.go.com/en-in/"
CARD = "div.product-card-content"          # one cruise card

# Show the browser window by default. Set HEADLESS=1 to hide it.
# On a Linux machine without a display (such as Colab) headless is automatic.
HEADLESS = os.environ.get("HEADLESS", "0") == "1"
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    HEADLESS = True

MAX_ATTEMPTS = 3          # retries for each search if something fails
MAX_STALLS = 5            # stop scrolling after this many scrolls with no new cards
MAX_SCROLL_ROUNDS = 300   # safety limit so the loop can never run forever

RAW_CSV = "raw_cards.csv"        # temporary data (Step 5)
FINAL_CSV = "results.csv"        # cleaned data (Step 6)
ANSWERS_TXT = "answers.txt"

# Columns that must not be empty in the final file
REQUIRED_COLUMNS = ["title", "departure_port", "sailing_to", "price", "currency", "num_dates"]


class AccessBlockedError(Exception):
    """Raised when the website refuses access (HTTP 403 Access Denied)."""


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def clean_text(text):
    """Replace line breaks and repeated spaces with a single space."""
    return re.sub(r"\s+", " ", text or "").strip()


def get_text(card, selector):
    """Return the text of the first element matching the selector, or '' if it is missing."""
    element = card.locator(selector)
    if element.count() == 0:
        return ""
    return clean_text(element.first.inner_text())


def card_key(row):
    """Identify a card by its content (used to combine the two holiday lists)."""
    return (row["title"], row["sailing_to"], row["price"], row["num_dates"])


# ----------------------------------------------------------------------------
# Browser steps
# ----------------------------------------------------------------------------
def accept_consent(page):
    """Step 1: click an Agree / Accept button if the site shows one."""
    try:
        page.get_by_role("button", name=re.compile(r"^(agree|accept)", re.I)).first.click(timeout=3000)
        print("  Consent banner accepted")
    except Exception:
        pass    # no banner was shown, nothing to click


def open_search(page, filter_type=None, option=None):
    """
    Steps 1-3: open the site, optionally choose a filter, click View Dates and
    wait for the cards.

    filter_type = None           -> no filter (all cruises)
    filter_type = "destination"  -> choose an option under "Sailing to"
    filter_type = "theme"        -> choose an option under "More Filters"
    """
    response = page.goto(URL, timeout=60000)
    if response is not None and response.status == 403:
        raise AccessBlockedError(
            "The website returned 403 Access Denied to this machine or network."
        )
    page.wait_for_timeout(5000)
    accept_consent(page)

    if filter_type == "destination":
        page.locator("text=/sailing to/i >> visible=true").first.click(timeout=15000)
        page.wait_for_timeout(2000)
        page.locator(f"text={option} >> visible=true").first.click(timeout=10000)
    elif filter_type == "theme":
        page.locator("text=/more filters/i >> visible=true").first.click(timeout=15000)
        page.wait_for_timeout(2000)
        page.locator(f"text={option} >> visible=true").first.click(timeout=10000)

    page.locator("text=/view dates/i >> visible=true").first.click(timeout=15000)
    page.wait_for_selector(CARD, timeout=30000)


def read_site_count(page):
    """Read the result count the website shows (for example '949 Cruises')."""
    try:
        text = page.locator("text=/\\d+\\s+cruises/i >> visible=true").first.inner_text(timeout=5000)
        return clean_text(text)
    except Exception:
        return ""


def scroll_to_end(page):
    """Step 4: keep scrolling until no new cards load."""
    stalls = 0
    rounds = 0
    while stalls < MAX_STALLS and rounds < MAX_SCROLL_ROUNDS:
        rounds += 1
        before = page.locator(CARD).count()
        page.mouse.wheel(0, 4000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            # wait until the number of cards grows
            page.wait_for_function(
                "n => document.querySelectorAll('div.product-card-content').length > n",
                arg=before,
                timeout=10000,
            )
            stalls = 0
            print(f"  Cards loaded so far: {page.locator(CARD).count()}")
        except PlaywrightTimeoutError:
            stalls += 1
    print(f"  Scrolling finished with {page.locator(CARD).count()} cards")


def extract_cards(page):
    """Step 4: read the text fields of every card on the page."""
    cards = page.locator(CARD)
    rows = []
    for i in range(cards.count()):
        card = cards.nth(i)
        try:
            title = get_text(card, "h2.product-card-content__name")

            # a card can list several ports, join them with " | "
            ports = card.locator(".product-card-content__sailing-to li").all_inner_texts()
            sailing_to = " | ".join(clean_text(p) for p in ports if clean_text(p))

            # the dates button says "Show 1 Date" or "Show 3 Dates"
            dates_button = get_text(card, ".product-card-footer-wrapper__btn")
            match_dates = re.search(r"Show\s+(\d+)\s+Dates?", dates_button, re.I)

            # the departure port is the text after "from" in the title
            match_from = re.search(r"\bfrom\s+(.+)$", title)

            rows.append({
                "title": title,
                "departure_port": match_from.group(1).strip() if match_from else "",
                "sailing_to": sailing_to,
                "price": get_text(card, ".wrapper-price__pricing"),
                "currency": get_text(card, ".wrapper-price__currency"),
                "dates_button": dates_button,
                "num_dates": int(match_dates.group(1)) if match_dates else "",
            })
        except Exception as error:
            print(f"  Card {i} skipped: {error}")
    return rows


def collect(browser, label, filter_type=None, option=None):
    """Run one search (with retries) and return (list of card rows, site count text)."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n[{label}] attempt {attempt} of {MAX_ATTEMPTS}")
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        try:
            open_search(page, filter_type, option)
            site_count = read_site_count(page)
            print(f"  Website says: {site_count or 'count not found'}")
            scroll_to_end(page)
            rows = extract_cards(page)
            if not rows:
                raise RuntimeError("no cards were extracted")
            print(f"  Cards extracted: {len(rows)}")
            return rows, site_count
        except AccessBlockedError:
            raise       # a block will not go away by retrying
        except Exception as error:
            print(f"  Failed: {error}")
        finally:
            page.close()
    raise RuntimeError(f"Could not collect data for: {label}")


# ----------------------------------------------------------------------------
# Data cleaning (Step 5)
# ----------------------------------------------------------------------------
def clean_data(raw):
    """Clean the raw data. Returns a dataframe with no empty fields and no duplicates."""
    df = raw.copy()
    print(f"\nRows before cleaning: {len(df)}")

    # 1. trim spaces, turn empty text into a real missing value
    df = df.apply(lambda column: column.str.strip())
    df = df.replace("", pd.NA)
    df["num_dates"] = pd.to_numeric(df["num_dates"], errors="coerce")

    # 2. remove rows with an empty required field (locations must be present)
    missing = df[REQUIRED_COLUMNS].isna()
    print("Rows with an empty field, by column:")
    print(missing.sum().to_string())
    df = df[~missing.any(axis=1)]
    print(f"Rows after removing empty fields: {len(df)}")

    # 3. remove duplicate rows
    before = len(df)
    df = df.drop_duplicates()
    print(f"Duplicate rows removed: {before - len(df)}")

    # 4. tidy types and add the departure city
    df["num_dates"] = df["num_dates"].astype(int)
    # "San Diego ending in Vancouver" -> "San Diego"
    df["departure_city"] = (
        df["departure_port"]
        .str.replace(r"\s+(?:ending|with)\b.*$", "", case=False, regex=True)
        .str.strip()
    )

    columns = ["title", "departure_port", "departure_city", "sailing_to",
               "price", "currency", "num_dates", "dates_button"]
    df = df[columns].reset_index(drop=True)

    empty_cells = int(df.isna().sum().sum())
    print(f"Rows after cleaning: {len(df)} | empty cells left: {empty_cells}")
    if empty_cells != 0:
        raise RuntimeError("Cleaning failed: empty cells are still present")
    return df


# ----------------------------------------------------------------------------
# Answers
# ----------------------------------------------------------------------------
def build_answers(df, raw, site_total, pacific, pacific_site, halloween, halloween_site,
                  merrytime, merrytime_site):
    """Return the lines to print for the five questions plus reference numbers."""
    pacific_cards = {card_key(r) for r in pacific}
    holiday_cards = {card_key(r) for r in halloween} | {card_key(r) for r in merrytime}

    miami = int(df["departure_city"].str.contains("Miami", case=False).sum())
    london = int(df["departure_city"].str.contains("London", case=False).sum())
    more_than_two = int((df["num_dates"] > 2).sum())
    pacific_in_titles = int(df["title"].str.contains("Pacific", case=False).sum())

    raw_dates = pd.to_numeric(raw["num_dates"], errors="coerce").sum()
    pacific_dates = sum(int(r["num_dates"]) for r in pacific if r["num_dates"] != "")

    lines = [
        "ANSWERS",
        "-------",
        f"1. Total cruises for the Pacific (Pacific Coast filter): {len(pacific_cards)}",
        f"2. Total cruises: {len(df)}",
        f"3. Holiday cruises (Halloween on the High Seas + Very Merrytime, no double count): {len(holiday_cards)}",
        f"4. Cruises with more than 2 dates: {more_than_two}",
        f"5. Cruises departing from Miami: {miami}, London: {london}, total: {miami + london}",
        "",
        "REFERENCE",
        "---------",
        f"Cards with 'Pacific' in the title (cross-check for Q1): {pacific_in_titles}",
        f"Website count, all cruises: {site_total or 'n/a'} | sum of dates on all cards: {int(raw_dates)}",
        f"Website count, Pacific Coast: {pacific_site or 'n/a'} | sum of dates on its cards: {pacific_dates}",
        f"Website count, Halloween: {halloween_site or 'n/a'} | Very Merrytime: {merrytime_site or 'n/a'}",
        "(The website counts sailing dates, one card can hold several dates.)",
        "",
        "Departure city counts:",
        df["departure_city"].value_counts().to_string(),
    ]
    return lines


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)

        # Steps 1-4: all cruises, no filter
        all_rows, site_total = collect(browser, "All cruises")

        # Extra searches used only for the questions
        pacific, pacific_site = collect(browser, "Pacific Coast", "destination", "Pacific Coast Cruises")
        halloween, halloween_site = collect(browser, "Halloween", "theme", "Halloween on the High Seas")
        merrytime, merrytime_site = collect(browser, "Very Merrytime", "theme", "Very Merrytime")

        browser.close()

    # Step 5: temporary data, then cleaning
    pd.DataFrame(all_rows).to_csv(RAW_CSV, index=False, encoding="utf-8-sig")
    print(f"\nTemporary data saved to {os.path.abspath(RAW_CSV)}")

    raw = pd.read_csv(RAW_CSV, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df = clean_data(raw)

    # Step 6: final CSV
    df.to_csv(FINAL_CSV, index=False, encoding="utf-8-sig")
    print(f"Final data saved to {os.path.abspath(FINAL_CSV)}")

    # Answers
    lines = build_answers(df, raw, site_total, pacific, pacific_site,
                          halloween, halloween_site, merrytime, merrytime_site)
    print("\n" + "\n".join(lines))
    with open(ANSWERS_TXT, "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    print(f"\nAnswers saved to {os.path.abspath(ANSWERS_TXT)}")


if __name__ == "__main__":
    try:
        main()
    except AccessBlockedError as error:
        print(f"\nSTOPPED: {error}")
        print("Run the script from a different network or machine (for example your own computer).")
        sys.exit(1)
