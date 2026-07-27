"""Daily Picks — per-viewer TMDB recommendation catalogs.

Vendored from the standalone nuvio-recs service so one addon per viewer can
serve both catalogs and streams. Kept as a self-contained subpackage rather
than merged into stream-picker's modules: this half runs a nightly batch and
owns its own SQLite, and keeping the seam visible means a change here cannot
quietly reach the playback path.
"""
