import os

import pytest

import llmcopy
from conftest import FAKE_AWS_KEY


@pytest.mark.parametrize("name,why", [
    ("main.py", ""), ("README.md", ""), ("Dockerfile", ""), (".gitignore", ""), (".env.example", ""),
    ("tsconfig.json", ""), ("license_checker.py", ""), ("history.py", ""), ("keyboard.ts", ""),
    ("package-lock.json", "lock file"), ("Cargo.lock", "lock file"), ("yarn.lock", "lock file"),
    ("logo.PNG", "image"), ("icon.svg", "svg"), ("font.woff2", "font"), ("app.min.js", "generated"),
    ("bundle.js.map", "source map"), ("model.safetensors", "binary"), ("data.csv", "data"),
    (".env", "secret"), (".env.production", "secret"), ("server.pem", "secret"), ("id_rsa", "secret"),
    ("LICENSE", "license"), ("LICENSE.txt", "license"), ("LICENSE-MIT.md", "license"),
    ("CHANGELOG.md", "changelog"), ("debug.log", "log"), (".llmcopy", "llmcopy settings"),
    ("messages.po", "translation"), ("api_pb2.py", "generated"),
])
def test_name_reason(name, why):
    assert llmcopy.name_reason(name) == why


def test_secret_detection():
    find = llmcopy.find_secret
    assert find(b"x = 1\n") == ""
    assert find(("a\nkey = '%s'\n" % FAKE_AWS_KEY).encode()) == "AWS access key, line 2"
    assert find(b"-----BEGIN RSA " + b"PRIVATE KEY-----\nMIIE\n").startswith("private key")  # split: not a real key
    assert find(b"-----BEGIN PUBLIC KEY-----\n") == ""
    assert find(b"token = 'ghp_" + b"a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + b"'") .startswith("GitHub token")
    # documentation placeholders and ordinary words are not secrets
    assert find(b"AWS_KEY=AKIAIOSFODNN7EXAMPLE") == ""
    assert find(b"the task-runner and the disk-cache use sk-learn") == ""
    assert find(b"OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") == ""
    assert find(b"OPENAI_API_KEY=sk-proj-" + b"Zq8Lw3Xn5Vb7Ty2Re9Ui4Op6As1Df0Gh" + b"\n").startswith("API key")


def test_token_estimate_is_in_the_right_range():
    # exact o200k_base counts for these snippets, recorded with tiktoken
    samples = [
        ("def add(a, b):\n    return a + b\n" * 40, 480),
        ("The quick brown fox jumps over the lazy dog. " * 50, 501),
        ('{"id": 1234, "name": "widget", "tags": ["a", "b"]}\n' * 30, 660),
    ]
    for text, exact in samples:
        est = llmcopy.estimate_tokens(text.encode())
        assert 0.7 * exact < est < 1.3 * exact, (est, exact)
    assert llmcopy.estimate_tokens(b"") == 0
    assert llmcopy.estimate_tokens(b"x") == 1


def test_formatting():
    f = llmcopy.fmt_tokens
    assert [f(0), f(999), f(1000), f(84_230), f(99_960), f(123_456), f(1_250_000)] == ["0", "999", "1.0k", "84.2k", "100k", "123k", "1.25M"]
    p = llmcopy.parse_tokens
    assert [p("100k"), p("1m"), p("120000"), p("1.5K"), p("50_000")] == [100_000, 1_000_000, 120_000, 1500, 50_000]
    with pytest.raises(ValueError):
        p("lots")
    assert llmcopy._clip("abcdefgh", 5) == "abcd…"
    assert llmcopy._width("日本") == 4
    assert llmcopy._clip("日本語日本", 5) == "日本…"
    assert llmcopy._bar(0.5, 8) == "█" * 4 + " " * 4
    assert llmcopy._bar(2.0, 4) == "█" * 4
    assert llmcopy._bar(0.01, 8) == " " * 8


def test_posix_mouse_reports():
    term = object.__new__(llmcopy._PosixTerm)  # the parser alone: no terminal needed
    term.pending = (b"\x1b[<0;80;3M" b"\x1b[<32;80;5M" b"\x1b[<0;80;5m"  # left button: press, move while held, release
                    b"\x1b[<65;4;2M" b"\x1b[<64;4;2M"  # wheel down, up
                    b"\x1b[<2;9;9M" b"\x1b[<34;9;8M")  # the right button does nothing, held and moved neither
    events = []
    term._parse(events)
    assert events == [("click", 79, 2), ("drag", 79, 4), ("wheel", 1, 3, 1), ("wheel", -1, 3, 1)]
    assert "\x1b[?1002h" in term.on and "\x1b[?1002l" in term.off  # moves are reported only while a button is held


@pytest.mark.skipif(os.name == "nt", reason="POSIX clipboard tools")
@pytest.mark.parametrize("platform,tool,env", [
    ("darwin", "pbcopy", {}),
    ("linux", "wl-copy", {"WAYLAND_DISPLAY": "wayland-0"}),
    ("linux", "xclip", {"DISPLAY": ":0"}),
    ("linux", "xsel", {"DISPLAY": ":0"}),
    ("linux", None, {"DISPLAY": ":0"}),  # a desktop without any tool: say what to install
])
def test_posix_clipboard_tools(tmp_path, monkeypatch, platform, tool, env):
    import sys
    text = "héllo 日本\n"
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "uname", lambda: os.uname_result(("Linux", "x", "6.1-generic", "x", "x")))  # not WSL
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
    if tool is None:
        with pytest.raises(OSError, match="install"):
            llmcopy.copy_to_clipboard(text)
        return
    fake = tmp_path / tool  # stands in for the real tool: keeps what it is given
    fake.write_text("#!/bin/sh\ncat > '%s'\n" % (tmp_path / "pasted"))
    fake.chmod(0o755)
    assert llmcopy.copy_to_clipboard(text) == "clipboard"
    assert (tmp_path / "pasted").read_bytes() == text.encode("utf-8")


