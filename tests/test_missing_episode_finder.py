import os
import tempfile
from unittest.mock import patch

from missing_episode_finder import (
    CSFDLookup,
    CSFDShowCandidate,
    fetch_csfd_show_detail,
    analyze_show,
    derive_show_search_query,
    parse_csfd_show_detail,
    format_csfd_display_name,
)


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb"):
        pass


def test_analyze_show_detects_missing_episode_numbers() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        show_dir = os.path.join(tmpdir, "Kancl")
        season_dir = os.path.join(show_dir, "Season 04")
        _touch(os.path.join(season_dir, "Fun Run (1) S04E01.mkv"))
        _touch(os.path.join(season_dir, "Fun Run (2) S04E02.mkv"))
        _touch(os.path.join(season_dir, "Money (2) S04E04.mkv"))
        report = analyze_show("Kancl", show_dir)
        assert 4 in report.seasons
        season = report.seasons[4]
        assert season.episodes_present == [1, 2, 4]
        assert season.missing_episodes == [3]


def test_analyze_show_supports_alternative_episode_formats() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        show_dir = os.path.join(tmpdir, "Example Show")
        season_dir = os.path.join(show_dir, "S02")
        _touch(os.path.join(season_dir, "Episode 2x01.mkv"))
        _touch(os.path.join(season_dir, "Episode 2x03.mkv"))
        report = analyze_show("Example Show", show_dir)
        season = report.seasons[2]
        assert season.episodes_present == [1, 3]
        assert season.missing_episodes == [2]


def test_analyze_show_detects_whole_missing_seasons() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        show_dir = os.path.join(tmpdir, "Gap Show")
        season_one = os.path.join(show_dir, "Season 01")
        season_three = os.path.join(show_dir, "Season 03")
        _touch(os.path.join(season_one, "Pilot S01E01.mkv"))
        _touch(os.path.join(season_three, "Return S03E01.mkv"))
        report = analyze_show("Gap Show", show_dir)
        assert report.missing_seasons == [2]


def test_analyze_show_uses_metadata_episode_counts() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        show_dir = os.path.join(tmpdir, "Metadata Show")
        season_dir = os.path.join(show_dir, "Season 02")
        _touch(os.path.join(season_dir, "Episode S02E01.mkv"))
        _touch(os.path.join(season_dir, "Episode S02E02.mkv"))
        metadata = CSFDShowCandidate(
            id=123,
            title="Metadata Show",
            year=None,
            original_title=None,
            origins=[],
            url="https://example.com",
            season_episode_counts={2: 4},
        )
        report = analyze_show("Metadata Show", show_dir, metadata=metadata)
        assert report.seasons[2].missing_episodes == [3, 4]


def test_parse_csfd_show_detail_extracts_episode_counts() -> None:
    html = """
    <div class="film-episodes-list">
        <ul>
            <li>
                <h3 class="film-title">
                    <a href="/film/1/serie-1/" class="film-title-name">Série 1</a>
                    <span class="film-title-info"><span class="info">(1989)</span> - 13 epizod</span>
                </h3>
            </li>
            <li>
                <h3 class="film-title">
                    <a href="/film/1/serie-2/" class="film-title-name">Série 2</a>
                    <span class="film-title-info"><span class="info">(1990)</span> - 22 epizod</span>
                </h3>
            </li>
        </ul>
    </div>
    """
    detail = parse_csfd_show_detail(html)
    assert detail["season_episode_counts"] == {1: 13, 2: 22}


def test_fetch_csfd_show_detail_handles_paginated_seasons() -> None:
    page_one = """
    <div class="box-header"><h3>Série (4)</h3></div>
    <div class="film-episodes-list">
        <ul>
            <li>
                <h3 class="film-title">
                    <a href="/film/demo/serie-1/" class="film-title-name">Série 1</a>
                    <span class="film-title-info"><span class="info">(1989)</span> - 13 epizod</span>
                </h3>
            </li>
            <li>
                <h3 class="film-title">
                    <a href="/film/demo/serie-2/" class="film-title-name">Série 2</a>
                    <span class="film-title-info"><span class="info">(1990)</span> - 22 epizod</span>
                </h3>
            </li>
        </ul>
    </div>
    """
    page_two = """
    <div class="film-episodes-list">
        <ul>
            <li>
                <h3 class="film-title">
                    <a href="/film/demo/serie-3/" class="film-title-name">Série 3</a>
                    <span class="film-title-info"><span class="info">(1991)</span> - 24 epizody</span>
                </h3>
            </li>
            <li>
                <h3 class="film-title">
                    <a href="/film/demo/serie-4/" class="film-title-name">Série 4</a>
                    <span class="film-title-info"><span class="info">(1992)</span> - 22 epizody</span>
                </h3>
            </li>
        </ul>
    </div>
    """

    def fake_download(url: str) -> str:
        if "seriePage=2" in url:
            return page_two
        return page_one

    with patch("missing_episode_finder._download_csfd_html", side_effect=fake_download) as mock_download:
        detail = fetch_csfd_show_detail("https://www.csfd.cz/film/demo/prehled/")

    assert detail["season_episode_counts"] == {1: 13, 2: 22, 3: 24, 4: 22}
    assert mock_download.call_count == 2


def test_derive_show_search_query_strips_years_and_symbols() -> None:
    assert derive_show_search_query("Kancl (2005) Season_04") == "Kancl Season 04"


