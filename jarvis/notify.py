"""Plain notification data and replay-safe writes; no UI or database access."""

from collections.abc import Callable, Iterable
from datetime import datetime
import hashlib
import json

from jarvis.work_ledger import EventKind, WorkEvent, event_payload_json


# Progress has two distinct points, sharing the ledger's progress event kind.
_POINTS = {
    "questions_ready": ("normal", "Sorular hazır", "Proje sorularını yanıtlayın."),
    "readback_ready": ("normal", "Özet hazır", "Anladığım özeti onaylayın."),
    "plan_ready": ("high", "Plan hazır", "Proje planını onaylayın."),
    "needs_input": ("high", "Bilgi gerekiyor", "Devam etmek için yanıtınız gerekiyor."),
    "blocked:budget": ("high", "Bütçe doldu", "Bütçeyi artırın veya çalışmayı durdurun."),
    "slice_failed_after_cap": ("high", "Deneme sınırına ulaşıldı", "Parça için sonraki adımı seçin."),
    "approval_required": ("high", "Onay gerekiyor", "İşlemi inceleyip onaylayın."),
    "awaiting_acceptance": ("normal", "Kontrolünüz bekleniyor", "Sonucu kontrol edin."),
    "done": ("normal", "Proje tamamlandı", "Sonucu ve kanıtları görüntüleyin."),
    "interrupted": ("normal", "Çalışma kesildi", "Devam etmek için onaylayın."),
    "slice_started": ("low", "Parça başladı", "Proje parçası üzerinde çalışılıyor."),
    "slice_accepted": ("low", "Parça kabul edildi", "Parçanın kontrolleri geçti."),
}


class NotificationCore:
    """Use PM02-compatible callables for atomic event/outbox writes.

    ``events(run_id)`` must return persisted ledger events. ``write`` follows
    Memory.append_work_event's signature and writes both rows atomically.
    Calls must be serialized with other ledger producers by the caller, which
    also owns sequence allocation. Replays return False, even after restart.
    Store failures propagate; no in-memory success cache can hide a retry.
    The caller owns eligibility (large-only/manual-check points).
    """

    def __init__(self, write: Callable[..., bool], events: Callable[[str], Iterable[WorkEvent]]):
        self._write = write
        self._events = events

    def notify(
        self,
        run_id: str,
        point: str,
        dedupe_key: str,
        *,
        seq: int,
        at: datetime,
        importance: str | None = None,
        step_key: str | None = None,
    ) -> bool:
        """Write one event and one outbox row, or collapse a persisted replay.

        Importance defaults to ?2.1; explicit values must be low/normal/high.
        Text is fixed Turkish plain data, never executable markup or UI code.
        """
        if not isinstance(point, str) or point not in _POINTS:
            raise ValueError("unknown notification point")
        default, title, text = _POINTS[point]
        if importance is None:
            importance = default
        if not isinstance(importance, str) or importance not in ("low", "normal", "high"):
            raise ValueError("importance must be low, normal or high")
        if not isinstance(dedupe_key, str) or not dedupe_key.strip():
            raise ValueError("dedupe_key must be non-empty")
        identity = json.dumps([run_id, point, dedupe_key], ensure_ascii=False, separators=(",", ":"))
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        kind = EventKind.PROGRESS if point in ("slice_started", "slice_accepted") else EventKind(point)
        payload = {
            "point": point,
            "dedupe_key": dedupe_key,
            "importance": importance,
            "title": title,
            "text": text,
        }
        event = WorkEvent(run_id, seq, kind, at, step_key, event_payload_json(payload))
        for previous in self._events(run_id):
            stored = json.loads(previous.payload_json)
            if (previous.run_id == run_id and previous.kind == kind
                    and stored.get("point") == point and stored.get("dedupe_key") == dedupe_key):
                return False
        return self._write(event, idempotency_key=key, severity=importance)
