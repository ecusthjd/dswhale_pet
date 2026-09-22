<h1 align="center">Big Fat Fish Eats Rice<br><sub>大肥鱼吃饭饭</sub></h1>

<p align="center">
  A desktop pet that <b>polls the DeepSeek balance API</b> and eats rice exactly as fast as your
  money burns.<br>
  She lives in a frameless, transparent, always-on-top window: four states, every one of them a
  frame-by-frame animation, and a right-click menu, drag-to-move and a status bubble to go with it.
</p>

<p align="center">
  <a href="https://github.com/ecusthjd/dswhale_pet/releases/download/v1.0.0/dswhale_pet.exe">
    <b>⬇ Download dswhale_pet.exe</b>
  </a><br>
  <sub>43 MB, one file, no Python and nothing to install — double-click it and she is on your
  desktop. Newer builds live on the <a href="https://github.com/ecusthjd/dswhale_pet/releases">releases page</a>.</sub>
</p>

<p align="center">
  <img alt="Windows 10 / 11" src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows&logoColor=white">
  <img alt="Python 3.8+" src="https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white">
  <img alt="PyQt5" src="https://img.shields.io/badge/GUI-PyQt5-41CD52?logo=qt&logoColor=white">
  <img alt="the API key never leaves your machine" src="https://img.shields.io/badge/API%20key-stays%20on%20your%20machine-critical">
</p>

No DeepSeek key at hand? The app has a **demo mode** (`--demo cycle` on the command line, or the
settings dialog): all four states and their animations are right there, no account needed.

## The four states

| 🍚 Ready to Eat | 🥄 Eating | 🔥 Eating Fast | 💸 Out of Rice |
|:---:|:---:|:---:|:---:|
| <img src="preview_pet/ready.gif" width="260" alt="Ready to Eat"> | <img src="preview_pet/eating.gif" width="260" alt="Eating"> | <img src="preview_pet/fast.gif" width="260" alt="Eating Fast"> | <img src="preview_pet/empty.gif" width="260" alt="Out of Rice"> |
| balance > 0, but it has not dropped for a while | money is burning, rate ≤ threshold (2 CNY/hour by default) | rate > threshold | balance < 0.01 CNY, or the API says `is_available:false` |

## She talks back

Every state change comes with one spoken line, and **the line depends on where she came from and
where she is going** (entering "Eating": just picked up the spoon / slowed back down / eating
again after a top-up); the "Eating Fast" lines read out the measured rate. The lines only mention
**things that are really on screen** — she holds a spoon, and shovels rice with her bare hand in
the fast state, so none of them ever mentions chopsticks.

## Running it

```powershell
dswhale_pet.exe                  # the downloaded build: nothing to install, no console window
python -m pip install PyQt5      # the only dependency
python dswhale_pet.py            # run it in a terminal (so you can see errors)
run_pet.bat                      # double-click (uses pythonw, no console window)
```

Right-click her (or the tray icon) → **Settings…** → paste your DeepSeek API key → save and start
monitoring. No key at hand? Pick a **demo mode** first (`--demo cycle` works too) and all four
states plus their animations are right there.

The key is written to `%APPDATA%\dswhale_pet\config.json` on this machine only, is used to talk to
`api.deepseek.com` directly and is never sent to anyone else.

## Building an exe

```powershell
python -m pip install --upgrade pyinstaller
python tools\build_exe.py        # -> dist\dswhale_pet.exe (one file, no console, with icon)
```

`build_exe.py` first generates `dswhale_pet.spec` from a whitelist (only the three asset paths the
app really reads, with the unused big Qt binaries stripped out), then lets PyInstaller build from
it — the exe drops from 62.5 MB to about 35 MB. All the assets it needs live in `assets/`, so just
run it; if something is missing it names the file instead of failing silently.

## Releasing the exe on GitHub

`dist/` is not in git (`.gitignore`), so the exe is published as a **release asset**, not as a commit:
build it, tag the commit you built from, then attach the exe to the release for that tag.

```powershell
python tools\build_exe.py                  # -> dist\dswhale_pet.exe
git tag -a v1.1.0 -m "dswhale_pet v1.1.0"  # a new tag; a tag can only be used once
git push origin v1.1.0
```

Then either:

* **Web UI** (no extra tools): repository → *Releases* → **Draft a new release** → *Choose a tag* →
  `v1.1.0` → write the title and notes → drag `dist\dswhale_pet.exe` into the *Attach binaries* box →
  **Publish release**.
* **GitHub CLI** (one command, after `winget install GitHub.cli` and `gh auth login`):

  ```powershell
  gh release create v1.1.0 dist\dswhale_pet.exe --title "dswhale_pet v1.1.0" --notes "What changed in this build."
  ```

A release asset may be up to 2 GB, while a file committed to git wants to stay under 100 MB — which is
exactly why the ~43 MB one-file exe belongs on the release page and not in the repository.

## What is in this folder

```
dswhale_pet.py             the app itself (PyQt5, single file; rate measuring and the four-state
                           logic are plain Python, no Qt in them)
run_pet.bat                double-click launcher
tools/build_exe.py         builds the exe (a thin wrapper around PyInstaller)
dswhale_pet.spec           build config (build_exe.py regenerates it on every run)
assets/pet/pet_meta.json   metadata for the four states (bowl rim position and friends)
assets/pet/anim/           four frame-by-frame animations: <state>/000.png… + meta.json
                           (35 frames, 11.5 MB)
assets/dswhale_pet.ico     exe / window icon
preview_pet/*.gif          the four preview animations shown above (ready / eating / fast / empty,
                           2 MB in total; eating is the banner, 400 px wide)
tools/make_preview_gifs.py rebuilds those GIFs from assets/pet/anim -- scaled down, one shared
                           palette, no dithering; --plain also writes transparent copies
```

> This folder **runs and builds on its own**: every asset the app reads ships inside `assets/`.
> What is *not* here is the asset pipeline and the acceptance suite (cutting the frames out of the
> source art, writing the animations, building the icon, all those verification scripts), and
> neither are the four static fallback portraits in `assets/pet/*.png` — they are never used once
> the frame-by-frame assets exist. The README's GIFs *are* rebuildable from here:
> `python tools\make_preview_gifs.py`.
> The character art and the animation frames are AI-generated; check the terms of the service you
> generated them with before redistributing them.
