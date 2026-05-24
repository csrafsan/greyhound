"""
Greyhound Recorder scraper.

Reads input.csv (columns: date, name), searches each dog, opens the matching
race by date, flattens the race results table into one row, and writes output.csv.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from typing import List, Optional

import pandas as pd
from dateutil import parser as date_parser
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SEARCH_URL = "https://thegreyhoundrecorder.com.au/search/"
DEFAULT_WAIT = 20

# Output fields per race (Name, Trainer, Sire only).
OUTPUT_COLUMNS = ["Name", "Trainer", "Sire"]
MIN_RACE_TABLE_CELLS = 10  # need at least through Sire column (index 9)


def _log(msg: str) -> None:
    """Print safely on Windows consoles that lack Unicode glyphs."""
    text = str(msg)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def parse_input_date(date_str: str) -> Optional[datetime]:
    if not date_str or str(date_str).strip().lower() in ("nan", "none", ""):
        return None
    try:
        return date_parser.parse(str(date_str).strip(), dayfirst=False)
    except (ValueError, TypeError, date_parser.ParserError):
        return None


def parse_table_date(date_str: str) -> Optional[datetime]:
    if not date_str or not str(date_str).strip():
        return None
    text = str(date_str).strip()
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%d-%m-%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return date_parser.parse(text, dayfirst=True)
    except (ValueError, TypeError, date_parser.ParserError):
        return None


def dates_match(target_date: str, table_date: str) -> bool:
    target = parse_input_date(target_date)
    row_date = parse_table_date(table_date)
    if target is None or row_date is None:
        return False
    return target.date() == row_date.date()


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).strip().lower())


def dropdown_matches_dog(item_text: str, dog_name: str) -> bool:
    """Match autocomplete row like 'Fernando Bale 12-03-2013'."""
    item = normalize_name(item_text)
    dog = normalize_name(dog_name)
    if not item or not dog:
        return False
    if item == dog:
        return True
    # Name is usually at the start before the whelp/birth date suffix.
    if item.startswith(dog):
        return True
    # All words from the CSV name appear at the start of the suggestion.
    words = dog.split()
    return item.startswith(" ".join(words))


class GreyhoundScraper:
    def __init__(
        self,
        csv_file: str = "input.csv",
        output_file: str = "output.csv",
        final_output_file: str = "final_output.csv",
        errors_file: str = "errors.csv",
        headless: bool = False,
        wait_seconds: int = DEFAULT_WAIT,
    ):
        self.csv_file = csv_file
        self.output_file = output_file
        self.final_output_file = final_output_file
        self.errors_file = errors_file
        self.headless = headless
        self.wait_seconds = wait_seconds
        self.results: List[List[str]] = []
        self.errors: List[dict] = []
        self.driver: Optional[webdriver.Chrome] = None
        self.wait: Optional[WebDriverWait] = None

    def setup_driver(self) -> None:
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        self.driver = webdriver.Chrome(options=options)
        self.driver.implicitly_wait(0)
        self.wait = WebDriverWait(self.driver, self.wait_seconds)

    def load_data(self) -> Optional[pd.DataFrame]:
        try:
            df = pd.read_csv(self.csv_file)
            if "name" not in df.columns or "date" not in df.columns:
                _log("[ERROR] CSV must have columns: date, name")
                return None
            _log(f"[OK] Loaded {len(df)} records from {self.csv_file}")
            return df
        except Exception as exc:
            _log(f"[ERROR] Loading CSV: {exc}")
            return None

    def open_search_page(self) -> bool:
        assert self.driver and self.wait
        try:
            _log(f"\nOpening {SEARCH_URL}")
            self.driver.get(SEARCH_URL)
            self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            self._greyhound_search_input()
            _log("[OK] Search page loaded")
            return True
        except Exception as exc:
            _log(f"[ERROR] Opening search page: {exc}")
            return False

    def _greyhound_search_input(self):
        """Return the Greyhound Search box (not Trainer Search)."""
        assert self.wait
        selectors = [
            "input[placeholder*='dog name' i]",
            "input[placeholder*='dog' i]",
            "input[placeholder*='Dog' i]",
        ]
        for selector in selectors:
            try:
                return self.wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
            except TimeoutException:
                continue
        # Fallback: first text input on the page (Greyhound is left column).
        inputs = self.wait.until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "input[type='text']")
        )
        if not inputs:
            raise TimeoutException("No search inputs found")
        return inputs[0]

    def search_dog(self, name: str) -> bool:
        assert self.driver and self.wait
        try:
            _log(f"  Searching for: {name}")
            search_box = self._greyhound_search_input()
            search_box.clear()
            search_box.click()
            search_box.send_keys(name)
            self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "li.search-filter__results-item a.search-filter__results-link")
                )
            )
            _log(f"  [OK] Typed '{name}'")
            return True
        except TimeoutException:
            _log("  [ERROR] Search input or autocomplete not found")
            return False
        except Exception as exc:
            _log(f"  [ERROR] Search failed: {exc}")
            return False

    def _autocomplete_items(self) -> List:
        assert self.driver
        items = self.driver.find_elements(
            By.CSS_SELECTOR, "li.search-filter__results-item"
        )
        visible = []
        for item in items:
            if not item.is_displayed():
                continue
            try:
                item.find_element(By.CSS_SELECTOR, "a.search-filter__results-link")
                visible.append(item)
            except NoSuchElementException:
                continue
        return visible

    def _dropdown_link_text(self, item) -> str:
        try:
            return item.find_element(
                By.CSS_SELECTOR, "a.search-filter__results-link"
            ).text.strip()
        except NoSuchElementException:
            return item.text.strip()

    def _dropdown_secondary_text(self, item) -> str:
        try:
            return item.find_element(
                By.CSS_SELECTOR, ".search-filter__results-secondary"
            ).text.strip()
        except NoSuchElementException:
            return ""

    def _matching_dropdown_items(self, name: str) -> List:
        items = self._autocomplete_items()
        return [
            item
            for item in items
            if dropdown_matches_dog(self._dropdown_link_text(item), name)
        ]

    def _click_dropdown_item(self, item) -> bool:
        assert self.driver and self.wait
        try:
            link = item.find_element(
                By.CSS_SELECTOR, "a.search-filter__results-link"
            )
            label = self._dropdown_link_text(item)
            secondary = self._dropdown_secondary_text(item)
            suffix = f" (whelp {secondary})" if secondary else ""
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", link
            )
            self.wait.until(EC.element_to_be_clickable(link))
            link.click()
            self.wait.until(
                lambda d: "/greyhounds/" in d.current_url.lower(),
                message="Profile page did not load after dropdown click",
            )
            _log(f"  [OK] Opened profile: {label}{suffix}")
            return True
        except TimeoutException:
            _log("  [WARN] Profile page did not load for this dropdown result")
            return False
        except Exception as exc:
            _log(f"  [WARN] Could not click dropdown result: {exc}")
            return False

    def open_profile_with_date(self, name: str, target_date: str):
        """
        Try each dropdown row with the same name (e.g. two 'Riendo' entries)
        until the profile's Race Results table contains the target date.
        Returns the matching table row, or None.
        """
        assert self.driver and self.wait
        try:
            matches = self.wait.until(
                lambda d: self._matching_dropdown_items(name) or False,
                message="No dropdown results",
            )
        except TimeoutException:
            _log(f"  [ERROR] No dropdown results for '{name}'")
            return None

        if not matches:
            _log(f"  [ERROR] '{name}' not found in dropdown")
            return None

        if len(matches) > 1:
            _log(f"  Found {len(matches)} dropdown matches for '{name}'")

        for index in range(len(matches)):
            if index > 0:
                self.return_to_search()
                if not self.search_dog(name):
                    break
                matches = self._matching_dropdown_items(name)
                if index >= len(matches):
                    _log("  [WARN] Dropdown list changed; stopping retries")
                    break

            item = matches[index]
            secondary = self._dropdown_secondary_text(item)
            _log(
                f"  Trying dropdown result {index + 1}/{len(matches)}"
                + (f" (whelp {secondary})" if secondary else "")
            )

            if not self._click_dropdown_item(item):
                continue

            if not self.navigate_to_profile(name):
                _log("  [WARN] Could not reach dog profile page")
                continue

            row = self.find_matching_date_row(target_date)
            if row is not None:
                if len(matches) > 1:
                    _log(f"  [OK] Matched profile from dropdown #{index + 1}")
                return row

            _log("  [WARN] Date not on this profile; trying next dropdown result")

        _log(f"  [ERROR] Date '{target_date}' not found on any matching profile")
        return None

    def navigate_to_profile(self, name: str) -> bool:
        """
        Step 3: Confirm we are on the dog profile page (not search results),
        then scroll down to the Race Results section.
        """
        assert self.driver and self.wait
        try:
            self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

            if "/greyhounds/" not in self.driver.current_url.lower():
                profile_links = self.driver.find_elements(
                    By.CSS_SELECTOR, "a[href*='/greyhounds/']"
                )
                clicked = False
                for link in profile_links:
                    if not link.is_displayed():
                        continue
                    if dropdown_matches_dog(link.text.strip(), name):
                        self.wait.until(EC.element_to_be_clickable(link))
                        link.click()
                        clicked = True
                        break
                if not clicked:
                    _log("  [ERROR] Profile link not found after search")
                    return False
                self.wait.until(
                    lambda d: "/greyhounds/" in d.current_url.lower(),
                    message="Profile page did not load",
                )

            _log("  [Step 3] On dog profile page")
            try:
                self.scroll_to_race_results()
            except TimeoutException:
                _log("  [ERROR] Race Results section not found on profile")
                return False
            return True
        except TimeoutException:
            _log("  [ERROR] Timed out loading dog profile page")
            return False
        except Exception as exc:
            _log(f"  [ERROR] Profile navigation failed: {exc}")
            return False

    def scroll_to_race_results(self):
        """Scroll to the Race Results table on the profile Form tab."""
        assert self.driver and self.wait
        heading = self.wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//h2[normalize-space()='Race Results']")
            )
        )
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", heading
        )
        table = self.wait.until(
            lambda d: heading.find_element(
                By.XPATH, "./following::table[.//tbody/tr/td[1]/a][1]"
            )
        )
        self.wait.until(
            lambda d: len(
                table.find_elements(By.CSS_SELECTOR, "tbody tr td:first-child a")
            )
            > 0
        )
        _log("  [Step 3] Scrolled to Race Results table")
        return table

    def _race_results_table(self):
        """Return the Race Results table (call scroll_to_race_results first)."""
        assert self.driver and self.wait
        try:
            heading = self.driver.find_element(
                By.XPATH, "//h2[normalize-space()='Race Results']"
            )
            return heading.find_element(
                By.XPATH, "./following::table[.//tbody/tr/td[1]/a][1]"
            )
        except NoSuchElementException:
            pass
        # Fallback: first profile table with date links in column 1.
        link = self.wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "table tbody tr td:first-child a")
            )
        )
        table = link.find_element(By.XPATH, "./ancestor::table[1]")
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", table
        )
        return table

    def find_matching_date_row(self, target_date: str):
        assert self.wait
        try:
            table = self._race_results_table()
        except TimeoutException:
            _log("  [ERROR] Race Results table not found on profile")
            return None
        rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
        _log(f"  Looking for date: {target_date}")

        for row in rows:
            try:
                date_cell = row.find_element(By.CSS_SELECTOR, "td:first-child")
                date_text = date_cell.text.strip()
                if not date_text:
                    continue
                if dates_match(target_date, date_text):
                    _log(f"  [OK] Matched race date: {date_text}")
                    return row
            except StaleElementReferenceException:
                continue

        _log(f"  [ERROR] No row matching date '{target_date}'")
        return None

    def click_date_link(self, row) -> bool:
        """Click the date link in the Race Results row (opens the race page)."""
        assert self.wait
        try:
            date_link = row.find_element(By.CSS_SELECTOR, "td:first-child a")
            date_text = date_link.text.strip()
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", date_link
            )
            self.wait.until(EC.element_to_be_clickable(date_link))
            date_link.click()
            if not self.wait_for_race_page():
                return False
            _log(f"  [OK] Clicked race date link ({date_text})")
            return True
        except Exception as exc:
            _log(f"  [ERROR] Could not open race page: {exc}")
            return False

    def wait_for_race_page(self) -> bool:
        """
        Step 4: Wait for the race results page and scroll to the results table.
        """
        assert self.driver and self.wait
        try:
            self.wait.until(
                lambda d: "/results/" in d.current_url.lower(),
                message="Race results URL did not load",
            )
            table = self.wait.until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//table[.//thead//th[normalize-space()='Plc' or "
                        "normalize-space()='Place']]",
                    )
                )
            )
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", table
            )
            self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ".results-event-selection__name, table tbody tr td")
                )
            )
            _log("  [Step 4] On race results page - table ready")
            return True
        except TimeoutException:
            _log("  [ERROR] Race results page did not load")
            return False
        except Exception as exc:
            _log(f"  [ERROR] Race page wait failed: {exc}")
            return False

    def _race_page_table(self):
        assert self.driver and self.wait
        return self.driver.find_element(
            By.XPATH,
            "//table[.//thead//th[normalize-space()='Plc' or normalize-space()='Place']]",
        )

    @staticmethod
    def _cell_text(cell) -> str:
        text = cell.text.strip()
        if text:
            return text
        try:
            img = cell.find_element(By.TAG_NAME, "img")
            return (img.get_attribute("alt") or img.get_attribute("title") or "").strip()
        except NoSuchElementException:
            return ""

    @staticmethod
    def _extract_name_cell(cell) -> str:
        """Dog name from results-event-selection__name (no box number)."""
        try:
            return cell.find_element(
                By.CSS_SELECTOR, ".results-event-selection__name"
            ).text.strip()
        except NoSuchElementException:
            text = GreyhoundScraper._cell_text(cell)
            return re.sub(r"\s*\([^)]*\)\s*", "", text).strip()

    @staticmethod
    def _extract_link_cell(cell) -> str:
        """Step 6: trainer / sire / dam from results-event-selection__link."""
        try:
            return cell.find_element(
                By.CSS_SELECTOR, ".results-event-selection__link"
            ).text.strip()
        except NoSuchElementException:
            return GreyhoundScraper._cell_text(cell)

    def _extract_race_row(self, cells: List) -> Optional[List[str]]:
        """Name, Trainer, Sire for one runner. None if SCR."""
        if len(cells) < MIN_RACE_TABLE_CELLS:
            return None

        if self._cell_text(cells[0]).upper() == "SCR":
            return None

        name = re.sub(
            r"\s*\([^)]*\)\s*", "", self._extract_name_cell(cells[2])
        ).strip().title()
        return [
            name,
            self._extract_link_cell(cells[3]),
            self._extract_link_cell(cells[9]),
        ]

    def extract_race_table_rows(self, search_name: str) -> Optional[List[List[str]]]:
        """
        Extract Name, Trainer, Sire for the searched dog in this race.
        Skips SCR rows.
        """
        assert self.driver and self.wait
        scr_skipped = 0

        for attempt in range(3):
            try:
                table = self._race_page_table()
                row_count = len(table.find_elements(By.CSS_SELECTOR, "tbody tr"))
                data_rows: List[List[str]] = []

                for row_index in range(row_count):
                    row = table.find_elements(By.CSS_SELECTOR, "tbody tr")[row_index]
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if not cells:
                        continue

                    if self._cell_text(cells[0]).upper() == "SCR":
                        scr_skipped += 1
                        continue

                    row_data = self._extract_race_row(cells)
                    if not row_data:
                        continue
                    if dropdown_matches_dog(row_data[0], search_name):
                        data_rows.append(row_data)
                        break

                if not data_rows:
                    _log(
                        f"  [ERROR] '{search_name}' not found in race "
                        "(or scratched / SCR)"
                    )
                    return None

                if scr_skipped:
                    _log(f"  [Step 5] Skipped {scr_skipped} SCR row(s)")
                _log(
                    f"  [Step 6] {search_name} -> "
                    f"Name={data_rows[0][0]}, Trainer={data_rows[0][1]}, "
                    f"Sire={data_rows[0][2]}"
                )
                return data_rows
            except StaleElementReferenceException:
                if attempt == 2:
                    raise
                continue
            except Exception as exc:
                _log(f"  [ERROR] Extracting race table: {exc}")
                return None
        return None

    @staticmethod
    def convert_to_horizontal(rows: List[List[str]]) -> Optional[List[str]]:
        """Step 7: single row with Name, Trainer, Sire."""
        if not rows:
            return None
        flat = list(rows[0])
        _log(f"  [Step 7] Output: Name={flat[0]}, Trainer={flat[1]}, Sire={flat[2]}")
        return flat

    def return_to_search(self) -> bool:
        assert self.driver and self.wait
        try:
            self.driver.get(SEARCH_URL)
            self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            self._greyhound_search_input()
            return True
        except Exception as exc:
            _log(f"  [WARN] Return to search failed: {exc}")
            return False

    def record_error(self, name: str, date: str, step: str, message: str) -> None:
        self.errors.append(
            {"name": name, "date": date, "step": step, "error": message}
        )

    def process_record(self, name: str, date: str) -> bool:
        if not self.search_dog(name):
            self.record_error(name, date, "search", "search failed")
            self.return_to_search()
            return False

        row = self.open_profile_with_date(name, date)
        if row is None:
            self.record_error(
                name, date, "date_match", "date not found on any matching profile"
            )
            self.return_to_search()
            return False

        # Step 3 (end): click date link -> opens race page (Step 4).
        if not self.click_date_link(row):
            self.record_error(name, date, "race_link", "could not open race page")
            self.return_to_search()
            return False

        # Steps 5 & 6: Name, Trainer, Sire for this dog only (SCR skipped).
        table_rows = self.extract_race_table_rows(name)
        if not table_rows:
            self.record_error(name, date, "extract", "no race table data")
            self.return_to_search()
            return False

        # Step 7: column-major flatten for output.csv.
        flat = self.convert_to_horizontal(table_rows)
        if not flat:
            self.record_error(name, date, "flatten", "could not flatten data")
            self.return_to_search()
            return False

        # output.csv: Name, Trainer, Sire only
        self.results.append(flat)
        self.return_to_search()
        _log(f"  [OK] Done: {name} ({date})\n")
        return True

    @staticmethod
    def merge_results_to_final_row(results: List[List[str]]) -> List[str]:
        """
        One row for final_output.csv:
        all dog names, then all trainers, then all sires.
        """
        if not results:
            return []
        names = [row[0] for row in results if len(row) >= 1 and row[0]]
        trainers = [row[1] for row in results if len(row) >= 2 and row[1]]
        sires = [row[2] for row in results if len(row) >= 3 and row[2]]
        return names + trainers + sires

    def save_final_output(self) -> bool:
        """Write final_output.csv: one row from all output.csv rows."""
        try:
            flat = self.merge_results_to_final_row(self.results)
            if not flat:
                _log("[WARN] No data for final_output.csv")
                return False
            pd.DataFrame([flat]).to_csv(
                self.final_output_file, index=False, header=False
            )
            _log(
                f"[OK] Saved final row ({len(flat)} values) to "
                f"{self.final_output_file}"
            )
            return True
        except Exception as exc:
            _log(f"[ERROR] Saving final output: {exc}")
            return False

    def save_results(self) -> bool:
        try:
            if self.results:
                pd.DataFrame(self.results).to_csv(
                    self.output_file, index=False, header=False
                )
                _log(f"\n[OK] Saved {len(self.results)} rows to {self.output_file}")
                self.save_final_output()
            else:
                _log("\n[WARN] No successful rows to save")

            if self.errors:
                pd.DataFrame(self.errors).to_csv(self.errors_file, index=False)
                _log(f"[OK] Saved {len(self.errors)} errors to {self.errors_file}")
            return bool(self.results)
        except Exception as exc:
            _log(f"[ERROR] Saving output: {exc}")
            return False

    def run(self, limit: Optional[int] = None) -> None:
        df = self.load_data()
        if df is None:
            return

        if limit is not None:
            df = df.head(limit)

        successful = 0
        failed = 0

        try:
            _log("=" * 60)
            _log("GREYHOUND RECORDER BOT")
            _log("=" * 60)

            self.setup_driver()
            if not self.open_search_page():
                return

            total = len(df)
            for index, row in df.iterrows():
                name = str(row["name"]).strip()
                date = str(row["date"]).strip()
                _log(f"\n[{index + 1}/{total}] {name} | {date}")

                try:
                    if self.process_record(name, date):
                        successful += 1
                    else:
                        failed += 1
                except Exception as exc:
                    _log(f"  [ERROR] Unexpected failure: {exc}")
                    self.record_error(name, date, "unexpected", str(exc))
                    self.return_to_search()
                    failed += 1

            self.save_results()

            _log("\n" + "=" * 60)
            _log("SUMMARY")
            _log("=" * 60)
            _log(f"Total:      {total}")
            _log(f"Successful: {successful}")
            _log(f"Failed:     {failed}")
            if total:
                _log(f"Success rate: {successful / total * 100:.1f}%")
            _log("=" * 60)
        except Exception as exc:
            _log(f"[ERROR] Critical: {exc}")
        finally:
            self.save_results()
            if self.driver:
                self.driver.quit()
                _log("[OK] Browser closed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Greyhound Recorder scraper")
    parser.add_argument("--input", default="input.csv", help="Input CSV path")
    parser.add_argument("--output", default="output.csv", help="Output CSV path")
    parser.add_argument(
        "--final-output",
        default="final_output.csv",
        help="Single-row merged CSV (all names, trainers, sires)",
    )
    parser.add_argument("--errors", default="errors.csv", help="Failed records log")
    parser.add_argument("--limit", type=int, default=None, help="Process only N rows")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless")
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT, help="Wait seconds")
    args = parser.parse_args()

    scraper = GreyhoundScraper(
        csv_file=args.input,
        output_file=args.output,
        final_output_file=args.final_output,
        errors_file=args.errors,
        headless=args.headless,
        wait_seconds=args.wait,
    )
    scraper.run(limit=args.limit)


if __name__ == "__main__":
    main()
