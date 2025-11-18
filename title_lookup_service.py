#!/usr/bin/env python3
"""Interactive CSFD lookup helper for the Jellyfin content renamer."""

from __future__ import annotations

import argparse
import curses
import functools
import gzip
import json
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
CSFD_SEARCH_URL = os.environ.get(
    "CSFD_SEARCH_URL",
    "https://www.csfd.cz/hledat/?q={query}",
)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,sk;q=0.8,en;q=0.6",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.csfd.cz/",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Cookie": "csfd_session=start;accept-xframes=deny",
}
DEFAULT_MAX_RESULTS = 10
NOISE_TOKENS = {
    "hd", "uhd", "uhdtv", "hdr", "hdrip", "bdrip", "brrip", "webrip", "webdl",
    "dvdrip", "remastered", "fullhd", "bluray", "br", "hevc", "x264", "x265",
    "h264", "h265", "ac3", "dts", "aac", "dd5", "dd51", "multi", "cz", "sk",
    "en", "pl", "dab", "dabing", "dub", "titulky", "tit", "subs", "subtitles",
}
RESOLUTION_PATTERN = re.compile(r"(?i)\b(480|576|720|1080|1440|2160)p\b")
YEAR_PATTERN = re.compile(r"(18[8-9][0-9]|19[0-9]{2}|20[0-4][0-9])")
SEPARATORS = re.compile(r"[._-]+")
NON_ALNUM = re.compile(r"[^0-9a-zA-ZáéěíóúůýščřžÁÉĚÍÓÚŮÝŠČŘŽ ]+")
WHITESPACE = re.compile(r"\s+")
INVALID_FILENAME_CHARS = re.compile(r'[\\/<>:"|?*]')
RUNTIME_PATTERN = re.compile(r"(\d{1,3})\s*(?:min|min\.|minut|minuty|minutes)", re.IGNORECASE)
VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".ts",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".flv",
    ".iso",
}

FFPROBE_WARNING_SHOWN = False


class UserAbort(Exception):
    """Raised when the user aborts the interactive workflow."""


class TUIError(Exception):
    """Raised when the interactive TUI cannot be displayed."""



def build_headers() -> dict:
    headers = dict(BASE_HEADERS)
    ua_override = os.environ.get("CSFD_USER_AGENT")
    headers["User-Agent"] = ua_override or random.choice(USER_AGENTS)
    return headers


class MovieSearchParser(HTMLParser):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit
        self.results: List[dict] = []
        self._current: Optional[dict] = None
        self._capture_title = False
        self._capture_year = False
        self._year_target: Optional[dict] = None

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: D401
        attr_map = dict(attrs)
        class_names = attr_map.get("class", "")
        class_set = {name for name in class_names.split() if name}

        if tag == "a" and "film-title-name" in class_set:
            if len(self.results) >= self.limit:
                self._current = None
                self._capture_title = False
                return
            href = attr_map.get("href", "")
            self._current = {
                "title": "",
                "year": None,
                "url": urllib.parse.urljoin("https://www.csfd.cz", href),
            }
            self._capture_title = True
            return

        if tag == "span" and "info" in class_set:
            if self.results:
                self._year_target = self.results[-1]
            if self._current is not None:
                self._year_target = self._current
            self._capture_year = self._year_target is not None

    def handle_endtag(self, tag: str) -> None:  # noqa: D401
        if tag == "a" and self._capture_title:
            self._capture_title = False
            if self._current and self._current.get("title"):
                self._current["title"] = self._current["title"].strip()
                if self._current["title"] and len(self.results) < self.limit:
                    self.results.append(self._current)
            self._current = None
            self._year_target = None
        elif tag == "span" and self._capture_year:
            self._capture_year = False
            self._year_target = None

    def handle_data(self, data: str) -> None:  # noqa: D401
        if self._capture_title and self._current is not None:
            self._current["title"] += data
            return
        if self._capture_year and self._year_target is not None and not self._year_target.get("year"):
            match = YEAR_PATTERN.search(data)
            if match:
                self._year_target["year"] = int(match.group(0))
                self._capture_year = False
                self._year_target = None


def strip_extensions(filename: str) -> str:
    stem = filename
    while "." in stem:
        base, ext = stem.rsplit(".", 1)
        if ext.lower() in {"mkv", "mp4", "avi", "mov", "ts", "wmv", "mpg", "mpeg", "flv", "iso"}:
            stem = base
        else:
            break
    return stem


def normalize_delimiters(text: str) -> str:
    return SEPARATORS.sub(" ", text)


def remove_bracketed_years(text: str) -> str:
    return re.sub(r"\([^)]*\)", lambda m: "" if YEAR_PATTERN.search(m.group(0) or "") else m.group(0), text)


