"""Shared utilities for netmon Python scripts (TUI, chart)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Satellite file suffixes for the old flat-file format
_SATELLITE_SUFFIXES = (
    "-traffic.csv", "-connections.csv", "-scan.csv",
    "-udp.csv", "-diagnostics.csv",
)


def to_float(value: str) -> Optional[float]:
    if value is None:
        return None
    value = value.strip()
    if not value or value == "?":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def latest_main_log(log_dir: Path) -> Optional[Path]:
    """Find the most recent main CSV across both session formats."""
    # New format: call-STAMP/main.csv
    candidates = list(log_dir.glob("call-*/main.csv"))
    # Old format: call-STAMP.csv (flat files)
    for path in log_dir.glob("call-*.csv"):
        if path.name.endswith(_SATELLITE_SUFFIXES):
            continue
        candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _is_session_dir(main_file: Path) -> bool:
    """True if main_file lives in a per-session directory (new format)."""
    return main_file.name == "main.csv"


def resolve_related(main_file: Path) -> Tuple[Path, Path, Path, Path, Path]:
    """Return (traffic, connections, scan, udp, diagnostics) paths."""
    if _is_session_dir(main_file):
        d = main_file.parent
        return (d / "traffic.csv", d / "connections.csv",
                d / "scan.csv", d / "udp.csv",
                d / "diagnostics.csv")
    base = str(main_file)[:-4] if str(main_file).endswith(".csv") else str(main_file)
    return (Path(f"{base}-traffic.csv"), Path(f"{base}-connections.csv"),
            Path(f"{base}-scan.csv"), Path(f"{base}-udp.csv"),
            Path(f"{base}-diagnostics.csv"))


def resolve_diag_file(main_file: Path) -> Path:
    return resolve_related(main_file)[4]


def resolve_main_file(diag_file: Path) -> Path:
    if diag_file.name == "diagnostics.csv":
        return diag_file.parent / "main.csv"
    return Path(str(diag_file).replace("-diagnostics.csv", ".csv"))


def session_name(main_file: Optional[Path]) -> str:
    """Derive a human-readable session name from the main file path."""
    if not main_file:
        return "netmon"
    if _is_session_dir(main_file):
        return main_file.parent.name
    return main_file.stem


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read a CSV file into a list of dicts. Returns [] if missing."""
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))
