#!/usr/bin/env python3
"""Big Fat Fish Eats Rice -- desktop pet (PyQt5)

A frameless, transparent, always-on-top little window: she sits at a little table eating rice, and
**how fast she eats is driven by how fast you really burn through data** -- which feed she watches
can be switched from the right-click menu ("Monitor (mode)"):

    DeepSeek balance  how fast the balance drops (CNY/h); balance at zero = out of rice
    OpenCode Go       how fast the rolling window's remaining percent drops (%/h) -- in other
                      words how fast the rolling value rises; spending faster than the threshold
                      (default 20%/h) puts her in "Eating Fast"; any of rolling / weekly / monthly
                      **at 100%** = out of rice

    ready  not eating                  frames in assets/pet/anim/ready/
    eating eating at a normal rate     frames in assets/pet/anim/eating/
    fast   eating fast                 frames in assets/pet/anim/fast/
    empty  balance 0 / window full / API says no   frames in assets/pet/anim/empty/

In Go mode the rolling window's used percent is folded into a **remaining percent** (only ever goes
down -- the same direction as the balance), so rate measuring, the four states, the curve, the
speed gauge and "how long will it last" all reuse the same code; only the unit goes from CNY to %.

All four states are **frame-by-frame animations**: AI animation frames cut out into transparent PNGs
(assets/pet/anim/) and looped here at their fps. Without frame art the app falls back to the static
portraits in assets/pet/*.png plus a programmatic breathing / sway. Frames are **rasterized for the
device resolution** (PetArt._fit: size x dpr, halving step by step when shrinking), so the pet stays
crisp at any size on a high DPI screen.

Run it::

    python dswhale_pet.py                  # or double-click run_pet.bat (no console)
    python dswhale_pet.py --demo cycle     # no API key needed, just look at her
    python dswhale_pet.py --key sk-xxx     # key on the command line (usually: right-click -> Settings)
    python dswhale_pet.py --state fast     # force one state (debugging / screenshots)
    python dswhale_pet.py --go-usage --go-key oc_sk-xxx   # one-shot Go usage check, then exit (no window)

Command line arguments are in parse_args below; the README has the algorithm and parameter details.

Depends on PyQt5 only: both endpoints (balance / Go usage) go through urllib, and the portraits are
pre-made transparent PNGs, so there is no requests / Pillow. Rate measuring and the four-state logic
live in Core, which **contains no Qt at all** and can be run on its own.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from PyQt5.QtCore import (QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal)
from PyQt5.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetrics, QIcon, QImage,
                         QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PyQt5.QtWidgets import (QActionGroup, QApplication, QCheckBox, QComboBox,
                             QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                             QLabel, QLineEdit, QMenu, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox,
                             QSystemTrayIcon, QVBoxLayout, QWidget)

APP = 'dswhale_pet'
if getattr(sys, 'frozen', False):            # packaged by PyInstaller: assets live in the unpack dir
    ROOT = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
PET_DIR = os.path.join(ROOT, 'assets', 'pet')
META_PATH = os.path.join(PET_DIR, 'pet_meta.json')
ANIM_META = os.path.join(PET_DIR, 'anim', 'meta.json')     # frame table, made by the asset pipeline
ICON_PATH = os.path.join(ROOT, 'assets', 'dswhale_pet.ico')  # window icon (made by the asset pipeline)
API = 'https://api.deepseek.com/user/balance'
# OpenCode Go (a $10/month subscription) usage: read-only percentages, no request counts, nothing is
# ever modified. Three windows -- rolling / weekly / monthly -- each carrying its own reset time.
GO_API = 'https://opencode.ai/zen/go/v1/usage'
GO_WINS = ('rolling', 'weekly', 'monthly')
GO_LABEL = {'rolling': 'Rolling', 'weekly': 'Weekly', 'monthly': 'Monthly'}
GO_WIN_COLOR = (0.50, 0.80)                          # usage bar: <50% green, <80% yellow, above red
GO_FULL = 100.0                                      # a window is full at 100% (any one = out of quota)
GO_RESET_EPS = 5.0                                   # remaining % jumped this much -> window loosened, start over

# Two modes: which data decides "how fast she eats". The two keys belong to two different services,
# she never uses both.
#   deepseek  how fast the DeepSeek balance (CNY) drops;
#   go        how fast the **rolling window's remaining percent** drops (= how fast the rolling
#             value rises); any window full (100%) = "out of rice".
# Folding it into a "remaining %" keeps it in the same direction as the balance: it only drops, and
# the faster it drops the faster she eats -- rate measuring, the four states, the curve, the speed
# gauge and "how long will it last" all work unchanged, the unit just goes from CNY to %.
MODES = {
    'deepseek': {'name': 'DeepSeek', 'label': 'DeepSeek balance (CNY/h)',
                 'unit': 'CNY', 'rate': 'CNY/hour', 'thresh': 'slowMax'},
    'go':       {'name': 'OpenCode Go', 'label': 'OpenCode Go usage (%/h)',
                 'unit': '%', 'rate': '%/hour', 'thresh': 'goSlowMax'},
}
MODE_KEYS = ('deepseek', 'go')

# Go-mode talk: the "money" phrases turn into "quota" phrases ('CNY/hour' -> '%/hour',
# "Balance hit zero" -> "quota used up" ...). The mode changed, so the spoken lines change too.
GO_TALK_SWAP = (('CNY/hour', '%/hour'), ('Balance hit zero', 'Quota used up'),
                ('balance', 'quota'), ('Food again', 'Quota back'),
                ('Bowl is full again', 'Quota back'), ('burning money', 'spending quota'))

# Defaults for the config (the key names are the field names in the config file)
CFG_DEFAULT = {
    'key': '', 'pollSec': 10, 'idleSec': 120, 'slowMax': 2.0, 'price': 4.0,
    'windowMin': 15,
    # which feed she eats off (deepseek / go, see MODES above); goSlowMax is the Go-mode
    # "fast eating" threshold
    'mode': 'deepseek', 'goSlowMax': 20.0,
    # OpenCode Go: goKey is a second key (opencode.ai's), unrelated to the DeepSeek one; goSec only
    # governs the "also peek at usage" cadence in DeepSeek mode (in Go mode the usage IS the main
    # data, so it follows pollSec instead)
    'goKey': '', 'goSec': 60, 'goBubble': True,
    # which rate drives the animation: 'burst' = instant (responsive, default) / 'fit' = fitted
    # (steady, half a beat late)
    'actOn': 'burst',
    # the pet's own settings: portrait long edge in px, always on top, bubble, click-through,
    # see-through under the cursor while click-through is on, last position
    'size': 300, 'topmost': True, 'bubble': True, 'clickThrough': False, 'thruLens': True,
    'x': None, 'y': None,
}

STATES = {
    'ready':  {'label': 'Ready to Eat', 'em': '🍚', 'fx': 'steam',  'accent': '#7fd7ff'},
    'eating': {'label': 'Eating',       'em': '🥄', 'fx': 'rice',   'accent': '#ffd479'},
    'fast':   {'label': 'Eating Fast',  'em': '🔥', 'fx': 'rice',   'accent': '#ff6b3d'},
    'empty':  {'label': 'Out of Rice',  'em': '💸', 'fx': 'dust',   'accent': '#7fd7ff'},
}
STATE_KEYS = ['ready', 'eating', 'fast', 'empty']

# The line spoken on a state change: keys are (where from -> where to), values are a few lines per
# pair, said in rotation. Why split by "where from": entering "eating" from "ready" means she just
# picked up the spoon, coming down from "fast" means she is **slowing down**, and coming back from
# "empty" means **a top-up landed** -- those should not sound the same.
# The lines also have to match the picture: she holds a **spoon** (eating), and in the fast state
# she **shovels rice with her bare hand** (there is no chopstick anywhere in the art) -- so none of
# these ever mentions chopsticks (verify_pet.py has a check that catches it).
# `{rate}` / `{bal}` are filled in by Core.state_msg (CNY/hour, balance including its currency).
MSG_TALK = {
    ('ready', 'eating'): ['🍽️ Dinner time -- starting with small bites', '🥄 Spending again, she picks up the spoon',
                          '🍚 Bowl is still full, chewing slowly'],
    ('ready', 'fast'):   ['⚡ Straight to full speed! {rate} CNY/hour',
                          '🔥 From silence to shovelling -- {rate} CNY/hour'],
    ('ready', 'empty'):  ['💸 Not a single bite, and the bowl is already empty',
                          '💸 Balance hit zero, she stares at an empty bowl'],
    ('eating', 'fast'):  ['🔥 Faster and faster! {rate} CNY/hour',
                          '🙌 Spoon tossed aside, shovelling with both hands -- {rate} CNY/hour',
                          '🔥 Shovel mode! {rate} CNY/hour'],
    ('eating', 'ready'): ['😌 She puts the spoon down and goes quiet…', '🫖 Not burning money right now, taking a break'],
    ('eating', 'empty'): ['💸 Eating straight through to the bottom, balance hit zero',
                          '🥄 Last bite! The spoon scrapes the bowl'],
    ('fast', 'eating'):  ['😮‍💨 Slowing down, but not stopping', '🙂 Rate back under the threshold, steady eating again'],
    ('fast', 'ready'):   ['🙌 The feast is over, she wipes her mouth', '😌 Stopped… that was a serious run'],
    ('fast', 'empty'):   ['💸 Finished in one go, balance hit zero', '🔥 Shovel run ends: the bowl is empty'],
    ('empty', 'eating'): ['💰 Food again! {bal} arrived, back to slow eating', '🍚 Bowl is full again ({bal}), eating'],
    ('empty', 'fast'):   ['⚡ Food means full speed! {rate} CNY/hour', '💰 {bal} arrived, straight into shovel mode'],
    ('empty', 'ready'):  ['💰 {bal} arrived, she takes it easy for now', '🍚 Bowl is full, waiting for you to burn more'],
}

# When there is no "previous state" (a fresh start, demo mode locking one state right away, --shots
# rendering), pick by "where to". These double as the fallback: if a combination is missing from the
# table above (a state added later and forgotten here), at least something sane comes out.
MSG_TALK_FALLBACK = {
    'ready':  ['😌 Quiet again… she sits down and waits for a top-up'],
    'eating': ['🍽️ Dinner time, eating slowly'],
    'fast':   ['🔥 Shovel mode! {rate} CNY/hour'],
    'empty':  ['💸 Balance hit zero, the bowl is empty'],
}

DEMOS = {
    'off':   {'label': 'Off (use the real API)', 'rate': None},
    'idle':  {'label': 'Idle: balance only, nothing burning', 'rate': 0.0},
    'slow':  {'label': 'Slow: 0.4 × threshold', 'rate': 0.4},
    'fast':  {'label': 'Fast: 3.5 × threshold', 'rate': 3.5},
    'drain': {'label': 'Drain: burn fast down to 0', 'rate': 6.0, 'drain': True},
    'cycle': {'label': 'Cycle: idle → slow → fast → drain', 'rate': 0.0, 'cycle': True},
}


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_fonts():
    """Register the system CJK / emoji fonts with Qt's font database.

    On the offscreen (headless) platform Qt only knows the fonts inside its own lib/fonts, so the
    currency sign and the emoji in the bubble text come out as boxes and the widths QFontMetrics
    reports are wrong (which would make the verification screenshots useless). Registering them
    once when a window opens normally does no harm either.
    """
    from PyQt5.QtGui import QFontDatabase
    for name in ('msyh.ttc', 'msyhbd.ttc', 'simhei.ttf', 'simsun.ttc',
                 'seguiemj.ttf', 'seguisym.ttf'):
        p = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', name)
        try:
            if os.path.exists(p):
                QFontDatabase.addApplicationFont(p)
        except Exception:
            pass


def quiet_qt():
    """Silence the offscreen "cannot find Qt's font directory" nagging; real errors still print."""
    from PyQt5.QtCore import qInstallMessageHandler
    noise = ('Cannot find font directory', 'Note that Qt no longer ships fonts')

    def handler(mode, ctx, msg):
        if any(n in msg for n in noise):
            return
        sys.stderr.write(msg + '\n')

    qInstallMessageHandler(handler)


def clamp(v, a, b):
    return a if v < a else (b if v > b else v)


def fmt(v, n=2) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 0.0
    if v != v:                       # NaN
        v = 0.0
    return '%.*f' % (n, v)


def hhmm(ms_) -> str:                # duration -> 1h 23m / 4m 05s
    s = max(0, int(ms_ // 1000))
    if s < 60:
        return '%ds' % s
    if s < 3600:
        return '%dm %02ds' % (s // 60, s % 60)
    return '%dh %02dm' % (s // 3600, (s % 3600) // 60)


def ago(ms_) -> str:
    s = int(ms_ // 1000)
    if s < 3:
        return 'just now'
    if s < 60:
        return '%ds' % s
    if s < 3600:
        return '%d min' % (s // 60)
    return '%dh' % (s // 3600)


def ago_text(at, now=None) -> str:
    """The "updated N ago" line (a fresh one must not read "just now ago")."""
    txt = ago((now or now_ms()) - at)
    return txt + ('' if txt == 'just now' else ' ago')


def num(v, d=0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return d
    return x if x == x else d


# =====================================================================
# 1) Rate measuring + four-state logic (plain Python, no Qt -- runs and verifies on its own)
#    Everything here is milliseconds (the "seconds" in the config are converted), and one sample is
#    a (timestamp_ms, balance) pair.
# =====================================================================
def cur_sym(cur):
    """Currency -> symbol (the balance drawn on the bowl uses this)."""
    return '¥' if (cur or 'CNY') == 'CNY' else '$'


class Core:
    def __init__(self, cfg):
        self.cfg = cfg
        self.demo = 'off'
        self.demoBalF = 0.0
        self.demoRateAt = 0
        self.forced = None
        self.reset()

    def reset(self, now=None, bal=None):
        t = now or now_ms()
        self.samples = []             # [(t, balance)]
        self.lastDropAt = 0           # when the balance last went down (tells "is she still eating")
        self.curBal = None
        self.isAvailable = None
        self.cur = 'CNY'
        self.topped = None
        self.granted = None
        self.lastOkAt = 0
        self.errMsg = ''
        self.failCount = 0
        self.state = 'ready'
        self.stateAt = t
        self.t0 = t
        self.bal0 = bal
        self.slope = 0.0
        self.burst = 0.0
        self.rate = 0.0
        self.rateAt = 0
        self.events = []              # balance changes not drawn yet (see take_events)
        self.msg_seen = {}            # line rotation: which line each pair is at (a counter, no RNG)
        self.go = {}                  # OpenCode Go usage: {window: {'percent', 'status', 'resetAt'}}
        self.goRaw = None             # the last raw response (shown in the dialog)
        self.goAt = 0                 # when the Go usage was last fetched successfully
        self.goErr = ''               # the Go fetch error (kept separate from the balance errMsg)
        self.goFull = False           # Go mode: some window is full (100%) = out of quota

    # ---------- balance change events (feed the floating labels in the render layer) ----------
    def add_event(self, kind, amount, t):
        """Record one balance change: kind = 'spend' (money eaten) / 'topup' (money added).

        Only the delta between two polls is recorded, i.e. the real increment -- the rate is a
        fitted number while the floating label wants "what was just charged", and those are two
        different things. The render layer drains this list; it is empty once taken.
        """
        if amount <= 1e-9:
            return
        self.events.append({'kind': kind, 'amount': float(amount), 'at': t})
        if len(self.events) > 64:                 # nobody draining yet (no window)? do not pile up
            self.events.pop(0)

    def take_events(self):
        """Take every event that has not been drawn yet (the render layer calls this per frame)."""
        ev, self.events = self.events, []
        return ev

    # ---------- sampling ----------
    def win_ms(self):
        return self.cfg['windowMin'] * 60000

    def prune(self, t):
        cut = t - max(self.win_ms(), self.cfg['idleSec'] * 1000) - 120000
        while len(self.samples) > 2 and self.samples[0][0] < cut:
            self.samples.pop(0)

    def push_sample(self, b, t, quiet=False):
        """Record one balance sample. quiet=True produces no floating-label event (used when
        rebuilding a curve out of history).

        "Less than the previous sample" means she is eating: that both tells whether she is still
        eating and feeds the floating label above the bowl.
        """
        if self.samples:
            d = self.samples[-1][1] - b
            if d > 1e-9:
                self.lastDropAt = t                 # less than the previous sample = eating
                if not quiet:
                    self.add_event('spend', d, t)
            elif d < -1e-9 and not quiet:           # sample went up (top-ups go through add_balance)
                self.add_event('topup', -d, t)
        self.samples.append((t, b))
        if self.bal0 is None:
            self.bal0 = b
        self.prune(t)

    def find(self, t):
        """First sample with t >= the given time (the list is ordered by time)."""
        for s in self.samples:
            if s[0] >= t:
                return s
        return None

    def demo_on(self):
        return self.demo not in (None, 'off')

    # ---------- rate ----------
    def speed_now(self, t):
        if self.demo_on() and t - self.demoRateAt < 3000:
            return                                  # demo mode injects the rate directly
        w = [s for s in self.samples if s[0] >= t - self.win_ms()]
        slope = 0.0
        if len(w) >= 2:
            x0 = w[0][0]
            n = len(w)
            sx = sy = sxx = sxy = 0.0
            for tt, bb in w:
                x = (tt - x0) / 3600000.0
                sx += x
                sy += bb
                sxx += x * x
                sxy += x * bb
            den = n * sxx - sx * sx
            if abs(den) > 1e-12:
                slope = max(0.0, -(n * sxy - sx * sy) / den)
        # Instant segment: compare the balance with the sample from max(idle window, 3x poll) ago,
        # so a single 0.01 step cannot blow up into tens of CNY/hour.
        # The Go feed only moves in **1% steps**, so over a short span the difference is all whole
        # steps (1 step / 2 min = 30%/hour); in Go mode give the span at least one full window.
        burst = 0.0
        span = max(self.cfg['idleSec'] * 1000, self.cfg['pollSec'] * 3000)
        if self.go_mode():
            span = max(span, self.win_ms())
        anchor = self.find(t - span)
        if (anchor is not None and self.curBal is not None
                and anchor[1] > self.curBal + 1e-9 and t - anchor[0] > 3000):
            burst = (anchor[1] - self.curBal) / ((t - anchor[0]) / 3600000.0)
        self.slope = slope
        self.burst = burst
        self.rateAt = t
        self.rate = max(slope, burst)
        return self.rate

    # ---------- four-state decision (hysteresis, so it does not flip-flop at the threshold) ----------
    def act_speed(self):
        """Which number drives the **animation**: instant or fitted (switchable in the settings,
        see cfg['actOn']).

        * `burst` (instant, default): compares the sample from "idle window / 3x poll" ago with the
          current balance, i.e. how much dropped in this short span. She reacts the moment eating
          speeds up, which feels responsive; the price is the odd twitch when the balance only
          moves in 0.01 steps.
        * `fit` (fitted): least squares over the whole window, steady but half a beat late (it needs
          enough samples before it moves).
        * For the record, `rate` is the larger of the two (the "fit / instant" pair in the bubble).
        """
        return self.burst if self.cfg.get('actOn') == 'burst' else self.rate

    def decide(self, t):
        """Which state it should be right now.

        DeepSeek mode: the balance only goes down (top-ups aside), so balance at zero / the API
        saying "unavailable" = out of rice.
        Go mode: the rolling window's "remaining %" only goes down too (spending), so the rules are
        identical -- with exactly one difference: **any window full (100%) = out of rice**.
        """
        if self.forced:
            return self.forced
        if self.curBal is None:
            return 'ready'
        if self.go_mode():
            if self.goFull or self.curBal <= 0.5:
                return 'empty'
        else:
            if self.isAvailable is False:
                return 'empty'
            if self.curBal < 0.01:
                return 'empty'
        look = self.cfg['idleSec'] * 3000            # slow burning gets 3x the grace
        grace, eps = self.cfg['idleSec'] * 1000, 0.01
        if self.go_mode():
            # Go's data only moves in 1% steps: even normal spending can skip a few minutes
            # between ticks, so the "is she still eating" time scale goes up to a whole window
            # and the movement threshold to "at least half a step".
            look = max(look, self.win_ms())
            grace = max(grace, self.win_ms())
            eps = 0.5
        an = self.find(t - look)
        active = ((t - self.lastDropAt) < grace
                  or (an is not None and an[1] - self.curBal >= eps))
        if not active:
            return 'ready'
        lim = self.thresh()
        r = self.act_speed()                         # instant or fitted, per settings
        if self.state == 'fast':                     # already fast: only fall back below 0.85x
            return 'fast' if r > lim * 0.85 else 'eating'
        return 'fast' if r > lim * 1.15 else 'eating'

    def apply_state(self, t):
        want = self.decide(t)
        if want == self.state:
            return False
        if t - self.stateAt < 1500:                  # stay put for at least 1.5s
            return False
        self.state = want
        self.stateAt = t
        return True

    def _fill_talk(self, text):
        """Fill `{rate}` / `{bal}` in a spoken line (CNY/hour, balance with its currency).

        In Go mode GO_TALK_SWAP runs afterwards: CNY/hour -> %/hour, "money" talk -> "quota" talk.
        The mode changed, so the spoken lines must change too, or the bubble would read %/hour
        while the line still talks about "balance hit zero".
        """
        bal = self.curBal if self.curBal is not None else 0.0
        if self.go_mode():
            txt = text.replace('{rate}', fmt(self.rate, 1)).replace('{bal}', fmt(bal, 0) + '%')
            for a, b in GO_TALK_SWAP:
                txt = txt.replace(a, b)
            return txt
        return (text.replace('{rate}', fmt(self.rate, 1))
                    .replace('{bal}', cur_sym(self.cur) + fmt(bal)))

    def state_msg(self, prev=None):
        """Which line to say for this transition: pick the pool by (where from -> where to), then
        rotate through the lines in it.

        The same destination gets different lines because the **situation** differs: entering
        "eating" from "ready" means she just picked up the spoon, coming down from "fast" means
        slowing down, and coming back from "empty" means a top-up landed (and that `{bal}` line
        doubles as a balance readout for you).
        `prev` is the previous state (`last_state` inside `PetRenderer.draw()`); leaving it out, or
        passing the current state (fresh start, demo mode locking one state), uses
        `MSG_TALK_FALLBACK` instead.

        Rotation uses a counter, not randomness: hitting the same pool twice says the next line,
        yet the whole sequence stays deterministic -- so screenshots and the acceptance suite stay
        reproducible (anything here that "looks random" is done this way).
        """
        key = (prev, self.state)
        pool = MSG_TALK.get(key)
        if pool is None:
            key = ('*', self.state)
            pool = MSG_TALK_FALLBACK.get(self.state) or MSG_TALK_FALLBACK['ready']
        i = self.msg_seen.get(key, 0)
        self.msg_seen[key] = i + 1
        return self._fill_talk(pool[i % len(pool)])

    def fx_mult(self):
        r = self.rate
        if r <= 0:
            return 1.0
        return clamp(1 + math.log10(1 + r / max(self.thresh(), 0.05)) * 2.2, 1.0, 3.4)

    def gauge_frac(self):
        top = max(self.thresh() * 4, 0.5)
        return clamp(math.log10(1 + max(self.rate, 0.0)) / math.log10(1 + top), 0.0, 1.0)

    # ---------- taking data in ----------
    def add_balance(self, js, t, src='api'):
        if self.demo_on():
            # Demo mode is on: never record numbers coming from the real API -- the demo walks its
            # own values, and a real balance slipping in (usually lower than the demo one) would be
            # pushed back up by the next demo tick, i.e. "money that was just eaten gets added
            # back". Turn the demo off if you want real data.
            return ''
        infos = js.get('balance_infos') or []
        if not infos:
            raise ValueError('the response has no balance_infos')
        info = next((i for i in infos if i.get('currency') == 'CNY'), infos[0])
        b = num(info.get('total_balance'), float('nan'))
        if b != b:
            raise ValueError('could not parse the balance field')
        msg = ''
        if self.curBal is not None and b > self.curBal + 1e-9:   # balance went up = top-up
            self.samples = []
            self.lastDropAt = 0
            self.bal0 = b
            msg = '💰 Top-up received! Balance ' + cur_sym(info.get('currency')) + fmt(b)
            self.add_event('topup', b - self.curBal, t)          # the decrease is booked by push_sample
        self.curBal = b
        self.cur = info.get('currency') or 'CNY'
        self.topped = num(info.get('topped_up_balance'), None)
        self.granted = num(info.get('granted_balance'), None)
        self.isAvailable = js.get('is_available', True) is not False
        self.lastOkAt = t
        self.errMsg = ''
        self.failCount = 0
        self.push_sample(b, t)
        self.speed_now(t)
        self.apply_state(t)
        return msg

    def set_error(self, msg):
        self.errMsg = str(msg)
        self.failCount += 1

    # ---------- OpenCode Go usage (subscription quota; its own thing, not the balance) ----------
    def set_go_usage(self, js, t):
        """Store one Go usage response: parse it and remember the raw one. A wrong shape raises,
        and the UI prints a human-readable message either way."""
        self.go = parse_go_usage(js)
        self.goRaw = js
        self.goAt = t
        self.goErr = ''
        return self.go

    def set_go_error(self, msg):
        """A Go fetch failed: only record the error, keep the previous data ("showing what was
        seen last" beats an empty space)."""
        self.goErr = str(msg)

    def go_binding(self):
        """Which line she is stuck on when out of quota -> (window key, reset time); None when no
        time was given.

        More than one window can be full at once (rolling + weekly together is common), and then
        the **latest** reset is the real bottleneck; if none is full but the rolling one ran dry,
        use its reset time. With no window data at all (nothing fetched yet) -> (None, None).
        """
        if not self.go_mode() or not self.go:
            return None, None
        full = [w for w in GO_WINS
                if w in self.go and self.go[w]['percent'] >= GO_FULL]
        if full:
            known = [w for w in full if self.go[w]['resetAt']]
            if known:
                w = max(known, key=lambda k: self.go[k]['resetAt'])
                return w, self.go[w]['resetAt']
            return full[0], None
        roll = self.go.get('rolling')
        return ('rolling', roll['resetAt']) if roll else (None, None)

    def go_empty(self):
        """Does Go mode count as "out of quota" right now (the same condition decide() uses)."""
        return (self.go_mode() and self.curBal is not None
                and (self.goFull or self.curBal <= 0.5))

    def go_clock(self, now=None):
        """The countdown string above her head: **only `HH:MM:SS`** (no reset time given ->
        `--:--:--`; nothing else is ever added).

        The render layer recomputes it every frame (with the current millisecond), so the seconds
        really tick; it only has a value while she is out of quota, otherwise None (i.e. hidden).
        """
        if not self.go_empty():
            return None
        _win, at = self.go_binding()
        if not at:
            return '--:--:--'
        return hms(max(0, at - (now or now_ms())))

    def add_go_usage(self, js, t):
        """**The Go-mode main data**: fold the "rolling window used percent" into a "remaining
        percent" and feed that to the rate-measuring machinery.

        Why fold it to remaining: it keeps the same direction as the balance -- only decreases as
        she eats, so "how fast the rolling value rises" equals "how fast the remainder drops", and
        the slope / instant / four states / curve / "how long will it last" all work as-is.

        Three things happen here:
        * any window full (>=100%) sets `goFull`, so decide() parks her on "out of rice";
        * the remaining % jumps **way up** from the last tick (old requests slid out of the
          window / the window reset) -- the old consumption no longer counts, clear the samples
          and start over, and return a line for the UI to toast (same channel as a top-up);
        * otherwise it behaves exactly like a balance tick: record a sample, measure the rate,
          decide the state.
        """
        usage = parse_go_usage(js)
        self.go = usage
        self.goRaw = js
        self.goAt = t
        self.goErr = ''
        self.goFull = any(info['percent'] >= GO_FULL for info in usage.values())
        info = usage.get('rolling')
        if info is None:
            # the rolling value is Go mode's main signal: without it "how fast it rises" cannot be
            # measured -- do not pretend it can
            raise ValueError('no rolling window in the response (Go mode needs it for the rate)')
        rem = max(0.0, GO_FULL - float(info['percent']))
        msg = ''
        if self.curBal is not None and rem > self.curBal + GO_RESET_EPS:
            self.samples = []
            self.lastDropAt = 0
            self.bal0 = rem
            msg = '🔄 Rolling window loosened: old requests slid out / the window reset, starting over'
        self.curBal = rem
        self.cur = 'CNY'
        self.isAvailable = True
        self.lastOkAt = t
        self.errMsg = ''
        self.failCount = 0
        self.push_sample(rem, t)
        self.speed_now(t)
        self.apply_state(t)
        return msg

    def go_worst(self):
        """The window being used the most (key, info) -- the tray tooltip and the bubble line pick
        it as the representative."""
        if not self.go:
            return None, None
        k = max(GO_WINS, key=lambda w: self.go[w]['percent'] if w in self.go else -1.0)
        return k, self.go.get(k)

    def go_pct_text(self):
        """A short line like "Monthly 35%"; empty while there is no data yet."""
        k, info = self.go_worst()
        if k is None or info is None:
            return ''
        return '%s %s%%' % (GO_LABEL[k], go_pct_str(info['percent']))

    def go_tip(self):
        """The tray tooltip snippet: '  Go Monthly 35%' (nothing while there is no data)."""
        txt = self.go_pct_text()
        return ('  Go ' + txt) if txt else ''

    # ---------- demo mode (fakes the data locally, no API key needed) ----------
    def demo_start(self, name, t, bal=None):
        self.demo = name if name in DEMOS else 'off'
        if self.demo == 'off':
            self.forced = None
            # Never leave the demo numbers behind as a baseline: the first real sample would look
            # like "one huge drop at once" (the bowl still shows 100.00 while the real balance
            # 12.34 arrives -> an invented "ate 87.66" floats up out of nowhere).
            self.samples = []
            self.curBal = None
            self.lastDropAt = 0
            self.bal0 = None
            return
        d = DEMOS[self.demo]
        start = 100.0 if bal is None else float(bal)
        if d.get('drain') and bal is None:
            start = 1.2
        self.demoBalF = start
        self.demoRateAt = 0
        self.reset(t, start)
        self.cur = 'CNY'
        self.isAvailable = True
        self.topped, self.granted = 60.0, 40.0
        self.curBal = start
        self.lastOkAt = t
        self.push_sample(start, t)
        self.speed_now(t)
        self.apply_state(t)

    def demo_mult(self, t):
        d = DEMOS.get(self.demo)
        if not d:
            return 0.0
        if not d.get('cycle'):
            return d['rate'] or 0.0
        el = ((t - self.t0) / 1000.0) % 46.0        # idle 8 -> slow 12 -> fast 12 -> drain 14
        if el < 8:
            return 0.0
        if el < 20:
            return 0.4
        if el < 32:
            return 3.5
        return 8.0

    def demo_tick(self, t, dt_s=2.0):
        """One demo tick (every 2 seconds).

        A few seconds of slow burning simply cannot be measured on a 0.01 step (0.01 CNY equals
        3.6 CNY/hour times 10 seconds), so demo mode injects the rate directly while the real API
        path always goes through the measurement in speed_now.
        """
        if not self.demo_on():
            return ''
        d = DEMOS[self.demo]
        r = self.demo_mult(t) * self.thresh()        # the threshold follows the mode (CNY/h or %/h)
        self.demoBalF = max(0.0, self.demoBalF - r * (dt_s / 3600.0)
                            * (0.7 + random.random() * 0.6))
        if d.get('drain') and self.demoBalF < (0.5 if self.go_mode() else 0.01):
            self.demoBalF = 0.0
        msg = ''
        if d.get('cycle') and self.demoBalF <= 0.02:
            self.demoBalF = 100.0
            self.samples = []
            self.bal0 = 100.0
            msg = '🧪 Demo: the bowl got a refill'
        self.curBal = round(self.demoBalF * 100) / 100       # the balance only moves in 0.01 steps
        self.isAvailable = True
        self.lastOkAt = t
        self.errMsg = ''
        self.push_sample(self.curBal, t)
        self.slope = r * (0.94 + random.random() * 0.12)
        self.burst = r * (0.90 + random.random() * 0.20)
        self.rate = max(self.slope, self.burst)
        self.rateAt = t
        self.demoRateAt = t
        self.lastDropAt = t if r > 0 else 0
        self.apply_state(t)
        return msg

    # ---------- a few small books the UI needs ----------
    def session_cost(self):
        if self.bal0 is None or self.curBal is None:
            return 0.0
        return max(0.0, self.bal0 - self.curBal)

    def survive_ms(self):
        if self.curBal is None:
            return None
        if self.rate > 0.0005:
            return self.curBal / self.rate * 3600000.0
        return float('inf')

    def sign(self):
        return '$' if self.cur == 'USD' else '¥'

    # ---------- mode: watch the balance or the Go usage ----------
    def mode(self):
        m = self.cfg.get('mode') or 'deepseek'
        return m if m in MODES else 'deepseek'

    def go_mode(self):
        return self.mode() == 'go'

    def thresh(self):
        """The current mode's "fast eating" threshold: DeepSeek in CNY/hour, Go in %/hour."""
        key = MODES[self.mode()]['thresh']
        v = num(self.cfg.get(key), None)
        if v is None or v <= 0:
            v = CFG_DEFAULT[key]
        return float(v)

    def unit(self):
        return MODES[self.mode()]['unit']

    def rate_unit(self):
        return MODES[self.mode()]['rate']

    def lab(self, v=None, pre='Left '):
        """A quantity with its unit: `¥88.50` (DeepSeek) / `Left 65%` (Go). The bowl label and the
        bubble's big number both use it."""
        v = self.curBal if v is None else v
        if self.go_mode():
            return (pre + '--%') if v is None else ('%s%s%%' % (pre, fmt(v, 0)))
        return self.sign() + (' --.--' if v is None else fmt(v, 2))

    def float_style(self):
        """How the floating labels carry their unit: money carries the currency sign (−¥0.03),
        Go carries % with fewer decimals (−1.0%)."""
        if self.go_mode():
            return {'sym': '', 'unit': '%', 'dec': 1}
        return {'sym': self.sign(), 'unit': '', 'dec': 2}

    def err_text(self):
        """The error for the current mode's feed (the bubble's "connection failed" line and the
        ⚠ line both use it)."""
        return self.goErr if self.go_mode() else self.errMsg

    def tip_val(self):
        """The number in the tray tooltip: `  ¥88.50` / `  Left 65%` (nothing while there is no
        data)."""
        return '' if self.curBal is None else ('  ' + self.lab())


def iso_ms(s):
    """ISO 8601 -> a millisecond timestamp (the API gives UTC, e.g. 2026-08-17T00:00:00.569Z).

    Understands a trailing Z, an explicit offset and fractional seconds; anything unrecognised
    returns None -- the reset time is only there to display "how long is left", and losing it
    should never break the pet.
    """
    if not isinstance(s, str) or not s.strip():
        return None
    t, off = s.strip(), 0
    m = re.search(r'([+-])(\d{2}):?(\d{2})$', t)
    if m:
        off = (1 if m.group(1) == '+' else -1) * (int(m.group(2)) * 3600 + int(m.group(3)) * 60)
        t = t[:m.start()]
    t = t.replace('Z', ' ').replace('T', ' ').split('.')[0].strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            dt = datetime.datetime.strptime(t, fmt)
        except ValueError:
            continue
        return int((dt - datetime.datetime(1970, 1, 1)).total_seconds() - off) * 1000
    return None


def go_pct(info):
    """The window's "used percent" (35 from the API is 35% used). A string with a % is fine too."""
    if not isinstance(info, dict):
        return None
    v = info.get('percent')
    if isinstance(v, str):
        v = v.strip().rstrip('%').strip()
    return num(v, None)


def go_pct_str(p) -> str:
    """A percent as text: whole numbers get no decimal point (35% / 12% / 0.5%)."""
    return ('%.1f' % p) if abs(p - round(p)) > 0.05 else ('%.0f' % p)


def go_ok(info) -> bool:
    """Is this window's status fine: the API says ok; anything else is printed as-is, not ignored."""
    st = str((info or {}).get('status') or '').strip().lower()
    return st in ('', 'ok')


def dhmm(ms_) -> str:
    """A duration as a short phrase: 12d4h / 5h12m / 40s (days only once there is a whole day)."""
    s = max(0, int(ms_ // 1000))
    if s >= 86400:
        return '%dd %dh' % (s // 86400, (s % 86400) // 3600)
    return hhmm(s * 1000)


def hms(ms_) -> str:
    """A duration as `HH:MM:SS` (hours never roll over into days: a weekly window with 3 days left
    is `72:00:00`).

    The "countdown to refill" badge above her head uses it: second by second, far more precise than
    a coarse "3 days 4 hours" (the renderer recomputes it every frame, so it really ticks rather
    than jumping once a minute).
    """
    s = max(0, int(ms_ // 1000))
    return '%02d:%02d:%02d' % (s // 3600, (s % 3600) // 60, s % 60)


def go_reset_text(info, now=None) -> str:
    """How long until the reset (said plainly when no reset time was given, or it already passed)."""
    at = (info or {}).get('resetAt')
    if not at:
        return ''
    left = at - (now or now_ms())
    return 'waiting for the reset' if left <= 0 else ('resets in ' + dhmm(left))


def parse_go_usage(js):
    """Break an OpenCode Go response into {window: {'percent', 'status', 'resetAt'}}.

    Only the three GO_WINS windows are picked: a future extra window cannot break anything, and a
    missing window is not an error either (the ones that are there still show). But "no usage at
    all" and "can't read percent" MUST be reported -- showing a wrong number is worse than showing
    nothing.
    """
    usage = js.get('usage') if isinstance(js, dict) else None
    if not isinstance(usage, dict):
        raise ValueError('the response has no usage field')
    out = {}
    for k in GO_WINS:
        info = usage.get(k)
        if info is None:
            continue
        if not isinstance(info, dict):
            raise ValueError('usage.%s is not an object' % k)
        pct = go_pct(info)
        if pct is None:
            raise ValueError('cannot read the percent of usage.%s' % k)
        out[k] = {'percent': max(0.0, pct), 'status': str(info.get('status') or ''),
                  'resetAt': iso_ms(info.get('resetsAt'))}
    if not out:
        raise ValueError('none of the three usage windows are present')
    return out


# =====================================================================
# 2) Config + data fetching
# =====================================================================
def config_path():
    """The config lives in %APPDATA%\\dswhale_pet\\config.json (override with --config)."""
    env = os.environ.get('DSWHALE_PET_CONFIG')
    if env:
        return env
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(base, APP, 'config.json')


def load_config(path=None):
    path = path or config_path()
    cfg = dict(CFG_DEFAULT)
    try:
        with open(path, encoding='utf-8') as fp:
            saved = json.load(fp)
        if isinstance(saved, dict):
            for k in list(cfg):
                if k in saved:
                    cfg[k] = saved[k]
    except FileNotFoundError:
        pass
    except Exception as e:                       # a broken config must never block the pet
        print('[warn] cannot read the config file (continuing with defaults): %s' % e)
    return cfg


def save_config(cfg, path=None):
    path = path or config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)                    # atomic replace, never a half-written file
        return path
    except Exception as e:
        print('[warn] cannot save the config: %s' % e)
        return None


def http_error_text(code, raw):
    """An HTTP error as one readable line: prefers the message the API put in its own JSON.

    Both DeepSeek and opencode.ai answer with `{"error": {"message": ...}}`, so the wording is
    settled in one place and shared by both fetch paths (a 401 looks like "HTTP 401: Unauthorized").
    """
    msg = (raw or '')[:140]
    try:
        js = json.loads(raw)
        err = js.get('error') if isinstance(js, dict) else None
        if isinstance(err, dict) and err.get('message'):
            msg = str(err['message'])
    except Exception:
        pass
    return 'HTTP %d: %s' % (code, msg or '')


def fetch_balance(key, timeout=10):
    """GET the balance endpoint. On failure the original error text is handed to the user (both the
    bubble and the message bar print it)."""
    req = urllib.request.Request(API, method='GET', headers={
        'Accept': 'application/json',
        'Authorization': 'Bearer ' + key,
        'Cache-Control': 'no-store',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        raw = ''
        try:
            raw = e.read().decode('utf-8', 'replace')
        except Exception:
            pass
        raise RuntimeError(http_error_text(e.code, raw))
    except urllib.error.URLError as e:
        raise RuntimeError('request could not be sent: %s' % e.reason)
    except Exception as e:
        raise RuntimeError('request timed out or the network failed: %s' % e)
    try:
        js = json.loads(raw)
    except Exception:
        raise RuntimeError('cannot make sense of the response: ' + raw[:140])
    if not isinstance(js, dict):
        raise RuntimeError('the response is not a JSON object: ' + raw[:140])
    return js


def fetch_go_usage(key, timeout=10):
    """GET the OpenCode Go usage endpoint (**read-only**: spends no quota, changes nothing).

    Returns the raw JSON -- with the shape (usage / percent) validated in here so bad data never
    floats up to the UI; a wrong shape raises right away. The error wording follows the balance
    route: HTTP status plus whatever the API says, so a bad key is instantly recognisable as
    "HTTP 401: Unauthorized".
    """
    req = urllib.request.Request(GO_API, method='GET', headers={
        'Accept': 'application/json',
        'Authorization': 'Bearer ' + key,
        'Cache-Control': 'no-store',
        'User-Agent': 'dswhale_pet/1.0',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        raw = ''
        try:
            raw = e.read().decode('utf-8', 'replace')
        except Exception:
            pass
        raise RuntimeError(http_error_text(e.code, raw))
    except urllib.error.URLError as e:
        raise RuntimeError('request could not be sent: %s' % e.reason)
    except Exception as e:
        raise RuntimeError('request timed out or the network failed: %s' % e)
    try:
        js = json.loads(raw)
    except Exception:
        raise RuntimeError('cannot make sense of the response: ' + raw[:140])
    if not isinstance(js, dict):
        raise RuntimeError('the response is not a JSON object: ' + raw[:140])
    try:
        parse_go_usage(js)
    except ValueError as e:
        raise RuntimeError('cannot make sense of the response: %s' % e)
    return js


def parse_balance_text(text):
    """Fallback: parse a whole block of JSON pasted in by hand (for when a browser or the network
    gets in the way of the API)."""
    try:
        js = json.loads(text)
    except Exception as e:
        raise RuntimeError('JSON parse failed: %s' % e)
    if not isinstance(js, dict):
        raise RuntimeError('what was pasted is not a JSON object')
    return js


class ApiWorker(QThread):
    """Background thread polling the balance (with the OpenCode Go usage polled along the way).

    The main thread calls ask() and it checks once right away; otherwise it sleeps for pollSec and
    checks again when it wakes up -- the Go leg is timed separately by goSec (both live in this one
    thread, so its real interval can never be tighter than pollSec). Cross-thread signals are
    queued, so updating the UI back on the main thread is safe.
    """

    got = pyqtSignal(dict, str)
    bad = pyqtSignal(str)
    gotGo = pyqtSignal(dict)
    badGo = pyqtSignal(str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._wake = threading.Event()
        self._stop = False
        self._goAt = 0.0              # when the Go usage was last fetched (time.monotonic)

    def ask(self):
        self._goAt = 0.0              # manual refresh / connection test: fetch Go next beat too
        self._wake.set()

    def resume(self):
        """Start (or restart) polling."""
        if not self.isRunning():
            self._stop = False
            self.start()
        self.ask()

    def stop(self):
        self._stop = True
        self._wake.set()
        if self.isRunning():
            self.wait(2500)

    def go_due(self, t=None):
        """Is this beat the time to "also" fetch the Go usage (only DeepSeek mode ever asks).

        In Go mode the usage IS the main data and gets fetched every beat (the interval is pollSec),
        see run(). With no goKey nothing is fetched at all -- no key, no network.
        """
        if not (self.cfg.get('goKey') or '').strip():
            return False
        return (t if t is not None else time.monotonic()) - self._goAt >= max(
            15, int(self.cfg.get('goSec') or 60))

    def go_mode(self):
        return (self.cfg.get('mode') or 'deepseek') == 'go'

    def tick(self):
        key = (self.cfg.get('key') or '').strip()
        if not key:
            self.bad.emit('no API key yet')
            return
        try:
            self.got.emit(fetch_balance(key), 'api')
        except Exception as e:
            self.bad.emit(str(e))

    def tick_go(self):
        key = (self.cfg.get('goKey') or '').strip()
        if not key:
            return
        try:
            self.gotGo.emit(fetch_go_usage(key))
        except Exception as e:
            self.badGo.emit(str(e))

    def run(self):
        while not self._stop:
            t = time.monotonic()
            if self.go_mode():
                # Go mode: the usage is the main data, fetched every beat (pollSec); the DeepSeek
                # side is never touched (two keys, two services -- cooking with Go usage means the
                # official tokens are not used)
                if (self.cfg.get('goKey') or '').strip():
                    self._goAt = t
                    self.tick_go()
            else:
                self.tick()
                if self.go_due(t):
                    self._goAt = t
                    self.tick_go()
            self._wake.wait(max(3, int(self.cfg.get('pollSec') or 10)))
            self._wake.clear()


# =====================================================================
# 3) Rendering: draw what a given moment should look like
#    Every animation is a function of t (seconds) and the particle state, so rendering offscreen at
#    a fixed t is all it takes to verify them -- that is the path tools/verify_pet.py takes when it
#    writes its images.
# =====================================================================
def text_w(fm, txt):
    try:
        return fm.horizontalAdvance(txt)
    except AttributeError:
        return fm.width(txt)


def pick_font(px, bold=False):
    f = QFont()
    try:
        f.setFamilies(['Microsoft YaHei UI', 'Microsoft YaHei', 'Segoe UI Emoji',
                       'Segoe UI', 'sans-serif'])
    except AttributeError:                       # Qt < 5.13
        f.setFamily('Microsoft YaHei UI')
    f.setPixelSize(max(8, int(px)))
    f.setBold(bool(bold))
    return f


def desaturate(pix):
    """Desaturated portrait: used by "out of rice".

    Qt5's QPainter has no HSL blend modes (neither Saturation nor Luminosity), so this goes
    "grayscale -> paste it back through the original alpha as a mask -> lay a cold tint on top":
    no per-pixel Python loop, pure Qt work, a few milliseconds for a 640px image.
    """
    src = pix.toImage().convertToFormat(QImage.Format_ARGB32)
    gray = src.convertToFormat(QImage.Format_Grayscale8)
    out = QImage(src.size(), QImage.Format_ARGB32)
    out.setDevicePixelRatio(src.devicePixelRatio() or 1.0)   # keep the copy crisp on high DPI
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.drawImage(0, 0, gray.convertToFormat(QImage.Format_ARGB32))
    p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    p.drawImage(0, 0, src)                       # keep only where the original is opaque
    p.setCompositionMode(QPainter.CompositionMode_SourceAtop)
    p.fillRect(out.rect(), QColor(140, 168, 200, 46))    # a cold tint, so it reads as "cold"
    p.end()
    return QPixmap.fromImage(out)


class PetArt:
    """The character art. Prefers the **frame animations** cut out from reference/
    (assets/pet/anim/), falls back to the static portraits in assets/pet/*.png -- both carry the
    bowl rim position (that is where the particles are emitted)."""

    def __init__(self, meta_path=META_PATH):
        if not os.path.exists(meta_path):
            raise SystemExit('missing %s\nrun this first: python tools/make_pet_assets.py' % meta_path)
        with open(meta_path, encoding='utf-8') as fp:
            self.meta = json.load(fp)
        missing = [k for k in STATE_KEYS if k not in self.meta]
        if missing:
            raise SystemExit('pet_meta.json is missing these states: %s' % missing)
        self.bowl = {}
        for k in STATE_KEYS:
            m = self.meta[k]
            self.bowl[k] = (float(m['bowl'][0]), float(m['bowl'][1]))

        self.anim = {}
        if os.path.exists(ANIM_META):                 # frame animations: use them, or act as if absent
            try:
                with open(ANIM_META, encoding='utf-8') as fp:
                    am = json.load(fp)
                for k in STATE_KEYS:
                    a = am.get(k) or {}
                    if int(a.get('n') or 0) > 0 and os.path.isdir(os.path.join(ROOT, a['dir'])):
                        self.anim[k] = a
                        self.bowl[k] = (float(a['bowl'][0]), float(a['bowl'][1]))
            except Exception as e:
                print('cannot read the frame table (%s), falling back to the static portraits' % e)
                self.anim = {}

        self._cache = {}          # static portraits
        self._gray = {}           # desaturated versions
        self._frames = {}         # frame animations: cached by (state, size, frame index)
        self.dpr = 1.0            # device pixel ratio for rasterizing (= screen DPR, see set_dpr)

    def set_dpr(self, dpr):
        """Tell the art at what device resolution these frames get drawn; on change, clear the
        cache and rasterize again."""
        dpr = max(1.0, float(dpr or 1.0))
        if abs(dpr - self.dpr) > 1e-6:
            self.dpr = dpr
            self.clear_cache()

    # ---------- frame animations ----------
    def n_frames(self, state):
        a = self.anim.get(state)
        return int(a['n']) if a else 1

    def fps(self, state):
        a = self.anim.get(state)
        return max(1.0, float(a.get('fps') or 10.0)) if a else 0.0

    def _fit(self, src, m, size):
        """Scale one frame to the target size -- **rasterize for the device resolution**, not "shrink
        it to the logical size and call it a day".

        Logical size (the window layout, the bowl rim ratio and the particles all work in it) and
        pixel size are two different things: the art is 528px wide and the screen DPR is 2.0, so
        rasterizing at 1x would make Qt **scale that 240px image up again** on every paint -- soft
        edges, and the smaller the pet the more obvious it is (a one-pixel edge smeared over two).

        So here:
        1) target pixels = logical size x dpr (a bigger canvas means detail is not thrown away
           early);
        2) when shrinking, **halve step by step**: going straight to 1/3 drops samples and leaves
           fuzzy edges, while a few steps amounts to averaging over regions -- the same idea as
           "average over every target pixel when rasterizing a vector" (what Qt itself recommends
           for downscaling);
        3) afterwards the devicePixelRatio is set back, so "logical size = target size, device
           pixels = dpr times that" lands 1:1 on screen with neither scaling nor resampling --
           crisp at any size.
        """
        w = max(24, int(round(size)))
        h = max(24, int(round(size * float(m['h']) / float(m['w']))))
        tw = max(1, int(round(w * self.dpr)))
        th = max(1, int(round(h * self.dpr)))
        out = src
        while out.width() > 2 * tw or out.height() > 2 * th:
            out = out.scaled(max(tw, out.width() // 2), max(th, out.height() // 2),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
        out = out.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        out.setDevicePixelRatio(self.dpr)
        return out

    def frame(self, state, size, idx):
        """Frame idx (wraps around modulo the frame count). Falls back to the static image when
        there is no frame art."""
        a = self.anim.get(state)
        if not a:
            return self.pix(state, size)
        idx = int(idx) % max(1, int(a['n']))
        key = (state, int(size), idx)
        if key not in self._frames:
            path = os.path.join(ROOT, a['dir'], '%03d.png' % idx)
            src = QPixmap(path)
            if src.isNull():
                raise SystemExit('cannot read the animation frame: %s' % path)
            self._frames[key] = self._fit(src, a, size)
        return self._frames[key]

    # ---------- static portraits (the fallback when frame art is missing) ----------
    def pix(self, state, size):
        if state in self.anim:
            return self.frame(state, size, 0)
        key = (state, int(size))
        if key not in self._cache:
            m = self.meta[state]
            src = QPixmap(os.path.join(ROOT, m['file']))
            if src.isNull():
                raise SystemExit('cannot read the portrait: %s' % m['file'])
            self._cache[key] = self._fit(src, m, size)
        return self._cache[key]

    def gray(self, state, size):
        key = (state, int(size))
        if key not in self._gray:
            self._gray[key] = desaturate(self.pix(state, size))
        return self._gray[key]

    def clear_cache(self):
        """Clear after a size change or new art, so scaled frames do not pile up forever."""
        self._cache.clear()
        self._gray.clear()
        self._frames.clear()


class FX:
    """Particles: steam / rice grains / speed streaks / dust / coins.

    The numbers are tuned against a 300px baseline and multiplied by a k at render time
    (= pet long edge / 300), so the look does not change when the pet is resized. Emission density
    follows the measured rate: the faster the burn, the crazier it gets.
    """

    MAX = 420

    def __init__(self):
        self.parts = []
        self.emit = 0.0
        self.ring_t = 0.0
        self.mode = 'steam'
        self.mult = 1.0

    def set_mode(self, mode, mult):
        if mode != self.mode:
            self.parts = []
            self.emit = 0.0
        self.mode = mode
        self.mult = clamp(mult, 0.5, 4.0)

    def _add(self, p):
        if len(self.parts) < self.MAX:
            self.parts.append(p)

    # ---------- emission ----------
    def rice(self, bx, by, k, v=1.0):
        self._add(dict(kind='rice', x=bx + random.uniform(-26, 26) * k,
                       y=by + random.uniform(-14, 6) * k,
                       vx=random.uniform(-1, 1) * (24 + 22 * v) * k,
                       vy=-(38 + 62 * v) * random.uniform(.6, 1.2) * k, g=210 * k,
                       life=0.0, max=random.uniform(.8, 1.4),
                       s=random.uniform(1.5, 3.2) * k,
                       rot=random.uniform(0, 6.28), vr=random.uniform(-7, 7)))

    def streak(self, bx, by, k, v=1.0):
        a = random.uniform(0, 6.283)
        sp = random.uniform(190, 260 + 320 * v) * k
        self._add(dict(kind='streak', x=bx, y=by, vx=math.cos(a) * sp,
                       vy=math.sin(a) * sp * .55, life=0.0,
                       max=random.uniform(.18, .4), s=random.uniform(1.1, 2.4) * k,
                       c='#ffd479' if random.random() < .55 else '#ff7b45'))

    def steam(self, bx, by, k):
        self._add(dict(kind='steam', x=bx + random.uniform(-24, 24) * k, y=by - 8 * k,
                       vx=random.uniform(-6, 6) * k, vy=random.uniform(-26, -12) * k,
                       life=0.0, max=random.uniform(2.2, 3.8),
                       s=random.uniform(7, 18) * k, ph=random.uniform(0, 6.28)))

    def dust(self, W, k):
        self._add(dict(kind='dust', x=random.uniform(0, W), y=random.uniform(-20, -4) * k,
                       vx=random.uniform(-10, 10) * k, vy=random.uniform(9, 22) * k,
                       life=0.0, max=random.uniform(5, 9),
                       s=random.uniform(.9, 2.4) * k, ph=random.uniform(0, 6.28)))

    def coin(self, bx, by, k):
        self._add(dict(kind='coin', x=bx + random.uniform(-40, 40) * k, y=by - 8 * k,
                       vx=random.uniform(-70, 70) * k, vy=random.uniform(-260, -150) * k,
                       g=420 * k, life=0.0, max=random.uniform(1.1, 1.9),
                       s=random.uniform(3.5, 6.5) * k,
                       rot=random.uniform(0, 6.28), vr=random.uniform(-9, 9)))

    def burst(self, kind, n=24, bx=0.0, by=0.0, k=1.0):
        for _ in range(n):
            if kind == 'coin':
                self.coin(bx, by, k)
            elif kind == 'rice':
                self.rice(bx, by, k, 1.6)
            else:
                self.streak(bx, by, k, 1.6)
        self.ring_t = 0.001

    def _n(self, want):
        return max(0, min(int(want), 24))            # never spit out hundreds at once when it stutters

    def spawn(self, dt, bx, by, k, W):
        m = self.mode
        rate = {'steam': 1.9 * self.mult, 'rice': 5 * self.mult,
                'streak': 22 * self.mult, 'dust': 1.1}.get(m, 0.0)
        if rate <= 0:
            return
        self.emit += dt * rate
        count = self._n(self.emit)
        self.emit -= count
        for _ in range(count):
            if m == 'steam':
                self.steam(bx, by, k)
            elif m == 'rice':
                self.rice(bx, by, k, self.mult * .5)
            elif m == 'streak':
                (self.streak if random.random() < .62 else self.rice)(
                    bx, by, k, self.mult * .5)
            else:
                self.dust(W, k)
        if m == 'streak':
            self.ring_t += dt
            if self.ring_t > 1.6:
                self.ring_t = 0.0001

    def step(self, dt, bx, by, k, W):
        self.spawn(dt, bx, by, k, W)
        alive = []
        for p in self.parts:
            p['life'] += dt
            if p['life'] >= p['max']:
                continue
            kind = p['kind']
            if kind == 'steam':
                p['x'] += p['vx'] * dt + math.sin(p['life'] * 1.7 + p['ph']) * 12 * dt * k
                p['y'] += p['vy'] * dt
            elif kind in ('rice', 'coin'):
                p['vy'] += p['g'] * dt
                p['x'] += p['vx'] * dt
                p['y'] += p['vy'] * dt
                p['rot'] += p['vr'] * dt
            elif kind == 'streak':
                p['x'] += p['vx'] * dt
                p['y'] += p['vy'] * dt
            elif kind == 'dust':
                p['x'] += p['vx'] * dt + math.sin(p['life'] + p['ph']) * 8 * dt * k
                p['y'] += p['vy'] * dt
            alive.append(p)
        self.parts = alive

    # ---------- painting (blend modes: rice/streaks add up with "lighter", the faster the brighter) ----------
    def paint(self, p):
        p.save()
        p.setPen(Qt.NoPen)
        for q in self.parts:
            t = clamp(q['life'] / q['max'], 0.0, 1.0)
            kind = q['kind']
            if kind == 'steam':
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                a = 0.15 * (1 - t) * (t / 0.15 if t < 0.15 else 1.0)
                p.setBrush(QColor(207, 230, 255, int(255 * a)))
                p.drawEllipse(QPointF(q['x'], q['y']), q['s'], q['s'] * 0.85)
            elif kind == 'rice':
                p.setCompositionMode(QPainter.CompositionMode_Plus)
                p.setBrush(QColor(255, 243, 220, int(255 * 0.85 * (1 - t * t))))
                p.save()
                p.translate(q['x'], q['y'])
                p.rotate(math.degrees(q['rot']))
                s = q['s']
                p.drawEllipse(QPointF(0, 0), s, s * 0.62)
                p.restore()
            elif kind == 'streak':
                p.setCompositionMode(QPainter.CompositionMode_Plus)
                c = QColor(q['c'])
                c.setAlphaF(clamp(0.9 * (1 - t), 0.0, 1.0))
                p.setPen(QPen(c, max(0.6, q['s'] * (1 - t * 0.5)),
                               Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(q['x'], q['y']),
                           QPointF(q['x'] - q['vx'] * 0.012, q['y'] - q['vy'] * 0.012))
                p.setPen(Qt.NoPen)
            elif kind == 'dust':
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                a = 0.4 * min(1.0, t * 4) * (1 - t)
                p.setBrush(QColor(157, 196, 255, int(255 * a)))
                p.drawEllipse(QPointF(q['x'], q['y']), q['s'], q['s'])
            elif kind == 'coin':
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                p.setBrush(QColor(255, 212, 121, int(255 * (1 - t * t))))
                p.setPen(QPen(QColor(183, 121, 31), max(0.8, q['s'] * 0.22)))
                p.save()
                p.translate(q['x'], q['y'])
                p.rotate(math.degrees(q['rot']))
                p.drawEllipse(QPointF(0, 0), q['s'],
                              max(0.5, q['s'] * abs(math.cos(q['rot']))))
                p.restore()
                p.setPen(Qt.NoPen)
        p.restore()


class PetRenderer:
    """Draws the pet frame by frame.

    There is no timer inside draw(): hand it a t (seconds) and the current Core and it renders that
    instant. The window's paintEvent and the offline acceptance run take the same path, so the
    images the acceptance suite writes are the real thing.
    """

    FADE = 0.35                                  # cross-fade on a state change (last frame fades out frozen)

    def __init__(self, art, cfg):
        self.art = art
        self.cfg = cfg
        self.fx = FX()
        self.size = int(cfg.get('size') or 300)
        self.fade_from = None
        self.fade_t0 = -99.0
        self.fade_pm = None                      # the frame **frozen** at the switch (fades out, no longer plays)
        self.state_t0 = 0.0                      # when the current state began: animation restarts at frame 0
        self.ring_t0 = None
        self.stamp_t0 = None
        self.toast = ''
        self.toast_t0 = -99.0
        self.toast_ms = 2400
        self.poke_t0 = -99.0
        self.last_state = None
        self.reactions = 0                        # how many pokes (used by the acceptance run)
        self.floats = []                          # "just spent / just topped up" labels floating above the bowl rim

    # ---------- size and layout ----------
    def set_size(self, px):
        px = int(clamp(px, 96, 640))
        if px == self.size:
            return False
        self.size = px
        self.art.clear_cache()                  # size changed: drop the scaled frames
        return True

    def pad(self):
        return int(round(self.size * 0.26))       # headroom for steam and speed streaks

    def pet_px(self, state):
        pm = self.art.pix(state, self.size)
        r = pm.devicePixelRatio() or 1.0          # the pixmap is rasterized at dpr, layout wants logical px
        return int(round(pm.width() / r)), int(round(pm.height() / r))

    def stage(self):
        w = h = 0
        for s in STATE_KEYS:                      # largest of the four, so the window does not jump
            pw, ph = self.pet_px(s)
            w = max(w, pw)
            h = max(h, ph)
        pad = self.pad()
        return w + pad * 2, h + pad * 2

    def pet_rect(self, state, W, H):
        pw, ph = self.pet_px(state)
        pad = self.pad()
        return QRectF((W - pw) / 2.0, H - pad - ph, pw, ph)

    def bowl_pt(self, state, W, H):
        r = self.pet_rect(state, W, H)
        bx, by = self.art.bowl[state]
        return r.x() + bx * r.width(), r.y() + by * r.height()

    def scale_k(self):
        return self.size / 300.0

    # ---------- small outward-facing actions ----------
    def set_toast(self, text, ms=2400, t=0.0):
        self.toast = text or ''
        self.toast_ms = ms
        self.toast_t0 = t

    def poke(self, t):
        self.poke_t0 = t
        self.reactions += 1

    # ---------- the balance on the bowl / the floating labels above the rim ----------
    FLOAT_LIFE = 2.8          # how many seconds one floating label lives
    FLOAT_RISE = 0.20         # how far it drifts up (fraction of the pet pixels)
    FLOAT_Y0 = 0.20           # it starts this far above the rim (the bowl block peaks ~0.155 above its centroid)
    BOWL_LABEL_DY = 0.082     # where the balance sits on the bowl: this much x size below the centroid of the
                              # bowl block (bowl + rice)

    def spawn_float(self, ev, bx, by, t, sym='', unit='', dec=2):
        """A balance change -> add one floating label above the bowl rim (bx/by is the rim position
        inside the window).

        The unit comes from the caller (`Core.float_style()`): money is `−¥0.03`, Go is `−1.0%`.
        Same direction and the previous one still floating (within 0.6s)? The amount is **merged
        into it**: with a 10 second poll that is fine, but in demo mode or with two samples in a
        row a stack of labels turns into a blur of digits. Merging also pulls it back to the start
        and floats it again -- the number jumps, which is exactly the "another charge" hint.
        """
        kind, amt = ev['kind'], ev['amount']
        last = self.floats[-1] if self.floats else None
        if last is not None and last['kind'] == kind and t - last['t0'] < 0.6:
            last['amt'] += amt
            last['t0'] = t
            return
        # spread them out by index so they do not stack on one vertical line; no RNG, so screenshots
        # stay reproducible
        self.floats.append({'kind': kind, 'amt': float(amt), 't0': t, 'sym': sym,
                            'unit': unit, 'dec': dec,
                            'dx': ((len(self.floats) % 3) - 1) * 0.05 * self.size})
        if len(self.floats) > 4:
            self.floats.pop(0)

    def step_floats(self, t):
        """Drop the ones that have finished floating."""
        self.floats = [f for f in self.floats if t - f['t0'] <= self.FLOAT_LIFE]

    def _floats(self, p, t, bx, by):
        """Money just spent (-) / just topped up (+): floats up from the bowl rim, fading as it
        goes, gone after a few seconds."""
        for f in self.floats:
            age = t - f['t0']
            if age < 0 or age > self.FLOAT_LIFE:
                continue
            f01 = age / self.FLOAT_LIFE
            a = clamp(age / 0.12, 0.0, 1.0) * clamp((1.0 - f01) / 0.45, 0.0, 1.0)
            rise = (1.0 - (1.0 - f01) ** 2) * self.FLOAT_RISE * self.size    # fast first, then slow
            spend = f['kind'] == 'spend'
            txt = ('−' if spend else '+') + f['sym'] + fmt(f['amt'], f.get('dec', 2)) \
                + f.get('unit', '')
            fnt = pick_font(max(9, self.size * 0.078), bold=True)
            fm = QFontMetrics(fnt)
            x = bx + f['dx'] - text_w(fm, txt) / 2.0
            y = by - self.size * self.FLOAT_Y0 - rise + fm.ascent()
            p.save()
            p.setOpacity(a)
            p.setFont(fnt)
            p.setPen(QColor(24, 28, 36, 200))                  # a dark copy underneath as a shadow, so it
            p.drawText(QPointF(x + 1.2, y + 1.2), txt)         # stays readable over the white bowl and
                                                               # light clothes (Qt has no text outline,
                                                               # so the text is simply drawn twice)
            p.setPen(QColor(255, 198, 124) if spend else QColor(150, 240, 170))
            p.drawText(QPointF(x, y), txt)
            p.restore()

    def _balance(self, p, t, core, bx, by, st, k):
        """Write the balance straight onto the **body of the bowl** -- no background, no frame; the
        bowl itself is the backdrop.

        Every choice follows "never cover the art": no frame, no fill, a near-black bold face over
        the white porcelain plus a very light white outline (so it stays crisp even where it lands
        on the bowl's own linework); what it writes comes from the mode (`Core.lab()`): DeepSeek is
        `¥88.50` (the currency follows the API), Go is `Left 65%`.
        It sits well below the rim, for the reasons spelled out on `BOWL_LABEL_DY` above.
        """
        if core.curBal is None:
            return
        txt = core.lab()
        px = max(9.0, self.size * 0.095)
        fnt = pick_font(px, bold=True)
        fm = QFontMetrics(fnt)
        tw = text_w(fm, txt)
        while tw > self.size * 0.34 and px > 9.0:              # more digits -> smaller font
            px = max(9.0, px * 0.92)
            fnt = pick_font(px, bold=True)
            fm = QFontMetrics(fnt)
            tw = text_w(fm, txt)
        cy = by + self.BOWL_LABEL_DY * self.size
        r = QRectF(bx - tw / 2.0, cy - fm.height() / 2.0, tw, fm.height())
        p.save()
        p.setFont(fnt)
        p.setPen(QColor(255, 255, 255, 130))                   # a light white outline: stays crisp over the linework
        for dx, dy in ((-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0)):
            p.drawText(r.translated(dx, dy), Qt.AlignCenter, txt)
        p.setPen(QColor(26, 28, 33, 235))                      # near-black bold; the bowl is its background
        p.drawText(r, Qt.AlignCenter, txt)
        p.restore()

    # ---------- drawing one frame ----------
    def draw(self, p, t, core, dt=0.016):
        self.art.set_dpr(p.device().devicePixelRatioF())   # rasterize for this device's DPR (re-done on a screen change)
        st = core.state
        if st != self.last_state:
            if self.last_state is not None:       # a real switch is the only thing that gets a ripple/toast
                self.fade_pm = self._piece(self.last_state, t)  # first **freeze** the previous animation
                self.fade_from = self.last_state
                self.fade_t0 = t
                self.ring_t0 = t
                # The toast shows the line for "coming from the previous state": entering the fast
                # state from "eating" and coming back from "out of rice" (a top-up landed) really
                # should not say the same thing
                self.set_toast(core.state_msg(self.last_state),
                               1800 if st == 'fast' else 2600, t)
            if st == 'empty':
                self.stamp_t0 = t
            self.state_t0 = t                     # the new animation starts at frame 0 (not mid-phase)
            self.last_state = st

        self.fx.set_mode(STATES[st]['fx'], core.fx_mult())
        W, H = self.stage()
        rect = self.pet_rect(st, W, H)
        bx, by = self.bowl_pt(st, W, H)
        k = self.scale_k()

        for ev in core.take_events():                 # fresh changes: float one above the bowl rim
            self.spawn_float(ev, bx, by, t, **core.float_style())
        self.step_floats(t)

        self.fx.step(dt, bx, by, k, W)
        self._ground(p, rect, QColor(STATES[st]['accent']), st, t)
        self._pet(p, t, st, rect, k)
        self.fx.paint(p)
        self._balance(p, t, core, bx, by, st, k)     # the balance written on the bowl
        self._floats(p, t, bx, by)                   # just spent / just topped up, drifting up
        self._ring(p, t, bx, by, k, st)
        if st == 'empty' and self.stamp_t0 is not None:
            self._stamp(p, t, rect)
        self._quota_badge(p, t, core, W, rect)      # Go mode out of quota: the "refill in HH:MM:SS" line
        self._toast(p, t, W, rect, st)              # the toast is drawn last: it covers the badge and stays readable
        return W, H

    # ---------- shadow + ground glow ----------
    def _ground(self, p, rect, acc, st, t):
        cx = rect.center().x()
        cy = rect.bottom() - rect.height() * 0.04
        p.save()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 62))               # the shadow that grounds her (otherwise she floats)
        p.drawEllipse(QPointF(cx, cy), rect.width() * 0.36, rect.height() * 0.028)
        pulse = 0.14 + 0.09 * math.sin(2 * math.pi * 0.28 * t)
        c1 = QColor(acc)
        c1.setAlphaF(clamp(pulse, 0.0, 0.5))
        c2 = QColor(acc)
        c2.setAlphaF(0.0)
        g = QRadialGradient(QPointF(cx, rect.center().y()), rect.width() * 0.8)
        g.setColorAt(0.0, c1)
        g.setColorAt(1.0, c2)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, rect.center().y()),
                      rect.width() * 0.8, rect.height() * 0.6)
        p.restore()

    @staticmethod
    def _blit(p, pm, rect, pivot, br, rot, alpha):
        if alpha <= 0.004:
            return
        p.save()
        p.setOpacity(clamp(alpha, 0.0, 1.0))
        p.translate(pivot)
        p.rotate(rot)
        p.scale(br, br)
        p.drawPixmap(QPointF(-rect.width() / 2.0, -rect.height()), pm)
        p.restore()

    # ---------- the art: frame animations (programmatic breathing/sway only without art) + cross-fade ----------
    def _pet(self, p, t, st, rect, k):
        if st in self.art.anim:
            # The motion (scooping rice / to the mouth / chewing) already lives in every frame of
            # the art, so **nothing is layered on top** here -- another wobble would only fight the
            # frames.
            br, rot, dx, dy = 1.0, 0.0, 0.0, 0.0
        elif st == 'ready':
            br = 1 + 0.012 * math.sin(2 * math.pi * 0.33 * t)
            rot = 1.2 * math.sin(2 * math.pi * 0.18 * t)
            dx = dy = 0.0
        elif st == 'empty':
            br = 1 + 0.006 * math.sin(2 * math.pi * 0.25 * t)
            rot = 2.4 * math.sin(2 * math.pi * 0.22 * t)
            dx = 0.0
            dy = 0.8 * math.sin(2 * math.pi * 0.4 * t)
        else:                                        # static fallback for eating / fast: breathe only
            br = 1 + 0.014 * math.sin(2 * math.pi * 0.30 * t)
            rot = 0.9 * math.sin(2 * math.pi * 0.16 * t)
            dx = dy = 0.0

        pk = t - self.poke_t0                        # poked: one bounce (a one-shot reaction)
        if 0.0 <= pk < 0.55:
            f = pk / 0.55
            br *= 1 + 0.06 * math.sin(math.pi * f) * (1 - f)
            dy -= 7.0 * k * math.sin(math.pi * f) * (1 - f)

        f = 1.0
        if self.fade_from:
            f = clamp((t - self.fade_t0) / self.FADE, 0.0, 1.0)
            if f >= 1.0:
                self.fade_from = None
                self.fade_pm = None
        pivot = QPointF(rect.center().x() + dx, rect.bottom() + dy)
        p.save()
        if self.fade_from and self.fade_pm is not None and f < 1.0:
            # The previous piece is a **frozen** still: it only fades, it no longer advances.
            # (It used to keep picking frames with t, i.e. two sets of motion at once -- which read
            # as "the switch did not happen".)
            self._blit(p, self.fade_pm, rect, pivot, br, rot, 1.0 - f)
        self._blit(p, self._piece(st, t), rect, pivot, br, rot, f)
        p.restore()

    def _piece(self, st, t):
        """The image to draw right now: the matching frame (looping) with frame art, the static
        portrait otherwise.

        The frame index comes from the **local clock of the current state** (`state_t0`), so a
        switch always starts at frame 0. Using the absolute clock `t` made a new state pop up
        halfway through the previous animation's phase, and with the old animation still fading out
        that read as "the switch did not happen".
        """
        if st in self.art.anim:
            n = max(1, self.art.n_frames(st))
            idx = int(max(0.0, t - self.state_t0) * self.art.fps(st)) % n
            return self.art.frame(st, self.size, idx)
        return self.art.gray(st, self.size) if st == 'empty' else self.art.pix(st, self.size)

    # ---------- ripples / shockwaves at the bowl rim ----------
    def _ring(self, p, t, bx, by, k, st):
        p.save()
        p.setBrush(Qt.NoBrush)
        if self.ring_t0 is not None:                  # the instant of a state change
            f = (t - self.ring_t0) / 0.55
            if f >= 1.0:
                self.ring_t0 = None
            else:
                rw = (0.16 + 0.62 * f) * self.size
                c = QColor(STATES[st]['accent'])
                c.setAlphaF(clamp(0.45 * (1 - f), 0.0, 1.0))
                p.setPen(QPen(c, max(1.0, 3.2 * k * (1 - f))))
                p.drawEllipse(QPointF(bx, by), rw, rw * 0.42)
        if self.fx.mode == 'streak' and 0 < self.fx.ring_t < 0.5:
            f = self.fx.ring_t / 0.5
            rw = (10 + f * self.size * 0.36) * k
            p.setPen(QPen(QColor(255, 180, 110, int(255 * 0.5 * (1 - f))), 2.6 * k))
            p.drawEllipse(QPointF(bx, by), rw, rw * 0.5)
        p.restore()

    # ---------- out of rice: the stamp ----------
    def _stamp(self, p, t, rect):
        age = t - self.stamp_t0
        if age > 4.0:
            return
        f = clamp(age / 0.55, 0.0, 1.0)
        s = 1.0 + 1.8 * math.exp(-3.6 * f) * math.cos(9.0 * f)     # the bounce of a stamp hitting the page
        alpha = clamp(age / 0.22, 0.0, 1.0) * 0.92
        txt = 'OUT OF CREDIT'
        fnt = pick_font(max(12, rect.height() * 0.105), bold=True)
        fm = QFontMetrics(fnt)
        tw = text_w(fm, txt) + rect.width() * 0.12
        th = fm.height() + rect.height() * 0.022
        box = QRectF(-tw / 2.0, -th / 2.0, tw, th)
        p.save()
        p.setOpacity(alpha)
        p.translate(rect.center().x(), rect.center().y() + rect.height() * 0.04)
        p.rotate(-9.0)
        p.scale(s, s)
        p.setBrush(QColor(190, 40, 40, 46))
        p.setPen(QPen(QColor(255, 90, 70, 230), max(1.6, rect.height() * 0.0075)))
        p.drawRoundedRect(box, th * 0.24, th * 0.24)
        p.setPen(QColor(255, 214, 205))
        p.setFont(fnt)
        p.drawText(box, Qt.AlignCenter, txt)
        p.restore()

    # ---------- the toast shown on a state change ----------
    TOAST_MAX_LINES = 3           # at most this many wrapped lines (more would fill the window and cover her face)
    TOAST_FONT_MIN = 9.5          # font size floor: a fixed 11 would fill the whole strip at small sizes

    # ---------- the "refill countdown" badge above her head (Go mode, out of quota only) ----------
    BADGE_LABEL = 'Refill in'     # what she says: when the quota comes back
    BADGE_CORE = '#ff87c3'        # the glyph core: candy pink (cute, and not as scary as alarm red)
    BADGE_GLOW = '#ff5fa8'        # the glow: a deeper pink
    BADGE_EDGE = (40, 16, 28, 205)    # dark outline: stays readable on bright wallpapers / white desktops

    @staticmethod
    def _wrap(fm, text, maxw):
        """Wrap to maxw: break at spaces when there are any, otherwise character by character.

        English wraps on word boundaries (breaking mid-word reads badly); text without spaces (CJK)
        falls back to measuring glyph by glyph, since there is nothing else to break on.
        """
        if ' ' in text:
            lines, cur = [], ''
            for word in text.split(' '):
                cand = word if not cur else cur + ' ' + word
                if cur and text_w(fm, cand) > maxw:
                    lines.append(cur)
                    cur = word
                else:
                    cur = cand
            lines.append(cur)
            return lines
        lines, cur = [], ''
        for ch in text:
            if cur and text_w(fm, cur + ch) > maxw:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
        return lines

    def toast_layout(self, text, W):
        """How the toast is laid out: font size, the text of each line, the box size -- all of it
        inside the window.

        The box used to be "as wide as the text", but the window only has the portrait plus
        0.26 x size of slack: a long line (a demo mode label, say) sticks right out of the window
        at **small sizes**, and `drawText(... AlignCenter)` then clips it from both sides -- that is
        the "text does not fit" bug. Now the box width follows the window, whatever does not fit
        wraps, then the font shrinks, at most TOAST_MAX_LINES lines, and the rest gets an ellipsis
        (wrapping beats clipping).
        """
        edge = max(5.0, self.size * 0.03)          # the outline is centered, so leave ~1.5px of room
        w_max = max(self.size * 0.8, W - edge * 2)
        inner = max(w_max - self.size * 0.10, self.size * 0.5)      # usable width inside the box
        px = max(self.TOAST_FONT_MIN, self.size * 0.045)
        while True:
            fnt = pick_font(px, bold=True)
            fm = QFontMetrics(fnt)
            lines = self._wrap(fm, text, inner)
            if len(lines) <= self.TOAST_MAX_LINES or px <= self.TOAST_FONT_MIN:
                break
            px = max(self.TOAST_FONT_MIN, px * 0.9)
        if len(lines) > self.TOAST_MAX_LINES:                       # still too many after shrinking
            head = lines[:self.TOAST_MAX_LINES - 1]
            head.append(fm.elidedText(''.join(lines[self.TOAST_MAX_LINES - 1:]),
                                      Qt.ElideRight, int(inner)))
            lines = head
        w = min(w_max, max(text_w(fm, ln) for ln in lines) + self.size * 0.10)
        h = fm.height() * len(lines) + self.size * 0.045
        return fnt, lines, w, h

    def badge_text(self, core):
        """The whole countdown line above her head: `Refill in HH:MM:SS` (only counts as "out of
        quota"; otherwise None)."""
        clock = core.go_clock()
        return (self.BADGE_LABEL + ' ' + clock) if clock else None

    def _quota_badge(self, p, t, core, W, rect):
        """Go mode out of quota: one line **`Refill in HH:MM:SS`** floats above her head (candy
        pink, with a glow).

        Just the one line -- **no fill, no capsule, no halo** (all three blur and are hard to read).
        Readability comes from two things: a dark outline underneath the glyphs (the same trick as
        the balance on the bowl -- readable on any wallpaper), then a pink glow drawn outside from
        faint to strong (Qt has no text blur, so it is simply drawn a few extra times).
        The countdown is recomputed every frame, so the seconds really tick; it lives in the strip
        above her head (a toast covers it for a moment while it is up).
        """
        text = self.badge_text(core)
        if not text:
            return
        edge = max(5.0, self.size * 0.03)
        px = max(12.0, self.size * 0.070)                  # bigger than the toast: it lives there, needs one look
        p.save()
        while True:                                        # measure -> too wide -> shrink a step -> measure again
            p.setFont(pick_font(px, bold=True))
            fm = p.fontMetrics()                           # the **painter's** metrics: device DPI and font fallback
            tw = text_w(fm, text)                          # are in there, so the measured width equals the drawn one
            if tw <= W - edge * 2 or px <= 7.0:             # never stick out of the window (down to 7px it must fit)
                break
            px *= 0.9
        cx = W / 2.0
        cy = max(fm.height() / 2.0 + 3.0, rect.top() - fm.height() / 2.0 - self.size * 0.02)
        box = QRectF(cx - tw / 2.0, cy - fm.height() / 2.0, tw, fm.height())
        pulse = 0.5 + 0.5 * abs(math.sin(t * 2.2))         # breathe, so the glow is not dead-stiff
        glow = QColor(self.BADGE_GLOW)
        p.setPen(QColor(*self.BADGE_EDGE))                 # dark outline: stays put over a busy desktop
        for dx, dy in ((-1.3, 0.0), (1.3, 0.0), (0.0, -1.3), (0.0, 1.3),
                       (-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0), (1.0, 1.0)):
            p.drawText(box.translated(dx, dy), Qt.AlignCenter, text)
        for radius, alpha in ((1.8, 78), (0.9, 128)):      # the glow: two rings of pink, faint then strong
            c = QColor(glow)
            c.setAlpha(int(alpha * (0.5 + 0.5 * pulse)))
            p.setPen(c)
            for dx, dy in ((-radius, 0.0), (radius, 0.0), (0.0, -radius), (0.0, radius)):
                p.drawText(box.translated(dx, dy), Qt.AlignCenter, text)
        p.setPen(QColor(self.BADGE_CORE))                  # the glyph core: candy pink
        p.drawText(box, Qt.AlignCenter, text)
        p.restore()

    def _toast(self, p, t, W, rect, st):
        if not self.toast:
            return
        dur = self.toast_ms / 1000.0
        age = t - self.toast_t0
        if age < 0 or age > dur:
            return
        a = clamp(age / 0.16, 0.0, 1.0) * clamp((dur - age) / 0.35, 0.0, 1.0)
        fnt, lines, w, h = self.toast_layout(self.toast, W)
        edge = max(5.0, self.size * 0.03)          # the outline is centered, so account for those 1.5px
        x = clamp((W - w) / 2.0, edge, max(edge, W - w - edge))
        y = max(max(3.0, self.size * 0.02), rect.top() - h - self.size * 0.02)
        r = QRectF(x, y, w, h)
        p.save()
        p.setOpacity(a)
        p.setBrush(QColor(22, 26, 34, 228))
        p.setPen(QPen(QColor(STATES[st]['accent']), 1.5))
        rad = min(h / 2.0, self.size * 0.05)      # one line gives a pill; wrapped lines stop short of round
        p.drawRoundedRect(r, rad, rad)
        p.setPen(QColor(236, 241, 248))
        p.setFont(fnt)
        p.drawText(r, Qt.AlignCenter, '\n'.join(lines))
        p.restore()


# =====================================================================
# 4) Windows: the pet itself + the info bubble + the Go usage dialog
# =====================================================================
class PetWindow(QWidget):
    """A frameless, transparent, always-on-top little window.

    Drag with the left button / click and she bounces / double-click to show or hide the bubble /
    wheel to resize / right-click for the menu. It owns no timing logic: the timer does exactly two
    things -- step the app, then repaint.
    """

    def __init__(self, app):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.app = app
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)      # never steal focus
        self.setWindowTitle('Big Fat Fish Eats Rice')
        self._drag = None
        self._moved = False
        self._lens_a = 0.0                                        # click-through lens: current brightness
        self._lens_pos = QPointF(0.0, 0.0)                        #   position, follows the cursor
        self.resize(320, 340)
        self.last = time.monotonic()
        self.t = 0.0
        self.tick = QTimer(self)
        self.tick.setInterval(33)                                 # ~30fps
        self.tick.timeout.connect(self._on_tick)
        self.tick.start()

    def _on_tick(self):
        now = time.monotonic()
        dt = clamp(now - self.last, 0.001, 0.05)                  # a hickup must not skip frames
        self.last = now
        self.t += dt
        self.app.step(dt)
        self._step_lens(dt)
        self.update()

    # ---------- the click-through "see-through": she fades out under the cursor ----------
    LensFade = 0.16                        # how long the fade in / fade out takes (seconds)
    LensClear = 0.86                       # how far it fades at most: 0.86 -> only 14% left
    LensCore = 0.55                        # from the center out to here stays at the lightest value

    def lens_target(self, gpos=None):
        """Where to fade her this frame, and by how much: returns (center inside the window, target
        brightness).

        Only active with "click-through on + this switch on": in that state she is an **invisible
        block** that happily eats mouse input with nothing to show for it (one reason click-through
        feels broken). Fading the patch under the cursor reveals the layer underneath, so it is
        obvious that the cursor is over her and a click will pass straight through.
        `gpos` takes screen coordinates to keep this testable (defaults to the real cursor).
        """
        cfg = self.app.cfg
        if not (bool(cfg.get('thruLens', True)) and bool(cfg.get('clickThrough'))):
            return None, 0.0
        local = self.mapFromGlobal(QCursor.pos() if gpos is None else gpos)
        r = self.rect()
        m = int(round(min(r.width(), r.height()) * 0.08))         # widen a bit, so grazing the edge still fades
        if not r.adjusted(-m, -m, m, m).contains(local):
            return None, 0.0
        return QPointF(clamp(float(local.x()), 6.0, r.width() - 6.0),
                       clamp(float(local.y()), 6.0, r.height() - 6.0)), 1.0

    def lens_radius(self):
        """Radius of the transparent patch: follows the window size (never blur the whole face at
        small sizes)."""
        return clamp(min(self.width(), self.height()) * 0.30, 40.0, 260.0)

    def _step_lens(self, dt, gpos=None):
        """Fade the brightness between 0 and 1 (no hard cuts either way) and follow the mouse."""
        pos, want = self.lens_target(gpos)
        step = dt / self.LensFade
        self._lens_a = clamp(self._lens_a + (step if want else -step), 0.0, 1.0)
        if pos is not None:
            self._lens_pos = pos

    def lens_mask(self, a=None):
        """The mask that fades her out under the cursor: a radial gradient whose value is
        **multiplied into her alpha**.

        The lightest point sits at LensClear (0.86 -> only 14% left, so whatever is underneath
        shows through: the desktop, other windows), then it goes linearly back to 1.0 by the radius
        (her edges are not touched at all) and nothing beyond that.
        Fading out is the only thing this does: no fill, no outline -- nothing is ever "painted on"
        here.
        `a` defaults to the current brightness: with a=0 every factor is 1.0, i.e. nothing is drawn.
        Returns (center, radius, gradient).
        """
        a = self._lens_a if a is None else float(a)
        keep = int(round(255 * clamp(1.0 - self.LensClear * a, 0.0, 1.0)))
        g = QRadialGradient(QPointF(self._lens_pos), self.lens_radius())
        g.setColorAt(0.0, QColor(0, 0, 0, keep))
        g.setColorAt(self.LensCore, QColor(0, 0, 0, keep))
        g.setColorAt(1.0, QColor(0, 0, 0, 255))
        return self._lens_pos, self.lens_radius(), g

    def _paint_lens(self, p):
        """Make the area under the cursor **transparent**: one stroke -- fill a circle with the
        gradient from `lens_mask()` using `DestinationIn`.

        DestinationIn **multiplies** the destination pixel by the source alpha (instead of covering
        it): where the source alpha is 255 the pixel is left alone, and the smaller it gets the more
        transparent she becomes -- so that patch "leaks" whatever is underneath her (the desktop,
        other windows, icons). Simpler than rendering her offscreen and cutting it with a mask, and
        there is nothing to worry about on high DPI.

        **Only subtracts**: outside that patch not a single pixel is touched (the mask is only
        filled up to the radius), and inside it only her alpha moves -- no glow, no outline, no
        ring. The point is "she became transparent", not "something was put on top of her"
        (something painted on would hide the very layer you wanted to see).
        """
        a = self._lens_a
        if a <= 0.01:
            return
        pos, r, mask = self.lens_mask(a)
        p.save()
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(mask))
        p.drawEllipse(pos, r, r)
        p.restore()                                               # the composition mode is restored with it

    def showEvent(self, ev):
        """Re-apply the OS level click-through every time the window shows up.

        Any change of window flags (toggling always-on-top, applying settings) makes Qt recreate the
        native window, and the ex-style goes back to its default, which drops click-through -- this
        is the safety net: whenever it is shown again, apply it once more (so nobody has to remember
        that when touching window flags later).
        """
        super().showEvent(ev)
        self.app._click_through()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)                   # clear first, or ghost trails remain
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        self.app.render(p, self.t)
        self._paint_lens(p)                                       # click-through: she goes see-through under the cursor
        p.end()

    # ---------- interaction ----------
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = ev.globalPos() - self.frameGeometry().topLeft()
            self._moved = False
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.move(ev.globalPos() - self._drag)
            self._moved = True
            self.app.on_pet_moved()

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            if self._moved:
                self.app.on_pet_moved(done=True)
            else:
                self.app.poke()                                   # a poke: she bounces
        self._drag = None

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.app.toggle_bubble()

    def wheelEvent(self, ev):
        step = 24 if ev.angleDelta().y() > 0 else -24
        self.app.resize_pet(int(self.app.cfg['size']) + step)

    def contextMenuEvent(self, ev):
        self.app.popup_menu(ev.globalPos())


class BubbleWindow(QWidget):
    """The little card that follows the pet: state / balance / rate / curve / time left.

    Deliberately mouse-transparent (the Qt attribute plus the OS bit, see set_click_through): it is
    display only, and must not get in the way of clicking anything else.
    """

    W, H = 306, 168              # H is the "balance block" height; the Go block is extra, see want_h
    GO_H = 64

    def __init__(self, app):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.app = app
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.resize(self.W, self.want_h())
        self.roll = None                                          # the rolling balance display

    def go_on(self):
        """Should the bubble carry the Go usage block: **goKey is filled** and the switch is on.

        No key, no block -- nobody should see an ad asking them to fill a key.
        """
        cfg = self.app.cfg
        return bool((cfg.get('goKey') or '').strip()) and bool(cfg.get('goBubble', True))

    def want_h(self):
        """How tall the bubble should be (whether the Go block counts in). PetApp._layout calls
        this after a settings change."""
        return self.H + (self.GO_H if self.go_on() else 0)

    def showEvent(self, ev):
        """Same as above: re-apply the OS click-through when shown (she must never block the mouse)."""
        super().showEvent(ev)
        self.app._click_through()

    def step_roll(self):
        """The balance number rolls over slowly (0.14 step, snapping when the gap is tiny)."""
        target = self.app.core.curBal
        if target is None:
            self.roll = None
            return
        if self.roll is None:
            self.roll = target
            return
        d = target - self.roll
        if abs(d) > 0.0004:
            self.roll += d * 0.14
            if abs(target - self.roll) < 0.0015:
                self.roll = target
        else:
            self.roll = target

    def _conn_text(self):
        core = self.app.core
        if core.demo_on():
            return 'demo mode (fake local data)'
        if core.go_mode():
            # Go mode: this line is about the main feed (the usage endpoint)
            if core.goErr:
                return 'connection failed'
            if core.goAt:
                return 'connected · updated ' + ago_text(core.goAt)
            return 'waiting for an OpenCode Go key' \
                if not (self.app.cfg.get('goKey') or '').strip() else 'starting up…'
        if core.errMsg:
            return 'connection failed'
        if core.lastOkAt:
            return 'connected · updated ' + ago_text(core.lastOkAt)
        return 'waiting for an API key' if not self.app.cfg['key'] else 'starting up…'

    def paintEvent(self, ev):
        core = self.app.core
        st = core.state
        acc = QColor(STATES[st]['accent'])
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        pad = 12.0
        W = float(self.width())
        H = float(self.height())
        p.setBrush(QColor(20, 24, 32, 228))
        p.setPen(QPen(acc, 1.4))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 12, 12)

        fA = pick_font(13, True)
        fDim = pick_font(10.5)
        fBig = pick_font(26, True)

        # ---- line 1: state + connection ----
        p.setPen(Qt.NoPen)
        p.setBrush(acc)
        p.drawEllipse(QPointF(pad + 4, 22), 4, 4)
        p.setPen(QColor(238, 243, 250))
        p.setFont(fA)
        p.drawText(QRectF(pad + 14, 13, 160, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   STATES[st]['em'] + ' ' + STATES[st]['label'])
        p.setPen(QColor(150, 162, 178))
        p.setFont(fDim)
        p.drawText(QRectF(pad, 13, W - pad * 2, 18), Qt.AlignRight | Qt.AlignVCenter,
                   self._conn_text())

        # ---- line 2: balance + rate ----
        p.setPen(QColor(255, 255, 255) if self.roll is not None else QColor(138, 148, 164))
        p.setFont(fBig)
        p.drawText(QRectF(pad, 34, W * 0.56, 30), Qt.AlignLeft | Qt.AlignVCenter,
                   core.lab(self.roll))            # CNY 88.50 / Left 65%
        r = core.rate
        p.setFont(pick_font(13, True))
        p.setPen(acc)
        p.drawText(QRectF(W * 0.52, 35, W * 0.48 - pad, 16), Qt.AlignRight | Qt.AlignVCenter,
                   fmt(r, 2 if r < 10 else 1) + ' ' + core.rate_unit())
        p.setFont(fDim)
        p.setPen(QColor(150, 162, 178))
        p.drawText(QRectF(W * 0.52, 51, W * 0.48 - pad, 14), Qt.AlignRight | Qt.AlignVCenter,
                   self._sub_line(core, r))

        # ---- last 15 minutes: the curve + the gauge ----
        self._spark(p, core, acc, QRectF(pad, 68, W - pad * 2, 34))
        self._gauge(p, core, acc, QRectF(pad, 111, W - pad * 2 - 52, 9))
        p.setFont(fDim)
        p.setPen(QColor(150, 162, 178))
        p.drawText(QRectF(pad, 110, W - pad * 2, 12), Qt.AlignRight | Qt.AlignVCenter,
                   'threshold ' + fmt(core.thresh(), 1 if not core.go_mode() else 0)
                   + ' ' + core.rate_unit())
        p.drawText(QRectF(pad, 124, W - pad * 2, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   'fitted ' + fmt(core.slope, 2) + ' / instant ' + fmt(core.burst, 2)
                   + ' · ' + self._basis(core))

        # ---- details: token estimate / the three usage windows / time left / session mileage ----
        cfg = self.app.cfg
        per_min = (r / cfg['price'] * 1e6 / 60.0) if cfg['price'] > 0 else 0.0
        tok = ('%.1fk' % (per_min / 1000)) if per_min >= 1000 else ('%.0f' % per_min)
        p.drawText(QRectF(pad, 138, W - pad * 2, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   self._go_summary(core) if core.go_mode()
                   else tok + ' tokens/min · ' + fmt(cfg['price'], 1) + ' CNY/million')
        left = core.survive_ms()
        tail = '—' if left is None else ('∞' if left == float('inf') else hhmm(left))
        p.drawText(QRectF(pad, 138, W - pad * 2, 14), Qt.AlignRight | Qt.AlignVCenter,
                   'lasts ' + tail)
        p.setPen(QColor(120, 132, 148))
        cost = ('%.1f%%' % core.session_cost()) if core.go_mode() else fmt(core.session_cost(), 4)
        p.drawText(QRectF(pad, 151, W - pad * 2, 13), Qt.AlignLeft | Qt.AlignVCenter,
                   'watched ' + hhmm(now_ms() - core.t0) + ' · spent ' + cost)
        err = core.err_text()
        if err:
            p.setPen(QColor(255, 150, 120))
            p.drawText(QRectF(pad, 151, W - pad * 2, 13), Qt.AlignRight | Qt.AlignVCenter,
                       '⚠ ' + err[:24])

        # ---- the OpenCode Go block (only when a key is filled; the height comes from want_h) ----
        if self.go_on():
            self._go_block(p, core, fDim, pad, float(self.H))
        p.end()

    def _go_block(self, p, core, fDim, pad, top):
        """The OpenCode Go usage: a title line + one line per window (label / bar / used % / time
        left).

        Even before any data arrives it says something -- "no key filled" and "couldn't fetch" are
        different things and get different lines.
        """
        W = float(self.width())
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawLine(QPointF(pad, top + 3), QPointF(W - pad, top + 3))      # separator from the block above
        y = top + 6
        p.setFont(fDim)
        p.setPen(QColor(160, 172, 188))
        p.drawText(QRectF(pad, y, W * 0.6, 13), Qt.AlignLeft | Qt.AlignVCenter,
                   '🐟 OpenCode Go usage')
        right = ''
        if core.goErr:
            right, col = '⚠ ' + core.goErr, QColor(255, 150, 120)
        elif core.goAt:
            right, col = ago_text(core.goAt), QColor(120, 132, 148)
        else:
            right, col = 'fetching…', QColor(120, 132, 148)
        p.setPen(col)
        p.drawText(QRectF(W * 0.45, y, W * 0.55 - pad, 13), Qt.AlignRight | Qt.AlignVCenter,
                   right[:28])
        y += 15
        if not core.go:
            p.setPen(QColor(120, 132, 148))
            p.drawText(QRectF(pad, y, W - pad * 2, 13), Qt.AlignLeft | Qt.AlignVCenter,
                       'nothing yet -- fill in an OpenCode Go API key in the settings' if not core.goErr
                       else 'keeping the previous data, waiting to retry…')
            return
        fLab = pick_font(9.5)
        for k in GO_WINS:
            info = core.go.get(k)
            if info is None:
                continue
            self._go_row(p, fLab, pad, y, k, info)
            y += 13

    def _go_row(self, p, fLab, pad, y, k, info):
        """One window per line: `Rolling [■■■□□□] 35%` + a right-aligned "12d 4h left"."""
        W = float(self.width())
        p.setFont(fLab)
        p.setPen(QColor(150, 162, 178))
        p.drawText(QRectF(pad, y, 26, 13), Qt.AlignLeft | Qt.AlignVCenter, GO_LABEL.get(k, k))
        bar = QRectF(pad + 28, y + 3.5, 84, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(bar, 3, 3)
        col = self._go_color(info)
        frac = clamp(info['percent'] / 100.0, 0.0, 1.0)
        if frac > 0.0:
            p.setBrush(col)
            p.drawRoundedRect(QRectF(bar.left(), bar.top(),
                                     max(bar.height(), bar.width() * frac), bar.height()), 3, 3)
        p.setFont(fLab)
        p.setPen(col)
        p.drawText(QRectF(bar.right() + 6, y, 42, 13), Qt.AlignLeft | Qt.AlignVCenter,
                   go_pct_str(info['percent']) + '%')
        tail = go_reset_text(info)
        if not go_ok(info):                       # an unhealthy status: carry the API's own wording
            tail = '%s%s' % (info['status'], (' · ' + tail) if tail else '')
        if tail:
            p.setPen(QColor(120, 132, 148))
            p.drawText(QRectF(0, y, W - pad, 13), Qt.AlignRight | Qt.AlignVCenter, tail)

    @staticmethod
    def _go_color(info):
        """The usage bar colour: green -> yellow -> red (same family as the gauge); an unhealthy
        status is always red."""
        if not go_ok(info):
            return QColor('#ff6b3d')
        f = clamp(info['percent'] / 100.0, 0.0, 1.0)
        lo, hi = GO_WIN_COLOR
        if f <= lo:
            return QColor('#5ee08a')
        return QColor('#ffd479') if f <= hi else QColor('#ff6b3d')

    @staticmethod
    def _sub_line(core, r):
        """The small line under the rate: DeepSeek is "≈ x CNY/day", Go is the rolling window's
        reset countdown."""
        if core.go_mode():
            info = core.go.get('rolling')
            left = go_reset_text(info) if info else ''
            if not left:
                return 'checks every %d s' % int(core.cfg['pollSec'])
            return left if left == 'waiting for the reset' else ('reset: ' + left)
        return '≈ ' + fmt(r * 24, 3 if r * 24 < 10 else 1) + ' CNY/day'

    @staticmethod
    def _go_summary(core):
        """Go mode's detail line: each of the three windows' used percent (all in one look)."""
        bits = ['%s %s%%' % (GO_LABEL[k], go_pct_str(core.go[k]['percent']))
                for k in GO_WINS if k in core.go]
        return ' · '.join(bits) if bits else 'usage not fetched yet'

    @staticmethod
    def _basis(core):
        """The "which rate counts" line in the bubble: the settings decide (cfg['actOn']), it is no
        longer "the larger of the two"."""
        if core.rate <= 0:
            return 'nothing burning yet'
        return 'animation on instant' if core.cfg.get('actOn') == 'burst' else 'animation on fitted'

    def _spark(self, p, core, acc, box):
        """The balance curve for the last N minutes (with a breathing dot at the end)."""
        now = now_ms()
        w = [s for s in core.samples if s[0] >= now - core.win_ms()]
        p.save()
        p.setPen(QPen(QColor(255, 255, 255, 20), 1))
        for i in (1, 2, 3):
            y = box.top() + box.height() * i / 4.0
            p.drawLine(QPointF(box.left(), y), QPointF(box.right(), y))
        if len(w) < 2:
            p.setPen(QColor(120, 132, 148))
            p.setFont(pick_font(10.5))
            p.drawText(box, Qt.AlignCenter,
                       'not enough samples yet (one every %d seconds)' % core.cfg['pollSec'])
            p.restore()
            return
        lo = min(s[1] for s in w)
        hi = max(s[1] for s in w)
        if hi - lo < (0.5 if core.go_mode() else 0.03):   # too flat: spread it (CNY graded at 0.01, % at 1)
            c = (hi + lo) / 2.0
            lo, hi = c - (0.25 if core.go_mode() else 0.015), \
                c + (0.25 if core.go_mode() else 0.015)
        t0 = now - core.win_ms()
        span = max(now - t0, 1)
        pts = []
        for tt, bb in w:
            pts.append(QPointF(box.left() + clamp((tt - t0) / span, 0.0, 1.0) * box.width(),
                               box.bottom() - (bb - lo) / (hi - lo) * box.height()))
        path = QPainterPath(pts[0])
        for q in pts[1:]:
            path.lineTo(q)
        fill = QPainterPath(path)
        fill.lineTo(pts[-1].x(), box.bottom())
        fill.lineTo(pts[0].x(), box.bottom())
        fill.closeSubpath()
        c = QColor(acc)
        c.setAlphaF(0.16)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPath(fill)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(acc, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        dot = QColor(acc)
        dot.setAlphaF(0.5 + 0.5 * abs(math.sin(time.monotonic() * 2.9)))
        p.setPen(Qt.NoPen)
        p.setBrush(dot)
        p.drawEllipse(pts[-1], 3.4, 3.4)
        p.restore()

    def _gauge(self, p, core, acc, box):
        """A log-scale rate bar plus a threshold tick line (the tick follows slowMax)."""
        p.save()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(box, box.height() / 2, box.height() / 2)
        f = core.gauge_frac()
        col = (QColor('#5ee08a') if f <= 0.48
               else (QColor('#ffd479') if f <= 0.78 else QColor('#ff6b3d')))
        p.setBrush(col)
        if f > 0.012:
            p.drawRoundedRect(QRectF(box.left(), box.top(),
                                     max(box.height(), box.width() * f), box.height()),
                              box.height() / 2, box.height() / 2)
        top = max(core.cfg['slowMax'] * 4, 0.5)
        fs = clamp(math.log10(1 + core.cfg['slowMax']) / math.log10(1 + top), 0.0, 1.0)
        tx = box.left() + box.width() * fs
        p.setPen(QPen(QColor(255, 255, 255, 150), 1.6))
        p.drawLine(QPointF(tx, box.top() - 2.5), QPointF(tx, box.bottom() + 2.5))
        p.restore()


# =====================================================================
# 5) The OpenCode Go usage dialog (a read-only peek: how much of the three windows is used and
#    when they reset)
# =====================================================================
class GoUsageDialog(QDialog):
    """OpenCode Go usage.

    One row per window (progress bar + "used x% (y% left) · time until reset") + a note + the raw
    response. **Read-only**: it spends no quota and changes no settings; the data comes from
    ApiWorker, and PetApp calls reload() when it lands, so the dialog keeps updating while open.
    """

    def __init__(self, app):
        super().__init__(None)
        self.app = app
        self.setWindowTitle('OpenCode Go Usage')
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)

        self.rows = {}
        for k in GO_WINS:
            row = QHBoxLayout()
            lab = QLabel(GO_LABEL[k])
            lab.setFixedWidth(34)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedWidth(150)
            txt = QLabel('—')
            txt.setMinimumWidth(200)
            for wdg in (lab, bar, txt):
                row.addWidget(wdg)
            row.addStretch(1)
            lay.addLayout(row)
            self.rows[k] = (bar, txt)

        self.note = QLabel('')
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        lay.addWidget(QLabel('Raw response (if the shape ever changes, look at this):'))
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setFixedHeight(104)
        lay.addWidget(self.raw)

        brow = QHBoxLayout()
        for txt, fn, default in (('Refresh now', self.on_refresh, True),
                                 ('Close', self.accept, False)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            b.setDefault(default)
            brow.addWidget(b)
        brow.addStretch(1)
        lay.addLayout(brow)
        self.reload()

    def on_refresh(self):
        """Nudge the worker (both balance and Go usage refresh together -- one fetching thread)."""
        self.app.refresh_go()
        self.note.setText('request sent…')

    def reload(self):
        """Paint the usage currently sitting in Core onto the window (PetApp calls this when new
        data lands)."""
        core = self.app.core
        cfg = self.app.cfg
        if not (cfg.get('goKey') or '').strip():
            self.note.setText('No OpenCode Go API key yet -- fill one in "Settings…" to get this '
                              'working.')
        elif core.goErr:
            self.note.setText('Fetch failed: ' + core.goErr)
        elif core.goAt:
            self.note.setText('Updated %s ago · checks every %d s · the API only gives percentages,'
                              ' no request counts'
                              % (ago(now_ms() - core.goAt), int(cfg.get('goSec') or 60)))
        else:
            self.note.setText('fetching…')
        for k, (bar, txt) in self.rows.items():
            info = core.go.get(k)
            if info is None:
                bar.setValue(0)
                txt.setText('—')
                continue
            pct = info['percent']
            bar.setValue(int(clamp(round(pct), 0, 100)))      # over 100%: the bar just fills
            bits = ['used %s%% (%s%% left)' % (go_pct_str(pct), go_pct_str(max(0.0, 100.0 - pct)))]
            if not go_ok(info):
                bits.append('status ' + info['status'])
            left = go_reset_text(info)
            if left:
                bits.append(left)
            txt.setText(' · '.join(bits))
        self.raw.setPlainText(json.dumps(core.goRaw, ensure_ascii=False, indent=2)
                              if core.goRaw else '(no data yet)')


# =====================================================================
# 6) The settings window
# =====================================================================
class SettingsDialog(QDialog):
    """Settings. The key only ever goes into the local config file; if the API is blocked, a chunk
    of JSON can be pasted in and parsed instead."""

    def __init__(self, app):
        super().__init__(None)
        self.app = app
        cfg = app.cfg
        self.setWindowTitle('Big Fat Fish Eats Rice · Settings')
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.key = QLineEdit(cfg.get('key') or '')
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText('sk-… (stays in the local config file, never sent anywhere else)')
        eye = QCheckBox('Show')
        eye.toggled.connect(lambda v: self.key.setEchoMode(
            QLineEdit.Normal if v else QLineEdit.Password))
        krow = QHBoxLayout()
        krow.addWidget(self.key, 1)
        krow.addWidget(eye)
        form.addRow('DeepSeek API Key', krow)

        self.goKey = QLineEdit(cfg.get('goKey') or '')
        self.goKey.setEchoMode(QLineEdit.Password)
        self.goKey.setPlaceholderText('oc_sk-… (the OpenCode Go key, a second one, unrelated to the one above)')
        goeye = QCheckBox('Show')
        goeye.toggled.connect(lambda v: self.goKey.setEchoMode(
            QLineEdit.Normal if v else QLineEdit.Password))
        grow = QHBoxLayout()
        grow.addWidget(self.goKey, 1)
        grow.addWidget(goeye)
        form.addRow('OpenCode Go API Key', grow)

        self.mode = QComboBox()
        for k in MODE_KEYS:
            self.mode.addItem(MODES[k]['label'], k)
        self.mode.setCurrentIndex(MODE_KEYS.index(app.core.mode()))
        self.mode.setToolTip('Which data decides "how fast she eats":\n'
                             '· DeepSeek balance: how fast the balance drops (CNY/hour), zero = out of rice;\n'
                             '· OpenCode Go: how fast the rolling window remaining percent drops (%/hour),\n'
                             '  spending faster than the threshold means "Eating Fast", any window at 100% = out')
        form.addRow('Monitor', self.mode)

        self.poll = QSpinBox()
        self.poll.setRange(3, 600)
        self.poll.setSuffix(' s')
        self.poll.setValue(int(cfg['pollSec']))
        self.idle = QSpinBox()
        self.idle.setRange(10, 3600)
        self.idle.setSuffix(' s')
        self.idle.setValue(int(cfg['idleSec']))
        self.slow = QDoubleSpinBox()
        self.slow.setRange(0.05, 1000.0)
        self.slow.setDecimals(2)
        self.slow.setSuffix(' CNY/hour')
        self.slow.setValue(float(cfg['slowMax']))
        self.price = QDoubleSpinBox()
        self.price.setRange(0.0, 1000.0)
        self.price.setDecimals(2)
        self.price.setSuffix(' CNY / million tokens')
        self.price.setValue(float(cfg['price']))
        self.wmins = QSpinBox()
        self.wmins.setRange(1, 120)
        self.wmins.setSuffix(' min')
        self.wmins.setValue(int(cfg['windowMin']))
        form.addRow('Poll interval', self.poll)
        form.addRow('Idle window', self.idle)
        form.addRow('Fast threshold', self.slow)
        self.goslow = QDoubleSpinBox()
        self.goslow.setRange(0.1, 1000.0)
        self.goslow.setDecimals(1)
        self.goslow.setSuffix(' %/hour')
        self.goslow.setValue(float(num(cfg.get('goSlowMax'), CFG_DEFAULT['goSlowMax'])))
        self.goslow.setToolTip("Only used in Go mode: when the rolling window's used percent implies "
                               'this many % per hour, she switches to "Eating Fast" '
                               '(in at 1.15x, out at 0.85x, so it does not flutter)')
        form.addRow('Fast threshold (Go)', self.goslow)
        form.addRow('Unit price (only for the token estimate)', self.price)
        form.addRow('Fitting window', self.wmins)

        self.goSec = QSpinBox()
        self.goSec.setRange(15, 3600)
        self.goSec.setSuffix(' s')
        self.goSec.setValue(int(cfg.get('goSec') or 60))
        self.goSec.setToolTip('How often to fetch the Go usage (the API only gives percentages, so '
                              'checking more often is pointless; the real interval is also bounded '
                              'by the poll interval -- both legs share one thread)')
        form.addRow('Go usage polling', self.goSec)

        self.act = QComboBox()
        self.act.addItem('Instant rate (responsive: reacts the moment eating speeds up)', 'burst')
        self.act.addItem('Fitted rate (steady: waits for enough samples, half a beat late)', 'fit')
        self.act.setCurrentIndex(0 if cfg.get('actOn', 'burst') == 'burst' else 1)
        self.act.setToolTip('Which rate decides the animation.\n'
                            'Instant = "how much dropped in this short span", reacts fast; '
                            'fitted = the slope over the whole window, steady.')
        form.addRow('Animation driven by', self.act)

        self.size = QSlider(Qt.Horizontal)
        self.size.setRange(96, 640)
        self.size.setValue(int(cfg['size']))
        self.sizeLb = QLabel('')
        self.size.valueChanged.connect(lambda v: self.sizeLb.setText('%d px' % v))
        self.sizeLb.setText('%d px' % int(cfg['size']))
        srow = QHBoxLayout()
        srow.addWidget(self.size, 1)
        srow.addWidget(self.sizeLb)
        form.addRow('Pet size', srow)

        self.top = QCheckBox('Always on top')
        self.top.setChecked(bool(cfg['topmost']))
        self.bub = QCheckBox('Show the info bubble (double-clicking the pet toggles it too)')
        self.bub.setChecked(bool(cfg['bubble']))
        self.thru = QCheckBox('Click-through (you cannot click her; undo it from the tray menu)')
        self.thru.setChecked(bool(cfg['clickThrough']))
        self.lens = QCheckBox('See through at the cursor (that patch goes transparent, showing the desktop)')
        self.lens.setChecked(bool(cfg['thruLens']))
        self.gobub = QCheckBox('Show the OpenCode Go usage in the bubble (only works once a key is filled)')
        self.gobub.setChecked(bool(cfg.get('goBubble', True)))
        form.addRow('', self.top)
        form.addRow('', self.bub)
        form.addRow('', self.thru)
        form.addRow('', self.lens)
        form.addRow('', self.gobub)
        lay.addLayout(form)

        arow = QHBoxLayout()
        for txt, fn in (('Open config folder', app.open_config_dir),
                        ('Clear samples', app.clear_samples),
                        ('Start watching again', app.restart_session)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            arow.addWidget(b)
        arow.addStretch(1)
        lay.addLayout(arow)

        lay.addWidget(QLabel('API blocked? Run curl locally once; pasting the whole JSON in works too:'))
        self.json = QPlainTextEdit()
        self.json.setPlaceholderText('curl -s https://api.deepseek.com/user/balance '
                                     '-H "Authorization: Bearer sk-your-key"')
        self.json.setFixedHeight(72)
        lay.addWidget(self.json)
        jrow = QHBoxLayout()
        pb = QPushButton('Parse this JSON')
        pb.clicked.connect(self.on_parse)
        jrow.addWidget(pb)
        jrow.addStretch(1)
        lay.addLayout(jrow)

        self.tip = QLabel('')
        self.tip.setWordWrap(True)
        lay.addWidget(self.tip)

        brow = QHBoxLayout()
        for txt, fn, default in (('Save and start watching', self.on_save, True),
                                 ('Test connection', app.test_connection, False),
                                 ('Test Go endpoint', app.test_go, False),
                                 ('Stop watching', app.stop_poll, False),
                                 ('Close', self.accept, False)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            b.setDefault(default)
            brow.addWidget(b)
        lay.addLayout(brow)

    def collect(self):
        c = dict(self.app.cfg)
        c.update({
            'key': self.key.text().strip(),
            'pollSec': int(self.poll.value()),
            'idleSec': int(self.idle.value()),
            'slowMax': float(self.slow.value()),
            'price': float(self.price.value()),
            'windowMin': int(self.wmins.value()),
            'actOn': self.act.currentData(),
            'size': int(self.size.value()),
            'topmost': bool(self.top.isChecked()),
            'bubble': bool(self.bub.isChecked()),
            'clickThrough': bool(self.thru.isChecked()),
            'thruLens': bool(self.lens.isChecked()),
            'goKey': self.goKey.text().strip(),
            'goSec': int(self.goSec.value()),
            'goBubble': bool(self.gobub.isChecked()),
            'mode': self.mode.currentData(),
            'goSlowMax': float(self.goslow.value()),
        })
        return c

    def on_save(self):
        self.app.apply_settings(self.collect(), restart=True)
        self.set_tip('Saved. The key lives in: %s' % self.app.config_file)

    def on_parse(self):
        try:
            js = parse_balance_text(self.json.toPlainText())
        except Exception as e:
            self.set_tip('Parse failed: %s' % e)
            return
        self.app.on_got(js, 'paste')
        if self.app.core.curBal is None:
            self.set_tip('Parse failed: %s' % (self.app.core.errMsg or 'cannot make sense of that JSON'))
        else:
            self.set_tip('Parsed: balance %s%s (the rate needs a few pastes to work out)'
                         % (self.app.core.sign(), fmt(self.app.core.curBal, 2)))

    def set_tip(self, text):
        self.tip.setText(text)


# =====================================================================
# 6) Wiring it all up
# =====================================================================
def screen_at(pt=None):
    """The screen the point is on (the primary screen if that fails)."""
    scr = None
    try:
        if pt is not None:
            scr = QApplication.screenAt(pt)
    except Exception:
        scr = None
    return scr or QApplication.primaryScreen()


# ---------- click-through: the Qt attribute only covers Qt, the OS layer needs its own ----------
WS_EX_TRANSPARENT = 0x00000020
GWL_EXSTYLE = -20


def ex_style_with(ex, on):
    """Set or clear the click-through bit in a window's ex-style, leaving every other bit alone
    (a pure function, so it is easy to test)."""
    return (int(ex) | WS_EX_TRANSPARENT) if on else (int(ex) & ~WS_EX_TRANSPARENT)


def set_click_through(win, on):
    """Make a top level window ignore the mouse at the **OS level**: the cursor moves onto whatever
    is underneath it.

    Setting Qt.WA_TransparentForMouseEvents alone is not enough: that attribute only covers Qt's own
    event dispatch (the pickMouseReceiver machinery), while Windows hit testing still asks her first
    -- which looks like "click-through is ticked, yet the cursor is still blocked by her box and I
    cannot click what is underneath". So the native window's WS_EX_TRANSPARENT is set directly (the
    OS skips windows carrying that bit when hit testing; she is LAYERED already, so adding it does
    not affect how she is drawn).

    Note: any change of window flags (toggling always-on-top, applying settings) makes Qt recreate
    the native window and the ex-style goes back to its default, so this has to be called again after
    every show -- that is what PetApp._click_through() is for.
    Returns whether the bit ended up right; no window / not writable gives False (that only costs
    click-through, and the app keeps running).
    """
    try:
        import ctypes
        if win.windowHandle() is None:                        # no native window yet: do not build one for this
            return False
        hwnd = int(win.winId())
        if not hwnd:
            return False
        u32 = ctypes.WinDLL('user32', use_last_error=True)
        get, put = u32.GetWindowLongPtrW, u32.SetWindowLongPtrW
        get.argtypes, get.restype = (ctypes.c_void_p, ctypes.c_int), ctypes.c_ssize_t
        put.argtypes, put.restype = ((ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t),
                                     ctypes.c_ssize_t)
        h = ctypes.c_void_p(hwnd)
        ex = get(h, GWL_EXSTYLE)
        want = ex_style_with(ex, on)
        if want != ex:
            ctypes.set_last_error(0)
            if not put(h, GWL_EXSTYLE, want) and ctypes.get_last_error():
                return False                                  # wrong handle (offscreen, say)
            u32.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_uint)
            u32.SetWindowPos(h, None, 0, 0, 0, 0,
                             0x0001 | 0x0002 | 0x0004 | 0x0020)   # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
        return bool(get(h, GWL_EXSTYLE) & WS_EX_TRANSPARENT) == bool(on)
    except Exception:
        return False


class PetApp:
    """Core (logic) + PetRenderer (the pixels) + two windows + the fetching thread + the tray icon."""

    def __init__(self, qapp, cfg, config_file=None, no_tray=False):
        self.qapp = qapp
        self.cfg = cfg
        self.config_file = config_file or config_path()
        self.core = Core(cfg)
        self.art = PetArt()
        self.rend = PetRenderer(self.art, cfg)
        self.win = PetWindow(self)
        self.bubble = BubbleWindow(self)
        self.demo_acc = 0.0
        self.pending_bal = None
        self.last_dt = 0.016
        self.worker = ApiWorker(cfg)
        self.worker.got.connect(self.on_got)
        self.worker.bad.connect(self.on_bad)
        self.worker.gotGo.connect(self.on_got_go)
        self.worker.badGo.connect(self.on_bad_go)
        self.tray = None
        self.no_tray = no_tray
        self._dlg = None
        self._go_dlg = None                       # the "OpenCode Go usage…" window (refreshed in place on new data)
        self._go_menu_txt = ''                    # the "Go usage" line in the menu (only rebuilt on change)
        self._tray_menu = None
        self._save_timer = QTimer(self.win)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1200)
        self._save_timer.timeout.connect(self.save_cfg)

    # ---------- start / stop ----------
    def start(self):
        self._auto_mode()                         # only one key filled? switch to that feed
        self._layout()
        self._restore_pos()
        self.win.show()
        self.win.raise_()
        if self.cfg['bubble']:
            self.bubble.show()
        self._click_through()                     # click-through has to reach the OS layer, see set_click_through
        if not self.no_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._make_tray()
        if self.primary_key() and not self.core.demo_on():
            self.worker.resume()

    def primary_key(self):
        """The current mode's key: Go mode needs goKey, DeepSeek mode needs key."""
        return ((self.cfg.get('goKey') or '') if self.core.go_mode()
                else (self.cfg.get('key') or '')).strip()

    def start_poll(self):
        if not self.primary_key():
            return
        if self.core.demo_on():                   # real data wanted: leave the demo first
            self.core.demo_start('off', now_ms())
        self.worker.resume()

    def stop_poll(self):
        self.worker.stop()

    def set_mode(self, name, auto=False):
        """Switch which feed decides "how fast she eats" (from the tray menu or the settings).

        The two sides have different units (CNY / %), so switching clears the samples and starts
        over -- keeping the old ones would make the first beat read as "a huge drop at once".
        """
        name = name if name in MODES else 'deepseek'
        if name == self.core.mode():
            return False
        self.cfg['mode'] = name
        self.stop_poll()                          # different data source: stop the old polling first
        self.core.reset(now_ms(), None)
        self.rend.set_toast(('🔀 Auto-switched to ' if auto else '🔀 Switched to ')
                            + MODES[name]['label'], 2800, self.win.t)
        self.save_cfg()
        self.refresh_tray_menu()
        self.worker.cfg = self.cfg
        self.start_poll()
        return True

    def _auto_mode(self):
        """Only one side has a key filled? Switch to that side automatically.

        Both filled / neither filled: leave it alone -- that case follows the config, it does not
        make choices for the user. (Exactly the moment "I just filled in a Go key and nothing
        happened at startup".)
        """
        has_ds = bool((self.cfg.get('key') or '').strip())
        has_go = bool((self.cfg.get('goKey') or '').strip())
        if has_ds == has_go:
            return None
        want = 'go' if has_go else 'deepseek'
        if want == self.core.mode():
            return None
        self.set_mode(want, auto=True)
        return want

    # ---------- layout ----------
    def _layout(self):
        W, H = self.rend.stage()
        self.win.resize(W, H)
        self.bubble.resize(BubbleWindow.W, self.bubble.want_h())
        self._place()
        self._place_bubble()

    def _place(self):
        g = self.win.frameGeometry()
        av = screen_at(g.center()).availableGeometry()
        x = clamp(g.left(), av.left() - 40, max(av.left() - 40, av.right() - 60))
        y = clamp(g.top(), av.top(), max(av.top(), av.bottom() - 80))
        if (x, y) != (g.left(), g.top()):
            self.win.move(int(x), int(y))

    def _place_bubble(self):
        g = self.win.frameGeometry()
        av = screen_at(g.center()).availableGeometry()
        bw, bh = self.bubble.width(), self.bubble.height()
        bx = g.right() - bw * 0.78
        by = g.top() - bh - 6
        if by < av.top() + 4:                     # no room above? tuck it at her feet
            by = g.bottom() - bh * 0.42
        bx = clamp(bx, av.left() + 4, av.right() - bw - 4)
        by = clamp(by, av.top() + 4, av.bottom() - bh - 4)
        self.bubble.move(int(bx), int(by))

    def _restore_pos(self):
        x, y = self.cfg.get('x'), self.cfg.get('y')
        if x is None or y is None:
            av = screen_at(None).availableGeometry()
            x = av.right() - self.win.width() - 60
            y = av.bottom() - self.win.height() - 90
        self.win.move(int(x), int(y))
        self._place()
        self._place_bubble()

    def _click_through(self):
        """Apply click-through at the OS level (the Qt attribute only covers Qt, see
        set_click_through for why).

        The bubble is a display-only card and is always click-through: it floats next to her, and
        blocking the mouse there would be a nuisance. Any change of window flags (toggling
        always-on-top, applying settings) makes Qt recreate the native window, so every one of those
        places has to call this again.
        """
        set_click_through(self.win, bool(self.cfg['clickThrough']))
        set_click_through(self.bubble, True)

    # ---------- per frame / per tick ----------
    def step(self, dt):
        """One step: demo mode makes data / real mode decides the state / bubble numbers roll and
        repaint / the tray tooltip updates."""
        self.last_dt = dt
        core = self.core
        if core.demo_on():
            self.demo_acc += dt
            while self.demo_acc >= 2.0:           # the demo ticks every 2s (same step as demo_tick)
                self.demo_acc -= 2.0
                core.demo_tick(now_ms(), 2.0)
        else:
            t = now_ms()
            core.speed_now(t)                     # recompute even without a new sample: quiet long enough = ready
            core.apply_state(t)
        self.bubble.step_roll()
        if self.bubble.isVisible():
            # The bubble is its own window and nobody sends it a repaint -- changing the numbers in
            # step_roll() alone would never show up. This pushes one repaint per frame, which is what
            # keeps lines like "connected · updated 3s ago" and "watched 0h 12m" counting up live.
            self.bubble.update()
        if self.tray and int(self.win.t * 2) % 2 == 0:
            self.tray.setToolTip('Big Fat Fish Eats Rice · ' + STATES[core.state]['label']
                                 + core.tip_val() + ('' if core.go_mode() else core.go_tip()))

    def render(self, p, t):
        self.rend.draw(p, t, self.core, self.last_dt)

    # ---------- events ----------
    def on_got(self, js, src):
        t = now_ms()
        if self.core.demo_on():
            # Demo mode: results from the real API are always dropped. One poll may still be in
            # flight the moment the demo starts, and recording it would just be undone by the next
            # demo tick -- which looks like "the money came back by itself".
            return
        try:
            msg = self.core.add_balance(js, t, src)
        except Exception as e:
            self.core.set_error(str(e))
            return
        if msg:                                   # top-up received: spit out coins
            self.coin_burst()
            self.rend.set_toast(msg, 3200, self.win.t)
        self.refresh_tray_icon()

    def on_bad(self, msg):
        if self.core.demo_on():                   # demo mode reports its own connection state
            return
        self.core.set_error(msg)

    # ---------- OpenCode Go usage ----------
    def on_got_go(self, js):
        """One Go usage response arrived.

        * Go mode: it is the **main data** -- handed to Core.add_go_usage (sampling / rate / the
          four states all come out of it);
        * DeepSeek mode: only shown alongside, does not touch the four states (two separate books,
          each keeps its own notes).
        """
        if self.core.demo_on():                   # same rule as the balance: no real data in the demo
            return
        if self.core.go_mode():
            try:
                msg = self.core.add_go_usage(js, now_ms())
            except Exception as e:
                self.core.set_go_error(str(e))
            else:
                if msg:                           # a window loosened / reset: a toast + coins
                    self.coin_burst()
                    self.rend.set_toast(msg, 3000, self.win.t)
            self.refresh_tray_icon()              # the state may have changed, the tray icon follows
        else:
            try:
                self.core.set_go_usage(js, now_ms())
            except Exception as e:
                self.core.set_go_error(str(e))
        self._go_reload()
        self._refresh_go_menu()

    def on_bad_go(self, msg):
        if self.core.demo_on():
            return
        self.core.set_go_error(msg)
        self._go_reload()

    def _refresh_go_menu(self):
        """The "Go usage" line in the menu follows the value; only rebuild the menu when it really
        changed (do not tear it down every 60 seconds)."""
        new = self.core.go_pct_text()
        if new != self._go_menu_txt:
            self._go_menu_txt = new
            self.refresh_tray_menu()

    def _go_reload(self):
        """Refresh the Go usage dialog in place if it is open (no-op otherwise)."""
        if self._go_dlg is None:
            return
        try:
            self._go_dlg.reload()
        except RuntimeError:                      # the window was just destroyed
            self._go_dlg = None

    def coin_burst(self):
        W, H = self.rend.stage()
        bx, by = self.rend.bowl_pt(self.core.state, W, H)
        self.rend.fx.burst('coin', 34, bx, by, self.rend.scale_k())

    def poke(self):
        """Poked: one bounce plus a few particles."""
        self.rend.poke(self.win.t)
        W, H = self.rend.stage()
        bx, by = self.rend.bowl_pt(self.core.state, W, H)
        kind = 'rice'                      # a poke spits rice too; no speed streaks for this
        self.rend.fx.burst(kind, 12, bx, by, self.rend.scale_k())

    def on_pet_moved(self, done=False):
        self._place_bubble()
        if done:
            self.cfg['x'] = self.win.x()
            self.cfg['y'] = self.win.y()
            self._save_timer.start()              # after a drag, wait a moment before writing to disk

    def toggle_bubble(self):
        self.cfg['bubble'] = not self.cfg['bubble']
        if self.cfg['bubble']:
            self.bubble.show()
            self._place_bubble()
        else:
            self.bubble.hide()
        self.save_cfg()

    def resize_pet(self, px):
        if self.rend.set_size(px):
            self.cfg['size'] = self.rend.size
            self._layout()
            self.save_cfg()
            self.refresh_tray_menu()              # the ticks in the "Size" submenu follow along

    def toggle_flag(self, key):
        self.cfg[key] = not self.cfg[key]
        on = bool(self.cfg[key])
        if key == 'bubble':
            if on:
                self.bubble.show()
                self._place_bubble()
            else:
                self.bubble.hide()
        elif key == 'topmost':
            for w in (self.win, self.bubble):
                w.setWindowFlag(Qt.WindowStaysOnTopHint, on)
                w.show()
            if not self.cfg['bubble']:
                self.bubble.hide()
            self._click_through()                 # the native window was recreated, so re-apply it
        elif key == 'clickThrough':
            # once click-through is on she cannot be clicked, only the tray menu can undo it
            self.win.setAttribute(Qt.WA_TransparentForMouseEvents, on)
            self._click_through()
        elif key == 'goBubble':
            self._layout()                        # the bubble's height follows (does the Go block count in)
        self.save_cfg()
        self.refresh_tray_menu()                  # the tray menu's ticks follow along

    def apply_settings(self, cfg, restart=False):
        demo_was = self.core.demo_on()
        self.cfg.clear()
        self.cfg.update(cfg)
        self.rend.cfg = self.cfg
        self.worker.cfg = self.cfg
        self.rend.set_size(int(self.cfg['size']))
        self.win.setWindowFlag(Qt.WindowStaysOnTopHint, bool(self.cfg['topmost']))
        self.win.setAttribute(Qt.WA_TransparentForMouseEvents, bool(self.cfg['clickThrough']))
        self.win.show()                           # window flags changed, so show() again
        if self.cfg['bubble']:
            self.bubble.show()
        else:
            self.bubble.hide()
        self._click_through()                     # that show() recreated the native window, apply it again
        self._layout()
        self.save_cfg()
        self.refresh_tray_menu()
        if restart and not demo_was:
            self.start_poll()

    # ---------- odds and ends ----------
    def set_demo(self, name):
        t = now_ms()
        if name == 'off':
            self.core.demo_start('off', t)
            self.rend.set_toast('Demo mode is off, watching the real balance again', 2200, self.win.t)
            if (self.cfg.get('key') or '').strip():
                self.start_poll()
            self.save_cfg()
            self.refresh_tray_menu()
            return
        self.core.demo_start(name, t, self.pending_bal)
        self.stop_poll()                          # the demo makes its own data: stop polling, or they mix
        self.rend.set_toast('🧪 Demo mode: ' + DEMOS[name]['label'], 2800, self.win.t)
        self.refresh_tray_menu()

    def force_state(self, key):
        self.core.forced = None if key == 'auto' else key
        if self.core.forced:
            self.core.state = self.core.forced
            self.core.stateAt = now_ms()
        self.refresh_tray_menu()

    def clear_samples(self):
        self.core.samples = []
        self.core.lastDropAt = 0

    def restart_session(self):
        self.core.reset(now_ms(), self.core.curBal)

    def save_cfg(self):
        save_config(self.cfg, self.config_file)

    def test_connection(self):
        if not (self.cfg.get('key') or '').strip():
            QMessageBox.information(None, 'Test connection', 'No API key yet.')
            return
        if self.core.demo_on():                   # testing the connection needs real data: leave the demo
            self.set_demo('off')                  #   (which starts polling again on the way out)
            return
        self.worker.resume()                      # start it if it is not running
        self.worker.ask()                         # otherwise just poke it right away

    # ---------- tray / menus ----------
    def tray_icon(self):
        pm = self.art.pix(self.core.state, 64)
        r = pm.devicePixelRatio() or 1.0          # scale by device pixels on high DPI, or the tray icon shrinks
        return QIcon(pm.scaled(max(1, int(round(32 * r))), max(1, int(round(32 * r))),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _make_tray(self):
        self.tray = QSystemTrayIcon(self.tray_icon(), self.win)
        self.tray.setToolTip('Big Fat Fish Eats Rice')
        self.tray.activated.connect(self._tray_click)
        self._tray_menu = self.build_menu()          # keep a reference, do not let it be collected
        self.tray.setContextMenu(self._tray_menu)
        self.tray.show()

    def refresh_tray_menu(self):
        """Swap in a fresh tray menu (the ticks follow the current state).

        A tray menu is "built once, used for life": `QSystemTrayIcon.setContextMenu` only knows that
        one object, and the ticks inside it are fixed at build time -- ticking "click-through" from
        the pet's context menu leaves the tray copy showing the old tick (the pet's menu is rebuilt
        every time, so it is always right). The two drift apart.

        So the whole menu is replaced whenever the state changes; the old one goes through
        deleteLater rather than a plain delete: the signal fires while the menu is still being torn
        down, and destroying it right there tends to step on the copy that is mid-teardown.
        """
        if self.tray is None:
            return
        old = self._tray_menu
        self._tray_menu = self.build_menu()                   # keep a reference, do not let it be collected
        self.tray.setContextMenu(self._tray_menu)
        if old is not None:
            old.deleteLater()

    def refresh_tray_icon(self):
        if self.tray:
            self.tray.setIcon(self.tray_icon())

    def _tray_click(self, why):
        if why in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.win.setVisible(not self.win.isVisible())

    def popup_menu(self, pos):
        menu = self.build_menu(self.win)
        menu.exec_(pos)
        menu.deleteLater()

    def _act(self, menu, text, slot, check=None, group=None, tip=None):
        a = menu.addAction(text)
        a.setCheckable(check is not None)
        if check is not None:
            a.setChecked(bool(check))
        if group is not None:
            group.addAction(a)
        if tip:
            a.setToolTip(tip)
        a.triggered.connect(slot)
        return a

    def build_menu(self, parent=None):
        """The context menu and the tray menu are the same one (rebuilt every time, so the ticks are
        always right)."""
        m = QMenu(parent)
        self._act(m, 'Show / hide', lambda: self.win.setVisible(not self.win.isVisible()))
        m.addSeparator()

        md = m.addMenu('Monitor (mode)')
        g0 = QActionGroup(md)
        for k in MODE_KEYS:
            self._act(md, MODES[k]['label'], lambda _b, kk=k: self.set_mode(kk),
                      check=(self.core.mode() == k), group=g0)

        demo = m.addMenu('Demo mode (no API key)')
        g1 = QActionGroup(demo)
        for key in ('off', 'idle', 'slow', 'fast', 'drain', 'cycle'):
            self._act(demo, DEMOS[key]['label'],
                      lambda _b, k=key: self.set_demo(k),
                      check=(self.core.demo == key), group=g1)

        st = m.addMenu('Force a state (debugging / screenshots)')
        g2 = QActionGroup(st)
        for key in ['auto'] + STATE_KEYS:
            label = 'Auto (real rate)' if key == 'auto' else \
                STATES[key]['em'] + ' ' + STATES[key]['label']
            self._act(st, label, lambda _b, k=key: self.force_state(k),
                      check=((self.core.forced or 'auto') == key), group=g2)

        size = m.addMenu('Size')
        g3 = QActionGroup(size)
        for px in (180, 240, 300, 420, 560):
            self._act(size, '%d px' % px, lambda _b, p=px: self.resize_pet(p),
                      check=(int(self.cfg['size']) == px), group=g3)

        for label, key, tip in (
                ('Show info bubble', 'bubble', 'double-clicking the pet toggles it too'),
                ('Always on top', 'topmost', None),
                ('Click-through', 'clickThrough', 'once on you cannot click her; turn it off from the tray menu'),
                ('See through at the cursor', 'thruLens',
                 'with click-through on, the patch of her under the cursor goes transparent so you '
                 'can see the desktop behind it (otherwise that "invisible block" makes people '
                 'think click-through is broken)'),
                ('Show Go usage in the bubble', 'goBubble',
                 'only visible once an OpenCode Go key is filled: an extra block of rolling / weekly '
                 '/ monthly bars at the bottom of the bubble')):
            self._act(m, label, lambda _b, k=key: self.toggle_flag(k),
                      check=bool(self.cfg[key]), tip=tip)
        m.addSeparator()
        self._act(m, 'OpenCode Go usage…' + (('  (' + self._go_menu_txt + ')')
                                             if self._go_menu_txt else ''),
                  self.open_go, tip='reads ' + GO_API + ' (read-only, spends no quota)')
        self._act(m, 'Refresh usage now', self.refresh_go,
                  tip='nudge one fetch: balance and Go usage refresh together (one thread)')
        self._act(m, 'Settings…', self.open_settings)
        self._act(m, 'Clear samples', self.clear_samples)
        self._act(m, 'Start watching again', self.restart_session)
        self._act(m, 'Open config folder', self.open_config_dir)
        self._act(m, 'About', self.about)
        m.addSeparator()
        self._act(m, 'Quit', self.quit)
        return m

    def open_settings(self):
        dlg = SettingsDialog(self)
        self._dlg = dlg
        dlg.exec_()

    def open_go(self):
        """"OpenCode Go usage…": a look at how much of the three windows is used and when they
        reset."""
        dlg = GoUsageDialog(self)
        self._go_dlg = dlg
        try:
            dlg.exec_()
        finally:
            self._go_dlg = None                     # after it closes, no need to poke it on new data

    def refresh_go(self):
        """Nudge one fetch (the balance goes along -- both legs share one thread, each on its own
        schedule)."""
        self.worker.ask()

    def test_go(self):
        """The "Test Go endpoint" button in the settings: say it plainly without a key, otherwise
        wake the polling."""
        if not (self.cfg.get('goKey') or '').strip():
            QMessageBox.information(None, 'OpenCode Go', 'No OpenCode Go API key yet.')
            return
        if self.core.demo_on():                   # testing a connection needs real data: leave the demo
            self.set_demo('off')                  #   (which starts polling again on the way out)
            return
        self.worker.resume()
        self.worker.ask()

    def about(self):
        QMessageBox.information(None, 'About · Big Fat Fish Eats Rice', (
            'Mode: ' + MODES[self.core.mode()]['label'] + ' (switchable in the tray menu / settings)\n'
            'Balance API: ' + API + '\n'
            'Go usage API: ' + GO_API + '\n'
            'Config file: ' + self.config_file + '\n'
            'Art: frame animations in assets/pet/anim/ (static portraits as the fallback)\n\n'
            '· Keys are only written to the local config file, only used to talk directly to '
            'api.deepseek.com and opencode.ai, and never sent to anyone else.\n'
            '· DeepSeek mode: the faster the balance drops, the faster she eats; balance at zero = '
            'out of rice.\n'
            '· OpenCode Go mode: it watches how fast the **rolling window remaining percent** drops '
            '(= how fast the rolling value rises); spending faster than the threshold (default '
            '20%/h) means "Eating Fast"; any of rolling / weekly / monthly at 100% = out of rice.\n'
            '· The balance API only reports three numbers (total / granted / topped up) and no token '
            'detail, so the token count is estimated from the unit price.\n'
            '· The balance only moves in 0.01 steps, so anything slower than 0.1 CNY/hour is '
            'essentially unmeasurable.\n'
            '· Rate = the larger of the 15 minute least squares slope and the instant drop rate, '
            'with hysteresis around the threshold.'))

    def open_config_dir(self):
        d = os.path.dirname(self.config_file) or '.'
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)                                   # Windows
        except Exception:
            try:
                import subprocess
                subprocess.Popen(['explorer', d])
            except Exception as e:
                QMessageBox.warning(None, 'Cannot open that folder', str(e))

    def quit(self):
        try:
            self.cfg['x'] = self.win.x()
            self.cfg['y'] = self.win.y()
            save_config(self.cfg, self.config_file)
        except Exception:
            pass
        self.worker.stop()
        if self.tray:
            self.tray.hide()
        self.qapp.quit()


# =====================================================================
# 7) Offscreen rendering (acceptance / screenshots) and the command line
# =====================================================================
def render_frames(outdir, size=300, seconds=3.0, fps=30.0, with_toast=True):
    """Render one frame per state offscreen.

    It first plays the animation for `seconds` seconds in 1/30 steps as a warm-up -- the particles
    have to build up, otherwise the shot catches an empty stage; t is a pure parameter, so every
    render is reproducible (except for the random particles). Returns
    [(state, path, width, height, particle count)].
    """
    os.makedirs(outdir, exist_ok=True)
    cfg = dict(CFG_DEFAULT)
    cfg['size'] = int(size)
    art = PetArt()
    made = []
    for st in STATE_KEYS:
        rend = PetRenderer(art, cfg)
        core = Core(cfg)
        core.forced = st
        core.state = st
        core.stateAt = now_ms()
        base = 100.0
        t0 = now_ms() - cfg['windowMin'] * 60000
        for i in range(91):                       # fake 15 minutes of history so the curve has content
            core.push_sample(round(base - i * 0.02, 2), t0 + i * 10000, quiet=True)
        core.bal0 = base
        core.curBal = core.samples[-1][1]
        core.rate = cfg['slowMax'] * (3.5 if st == 'fast' else (0.4 if st == 'eating' else 0.0))
        W, H = rend.stage()
        img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        step = 1.0 / fps
        t = 0.0
        while t < seconds:
            t += step
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(img.rect(), Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            rend.draw(p, t, core, step)
        if with_toast:                            # one more frame, so the toast lands in the image too
            rend.set_toast(core.state_msg(), 2400, t - 0.7)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(img.rect(), Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            rend.draw(p, t + 0.05, core, step)
        p.end()
        path = os.path.join(outdir, '%s.png' % st)
        img.save(path)
        made.append((st, path, W, H, len(rend.fx.parts)))
    return made


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog='dswhale_pet.py',
        description='Big Fat Fish Eats Rice -- desktop pet (four states driven by your DeepSeek '
                    'burn rate; fill in an OpenCode Go key for an extra usage block)',
        epilog='e.g. --demo=cycle cycles all four states / --slow=2 sets the "fast" threshold to 2 '
               'CNY/hour /\n     --go-usage --go-key oc_sk-… one-shot Go usage check, then exit\n')
    ap.add_argument('--key', help='DeepSeek API key (normally: right-click -> Settings)')
    ap.add_argument('--mode', choices=list(MODE_KEYS),
                    help='which feed to watch: deepseek (balance, CNY/h) / go (OpenCode Go usage, %%/h)')
    ap.add_argument('--go-slow', type=float, help='Go-mode "fast eating" threshold, %%/hour (default 20)')
    ap.add_argument('--go-key',
                    help='OpenCode Go API key (a second key, read-only usage; usually in the settings)')
    ap.add_argument('--go-sec', type=float, help='Go usage polling interval, seconds (default 60, min 15)')
    ap.add_argument('--go-usage', action='store_true',
                    help='print the OpenCode Go usage to the terminal and exit (no window, no Qt)')
    ap.add_argument('--go-json', action='store_true', help='with --go-usage: also print the raw JSON')
    ap.add_argument('--demo', choices=list(DEMOS), help='demo mode (no API key needed)')
    ap.add_argument('--bal', type=float, help='demo starting value (DeepSeek in CNY, Go in remaining %%)')
    ap.add_argument('--slow', type=float, help='the "fast" threshold, CNY/hour')
    ap.add_argument('--poll', type=float, help='poll interval, seconds')
    ap.add_argument('--win', type=float, help='fitting window, minutes')
    ap.add_argument('--price', type=float, help='unit price, CNY per million tokens')
    ap.add_argument('--state', choices=STATE_KEYS, help='force one state (debugging / screenshots)')
    ap.add_argument('--act', choices=['burst', 'fit'],
                    help='drive the animation from the instant (burst, default) or fitted (fit) rate')
    ap.add_argument('--size', type=int, help='portrait long edge in px (96~640)')
    ap.add_argument('--pos', help='window position, like 1200,700')
    ap.add_argument('--config', help='config file path (default %%APPDATA%%\\dswhale_pet\\config.json)')
    ap.add_argument('--no-tray', action='store_true', help='no tray icon')
    ap.add_argument('--quit-after', type=float, metavar='SEC',
                    help='quit automatically after this many seconds (self-test / demo)')
    ap.add_argument('--shots', metavar='DIR', help='render one PNG per state offscreen, then quit')
    ap.add_argument('--frames', type=float, default=3.0, help='animation warm-up seconds for --shots')
    return ap.parse_args(argv)


def apply_app_icon(qapp):
    """The brand logo used for the window / taskbar / Alt-Tab (a .ico made from the logo art).
    The tray icon is a different thing: it follows the balance state, see PetApp.tray_icon."""
    if os.path.exists(ICON_PATH):
        qapp.setWindowIcon(QIcon(ICON_PATH))


def cfg_from_args(cfg, args):
    """Hang the command-line arguments over the config (every path except --shots builds its
    config through this first)."""
    if args.key is not None:
        cfg['key'] = args.key.strip()
    if args.mode:
        cfg['mode'] = args.mode
    if args.go_slow is not None:
        cfg['goSlowMax'] = float(args.go_slow)
    if args.go_key is not None:
        cfg['goKey'] = args.go_key.strip()
    if args.go_sec is not None:
        cfg['goSec'] = int(clamp(args.go_sec, 15, 3600))
    if args.slow is not None:
        cfg['slowMax'] = float(args.slow)
    if args.poll is not None:
        cfg['pollSec'] = int(args.poll)
    if args.win is not None:
        cfg['windowMin'] = int(args.win)
    if args.price is not None:
        cfg['price'] = float(args.price)
    if args.act:
        cfg['actOn'] = args.act
    if args.size is not None:
        cfg['size'] = int(clamp(args.size, 96, 640))
    if args.pos:
        try:
            x, y = [int(v) for v in args.pos.replace(' ', '').split(',')]
            cfg['x'], cfg['y'] = x, y
        except Exception:
            print('[warn] --pos must look like x,y')
    return cfg


def print_go_usage(cfg, as_json=False, out=print):
    """--go-usage: fetch the Go usage once and print it, return the exit code
    (0 = ok / 2 = no key / 1 = fetch failed).

    Plain text, no window -- one command that validates the key / endpoint / network without
    touching Qt or waiting for a window to start.
    """
    key = (cfg.get('goKey') or '').strip()
    if not key:
        out('No OpenCode Go API key yet: pass one with --go-key, or fill it in the settings first.')
        return 2
    try:
        js = fetch_go_usage(key)
    except Exception as e:
        out('Fetch failed: %s' % e)
        return 1
    usage, now = parse_go_usage(js), now_ms()
    for k in GO_WINS:
        info = usage.get(k)
        if info is None:
            out('%-6s -- (the API does not report this window)' % GO_LABEL[k])
            continue
        bits = ['used %s%%' % go_pct_str(info['percent']),
                '%s%% left' % go_pct_str(max(0.0, 100.0 - info['percent']))]
        if not go_ok(info):
            bits.append('status ' + info['status'])
        left = go_reset_text(info, now)
        if left:
            bits.append(left)
        out('%-6s %s' % (GO_LABEL[k], ' · '.join(bits)))
    if as_json:
        out(json.dumps(js, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    args = parse_args(argv)
    cfg = cfg_from_args(load_config(args.config), args)   # config first: the --go-usage path never needs Qt

    if args.go_usage:                            # usage only: plain text, no window
        return print_go_usage(cfg, as_json=args.go_json)

    if args.shots:                                # images only, no window
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        quiet_qt()
        qapp = QApplication(sys.argv[:1])         # must stay referenced, or it is collected and there is no app
        ensure_fonts()
        made = render_frames(args.shots, size=args.size or CFG_DEFAULT['size'],
                             seconds=args.frames)
        for st, path, w, h, n in made:
            print('%-6s %4dx%-4d particles %3d  ->  %s' % (st, w, h, n, path))
        qapp.quit()
        return 0

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    quiet_qt()
    qapp = QApplication(sys.argv[:1])
    qapp.setQuitOnLastWindowClosed(False)         # closing the bubble must not quit
    apply_app_icon(qapp)
    ensure_fonts()

    pet = PetApp(qapp, cfg, args.config, args.no_tray)
    pet.pending_bal = args.bal
    if args.state:
        pet.core.forced = args.state
        pet.core.state = args.state
    if args.demo:
        pet.core.demo_start(args.demo, now_ms(), args.bal)
    pet.start()
    if args.quit_after:
        QTimer.singleShot(int(max(1.0, args.quit_after) * 1000), pet.quit)
    return qapp.exec_()


if __name__ == '__main__':
    sys.exit(main())