def remove_noise_tokens(tokens: Iterable[str]) -> List[str]:
    cleaned: List[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered in NOISE_TOKENS or RESOLUTION_PATTERN.fullmatch(token):
            continue
        if YEAR_PATTERN.fullmatch(token):
            continue
        cleaned.append(token)
    return cleaned


def derive_search_query(filename: str, hint: Optional[str] = None) -> str:
    base = hint or filename
    base = strip_extensions(base)
    base = remove_bracketed_years(base)
    base = normalize_delimiters(base)
    base = NON_ALNUM.sub(" ", base)
    tokens = [token for token in base.split() if token]
    tokens = remove_noise_tokens(tokens)
    cleaned = WHITESPACE.sub(" ", " ".join(tokens)).strip()
    return cleaned


def fetch_csfd_results(query: str, limit: int) -> List[dict]:
    if not query:
        return []
    url = CSFD_SEARCH_URL.format(query=urllib.parse.quote(query))
    request = urllib.request.Request(url, headers=build_headers())
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "").lower()
            if "gzip" in encoding:
                payload = gzip.decompress(raw).decode("utf-8", errors="ignore")
            elif "deflate" in encoding:
                payload = zlib.decompress(raw).decode("utf-8", errors="ignore")
            else:
                payload = raw.decode("utf-8", errors="ignore")
    except urllib.error.URLError as exc:  # pragma: no cover
        print(f"CSFD lookup failed: {exc}", file=sys.stderr)
        return []
    parser = MovieSearchParser(limit)
    parser.feed(payload)
    return parser.results


def format_result(idx: int, result: dict) -> str:
    title = result["title"]
    year = result.get("year")
    url = result.get("url", "")
    suffix = f" ({year})" if year else ""
    duration = result.get("duration_minutes")
    duration_text = f" [{duration} min]" if duration else ""
    return f"  {idx}. {title}{suffix}{duration_text}\n      {url}"


def prompt(prompt_text: str) -> str:
    try:
        return input(prompt_text)
    except EOFError:
        return ""


def supports_curses() -> bool:
    term = os.environ.get("TERM", "")
    return sys.stdin.isatty() and sys.stdout.isatty() and term and term.lower() != "dumb"


def parse_runtime(html: str) -> Optional[int]:
    match = RUNTIME_PATTERN.search(html)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


@functools.lru_cache(maxsize=256)
def fetch_csfd_detail(url: str) -> Dict[str, Optional[int]]:
    if not url:
        return {}
    absolute_url = urllib.parse.urljoin("https://www.csfd.cz", url)
    request = urllib.request.Request(absolute_url, headers=build_headers())
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "").lower()
            try:
                if "gzip" in encoding:
                    payload = gzip.decompress(raw).decode("utf-8", errors="ignore")
                elif "deflate" in encoding:
                    payload = zlib.decompress(raw).decode("utf-8", errors="ignore")
                else:
                    payload = raw.decode("utf-8", errors="ignore")
            except (OSError, zlib.error, UnicodeDecodeError):
                return {}
    except urllib.error.URLError:
        return {}
    duration = parse_runtime(payload)
    return {"duration_minutes": duration}


def enrich_csfd_results(results: Sequence[dict]) -> List[dict]:
    enriched: List[dict] = []
    for item in results:
        enriched_item = dict(item)
        detail = fetch_csfd_detail(enriched_item.get("url", ""))
        if detail:
            enriched_item["duration_minutes"] = detail.get("duration_minutes")
        else:
            enriched_item.setdefault("duration_minutes", None)
        enriched.append(enriched_item)
    return enriched


def get_media_duration(file_path: str) -> Optional[int]:
    global FFPROBE_WARNING_SHOWN
    if not os.path.exists(file_path):
        return None
    ffprobe = os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe")
    if not ffprobe:
        if not FFPROBE_WARNING_SHOWN:
            print(
                "Warning: ffprobe not found; install FFmpeg to enable media length detection.",
                file=sys.stderr,
            )
            FFPROBE_WARNING_SHOWN = True
        return None
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=duration",
        "-of",
        "json",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    def _parse_duration(value: object) -> Optional[float]:
        if value in (None, "", "N/A"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    durations: List[float] = []
    fmt = data.get("format") if isinstance(data, dict) else None
    if isinstance(fmt, dict):
        parsed = _parse_duration(fmt.get("duration"))
        if parsed and parsed > 0:
            durations.append(parsed)
    streams = data.get("streams") if isinstance(data, dict) else None
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict):
                parsed = _parse_duration(stream.get("duration"))
                if parsed and parsed > 0:
                    durations.append(parsed)
    if not durations:
        return None
    seconds = max(durations)
    if seconds <= 0:
        return None
    minutes = int(round(seconds / 60))
    return minutes if minutes > 0 else None


