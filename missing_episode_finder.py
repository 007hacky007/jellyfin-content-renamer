#!/usr/bin/env python3
"""Detect missing episodes for TV shows stored in Jellyfin-style folders."""

from __future__ import annotations

import argparse
import curses
import gzip
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from title_lookup_service import build_headers, fetch_csfd_results

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

DEFAULT_CSFD_MAX_RESULTS = 5

SEASON_HINT_PATTERN = re.compile(r"(?i)(?:season|series|s)\s*(\d+)")
SEASON_SHORT_PATTERN = re.compile(r"(?i)^s(\d{1,2})$")
EPISODE_PATTERNS = [
    re.compile(r"(?i)[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})"),
    re.compile(r"(?i)(\d{1,2})x(\d{1,3})"),
]
EPISODE_ONLY_PATTERN = re.compile(r"(?i)[Ee](\d{1,3})")
SPECIALS_PATTERN = re.compile(r"(?i)specials")
SHOW_YEAR_PATTERN = re.compile(r"\((?:19|20)\d{2}(?:/(?:19|20)\d{2})?\)")
SHOW_NON_ALNUM = re.compile(r"[^0-9a-zA-ZáéěíóúůýščřžÁÉĚÍÓÚŮÝŠČŘŽ ]+")
SHOW_WHITESPACE = re.compile(r"\s+")
CSFD_ID_PATTERN = re.compile(r"/film/(\d+)-")
ORIGIN_SPLITTER = re.compile(r"[,/]")


@dataclass
class SeasonReport:
    season: int
    episodes_present: List[int] = field(default_factory=list)
    missing_episodes: List[int] = field(default_factory=list)


@dataclass
class CSFDShowCandidate:
    id: Optional[int]
    title: str
    year: Optional[int]
    original_title: Optional[str]
    origins: List[str]
    url: str


@dataclass
class ShowReport:
    name: str
    path: str
    seasons: Dict[int, SeasonReport] = field(default_factory=dict)
    missing_seasons: List[int] = field(default_factory=list)
    csfd: Optional[CSFDShowCandidate] = None

    def missing_summary(self) -> List[Tuple[int, List[int]]]:
        return [
            (season, report.missing_episodes)
            for season, report in sorted(self.seasons.items())
            if report.missing_episodes
        ]


def supports_curses() -> bool:
    term = os.environ.get("TERM", "")
    return sys.stdin.isatty() and sys.stdout.isatty() and term and term.lower() != "dumb"


def derive_show_search_query(name: str) -> str:
    if not name:
        return ""
    cleaned = SHOW_YEAR_PATTERN.sub(" ", name)
    cleaned = SHOW_NON_ALNUM.sub(" ", cleaned)
    cleaned = SHOW_WHITESPACE.sub(" ", cleaned)
    return cleaned.strip()


class CSFDShowDetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_origin = False
        self._origin_span_depth = 0
        self._origin_parts: List[str] = []
        self._in_names_list = False
        self._capturing_name = False
        self._name_suppressed = 0
        self._name_parts: List[str] = []
        self.original_title: Optional[str] = None
        self.origins: List[str] = []
        self.media_type: Optional[str] = None
        self._capturing_type = False

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: D401
        attr_map = dict(attrs)
        class_names = attr_map.get("class", "")
        class_set = {part.strip() for part in class_names.split() if part.strip()}
        if tag == "div" and "origin" in class_set:
            self._in_origin = True
            self._origin_span_depth = 0
            self._origin_parts.clear()
        elif self._in_origin and tag == "span":
            self._origin_span_depth += 1
        if tag == "ul" and "film-names" in class_set:
            self._in_names_list = True
        elif tag == "li" and self._in_names_list and not self.original_title:
            if "more-names" in class_set:
                return
            self._capturing_name = True
            self._name_parts.clear()
        elif tag == "span" and self._capturing_name and ("span-more-small" in class_set or "normal" in class_set or "info" in class_set):
            self._name_suppressed += 1
        if tag == "span" and "type" in class_set and not self.media_type:
            self._capturing_type = True

    def handle_endtag(self, tag: str) -> None:  # noqa: D401
        if tag == "div" and self._in_origin:
            self._in_origin = False
            self.origins = self._finalize_origins()
        elif tag == "span" and self._in_origin and self._origin_span_depth > 0:
            self._origin_span_depth -= 1
        if tag == "li" and self._capturing_name:
            self._capturing_name = False
            if not self.original_title:
                candidate = "".join(self._name_parts).strip()
                if candidate:
                    self.original_title = candidate
            self._name_parts.clear()
        elif tag == "ul" and self._in_names_list:
            self._in_names_list = False
        elif tag == "span" and self._capturing_name and self._name_suppressed > 0:
            self._name_suppressed -= 1
        if tag == "span" and self._capturing_type:
            self._capturing_type = False

    def handle_data(self, data: str) -> None:  # noqa: D401
        if self._in_origin and self._origin_span_depth == 0:
            self._origin_parts.append(data)
        if self._capturing_name and self._name_suppressed == 0:
            self._name_parts.append(data)
        if self._capturing_type:
            current = (self.media_type or "") + data
            self.media_type = current.strip()

    def _finalize_origins(self) -> List[str]:
        raw = "".join(self._origin_parts).strip()
        if not raw:
            return []
        country_segment = raw.split("(", 1)[0]
        values = [part.strip(" ,") for part in ORIGIN_SPLITTER.split(country_segment) if part.strip(" ,")]
        unique: List[str] = []
        for value in values:
            if value and value not in unique:
                unique.append(value)
        return unique


