"""STFU mode state - in-memory per-chat toggle."""

_stfu_mode: dict[int, bool] = {}


def is_stfu(chat_id: int) -> bool:
    return _stfu_mode.get(chat_id, False)


def set_stfu(chat_id: int, enabled: bool) -> None:
    _stfu_mode[chat_id] = enabled


def toggle_stfu(chat_id: int) -> bool:
    new_state = not is_stfu(chat_id)
    set_stfu(chat_id, new_state)
    return new_state
