from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


COUNT_TOKEN_PATTERN = r"[0-9][0-9\s.,]*[KMBkmb]?"


@dataclass(frozen=True)
class InstagramStats:
    username: str
    posts: int
    followers: int
    following: int
    updated_at: str
    source: str = "instagram_public_profile"
    schema_version: int = 1


class MetaTagParser(HTMLParser):
    """Collect meta-tag attributes without adding a parser dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_tags: list[dict[str, str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "meta":
            return

        normalized = {
            str(name).lower(): str(value or "")
            for name, value in attrs
        }
        self.meta_tags.append(normalized)


def parse_compact_count(raw_value: str) -> int:
    """Convert values such as 1.234, 1,2K and 3M to integers."""
    value = html.unescape(raw_value).replace("\u00a0", " ").strip()
    value = re.sub(r"\s+", "", value)

    match = re.fullmatch(
        r"(?P<number>[0-9][0-9.,]*)(?P<suffix>[KMBkmb]?)",
        value,
    )

    if not match:
        raise ValueError(f"Ongeldige Instagram-telwaarde: {raw_value!r}")

    number = match.group("number")
    suffix = match.group("suffix").upper()

    if suffix:
        if "," in number and "." in number:
            decimal_separator = (
                "," if number.rfind(",") > number.rfind(".") else "."
            )
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
        result = int(digits)

    if not 0 <= result <= 2_000_000_000:
        raise ValueError(f"Instagram-telwaarde buiten bereik: {raw_value!r}")

    return result


def _extract_labeled_counts(text: str) -> dict[str, int]:
    normalized = html.unescape(text).replace("\u00a0", " ")

    aliases = {
        "posts": (
            "posts",
            "post",
            "berichten",
            "bericht",
            "publicaciones",
            "publicação",
            "publicações",
        ),
        "followers": (
            "followers",
            "follower",
            "volgers",
            "volger",
            "seguidores",
            "seguidores/as",
        ),
        "following": (
            "following",
            "volgend",
            "seguidos",
            "seguindo",
        ),
    }

    result: dict[str, int] = {}

    for key, labels in aliases.items():
        label_pattern = "|".join(
            re.escape(label)
            for label in labels
        )
        patterns = (
            rf"(?P<count>{COUNT_TOKEN_PATTERN})\s*(?:{label_pattern})\b",
            rf"(?:{label_pattern})\s*[:\-]?\s*(?P<count>{COUNT_TOKEN_PATTERN})\b",
        )

        for pattern in patterns:
            match = re.search(
                pattern,
                normalized,
                flags=re.IGNORECASE,
            )
            if not match:
                continue

            try:
                result[key] = parse_compact_count(match.group("count"))
                break
            except ValueError:
                continue

    return result


def extract_from_meta_tags(page_source: str) -> dict[str, int]:
    parser = MetaTagParser()
    parser.feed(page_source)

    preferred_properties = {
        "og:description",
        "twitter:description",
        "description",
    }

    for meta_tag in parser.meta_tags:
        property_name = (
            meta_tag.get("property")
            or meta_tag.get("name")
            or ""
        ).lower()

        if property_name not in preferred_properties:
            continue

        content = meta_tag.get("content", "")
        counts = _extract_labeled_counts(content)

        if all(key in counts for key in ("posts", "followers", "following")):
            return counts

    return {}


def _search_json_number(
    page_source: str,
    field_names: Iterable[str],
) -> int | None:
    normalized = html.unescape(page_source).replace('\\"', '"')

    for field_name in field_names:
        patterns = (
            rf'"{re.escape(field_name)}"\s*:\s*(?P<count>[0-9]+)',
            rf'"{re.escape(field_name)}"\s*:\s*\{{\s*"count"\s*:\s*(?P<count>[0-9]+)',
        )

        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match:
                return int(match.group("count"))

    return None


def extract_from_embedded_json(page_source: str) -> dict[str, int]:
    field_names = {
        "posts": (
            "media_count",
            "edge_owner_to_timeline_media",
            "edge_felix_video_timeline",
        ),
        "followers": (
            "follower_count",
            "edge_followed_by",
        ),
        "following": (
            "following_count",
            "edge_follow",
        ),
    }

    counts: dict[str, int] = {}

    for key, candidates in field_names.items():
        value = _search_json_number(page_source, candidates)
        if value is not None:
            counts[key] = value

    return counts


def extract_instagram_counts(
    page_source: str,
    visible_text: str = "",
) -> dict[str, int]:
    """Try stable metadata first, then embedded JSON and visible text."""
    extractors = (
        extract_from_meta_tags(page_source),
        extract_from_embedded_json(page_source),
        _extract_labeled_counts(visible_text),
    )

    combined: dict[str, int] = {}

    for extracted in extractors:
        for key, value in extracted.items():
            combined.setdefault(key, value)

        if all(
            key in combined
            for key in ("posts", "followers", "following")
        ):
            return combined

    missing = [
        key
        for key in ("posts", "followers", "following")
        if key not in combined
    ]

    raise RuntimeError(
        "Instagram-statistieken konden niet volledig worden gelezen. "
        f"Ontbrekend: {', '.join(missing)}."
    )


def _dismiss_cookie_dialog(driver) -> None:
    from selenium.webdriver.common.by import By

    button_labels = (
        "Allow all cookies",
        "Accept all cookies",
        "Decline optional cookies",
        "Only allow essential cookies",
        "Alle cookies toestaan",
        "Optionele cookies weigeren",
        "Alleen essentiële cookies toestaan",
    )

    for label in button_labels:
        buttons = driver.find_elements(
            By.XPATH,
            (
                "//button[normalize-space()="
                f"{json.dumps(label)}]"
            ),
        )

        for button in buttons:
            try:
                if button.is_displayed() and button.is_enabled():
                    button.click()
                    time.sleep(0.8)
                    return
            except Exception:
                continue


def scrape_instagram_profile(
    username: str,
    timeout_seconds: int,
) -> tuple[str, str]:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    profile_url = f"https://www.instagram.com/{username}/"

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,1600")
    options.add_argument("--lang=en-US")
    options.add_argument(
        "--user-agent="
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(timeout_seconds)

    try:
        driver.get(profile_url)

        WebDriverWait(driver, timeout_seconds).until(
            lambda browser: browser.execute_script(
                "return document.readyState"
            ) in {"interactive", "complete"}
        )

        _dismiss_cookie_dialog(driver)

        deadline = time.monotonic() + timeout_seconds
        latest_source = driver.page_source
        latest_text = ""

        while time.monotonic() < deadline:
            latest_source = driver.page_source

            try:
                latest_text = driver.find_element(
                    By.TAG_NAME,
                    "body",
                ).text
            except Exception:
                latest_text = ""

            try:
                extract_instagram_counts(
                    latest_source,
                    latest_text,
                )
                break
            except RuntimeError:
                time.sleep(1.0)

        return latest_source, latest_text
    finally:
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
        updated_at=datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(
        f"{output_path.suffix}.tmp"
    )
    temporary_path.write_text(
        json.dumps(
            asdict(stats),
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(output_path)

    return stats


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Lees openbare Instagram-profielstatistieken en schrijf een "
            "klein JSON-bestand voor GitHub Pages."
        )
    )
    parser.add_argument(
        "--username",
        default="moodzcoffeebar",
        help="Instagram-gebruikersnaam zonder @.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_site/instagram-stats.json"),
        help="Bestemming van het JSON-bestand.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=25,
        help="Maximale Selenium-wachttijd in seconden.",
    )
    parser.add_argument(
        "--html-file",
        type=Path,
        help="Lees lokaal HTML in plaats van Selenium; nuttig voor tests.",
    )
    parser.add_argument(
        "--visible-text-file",
        type=Path,
        help="Optioneel lokaal bestand met zichtbare paginatekst.",
    )
    return parser.parse_args()


def normalize_username(raw_username: str) -> str:
    username = raw_username.strip().lstrip("@").lower()

    if not re.fullmatch(r"[a-z0-9._]{1,30}", username):
        raise ValueError("De Instagram-gebruikersnaam is ongeldig.")

    return username


def main() -> int:
    arguments = parse_arguments()

    try:
        username = normalize_username(arguments.username)

        if arguments.html_file:
            page_source = arguments.html_file.read_text(
                encoding="utf-8"
            )
            visible_text = ""

            if arguments.visible_text_file:
                visible_text = arguments.visible_text_file.read_text(
                    encoding="utf-8"
                )
        else:
            page_source, visible_text = scrape_instagram_profile(
                username=username,
                timeout_seconds=max(10, min(arguments.timeout, 60)),
            )

        counts = extract_instagram_counts(
            page_source,
            visible_text,
        )
        stats = write_stats(
            output_path=arguments.output,
            username=username,
            counts=counts,
        )

        print(
            "Instagram-statistieken bijgewerkt: "
            f"{stats.posts} berichten, "
            f"{stats.followers} volgers, "
            f"{stats.following} volgend."
        )
        return 0
    except Exception as error:
        print(
            f"Instagram-statistieken niet bijgewerkt: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
