import os
import subprocess
import threading
import time

import llmcopy
from conftest import FAKE_AWS_KEY, check_totals, has, node, scan, selected, settle, write

DEFAULT = [".gitignore", "README.md", "notes/utf16.txt", "src/app.py", "src/deep/mod.py", "src/util.py"]


def default(git):
    # .gitignore is git's business: without a repository only the built-in rules apply,
    # so a file that merely *would* be ignored (local.json) is an ordinary file.
    return DEFAULT if git else sorted(DEFAULT + ["local.json"])


def test_default_selection(project):
    root, git = project
    m = scan(root)
    assert m.git == git
    assert selected(m) == default(git)
    check_totals(m)
    assert m.root.nsel == len(default(git))
    assert m.root.tsel > 0


def test_reasons(project):
    root, git = project
    m = scan(root)
    m.reveal_ignored()
    settle(m)
    why = {f.rel: f.why for f in m.files_under(m.root)}
    assert why["package-lock.json"] == "lock file"
    assert why["logo.png"] == "image"
    assert why["blob.xyz"] == "binary"  # found by content, not by name
    assert why["big.txt"] == "large"
    assert why[".env"] == "secret"
    assert why["src/config.py"] == "secret"
    assert why["LICENSE"] == "license"
    assert why["debug.log"] == "log"
    assert node(m, "src/config.py").note.startswith("AWS access key, line 1")
    # ignored folders are present but not entered
    for rel in ("build/", "node_modules/", "src/__pycache__/"):
        d = node(m, rel)
        assert d.why == "ignored" and d.xcl and not d.on, rel
    assert node(m, "node_modules/").state == llmcopy.LAZY
    assert selected(m) == default(git)
    check_totals(m)


def test_git_does_not_walk_ignored_folders(git_project):
    m = scan(git_project)
    assert not has(m, "node_modules/") and not has(m, "build/") and not has(m, "local.json")
    m = scan(git_project, reveal=True)  # what the picker does: ignored entries are listed, but not entered
    assert node(m, "local.json").why == "ignored"
    assert node(m, "build/").state == llmcopy.LAZY and not has(m, "build/out.js")
    assert selected(m) == DEFAULT
    check_totals(m)


def test_ignored_entries_are_listed_last(git_project, monkeypatch):
    gate = threading.Event()
    list_dir = llmcopy.list_dir
    monkeypatch.setattr(llmcopy, "list_dir", lambda path: gate.wait(10) and list_dir(path))
    m = llmcopy.Model(git_project, {}, reveal=True)
    give_up = time.time() + 10
    while m.counting and time.time() < give_up:
        m.pump()
        time.sleep(0.002)
    # everything is counted while the listings for ignored entries have not even begun
    assert not m.counting and m.busy
    assert selected(m) == DEFAULT and node(m, "src/app.py").measured == 2 and not has(m, "local.json")
    total = m.root.tsel
    gate.set()
    settle(m)
    assert node(m, "local.json").why == "ignored" and has(m, "node_modules/")
    assert m.root.tsel == total
    check_totals(m)


def test_toggle_file_and_folder(project):
    root, git = project
    m = scan(root)
    m.toggle(node(m, "src/app.py"))
    assert "src/app.py" not in selected(m)
    assert m.dir_checked(node(m, "src/")) == 1
    m.toggle(node(m, "src/"))  # partial -> all
    assert m.dir_checked(node(m, "src/")) == 2
    assert [r for r in selected(m) if r.startswith("src/")] == ["src/app.py", "src/deep/mod.py", "src/util.py"]
    m.toggle(node(m, "src/"))  # all -> none
    assert not [r for r in selected(m) if r.startswith("src/")]
    assert m.dir_checked(node(m, "src/")) == 0
    check_totals(m)
    m.toggle(m.root)
    if m.dir_checked(m.root) != 2:
        m.toggle(m.root)
    assert selected(m) == default(git)  # "all" means the defaults, never the junk
    check_totals(m)


def test_excluded_file_can_be_forced(project):
    root, _ = project
    m = scan(root)
    lock = node(m, "package-lock.json")
    assert m.toggle(lock) == ""
    settle(m)
    assert lock.sel and lock.pin and lock.tok > 0
    big = node(m, "big.txt")
    m.toggle(big)
    settle(m)
    assert big.sel and big.measured == 2 and big.tok > 50_000
    assert m.toggle(node(m, "logo.png"))  # a message: binary files cannot be selected
    assert not node(m, "logo.png").sel
    check_totals(m)


def test_ticking_an_ignored_folder_loads_it(project):
    root, _ = project
    m = scan(root)
    m.reveal_ignored()
    settle(m)
    build = node(m, "build/")
    m.toggle(build)
    settle(m)
    assert "build/out.js" in selected(m)
    assert node(m, "build/out.js").tok > 0
    # a dependency folder nested inside stays out
    assert not any(r.startswith("build/node_modules/") for r in selected(m))
    assert m.dir_checked(build) == 2
    check_totals(m)
    m.toggle(build)
    assert "build/out.js" not in selected(m)
    check_totals(m)


