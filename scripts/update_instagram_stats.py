from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests


COUNT_TOKEN_PATTERN = r"[0-9][0-9\s.,]*[KMBkmb]?"
INSTAGRAM_WEB_APP_ID = "936619743392459"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class InstagramStats:
    username: str
    posts: int
    followers: int
    following: int
    updated_at: str
    source: str
    schema_version: int = 1


class MetaTagParser(HTMLParser):
    """Collect meta-tag attributes without an extra parser dependency."""

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

        self.meta_tags.append(
            {
                str(name).lower(): str(value or "")
                for name, value in attrs
            }
        )


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
        label_pattern = "|".join(re.escape(label) for label in labels)
        patterns = (
            rf"(?P<count>{COUNT_TOKEN_PATTERN})\s*(?:{label_pattern})\b",
            rf"(?:{label_pattern})\s*[:\-]?\s*(?P<count>{COUNT_TOKEN_PATTERN})\b",
        )

        for pattern in patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
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

        counts = _extract_labeled_counts(meta_tag.get("content", ""))
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
            (
                rf'"{re.escape(field_name)}"\s*:\s*\{{\s*'
                rf'"count"\s*:\s*(?P<count>[0-9]+)'
            ),
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
            "follows_count",
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
    extractors = (
        extract_from_meta_tags(page_source),
        extract_from_embedded_json(page_source),
        _extract_labeled_counts(visible_text),
    )

    combined: dict[str, int] = {}
    for extracted in extractors:
        for key, value in extracted.items():
            combined.setdefault(key, value)

        if all(key in combined for key in ("posts", "followers", "following")):
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