def select_result_simple(
    results: Sequence[dict],
    query: str,
    auto_choice: Optional[int],
    suggest_skip: bool = False,
) -> Tuple[str, Optional[dict]]:
    if not results:
        return ("skip", None)
    if auto_choice is not None:
        if 1 <= auto_choice <= len(results):
            return ("accept", results[auto_choice - 1])
        return ("skip", None)
    print(f"\nFound {len(results)} result(s) for '{query}':")
    for idx, result in enumerate(results, start=1):
        print(format_result(idx, result))
    if suggest_skip:
        print("\nSuggestion: current name already matches the first CSFD hit. Press Enter to skip.")
    print(
        "\nChoose an option: [1-{}], Enter to {}#1, 'r' refine, 's' skip, 'q' abort.".format(
            len(results),
            "skip | accept " if suggest_skip else "accept ",
        )
    )
    while True:
        choice = prompt("Selection> ").strip().lower()
        if choice in {"q", "quit"}:
            return ("abort", None)
        if choice in {"s", "skip"}:
            return ("skip", None)
        if choice == "":
            if suggest_skip:
                return ("skip", None)
            return ("accept", results[0])
        if choice.startswith("r"):
            return ("refine", None)
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(results):
                return ("accept", results[idx - 1])
        print("Invalid choice, please try again.")


