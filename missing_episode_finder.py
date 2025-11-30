#!/usr/bin/env python3
"""Detect missing episodes for TV shows stored in Jellyfin-style folders."""

from __future__ import annotations

import argparse
import curses
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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

SEASON_HINT_PATTERN = re.compile(r"(?i)(?:season|series|s)\s*(\d+)")
SEASON_SHORT_PATTERN = re.compile(r"(?i)^s(\d{1,2})$")
EPISODE_PATTERNS = [
    re.compile(r"(?i)[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})"),
    re.compile(r"(?i)(\d{1,2})x(\d{1,3})"),
]
EPISODE_ONLY_PATTERN = re.compile(r"(?i)[Ee](\d{1,3})")
SPECIALS_PATTERN = re.compile(r"(?i)specials")


@dataclass
class SeasonReport:
    season: int
    episodes_present: List[int] = field(default_factory=list)
    missing_episodes: List[int] = field(default_factory=list)


@dataclass
class ShowReport:
    name: str
    path: str
    seasons: Dict[int, SeasonReport] = field(default_factory=dict)

    def missing_summary(self) -> List[Tuple[int, List[int]]]:
        return [
            (season, report.missing_episodes)
            for season, report in sorted(self.seasons.items())
            if report.missing_episodes
        ]


def supports_curses() -> bool:
    term = os.environ.get("TERM", "")
    return sys.stdin.isatty() and sys.stdout.isatty() and term and term.lower() != "dumb"


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


def analyze_show(show_name: str, show_path: str) -> ShowReport:
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
    return ShowReport(name=show_name, path=show_path, seasons=seasons)


def format_episode(tag_season: int, episode: int) -> str:
    return f"S{tag_season:02d}E{episode:02d}"


def display_progress(show_idx: int, total: int, report: ShowReport) -> None:
    header = f"[{show_idx}/{total}] {report.name}"
    print(header)
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


def summarize_results(reports: Sequence[ShowReport]) -> None:
    print("\nMissing episodes summary:")
    any_missing = False
    for report in reports:
        missing = report.missing_summary()
        if not missing:
            continue
        any_missing = True
        print(f"- {report.name}")
        for season, episodes in missing:
            formatted = ", ".join(format_episode(season, ep) for ep in episodes)
            print(f"    Season {season:02d}: {formatted}")
    if not any_missing:
        print("All processed seasons appear complete (no gaps detected).")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find missing TV episodes under a Jellyfin library path.")
    parser.add_argument("--path", required=True, help="Root directory containing show folders")
    parser.add_argument("--show", help="Case-insensitive substring to target a specific show")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root_path = os.path.abspath(args.path)
    shows = discover_shows(root_path)
    if not shows:
        print(f"No show folders found under: {root_path}", file=sys.stderr)
        return 1
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
        report = analyze_show(show_name, show_path)
        display_progress(idx, total, report)
        reports.append(report)
    summarize_results(reports)
    return 0


if __name__ == "__main__":
    sys.exit(main())
