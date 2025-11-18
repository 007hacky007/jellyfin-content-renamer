import os
import tempfile

from title_lookup_service import (
    find_year_hint,
    format_media_name,
    parse_runtime,
    guess_search_query,
    remap_path,
    rename_media_paths,
    sanitize_component,
)


def test_sanitize_component_removes_invalid_characters() -> None:
    assert sanitize_component("Bad:/Name*? ") == "Bad Name"


def test_format_media_name_includes_year() -> None:
    assert format_media_name("Movie Title", 1999) == "Movie Title (1999)"


def test_find_year_hint_prefers_first_match() -> None:
    assert find_year_hint("Title 2001", "Other 1999") == 2001


def test_guess_search_query_uses_directory_fallback() -> None:
    path = os.path.join("/tmp", "Some Title (2010)", "1080p.mkv")
    assert guess_search_query(path) == "Some Title"


def test_remap_path_prefers_longest_match() -> None:
    mapping = {
        "/a/b": "/a/c",
        "/a/b/d": "/a/c/e",
    }
    assert remap_path("/a/b/d/file.mkv", mapping) == "/a/c/e/file.mkv"


def test_parse_runtime_extracts_minutes() -> None:
    html = "<div class='runtime'>Délka: 142 min</div>"
    assert parse_runtime(html) == 142


def test_rename_media_paths_updates_file_and_directory() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = os.path.join(tmpdir, "Library")
        movie_dir = os.path.join(root, "old name")
        os.makedirs(movie_dir)
        original = os.path.join(movie_dir, "movie_file.mkv")
        with open(original, "wb"):
            pass
        base_name = "New Movie (2001)"
        new_path, dir_change, changed = rename_media_paths(original, base_name, root)
        expected_dir = os.path.join(root, base_name)
        expected_file = os.path.join(expected_dir, f"{base_name}.mkv")
        assert changed
        assert dir_change == (os.path.join(root, "old name"), expected_dir)
        assert new_path == expected_file
        assert os.path.exists(expected_file)


def test_rename_media_paths_creates_folder_for_root_level_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = tmpdir
        original = os.path.join(root, "Some Movie.mkv")
        with open(original, "wb"):
            pass
        base_name = "Some Movie (2020)"
        new_path, dir_change, changed = rename_media_paths(original, base_name, root)
        expected_dir = os.path.join(root, base_name)
        expected_file = os.path.join(expected_dir, f"{base_name}.mkv")
        assert changed
        assert dir_change is None
        assert new_path == expected_file
        assert os.path.exists(expected_file)
        assert os.path.isdir(expected_dir)
