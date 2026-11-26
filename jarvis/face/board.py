"""Read the conveyor board into immutable data for the Konveyör tab."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISLNK, S_ISREG

UNKNOWN = None
PANO_DIR = Path(__file__).absolute().parents[2] / "build" / "pano"
MAX_BYTES = 1024 * 1024
MAX_COUNT = 2**53
MAX_TEXT = 400
MAX_AGE = timedelta(minutes=15)
_PATH_PART = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]|DATA)(?:\..*)?", re.I)
_ISO_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


@dataclass(frozen=True, slots=True)
class Lane:
    id: str | None
    title: str | None
    doing: str | None


@dataclass(frozen=True, slots=True)
class Board:
    lanes_running: int | None = UNKNOWN
    lanes_max: int | None = UNKNOWN
    waiting_for_salih: int | None = UNKNOWN
    usage_pct: int | float | None = UNKNOWN
    plan_pct: int | float | None = UNKNOWN
    reset_at: datetime | None = UNKNOWN
    projected_pct: int | float | None = UNKNOWN
    today_finished: int | None = UNKNOWN
    today_passed: int | None = UNKNOWN
    lanes: tuple[Lane, ...] | None = UNKNOWN
    queue_head: tuple[str | None, ...] | None = UNKNOWN
    updated: datetime | None = UNKNOWN
    stale: bool = True


def _inside_pano(path: str | Path, pano_dir: str | Path) -> Path | None:
    raw = str(path)
    if raw.startswith(("//", "\\")) or ("/" in raw and "\\" in raw):
        return UNKNOWN
    raw_parts = re.split(r"[/\\]", raw)
    if any(part in (".", "..") for part in raw_parts) or "" in raw_parts[1:]:
        return UNKNOWN
    root = Path(pano_dir).absolute()
    candidate = Path(path)
    if candidate.drive and not candidate.is_absolute():
        return UNKNOWN
    candidate = candidate.absolute()
    relative = candidate.relative_to(root)
    if not relative.parts or any(
        not _PATH_PART.fullmatch(part) or _RESERVED.fullmatch(part) for part in relative.parts
    ):
        return UNKNOWN
    current = root
    for part in ("", *relative.parts):
        current /= part
        info = current.lstat()
        if S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
            return UNKNOWN
    resolved_root = root.resolve(strict=True)
    target = candidate.resolve(strict=True)
    if resolved_root != root or target.parts[:len(resolved_root.parts)] != resolved_root.parts:
        return UNKNOWN
    return target


def _parse_int(raw: str) -> int | None:
    # Constrain conversion per field, including integers above Python's digit limit.
    return int(raw) if len(raw) <= 17 else UNKNOWN


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        result[key] = UNKNOWN if key in result else value
    return result


def _mapping(value: object) -> dict:
    return value if type(value) is dict else {}


def _records(value: object) -> list[dict] | None:
    return value if type(value) is list and all(type(item) is dict for item in value) else UNKNOWN


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_COUNT else UNKNOWN


def _percent(value: object) -> int | float | None:
    return value if type(value) in (int, float) and 0 <= value <= 200 else UNKNOWN


def _text(value: object) -> str | None:
    if type(value) is str and 0 < len(value) <= MAX_TEXT and value.isprintable() and value.strip():
        return value
    return UNKNOWN


def _timestamp(value: object) -> datetime | None:
    if type(value) is not str or len(value) > 32 or not _ISO_TIMESTAMP.fullmatch(value):
        return UNKNOWN
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is not None:
            stamp.astimezone(timezone.utc)  # Reject offsets that overflow at the bounds.
            return stamp
    except (ValueError, OverflowError):
        return UNKNOWN
    return UNKNOWN


def _stale(updated: datetime | None, now: datetime) -> bool:
    if updated is UNKNOWN or type(now) is not datetime:
        return True
    try:
        if now.utcoffset() is not None:
            age = now.astimezone(timezone.utc) - updated.astimezone(timezone.utc)
            return not timedelta() <= age <= MAX_AGE
    except Exception:
        return True
    return True


def read(path: str | Path, now: datetime, *, pano_dir: str | Path = PANO_DIR) -> Board:
    """Read a file inside the trusted pano directory using an injected aware clock."""
    try:
        target = _inside_pano(path, pano_dir)
        if target is UNKNOWN:
            return Board()
        info = target.stat()
        if not S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            return Board()
        with target.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return Board()
        payload = _mapping(json.loads(raw, parse_int=_parse_int, object_pairs_hook=_object))
    except Exception:
        return Board()

    usage = _mapping(payload.get("usage"))
    today = _mapping(payload.get("today"))
    lanes_max = _count(_mapping(payload.get("conveyor")).get("lanes_max"))
    lanes = _records(payload.get("lanes"))
    queue = _records(payload.get("queue"))
    attention = _records(payload.get("attention"))
    updated = _timestamp(payload.get("updated"))
    running = len(lanes) if lanes is not UNKNOWN else UNKNOWN
    if running is not UNKNOWN and lanes_max is not UNKNOWN and running > lanes_max:
        running = UNKNOWN
    return Board(
        lanes_running=running,
        lanes_max=lanes_max,
        waiting_for_salih=len(attention) if attention is not UNKNOWN else UNKNOWN,
        usage_pct=_percent(usage.get("pct")),
        plan_pct=_percent(usage.get("plan_pct")),
        reset_at=_timestamp(usage.get("reset_at")),
        projected_pct=_percent(usage.get("projected_pct_at_reset")),
        today_finished=_count(today.get("finished")),
        today_passed=_count(today.get("passed")),
        lanes=tuple(Lane(_text(row.get("id")), _text(row.get("title")), _text(row.get("activity")))
                    for row in lanes[:8]) if lanes is not UNKNOWN else UNKNOWN,
        queue_head=tuple(_text(row.get("title")) for row in queue[:5]) if queue is not UNKNOWN else UNKNOWN,
        updated=updated,
        stale=_stale(updated, now),
    )


def _display(value: str | int | float | datetime | None) -> str:
    if value is UNKNOWN:
        return "bilinmiyor"
    if type(value) is datetime:
        return value.isoformat(sep=" ")
    if type(value) is float and value.is_integer():
        return str(int(value))
    return str(value)


def _display_pct(value: int | float | None) -> str:
    return _display(value) if value is UNKNOWN else f"%{_display(value)}"


def to_view(board: Board) -> dict[str, str]:
    """Format the summary as plain Turkish display strings."""
    usage = _display(UNKNOWN)
    if board.usage_pct is not UNKNOWN or board.plan_pct is not UNKNOWN:
        usage = f"Haftalık kota {_display_pct(board.usage_pct)} · plan {_display_pct(board.plan_pct)}"
    return {
        "lanes": f"Hat {_display(board.lanes_running)}/{_display(board.lanes_max)}",
        "waiting_for_salih": _display(board.waiting_for_salih),
        "usage": usage,
        "reset_at": _display(board.reset_at),
        "projected_pct": _display_pct(board.projected_pct),
        "today_finished": _display(board.today_finished),
        "today_passed": _display(board.today_passed),
        "lane_details": "\n".join(
            f"{_display(lane.id)} · {_display(lane.title)} · {_display(lane.doing)}" for lane in board.lanes
        ) if board.lanes is not UNKNOWN else _display(UNKNOWN),
        "queue_head": "\n".join(map(_display, board.queue_head)) if board.queue_head is not UNKNOWN else _display(UNKNOWN),
        "updated": _display(board.updated),
        "stale": "güncel değil" if board.stale else "güncel",
    }