def test_looking_into_an_ignored_folder_selects_nothing(project):
    root, git = project
    m = scan(root)
    m.reveal_ignored()
    settle(m)
    nm = node(m, "node_modules/")
    m.expand(nm)
    settle(m)
    assert node(m, "node_modules/pkg/").why == llmcopy.INHERITED
    assert selected(m) == default(git)
    m.expand(node(m, "node_modules/pkg/"))
    settle(m)
    f = node(m, "node_modules/pkg/index.js")
    assert f.why == llmcopy.INHERITED and not f.sel
    m.toggle(f)
    settle(m)
    assert "node_modules/pkg/index.js" in selected(m)
    check_totals(m)


def test_secret_in_content_is_held_back_but_can_be_forced(project):
    root, _ = project
    m = scan(root)
    cfg = node(m, "src/config.py")
    assert not cfg.sel and cfg.why == "secret"
    text, info = llmcopy.build_output(m)
    assert FAKE_AWS_KEY not in text
    assert info["held"] == [("src/config.py", cfg.note)]
    assert "config.py  (omitted)" in text
    m.toggle(cfg)
    text, info = llmcopy.build_output(m)
    assert FAKE_AWS_KEY in text and not info["held"]


def test_rules_roundtrip(project):
    root, _ = project
    m = scan(root)
    m.reveal_ignored()
    settle(m)
    m.toggle(node(m, "src/deep/"))          # drop a folder
    m.toggle(node(m, "README.md"))          # drop a file
    m.toggle(node(m, "package-lock.json"))  # add an excluded file
    m.toggle(node(m, "build/"))             # add an ignored folder
    settle(m)
    before = selected(m)
    assert m.save_rules() == 4
    assert llmcopy.load_rules(root) == {"README.md": False, "build/": True, "package-lock.json": True, "src/deep/": False}
    m2 = scan(root)
    assert selected(m2) == before
    check_totals(m2)
    assert m2.collect_rules() == m.collect_rules()
    # a new file in a dropped folder stays dropped, a new file elsewhere is picked up
    write(root, "src/deep/new.py", "x = 1\n")
    write(root, "src/fresh.py", "y = 2\n")
    m3 = scan(root)
    assert "src/fresh.py" in selected(m3) and "src/deep/new.py" not in selected(m3)


def test_rule_for_a_file_inside_an_ignored_folder(project):
    root, git = project
    write(root, llmcopy.RULES_FILE, "+ node_modules/pkg/index.js\n+ build/out.js\n- README.md\n")
    m = scan(root)
    assert "node_modules/pkg/index.js" in selected(m)
    assert "build/out.js" in selected(m)
    assert "README.md" not in selected(m)
    assert node(m, "node_modules/pkg/index.js").tok > 0
    check_totals(m)
    assert sorted(m.collect_rules()) == [("+", "build/out.js"), ("+", "node_modules/pkg/index.js"), ("-", "README.md")]


def test_saving_defaults_removes_the_rules_file(project):
    root, _ = project
    write(root, llmcopy.RULES_FILE, "- README.md\n")
    m = scan(root)
    assert "README.md" not in selected(m)
    m.toggle(node(m, "README.md"))
    assert m.save_rules() == 0 and not os.path.exists(os.path.join(root, llmcopy.RULES_FILE))


def test_none_then_one_file(project):
    root, _ = project
    m = scan(root)
    m.toggle(m.root)
    assert selected(m) == []
    m.toggle(node(m, "src/util.py"))
    m.save_rules()
    m2 = scan(root)
    assert selected(m2) == ["src/util.py"]
    write(root, "later.py", "z = 3\n")
    assert selected(scan(root)) == ["src/util.py"]  # everything is off by default now


def test_all_is_everything_but_the_ignored_and_none_is_none(project):
    root, git = project
    m = scan(root, reveal=True)
    build = node(m, "build/")
    m.toggle(build)  # left-out things ticked by hand: a folder, a file, a file deep in an ignored folder
    m.toggle(node(m, "package-lock.json"))
    m.expand(node(m, "node_modules/"))
    settle(m)
    m.expand(node(m, "node_modules/pkg/"))
    settle(m)
    m.toggle(node(m, "node_modules/pkg/index.js"))
    settle(m)
    by_hand = {"build/out.js", "package-lock.json", "node_modules/pkg/index.js"}
    assert set(selected(m)) == set(default(git)) | by_hand and m.dir_checked(m.root) == 2
    m.toggle(m.root)  # all -> none
    assert selected(m) == []
    assert build.xcl and node(m, "build/out.js").why == llmcopy.INHERITED  # an ignored folder again
    m.toggle(m.root)  # none -> all
    assert selected(m) == default(git)
    assert m.collect_rules() == []
    check_totals(m)
    m.toggle(node(m, "src/app.py"))
    m.toggle(build)
    m.toggle(node(m, "package-lock.json"))
    settle(m)
    m.toggle(m.root)  # some -> all
    assert selected(m) == default(git)
    check_totals(m)