def extract_csfd_id(url: str) -> Optional[int]:
    match = CSFD_ID_PATTERN.search(url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_csfd_show_detail(html: str) -> Dict[str, Optional[str]]:
    parser = CSFDShowDetailParser()
    parser.feed(html)
    original = parser.original_title.strip() if parser.original_title else None
    origins = parser.origins
    media_type = parser.media_type.strip() if parser.media_type else None
    return {"original_title": original, "origins": origins, "media_type": media_type}


def _decode_csfd_payload(raw: bytes, encoding: str) -> str:
    codec = encoding.lower()
    try:
        if "gzip" in codec:
            return gzip.decompress(raw).decode("utf-8", errors="ignore")
        if "deflate" in codec:
            return zlib.decompress(raw).decode("utf-8", errors="ignore")
        return raw.decode("utf-8", errors="ignore")
    except (OSError, zlib.error, UnicodeDecodeError):  # pragma: no cover - corrupted payloads
        return raw.decode("utf-8", errors="ignore")


@lru_cache(maxsize=128)
def fetch_csfd_show_detail(url: str) -> Dict[str, Optional[str]]:
    if not url:
        return {}
    absolute_url = urllib.parse.urljoin("https://www.csfd.cz", url)
    request = urllib.request.Request(absolute_url, headers=build_headers())
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "")
    except urllib.error.URLError:  # pragma: no cover - network failure
        return {}
    html = _decode_csfd_payload(raw, encoding)
    return parse_csfd_show_detail(html)


class CSFDLookup:
    def __init__(
        self,
        max_results: int = DEFAULT_CSFD_MAX_RESULTS,
        chooser: Optional[Callable[[str, Sequence[CSFDShowCandidate]], Optional[CSFDShowCandidate]]] = None,
    ) -> None:
        self.max_results = max(1, max_results)
        self.chooser = chooser or select_csfd_candidate

    def resolve(self, display_name: str) -> Optional[CSFDShowCandidate]:
        query = derive_show_search_query(display_name)
        if not query:
            return None
        fetch_limit = max(self.max_results, self.max_results * 3)
        results = fetch_csfd_results(query, fetch_limit)
        candidates = self._build_candidates(results)
        if len(candidates) > self.max_results:
            candidates = candidates[: self.max_results]
        if not candidates:
            print(f"No CSFD matches for '{display_name}'.", file=sys.stderr)
            return None
        if len(candidates) == 1:
            return candidates[0]
        selection = self.chooser(display_name, candidates)
        if selection is None:
            print(f"Skipped CSFD selection for '{display_name}'.", file=sys.stderr)
        return selection

    def _build_candidates(self, entries: Sequence[dict]) -> List[CSFDShowCandidate]:
        built: List[CSFDShowCandidate] = []
        for entry in entries:
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            if title.casefold() == "filmy".casefold():
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                continue
            detail = fetch_csfd_show_detail(url)
            if not detail:
                continue
            media_type = (detail.get("media_type") or "").lower()
            if media_type and "seri" not in media_type:
                continue
            year = entry.get("year") if isinstance(entry.get("year"), int) else None
            built.append(
                CSFDShowCandidate(
                    id=extract_csfd_id(url),
                    title=title,
                    year=year,
                    original_title=detail.get("original_title") or title,
                    origins=detail.get("origins") or [],
                    url=urllib.parse.urljoin("https://www.csfd.cz", url),
                )
            )
        return built


