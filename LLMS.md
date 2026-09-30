# llmcopy - reference for LLMs

This file is the complete description of llmcopy: what it does, how it behaves, and how the code is organised.
It is written for an AI assistant that has to answer questions about the tool or change it.

## What it is

A terminal program. Run in a project folder, it shows the project as a tree with checkboxes and token counts, and
on Enter copies the selected files to the clipboard as one text block, ready to paste into an AI chat. It is a single
Python file (`llmcopy.py`, Python 3.9+, standard library only) for Windows, macOS and Linux.

Three situations drive the design:

| situation | what the user does |
|---|---|
| the project fits in one chat message | `llmcopy`, Enter |
| it does not fit | the tree opens sorted by size with the cursor on the dominant folder: Space, Enter |
| only a slice is wanted | `a` (none), `/text`, Enter, `a` (all matches), Enter |

## Install and run

```
pipx install llmcopy        # or: uv tool install llmcopy / pip install llmcopy
llmcopy                     # picker for the current folder
llmcopy path/to/project
llmcopy -y                  # no picker: copy the default (or saved) selection
llmcopy -o out.txt          # write a file instead of the clipboard; "-o -" prints to standard output
llmcopy -b 200k             # token budget for the meter (25k ... 1m)
llmcopy -V | -h
```

`llmcopy.py` is self-contained: `python llmcopy.py` works without installing. Exit codes: 0 copied, 1 nothing
copied (quit, or nothing selectable), 2 usage or terminal problem, 130 interrupted.

Without a terminal: if standard output is a pipe or file, the text goes there (`llmcopy | less`) and no picker is
shown; if there is no keyboard (standard input is not a terminal) and `-y` was not given, it refuses with exit 2
rather than copying silently.

## The screen

```
 llmcopy  shopfront                              ~71.1k / 100k tokens  ███████░░░  71%     header: meter
 [x] ▼ shopfront/                                        52 files    71.1k ██████          the project root
 [x]   ▼ api/                                            23 files    57.9k ████▌           folder: files, tokens, share
 [ ]       config.py        secret: AWS access key, line 70            664                  held back, yellow
 [x]       db.py                                                       638
 [ ]     .env                                              secret      ~19                  left out by default, dimmed
 [ ]     package-lock.json                              lock file   ~14.3k
 1 file left out: possible secret.                                                          status line
 enter copy  space select  a all  ←→ fold  / filter  t sort  i ignored  b budget  s save  q quit
```

- Header: selected tokens / budget, a bar, and a percentage; red with "N over" when over budget; "counting…"
  while files are still being counted.
- Row: checkbox (`[x]` all, `[-]` some, `[ ]` none; blank for files that are not text), fold arrow, name, an
  info cell (file count for folders, the reason for entries left out), tokens (`~` = rough, from the file size), and
  a bar showing the share of the budget.
- Status line: details of the entry under the cursor, or the last message, or the filter being typed.
- Hint line: the keys. Each hint is also a button. It shortens itself in narrow terminals.
- Layout adapts from 24 columns upwards; columns are dropped, nothing wraps.

| key | mouse | action |
|---|---|---|
| up / down, `k` / `j`, PgUp / PgDn, Home / End | wheel | move |
| space | click a checkbox, or a file row | select / unselect a file or a whole folder |
| right / left, `l` / `h`, Tab | click a folder name | unfold / fold (left on a file jumps to its folder) |
| enter, `c` | click `enter copy` | copy the selection and quit |
| `a` | | all / none (with a filter: all / none of the matches) |
| `/` | | filter by substring of the path; several words must all match; Enter keeps the filter, Esc clears it |
| `t` | | sort by size / by name |
| `i` | | hide / show what is left out by default |
| `b` | click the meter | next budget: 25k, 50k, 100k, 200k, 400k, 1M (remembered) |
| `s` | | save the selection as this folder's default |
| `q`, Esc, Ctrl+C | | quit without copying; Esc asks once more if the selection was changed |

## What is selected by default

