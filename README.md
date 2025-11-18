# jellyfin-content-renamer

**TL;DR**: Interactive CLI that looks up titles on ČSFD, previews metadata in a curses TUI, and renames your media files into Jellyfin-friendly structures.

## Quick Start

- Install ffmpeg/ffprobe if you want to display movie length information.
- Run `./title_lookup_service.py --path /path/to/media/folder` to launch the TUI and process files.

## Highlights

- Recursive scan with Jellyfin-safe folder/file naming.
- ČSFD search with interactive selection, progress bar, and duration deltas.
- Optional skip suggestions when names already match metadata.