def test_csfd_lookup_uses_custom_choice_and_details() -> None:
    entries = [
        {"title": "Kancl", "year": 2005, "url": "/film/101-kancl/"},
        {"title": "Kancl", "year": 2014, "url": "/film/202-kancl/"},
    ]
    details = {
        "/film/101-kancl/": {"original_title": "The Office (US)", "origins": ["USA"], "media_type": "seriál"},
        "/film/202-kancl/": {"original_title": "Kancl (CZ)", "origins": ["Česko"], "media_type": "seriál"},
    }
    with patch("missing_episode_finder.fetch_csfd_results", return_value=entries) as mock_search, patch(
        "missing_episode_finder.fetch_csfd_show_detail",
        side_effect=lambda url: details[url],
    ) as mock_detail:
        lookup = CSFDLookup(max_results=5, chooser=lambda _, options: options[1])
        result = lookup.resolve("Kancl (2005)")
    assert mock_search.call_args[0][0] == "Kancl"
    assert mock_detail.call_count == 2
    assert result is not None
    assert result.origins == ["Česko"]
    assert result.original_title == "Kancl (CZ)"


def test_csfd_lookup_skips_filmy_placeholder() -> None:
    entries = [
        {"title": "Filmy", "year": None, "url": "/film/0-filmy/"},
        {"title": "Kancl", "year": 2005, "url": "/film/101-kancl/"},
    ]

    def detail_lookup(url: str) -> dict:
        assert url != "/film/0-filmy/"
        return {"original_title": "The Office", "origins": ["USA"], "media_type": "seriál"}

    with patch("missing_episode_finder.fetch_csfd_results", return_value=entries), patch(
        "missing_episode_finder.fetch_csfd_show_detail",
        side_effect=detail_lookup,
    ):
        lookup = CSFDLookup(max_results=5)
        result = lookup.resolve("Kancl")
    assert result is not None
    assert result.title == "Kancl"


def test_csfd_lookup_prefers_series_even_if_films_first() -> None:
    entries = [
        {"title": "Černá kniha", "year": 2006, "url": "/film/1-cerna-kniha/"},
        {"title": "Malá černá skříňka", "year": 2004, "url": "/film/2-mala-cerna-skrinka/"},
        {"title": "Black Books", "year": 2000, "url": "/film/3-black-books/"},
    ]
    details = {
        "/film/1-cerna-kniha/": {"media_type": "Film", "origins": ["Nizozemsko"], "original_title": "Zwartboek", "localized_title": "Černá kniha"},
        "/film/2-mala-cerna-skrinka/": {"media_type": "Film", "origins": ["USA"], "original_title": "Little Black Book", "localized_title": "Malá černá skříňka"},
        "/film/3-black-books/": {
            "media_type": "Seriál",
            "origins": ["Velká Británie"],
            "original_title": "Black Books",
            "localized_title": "Černá kniha",
            "total_seasons": 3,
        },
    }

    with patch("missing_episode_finder.fetch_csfd_results", return_value=entries), patch(
        "missing_episode_finder.fetch_csfd_show_detail",
        side_effect=lambda url: details[url],
    ) as mock_detail:
        lookup = CSFDLookup(max_results=1)
        result = lookup.resolve("Black Books")

    assert mock_detail.call_count == 3
    assert result is not None
    assert result.title == "Černá kniha"
    assert result.total_seasons == 3


def test_csfd_lookup_skips_entries_without_type() -> None:
    entries = [
        {"title": "Some Film", "year": 2010, "url": "/film/10-some-film/"},
        {"title": "Some Series", "year": 2011, "url": "/film/11-some-series/"},
    ]
    details = {
        "/film/10-some-film/": {"media_type": "", "origins": ["USA"], "original_title": "Some Film", "localized_title": "Some Film"},
        "/film/11-some-series/": {"media_type": "Seriál", "origins": ["USA"], "original_title": "Some Series", "localized_title": "Nějaký seriál"},
    }
    with patch("missing_episode_finder.fetch_csfd_results", return_value=entries), patch(
        "missing_episode_finder.fetch_csfd_show_detail",
        side_effect=lambda url: details[url],
    ):
        lookup = CSFDLookup(max_results=5)
        result = lookup.resolve("Some Series")
    assert result is not None
    assert result.title == "Nějaký seriál"


def test_analyze_show_uses_csfd_total_seasons_for_trailing_gaps() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        show_dir = os.path.join(tmpdir, "Long Show")
        season1 = os.path.join(show_dir, "Season 01")
        season3 = os.path.join(show_dir, "Season 03")
        _touch(os.path.join(season1, "Pilot S01E01.mkv"))
        _touch(os.path.join(season3, "Return S03E01.mkv"))
        metadata = CSFDShowCandidate(
            id=10,
            title="Long Show",
            year=None,
            original_title=None,
            origins=[],
            url="https://example.com",
            total_seasons=5,
        )
        report = analyze_show("Long Show", show_dir, metadata=metadata)
        assert report.missing_seasons == [2, 4, 5]
def test_format_csfd_display_name_shows_both_titles_when_different() -> None:
    candidate = CSFDShowCandidate(
        id=1,
        title="Kancl",
        year=2005,
        original_title="The Office",
        origins=["USA"],
        url="https://example.com",
    )
    assert format_csfd_display_name(candidate) == "Kancl / The Office (2005)"


def test_format_csfd_display_name_omits_duplicate_original() -> None:
    candidate = CSFDShowCandidate(
        id=2,
        title="Kancl",
        year=None,
        original_title="Kancl",
        origins=[],
        url="https://example.com",
    )
    assert format_csfd_display_name(candidate) == "Kancl (?)"
