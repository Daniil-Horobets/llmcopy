"""The interface without a terminal: feed events, look at the frames it would draw."""
import os
import re

import pytest

import llmcopy
from conftest import check_totals, node, scan, selected

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class FakeTerm:
    on = off = ""

    def __init__(self, events=()):
        self.events = list(events)

    def wake(self):
        pass

    def read(self, timeout):
        if self.events:
            return [self.events.pop(0)]
        return [("key", "ctrl-c")]  # script is over: leave


def make_ui(root, monkeypatch, size=(100, 30), events=(), budget=100_000, **kw):
    monkeypatch.setattr(os, "get_terminal_size", lambda *_: os.terminal_size(size))
    m = scan(root, reveal=True)
    ui = llmcopy.UI(m, FakeTerm(events), budget, **kw)
    ui.resize()
    ui.settle()
    return ui


def screen(ui):
    return [ANSI.sub("", line) for line in ui.frame()]


def keys(*names):
    return [("char", n[2:]) if n.startswith("c:") else ("key", n) for n in names]


def test_first_frame(git_project, monkeypatch):
    ui = make_ui(git_project, monkeypatch)
    lines = screen(ui)
    assert len(lines) == 30
    assert lines[0].startswith(" llmcopy  proj") and "/ 100k tokens" in lines[0]
    assert lines[1].startswith(" [-] \u25bc proj/") or lines[1].startswith(" [x] \u25bc proj/")
    assert any("src/" in line for line in lines)
    assert "enter copy" in lines[-1] and "q quit" in lines[-1]
    # the held-back secret is on screen; what is left out is listed too, until `i` hides it
    text = "\n".join(lines)
    assert "config.py" in text and "secret: AWS access key" in text
    assert "node_modules/" in text and "package-lock.json" in text and "lock file" in text
    assert "a all" in lines[-1] and "i ignored" in lines[-1]
    ui.handle(("char", "i"))
    text = "\n".join(screen(ui))
    assert "node_modules" not in text and "package-lock.json" not in text and "config.py" in text
    ui.handle(("char", "i"))
    assert "node_modules/" in "\n".join(screen(ui))


@pytest.mark.parametrize("width", [24, 40, 59, 60, 75, 76, 80, 100, 160])
def test_no_line_is_wider_than_the_terminal(git_project, monkeypatch, width):
    ui = make_ui(git_project, monkeypatch, size=(width, 20))
    for line in screen(ui):
        assert llmcopy._width(line) <= width, (width, line)
    ui.act("filter")
    ui.on_char("s")
    for line in screen(ui):
        assert llmcopy._width(line) <= width, (width, line)


def test_keys_move_select_and_copy(git_project, monkeypatch, capsys):
    ui = make_ui(git_project, monkeypatch)
    downs = next(i for i, line in enumerate(screen(ui)) if " src/" in line) - 1
    ui.term.events = keys(*["down"] * downs, "space", "enter")
    assert ui.run() == "copy"
    assert selected(ui.m) == [".gitignore", "README.md", "notes/utf16.txt"]
    check_totals(ui.m)
    out = capsys.readouterr().out
    assert "\x1b[?1049h" in out and out.rstrip().endswith("\x1b[?1049l")  # screen restored on the way out


def test_quit_keys(git_project, monkeypatch, capsys):
    for ev in (("char", "q"), ("key", "esc"), ("key", "ctrl-c")):
        ui = make_ui(git_project, monkeypatch, events=[ev])
        assert ui.run() == "quit"


def test_esc_does_not_discard_a_changed_selection_by_accident(git_project, monkeypatch):
    ui = make_ui(git_project, monkeypatch)
    ui.handle(("key", "space"))
    ui.handle(("key", "esc"))
    assert ui.result is None and "Esc again" in screen(ui)[-2]
    ui.handle(("key", "down"))  # anything else disarms it
    ui.handle(("key", "esc"))
    assert ui.result is None
    ui.handle(("key", "esc"))
    assert ui.result == "quit"


def test_enter_with_nothing_selected_does_not_leave(git_project, monkeypatch, capsys):
    ui = make_ui(git_project, monkeypatch, events=keys("c:a", "enter", "c:q"))
    assert ui.run() == "quit"
    assert selected(ui.m) == []


def test_filter_flow(git_project, monkeypatch):
    ui = make_ui(git_project, monkeypatch)
    for ev in keys("c:a", "c:/", "c:u", "c:t", "c:i", "c:l"):
        ui.handle(ev)
    lines = screen(ui)
    assert "filter: util" in lines[-2] and "1 file matches" in lines[-2]
    rows = [line for line in lines[1:-2] if line.strip()]
    assert len(rows) == 3 and "util.py" in rows[-1]  # root, src/, the match
    for ev in keys("enter", "c:a"):
        ui.handle(ev)
    assert selected(ui.m) == ["src/util.py"]
    ui.handle(("key", "esc"))
    assert ui.filter == "" and len(ui.rows) > 3 or screen(ui)
    assert ui.result is None  # esc cleared the filter, it did not quit


