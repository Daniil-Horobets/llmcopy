# llmcopy

```sh
pipx install llmcopy
```
```sh
uv tool install llmcopy
```

Either one gives you a `llmcopy` command that works from any folder and lives in its own environment, so it never
mixes with your projects' packages. pipx is the long-standing tool for exactly this; uv is newer, faster, and
fetches Python itself if the machine has none. `pip install llmcopy` works too, but installs into whatever Python
`pip` belongs to. Python 3.9+, nothing else; Windows, macOS and Linux. Then run `llmcopy` in a project folder.

![llmcopy in a terminal](https://raw.githubusercontent.com/Daniil-Horobets/llmcopy/main/screenshot.png)

Copy a code project to the clipboard, ready to paste into ChatGPT, Claude, Gemini or any other AI chat.
Glance at the tree, press **Enter**, paste.

- Knows what belongs to the project: asks git, and leaves out dependencies, build output, lock files, binaries.
  What is left out stays visible, dimmed, and can be ticked; `i` hides it.
- Counts tokens per file and per folder against a budget. When the project is too big for one message, the
  heaviest folders come first and the cursor is already on the biggest one: **Space** unticks it.
- Keyboard and mouse. Every key is listed in the bottom line, and the bottom line is clickable.
- `s` saves the selection as the default for this folder. `a` selects or clears everything. `/` filters by name.
- Files that look like they contain credentials are held back and marked.
- Starts in about 0.1 s. One file, no dependencies.

## For LLMs

[LLMS.md](LLMS.md) describes everything - behaviour, options, file formats, the structure of the code - in one
document written to be read by an AI assistant. Give it to yours and ask.

MIT licence.
