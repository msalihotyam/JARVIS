"""Plan outbound conversation actions from log entries without performing I/O."""

_VIEWS = ("pc", "telegram")
_KINDS = ("text", "voice", "image", "file", "transcript", "approval_card")
_LABELS = {"pc": "Sen (PC): ", "telegram": "Telefon: "}
_TEXT_LIMIT = 4000


def _text_parts(text, label):
    if len(label) + len(text) <= _TEXT_LIMIT:
        return [label + text]

    digits = 1
    while True:
        # Reserve enough room for both numbers on every part, including the label.
        capacity = _TEXT_LIMIT - len(label) - (2 * digits + 4)
        parts = []
        start = 0
        while len(text) - start > capacity:
            end = start + capacity
            paragraph_end = max(
                (boundary + len(separator)
                 for separator in ("\n\n", "\r\n\r\n")
                 if (boundary := text.rfind(separator, start, end)) != -1),
                default=end,
            )
            parts.append(text[start:paragraph_end])
            start = paragraph_end
        parts.append(text[start:])

        total = len(parts)
        if len(str(total)) <= digits:
            return [f"{label}({index}/{total}) {part}"
                    for index, part in enumerate(parts, 1)]
        digits = len(str(total))


def plan_mirror(entry: dict, *, audio=None) -> list[dict]:
    """Return action dictionaries in view order and then in message part order.

    `delivered` maps views to booleans, with absent views pending. For a JARVIS
    voice reply, `source` is the request view and `audio` is an optional opaque
    payload for that view. System entries have no outbound route.
    """
    source, who, kind, text = (entry[key] for key in ("source", "who", "kind", "text"))
    needs_you = entry.get("needs_you", False)
    delivered = entry.get("delivered", {})
    if source not in _VIEWS:
        raise ValueError("Unknown entry source")
    if kind not in _KINDS:
        raise ValueError("Unknown entry kind")
    if who not in ("salih", "jarvis", "system"):
        raise ValueError("Unknown entry author")
    if not isinstance(needs_you, bool) or (needs_you and who != "jarvis"):
        raise ValueError("Only JARVIS entries may have needs_you set to true")
    if not isinstance(text, str):
        raise ValueError("Entry text must be a string")
    if not isinstance(delivered, dict) or any(
        view not in _VIEWS or not isinstance(sent, bool) for view, sent in delivered.items()
    ):
        raise ValueError("Delivery state must map views to booleans")
    if who == "system":
        return []

    label = _LABELS[source] if who == "salih" else ""
    actions = []
    for view in _VIEWS:
        if delivered.get(view) or (who == "salih" and view == source):
            continue
        for index, part in enumerate(_text_parts(text, label)):
            action = {
                "entry_id": entry["id"],
                "view": view,
                "kind": "text",
                "text": part,
                "silent": view == "telegram" and not needs_you,
            }
            if index == 0 and view == source and who == "jarvis" and kind == "voice" and audio is not None:
                action.update(kind="voice", audio=audio)
            actions.append(action)
    return actions