class SearchTUI:
    def __init__(self, query: str, results: Sequence[dict], context: Optional[dict] = None):
        self.query = query
        self.results = list(results)
        self.context = context or {}
        self.suggest_skip = bool(self.context.get("suggest_skip"))
        self.selected = -1 if self.suggest_skip else 0
        self.top = 0
        self.outcome: Tuple[str, Optional[dict]] = ("skip", None)
        self.colors: Dict[str, int] = {}

    def run(self) -> Tuple[str, Optional[dict]]:
        try:
            curses.wrapper(self._main)
        except Exception as exc:  # noqa: BLE001 - convert to TUIError for graceful fallback
            raise TUIError(str(exc)) from exc
        return self.outcome

    def _main(self, stdscr: "curses._CursesWindow") -> None:  # type: ignore[name-defined]
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
        self._init_colors()
        while True:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            if height < 12 or width < 60:
                msg = "Terminal too small. Resize or press q to abort."
                self._addstr(stdscr, max(0, height // 2), max(0, (width - len(msg)) // 2), msg, curses.A_BOLD)
                key = stdscr.getch()
                if key in (ord("q"), ord("Q"), 27):
                    self.outcome = ("abort", None)
                    break
                continue
            self._draw(stdscr, height, width)
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                self._move_selection(-1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self._move_selection(1)
            elif key in (curses.KEY_PPAGE, ord("b")):
                self._move_selection(-5)
            elif key in (curses.KEY_NPAGE, ord("f")):
                self._move_selection(5)
            elif key == curses.KEY_HOME:
                self.selected = -1 if self.results else -1
                self.top = 0
            elif key == curses.KEY_END:
                self.selected = len(self.results) - 1 if self.results else -1
                if self.selected >= 0:
                    visible = max(1, self._visible_items())
                    self.top = max(0, self.selected - visible + 1)
                else:
                    self.top = 0
            elif key in (10, 13, curses.KEY_ENTER):
                if self.selected == -1:
                    self.outcome = ("skip", None)
                else:
                    self.outcome = ("accept", self.results[self.selected])
                break
            elif key in (ord("r"), ord("R")):
                self.outcome = ("refine", None)
                break
            elif key in (ord("s"), ord("S")):
                self.outcome = ("skip", None)
                break
            elif key in (ord("q"), ord("Q"), 27):
                self.outcome = ("abort", None)
                break

    def _init_colors(self) -> None:
        if not curses.has_colors():
            self.colors = {
                "selected": curses.A_REVERSE | curses.A_BOLD,
                "progress": curses.A_BOLD,
                "progress_bar": curses.A_BOLD,
                "info": curses.A_DIM,
                "year": curses.A_BOLD,
                "url": curses.A_DIM,
                "length": curses.A_BOLD,
                "delta_low": curses.A_BOLD,
                "delta_medium": curses.A_BOLD,
                "delta_high": curses.A_BOLD,
                "skip": curses.A_BOLD,
            }
            return
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_WHITE, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
        curses.init_pair(6, curses.COLOR_CYAN, -1)
        curses.init_pair(7, curses.COLOR_GREEN, -1)
        curses.init_pair(8, curses.COLOR_YELLOW, -1)
        curses.init_pair(9, curses.COLOR_RED, -1)
        curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        self.colors = {
            "selected": curses.color_pair(1) | curses.A_BOLD,
            "progress": curses.color_pair(2) | curses.A_BOLD,
            "progress_bar": curses.color_pair(2),
            "info": curses.color_pair(3),
            "year": curses.color_pair(4) | curses.A_BOLD,
            "url": curses.color_pair(5) | curses.A_DIM,
            "length": curses.color_pair(6) | curses.A_BOLD,
            "delta_low": curses.color_pair(7) | curses.A_BOLD,
            "delta_medium": curses.color_pair(8) | curses.A_BOLD,
            "delta_high": curses.color_pair(9) | curses.A_BOLD,
            "skip": curses.color_pair(10) | curses.A_BOLD,
        }

    def _move_selection(self, delta: int) -> None:
        if not self.results:
            self.selected = -1
            self.top = 0
            return
        new_selected = self.selected + delta
        new_selected = max(-1, min(new_selected, len(self.results) - 1))
        if new_selected == self.selected:
            return
        self.selected = new_selected
        if self.selected < 0:
            self.top = 0
            return
        if self.selected < self.top:
            self.top = self.selected
        visible = max(1, self._visible_items())
        if self.selected >= self.top + visible:
            self.top = self.selected - visible + 1

    def _visible_items(self) -> int:
        height = self.context.get("_cached_height")
        if height is None:
            return 5
        start_row = self._list_start_row(height)
        available = height - start_row - 2
        return max(1, available // 2)

    def _list_start_row(self, height: int) -> int:
        return 6

    def _draw(self, stdscr: "curses._CursesWindow", height: int, width: int) -> None:  # type: ignore[name-defined]
        self.context["_cached_height"] = height
        progress_line = self._build_progress_line()
        self._addstr(stdscr, 0, 0, progress_line.ljust(width), self.colors.get("progress", curses.A_BOLD))
        self._draw_progress_bar(stdscr, 1, width)

        file_line, query_line, duration_line = self._build_header_lines(width)
        self._write_highlighted(
            stdscr,
            2,
            0,
            file_line[0],
            file_line[1],
            self.colors.get("info", curses.A_NORMAL) | curses.A_BOLD,
            self.colors.get("year", curses.A_BOLD),
            width,
        )
        self._write_highlighted(
            stdscr,
            3,
            0,
            query_line[0],
            query_line[1],
            self.colors.get("info", curses.A_NORMAL),
            self.colors.get("year", curses.A_BOLD),
            width,
        )
        self._addstr(
            stdscr,
            4,
            0,
            duration_line[:width],
            self.colors.get("info", curses.A_DIM),
        )

        skip_label = "Skip (s)"
        if self.suggest_skip:
            skip_label = "Skip (s) – suggested (already matches)"
        skip_attr = self.colors.get("info", curses.A_DIM)
        if self.selected == -1:
            skip_attr = self.colors.get("skip", curses.A_BOLD)
        self._addstr(stdscr, 5, 0, skip_label[:width], skip_attr)

        start_row = self._list_start_row(height)
        visible = max(1, (height - start_row - 3) // 2)
        end_index = min(len(self.results), self.top + visible)
        row = start_row
        for idx in range(self.top, end_index):
            result = self.results[idx]
            title_line, highlight = self._build_result_title(idx, result)
            segments = self._build_result_detail(result)
            base_attr = self.colors.get("info", curses.A_NORMAL)
            highlight_attr = self.colors.get("year", curses.A_BOLD)
            secondary_attr = self.colors.get("url", curses.A_DIM)
            if idx == self.selected:
                base_attr = self.colors.get("selected", curses.A_REVERSE | curses.A_BOLD)
                highlight_attr = base_attr | curses.A_BOLD
                secondary_attr = base_attr | curses.A_DIM
            self._write_highlighted(stdscr, row, 0, title_line, highlight, base_attr, highlight_attr, width)
            self._write_segments(stdscr, row + 1, 4, segments, width - 4, secondary_attr)
            row += 2

        instructions = "↑/↓ navigate • Enter accept • r refine • s skip • q abort"
        self._addstr(stdscr, height - 1, 0, instructions[:width], curses.A_DIM)

    def _draw_progress_bar(self, window: "curses._CursesWindow", row: int, width: int) -> None:  # type: ignore[name-defined]
        if width <= 0:
            return
        progress = self.context.get("progress") or {}
        total = progress.get("total") or 0
        counts = progress.get("counts") or {}
        completed = sum(counts.values()) if isinstance(counts, dict) else 0
        ratio = 0.0
        if total:
            ratio = max(0.0, min(completed / total, 1.0))
        bar_width = max(10, width - 12)
        filled = int(round(ratio * bar_width))
        filled = min(filled, bar_width)
        bar = "[" + "#" * filled + "-" * (bar_width - filled) + "]"
        percent = f" {int(round(ratio * 100)):3d}%"
        line = (bar + percent)[:width]
        attr = self.colors.get("progress_bar", self.colors.get("progress", curses.A_BOLD))
        self._addstr(window, row, 0, line, attr)

    def _build_progress_line(self) -> str:
        progress = self.context.get("progress") or {}
        total = progress.get("total")
        current = progress.get("current_index")
        counts = progress.get("counts") or {}
        processed = sum(counts.values())
        parts: List[str] = []
        if total:
            current_part = f"Processing {current or processed + 1}/{total}"
            parts.append(current_part)
            parts.append(f"Done {processed}")
            remaining = max(0, total - processed)
            parts.append(f"Remaining {remaining}")
        else:
            parts.append("Processing library")
        breakdown = ", ".join(f"{name}:{counts.get(name, 0)}" for name in ("renamed", "unchanged", "skipped") if counts.get(name))
        if breakdown:
            parts.append(breakdown)
        return " | ".join(parts)

    def _build_header_lines(self, width: int) -> Tuple[Tuple[str, Optional[str]], Tuple[str, Optional[str]], str]:
        file_name = self.context.get("file_name")
        file_path = self.context.get("file_path")
        if not file_name and file_path:
            file_name = os.path.basename(file_path)
        if not file_name:
            file_name = self.context.get("display_name") or "Current item"
        year_hint = self.context.get("year_hint")
        file_line = (f"File: {file_name}", str(year_hint) if year_hint else None)
        query = self.query or self.context.get("derived_query") or ""
        query_line = (f"Query: {query}", str(year_hint) if year_hint else None)
        file_duration = self.context.get("file_duration")
        duration_parts: List[str] = []
        if file_duration:
            duration_parts.append(f"File length {file_duration} min")
        else:
            duration_parts.append("File length unknown")
        duration_line = " | ".join(duration_parts)
        return file_line, query_line, duration_line

    def _build_result_title(self, idx: int, result: dict) -> Tuple[str, Optional[str]]:
        year = result.get("year")
        title = result.get("title", "(no title)")
        label = f"{idx + 1:>2}. {title}"
        if year:
            label += f" ({year})"
        return label, str(year) if year else None

    def _delta_attr(self, delta: int) -> int:
        magnitude = abs(delta)
        if magnitude <= 5:
            return self.colors.get("delta_low", curses.A_BOLD)
        if magnitude <= 15:
            return self.colors.get("delta_medium", curses.A_BOLD)
        return self.colors.get("delta_high", curses.A_BOLD)

    def _build_result_detail(self, result: dict) -> List[Tuple[str, Optional[int]]]:
        segments: List[Tuple[str, Optional[int]]] = []

        def append_segment(text: str, attr: Optional[int]) -> None:
            nonlocal segments
            if not text:
                return
            if segments:
                segments.append((" | ", None))
            segments.append((text, attr))

        csfd_duration = result.get("duration_minutes")
        if csfd_duration:
            append_segment(f"CSFD {csfd_duration} min", self.colors.get("length"))
        else:
            append_segment("CSFD ?", self.colors.get("length"))
        file_duration = self.context.get("file_duration")
        if file_duration:
            append_segment(f"File {file_duration} min", None)
        delta = None
        if csfd_duration and file_duration:
            delta = file_duration - csfd_duration
        if delta is not None:
            sign = "+" if delta >= 0 else ""
            append_segment(f"Δ {sign}{delta} min", self._delta_attr(delta))
        url = result.get("url")
        if url:
            append_segment(url, self.colors.get("url"))
        return segments

    def _addstr(self, window: "curses._CursesWindow", y: int, x: int, text: str, attr: int) -> None:  # type: ignore[name-defined]
        try:
            window.addnstr(y, x, text, max(0, window.getmaxyx()[1] - x), attr)
        except curses.error:
            pass

    def _write_highlighted(
        self,
        window: "curses._CursesWindow",  # type: ignore[name-defined]
        y: int,
        x: int,
        text: str,
        highlight: Optional[str],
        base_attr: int,
        highlight_attr: int,
        width: int,
    ) -> None:
        if width <= 0:
            return
        segment = text[: max(0, width - x)]
        if not highlight or highlight not in segment:
            self._addstr(window, y, x, segment, base_attr)
            return
        idx = segment.find(highlight)
        before = segment[:idx]
        match = segment[idx : idx + len(highlight)]
        after = segment[idx + len(highlight) :]
        self._addstr(window, y, x, before, base_attr)
        self._addstr(window, y, x + len(before), match, highlight_attr)
        self._addstr(window, y, x + len(before) + len(match), after, base_attr)

    def _write_segments(
        self,
        window: "curses._CursesWindow",  # type: ignore[name-defined]
        y: int,
        x: int,
        segments: List[Tuple[str, Optional[int]]],
        width: int,
        default_attr: int,
    ) -> None:
        if width <= 0:
            return
        cursor = x
        max_x = x + max(0, width)
        for text, attr in segments:
            if not text:
                continue
            slice_len = max(0, min(len(text), max_x - cursor))
            if slice_len <= 0:
                break
            segment_text = text[:slice_len]
            self._addstr(window, y, cursor, segment_text, attr or default_attr)
            cursor += slice_len


def select_result_tui(
    results: Sequence[dict],
    query: str,
    context: Optional[dict] = None,
) -> Tuple[str, Optional[dict]]:
    if not results:
        return ("skip", None)
    tui = SearchTUI(query, results, context)
    return tui.run()


def interactive_select_title(
    query: str,
    max_results: int,
    auto_choice: Optional[int],
    year_hint: Optional[int],
    context: Optional[dict] = None,
) -> Optional[dict]:
    ctx = dict(context or {})
    if year_hint and "year_hint" not in ctx:
        ctx["year_hint"] = year_hint
    ctx.setdefault("derived_query", query)
    current_query = query
    pending_auto_choice = auto_choice
    while True:
        results = fetch_csfd_results(current_query, max_results)
        if not results:
            print(f"No CSFD matches for '{current_query}'.", file=sys.stderr)
            current_query = prompt("Enter new search term (blank to cancel): ").strip()
            if not current_query:
                return None
            pending_auto_choice = None
            continue
        enriched_results = enrich_csfd_results(results)
        suggest_skip = bool(ctx.get("suggest_skip"))
        if not suggest_skip and enriched_results:
            first = enriched_results[0]
            expected = format_media_name(first.get("title", ""), first.get("year"))
            if expected:
                file_path = ctx.get("file_path")
                current_bases: List[str] = []
                if isinstance(file_path, str) and file_path:
                    file_base = sanitize_component(os.path.splitext(os.path.basename(file_path))[0])
                    current_bases.append(file_base)
                    parent = os.path.basename(os.path.dirname(file_path))
                    current_bases.append(sanitize_component(parent))
                display_name = ctx.get("display_name")
                if isinstance(display_name, str) and display_name:
                    current_bases.append(sanitize_component(display_name))
                suggest_skip = any(base == expected for base in current_bases if base)
        ctx["suggest_skip"] = suggest_skip
        ctx["current_query"] = current_query
        if pending_auto_choice is not None:
            action, selection = select_result_simple(
                enriched_results,
                current_query,
                pending_auto_choice,
                suggest_skip=suggest_skip,
            )
        else:
            if supports_curses():
                try:
                    action, selection = select_result_tui(enriched_results, current_query, ctx)
                except TUIError:
                    action, selection = select_result_simple(
                        enriched_results,
                        current_query,
                        None,
                        suggest_skip=suggest_skip,
                    )
            else:
                action, selection = select_result_simple(
                    enriched_results,
                    current_query,
                    None,
                    suggest_skip=suggest_skip,
                )
        pending_auto_choice = None
        if action == "abort":
            raise UserAbort()
        if action == "skip":
            return None
        if action == "refine":
            current_query = prompt("Enter new search term (blank to cancel): ").strip()
            if not current_query:
                return None
            continue
        if action == "accept" and selection is not None:
            chosen = dict(selection)
            year = chosen.get("year") or year_hint
            if not year:
                year_input = prompt("Enter release year (or leave blank): ").strip()
                if year_input.isdigit():
                    year = int(year_input)
            chosen["year"] = year
            return chosen



def sanitize_component(text: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub(" ", text)
    cleaned = cleaned.replace("\t", " ")
    cleaned = WHITESPACE.sub(" ", cleaned)
    return cleaned.strip(" .")


def format_media_name(title: str, year: Optional[int]) -> str:
    base_title = WHITESPACE.sub(" ", title).strip()
    formatted = f"{base_title} ({year})" if year else base_title
    return sanitize_component(formatted)


def is_video_file(path: str) -> bool:
    _, ext = os.path.splitext(path)
    return ext.lower() in VIDEO_EXTENSIONS


def find_year_hint(*candidates: str) -> Optional[int]:
    for candidate in candidates:
        if not candidate:
            continue
        match = YEAR_PATTERN.search(candidate)
        if match:
            return int(match.group(0))
    return None


def guess_search_query(file_path: str) -> str:
    filename = os.path.basename(file_path)
    directory = os.path.basename(os.path.dirname(file_path))
    query = derive_search_query(filename)
    if query:
        return query
    return derive_search_query(directory)


def rename_media_paths(
    file_path: str,
    base_name: str,
    root_path: str,
) -> Tuple[str, Optional[Tuple[str, str]], bool]:
    current_path = file_path
    original_dir = os.path.dirname(file_path)
    dir_path = original_dir
    filename = os.path.basename(file_path)
    _, ext = os.path.splitext(filename)
    root_abs = os.path.abspath(root_path)
    dir_abs = os.path.abspath(dir_path)
    dir_change: Optional[Tuple[str, str]] = None
    if dir_abs == root_abs:
        target_dir_parent = dir_path
    else:
        target_dir_parent = os.path.dirname(dir_path)
    target_dir = os.path.join(target_dir_parent, base_name)
    target_dir_abs = os.path.abspath(target_dir)
    changed = False
    renamed_directory = False
    if dir_abs != target_dir_abs:
        if dir_abs != root_abs and not os.path.exists(target_dir):
            os.rename(dir_path, target_dir)
            print(f"  Directory renamed:\n    {dir_path}\n    -> {target_dir}")
            dir_change = (dir_path, target_dir)
            dir_path = target_dir
            dir_abs = target_dir_abs
            renamed_directory = True
            current_path = os.path.join(dir_path, os.path.basename(current_path))
        else:
            if not os.path.exists(target_dir):
                os.makedirs(target_dir, exist_ok=True)
            dir_path = target_dir
            dir_abs = target_dir_abs
    if dir_abs == target_dir_abs:
        dir_path = target_dir
    target_filename = f"{base_name}{ext}"
    target_filename = sanitize_component(target_filename)
    target_path = os.path.join(dir_path, target_filename)
    if target_path != current_path:
        if os.path.exists(target_path):
            print(f"  Skipping file rename, target exists: {target_path}", file=sys.stderr)
        else:
            os.makedirs(dir_path, exist_ok=True)
            os.rename(current_path, target_path)
            print(f"  File renamed:\n    {current_path}\n    -> {target_path}")
            current_path = target_path
            changed = True
    original_dir_abs = os.path.abspath(original_dir)
    if (
        not renamed_directory
        and original_dir_abs != os.path.abspath(dir_path)
        and original_dir_abs != root_abs
    ):
        # Cleanup empty original directory if we moved the file into a new folder.
        try:
            os.rmdir(original_dir)
        except OSError:
            pass
    changed = changed or renamed_directory
    return current_path, dir_change, changed


def remap_path(path: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return path
    for old in sorted(mapping.keys(), key=len, reverse=True):
        new = mapping[old]
        if path == old or path.startswith(old + os.sep):
            return new + path[len(old) :]
    return path


def process_media_file(
    file_path: str,
    root_path: str,
    args: argparse.Namespace,
    progress: Optional[dict] = None,
) -> Tuple[str, str, Optional[Tuple[str, str]]]:
    if not os.path.exists(file_path):
        print(f"Missing file, skipping: {file_path}", file=sys.stderr)
        return ("skipped", file_path, None)
    print(f"\nProcessing: {file_path}")
    query = guess_search_query(file_path)
    if not query:
        print("  Unable to derive a CSFD query, skipping.", file=sys.stderr)
        return ("skipped", file_path, None)
    year_hint = find_year_hint(os.path.basename(file_path), os.path.basename(os.path.dirname(file_path)))
    if not year_hint and args.year:
        year_hint = args.year
    file_duration = get_media_duration(file_path)
    context = {
        "file_path": file_path,
        "file_name": os.path.basename(file_path),
        "file_duration": file_duration,
        "year_hint": year_hint,
        "progress": progress or {},
        "derived_query": query,
    }
    if progress and not supports_curses():
        total = progress.get("total")
        counts = progress.get("counts", {})
        processed = sum(counts.values())
        remaining = (total - processed) if total else None
        current_index = progress.get("current_index")
        summary_parts = [
            f"Progress: processing {current_index or processed + 1}/{total}" if total else "Progress: processing items",
            f"done {processed}",
        ]
        if remaining is not None:
            summary_parts.append(f"remaining {remaining}")
        breakdown = ", ".join(f"{name}={counts.get(name, 0)}" for name in ("renamed", "unchanged", "skipped"))
        if breakdown:
            summary_parts.append(breakdown)
        print("  " + " | ".join(summary_parts))
    try:
        selection = interactive_select_title(
            query,
            args.max_results,
            args.auto_choice,
            year_hint,
            context=context,
        )
    except UserAbort:
        raise
    if not selection:
        print("  Skipped.")
        return ("skipped", file_path, None)
    title = selection["title"]
    year = selection.get("year")
    display = f"{title} ({year})" if year else title
    print(f"  Selected: {display}")
    base_name = format_media_name(title, year)
    if not base_name:
        print("  Computed name is empty, skipping.", file=sys.stderr)
        return ("skipped", file_path, None)
    new_file_path, dir_change, changed = rename_media_paths(file_path, base_name, root_path)
    outcome = "renamed" if changed else "unchanged"
    return (outcome, new_file_path, dir_change)


def iter_video_files(root_path: str) -> List[str]:
    matches: List[str] = []
    for current_root, dirnames, files in os.walk(root_path):
        dirnames.sort()
        for name in sorted(files):
            if is_video_file(name):
                matches.append(os.path.join(current_root, name))
    return matches


def process_library_path(args: argparse.Namespace) -> int:
    if not args.path:
        return 1
    root_path = os.path.abspath(args.path)
    if not os.path.exists(root_path):
        print(f"Path does not exist: {root_path}", file=sys.stderr)
        return 1
    if os.path.isfile(root_path):
        if not is_video_file(root_path):
            print(f"No supported video files under: {root_path}", file=sys.stderr)
            return 1
        try:
            progress_info = {"current_index": 1, "total": 1, "counts": {}}
            outcome, _, _ = process_media_file(
                root_path,
                os.path.dirname(root_path),
                args,
                progress=progress_info,
            )
        except UserAbort:
            print("Aborted by user.", file=sys.stderr)
            return 1
        return 0 if outcome in {"renamed", "unchanged"} else 1
    files = iter_video_files(root_path)
    if not files:
        print(f"No supported video files under: {root_path}", file=sys.stderr)
        return 1
    dir_mapping: dict[str, str] = {}
    stats = {"renamed": 0, "unchanged": 0, "skipped": 0}
    try:
        for idx in range(len(files)):
            mapped_path = remap_path(files[idx], dir_mapping)
            progress_info = {
                "current_index": idx + 1,
                "total": len(files),
                "counts": dict(stats),
            }
            outcome, new_file_path, dir_change = process_media_file(
                mapped_path,
                root_path,
                args,
                progress=progress_info,
            )
            stats[outcome] = stats.get(outcome, 0) + 1
            files[idx] = new_file_path
            if dir_change:
                old_dir, new_dir = dir_change
                dir_mapping[os.path.abspath(old_dir)] = os.path.abspath(new_dir)
                for j in range(idx + 1, len(files)):
                    files[j] = remap_path(files[j], dir_mapping)
    except UserAbort:
        print("Aborted by user.", file=sys.stderr)
        return 1
    total = len(files)
    print(
        f"\nProcessed {total} file(s): {stats['renamed']} renamed, {stats['unchanged']} already matching, {stats['skipped']} skipped."
    )
    return 0


def interactive_lookup(args: argparse.Namespace) -> int:
    query = derive_search_query(args.filename or "", args.query)
    if not query:
        print("Unable to derive a CSFD query from the provided filename.", file=sys.stderr)
        return 1
    context = {
        "display_name": args.filename or args.query or query,
        "year_hint": args.year,
        "progress": {"current_index": 1, "total": 1, "counts": {}},
        "derived_query": query,
    }
    if args.filename and os.path.exists(args.filename):
        context.update(
            {
                "file_path": args.filename,
                "file_name": os.path.basename(args.filename),
                "file_duration": get_media_duration(args.filename),
            }
        )
    try:
        selection = interactive_select_title(query, args.max_results, args.auto_choice, args.year, context=context)
    except UserAbort:
        return 1
    if not selection:
        return 1
    title = selection["title"]
    year = selection.get("year")
    print(f"{title}|{year or ''}")
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.epilog = textwrap.dedent(
        """
        Usage examples:
          python title_lookup_service.py --filename "Temn ryt (2008) 1080p.mkv"
          python title_lookup_service.py --query "Pulp Fiction Historky z podsveti" --year 1994
          python title_lookup_service.py --path /data/library/Misc
        """
    )
    parser.add_argument("--path", help="Directory to scan recursively and rename entries for Jellyfin")
    parser.add_argument("--filename", help="Original filename (used to derive the search query)")
    parser.add_argument("--query", help="Optional manual search query")
    parser.add_argument("--year", type=int, help="Year hint passed back if CSFD data lacks it")
    parser.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS, help="Limit displayed CSFD hits")
    parser.add_argument(
        "--auto-choice",
        type=int,
        help="Pick a specific result automatically (useful for automated tests)",
    )
    args = parser.parse_args(argv)
    if args.path:
        if args.filename or args.query:
            parser.error("--path cannot be combined with --filename or --query")
    elif not args.filename and not args.query:
        parser.error("Provide --filename and/or --query, or use --path")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.path:
        return process_library_path(args)
    return interactive_lookup(args)


if __name__ == "__main__":
    sys.exit(main())
