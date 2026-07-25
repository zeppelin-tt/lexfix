# LexFix

Menu-bar tool for macOS that fixes garbled English fragments in text via a
global hotkey. Select a word, press **⌘⇧S**, get a popup with ranked
replacement candidates — dictionary matches, learned rules, and free-form
LLM guesses — pick one with the arrow keys and hit Enter, or type your own.

Built originally as a companion to a local Russian speech-to-text pipeline
(GigaAM), where the model transliterates English brand/tool names into
Cyrillic-looking Latin garbage (`Editers` instead of `Editors`). It has no
dependency on that pipeline, though — it works on any selected text in any
app, from any source (typed, pasted, dictated).

> **This README is written to be followed mechanically, including by an AI
> coding agent with shell access.** Every step has a concrete command and an
> expected result. If you're an agent installing this for a human: run the
> commands in order, show the human the output, and stop at the one step
> that genuinely requires their input (granting Accessibility — see step 5).

## What it does

- **⌘⇧S** opens a popup pre-filled with whatever's selected in the
  frontmost app (via a simulated ⌘C, not the system clipboard history).
- Looks up the word against a local dictionary (exact match, fuzzy match,
  transliteration-skeleton match for Cyrillic-typed English words).
- If enabled, asks a local LLM (via [Ollama](https://ollama.com), fully
  offline, no data leaves the machine) to (a) rerank dictionary candidates,
  (b) guess spellings for words not in any dictionary at all — reasoning
  about spelling mistakes and transliteration, not just rearranging letters.
- Shows up to 6 ranked variants, each labeled with its source and trust
  level: `правило` (a rule you confirmed before), `словарь` (dictionary
  hit), `догадка` (LLM guess — unverified, shown for you to judge).
- One Enter applies the top pick; ↑↓ or a click picks another; typing over
  the field ignores all suggestions.
- A second field lets you teach it a brand-new proper noun (not a
  correction — this makes the fuzzy-match layer aware the word exists at
  all, so future typos in it get caught automatically).

## Requirements

- macOS (uses AppKit/PyObjC + the Accessibility API — no other platform).
- Python 3.12 (installed automatically via Homebrew if missing).
- [Homebrew](https://brew.sh) — only if Python 3.12 isn't already present.
  `install.sh` does **not** install Homebrew itself (its official installer
  needs `sudo` and an interactive password prompt, which is exactly the kind
  of system-level, credential-requiring step this script deliberately
  doesn't automate) — if neither is present, it stops and prints the two
  one-line options (install Homebrew, or install Python 3.12 directly from
  python.org) instead of guessing.
- [Ollama](https://ollama.com) — **optional**. Without it, LexFix still
  does exact/fuzzy/transliteration dictionary correction; it just skips the
  LLM reranking and free-guess layers, and homonym resolution (see below).
- No GPU, no torch, no ML framework beyond Ollama itself. This repo has no
  runtime dependency on GigaAM or any speech-to-text system.

## Install

```bash
git clone https://github.com/zeppelin-tt/lexfix.git
cd lexfix
bash install.sh
```

`git clone` itself has one first-run wrinkle worth knowing about: on a
genuinely brand-new Mac that has never run any developer tool, the very
first invocation of `git` pops up a **macOS system dialog** offering to
install Command Line Tools — a GUI prompt, not something a script can click
through. If that appears, install the tools it offers (or run
`xcode-select --install` yourself) and re-run the clone once that finishes.

`install.sh` is safe to run **non-interactively** (no terminal attached,
e.g. invoked by an agent's command-execution tool rather than a real shell)
— every step that would otherwise ask a yes/no question detects the missing
terminal and picks the conservative default (skip, don't guess) instead of
hanging or silently downloading something large without anyone agreeing to
it.

It's also idempotent — safe to re-run after a `git pull` or if a
previous run stopped partway through. It does, in order:

1. **Finds or installs Python 3.12** (via Homebrew if not already present).
2. **Creates `venv/`** and installs `requirements.txt` (PyObjC bindings +
   `py2app`; nothing else — the correction engine itself is stdlib-only
   except for the Ollama HTTP call).
3. **Builds the dictionary** (`lexicon.json`, `ru_stop.txt`,
   `homonyms.txt`) from the files already in this repo (`tech_terms.txt`,
   `vocab/*_curated.txt`, `vocab/dev_languages.txt`) — works fully offline,
   skipped if already built. See [Customizing the dictionary](#customizing-the-dictionary)
   for adding your own terms or a music-artist layer.
4. **Creates a local code-signing certificate** (`LexFix Local Signing`) if
   one doesn't already exist in your keychain — see
   [Why a certificate at all](#why-a-certificate-at-all) below. Tries the
   fully automated CLI path first; if your keychain blocks non-interactive
   import, it prints exact manual steps (Keychain Access → Certificate
   Assistant) and asks you to re-run `install.sh` after.
5. **Registers a LaunchAgent** (`~/Library/LaunchAgents/local.lex-widget.plist`)
   so LexFix starts automatically at login.
6. **Builds and installs `LexFix.app`** to `/Applications`, signs it, and
   launches it (via `build_app.sh`).
7. **Offers to install Ollama + `qwen2.5:7b`** (~4.7 GB) if not already
   present — asks for confirmation, skippable.

At the end it prints the one step it *cannot* automate: granting
Accessibility permission (step below).

### Step that needs a human: Accessibility permission

macOS will not let any process grant itself Accessibility trust — this is
by design, and there's no scriptable workaround (any tool that claimed to
do this non-interactively should be treated as suspicious).

**System Settings → Privacy & Security → Accessibility → enable LexFix**
(click `+` and pick `/Applications/LexFix.app` if it's not already listed).

Without this, LexFix still opens on ⌘⇧S and the hotkey still works, but the
word you had selected won't auto-fill the popup (the simulated ⌘C that
reads your selection needs this permission). Everything else — typing a
correction manually, teaching new names — works regardless.

### Verify

```bash
bash status.sh
```

Checks the dictionary, the app's code signature, whether the process is
running, and whether Ollama is available.

## Why a certificate at all

`codesign -s -` (ad-hoc signing) recomputes the signature from the binary's
contents on every build. macOS's permission database (TCC) grants
Accessibility to a specific *signing identity*, so an ad-hoc-signed app
loses its grant every time you rebuild — even for an unrelated one-line
change. A stable, self-signed **code-signing certificate** produces the
same signature across rebuilds, so the Accessibility grant survives.

This is a certificate for *your own machine only* — it authenticates
"this binary came from the same signing key as last time," not "this
software is vetted by anyone." It is never sent anywhere and never needs
to be. `install.sh` creates it automatically via `openssl` + `security`;
if that's blocked in your environment (some keychains refuse
non-interactive trust changes), create it by hand:

```
Keychain Access → menu Keychain Access → Certificate Assistant → Create a Certificate…
  Name: LexFix Local Signing
  Identity Type: Self Signed Root
  Certificate Type: Code Signing
  (leave everything else default → Continue → Create → Done)
```

Then re-run `bash install.sh`.

⚠️ If you ever rebuild after *changing the signing setup itself* (new
certificate, different bundle ID), the Accessibility grant made for the old
identity goes stale silently — remove LexFix from the Accessibility list
(`−` button) and re-add it (`+`). `bash status.sh` tells you if the
signature is invalid; it can't tell you if the grant is stale for a
signature that's technically valid but different from what TCC remembers,
so if the popup stops auto-filling right after a signing change, try the
remove-and-re-add first.

## Usage

| Action | How |
|---|---|
| Fix a word | Select it anywhere → **⌘⇧S** → pick a variant (↑↓ or click) → **Enter** |
| Type your own | Just type over either field — no variant fits, no problem |
| Teach a new proper noun | Bottom field, type the correct spelling → **Enter** (this doesn't force a rule, it teaches the fuzzy matcher the name exists) |
| Right-click the ✎ icon | Open `learned.json`, force a dictionary rebuild, quit |

### Command-line (no popup)

```bash
venv/bin/python3 lex.py fix "editers" "Editors"     # hard rule
venv/bin/python3 lex.py add "Nine Inch Nails"        # teach a name (fuzzy layer handles typos after)
venv/bin/python3 lex.py block kiss                   # stop correcting this token
venv/bin/python3 lex.py why editers                  # explain a past decision
venv/bin/python3 lex.py test "включи editers"        # dry-run correction on a string
venv/bin/python3 lex.py list                          # your rules
```

`fix` vs `add`: `fix` is one hard mapping for a recurring, specific typo.
`add` teaches the *name itself*, after which the fuzzy layer catches *any*
typo in it on its own.

## Customizing the dictionary

Everything the dictionary is built from lives in this repo already
(`tech_terms.txt`, `vocab/*.txt`) except one **optional, personal** layer:

```bash
LEXFIX_SCROBBLES=~/path/to/scrobbles.jsonl venv/bin/python3 build_lexicon.py
```

If you have a Last.fm listening-history export (JSONL, one object per
scrobble with an `"artist"` key), this adds band/artist names weighted by
play count — useful if you dictate or type about music a lot. Skip it and
the dictionary still builds fine from the curated + tech-term sources; this
is why it's not wired up by default in `install.sh`.

To add your own always-on terms permanently: edit `tech_terms.txt` (one
name per line) and rerun `venv/bin/python3 build_lexicon.py`, or use
`lex.py add "Name"` at runtime (does the same thing, no manual rebuild).

To refresh the auto-downloaded language/software list from upstream
sources (needs internet, rarely necessary):

```bash
venv/bin/python3 fetch_sources.py
```

This never touches `vocab/*_curated.txt` — those are hand-maintained and
survive any refetch.

## How correction decides (architecture, short version)

Three trust tiers, cheapest/most-certain first:

1. **Exact dictionary match** — instant, 0 LLM calls.
2. **Fuzzy match** (`difflib`, ratio ≥ 0.88, single unambiguous candidate)
   — instant, 0 LLM calls.
3. **LLM as reranker** — given the phrase and a numbered list of
   dictionary candidates, the model returns *one digit* via a JSON-Schema
   constrained response. It cannot return free text here — there's nothing
   to inject or hallucinate into, it's picking an index.

This is the layer used for **automatic, unattended** correction — e.g. if
you wire this engine into your own pipeline the way the original GigaAM
bridge does. The model is never allowed to freely generate text that gets
applied without a human looking at it first.

The **popup's variant list** (`corrector.suggest_variants()`) is the one
deliberate exception: there, the LLM *is* allowed to freely guess a
spelling it thinks you meant (`aifel tover` → `Eiffel Tower`), because the
guess is only ever *displayed*, never applied, until you click it and hit
Enter. The boundary isn't "how good is the model" — it's "does the result
get applied without confirmation."

Cyrillic homonyms (`докер`/`Docker`, `питон`/`Python`, `флаттер`/`Flutter`)
are resolved by the LLM from sentence context and are intentionally never
memorized as a hard rule — the correct answer depends on the sentence, not
the word.

## Troubleshooting

**Selected word doesn't appear in the popup, but the hotkey opens it fine.**
Almost always a signature problem, not a permissions problem you can see in
System Settings — the checkbox can be *on* while `AXIsProcessTrusted()`
still returns false, because TCC matches by signing identity, and a broken
or ad-hoc signature doesn't match anything.
```bash
codesign --verify --strict --verbose=2 /Applications/LexFix.app
```
If this fails: `bash build_app.sh`, then remove-and-re-add LexFix in
Accessibility settings (a broken-signature grant doesn't self-heal once
the signature becomes valid — the OS needs the removal to clear stale
state).

**Rebuilt after editing `menubar.py` and it's acting on old code.**
`menubar.py`/`hotkey.py`/`setup.py` are compiled into the `.app` bundle at
build time — you must `bash build_app.sh` after touching them.
`corrector.py`/`lex.py`/`translit.py` and the dictionary files are *not*
bundled — the app reads them live from this directory on every launch (see
`LexFixProjectDir` in `setup.py` / `_project_dir()` in `menubar.py`), so
editing those needs no rebuild, just `lex.py test "..."` or a popup retry.

**LLM suggestions never appear, only dictionary matches.**
```bash
curl -s http://localhost:11434/api/tags | grep qwen2.5:7b
```
If empty: `ollama pull qwen2.5:7b` (Ollama itself must also be running —
`brew services start ollama`, or `ollama serve` in a terminal). This is a
soft dependency — LexFix works without it, just with fewer/no free-guess
variants and no homonym resolution.

**`build_app.sh` fails with "нет venv/".**
Run `bash install.sh` first (or manually: `python3.12 -m venv venv &&
venv/bin/pip install -r requirements.txt`).

## Uninstall

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.lex-widget.plist
rm ~/Library/LaunchAgents/local.lex-widget.plist
rm -rf /Applications/LexFix.app
security delete-certificate -c "LexFix Local Signing" ~/Library/Keychains/login.keychain-db
```
Then remove LexFix from Accessibility settings, and delete this cloned
directory whenever you like — nothing outside it was touched.

## License

MIT — see [LICENSE](LICENSE).
