# Big Fat Fish Eats Rice

> 大肥鱼吃饭饭

A desktop pet that polls the DeepSeek balance API and switches between four states based on
**how fast the money is burning**. It lives in a frameless, transparent, always-on-top window:
she sits on your desktop eating rice, and you get a right-click menu, drag-to-move and a status
bubble. All four states are **frame-by-frame animations**.

![Ready to Eat](preview_pet/ready.gif)
![Eating](preview_pet/eating.gif)
![Eating Fast](preview_pet/fast.gif)
![Out of Rice](preview_pet/empty.gif)

| State | When it shows up |
| --- | --- |
| 🍚 Ready to Eat | balance > 0, but it has not dropped for a while |
| 🥄 Eating | money is burning, rate ≤ threshold (2 CNY/hour by default) |
| 🔥 Eating Fast | rate > threshold |
| 💸 Out of Rice | balance < 0.01 CNY, or the API says `is_available:false` |

Every state change comes with one spoken line, and **the line depends on where she came from and
where she is going** (entering "Eating": just picked up the spoon / slowed back down / eating
again after a top-up); the "Eating Fast" lines read out the measured rate. The lines only mention
**things that are really on screen** — she holds a spoon, and shovels rice with her bare hand in
the fast state, so none of them ever mentions chopsticks.

## Running it

```powershell
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
preview_pet/*.gif          the four preview animations (the ones shown above)
preview_pet/gif/           anti-jitter versions: <state>_plain.gif (transparent) and
                           <state>_table.gif (a generated table added)
```

> This folder **runs and builds on its own**: every asset the app reads ships inside `assets/`.
> What is *not* here is the asset pipeline and the acceptance suite (cutting the frames out,
> writing the animations, making the GIFs, building the icon, all those verification scripts),
> and neither are the four static fallback portraits in `assets/pet/*.png` — they are never used
> once the frame-by-frame assets exist.
> The character art and the animation frames are AI-generated; check the terms of the service you
> generated them with before redistributing them.
