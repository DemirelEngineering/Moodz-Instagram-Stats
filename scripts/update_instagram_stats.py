from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# De drie doorgegeven volledige XPaths. De volgorde op de bronpagina is:
# volgers, volgend, berichten.
STAT_XPATHS: dict[str, str] = {
    "followers": (
        "/html/body/div[1]/div/div[3]/div[3]/div/div/div[2]/div[2]/div/"
        "div/div/div[3]/div/div[1]/div[1]/div[1]/div[1]/h4"
    ),
    "following": (
        "/html/body/div[1]/div/div[3]/div[3]/div/div/div[2]/div[2]/div/"
        "div/div/div[3]/div/div[1]/div[1]/div[1]/div[2]/h4"
    ),
    "posts": (
        "/html/body/div[1]/div/div[3]/div[3]/div/div/div[2]/div[2]/div/"
        "div/div/div[3]/div/div[1]/div[1]/div[1]/div[3]/h4"
    ),
}

# Iets minder breekbare fallback vanaf de gezamenlijke container.
STAT_RELATIVE_XPATHS: dict[str, str] = {
    "followers": (
        "(//div[div[1]/h4 and div[2]/h4 and div[3]/h4]"
        "[div[1]/h4[contains(@class,'font-weight-bolder')]])[1]/div[1]/h4"
    ),
    "following": (
        "(//div[div[1]/h4 and div[2]/h4 and div[3]/h4]"
        "[div[1]/h4[contains(@class,'font-weight-bolder')]])[1]/div[2]/h4"
    ),
    "posts": (
        "(//div[div[1]/h4 and div[2]/h4 and div[3]/h4]"
        "[div[1]/h4[contains(@class,'font-weight-bolder')]])[1]/div[3]/h4"
    ),
}


@dataclass(frozen=True)
class InstagramStats:
    username: str
    posts: int
    followers: int
    following: int
    updated_at: str
    source: str
    schema_version: int = 1


def parse_count(raw_value: str) -> int:
    """Zet 197, 1.234, 1,2K en 3M om naar een geheel getal."""
    value = raw_value.replace("\u00a0", " ").strip()
    value = re.sub(r"\s+", "", value)

    match = re.fullmatch(
        r"(?P<number>[0-9][0-9.,]*)(?P<suffix>[KMBkmb]?)",
        value,
    )
    if not match:
        raise ValueError(f"Ongeldige telwaarde: {raw_value!r}")

    number = match.group("number")
    suffix = match.group("suffix").upper()

    if suffix:
        if "," in number and "." in number:
            decimal_separator = "," if number.rfind(",") > number.rfind(".") else "."
            thousands_separator = "." if decimal_separator == "," else ","
            number = number.replace(thousands_separator, "")
            number = number.replace(decimal_separator, ".")
        else:
            number = number.replace(",", ".")

        multiplier = {
            "K": 1_000,
            "M": 1_000_000,
            "B": 1_000_000_000,
        }[suffix]
        result = int(round(float(number) * multiplier))
    else:
        digits = re.sub(r"\D", "", number)
        if not digits:
            raise ValueError(f"Geen cijfers gevonden in: {raw_value!r}")
        result = int(digits)

    if not 0 <= result <= 2_000_000_000:
        raise ValueError(f"Telwaarde buiten bereik: {raw_value!r}")

    return result


def normalize_username(raw_username: str) -> str:
    username = raw_username.strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", username):
        raise ValueError("De Instagram-gebruikersnaam is ongeldig.")
    return username


def validate_source_url(raw_url: str) -> str:
    source_url = raw_url.strip()
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "De bron-URL ontbreekt of is ongeldig. Stel de GitHub-variable "
            "STATS_SOURCE_URL in op de volledige openbare profielpagina."
        )
    return source_url


def _dismiss_common_dialogs(driver) -> None:
    """Sluit bekende cookieknoppen wanneer zo'n dialoog de pagina bedekt."""
    from selenium.webdriver.common.by import By

    labels = (
        "Accept",
        "Accept all",
        "Allow all cookies",
        "I agree",
        "Akkoord",
        "Accepteren",
        "Alles accepteren",
        "Alle cookies toestaan",
    )

    for label in labels:
        xpath = (
            "//button[normalize-space()=" + json.dumps(label) + "]"
            " | //a[normalize-space()=" + json.dumps(label) + "]"
        )
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if element.is_displayed() and element.is_enabled():
                    driver.execute_script("arguments[0].click();", element)
                    time.sleep(0.5)
                    return
            except Exception:
                continue


def _read_count_by_xpath(driver, primary_xpath: str, fallback_xpath: str) -> tuple[int, str]:
    from selenium.webdriver.common.by import By

    last_error: Exception | None = None
    for xpath in (primary_xpath, fallback_xpath):
        try:
            element = driver.find_element(By.XPATH, xpath)
            text = (element.text or element.get_attribute("textContent") or "").strip()
            return parse_count(text), xpath
        except Exception as error:
            last_error = error

    if last_error is not None:
        raise last_error
    raise RuntimeError("Het statistiekelement kon niet worden gevonden.")


