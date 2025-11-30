import os
import tempfile

from missing_episode_finder import analyze_show


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
