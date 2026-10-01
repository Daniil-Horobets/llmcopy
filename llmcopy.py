#!/usr/bin/env python3
"""llmcopy - pick project files in a terminal tree and copy them to the clipboard for an AI chatbot.

One file, standard library only, Python 3.9+. Windows, macOS, Linux. Details: LLMS.md.
"""

import os
import sys
from time import perf_counter

try:
    from _collections import deque  # the C type alone: importing `collections` costs ~4 ms at startup
except ImportError:
    from collections import deque

__version__ = "2.1.0"

_T0 = perf_counter()
DEFAULT_BUDGET = 100_000
BUDGETS = (25_000, 50_000, 100_000, 200_000, 400_000, 1_000_000)
MAX_BYTES = 256 * 1024  # larger text files are left out by default
SNIFF = 8192  # bytes inspected to tell text from binary
RULES_FILE = ".llmcopy"
COLD_THREADS = 16  # readers used when files are slow to open
BATCH, BATCH_SECONDS = 64, 0.004  # scan results reach the interface in batches
FIRST_FRAME, FIRST_FRAME_MAX = 0.09, 0.30  # head start for the scan before the first frame (seconds)


# -- what is left out by default -------------------------------------------
# Everything outside the default selection carries a reason ("why"). It stays
# in the tree, dimmed, and anything that is text can be ticked by hand.

VCS_DIRS = frozenset((".git", ".hg", ".svn", ".jj"))
JUNK_DIRS = frozenset(
    """
    node_modules bower_components jspm_packages .yarn .pnpm-store vendor third_party Pods
    .venv venv __pycache__ .pytest_cache .mypy_cache .ruff_cache .tox .nox .eggs .ipynb_checkpoints htmlcov
    .next .nuxt .svelte-kit .astro .vite .turbo .parcel-cache .angular .expo .cache
    .gradle .dart_tool .terraform .serverless DerivedData
    .idea .vscode .vs .claude .cursor
    """.split()
)
MAYBE_JUNK_DIRS = frozenset("dist build out target obj coverage tmp temp env _build".split())  # only without git

INHERITED = "in ignored folder"
NOT_TEXT = frozenset(("binary", "image", "font", "archive", "media", "document", "missing", "unreadable"))

_NAME_WHY = {RULES_FILE: "llmcopy settings"}
_EXT_WHY = {}
for _why, _names in (
    ("lock file", "package-lock.json npm-shrinkwrap.json yarn.lock pnpm-lock.yaml bun.lock bun.lockb poetry.lock "
                  "pipfile.lock uv.lock pdm.lock cargo.lock composer.lock gemfile.lock go.sum pubspec.lock podfile.lock "
                  "packages.lock.json flake.lock mix.lock gradle.lockfile"),
    ("junk", ".ds_store thumbs.db desktop.ini .coverage"),
    ("secret", ".netrc .pypirc .htpasswd id_rsa id_dsa id_ecdsa id_ed25519 secrets.yaml secrets.yml secrets.json secrets.toml"),
):
    for _n in _names.split():
        _NAME_WHY[_n] = _why
for _why, _names in (
    ("image", "png jpg jpeg gif bmp ico webp avif tif tiff psd heic icns"),
    ("svg", "svg"),
    ("font", "woff woff2 ttf otf eot"),
    ("archive", "zip gz tgz tar bz2 xz 7z rar jar war ear whl egg apk aab ipa dmg iso deb rpm"),
    ("media", "mp3 mp4 m4a mov avi mkv webm wav ogg flac aac wmv"),
    ("document", "pdf doc docx xls xlsx ppt pptx odt ods odp"),
    ("binary", "exe dll so dylib a o obj lib class pyc pyo pyd wasm bin dat pdb node sqlite sqlite3 db parquet pkl "
               "pickle npy npz h5 hdf5 onnx pt pth safetensors ckpt gguf mo"),
    ("data", "csv tsv jsonl ndjson"),
    ("source map", "map"),
    ("log", "log"),
    ("lock file", "lock"),
    ("snapshot", "snap"),
    ("translation", "po pot"),
    ("secret", "pem key p12 pfx jks keystore kdbx tfstate"),
):
    for _n in _names.split():
        _EXT_WHY["." + _n] = _why
del _why, _names, _n

_GENERATED = (".min.js", ".min.css", ".bundle.js", ".pb.go", "_pb2.py", "_pb2_grpc.py", ".g.dart", ".freezed.dart",
              ".generated.ts", ".generated.cs", ".designer.cs")
_ENV_OK = (".example", ".sample", ".template", ".dist", ".defaults", ".tpl")
_DOC_EXT = ("", ".md", ".txt", ".rst")


def name_reason(name):
    """Why a file with this name is not part of the default selection ('' = it is)."""
    low = name.lower()
    why = _NAME_WHY.get(low)
    if why:
        return why
    dot = low.rfind(".")
    ext, stem = (low[dot:], low[:dot]) if dot > 0 else ("", low)
    if ext in _EXT_WHY:
        return _EXT_WHY[ext]
    if low.endswith(_GENERATED):
        return "generated"
    if stem in ("license", "licence", "copying", "unlicense") or (ext in _DOC_EXT and stem.startswith(("license", "licence"))):
        return "license"
    if ext in _DOC_EXT and stem in ("changelog", "changes", "history", "release-notes", "releases", "news"):
        return "changelog"
    if low.startswith(".env") and not low.endswith(_ENV_OK):
        return "secret"
    return ""


# -- token estimate ----------------------------------------------------------
# A linear model over ten byte-level counts, each one C-speed pass, fitted to
# OpenAI's o200k_base: ~4% off per project, ~4x faster than tiktoken, no dependency.


def _table(pred, ch):
    return bytes((ch if pred(c) else 32) for c in range(256))


_PUNCT = frozenset(c for c in range(33, 127) if not (chr(c).isalnum() or c == 95))
_T_ALPHA = _table(lambda c: 65 <= c <= 90 or 97 <= c <= 122, 97)
_T_PUNCT = _table(lambda c: c in _PUNCT, 112)
_T_CASE = bytes((108 if 97 <= c <= 122 else 85 if 65 <= c <= 90 else 32) for c in range(256))
_HIGH = bytes(range(128, 256))
# words, camelCase humps, digits, punctuation, punctuation runs, lines, indented lines, non-ASCII bytes, letters, underscores
_COEF = (1.0822, 0.8878, 0.6285, 0.1115, 0.7216, 0.7330, 0.6284, 0.1697, -0.0135, 0.3977)


def token_features(b):
    n = len(b)
    a = b.translate(_T_ALPHA)
    p = b.translate(_T_PUNCT)
    return (
        a.count(b" a") + (a[:1] == b"a"),
        b.translate(_T_CASE).count(b"lU"),
        n - len(b.translate(None, b"0123456789")),
        n - p.count(b" "),
        p.count(b" p") + (p[:1] == b"p"),
        b.count(b"\n"),
        b.count(b"\n ") + b.count(b"\n\t"),
        0 if b.isascii() else n - len(b.translate(None, _HIGH)),
        n - a.count(b" "),
        b.count(b"_"),
    )


def estimate_tokens(b):
    if not b:
        return 0
    t = sum(c * f for c, f in zip(_COEF, token_features(b)))
    return int(t + 0.5) if t > 1 else 1


# -- secrets -------------------------------------------------------------------
# Only unmistakable credential formats. (label, fragments one of which must be
# present - a C-speed pre-check -, pattern)

