"""gui_menu._ensure_target_unlocked() -- преполётная проверка LOCK перед реальной сборкой в
GUI. Живой LOCK (владелец жив ИЛИ неизвестен) движок иначе превратил бы в тихий возврат в
меню -- теперь пользователь видит окно и решает. LOCK мёртвого процесса движок снимает сам,
до окна не доходит.

tkinter на этой (Linux) машине отсутствует -- _confirm_clear_lock() и inspect/clear
monkeypatch'атся, живой диалог тут не поднимается (тот же приём, что и в test_gui_menu.py)."""
import gui_menu as g


def _patch(monkeypatch, lock_state, answer=None):
    calls = {"confirm": 0, "clear": 0}

    monkeypatch.setattr(g.m, "inspect_target_lock", lambda target: lock_state)

    def _fake_clear(target, log=print):
        calls["clear"] += 1
        return True

    monkeypatch.setattr(g.m, "clear_target_lock", _fake_clear)

    def _fake_confirm(message):
        calls["confirm"] += 1
        return answer

    monkeypatch.setattr(g, "_confirm_clear_lock", _fake_confirm)
    return calls


def test_no_lock_proceeds_without_dialog(monkeypatch):
    calls = _patch(monkeypatch, None)
    assert g._ensure_target_unlocked("D:/__PhotoArchive__", log=lambda *a: None) is True
    assert calls["confirm"] == 0 and calls["clear"] == 0


def test_dead_holder_proceeds_without_dialog(monkeypatch):
    """Движок (TargetLock.__enter__) снимет такой LOCK сам -- GUI не должен спрашивать."""
    calls = _patch(monkeypatch, {"pid": 111, "pid_alive": False, "age_seconds": 10})
    assert g._ensure_target_unlocked("D:/__PhotoArchive__", log=lambda *a: None) is True
    assert calls["confirm"] == 0 and calls["clear"] == 0


def test_live_holder_user_confirms_clears_and_proceeds(monkeypatch):
    calls = _patch(monkeypatch, {"pid": 222, "pid_alive": True, "age_seconds": 90}, answer=True)
    assert g._ensure_target_unlocked("D:/__PhotoArchive__", log=lambda *a: None) is True
    assert calls["confirm"] == 1 and calls["clear"] == 1


def test_live_holder_user_declines_aborts(monkeypatch):
    calls = _patch(monkeypatch, {"pid": 222, "pid_alive": True, "age_seconds": 90}, answer=False)
    assert g._ensure_target_unlocked("D:/__PhotoArchive__", log=lambda *a: None) is False
    assert calls["confirm"] == 1 and calls["clear"] == 0


def test_unknown_holder_prompts_too(monkeypatch):
    calls = _patch(monkeypatch, {"pid": None, "pid_alive": None, "age_seconds": 30}, answer=True)
    assert g._ensure_target_unlocked("D:/__PhotoArchive__", log=lambda *a: None) is True
    assert calls["confirm"] == 1 and calls["clear"] == 1
