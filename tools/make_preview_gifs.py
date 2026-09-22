"""Turn the frame-by-frame animations into the GIFs the README shows.

The app draws its four states from `assets/pet/anim/<state>/000.png…` (11 fps, 528x560, RGBA).
GitHub only renders an image that really lives in the repository, so the README needs those frames
as files of their own -- and a raw 2.7 MB PNG cannot go in a README anyway: everything is scaled
down and written as a GIF with one shared palette, which is what browsers play inline.

Two flavours, both off the same frames:

* `preview_pet/<state>.gif`  -- the pet composited on a soft light backdrop. This is the one the
  README shows: the art keeps its soft anti-aliased edges (nothing is cut against transparency, so
  there are no white halos) and the light card reads well on GitHub's light **and** dark theme.
* `preview_pet/gif/<state>_plain.gif` (with `--plain`) -- the same animation over nothing, for
  anywhere a transparent image is wanted.

Usage:
    python tools/make_preview_gifs.py                  # -> preview_pet/<state>.gif
    python tools/make_preview_gifs.py --plain          # + preview_pet/gif/<state>_plain.gif
    python tools/make_preview_gifs.py --width 400 --colors 128
    python tools/make_preview_gifs.py --dither         # slower/noisier, ~30% bigger
    python tools/make_preview_gifs.py --states eating  # just one state (while tuning the banner)

Dithering is **off** by default: on this flat-shaded art it costs about a third of the file size
(eating at 400 px: 800 KB instead of 1.27 MB, same palette) and buys nothing but grain in the
backdrop gradient -- with it off the smooth areas stay smooth.

Dependency: Pillow (already needed if you ever regenerate the assets): python -m pip install Pillow
"""
from __future__ import annotations

import argparse
import glob
import json
import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ANIM = os.path.join(ROOT, 'assets', 'pet', 'anim')          # <state>/000.png… + meta.json
META = os.path.join(ANIM, 'meta.json')                      # fps / frame count / size per state
OUT = os.path.join(ROOT, 'preview_pet')                     # what the README points at
PLAIN_DIR = os.path.join(OUT, 'gif')                        # the transparent copies

# The backdrop behind the pet: a barely-there top-to-bottom gradient, light enough that the dark
# blue hair and the white apron both stand out on GitHub's light theme and its dark one.
BACK_TOP = (252, 253, 255)
BACK_BOTTOM = (226, 233, 243)

# The README's banner is the wide one; the other three sit side by side in a table.
BANNER_WIDTH = 400
THUMB_WIDTH = 260
BANNER_STATE = 'eating'


def load_meta():
    """meta.json is what the app itself reads, so the GIFs use the very same numbers."""
    if not os.path.exists(META):
        raise SystemExit('missing %s\nrun this first: python tools/make_pet_anim.py' % META)
    with open(META, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def frames(state):
    paths = sorted(glob.glob(os.path.join(ANIM, state, '*.png')))
    if not paths:
        raise SystemExit('no frames in %s' % os.path.join(ANIM, state))
    out = []
    for p in paths:
        with Image.open(p) as im:
            out.append(im.convert('RGBA'))
    return out


def backdrop(size):
    """A soft vertical gradient, built row by row (a few hundred rows, so it costs nothing)."""
    w, h = size
    img = Image.new('RGB', size)
    px = img.load()
    for y in range(h):
        t = y / float(max(1, h - 1))
        row = tuple(int(round(BACK_TOP[i] + (BACK_BOTTOM[i] - BACK_TOP[i]) * t)) for i in range(3))
        for x in range(w):
            px[x, y] = row
    return img


def scaled(img, size, back):
    small = img.resize(size, Image.LANCZOS)
    if back is None:
        return small
    flat = back.copy()
    flat.paste(small, (0, 0), small)                        # alpha-composite on the backdrop
    return flat


def quantized(imgs, colors, dither=Image.NONE):
    """One shared palette for every frame: smaller file, no colour flicker while it plays."""
    pal = imgs[0].convert('RGB').quantize(colors=colors, method=Image.MEDIANCUT)
    return [im.convert('RGB').quantize(palette=pal, dither=dither) for im in imgs]


def save_gif(imgs, path, duration):
    """One palette for every frame: smaller file, and no colour flicker between frames."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=duration, loop=0, optimize=True, disposal=2)
    return os.path.getsize(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description='build the README preview GIFs from the animation frames')
    ap.add_argument('--width', type=int, default=BANNER_WIDTH,
                    help='width of the banner state in px (default: %d)' % BANNER_WIDTH)
    ap.add_argument('--thumb-width', type=int, default=THUMB_WIDTH,
                    help='width of the other three in px (default: %d)' % THUMB_WIDTH)
    ap.add_argument('--banner', default=BANNER_STATE, help='which state gets --width (default: eating)')
    ap.add_argument('--colors', type=int, default=256, help='palette size, 2~256 (default: 256)')
    ap.add_argument('--dither', action='store_true',
                    help='error-diffusion dither the frames (bigger, grainier; off by default)')
    ap.add_argument('--plain', action='store_true',
                    help='also write preview_pet/gif/<state>_plain.gif (transparent)')
    ap.add_argument('--states', nargs='*', help='only these states (default: all of them)')
    args = ap.parse_args(argv)

    dither = Image.FLOYDSTEINBERG if args.dither else Image.NONE

    meta = load_meta()
    states = args.states or [s for s in ('ready', 'eating', 'fast', 'empty') if s in meta]
    if not os.path.isdir(OUT):
        os.makedirs(OUT)

    total = 0
    for st in states:
        info = meta.get(st)
        if not info:
            raise SystemExit('%s is not in meta.json (have: %s)' % (st, ', '.join(sorted(meta))))
        w = args.width if st == args.banner else args.thumb_width
        src = frames(st)
        size = (w, int(round(src[0].height * w / float(src[0].width))))
        duration = int(round(1000.0 / float(info.get('fps', 11.0))))

        back = backdrop(size)
        shown = [scaled(im, size, back) for im in src]
        path = os.path.join(OUT, '%s.gif' % st)
        n = save_gif(quantized(shown, args.colors, dither), path, duration)
        total += n
        print('%-6s %2d frames %4dx%-4d %6.0f KB  ->  %s' % (
            st, len(shown), size[0], size[1], n / 1024.0, os.path.relpath(path, ROOT).replace(os.sep, '/')))

        if args.plain:
            bare = [scaled(im, size, None) for im in src]
            plain = os.path.join(PLAIN_DIR, '%s_plain.gif' % st)
            n2 = save_gif(quantized(bare, args.colors, dither), plain, duration)
            total += n2
            print('%-6s %2d frames (transparent)         %6.0f KB  ->  %s' % (
                st, len(bare), n2 / 1024.0, os.path.relpath(plain, ROOT).replace(os.sep, '/')))

    print('total: %.0f KB in %s (the app never reads these -- they are for the README only)'
          % (total / 1024.0, os.path.relpath(OUT, ROOT).replace(os.sep, '/')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