def format_csfd_display_name(candidate: CSFDShowCandidate) -> str:
    local_title = candidate.title.strip()
    original = (candidate.original_title or "").strip()
    show_original = original and original.casefold() != local_title.casefold()
    year = candidate.year or "?"
    if show_original:
        return f"{local_title} / {original} ({year})"
    return f"{local_title} ({year})"


def is_video_file(name: str) -> bool:
    _, ext = os.path.splitext(name)
    return ext.lower() in VIDEO_EXTENSIONS


def discover_shows(root_path: str) -> List[Tuple[str, str]]:
    try:
        entries = os.listdir(root_path)
    except FileNotFoundError:
        return []
    shows: List[Tuple[str, str]] = []
    for entry in sorted(entries):
        abs_path = os.path.join(root_path, entry)
        if os.path.isdir(abs_path):
            shows.append((entry, abs_path))
    return shows


def filter_shows(shows: Sequence[Tuple[str, str]], needle: str) -> List[Tuple[str, str]]:
    if not needle:
        return list(shows)
    lowered = needle.lower()
    return [show for show in shows if lowered in show[0].lower()]


def select_csfd_candidate(show_name: str, candidates: Sequence[CSFDShowCandidate]) -> Optional[CSFDShowCandidate]:
    if len(candidates) == 1:
        return candidates[0]
    selected_idx: Optional[int]
    if supports_curses():
        try:
            tui = CSFDShowSelectionTUI(show_name, candidates)
            selected_idx = tui.run()
        except Exception:
            selected_idx = None
    else:
        selected_idx = prompt_csfd_selection_cli(show_name, candidates)
    if selected_idx is None:
        return None
    return candidates[selected_idx]


def prompt_csfd_selection_cli(show_name: str, candidates: Sequence[CSFDShowCandidate]) -> Optional[int]:
    print(f"CSFD offers multiple matches for '{show_name}':")
    for idx, candidate in enumerate(candidates, start=1):
        origins = ", ".join(candidate.origins) if candidate.origins else "Unknown origin"
        display = format_csfd_display_name(candidate)
        print(f"  {idx}. {display} | {origins}")
    print("Press Enter to accept #1, or type the desired number. Type 'q' to skip.")
    while True:
        choice = input("Selection> ").strip().lower()
        if not choice:
            return 0
        if choice in {"q", "quit"}:
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(candidates):
                return idx - 1
        print("Invalid choice, try again.")