def scrape_stats_page(
    source_url: str,
    timeout_seconds: int,
    debug_dir: Path,
) -> tuple[dict[str, int], dict[str, Any]]:
    """Lees de drie dynamische h4-elementen en wacht tot de waarden stabiel zijn."""
    from selenium import webdriver
    from selenium.webdriver.support.ui import WebDriverWait

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,1800")
    options.add_argument("--lang=nl-NL")
    options.add_argument(f"--user-agent={DEFAULT_USER_AGENT}")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(timeout_seconds)

    page_source = ""
    visible_text = ""
    selected_xpaths: dict[str, str] = {}
    diagnostic: dict[str, Any] = {
        "method": "third_party_xpath_selenium",
        "source_url": source_url,
    }

    try:
        driver.get(source_url)
        WebDriverWait(driver, timeout_seconds).until(
            lambda browser: browser.execute_script("return document.readyState")
            in {"interactive", "complete"}
        )
        _dismiss_common_dialogs(driver)

        deadline = time.monotonic() + timeout_seconds
        last_counts: dict[str, int] | None = None
        stable_since: float | None = None
        latest_raw_error = ""

        while time.monotonic() < deadline:
            try:
                counts: dict[str, int] = {}
                current_xpaths: dict[str, str] = {}

                for key in ("followers", "following", "posts"):
                    count, used_xpath = _read_count_by_xpath(
                        driver,
                        STAT_XPATHS[key],
                        STAT_RELATIVE_XPATHS[key],
                    )
                    counts[key] = count
                    current_xpaths[key] = used_xpath

                # De site kan de getallen animeren. Pas publiceren nadat dezelfde
                # drie waarden minimaal twee seconden onveranderd zijn gebleven.
                if counts == last_counts:
                    if stable_since is None:
                        stable_since = time.monotonic()
                    if time.monotonic() - stable_since >= 2.0:
                        selected_xpaths = current_xpaths
                        diagnostic.update(
                            {
                                "final_url": driver.current_url,
                                "title": driver.title,
                                "selected_xpaths": selected_xpaths,
                                "counts": counts,
                            }
                        )
                        return counts, diagnostic
                else:
                    last_counts = counts
                    stable_since = time.monotonic()

                latest_raw_error = ""
            except Exception as error:
                latest_raw_error = f"{type(error).__name__}: {error}"
                stable_since = None

            time.sleep(0.5)

        diagnostic.update(
            {
                "final_url": driver.current_url,
                "title": driver.title,
                "last_counts": last_counts,
                "last_read_error": latest_raw_error,
            }
        )
        raise RuntimeError(
            "De drie statistiekwaarden konden niet binnen de wachttijd stabiel "
            "worden uitgelezen. Bekijk het debug-artifact van deze run."
        )
    finally:
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            page_source = driver.page_source
        except Exception:
            page_source = ""
        try:
            visible_text = driver.find_element("tag name", "body").text
        except Exception:
            visible_text = ""

        if page_source:
            (debug_dir / "source-page.html").write_text(
                page_source,
                encoding="utf-8",
            )
        if visible_text:
            (debug_dir / "source-visible-text.txt").write_text(
                visible_text,
                encoding="utf-8",
            )
        try:
            driver.save_screenshot(str(debug_dir / "source-page.png"))
        except Exception:
            pass
        driver.quit()


def write_stats(
    output_path: Path,
    username: str,
    counts: dict[str, int],
) -> InstagramStats:
    stats = InstagramStats(
        username=username,
        posts=counts["posts"],
        followers=counts["followers"],
        following=counts["following"],
        updated_at=datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        source="third_party_xpath_selenium",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(asdict(stats), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    return stats


def write_diagnostics(debug_dir: Path, diagnostics: list[dict[str, Any]]) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Lees berichten, volgers en volgend via drie dynamische XPath-"
            "elementen en publiceer een klein JSON-bestand."
        )
    )
    parser.add_argument("--username", default="moodzcoffeebar")
    parser.add_argument("--source-url", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_site/instagram-stats.json"),
    )
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument(
        "--debug-dir",
        type=Path,
        default=Path("_debug"),
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    diagnostics: list[dict[str, Any]] = []

    try:
        username = normalize_username(arguments.username)
        source_url = validate_source_url(arguments.source_url)
        timeout_seconds = max(15, min(arguments.timeout, 90))

        counts, diagnostic = scrape_stats_page(
            source_url=source_url,
            timeout_seconds=timeout_seconds,
            debug_dir=arguments.debug_dir,
        )
        diagnostics.append(diagnostic)

        stats = write_stats(
            output_path=arguments.output,
            username=username,
            counts=counts,
        )
        write_diagnostics(arguments.debug_dir, diagnostics)

        print(
            "Statistieken bijgewerkt: "
            f"{stats.posts} berichten, "
            f"{stats.followers} volgers, "
            f"{stats.following} volgend "
            f"({stats.source})."
        )
        return 0
    except Exception as error:
        diagnostics.append(
            {
                "method": "final",
                "error": f"{type(error).__name__}: {error}",
            }
        )
        write_diagnostics(arguments.debug_dir, diagnostics)
        print(f"Statistieken niet bijgewerkt: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