def test_filter_toggle_only_touches_matches(project):
    root, git = project
    m = scan(root)
    m.toggle(m.root, match=lambda f: "util" in f.rel)
    assert "src/util.py" not in selected(m) and "src/app.py" in selected(m)
    m.toggle(m.root, match=lambda f: "util" in f.rel)
    assert selected(m) == default(git)
    check_totals(m)


def test_output_format(git_project):
    m = scan(git_project)
    m.toggle(node(m, "src/deep/"))
    text, info = llmcopy.build_output(m)
    assert info["files"] == 5
    assert text.startswith('Project "proj": 5 files.')
    assert "<tree>\nproj/\n" in text
    assert '    deep/  (omitted)' in text
    assert '<file path="src/app.py">\nimport util\n' in text
    assert "hello wide world" in text  # UTF-16 source decoded
    assert "\r" not in text
    assert text.count("<file path=") == 5 + 1  # + the mention in the first line
    assert info["tokens"] > 0


def test_deleted_tracked_file_and_nested_repo(git_project):
    root = git_project
    subprocess.run(["git", "-C", root, "add", "README.md", "src"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.remove(os.path.join(root, "src", "util.py"))  # still in the index
    write(root, "vendored/lib.py", "a = 1\n")
    subprocess.run(["git", "init", "-q", os.path.join(root, "vendored")], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    m = scan(root)
    assert "src/util.py" not in selected(m)
    assert node(m, "src/util.py").why == "missing"
    assert not any(r.startswith("vendored/") for r in selected(m))
    check_totals(m)
    text, info = llmcopy.build_output(m)
    assert info["files"] == len(selected(m))


def test_repositories_inside_a_plain_folder(tmp_path):
    root = str(tmp_path / "work").replace("\\", "/")
    from conftest import make_project
    make_project(root + "/one", git=True)
    make_project(root + "/two", git=False)
    write(root, "notes.md", "# notes\n")
    m = scan(root)
    assert not m.git and node(m, "one/").git and not node(m, "two/").git
    sel = selected(m)
    assert "notes.md" in sel
    assert "one/src/app.py" in sel and "two/src/app.py" in sel
    assert "one/local.json" not in sel  # one/.gitignore is honoured ...
    assert "two/local.json" in sel      # ... two/ has no repository, so its .gitignore means nothing
    check_totals(m)
    m.reveal_ignored()
    settle(m)
    assert node(m, "one/local.json").why == "ignored"
    assert node(m, "one/node_modules/").state == llmcopy.LAZY
    check_totals(m)


def test_submodule_like_entry_becomes_an_ignored_folder(git_project):
    root = git_project
    write(root, "sub/lib.py", "a = 1\n")
    subprocess.run(["git", "init", "-q", root + "/sub"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    m = scan(root)
    sub = node(m, "sub/")
    assert sub.kids is not None and sub.why == "ignored" and sub.state == llmcopy.LAZY
    m.toggle(sub)  # ticking it asks *its* git
    settle(m)
    assert "sub/lib.py" in selected(m)
    check_totals(m)


def test_real_submodule_entry(git_project):
    root = git_project

    def git(*args, cwd=root):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    write(root, "dep/lib.py", "a = 1\n")
    git("init", "-q", cwd=root + "/dep")
    git("add", "lib.py", cwd=root + "/dep")
    git("commit", "-q", "-m", "init", cwd=root + "/dep")
    sha = git("rev-parse", "HEAD", cwd=root + "/dep")
    git("update-index", "--add", "--cacheinfo", "160000,%s,dep" % sha)  # what `git submodule add` records
    m = scan(root)
    dep = node(m, "dep/")
    assert dep.kids is not None and dep.why == "ignored" and not any(r.startswith("dep") for r in selected(m))
    check_totals(m)
    m.toggle(dep)
    settle(m)
    assert "dep/lib.py" in selected(m)
    check_totals(m)


def test_run_from_a_subfolder_of_a_repo(git_project):
    m = scan(git_project + "/src")
    assert m.git
    assert selected(m) == ["app.py", "deep/mod.py", "util.py"]


def test_virtualenv_with_an_unusual_name(tmp_path):
    root = str(tmp_path / "p").replace("\\", "/")
    write(root, "main.py", "print(1)\n")
    write(root, "myenv/pyvenv.cfg", "home = /usr\n")
    write(root, "myenv/lib/site.py", "x = 1\n")
    m = scan(root)
    assert selected(m) == ["main.py"]
    assert node(m, "myenv/").why == "ignored"
    check_totals(m)


def test_git_refusing_falls_back_and_says_so(git_project, monkeypatch):
    monkeypatch.setattr(llmcopy, "start_git", lambda root: lambda: None)  # e.g. "dubious ownership", or git missing
    m = scan(git_project)
    assert m.git_failed and not m.git
    assert "local.json" in selected(m) and "src/app.py" in selected(m)  # walked like a plain folder
    check_totals(m)