Inside a git repository the file list comes from `git ls-files --cached --others --exclude-standard`: tracked files
plus untracked files that are not ignored. So `.gitignore` (nested ones, global excludes) is honoured exactly. A
repository found inside a plain folder is asked the same way. Outside a repository the folder is walked and a
built-in list decides. If git is missing or refuses the repository (for example "dubious ownership" on a drive
that does not record owners), the folder is walked as well and a note says that `.gitignore` is not applied.

Everything not selected by default carries a reason and is still listed, dimmed; `i` hides these entries.

| reason | what | can be ticked |
|---|---|---|
| `ignored` | ignored by git; folders `node_modules .venv venv __pycache__ vendor third_party .idea .vscode .next .gradle …` anywhere; without git also `dist build out target obj coverage tmp temp env _build`, `*.egg-info`, and any folder containing `pyvenv.cfg` | yes (a folder is read when ticked or unfolded) |
| `lock file` | `package-lock.json yarn.lock pnpm-lock.yaml poetry.lock uv.lock Cargo.lock go.sum *.lock …` | yes |
| `generated` `source map` `snapshot` `log` `data` `translation` `svg` | `*.min.js *.min.css *.bundle.js *_pb2.py *.pb.go *.g.dart …`, `*.map`, `*.snap`, `*.log`, `*.csv *.tsv *.jsonl`, `*.po`, `*.svg` | yes |
| `license` `changelog` | `LICENSE* COPYING`, `CHANGELOG HISTORY CHANGES RELEASES NEWS` (`.md .txt .rst` or no extension) | yes |
| `large` | text files over 256 KB | yes |
| `secret` | `.env*` (not `.env.example` and similar), `*.pem *.key *.p12 *.pfx *.jks`, `id_rsa …`, `.netrc`, `secrets.*`; files whose content has a private key block or an AWS, GitHub, OpenAI/Anthropic-style `sk-`, Slack, Google, Stripe live, GitLab or npm credential (placeholders such as `…EXAMPLE` are skipped) | yes, per file only |
| `image` `font` `archive` `media` `document` `binary` | by extension, or a NUL byte in the first 8 KB (UTF-16 text with a byte-order mark counts as text) | no |

Rules of selection:

- Ticking a folder selects its ordinary files; entries with a reason inside it stay out, and those ticked by hand
  before are unticked again. Ticking an ignored folder itself includes its contents (nested ignored folders still
  stay out); unticking it makes it an ignored folder again.
- Ticking a single file always wins, whatever its reason.
- A file with a possible secret is never included unless that file itself was ticked; this is checked again on the
  actual bytes when copying.
- `a` is that folder rule applied to the whole project. With nothing or only some things selected it selects
  everything except what is left out by default (ignored, lock files, secrets, …), also if such an entry was ticked
  by hand. With everything selected it clears everything, hand-ticked entries included.

## Over budget

When the scan finishes and the selection exceeds the budget (and no key was pressed yet), the tree is sorted by
size, folded except for the folders that hold at least 15% of the tokens, the path to the folder that holds at
least half of its parent is unfolded, and the cursor is put on that folder. Unticking updates the header; the status
line says how much is still over, or "Fits now". The order does not change while unticking. Copying while over
budget is allowed and reported.

## Saved selection: `.llmcopy`

`s` writes `.llmcopy` in the folder llmcopy was started in (or removes it if the selection equals the defaults):

```
# llmcopy: what to copy from this folder by default.
- docs/
- api/tests/fixtures/
+ package-lock.json
```

`+ path` always include, `- path` never include; folders end with `/`; `- ./` means nothing is selected unless a
`+` rule says so. Only differences from the defaults are stored, so a new file in an excluded folder stays out and
a new file elsewhere is included. The file can be edited by hand or committed. `llmcopy -y` uses it.

The token budget is remembered per user, not per project: `%APPDATA%\llmcopy\budget` on Windows,
`$XDG_CONFIG_HOME/llmcopy/budget` or `~/.config/llmcopy/budget` elsewhere.

## Output format

```
Project "shopfront": 50 files. The file tree comes first, then each file in a <file path="..."> block. Entries marked "(omitted)" exist in the project but their contents are not included.

<tree>
shopfront/
  api/
    routes/
      auth.py
    tests/
      fixtures/  (omitted)
    config.py  (omitted)
  README.md
</tree>

<file path="api/routes/auth.py">
...file content...
</file>

<file path="README.md">
...
</file>
```