class CSFDShowSelectionTUI:
    def __init__(self, show_name: str, candidates: Sequence[CSFDShowCandidate]):
        self.show_name = show_name
        self.candidates = list(candidates)
        self.selected = 0
        self.outcome: Optional[int] = None

    def run(self) -> Optional[int]:
        try:
            curses.wrapper(self._main)
        except Exception:
            return None
        return self.outcome

    def _main(self, stdscr: "curses._CursesWindow") -> None:  # type: ignore[name-defined]
        curses.curs_set(0)
        while True:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            header = f"CSFD matches for '{self.show_name}' ({len(self.candidates)})"
            try:
                stdscr.addnstr(0, 0, header[:width], width)
                stdscr.addnstr(1, 0, "↑/↓ move • Enter select • q abort"[:width], width)
            except curses.error:
                pass
            visible_rows = max(1, (height - 4) // 2)
            top = max(0, min(self.selected - visible_rows // 2, len(self.candidates) - visible_rows))
            row = 2
            for idx in range(top, min(len(self.candidates), top + visible_rows)):
                candidate = self.candidates[idx]
                prefix = "> " if idx == self.selected else "  "
                line = f"{prefix}{format_csfd_display_name(candidate)}"
                attr = curses.A_REVERSE | curses.A_BOLD if idx == self.selected else curses.A_NORMAL
                try:
                    stdscr.addnstr(row, 0, line[:width], width, attr)
                except curses.error:
                    pass
                row += 1
                details = self._format_details(candidate)
                try:
                    stdscr.addnstr(row, 2, details[: max(0, width - 2)], max(0, width - 2))
                except curses.error:
                    pass
                row += 1
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                self.selected = max(0, self.selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.selected = min(len(self.candidates) - 1, self.selected + 1)
            elif key in (10, 13, curses.KEY_ENTER):
                self.outcome = self.selected
                break
            elif key in (ord("q"), ord("Q"), 27):
                self.outcome = None
                break

    def _format_details(self, candidate: CSFDShowCandidate) -> str:
        origins = ", ".join(candidate.origins) if candidate.origins else "Unknown origin"
        return origins


class ShowSelectionTUI:
    def __init__(self, query: str, candidates: Sequence[Tuple[str, str]]):
        self.query = query
        self.candidates = list(candidates)
        self.selected = 0
        self.outcome: Optional[int] = None

    def run(self) -> Optional[int]:
        try:
            curses.wrapper(self._main)
        except Exception:  # noqa: BLE001 - degrade gracefully
            return None
        return self.outcome

    def _main(self, stdscr: "curses._CursesWindow") -> None:  # type: ignore[name-defined]
        curses.curs_set(0)
        while True:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            header = f"Matches for '{self.query}': {len(self.candidates)} show(s)"
            try:
                stdscr.addnstr(0, 0, header[: width], width)
            except curses.error:
                pass
            instructions = "↑/↓ move • Enter select • q abort"
            try:
                stdscr.addnstr(1, 0, instructions[: width], width)
            except curses.error:
                pass
            visible = max(1, height - 3)
            top = max(0, min(self.selected - visible // 2, len(self.candidates) - visible))
            for idx in range(top, min(len(self.candidates), top + visible)):
                row = idx - top + 2
                name = self.candidates[idx][0]
                prefix = "> " if idx == self.selected else "  "
                attr = curses.A_REVERSE | curses.A_BOLD if idx == self.selected else curses.A_NORMAL
                try:
                    stdscr.addnstr(row, 0, (prefix + name)[: width], width, attr)
                except curses.error:
                    pass
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                self.selected = max(0, self.selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.selected = min(len(self.candidates) - 1, self.selected + 1)
            elif key in (10, 13, curses.KEY_ENTER):
                self.outcome = self.selected
                break
            elif key in (ord("q"), ord("Q"), 27):
                self.outcome = None
                break


def prompt_selection_cli(candidates: Sequence[Tuple[str, str]]):
    print("Multiple shows matched; choose one:")
    for idx, (name, _) in enumerate(candidates, start=1):
        print(f"  {idx}. {name}")
    while True:
        choice = input("Selection> ").strip()
        if not choice:
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(candidates):
                return idx - 1
        print("Invalid choice, try again.")


def select_show_candidate(query: str, candidates: Sequence[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    if supports_curses():
        tui = ShowSelectionTUI(query, candidates)
        selected_idx = tui.run()
    else:
        selected_idx = prompt_selection_cli(candidates)
    if selected_idx is None:
        return None
    return candidates[selected_idx]


def extract_season_hint(parts: Iterable[str]) -> Optional[int]:
    for part in reversed(list(parts)):
        normalized = part.strip()
        if SPECIALS_PATTERN.fullmatch(normalized):
            return 0
        match = SEASON_HINT_PATTERN.search(normalized)
        if match:
            return int(match.group(1))
        match_short = SEASON_SHORT_PATTERN.match(normalized)
        if match_short:
            return int(match_short.group(1))
    return None


def extract_episode_matches(name: str) -> List[Tuple[Optional[int], int]]:
    matches: List[Tuple[Optional[int], int]] = []
    for pattern in EPISODE_PATTERNS:
        for season_str, episode_str in pattern.findall(name):
            matches.append((int(season_str), int(episode_str)))
    if not matches:
        for episode_str in EPISODE_ONLY_PATTERN.findall(name):
            matches.append((None, int(episode_str)))
    return matches


def normalize_episode_numbers(values: Iterable[int]) -> List[int]:
    unique = sorted({value for value in values if value > 0})
    return unique


def analyze_show(show_name: str, show_path: str, metadata: Optional[CSFDShowCandidate] = None) -> ShowReport:
    seasons: Dict[int, SeasonReport] = {}
    for current_root, _, files in os.walk(show_path):
        rel_root = os.path.relpath(current_root, show_path)
        rel_parts = [] if rel_root == os.curdir else rel_root.split(os.sep)
        season_hint = extract_season_hint(rel_parts)
        for file_name in files:
            if not is_video_file(file_name):
                continue
            matches = extract_episode_matches(file_name)
            if not matches:
                if season_hint is not None:
                    seasons.setdefault(season_hint, SeasonReport(season=season_hint))
                continue
            for season_candidate, episode_candidate in matches:
                season_number = season_candidate if season_candidate is not None else season_hint
                if season_number is None:
                    continue
                report = seasons.setdefault(season_number, SeasonReport(season=season_number))
                if episode_candidate is not None:
                    report.episodes_present.append(episode_candidate)
    for report in seasons.values():
        if not report.episodes_present:
            continue
        episodes = normalize_episode_numbers(report.episodes_present)
        report.episodes_present = episodes
        if episodes:
            max_episode = episodes[-1]
            report.missing_episodes = [num for num in range(1, max_episode + 1) if num not in episodes]
    season_numbers = sorted(num for num in seasons if num > 0)
    missing_seasons: List[int] = []
    if season_numbers:
        max_season = season_numbers[-1]
        present = set(season_numbers)
        for season_number in range(1, max_season + 1):
            if season_number not in present:
                missing_seasons.append(season_number)
    return ShowReport(
        name=show_name,
        path=show_path,
        seasons=seasons,
        missing_seasons=missing_seasons,
        csfd=metadata,
    )


def format_episode(tag_season: int, episode: int) -> str:
    return f"S{tag_season:02d}E{episode:02d}"


def display_progress(show_idx: int, total: int, report: ShowReport) -> None:
    header = f"[{show_idx}/{total}] {report.name}"
    print(header)
    if report.csfd:
        origins = ", ".join(report.csfd.origins) if report.csfd.origins else "Unknown origin"
        original = report.csfd.original_title or "Unknown original title"
        print(
            f"  CSFD match: {report.csfd.title} ({report.csfd.year or '?'}) | {origins} | Original: {original}"
        )
    if not report.seasons:
        print("  No seasons detected (no season folders or SxxEyy markers).")
        return
    for season_number in sorted(report.seasons):
        season_report = report.seasons[season_number]
        missing = season_report.missing_episodes
        if not season_report.episodes_present:
            print(f"  Season {season_number:02d}: no episode markers found")
            continue
        if missing:
            formatted_missing = ", ".join(format_episode(season_number, ep) for ep in missing)
            print(f"  Season {season_number:02d}: missing {formatted_missing}")
        else:
            print(f"  Season {season_number:02d}: complete (1-{season_report.episodes_present[-1]:02d})")
    if report.missing_seasons:
        formatted_seasons = ", ".join(f"S{season:02d}" for season in report.missing_seasons)
        print(f"  Missing full seasons: {formatted_seasons}")


def summarize_results(reports: Sequence[ShowReport]) -> None:
    print("\nMissing episodes summary:")
    any_missing = False
    for report in reports:
        missing = report.missing_summary()
        has_missing = bool(missing or report.missing_seasons)
        if not has_missing:
            continue
        any_missing = True
        print(f"- {report.name}")
        if report.missing_seasons:
            formatted_seasons = ", ".join(f"S{season:02d}" for season in report.missing_seasons)
            print(f"    Missing full seasons: {formatted_seasons}")
        for season, episodes in missing:
            formatted = ", ".join(format_episode(season, ep) for ep in episodes)
            print(f"    Season {season:02d}: {formatted}")
    if not any_missing:
        print("All processed seasons appear complete (no gaps detected).")


def build_csfd_lookup(args: argparse.Namespace) -> Optional[CSFDLookup]:
    if args.no_csfd:
        return None
    return CSFDLookup(max_results=args.csfd_max_results)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find missing TV episodes under a Jellyfin library path.")
    parser.add_argument("--path", required=True, help="Root directory containing show folders")
    parser.add_argument("--show", help="Case-insensitive substring to target a specific show")
    parser.add_argument(
        "--csfd-max-results",
        type=int,
        default=DEFAULT_CSFD_MAX_RESULTS,
        help="How many CSFD matches to display in the selection UI (default: %(default)s).",
    )
    parser.add_argument(
        "--no-csfd",
        action="store_true",
        help="Disable CSFD lookups and rely only on local folder names.",
    )
    args = parser.parse_args(argv)
    if args.csfd_max_results <= 0:
        parser.error("--csfd-max-results must be a positive integer")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root_path = os.path.abspath(args.path)
    shows = discover_shows(root_path)
    if not shows:
        print(f"No show folders found under: {root_path}", file=sys.stderr)
        return 1
    csfd_lookup = build_csfd_lookup(args)
    if args.show:
        matches = filter_shows(shows, args.show)
        if not matches:
            print(f"No shows matched '{args.show}'.", file=sys.stderr)
            return 1
        selected = select_show_candidate(args.show, matches)
        if selected is None:
            print("Selection aborted.", file=sys.stderr)
            return 1
        shows_to_process = [selected]
    else:
        shows_to_process = shows
    reports: List[ShowReport] = []
    total = len(shows_to_process)
    for idx, (show_name, show_path) in enumerate(shows_to_process, start=1):
        csfd_metadata = csfd_lookup.resolve(show_name) if csfd_lookup else None
        report = analyze_show(show_name, show_path, metadata=csfd_metadata)
        display_progress(idx, total, report)
        reports.append(report)
    summarize_results(reports)
    return 0


if __name__ == "__main__":
    sys.exit(main())