def test_read_file(tmp_path):
    big = tmp_path / "big.bin"
    data = bytes(range(256)) * 3000 + b"tail"  # 768 KiB + 4: three full chunks and a short one
    big.write_bytes(data)
    assert llmcopy.read_file(str(big)) == data
    part = llmcopy.read_file(str(big), 100_000)  # "more than the limit" is all the caller needs to know
    assert 100_000 < len(part) < len(data) and data.startswith(part)
    exact = tmp_path / "exact.bin"
    exact.write_bytes(b"x" * llmcopy._CHUNK)
    assert llmcopy.read_file(str(exact)) == b"x" * llmcopy._CHUNK
    (tmp_path / "empty").write_bytes(b"")
    assert llmcopy.read_file(str(tmp_path / "empty")) == b""
    assert llmcopy.read_file(str(tmp_path / "missing")) is None
    assert llmcopy.read_file(str(tmp_path)) is None  # a folder


def test_decode_text():
    d = llmcopy.decode_text
    assert d(b"\xef\xbb\xbfhi\r\nthere\r\n") == "hi\nthere\n"
    assert d("﻿wide".encode("utf-16-le")) == "wide"
    assert d(b"caf\xe9") == "café"  # legacy Windows-1252 file


def test_cli_writes_a_file(tmp_path, capsys):
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.py").write_text("print('hi')\n")
    (root / "b.md").write_text("# doc\n")
    out = tmp_path / "out.txt"
    assert llmcopy.main([str(root), "-y", "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert '<file path="a.py">\nprint(\'hi\')\n</file>' in text
    err = capsys.readouterr().err
    assert "Wrote 2 files" in err and "\x1b" not in err


def test_cli_empty_folder(tmp_path, capsys):
    assert llmcopy.main([str(tmp_path), "-y", "-o", str(tmp_path / "o.txt")]) == 1
    assert "nothing to copy" in capsys.readouterr().err


def test_cli_without_a_terminal_refuses_to_guess(tmp_path, capsys, monkeypatch):
    (tmp_path / "a.txt").write_text("hello\n")

    class Tty:
        def __init__(self, stream, tty):
            self.stream, self.tty = stream, tty

        def isatty(self):
            return self.tty

        def __getattr__(self, name):
            return getattr(self.stream, name)

    import sys
    # a screen but no keyboard: say so, copy nothing
    monkeypatch.setattr(sys, "stdout", Tty(sys.stdout, True))
    monkeypatch.setattr(sys, "stdin", Tty(sys.stdin, False))
    assert llmcopy.main([str(tmp_path)]) == 2
    assert "needs a terminal" in capsys.readouterr().err


def test_cli_piped_output_is_the_text(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello\n")
    assert llmcopy.main([str(tmp_path)]) == 0  # under pytest stdout is not a terminal
    assert "<file path=\"a.txt\">" in capsys.readouterr().out


def test_cli_stdout(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello\n")
    assert llmcopy.main([str(tmp_path), "-o", "-"]) == 0
    cap = capsys.readouterr()
    assert "<file path=\"a.txt\">\nhello\n</file>" in cap.out


def test_budget_is_remembered(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert llmcopy.saved_budget() == 0
    llmcopy.remember_budget(200_000)
    assert llmcopy.saved_budget() == 200_000
    assert os.path.exists(llmcopy._config_file())