- One format. Path-tagged blocks cannot collide with Markdown fences inside files.
- Files appear in name order, folders first. Line endings are normalised to `\n`. Text is decoded as UTF-8
  (byte-order mark removed), UTF-16 with a byte-order mark, or Windows-1252 as a last resort.
- The tree lists included files and marks ordinary files and folders that were unticked, and files held back as
  possible secrets, with `(omitted)`. Things left out by default for other reasons are not mentioned.
- After copying, a summary is printed: files, tokens, where it went, and warnings (over budget, files held back as
  possible secrets, unreadable files).

## Token counts

Counts estimate OpenAI's `o200k_base` tokenizer with a linear model over ten counts per file (words, camelCase
humps, digits, punctuation characters, punctuation runs, lines, indented lines, non-ASCII bytes, letters,
underscores). Measured on 21 open-source repositories (42.6M tokens) against tiktoken, on repositories the model was
not fitted on: project total off by 3.8% on average, 10.7% at worst; characters/4 is off by 7.7% and 23.8%. Other
vendors' tokenizers give noticeably more tokens for the same code, so the budget is a gauge, not an exact limit.
Each file's count includes about ten tokens for its `<file>` wrapper and tree line.

## Platforms

| where | input | clipboard |
|---|---|---|
| Windows: Windows Terminal, PowerShell, cmd, VS Code, Git Bash, classic console window | console input records (`ReadConsoleInputW`): keys, mouse, resize | native clipboard API through ctypes |
| Git Bash without ConPTY (older Git for Windows) | no console there: llmcopy restarts itself under `winpty` if it is on PATH | same |
| macOS | termios raw mode, xterm mouse reports (SGR 1006) | `pbcopy` |
| Linux | same as macOS | `wl-copy`, `xclip` or `xsel`; on a desktop without them it says which to install and writes a temporary file |
| WSL | same as Linux | `clip.exe` |
| remote shell without a display | same as Linux | asks the terminal with OSC 52 and says so |
| Cygwin / MSYS Python | same as Linux | `/dev/clipboard` |

Only glyphs that every Windows console font has are used (`► ▼ █ ▌ ░ … ·`). Colours are the 16 ANSI colours plus
bold, dim and reverse; `NO_COLOR` drops the hues. The terminal state is restored on exit, on Ctrl+C and on
SIGTERM/SIGHUP.

Verified by tests and scripted sessions in: ConPTY with PowerShell, cmd and Git Bash; a real mintty window with
and without ConPTY; a classic console window; a Linux pseudo-terminal (WSL). Python 3.9, 3.10, 3.12 and 3.13.
macOS shares the POSIX code path with Linux; its `pbcopy` branch is covered by a test with a stand-in tool.

## Speed

Measured on a laptop (Windows 11, NVMe), median, from process start to the first frame: 85 ms for 52 files, 126 ms
for 220 files (flask), 119 ms for 2,578 (vite), 132 ms for 4,844 (rails), 260 ms for 4,304 (django; git itself
needs 140 ms there). Counting every file continues in the background: 0.47 s for vite, 1.05 s for rails. The
entries git ignores are listed last, about 0.1 s after that (0.3 s for django); the totals do not wait for them.
On files never read before (fresh clone) a 2,920-file repository is usable after 0.14 s and fully counted after
1.7 s.

Why it is fast - each point was measured, and alternatives were dropped when they lost:

- No framework and lazy imports: a TUI library costs more to import than the whole path to the first frame.
- git is started before anything else, with `CreateProcess` / `posix_spawn` directly (`import subprocess` alone
  costs ~15 ms on Windows), and read on the scanner thread.
- Files are read with `os.open`/`os.read` in 64 KiB requests without `stat` (1.5x faster than `open().read()`;
  1 MiB requests are ~5x slower because `os.read` allocates the whole request).
- One reader thread for cached files (threads are slower there), sixteen as soon as opens turn out to be slow
  (first read after a clone or checkout: ~15x faster).
- Results reach the interface in batches; folder totals are running sums updated along the path to the root; a
  frame formats only the rows on screen (0.15 ms) and only changed lines are written.
