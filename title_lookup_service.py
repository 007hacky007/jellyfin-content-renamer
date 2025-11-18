#!/usr/bin/env python3
"""Interactive CSFD lookup helper for the Jellyfin content renamer."""

from __future__ import annotations

import argparse
import gzip
import os
import random
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from typing import Iterable, List, Optional, Sequence, Tuple

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


class UserAbort(Exception):
    """Raised when the user aborts the interactive workflow."""



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
    return f"  {idx}. {title}{suffix}\n      {url}"


def prompt(prompt_text: str) -> str:
    try:
        return input(prompt_text)
    except EOFError:
        return ""


def select_result(results: Sequence[dict], query: str, auto_choice: Optional[int]) -> Tuple[str, Optional[dict]]:
    if not results:
        return ("skip", None)
    if auto_choice is not None:
        if 1 <= auto_choice <= len(results):
            return ("accept", results[auto_choice - 1])
        return ("skip", None)
    print(f"\nFound {len(results)} result(s) for '{query}':")
    for idx, result in enumerate(results, start=1):
        print(format_result(idx, result))
    print(
        "\nChoose an option: [1-{}], Enter to accept #1, 'r' to refine search, 's' to skip, or 'q' to abort.".format(
            len(results)
        )
    )
    while True:
        choice = prompt("Selection> ").strip().lower()
        if choice in {"q", "quit"}:
            return ("abort", None)
        if choice in {"s", "skip"}:
            return ("skip", None)
        if choice == "":
            return ("accept", results[0])
        if choice.startswith("r"):
            return ("refine", None)
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(results):
                return ("accept", results[idx - 1])
        print("Invalid choice, please try again.")


def interactive_select_title(
    query: str,
    max_results: int,
    auto_choice: Optional[int],
    year_hint: Optional[int],
) -> Optional[dict]:
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
        action, selection = select_result(results, current_query, pending_auto_choice)
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
    try:
        selection = interactive_select_title(query, args.max_results, args.auto_choice, year_hint)
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
            outcome, _, _ = process_media_file(root_path, os.path.dirname(root_path), args)
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
            outcome, new_file_path, dir_change = process_media_file(mapped_path, root_path, args)
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
    try:
        selection = interactive_select_title(query, args.max_results, args.auto_choice, args.year)
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