_SECRETS = (
    ("private key", (b"PRIVATE KEY-----",), rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    ("AWS access key", (b"AKIA", b"ASIA"), rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("GitHub token", (b"ghp_", b"gho_", b"ghu_", b"ghs_", b"ghr_", b"github_pat_"),
     rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
    ("API key", (b"sk-",), rb"\bsk-(?:ant-|proj-|svcacct-|admin-|or-)?[A-Za-z0-9_\-]{32,}"),
    ("Slack token", (b"xox",), rb"\bxox[abprs]-[A-Za-z0-9\-]{20,}"),
    ("Google API key", (b"AIza",), rb"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("Stripe live key", (b"k_live_",), rb"\b[sr]k_live_[0-9a-zA-Z]{20,}"),
    ("Google OAuth secret", (b"GOCSPX-",), rb"\bGOCSPX-[A-Za-z0-9_\-]{20,}"),
    ("GitLab token", (b"glpat-",), rb"\bglpat-[A-Za-z0-9_\-]{20,}"),
    ("npm token", (b"npm_",), rb"\bnpm_[A-Za-z0-9]{36}\b"),
)
_SECRET_RX = {}
_PLACEHOLDER = (b"example", b"xxxx", b"your", b"placeholder", b"dummy", b"fake", b"sample", b"0000000", b"1234567")


def find_secret(data):
    """'' or a short description such as 'AWS access key, line 12'."""
    for label, fragments, pattern in _SECRETS:
        if not any(fragment in data for fragment in fragments):
            continue
        rx = _SECRET_RX.get(label)
        if rx is None:
            import re

            rx = _SECRET_RX[label] = re.compile(pattern)
        for m in rx.finditer(data):
            hit = m.group(0)
            if label != "private key" and (len(set(hit)) < 10 or any(p in hit.lower() for p in _PLACEHOLDER)):
                continue
            return "%s, line %d" % (label, data.count(b"\n", 0, m.start()) + 1)
    return ""


# -- reading files ---------------------------------------------------------------

_O_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOCTTY", 0)
_CHUNK = 64 * 1024  # os.read allocates the whole request up front; megabyte requests are ~12x slower per call


def read_file(path, limit=0):
    """File bytes, or None if unreadable. With `limit`, reading stops once more than `limit` bytes are in hand."""
    try:
        fd = os.open(path, _O_FLAGS)
    except OSError:
        return None
    try:
        data = os.read(fd, _CHUNK)
        if len(data) < _CHUNK:
            return data
        chunks = [data]
        size = _CHUNK
        while not limit or size <= limit:
            data = os.read(fd, _CHUNK)
            if not data:
                break
            chunks.append(data)
            size += len(data)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _is_utf16(head):
    return head[:2] in (b"\xff\xfe", b"\xfe\xff")


def decode_text(data):
    if _is_utf16(data):
        text = data.decode("utf-16", "replace")
    else:
        if data[:3] == b"\xef\xbb\xbf":
            data = data[3:]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1252", "replace")
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def measure(path, full=False):
    """-> (why, tokens, size, lines, note) for one file; why '' means ordinary text."""
    data = read_file(path, 0 if full else MAX_BYTES)
    if data is None:
        if os.path.isdir(path):
            return ("folder", 0, 0, 0, "")
        return ("unreadable" if os.path.lexists(path) else "missing", 0, 0, 0, "")
    size = len(data)
    if b"\0" in data[:SNIFF]:
        if not _is_utf16(data):
            return ("binary", 0, size, 0, "")
        data = data.decode("utf-16", "replace").encode("utf-8")
    if size > MAX_BYTES and not full:
        try:
            size = os.stat(path).st_size
        except OSError:
            pass
        return ("large", size // 4, size, 0, "")
    note = find_secret(data)
    lines = data.count(b"\n") + (not data.endswith(b"\n"))
    return ("secret" if note else "", estimate_tokens(data), size, lines, note)


# -- background scanner: I/O only, no tree logic -----------------------------------


def list_dir(path):
    """[(name, is_dir, size)] or None if the directory cannot be read."""
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        out.append((e.name, True, 0))
                    else:
                        out.append((e.name, False, e.stat().st_size))
                except OSError:
                    out.append((e.name, False, -1))
    except OSError:
        return None
    return out


GIT_LS = ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard")
GIT_FAILED = "git could not list this repository (try `git status` here), so .gitignore is not applied"


def start_git(root):
    """Start `git ls-files` now; returns a function that waits for it and returns the paths git
    considers part of the project (tracked + untracked-but-not-ignored), or None.

    The process is created with OS primitives: importing `subprocess` alone costs ~15 ms on Windows.
    """
    try:
        if os.name == "nt":
            import _winapi
            import msvcrt

            me = _winapi.GetCurrentProcess()
            read, write = _winapi.CreatePipe(None, 0)
            nul = os.open(os.devnull, os.O_RDWR)
            try:
                out = _winapi.DuplicateHandle(me, write, me, 0, True, _winapi.DUPLICATE_SAME_ACCESS)
                _winapi.CloseHandle(write)
                null = _winapi.DuplicateHandle(me, msvcrt.get_osfhandle(nul), me, 0, True, _winapi.DUPLICATE_SAME_ACCESS)
            finally:
                os.close(nul)

            class startup:  # the STARTUPINFO fields CreateProcess reads
                dwFlags = _winapi.STARTF_USESTDHANDLES
                wShowWindow = 0
                hStdInput = hStdError = null
                hStdOutput = out
                lpAttributeList = {"handle_list": [null, out]}

            try:
                process, thread, _, _ = _winapi.CreateProcess(None, " ".join(GIT_LS), None, None, True, 0, None, root, startup)
            finally:
                _winapi.CloseHandle(out)
                _winapi.CloseHandle(null)
            _winapi.CloseHandle(thread)
            fd = msvcrt.open_osfhandle(read, os.O_RDONLY)

            def wait():
                _winapi.WaitForSingleObject(process, _winapi.INFINITE)
                code = _winapi.GetExitCodeProcess(process)
                _winapi.CloseHandle(process)
                return code
        else:
            fd, write = os.pipe()
            try:
                pid = os.posix_spawnp("git", ("git", "-C", root) + GIT_LS[1:], os.environ, file_actions=[
                    (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                    (os.POSIX_SPAWN_DUP2, write, 1),
                    (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
                    (os.POSIX_SPAWN_CLOSE, fd),
                ])
            except BaseException:
                os.close(fd)
                raise
            finally:
                os.close(write)

            def wait():
                return os.waitpid(pid, 0)[1]
    except (OSError, AttributeError, ImportError):
        return _start_git_portable(root)

    def finish():
        chunks = []
        try:
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
        if wait() != 0:
            return None
        return b"".join(chunks).decode("utf-8", "surrogateescape").split("\0")

    return finish


def _start_git_portable(root):
    def finish():
        import subprocess

        try:
            p = subprocess.run(GIT_LS, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return None
        return p.stdout.decode("utf-8", "surrogateescape").split("\0") if p.returncode == 0 else None

    return finish


def in_git_repo(path):
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


class Scanner:
    """Worker thread(s) that list folders and measure files.

    Opening a cached file takes ~50 microseconds, a never-read one ~10 ms (antivirus, cold disk).
    Threads make the first case slower and the second ~15x faster, so there is one thread until
    opens turn out to be slow.
    """

    def __init__(self, root):
        try:
            from _queue import SimpleQueue
        except ImportError:
            from queue import SimpleQueue
        import _thread

        self.root = root
        self.wake = lambda: None  # set by whoever wants to hear about new results
        self.tickets = SimpleQueue()
        self.hi = deque()  # folder listings first: they shape the tree
        self.lo = deque()  # file measurements
        self.last = deque()  # listings that only add what git ignores: nothing waits for those
        self.out = deque()
        self.threads = 1
        self._start = _thread.start_new_thread
        self._start(self._run, (True,))

    def submit(self, kind, node, arg=None, last=False):
        (self.last if last else self.lo if kind == "tok" else self.hi).append((kind, node, arg))
        self.tickets.put(1)

    def path(self, node):
        return self.root + "/" + node.rel.rstrip("/") if node.rel else self.root

    def _run(self, first):
        tickets, hi, lo, last, out = self.tickets, self.hi, self.lo, self.last, self.out
        batch = []
        flushed = perf_counter()
        spent = 0.0
        count = 0
        while True:
            if batch and (not lo or tickets.empty()):  # nothing more to count right now: hand over what is counted
                out.extend(batch)
                batch = []
                self.wake()
            tickets.get()
            while True:
                try:
                    kind, node, arg = (hi or lo or last).popleft()
                    break
                except IndexError:  # another thread was quicker
                    pass
            t = perf_counter()
            if kind == "tok":
                res = measure(self.path(node), arg)
                if first and self.threads == 1:
                    spent += perf_counter() - t
                    count += 1
                    if spent > 0.03 and spent > 0.001 * count and len(lo) > COLD_THREADS > 1:
                        self.threads = COLD_THREADS
                        for _ in range(COLD_THREADS - 1):
                            self._start(self._run, (False,))
            elif kind == "ls":
                res = list_dir(self.path(node))
            else:
                res = arg()
            batch.append((kind, node, res))
            if kind != "tok" or len(batch) >= BATCH or t - flushed > BATCH_SECONDS:
                out.extend(batch)
                batch = []
                flushed = t
                self.wake()


# -- the tree ------------------------------------------------------------------------

LAZY, LOADING, LOADED = 0, 1, 2


class Node:
    # why: reason it is outside the default selection   pin: decided by the user or a saved rule
    # measured: 0 no, 1 rough (from its size), 2 counted
    # folders - on: new children start selected   xcl: ignored and not ticked   git: listed by git
    #           n, nsel, ttot, tsel, nv, nheld: running totals, see _contrib
    __slots__ = ("name", "parent", "kids", "rel", "depth", "why", "note", "sel", "pin", "tok", "size", "lines",
                 "measured", "state", "open", "on", "xcl", "git", "n", "nsel", "ttot", "tsel", "nv", "nheld")

    def __init__(self, name, parent, is_dir):
        self.name = name
        self.parent = parent
        self.kids = [] if is_dir else None
        self.rel = parent.rel + name + ("/" if is_dir else "") if parent else ""
        self.depth = parent.depth + 1 if parent else 0
        self.why = self.note = ""
        self.sel = self.pin = self.open = self.on = self.xcl = self.git = False
        self.tok = self.lines = self.measured = 0
        self.size = -1
        self.state = LOADED
        self.n = self.nsel = self.ttot = self.tsel = self.nv = self.nheld = 0
        if parent:
            parent.kids.append(self)


def _contrib(f):
    """A file's share of its folders' totals: (candidates, selected, tokens, selected tokens,
    part of the project, held back as a possible secret)."""
    w, s = f.why, f.sel
    return (
        1 if (s or not w) else 0,
        1 if s else 0,
        0 if w else f.tok,
        f.tok if s else 0,
        1 if (s or not w or w in ("secret", "large")) else 0,
        1 if (w == "secret" and f.note and not s) else 0,
    )


def _totals(d):
    return (d.n, d.nsel, d.ttot, d.tsel, d.nv, d.nheld)


def _name_key(n):
    return (n.kids is None, n.name.lower(), n.name)


def _size_key(n):
    if n.kids is None:
        return (-(n.tok if n.sel else 0), -n.tok, n.name.lower())
    return (-n.tsel, -n.ttot, n.name.lower())


def load_rules(root):
    """{'path' or 'dir/': True (always include) | False (never include)} from .llmcopy."""
    rules = {}
    try:
        with open(os.path.join(root, RULES_FILE), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if len(line) > 2 and line[0] in "+-" and line[1] == " ":
                    rel = line[2:].strip().replace("\\", "/")
                    rules["" if rel in (".", "./") else rel] = line[0] == "+"
    except (OSError, UnicodeDecodeError):
        pass
    return rules


class Model:
    """Project tree, selection and totals. Only ever touched by the main thread."""

    def __init__(self, root, rules, reveal=False):
        self.root_path = root
        self.rules = rules
        self.reveal = reveal  # also list what git ignores (shown dimmed)
        self.rule_dirs = set()  # folders on the way to a "+" rule: to be looked into even if ignored
        for rel, include in rules.items():
            if include:
                parts = rel.rstrip("/").split("/")
                self.rule_dirs.update("/".join(parts[:i]) + "/" for i in range(1, len(parts)))
        self.sort_size = False
        self.jobs_ls = 1  # folders still to be listed: the shape of the project is not known yet
        self.jobs_more = 0  # listings that only add ignored entries: done last, the totals do not depend on them
        self.jobs_tok = 0
        self.structure = 0  # bumped whenever the visible rows may have changed
        self.git = False  # the project root is a git repository
        self.git_failed = False
        self._no_git = set()
        self._discovering = {}
        self.scan = Scanner(root)
        r = self.root = Node(os.path.basename(root.rstrip("/\\")) or root, None, True)
        r.state = LOADING
        r.open = True
        r.on = rules.get("", True)
        if in_git_repo(root):
            self.scan.submit("git", r, start_git(root))
        else:
            self.scan.submit("ls", r)

    # nodes

    def _dir(self, parent, name, why, git=False):
        d = Node(name, parent, True)
        rule = self.rules.get(d.rel)
        d.why = why or (INHERITED if parent.xcl else "")
        d.on = rule if rule is not None else (parent.on and not d.why)
        d.xcl = bool(d.why) and rule is not True
        d.git = git
        d.state = LOADED if git else LAZY
        return d

    def _file(self, parent, name, why, size=-1):
        f = Node(name, parent, False)
        rule = self.rules.get(f.rel)
        f.why = why = why or (INHERITED if parent.xcl else "large" if size > MAX_BYTES else "")
        f.sel = (parent.on and not why) if rule is None else (rule and why not in NOT_TEXT)
        f.pin = rule is not None
        f.size = size
        f.tok = size // 4 if size > 0 else 0
        return f

    def _want_measure(self, f):
        if f.measured < 2 and f.why not in NOT_TEXT and (f.sel or not f.why):
            self._submit_tok(f, f.sel and f.pin)

    def _submit_tok(self, f, full=False):
        self.jobs_tok += 1
        self.scan.submit("tok", f, full)

    def _submit_ls(self, d):
        if d.state == LAZY:
            d.state = LOADING
            self.jobs_ls += 1
            self.scan.submit("ls", d)

    def _discover(self, d, last):
        """List a folder git already told us about, to find what git ignores in it.
        `last`: only for show, after everything that counts; otherwise a saved rule points in there."""
        if id(d) not in self._discovering:
            self._discovering[id(d)] = last
            if last:
                self.jobs_more += 1
            else:
                self.jobs_ls += 1
            self.scan.submit("ls", d, last=last)

    # totals

    def _bump(self, d, a, b):
        delta = [y - x for x, y in zip(a, b)]
        if delta[4] and ((d.nv == 0) != (d.nv + delta[4] == 0)):
            self.structure += 1
        while d is not None:
            d.n += delta[0]
            d.nsel += delta[1]
            d.ttot += delta[2]
            d.tsel += delta[3]
            d.nv += delta[4]
            d.nheld += delta[5]
            d = d.parent

    def _recount(self, d):
        total = [0, 0, 0, 0, 0, 0]
        for k in d.kids:
            if k.kids is None:
                c = _contrib(k)
            else:
                self._recount(k)
                c = _totals(k)
            for i in range(6):
                total[i] += c[i]
        d.n, d.nsel, d.ttot, d.tsel, d.nv, d.nheld = total

    def _recount_from(self, d):
        """Recompute a subtree after a bulk change and carry the difference upwards."""
        a = _totals(d)
        self._recount(d)
        if d.parent is not None:
            self._bump(d.parent, a, _totals(d))
        self.structure += 1

    # scanner results

    @property
    def enumerating(self):
        return self.jobs_ls > 0

    @property
    def counting(self):
        return self.jobs_ls > 0 or self.jobs_tok > 0

    @property
    def busy(self):
        return self.counting or self.jobs_more > 0

    def pump(self, limit=0.008):
        """Apply finished scanner work for at most `limit` seconds. True if anything changed."""
        out = self.scan.out
        if not out:
            return False
        end = perf_counter() + limit
        n = 0
        while out:
            kind, node, res = out.popleft()
            if kind == "tok":
                self.jobs_tok -= 1
                self._apply_tok(node, res)
            elif kind == "ls":
                if self._discovering.pop(id(node), False):
                    self.jobs_more -= 1
                else:
                    self.jobs_ls -= 1
                self._apply_ls(node, res)
            else:
                self._apply_git(node, res)
            n += 1
            if not n & 31 and perf_counter() > end:
                break
        return True

    def finish(self):
        """Block until the scan is complete (no interface)."""
        import _thread

        ready = _thread.allocate_lock()

        def wake():
            try:
                ready.release()
            except RuntimeError:
                pass

        self.scan.wake = wake
        while self.busy:
            if not self.pump(0.5):
                ready.acquire(True, 0.05)

    def _apply_tok(self, f, res):
        why, tok, size, lines, note = res
        a = _contrib(f)
        if why == "folder":  # git lists a submodule as one entry
            f.kids = []
            f.rel += "/"
            f.why = "ignored"
            f.sel = f.pin = False
            f.xcl = True
            f.tok = 0
            f.state = LAZY
            self._bump(f.parent, a, (0, 0, 0, 0, 0, 0))
            f.parent.kids.sort(key=_size_key if self.sort_size else _name_key)
            self.structure += 1
            return
        f.size = size
        f.lines = lines
        f.measured = 2
        if why in NOT_TEXT:
            f.why = why
            f.sel = False
            f.tok = 0
        elif why == "large":
            f.tok = tok
            f.measured = 1
            f.why = f.why or why
            if f.sel and not f.pin:
                f.sel = False
            elif f.sel:
                self._submit_tok(f, True)
        else:
            f.tok = tok + 10 + len(f.rel) // 3  # + the <file> wrapper and its line in the tree
            if why == "secret":
                f.note = note
                f.why = f.why or why
                if f.sel and not f.pin:
                    f.sel = False
        b = _contrib(f)
        if a != b:
            self._bump(f.parent, a, b)

    def _apply_git(self, base, paths):
        """Build the subtree of `base` (the root, or a repository found inside a plain folder) from git's file list."""
        if paths is None:  # git missing or unhappy: walk the folder ourselves
            self.git_failed = True
            self._no_git.add(id(base))
            self.scan.submit("ls", base)
            return
        self.jobs_ls -= 1
        if base is self.root:
            self.git = True
        base.git = True
        base.state = LOADED
        dirs = {"": base}
        seen = set()

        def folder(rel):
            d = dirs.get(rel)
            if d is None:
                head, _, name = rel.rpartition("/")
                d = dirs[rel] = self._dir(folder(head), name, "ignored" if name in JUNK_DIRS else "", git=True)
            return d

        for p in paths:
            if not p or p in seen:
                continue
            seen.add(p)
            head, _, name = p.rstrip("/").rpartition("/")
            if p.endswith("/"):  # an untracked repository inside this one: git lists only the folder
                self._dir(folder(head), name, "ignored")
            else:
                self._file(folder(head), name, name_reason(name))

        key = _size_key if self.sort_size else _name_key
        for d in dirs.values():
            d.kids.sort(key=key)
        self._recount_from(base)
        self._measure_tree(base)
        # A "+" rule may point at something git does not list: look into the closest folder git knows.
        prefix = base.rel
        for rel, include in self.rules.items():
            local = rel[len(prefix):]
            key = local.rstrip("/")
            if not include or not rel.startswith(prefix) or not key or local in seen or key in dirs:
                continue
            while key not in dirs:
                key = key.rpartition("/")[0]
            self._discover(dirs[key], False)
        if self.reveal:
            self._reveal(base)

    def _measure_tree(self, d):
        for k in d.kids:
            if k.kids is None:
                self._want_measure(k)
            else:
                if k.state == LAZY and not k.xcl:
                    self._submit_ls(k)  # a nested repository switched on by a rule
                self._measure_tree(k)

    def _apply_ls(self, d, entries):
        discover = d.git  # git listed this folder already: anything else in it is git-ignored
        self.structure += 1
        if entries is None:
            d.state = LOADED
            return
        if not discover and not d.xcl and id(d) not in self._no_git and any(e[0] == ".git" for e in entries):
            # a repository inside a plain folder: its own git knows best what belongs to it
            self.jobs_ls += 1
            d.state = LOADING
            self.scan.submit("git", d, start_git(self.scan.path(d)))
            return
        d.state = LOADED
        have = {k.name: k for k in d.kids}
        if not discover and d.parent is not None and not d.why and any(e[0] == "pyvenv.cfg" for e in entries):
            d.why = "ignored"  # a Python virtualenv under an unusual name
            d.xcl = self.rules.get(d.rel) is not True
            d.on = d.on and not d.xcl
        a = _totals(d)
        for name, is_dir, size in entries:
            old = have.get(name)
            if old is not None:
                if old.kids is None and old.size < 0 <= size:
                    old.size = size
                    if not old.measured and not old.tok:
                        old.tok = size // 4
            elif is_dir:
                if name in VCS_DIRS:
                    continue
                junk = discover or name in JUNK_DIRS or name in MAYBE_JUNK_DIRS or name.endswith(".egg-info")
                k = self._dir(d, name, "ignored" if junk else "")
                if not k.xcl or k.rel in self.rule_dirs:
                    self._submit_ls(k)  # part of the project, or a "+" rule points inside
            else:
                self._want_measure(self._file(d, name, name_reason(name) or ("ignored" if discover else ""), size))
        d.kids.sort(key=_size_key if self.sort_size else _name_key)
        self._recount(d)
        if d.parent is not None:
            self._bump(d.parent, a, _totals(d))

    # selection

    def toggle_file(self, f):
        """Flip one file. Returns a message if it cannot be selected."""
        if f.why in NOT_TEXT:
            return "%s: %s file, nothing to paste" % (f.name, f.why)
        a = _contrib(f)
        f.sel = not f.sel
        f.pin = True
        self._bump(f.parent, a, _contrib(f))
        if f.sel and f.measured < 2:
            self._submit_tok(f, True)
        return ""

    def set_dir(self, d, value, match=None):
        """Select or clear a whole folder. `match` limits it to some files (filter view)."""
        self._set(d, value, True, match)
        self._recount_from(d)

    def _set(self, node, value, top, match):
        if match is None and not value and not node.why and node.parent is not None and node.parent.xcl:
            node.why = INHERITED  # its folder is ignored again
        if node.kids is None:
            if match is None or match(node):
                node.sel = value and not node.why
                node.pin = False
                if node.sel and node.measured < 2:
                    self._submit_tok(node)
            return
        if match is not None:
            if node.xcl and value:
                return
        elif value and (top or not node.why):
            if node.xcl:
                self._force(node)
            node.on = True
        else:  # cleared - or an ignored folder inside the one being ticked: that goes back to left out
            value = False
            node.on = False
            node.xcl = bool(node.why)
        for k in node.kids:
            self._set(k, value, False, match)

    def _force(self, d):
        """The user ticked an ignored folder: treat its contents as project files."""
        d.xcl = False
        if d.state == LAZY:
            self._submit_ls(d)
        for k in d.kids:
            if k.why == INHERITED:
                k.why = ""
                if k.kids is not None:
                    self._force(k)

    def dir_checked(self, d):
        """0 none, 1 some, 2 all."""
        if d.nsel:
            return 2 if d.nsel == d.n else 1
        return 2 if d.state != LOADED and d.on else 0

    def toggle(self, node, match=None):
        if node.kids is None:
            return self.toggle_file(node)
        if match is None:
            self.set_dir(node, self.dir_checked(node) != 2)
        else:
            files = [f for f in self.files_under(node) if not f.why and match(f)]
            self.set_dir(node, not all(f.sel for f in files), match)
        return ""

    def files_under(self, d):
        stack = [d]
        while stack:
            n = stack.pop()
            if n.kids is None:
                yield n
            else:
                stack.extend(n.kids)

    def resort(self):
        key = _size_key if self.sort_size else _name_key
        stack = [self.root]
        while stack:
            d = stack.pop()
            d.kids.sort(key=key)
            stack.extend(k for k in d.kids if k.kids is not None)
        self.structure += 1

    def reveal_ignored(self):
        self.reveal = True
        self._reveal(self.root)

    def _reveal(self, base):
        todo = [base]
        for d in todo:  # top level first: that is what is on screen
            if not d.xcl:
                if d.git:  # (folders we walked ourselves already know all their entries)
                    self._discover(d, True)
                todo.extend(k for k in d.kids if k.kids is not None)

    def expand(self, d):
        self._submit_ls(d)
        d.open = True
        self.structure += 1

    # saved selection

    def collect_rules(self):
        """The smallest set of +/- rules that reproduces the current selection from the defaults."""
        out = []

        def state(d, expect):
            if d.xcl:
                return False  # still ignored as a folder; files ticked inside get their own rule
            if d.state != LOADED:
                return d.on
            if d.n == 0:
                return expect
            if d.nsel == 0 or d.nsel == d.n:
                return d.nsel > 0
            return d.on

        def walk(d, inherited):
            for k in sorted(d.kids, key=_name_key):
                expect = inherited and not k.why
                if k.kids is None:
                    if k.sel != expect and k.why not in NOT_TEXT:
                        out.append(("+" if k.sel else "-", k.rel))
                    continue
                actual = state(k, expect)
                if actual != expect:
                    out.append(("+" if actual else "-", k.rel))
                if k.state == LOADED:
                    walk(k, actual)

        on = state(self.root, True)
        if not on:
            out.append(("-", "./"))
        walk(self.root, on)
        return out

    def save_rules(self):
        """Write .llmcopy (or remove it if the selection is the default). Returns the number of rules."""
        rules = self.collect_rules()
        path = os.path.join(self.root_path, RULES_FILE)
        if rules:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("# llmcopy: what to copy from this folder by default.\n"
                        '# "+ path" always include, "- path" never include; the rest follows the built-in defaults.\n')
                f.writelines("%s %s\n" % r for r in rules)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
        self.rules = {("" if rel == "./" else rel): sign == "+" for sign, rel in rules}
        return len(rules)


# -- output ----------------------------------------------------------------------------


def build_output(model):
    """-> (text, info); info has files, tokens, held (possible secrets), failed (unreadable)."""
    root = model.root
    blocks, tree, included, held, failed = [], [], set(), [], []
    omitted = False

    def collect(d):
        for k in sorted(d.kids, key=_name_key):
            if k.kids is not None:
                if k.nsel:
                    collect(k)
            elif k.sel:
                data = read_file(model.root_path + "/" + k.rel)
                if data is None or (b"\0" in data[:SNIFF] and not _is_utf16(data)):
                    failed.append(k.rel)
                    continue
                note = "" if k.pin else find_secret(data)
                if note:
                    held.append((k.rel, note))
                    continue
                text = decode_text(data)
                rel = k.rel.encode("utf-8", "replace").decode("utf-8").replace('"', "&quot;")
                blocks.append('<file path="%s">\n%s%s</file>\n' % (rel, text, "" if text.endswith("\n") or not text else "\n"))
                included.add(k.rel)

    def outline(d, depth):
        nonlocal omitted
        pad = "  " * depth
        for k in sorted(d.kids, key=_name_key):
            if k.kids is None:
                if k.rel in included:
                    tree.append(pad + k.name)
                elif not k.why or k.sel or (k.why == "secret" and k.note):
                    tree.append(pad + k.name + "  (omitted)")
                    omitted = True
            elif k.nsel:
                tree.append(pad + k.name + "/")
                outline(k, depth + 1)
            elif k.n and not k.why:
                tree.append(pad + k.name + "/  (omitted)")
                omitted = True

    collect(root)
    outline(root, 1)
    if root.nheld:  # files the scan already set aside
        held += [(f.rel, f.note) for f in model.files_under(root) if f.why == "secret" and f.note and not f.sel]
    n = len(blocks)
    head = 'Project "%s": %d file%s. The file tree comes first, then each file in a <file path="..."> block.' % (
        root.name, n, "" if n == 1 else "s")
    if omitted:
        head += ' Entries marked "(omitted)" exist in the project but their contents are not included.'
    text = "%s\n\n<tree>\n%s/\n%s\n</tree>\n\n%s" % (head, root.name, "\n".join(tree), "\n".join(blocks))
    return text, {"files": n, "tokens": estimate_tokens(text.encode("utf-8", "replace")), "held": sorted(held), "failed": failed}


# -- clipboard ---------------------------------------------------------------------------


def _clip_windows(text):
    import ctypes
    from ctypes import wintypes as w
    from time import sleep

    u32 = ctypes.WinDLL("user32")
    k32 = ctypes.WinDLL("kernel32")
    k32.GlobalAlloc.restype = w.HGLOBAL
    k32.GlobalAlloc.argtypes = (w.UINT, ctypes.c_size_t)
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = k32.GlobalUnlock.argtypes = k32.GlobalFree.argtypes = (w.HGLOBAL,)
    u32.OpenClipboard.argtypes = (w.HWND,)
    u32.SetClipboardData.restype = w.HANDLE
    u32.SetClipboardData.argtypes = (w.UINT, w.HANDLE)

    data = text.encode("utf-16-le", "replace") + b"\0\0"
    for _ in range(20):  # another program may hold the clipboard for a moment
        if u32.OpenClipboard(None):
            break
        sleep(0.02)
    else:
        raise OSError("the clipboard is locked by another program")
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0002, len(data))
        if not h:
            raise OSError("not enough memory for the clipboard")
        ctypes.memmove(k32.GlobalLock(h), data, len(data))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(13, h):  # CF_UNICODETEXT
            k32.GlobalFree(h)
            raise OSError("the clipboard refused the text")
    finally:
        u32.CloseClipboard()


def copy_to_clipboard(text):
    """Put text on the clipboard. Returns "clipboard", or "terminal" when it could only ask the terminal (OSC 52)."""
    if os.name == "nt":
        _clip_windows(text)
        return "clipboard"
    import subprocess
    from shutil import which

    utf8 = text.encode("utf-8", "replace")
    env = None
    if sys.platform in ("cygwin", "msys"):
        with open("/dev/clipboard", "wb") as f:
            f.write(utf8)
        return "clipboard"
    if sys.platform == "darwin":
        tools = [(["pbcopy"], utf8)]
        env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")  # pbcopy reads bytes in the locale's encoding
    else:
        tools = []
        if "microsoft" in os.uname().release.lower() and which("clip.exe"):  # WSL; no BOM, clip.exe would paste it
            tools.append((["clip.exe"], text.encode("utf-16-le", "replace")))
        if os.environ.get("WAYLAND_DISPLAY") and which("wl-copy"):
            tools.append((["wl-copy"], utf8))
        if os.environ.get("DISPLAY"):
            tools += [(cmd, utf8) for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]) if which(cmd[0])]
    for cmd, data in tools:
        try:
            subprocess.run(cmd, input=data, check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "clipboard"
        except (OSError, subprocess.SubprocessError):
            continue
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        raise OSError("no clipboard tool found: install wl-clipboard (Wayland) or xclip (X11)")
    # No desktop (a remote shell): ask the terminal itself.
    from base64 import b64encode

    try:
        with open("/dev/tty", "wb") as tty:
            tty.write(b"\x1b]52;c;" + b64encode(utf8) + b"\x07")
    except OSError:
        raise OSError("no clipboard tool found (install wl-clipboard, xclip or xsel)")
    return "terminal"


# -- terminal: raw keyboard + mouse input ------------------------------------------------
# read() returns events: ("key", name) | ("char", c) | ("click", x, y) | ("drag", x, y) | ("wheel", +1/-1, x, y)
# | ("resize",). A drag is the pointer moving with the left button held.


class _WinTerm:
    """Windows console through native input records: Windows Terminal, PowerShell, cmd, VS Code,
    Git Bash with ConPTY and the classic console window."""

    _VK = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x21: "pgup", 0x22: "pgdn", 0x24: "home",
           0x23: "end", 0x0D: "enter", 0x1B: "esc", 0x20: "space", 0x09: "tab", 0x08: "backspace"}
    on = off = ""  # mouse reports: the console sends them as input records

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class KEY(ctypes.Structure):
            _fields_ = [("down", w.BOOL), ("repeat", w.WORD), ("vk", w.WORD), ("scan", w.WORD), ("char", w.WCHAR),
                        ("ctrl", w.DWORD)]

        class MOUSE(ctypes.Structure):
            _fields_ = [("pos", COORD), ("buttons", w.DWORD), ("ctrl", w.DWORD), ("flags", w.DWORD)]

        class EVENT(ctypes.Union):
            _fields_ = [("key", KEY), ("mouse", MOUSE), ("size", COORD)]

        class RECORD(ctypes.Structure):
            _fields_ = [("type", w.WORD), ("ev", EVENT)]

        k = self.k = ctypes.WinDLL("kernel32")
        k.GetStdHandle.restype = k.CreateEventW.restype = w.HANDLE
        k.GetConsoleMode.argtypes = k.GetNumberOfConsoleInputEvents.argtypes = (w.HANDLE, w.LPDWORD)
        k.SetConsoleMode.argtypes = (w.HANDLE, w.DWORD)
        k.ReadConsoleInputW.argtypes = (w.HANDLE, ctypes.c_void_p, w.DWORD, w.LPDWORD)
        k.CreateEventW.argtypes = (ctypes.c_void_p, w.BOOL, w.BOOL, ctypes.c_void_p)
        k.SetEvent.argtypes = (w.HANDLE,)
        k.WaitForMultipleObjects.restype = w.DWORD
        k.WaitForMultipleObjects.argtypes = (w.DWORD, ctypes.c_void_p, w.BOOL, w.DWORD)

        self.hin = k.GetStdHandle(-10)
        self.hout = k.GetStdHandle(-11)
        self.old_in, self.old_out = w.DWORD(), w.DWORD()
        if not k.GetConsoleMode(self.hin, ctypes.byref(self.old_in)) or not k.GetConsoleMode(self.hout, ctypes.byref(self.old_out)):
            raise OSError("not a console")
        if not k.SetConsoleMode(self.hout, self.old_out.value | 0x0001 | 0x0004):  # escape sequences
            raise OSError("this console does not understand escape sequences")
        k.SetConsoleMode(self.hin, 0x0008 | 0x0010 | 0x0080)  # size + mouse events; no echo, line editing or QuickEdit
        self.event = k.CreateEventW(None, False, False, None)
        # The wake-up event comes first: when scan results and input are both pending, results win
        # (asking the console for input can block while it is busy drawing).
        self.handles = (w.HANDLE * 2)(self.event, self.hin)
        self.buf = (RECORD * 256)()
        self.count = w.DWORD()
        self.byref = ctypes.byref
        self.short = ctypes.c_short
        self.left_down = False

    def wake(self):
        self.k.SetEvent(self.event)

    def close(self):
        self.k.SetConsoleMode(self.hin, self.old_in.value)
        self.k.SetConsoleMode(self.hout, self.old_out.value)

    def read(self, timeout):
        """Wait for input, a wake-up or the timeout (seconds, None = forever)."""
        k = self.k
        ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        if k.WaitForMultipleObjects(2, self.handles, False, ms) != 1:
            return []
        events = []
        n = self.count
        k.GetNumberOfConsoleInputEvents(self.hin, self.byref(n))
        if not n.value:
            return events
        k.ReadConsoleInputW(self.hin, self.buf, 256, self.byref(n))
        for i in range(n.value):
            r = self.buf[i]
            if r.type == 1:
                key = r.ev.key
                if not key.down:
                    continue
                name = self._VK.get(key.vk) or ("ctrl-c" if key.char == "\x03" else None)
                if name:
                    events.extend((("key", name),) * max(1, key.repeat))
                elif key.char >= " " and key.char != "\x7f":
                    events.append(("char", key.char))
            elif r.type == 2:
                m = r.ev.mouse
                if m.flags & 4:  # wheel: the sign of the high word is the direction
                    events.append(("wheel", -1 if self.short(m.buttons >> 16).value > 0 else 1, m.pos.X, m.pos.Y))
                elif m.flags in (0, 2):
                    down = bool(m.buttons & 1)
                    if down and not self.left_down:
                        events.append(("click", m.pos.X, m.pos.Y))
                    self.left_down = down
                elif m.flags & 1 and m.buttons & 1 and self.left_down:
                    events.append(("drag", m.pos.X, m.pos.Y))
            elif r.type == 4:
                events.append(("resize",))
        return events


class _PosixTerm:
    """macOS, Linux, WSL, Cygwin: termios raw mode and xterm mouse reports."""

    _CSI = {b"A": "up", b"B": "down", b"C": "right", b"D": "left", b"H": "home", b"F": "end", b"5~": "pgup",
            b"6~": "pgdn", b"1~": "home", b"7~": "home", b"4~": "end", b"8~": "end"}
    _CTRL = {0x0D: "enter", 0x0A: "enter", 0x7F: "backspace", 0x08: "backspace", 0x09: "tab", 0x03: "ctrl-c", 0x20: "space"}
    on = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"  # presses, and moves while a button is held
    off = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"

    def __init__(self):
        import select
        import signal
        import termios

        self.select = select.select
        self.termios = termios
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        new = termios.tcgetattr(self.fd)
        new[0] &= ~(termios.IXON | termios.ICRNL | termios.INLCR | termios.IGNCR | termios.ISTRIP | termios.BRKINT)
        new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSADRAIN, new)
        self.r, self.w = os.pipe()  # lets the scanner thread and SIGWINCH interrupt select()
        os.set_blocking(self.r, False)
        os.set_blocking(self.w, False)
        self.resized = False
        self.pending = b""
        signal.signal(signal.SIGWINCH, self._winch)
        for sig in (signal.SIGTERM, signal.SIGHUP):  # leave through the `finally` blocks: the terminal is restored
            signal.signal(sig, self._quit)

    def _winch(self, *_):
        self.resized = True
        self.wake()

    def _quit(self, *_):
        raise KeyboardInterrupt

    def wake(self):
        try:
            os.write(self.w, b"x")
        except OSError:
            pass  # pipe full: a wake-up is already on its way

    def close(self):
        self.termios.tcsetattr(self.fd, self.termios.TCSADRAIN, self.old)

    def read(self, timeout):
        ready = self.select([self.fd, self.r], [], [], timeout)[0]
        events = []
        if self.r in ready:
            try:
                os.read(self.r, 4096)
            except OSError:
                pass
        if self.resized:
            self.resized = False
            events.append(("resize",))
        if self.fd in ready:
            data = os.read(self.fd, 4096)
            if not data:
                return [("key", "ctrl-c")]  # the terminal went away
            self.pending += data
            self._parse(events)
        return events

    def _more(self):
        """An escape sequence may arrive in pieces: give the rest 30 ms."""
        if self.select([self.fd], [], [], 0.03)[0]:
            data = os.read(self.fd, 4096)
            self.pending += data
            return bool(data)
        return False

    def _parse(self, events):
        while self.pending:
            buf = self.pending
            c = buf[0]
            if c == 0x1B:
                if len(buf) == 1:
                    if self._more():
                        continue
                    events.append(("key", "esc"))
                    self.pending = b""
                    return
                if buf[1] not in (0x5B, 0x4F):  # not CSI or SS3: a lone Esc
                    self.pending = buf[1:]
                    events.append(("key", "esc"))
                    continue
                j = 2
                while j < len(buf) and not 0x40 <= buf[j] <= 0x7E:
                    j += 1
                if j >= len(buf):
                    if self._more():
                        continue
                    self.pending = b""
                    return
                seq = buf[2:j + 1]
                self.pending = buf[j + 1:]
                if seq[:1] == b"<":  # mouse: <button;x;y then M (press) or m (release)
                    try:
                        b, x, y = (int(v) for v in seq[1:-1].split(b";"))
                    except ValueError:
                        continue
                    if b & 64:
                        events.append(("wheel", 1 if b & 1 else -1, x - 1, y - 1))
                    elif b & 3 == 0 and seq[-1:] == b"M":  # the left button: pressed, or moved while held
                        events.append(("drag" if b & 32 else "click", x - 1, y - 1))
                    continue
                name = self._CSI.get(seq) or self._CSI.get(seq[-1:])
                if name:
                    events.append(("key", name))
            elif c >= 0x80:  # a UTF-8 character, possibly still arriving
                need = 2 if c < 0xE0 else 3 if c < 0xF0 else 4
                if len(buf) < need:
                    if self._more():
                        continue
                    self.pending = b""
                    return
                self.pending = buf[need:]
                try:
                    events.append(("char", buf[:need].decode("utf-8")))
                except UnicodeDecodeError:
                    pass
            else:
                self.pending = buf[1:]
                name = self._CTRL.get(c)
                if name:
                    events.append(("key", name))
                elif c >= 0x20:
                    events.append(("char", chr(c)))


def open_terminal():
    return _WinTerm() if os.name == "nt" else _PosixTerm()


def _msys_pty(stream):
    """Windows only: the stream is a Git Bash / Cygwin terminal that is not a console (mintty without ConPTY)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        import msvcrt

        k = ctypes.WinDLL("kernel32")
        k.GetFileInformationByHandleEx.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong)
        buf = ctypes.create_string_buffer(1024)  # FILE_NAME_INFO: a length, then the name in UTF-16
        if not k.GetFileInformationByHandleEx(msvcrt.get_osfhandle(stream.fileno()), 2, buf, 1024):
            return False
        name = buf.raw[4:4 + int.from_bytes(buf.raw[:4], "little")].decode("utf-16-le", "replace")
        return ("msys-" in name or "cygwin-" in name) and "-pty" in name
    except Exception:
        return False


def _is_tty(stream):
    try:
        return stream.isatty() or _msys_pty(stream)
    except (AttributeError, ValueError, OSError):
        return False


# -- interface ------------------------------------------------------------------------------


def fmt_tokens(n):
    if n < 1000:
        return str(n)
    if n < 99_950:
        return "%.1fk" % (n / 1000)
    if n < 999_500:
        return "%.0fk" % (n / 1000)
    return "%.2fM" % (n / 1_000_000)


def parse_tokens(s):
    s = s.strip().lower().replace(",", "").replace("_", "")
    mult = 1000 if s.endswith("k") else 1_000_000 if s.endswith("m") else 1
    value = int(float(s.rstrip("km")) * mult)
    if value <= 0:
        raise ValueError("budget must be positive")
    return value


def _cell(c):
    from unicodedata import combining, east_asian_width

    return 0 if combining(c) else 2 if east_asian_width(c) in "WF" else 1


def _width(s):
    return len(s) if s.isascii() else sum(map(_cell, s))


def _clip(s, width):
    """Shorten s to at most `width` terminal cells."""
    if width <= 0:
        return ""
    if _width(s) <= width:
        return s
    out, used = [], 0
    for c in s:
        used += 1 if c < "\x80" else _cell(c)
        if used > width - 1:
            break
        out.append(c)
    return "".join(out) + "…"


def _bar(frac, cells, track=" "):
    halves = int(max(0.0, min(1.0, frac)) * cells * 2 + 0.5)
    full, half = divmod(halves, 2)
    return "█" * full + "▌" * half + track * (cells - full - half)


def _config_file():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "llmcopy", "budget")


def saved_budget():
    try:
        with open(_config_file(), encoding="ascii") as f:
            return parse_tokens(f.read())
    except (OSError, ValueError):
        return 0


def remember_budget(value):
    path = _config_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii") as f:
            f.write("%d\n" % value)
    except OSError:
        pass


class UI:
    HEAD, FOOT = 1, 2  # rows above and below the tree
    KEYS = {"q": "quit", "c": "copy", "t": "sort", "i": "ignored", "b": "budget", "s": "save", "/": "filter", "a": "all",
            "j": "down", "k": "up", "h": "left", "l": "right"}

    def __init__(self, model, term, budget, color=True):
        self.m = model
        self.term = term
        self.budget = budget
        self.color = color
        self.rows = []
        self.index = {}
        self.rows_at = -1
        self.cur = model.root
        self.top = 0
        self.show_ignored = True
        self.filter = ""
        self.terms = []
        self.typing = False
        self.msg = ""
        self.msg_style = "2"
        self.touched = False  # the user has done something
        self.folded_by_user = False
        self.expanded_once = False
        self.settled = False
        self.edited = False  # the selection differs from what was loaded or last saved
        self.esc_armed = False
        self.want_copy = False
        self.result = None
        self.prev = []
        self.size = (0, 0)
        self.hints = []  # clickable footer regions: (x0, x1, action)
        self.grab = None  # while the scrollbar is dragged: the line of its thumb the pointer holds
        self.meter_x = 0
        self.dirty = True
        self.last_draw = 0.0

    def s(self, code, text):
        """Wrap text in an SGR style. NO_COLOR keeps bold/dim/reverse and drops hues."""
        if not self.color:
            code = ";".join(c for c in code.split(";") if len(c) == 1)
        return "\x1b[%sm%s\x1b[0m" % (code, text) if code and text else text

    # rows

    def _visible(self, n):
        if self.show_ignored:
            return True
        if n.kids is None:
            return n.sel or not n.why or n.why in ("secret", "large")
        return n.nv > 0 or (not n.xcl and n.state != LOADED)

    def _match(self, f):
        low = f.rel.lower()
        return all(term in low for term in self.terms)

    def build_rows(self):
        root = self.m.root
        visible = self._visible
        rows = [root]
        if self.filter:
            self.terms = self.filter.lower().split()

            def walk(d):
                found = []
                for k in d.kids:
                    if not visible(k):
                        continue
                    if k.kids is None:
                        if self._match(k):
                            found.append(k)
                    else:
                        sub = walk(k)
                        if sub:
                            found.append(k)
                            found.extend(sub)
                return found

            rows.extend(walk(root))
        else:
            stack = [k for k in reversed(root.kids) if visible(k)]
            while stack:
                n = stack.pop()
                rows.append(n)
                if n.kids and n.open:
                    stack.extend(k for k in reversed(n.kids) if visible(k))
        self.rows = rows
        self.index = {id(n): i for i, n in enumerate(rows)}
        self.rows_at = self.m.structure
        n = self.cur  # keep the cursor on the same entry, or the closest ancestor still on screen
        while id(n) not in self.index and n.parent is not None:
            n = n.parent
        self.cur = n

    def cursor_index(self):
        return self.index.get(id(self.cur), 0)

    def body_height(self):
        return max(1, self.size[1] - self.HEAD - self.FOOT)

    def thumb(self):
        """The scrollbar, in lines of the tree area: (where its thumb starts, the thumb's length, the
        last line it can start on). None while every row is on screen."""
        h, n = self.body_height(), len(self.rows)
        if n <= h or h < 2:
            return None
        size = max(1, (h * h + n // 2) // n)
        span, last = h - size, n - h
        pos = (self.top * span + last // 2) // last
        if span > 1:  # it touches an end only when the list is at that end
            pos = min(max(pos, self.top > 0), span - (self.top < last))
        return pos, size, span

    def _fold_all(self):
        stack = [self.m.root]
        while stack:
            d = stack.pop()
            d.open = d.parent is None
            stack.extend(k for k in d.kids if k.kids is not None)
        self.m.structure += 1
        return self.body_height() - 1 - sum(1 for k in self.m.root.kids if self._visible(k))

    def _unfold(self, d, room):
        need = sum(1 for k in d.kids if self._visible(k))
        if d.parent.open and need <= room:
            d.open = True
            return room - need
        return room

    def auto_expand(self):
        """Unfold level by level, top to bottom, while everything fits on one screen: a small project
        shows completely, a large one as an overview. Folders with nothing selected stay folded."""
        room = self._fold_all()
        level = [self.m.root]
        while level and room > 0:
            level = [k for d in level if d.open for k in d.kids
                     if k.kids is not None and not k.xcl and k.nsel and self._visible(k)]
            for d in level:
                room = self._unfold(d, room)

    def focus_heavy(self):
        """Over budget: unfold only the folders that hold the bulk of the tokens and put the cursor
        on the one that dominates - the likely thing to untick."""
        root = self.m.root
        room = self._fold_all()
        n = root
        while True:  # follow the folder that holds at least half of its parent
            best = max((k for k in n.kids if k.kids is not None), key=lambda k: k.tsel, default=None)
            if best is None or best.tsel * 2 < n.tsel:
                break
            if n is not root:  # the way to the cursor is unfolded even if the screen then scrolls
                n.open = True
                room -= sum(1 for k in n.kids if self._visible(k))
            n = best
        heavy = [d for d in self._dirs(root) if d.tsel * 100 >= root.tsel * 15 and not d.open]
        for d in sorted(heavy, key=lambda d: (-d.tsel, d.depth)):
            room = self._unfold(d, room)
        self.cur = min(root.kids, key=_size_key) if n is root and root.kids else n
        self.top = 0

    def _dirs(self, d):
        for k in d.kids:
            if k.kids is not None:
                yield k
                yield from self._dirs(k)

    # drawing

    def resize(self):
        try:
            size = tuple(os.get_terminal_size(sys.stdout.fileno()))
        except (OSError, ValueError):
            size = self.size if self.size[0] else (80, 24)
        if size != self.size:
            self.size = size
            self.prev = []  # the terminal reflowed or cleared: repaint everything

    def frame(self):
        """The whole screen as a list of lines."""
        m = self.m
        if self.rows_at != m.structure:
            self.build_rows()
        self.resize()
        W, H = self.size
        h = self.body_height()
        cur = self.cursor_index()
        if cur >= self.top + h:  # keep the cursor on screen
            self.top = cur - h + 1
        self.top = max(0, min(self.top, cur, len(self.rows) - h))
        denom = max(self.budget, m.root.tsel, 1)
        lines = [self._header(W)]
        pos, size, _ = self.thumb() or (0, 0, 0)
        gap, mark = (" ", self.s("2", "▐")) if size else ("", "")  # the scrollbar: the last column, otherwise blank
        first, stop = self.top + pos, self.top + pos + size
        for i in range(self.top, self.top + h):
            edge = mark if first <= i < stop else gap
            lines.append(self._row(self.rows[i], W, i == cur, denom, edge) if i < len(self.rows) else "")
        lines.append(self._status(W))
        lines.append(self._hints(W))
        return lines[:H]

    def draw(self):
        """Repaint, sending only the lines that changed."""
        lines = self.frame()
        out = []
        if len(self.prev) != len(lines):
            self.prev = [None] * len(lines)
            out.append("\x1b[2J")
        for y, line in enumerate(lines):
            if line != self.prev[y]:
                # erase, then write: erasing after a line that fills the last column would wipe its last character
                out.append("\x1b[%d;1H\x1b[2K%s\x1b[0m" % (y + 1, line))
                self.prev[y] = line
        if out:
            sys.stdout.write("\x1b[?2026h" + "".join(out) + "\x1b[?2026l")
            sys.stdout.flush()
        self.dirty = False
        self.last_draw = perf_counter()

    def _header(self, W):
        m = self.m
        total, budget = m.root.tsel, self.budget
        over = total > budget
        code = "1;31" if over else "33" if total > 0.85 * budget else "32"
        tail = "counting…" if m.counting else "%s over" % fmt_tokens(total - budget) if over else "%d%%" % (100 * total // budget)
        parts = [(code, "~%s / %s tokens" % (fmt_tokens(total), fmt_tokens(budget)))]
        if W >= 64:
            parts.append((code, _bar(total / budget, 10, "░")))
        if W >= 44:
            parts.append((code if over and not m.counting else "2", tail))
        right_w = sum(len(t) for _, t in parts) + 2 * (len(parts) - 1) + 1
        left = _clip(" llmcopy  %s" % m.root.name, max(0, W - right_w - 1))
        gap = max(1, W - right_w - _width(left))
        self.meter_x = _width(left) + gap
        return self.s("1", left) + " " * gap + "  ".join(self.s(c, t) for c, t in parts) + " "

    def _row(self, n, W, is_cur, denom, edge=""):
        """One tree line: checkbox, indented name, [what it is], tokens, share-of-budget bar. `edge` is
        the scrollbar's cell; it takes the place of the last column, which is blank."""
        is_dir = n.kids is not None
        info = ""
        if is_dir:
            state = self.m.dir_checked(n)
            box = "[x]" if state == 2 else "[-]" if state == 1 else "[ ]"
            arrow = "▼" if (n.open or self.filter) and n.state == LOADED else "►"
            name = n.name + "/"
            chosen = state > 0
            tok = n.tsel if n.nsel else n.ttot
            rough = False
            if n.state == LOADING:
                info = "reading…"
            elif n.state == LAZY or (n.why and not n.nsel):
                info = "ignored"
            elif n.nsel and n.nsel != n.n:
                info = "%d/%d files" % (n.nsel, n.n)
            elif n.n:
                info = "%d file%s" % (n.n, "" if n.n == 1 else "s")
            counting = bool(info) and not info.startswith(("reading", "ignored"))
            folded = not (n.open or self.filter)
            if n.nheld and folded:  # a folded folder must not hide a held-back file
                held = "%d secret%s" % (n.nheld, "" if n.nheld == 1 else "s")
                info = held + " + " + info if info else held
                counting = False
        else:
            no_text = n.why in NOT_TEXT
            box = "[x]" if n.sel else "   " if no_text else "[ ]"
            arrow = " "
            name = n.name
            chosen = n.sel
            tok = 0 if no_text else n.tok
            rough = n.measured < 2
            counting = False
            if n.why == "secret":
                info = "secret: " + n.note if n.note else "secret"
            elif n.why and not n.sel:
                info = "ignored" if n.why == INHERITED else n.why
        tok_s = ("~" if rough else "") + fmt_tokens(tok) if tok else ""

        wide = W >= 60
        tail_w = 17 if wide else 8  # tokens(7) + space [+ bar(8) + space]
        lead = " %s %s%s " % (box, "  " * n.depth, arrow)
        info_w = 0
        if info and (W >= 76 or not counting):  # plain file counts are the first thing to go when narrow
            info = _clip(info, max(10, W // 3))
            info_w = _width(info) + 2
            if W - len(lead) - tail_w - info_w < 8:
                info, info_w = "", 0
        room = W - len(lead) - tail_w - info_w
        name = _clip(name, max(1, room))
        pad = " " * max(0, room - _width(name))
        info_cell = info + "  " if info_w else ""
        bar = _bar(tok / denom, 8) if (wide and chosen and tok) else " " * 8
        if is_cur:
            line = lead + name + pad + info_cell + tok_s.rjust(7) + (" " + bar if wide else "")
            return self.s("7", line) + edge if edge else self.s("7", line + " ")
        s = self.s
        warn = n.why == "secret" or (is_dir and n.nheld and folded)
        return (" " + s("32" if box == "[x]" else "33" if box == "[-]" else "2", box) + " " + "  " * n.depth
                + s("2", arrow) + " " + s(("1" if is_dir else "") if chosen else "2", name) + pad
                + s("33" if warn else "2", info_cell) + s("" if chosen else "2", tok_s.rjust(7))
                + (" " + s("36", bar) if wide else "") + (edge or " "))

    def _status(self, W):
        s = self.s
        if self.typing or self.filter:
            shown = sum(1 for n in self.rows if n.kids is None)
            text = " filter: %s%s" % (self.filter, "█" if self.typing else "")
            note = "   type part of a file or folder name"
            if self.filter:
                note = "   %d file%s" % (shown, " matches" if shown == 1 else "s match")
            return s("1;36", _clip(text, W - 1)) + s("2", _clip(note, max(0, W - 1 - _width(text))))
        if self.msg:
            return " " + s(self.msg_style, _clip(self.msg, W - 2))
        n = self.cur
        parts = [n.rel or n.name + "/"]
        if n.kids is not None:
            if n.state == LOADING:
                parts.append("reading…")
            elif n.state == LAZY:
                parts.append("ignored folder; space includes it, → looks inside")
            else:
                parts += ["%d of %d files selected" % (n.nsel, n.n), "%s tokens" % fmt_tokens(n.tsel)]
        elif n.why in NOT_TEXT:
            parts.append("%s file, cannot be pasted as text" % n.why)
        else:
            if n.tok:
                parts.append("%s%s tokens" % ("~" if n.measured < 2 else "", fmt_tokens(n.tok)))
            if n.why == "secret":
                parts.append("included although it may hold a secret" if n.sel
                             else "left out as a possible secret; space includes it anyway")
            elif n.lines:
                parts.append("%d lines" % n.lines)
            if n.why and n.why != "secret" and not n.sel:
                parts.append("left out by default (%s); space includes it" % ("inside an ignored folder" if n.why == INHERITED else n.why))
        return " " + s("2", _clip("  ·  ".join(parts), W - 2))

    def _hints(self, W):
        if self.typing:
            variants = [[("enter", "keep filter", "filter-keep"), ("esc", "clear", "filter-clear"),
                         ("space", "select", "toggle"), ("↑↓", "move", None)]]
        else:
            full = [("enter", "copy", "copy"), ("space", "select", "toggle"), ("a", "all/none", "all"), ("←→", "fold", None),
                    ("esc", "clear filter", "filter-clear") if self.filter else ("/", "filter", "filter"),
                    ("t", "sort by name" if self.m.sort_size else "sort by size", "sort"),
                    ("i", "hide ignored" if self.show_ignored else "show ignored", "ignored"),
                    ("b", "budget", "budget"), ("s", "save", "save"), ("q", "quit", "quit")]
            short = [(k, {"sort": "sort", "ignored": "ignored", "all": "all"}.get(a, label), a) for k, label, a in full]
            variants = [full, short] + [[h for h in short if h[2] and h[2] not in drop] for drop in (
                (), ("budget",), ("budget", "sort"), ("budget", "sort", "ignored", "filter", "filter-clear", "save"))]
        items = next((v for v in variants if sum(_width(k) + 1 + len(label) + 2 for k, label, _ in v) - 1 <= W), variants[-1])
        out, x = [" "], 1
        self.hints = []
        for key, label, action in items:
            w = _width(key) + 1 + len(label)
            if x + w > W:
                break
            if action:
                self.hints.append((x, x + w, action))
            out.append(self.s("1;32" if action == "copy" else "1", key) + " " + self.s("" if action == "copy" else "2", label) + "  ")
            x += w + 2
        return "".join(out).rstrip()

    # actions

    def say(self, text, style="2"):
        self.msg = text
        self.msg_style = style

    def move(self, delta):
        if self.rows:
            self.cur = self.rows[max(0, min(len(self.rows) - 1, self.cursor_index() + delta))]

    def fold(self, n, open_):
        self.folded_by_user = True
        if open_:
            self.m.expand(n)
        else:
            n.open = False
            self.m.structure += 1

    def act(self, action):
        m = self.m
        n = self.cur
        if action in ("toggle", "all"):
            self.edited = True
            over = m.root.tsel - self.budget
            text = m.toggle(n if action == "toggle" else m.root, self._match if self.filter else None)
            now = m.root.tsel - self.budget
            if text:
                self.say(text, "33")
            elif action == "toggle" and n.kids is None and n.sel and n.why == "secret":
                self.say("Included anyway: %s may contain a secret (%s)." % (n.name, n.note or "by its name"), "33")
            elif now > 0:
                self.say("%s over budget." % fmt_tokens(now), "33")
            elif over > 0 and m.root.nsel:
                self.say("Fits now, with %s to spare. Enter copies." % fmt_tokens(-now), "32")
        elif action == "copy":
            self.want_copy = True
        elif action == "quit":
            self.result = "quit"
        elif action == "sort":
            m.sort_size = not m.sort_size
            m.resort()
            self.say("Sorted by size: the heaviest selected items come first." if m.sort_size else "Sorted by name.")
        elif action == "ignored":
            self.show_ignored = not self.show_ignored
            if self.show_ignored and not m.reveal:
                m.reveal_ignored()
            m.structure += 1
            self.say("Showing what is left out by default (dimmed); space includes it." if self.show_ignored
                     else "Hiding what is left out by default.")
        elif action == "budget":
            self.budget = next((b for b in BUDGETS if b > self.budget), BUDGETS[0])
            remember_budget(self.budget)
            self.say("Budget: %s tokens per message (remembered)." % fmt_tokens(self.budget))
        elif action == "save":
            try:
                count = m.save_rules()
            except OSError as exc:
                self.say("Could not save: %s" % exc, "31")
                return
            self.edited = False
            self.say("Saved: this selection is now the default here (%s)." % RULES_FILE if count
                     else "Saved: back to the built-in defaults (no %s needed)." % RULES_FILE, "32")
        elif action == "filter":
            self.typing = True
        elif action in ("filter-keep", "filter-clear"):
            self.typing = False
            if action == "filter-clear":
                self.filter = ""
            m.structure += 1

    def on_key(self, name):
        n = self.cur
        self.msg = ""
        armed, self.esc_armed = self.esc_armed, False
        is_dir = n.kids is not None and n.parent is not None and not self.filter
        if name in ("up", "down"):
            self.move(1 if name == "down" else -1)
        elif name in ("pgup", "pgdn"):
            self.move(self.body_height() * (1 if name == "pgdn" else -1))
        elif name in ("home", "end"):
            self.move(len(self.rows) * (1 if name == "end" else -1))
        elif name == "right":
            if is_dir and not n.open:
                self.fold(n, True)
            else:
                self.move(1)
        elif name == "left":
            if is_dir and n.open:
                self.fold(n, False)
            elif n.parent is not None:
                self.cur = n.parent
        elif name == "tab":
            if is_dir:
                self.fold(n, not n.open)
        elif name == "space":
            self.act("toggle")
        elif name == "enter":
            self.act("filter-keep" if self.typing else "copy")
        elif name == "backspace":
            if self.typing:
                self.typing = bool(self.filter)
                self.filter = self.filter[:-1]
                self.m.structure += 1
        elif name == "esc":
            if self.typing or self.filter:
                self.act("filter-clear")
            elif self.edited and not armed:  # Esc is also "back": do not drop a changed selection on a stray press
                self.esc_armed = True
                self.say("Esc again quits without copying (s saves this selection for next time).", "33")
            else:
                self.act("quit")
        elif name == "ctrl-c":
            self.act("quit")

    def on_char(self, c):
        if self.typing:
            self.filter += c
            self.m.structure += 1
            return
        action = self.KEYS.get(c)
        if action in ("up", "down", "left", "right"):
            self.on_key(action)
        elif action:
            self.msg = ""
            self.esc_armed = False
            self.act(action)

    def on_click(self, x, y):
        W, H = self.size
        self.msg = ""
        i = self.top + y - self.HEAD
        bar = self.thumb()
        self.grab = None
        if bar and x >= W - 2 and self.HEAD <= y < H - self.FOOT:  # the scrollbar, or right beside it: it is thin
            line = y - self.HEAD - bar[0]
            self.grab = line if 0 <= line < bar[1] else bar[1] // 2  # taken where it was hit, else by its middle
            self.on_drag(y)
        elif y == H - 1:
            for x0, x1, action in self.hints:
                if x0 <= x < x1:
                    self.act(action)
        elif y == 0:
            if x >= self.meter_x:
                self.act("budget")
        elif y < H - self.FOOT and i < len(self.rows):
            n = self.cur = self.rows[i]
            if n.kids is None or x <= 4:  # a file anywhere, a folder on its box
                self.act("toggle")
            elif n.parent is not None and not self.filter:
                self.fold(n, not n.open)

    def on_drag(self, y):
        """The scrollbar's thumb follows the pointer."""
        bar = self.thumb()
        if self.grab is None or not bar:
            return
        pos, _, span = bar
        want = max(0, min(span, y - self.HEAD - self.grab))
        if want != pos:
            self.scroll((want * (len(self.rows) - self.body_height()) + span // 2) // span)

    def on_wheel(self, d):
        self.scroll(self.top + 3 * d)

    def scroll(self, top):
        """Show the rows from `top` on; the cursor comes along when it would be left behind."""
        h = self.body_height()
        self.top = max(0, min(top, len(self.rows) - h))
        i = self.cursor_index()
        if not self.top <= i < self.top + h:
            self.cur = self.rows[self.top if i < self.top else self.top + h - 1]

    def handle(self, ev):
        kind = ev[0]
        if kind == "wheel":
            self.on_wheel(ev[1])
        elif kind == "drag":
            self.on_drag(ev[2])
        elif kind != "resize":
            self.touched = True
            if kind == "key":
                self.on_key(ev[1])
            elif kind == "char":
                self.on_char(ev[1])
            else:
                self.on_click(ev[1], ev[2])
        self.dirty = True

    # main loop

    def settle(self):
        """Things that happen once, when the scan has caught up."""
        m = self.m
        if not self.expanded_once and not m.enumerating:
            self.expanded_once = True
            if not self.folded_by_user:
                self.auto_expand()
        if self.settled or m.counting or not self.expanded_once:
            return
        self.settled = True
        over = m.root.tsel - self.budget
        notes = []
        if over > 0 and not self.touched:  # the job is now to trim
            m.sort_size = True
            m.resort()
            self.focus_heavy()
            notes.append("%s over budget. Heaviest first: space unticks the highlighted item." % fmt_tokens(over))
        elif over > 0:
            notes.append("%s over budget (t sorts by size)." % fmt_tokens(over))
        else:
            if m.rules:
                notes.append("Using your saved selection (%s)." % RULES_FILE)
            if m.root.nheld:
                notes.append("%d file%s left out: possible secret." % (m.root.nheld, "" if m.root.nheld == 1 else "s"))
        if m.sort_size and self.touched:
            m.resort()
        if m.git_failed:
            notes.insert(0, GIT_FAILED + ".")
        if notes and not self.msg:
            self.say("  ".join(notes), "33" if (m.root.nheld or over > 0 or m.git_failed) else "2")
        self.dirty = True

    def run(self):
        m, term = self.m, self.term
        early = []
        while m.busy:  # a head start, so that a small project is complete in its very first frame
            m.pump(0.05)
            t = perf_counter() - _T0
            if t > FIRST_FRAME_MAX or (t > FIRST_FRAME and not m.enumerating):
                break
            early.extend(term.read(0.004))
        self.resize()
        self.settle()
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l" + term.on)  # alternate screen, no cursor, no wrapping
        try:
            for ev in early:
                self.handle(ev)
            while self.result is None:
                if self.want_copy:
                    if m.enumerating:
                        self.say("Finishing the scan, then copying…")
                    elif m.root.nsel:
                        self.result = "copy"
                        break
                    else:
                        self.want_copy = False
                        self.say("Nothing is selected yet: tick something with space first.", "33")
                since = perf_counter() - self.last_draw
                if self.dirty and (not m.busy or since >= 0.033):  # at most ~30 frames a second while results stream in
                    self.draw()
                if m.scan.out:
                    wait = 0.0
                elif self.dirty:
                    wait = max(0.001, 0.033 - since)
                else:
                    wait = 0.25 if m.busy else None  # idle: sleep until a key, a click or a resize
                for ev in term.read(wait):
                    self.handle(ev)
                if m.pump():
                    self.dirty = True
                self.settle()
        finally:
            sys.stdout.write(term.off + "\x1b[?7h\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
        return self.result


# -- command line ------------------------------------------------------------------------------

USAGE = """\
usage: llmcopy [folder] [-y] [-o FILE] [-b N]

Pick project files in a terminal tree and copy them to the clipboard, ready to
paste into ChatGPT, Claude, Gemini or any other AI chat.

  folder             project folder (default: the current one)
  -y, --yes          no picker: copy the default / saved selection right away
  -o, --output FILE  write to FILE instead of the clipboard ("-" = standard output)
  -b, --budget N     token budget for the meter, e.g. 50k, 200k, 1m
  -V, --version      -h, --help

keys:  up/down move    left/right fold    space select    a all/none    enter copy
       / filter    t sort by size    i hide/show ignored    b budget    s save default    q quit
mouse: click a box or a file to select, a folder to fold; wheel or scrollbar to scroll
"""


def usage_error(text):
    print("llmcopy: " + text, file=sys.stderr)
    raise SystemExit(2)


def parse_args(argv):
    opts = {"path": ".", "yes": False, "output": None, "budget": 0}
    args = list(argv)
    while args:
        a = args.pop(0)
        name, eq, value = a.partition("=")
        if a in ("-h", "--help"):
            sys.stdout.write(USAGE)
            raise SystemExit(0)
        if a in ("-V", "--version"):
            print("llmcopy", __version__)
            raise SystemExit(0)
        if a in ("-y", "--yes"):
            opts["yes"] = True
        elif name in ("-o", "--output", "-b", "--budget"):
            if not eq:
                if not args:
                    usage_error("%s needs a value" % a)
                value = args.pop(0)
            if name in ("-o", "--output"):
                opts["output"] = value
            else:
                try:
                    opts["budget"] = parse_tokens(value)
                except ValueError:
                    usage_error("cannot read the budget %r (try 100k)" % value)
        elif a.startswith("-") and a != "-":
            usage_error("unknown option %s (see llmcopy -h)" % a)
        else:
            opts["path"] = a
    return opts


def _color_ok(stream):
    if os.environ.get("NO_COLOR") or not _is_tty(stream):
        return False
    if os.name != "nt" or not stream.isatty():
        return True
    try:  # a classic console window needs escape sequences switched on
        import ctypes
        import msvcrt

        k = ctypes.WinDLL("kernel32")
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
        mode = ctypes.c_ulong()
        return bool(k.GetConsoleMode(handle, ctypes.byref(mode)) and (mode.value & 4 or k.SetConsoleMode(handle, mode.value | 4)))
    except Exception:
        return False


def main(argv=None):
    try:
        return _main(sys.argv[1:] if argv is None else argv)
    except KeyboardInterrupt:
        return 130


def _main(argv):
    opts = parse_args(argv)
    root = os.path.abspath(opts["path"]).replace("\\", "/").rstrip("/") or "/"
    if not os.path.isdir(root):
        print("llmcopy: %s is not a folder" % opts["path"], file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # No picker without a terminal. A pipe on the way out gets the text itself (`llmcopy | less`);
    # no keyboard is an error rather than a silent copy.
    to_stdout = opts["output"] == "-" or (opts["output"] is None and not _is_tty(sys.stdout))
    interactive = not opts["yes"] and not to_stdout
    if interactive and not _is_tty(sys.stdin):
        print("llmcopy: the picker needs a terminal. Use -y to copy the default selection without it.", file=sys.stderr)
        return 2
    if interactive and os.name == "nt" and not sys.stdin.isatty():
        # Git Bash without ConPTY: there is no Windows console to read keys from. winpty provides one.
        from shutil import which

        if which("winpty") and not os.environ.get("LLMCOPY_WINPTY"):
            import subprocess

            return subprocess.call(["winpty", sys.executable, os.path.abspath(__file__)] + list(argv),
                                   env=dict(os.environ, LLMCOPY_WINPTY="1"))
        print("llmcopy: this terminal has no Windows console. Start it as `winpty llmcopy`, or use -y.", file=sys.stderr)
        return 2

    budget = opts["budget"] or saved_budget() or DEFAULT_BUDGET
    model = Model(root, load_rules(root), reveal=interactive)  # the scan starts now, in the background
    result = "copy"
    if interactive:
        try:
            term = open_terminal()
        except Exception as exc:
            print("llmcopy: cannot use this terminal (%s). Use -y to copy the default selection." % exc, file=sys.stderr)
            return 2
        model.scan.wake = term.wake
        ui = UI(model, term, budget, color=not os.environ.get("NO_COLOR"))
        try:
            result = ui.run()
        finally:
            term.close()
        budget = ui.budget
    else:
        model.finish()

    err = sys.stderr
    color = _color_ok(err)

    def paint(code, text):
        return "\x1b[%sm%s\x1b[0m" % (code, text) if color else text

    if result != "copy":
        print("Nothing copied.", file=err)
        return 1
    text, info = build_output(model)
    if not info["files"]:
        print("llmcopy: nothing to copy here (no readable text files selected).", file=err)
        return 1
    verb, where = "Copied", "to the clipboard"
    if to_stdout:
        raw = getattr(sys.stdout, "buffer", None)
        if raw is not None:  # bytes: no newline translation on Windows
            raw.write(text.encode("utf-8", "replace"))
        else:
            sys.stdout.write(text)
        sys.stdout.flush()
        verb, where = "Wrote", "to standard output"
    elif opts["output"]:
        with open(opts["output"], "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        verb, where = "Wrote", "to " + opts["output"]
    else:
        try:
            if copy_to_clipboard(text) == "terminal":
                where = "through the terminal (OSC 52; if pasting gives nothing, use -o FILE)"
        except OSError as exc:
            import tempfile

            fd, path = tempfile.mkstemp(prefix="llmcopy-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            print(paint("33", "Could not reach the clipboard: %s" % exc), file=err)
            verb, where = "Wrote", "to " + path + " instead"

    tokens = info["tokens"]
    print("%s %d file%s, ~%s tokens, %s." % (paint("1;32", verb), info["files"], "" if info["files"] == 1 else "s",
                                            fmt_tokens(tokens), where), file=err)
    notes = [GIT_FAILED] if model.git_failed else []
    if tokens > budget:
        notes.append("%s over your %s budget - the chatbot may cut it off" % (fmt_tokens(tokens - budget), fmt_tokens(budget)))
    notes += ["not included: %s - possible secret (%s)" % held for held in info["held"]]
    notes += ["could not read %s" % rel for rel in info["failed"][:5]]
    for note in notes:
        print(paint("33", "  ! " + note), file=err)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