- A small project is complete in its first frame: the interface waits up to 90 ms for the scan before drawing.
- Installed through pip/pipx/uv the module's bytecode is cached; `python llmcopy.py` compiles it on every start
  (~20 ms).

## Code map (`llmcopy.py`, top to bottom)

| part | contents |
|---|---|
| constants | `DEFAULT_BUDGET`, `BUDGETS`, `MAX_BYTES`, `COLD_THREADS`, `BATCH`, `FIRST_FRAME` |
| what is left out | `VCS_DIRS`, `JUNK_DIRS`, `MAYBE_JUNK_DIRS`, name/extension tables, `name_reason(name)` |
| token estimate | `token_features(bytes)`, `estimate_tokens(bytes)`, coefficients `_COEF` |
| secrets | `_SECRETS` (label, literal fragments, regex), `find_secret(bytes)` |
| reading | `read_file(path, limit)`, `decode_text`, `measure(path, full)` -> `(why, tokens, size, lines, note)` |
| scanner | `list_dir`, `start_git`, `in_git_repo`, class `Scanner` |
| tree | class `Node`, `_contrib`, class `Model` |
| output | `build_output(model)` -> `(text, info)` |
| clipboard | `_clip_windows`, `copy_to_clipboard` |
| terminal | `_WinTerm`, `_PosixTerm`, `open_terminal`, `_msys_pty`, `_is_tty` |
| interface | formatting helpers, class `UI` |
| command line | `USAGE`, `parse_args`, `main` |

Threads. The main thread owns the tree and the screen. `Scanner` threads only do I/O: tasks `("git", node, fn)`,
`("ls", node)`, `("tok", node, full)` go in, `(kind, node, result)` tuples come out through `Scanner.out`, and
`Model.pump()` applies them on the main thread. The scanner wakes the main loop through `term.wake()` (an event on
Windows, a pipe elsewhere). Three queues set the order: listings that shape the project, then file measurements,
then the listings that only add what git ignores (`Model.counting` turns false once the first two are empty, `busy`
once all three are).

`Node` fields: `kids` (`None` for files), `rel` (path from the root, folders end with `/`), `why` (reason it is
outside the default selection, `""` if none), `sel`, `pin` (decided by the user or a rule), `tok`, `measured`
(0 no, 1 rough, 2 counted), `state` (`LAZY`, `LOADING`, `LOADED`), `open`; for folders `on` (new children start
selected), `xcl` (ignored and not ticked), `git` (listed by git) and the running totals `n` (candidates), `nsel`,
`ttot`, `tsel`, `nv` (entries that belong to the project), `nheld` (possible secrets held back).

`Model`: `_apply_git` builds a subtree from git's list; `_apply_ls` adds a folder listing (for a git folder these
are the ignored entries); `_apply_tok` stores a measurement; `toggle`, `set_dir`, `_force` change the selection;
`collect_rules` / `save_rules` / `load_rules` handle `.llmcopy`; `finish()` blocks until the scan is done (used by
`-y`). Invariant: every folder's totals equal the sum over its files of `_contrib(file)`.

`UI`: `build_rows` flattens the visible tree; `frame()` returns the screen as a list of lines, `draw()` writes the
changed ones; `auto_expand` and `focus_heavy` choose what is unfolded; `act(name)` performs a command; `on_key`,
`on_char`, `on_click`, `on_wheel` translate input; `settle()` runs once when the scan is complete; `run()` is the
loop. A terminal object has `read(timeout) -> events`, `wake()`, `close()` and the strings `on` / `off`; events are
`("key", name)`, `("char", c)`, `("click", x, y)`, `("wheel", ±1, x, y)`, `("resize",)`.

Tests: `python -m pytest` (`tests/`): selection logic in git and plain folders, rules round trip, output, secrets,
the interface driven without a terminal, line widths from 24 to 160 columns, clipboard tools.

## Limits

- Token counts are estimates of one vendor's tokenizer.
- Secret detection covers a few unmistakable formats; it is a safety net, not a scanner.
- Outside git, what counts as build output is decided by folder names.
- What a chat page does with a very long paste is up to that page.