def _nested_count(user: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = user.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
        if isinstance(value, dict):
            count = value.get("count")
            if isinstance(count, (int, float)):
                return int(count)
            if isinstance(count, str) and count.isdigit():
                return int(count)
    return None


def extract_from_profile_json(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise RuntimeError("Instagram gaf geen JSON-object terug.")

    user: Any = payload
    for key in ("data", "user"):
        if isinstance(user, dict) and isinstance(user.get(key), dict):
            user = user[key]

    if not isinstance(user, dict):
        raise RuntimeError("Instagram-JSON bevat geen gebruikersobject.")

    counts = {
        "posts": _nested_count(
            user,
            "media_count",
            "edge_owner_to_timeline_media",
            "edge_felix_video_timeline",
        ),
        "followers": _nested_count(
            user,
            "follower_count",
            "followers_count",
            "edge_followed_by",
        ),
        "following": _nested_count(
            user,
            "following_count",
            "follows_count",
            "edge_follow",
        ),
    }

    missing = [key for key, value in counts.items() if value is None]
    if missing:
        raise RuntimeError(
            "Instagram-JSON mist de velden: " + ", ".join(missing) + "."
        )

    return {key: int(value) for key, value in counts.items() if value is not None}


def _common_headers(username: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.instagram.com/{username}/",
        "Origin": "https://www.instagram.com",
        "User-Agent": DEFAULT_USER_AGENT,
        "X-IG-App-ID": INSTAGRAM_WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
    }


def fetch_profile_json(
    username: str,
    timeout_seconds: int,
    session_id: str = "",
) -> tuple[dict[str, int], dict[str, Any]]:
    endpoint = (
        "https://i.instagram.com/api/v1/users/web_profile_info/"
        f"?username={quote(username)}"
    )
    session = requests.Session()
    session.headers.update(_common_headers(username))
    if session_id:
        session.cookies.set("sessionid", session_id, domain=".instagram.com")

    response = session.get(
        endpoint,
        timeout=timeout_seconds,
        allow_redirects=True,
    )
    diagnostic = {
        "method": "web_profile_info_http",
        "status_code": response.status_code,
        "url": response.url,
        "content_type": response.headers.get("content-type", ""),
        "body_preview": response.text[:1500],
    }
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("Instagram gaf via het profielendpoint geen JSON terug.") from error

    return extract_from_profile_json(payload), diagnostic


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
            "//button[normalize-space()=" + json.dumps(label) + "]",
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
    debug_dir: Path,
    session_id: str = "",
) -> tuple[dict[str, int], dict[str, Any]]:
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
    options.add_argument(f"--user-agent={DEFAULT_USER_AGENT}")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(timeout_seconds)

    latest_source = ""
    latest_text = ""
    diagnostic: dict[str, Any] = {"method": "selenium_profile_page"}

    try:
        if session_id:
            driver.get("https://www.instagram.com/")
            driver.add_cookie(
                {
                    "name": "sessionid",
                    "value": session_id,
                    "domain": ".instagram.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            )

        driver.get(profile_url)
        WebDriverWait(driver, timeout_seconds).until(
            lambda browser: browser.execute_script("return document.readyState")
            in {"interactive", "complete"}
        )
        _dismiss_cookie_dialog(driver)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            latest_source = driver.page_source
            try:
                latest_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                latest_text = ""

            try:
                counts = extract_instagram_counts(latest_source, latest_text)
                diagnostic.update(
                    {
                        "url": driver.current_url,
                        "title": driver.title,
                    }
                )
                return counts, diagnostic
            except RuntimeError:
                time.sleep(1.0)

        diagnostic.update(
            {
                "url": driver.current_url,
                "title": driver.title,
                "body_preview": latest_text[:1500],
            }
        )
        raise RuntimeError(
            "De Selenium-pagina bevatte geen volledige profielstatistieken."
        )
    finally:
        debug_dir.mkdir(parents=True, exist_ok=True)
        if latest_source:
            (debug_dir / "instagram-page.html").write_text(
                latest_source,
                encoding="utf-8",
            )
        if latest_text:
            (debug_dir / "instagram-visible-text.txt").write_text(
                latest_text,
                encoding="utf-8",
            )
        try:
            driver.save_screenshot(str(debug_dir / "instagram-page.png"))
        except Exception:
            pass
        driver.quit()


def write_stats(
    output_path: Path,
    username: str,
    counts: dict[str, int],
    source: str,
) -> InstagramStats:
    stats = InstagramStats(
        username=username,
        posts=counts["posts"],
        followers=counts["followers"],
        following=counts["following"],
        updated_at=datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        source=source,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(asdict(stats), ensure_ascii=False, indent=2) + "\n",
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
    parser.add_argument("--username", default="moodzcoffeebar")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_site/instagram-stats.json"),
    )
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument(
        "--debug-dir",
        type=Path,
        default=Path("_debug"),
    )
    parser.add_argument("--html-file", type=Path)
    parser.add_argument("--visible-text-file", type=Path)
    return parser.parse_args()


def normalize_username(raw_username: str) -> str:
    username = raw_username.strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", username):
        raise ValueError("De Instagram-gebruikersnaam is ongeldig.")
    return username


def _write_diagnostics(debug_dir: Path, diagnostics: list[dict[str, Any]]) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    arguments = parse_arguments()
    debug_dir = arguments.debug_dir
    diagnostics: list[dict[str, Any]] = []

    try:
        username = normalize_username(arguments.username)
        timeout_seconds = max(10, min(arguments.timeout, 60))
        session_id = os.getenv("INSTAGRAM_SESSIONID", "").strip()

        if arguments.html_file:
            page_source = arguments.html_file.read_text(encoding="utf-8")
            visible_text = ""
            if arguments.visible_text_file:
                visible_text = arguments.visible_text_file.read_text(
                    encoding="utf-8"
                )
            counts = extract_instagram_counts(page_source, visible_text)
            source = "local_test_file"
        else:
            counts: dict[str, int] | None = None
            source = ""

            try:
                counts, diagnostic = fetch_profile_json(
                    username=username,
                    timeout_seconds=timeout_seconds,
                    session_id=session_id,
                )
                diagnostics.append(diagnostic)
                source = "instagram_web_profile_info"
            except Exception as error:
                diagnostics.append(
                    {
                        "method": "web_profile_info_http",
                        "error": f"{type(error).__name__}: {error}",
                    }
                )

            if counts is None:
                try:
                    counts, diagnostic = scrape_instagram_profile(
                        username=username,
                        timeout_seconds=timeout_seconds,
                        debug_dir=debug_dir,
                        session_id=session_id,
                    )
                    diagnostics.append(diagnostic)
                    source = "instagram_public_profile_selenium"
                except Exception as error:
                    diagnostics.append(
                        {
                            "method": "selenium_profile_page",
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                    raise RuntimeError(
                        "Zowel het openbare Instagram-profielendpoint als "
                        "de Selenium-profielpagina leverden geen volledige "
                        "statistieken op. Bekijk het debug-artifact van deze run."
                    ) from error

        stats = write_stats(
            output_path=arguments.output,
            username=username,
            counts=counts,
            source=source,
        )
        _write_diagnostics(debug_dir, diagnostics)

        print(
            "Instagram-statistieken bijgewerkt: "
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
        _write_diagnostics(debug_dir, diagnostics)
        print(f"Instagram-statistieken niet bijgewerkt: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