def test_mouse(git_project, monkeypatch):
    ui = make_ui(git_project, monkeypatch)
    lines = screen(ui)
    y = next(i for i, line in enumerate(lines) if "README.md" in line)
    ui.handle(("click", 20, y))  # anywhere on a file row
    assert "README.md" not in selected(ui.m)
    y = next(i for i, line in enumerate(lines) if " src/" in line)
    was_open = node(ui.m, "src/").open
    ui.handle(("click", 12, y))  # a folder name folds/unfolds
    assert node(ui.m, "src/").open != was_open
    ui.handle(("click", 2, y))  # its box selects
    assert not any(r.startswith("src/") for r in selected(ui.m))
    lines = screen(ui)
    x = lines[-1].index("q quit")
    ui.handle(("click", x + 1, len(lines) - 1))  # footer hints are buttons
    assert ui.result == "quit"


def test_wheel_scrolls(git_project, monkeypatch):
    ui = make_ui(git_project, monkeypatch, size=(80, 8))
    screen(ui)
    top = ui.top
    ui.handle(("wheel", 1, 5, 5))
    screen(ui)
    assert ui.top > top
    assert ui.top <= ui.cursor_index() < ui.top + ui.body_height()


def test_over_budget_focuses_the_heaviest_folder(tmp_path, monkeypatch):
    root = str(tmp_path / "p").replace("\\", "/")
    from conftest import write
    write(root, "small.py", "x = 1\n")
    write(root, "lib/a.py", "y = 2\n" * 50)
    write(root, "lib/data/big.json", '{"key": "value", "n": 12345}\n' * 900)
    write(root, "lib/data/more.json", '{"key": "value", "n": 12345}\n' * 300)
    monkeypatch.setattr(os, "get_terminal_size", lambda *_: os.terminal_size((100, 30)))
    m = scan(root)
    ui = llmcopy.UI(m, FakeTerm(), budget=5000)
    ui.resize()
    ui.settle()
    lines = screen(ui)
    assert m.sort_size and ui.cur is node(m, "lib/data/")
    assert "over budget" in lines[-2]
    assert lines[1].lstrip().startswith("[x] \u25bc p/")
    ui.handle(("key", "space"))
    assert m.root.tsel < 5000 and "Fits now" in screen(ui)[-2]


def test_all_none_key(git_project, monkeypatch):
    ui = make_ui(git_project, monkeypatch)
    everything = selected(ui.m)
    ui.handle(("char", "a"))
    assert selected(ui.m) == [] and screen(ui)[1].startswith(" [ ]")
    ui.handle(("char", "a"))
    assert selected(ui.m) == everything  # the defaults again: ignored things stay out
    for name in ("build/", "local.json"):  # ...also those ticked by hand before
        ui.cur = node(ui.m, name)
        ui.handle(("key", "space"))
    ui.m.finish()
    assert "build/out.js" in selected(ui.m) and "local.json" in selected(ui.m)
    ui.handle(("char", "a"))
    assert selected(ui.m) == []
    ui.handle(("char", "a"))
    assert selected(ui.m) == everything
    ui.handle(("char", "i"))
    assert "build/" not in "\n".join(screen(ui))


def test_budget_cycles_and_is_remembered(git_project, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    ui = make_ui(git_project, monkeypatch)
    ui.handle(("char", "b"))
    assert ui.budget == 200_000 and llmcopy.saved_budget() == 200_000
    assert "/ 200k tokens" in screen(ui)[0]
    for _ in range(5):
        ui.handle(("char", "b"))
    assert ui.budget == 100_000


def test_save_key(git_project, monkeypatch):
    ui = make_ui(git_project, monkeypatch)
    ui.cur = node(ui.m, "README.md")
    ui.handle(("key", "space"))
    ui.handle(("char", "s"))
    assert llmcopy.load_rules(git_project) == {"README.md": False}
    assert "Saved" in screen(ui)[-2]
    ui2 = make_ui(git_project, monkeypatch)
    assert "saved selection" in screen(ui2)[-2]


def test_no_color(git_project, monkeypatch):
    ui = make_ui(git_project, monkeypatch, color=False)
    raw = "\n".join(ui.frame())
    assert "\x1b[7m" in raw  # the cursor bar stays
    assert not re.search(r"\x1b\[[0-9;]*3[0-9]", raw)  # no hues
