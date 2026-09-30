import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llmcopy  # noqa: E402

FAKE_AWS_KEY = "AKIA" + "Q7XW3ZP5" + "RL2MD8TV"  # assembled so that secret scanners skip this test file


def write(root, rel, content):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8", "newline": "\n"}
    with open(path, mode, **kwargs) as f:
        f.write(content)


def make_project(root, git):
    files = {
        ".gitignore": "build/\nnode_modules/\n*.log\n.env\nlocal.json\n",
        "README.md": "# Demo\n\nA small project.\n",
        "LICENSE": "MIT License\n",
        "package-lock.json": '{"lockfileVersion": 3}\n',
        "src/app.py": "import util\n\n\ndef main():\n    return util.helper(1)\n",
        "src/util.py": "def helper(x):\n    return x + 1\n",
        "src/deep/mod.py": "VALUE = 42\n",
        "src/__pycache__/app.cpython-313.pyc": b"\x00\x01\x02\x03binary",
        "src/config.py": 'KEY = "%s"\n' % FAKE_AWS_KEY,
        "build/out.js": "console.log('built');\n",
        "build/node_modules/x/index.js": "module.exports = 1;\n",
        "node_modules/pkg/index.js": "module.exports = {};\n",
        "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00",
        "blob.xyz": b"abc\x00def",
        "big.txt": "line of text\n" * 30000,
        ".env": "PASSWORD=hunter2\n",
        "local.json": '{"mine": true}\n',
        "debug.log": "log\n",
        "notes/utf16.txt": "﻿hello wide world\n".encode("utf-16-le"),
    }
    for rel, content in files.items():
        write(root, rel, content)
    if git:
        subprocess.run(["git", "init", "-q", root], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return files


def scan(root, rules=None, reveal=False):
    root = str(root).replace("\\", "/")
    return settle(llmcopy.Model(root, llmcopy.load_rules(root) if rules is None else rules, reveal=reveal))


def settle(model):
    model.finish()
    return model


def selected(model):
    return sorted(f.rel for f in model.files_under(model.root) if f.sel)


def node(model, rel):
    cur = model.root
    parts = rel.rstrip("/").split("/")
    for i, part in enumerate(parts):
        for k in cur.kids:
            if k.name == part:
                cur = k
                break
        else:
            raise KeyError(rel)
    return cur


def has(model, rel):
    try:
        node(model, rel)
        return True
    except KeyError:
        return False


def check_totals(model):
    """The incrementally maintained folder totals must equal a from-scratch recount."""
    def walk(d):
        total = (0, 0, 0, 0, 0, 0)
        for k in d.kids:
            c = llmcopy._contrib(k) if k.kids is None else walk(k)
            total = tuple(x + y for x, y in zip(total, c))
        assert (d.n, d.nsel, d.ttot, d.tsel, d.nv, d.nheld) == total, d.rel
        return total

    walk(model.root)


@pytest.fixture(params=["git", "plain"])
def project(request, tmp_path):
    root = str(tmp_path / "proj").replace("\\", "/")
    make_project(root, git=request.param == "git")
    return root, request.param == "git"


@pytest.fixture
def git_project(tmp_path):
    root = str(tmp_path / "proj").replace("\\", "/")
    make_project(root, git=True)
    return root
