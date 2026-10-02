"""
Live (causal) LAD fused-lasso smoothing of a futures-FRA basis.

Two causal estimators, both only use data up to time t:

  A. Rolling re-fit, endpoint only
     Every `every_s` seconds solve the LAD fused lasso on the trailing `window_s`
     seconds and publish ONLY the last fitted value. Exact same objective as the
     batch fit; ~50ms per solve for a 15-minute window.

  B. OnlineLADFilter (O(1)-ish per tick)
     Streaming analogue: hold the level at the running median of the current
     segment, and run two one-sided sign-CUSUMs on the residuals. A new segment is
     opened when one-sided evidence exceeds `h` seconds (h ~ lam), mirroring the
     LAD-TV boundary condition. No solver, suitable for a tick handler.

Both are driven by a 1-second clock with last-value-carried-forward, which is
causal and gives every second weight 1 (a tick's duration is only known when the
next tick arrives, so duration weights are not usable live).

Usage
    python lad_live.py demo_basis.csv [--time-col ... --value-col ... --lam 30 ...]
"""
import argparse
import time
from bisect import insort, bisect_left
from collections import deque

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from lad_fused_lasso_csv import (load_csv, split_sessions, lad_fused_lasso,
                                 polish, prune, fit_series)


# ----------------------------------------------------------------------------
# 1-second clock (causal)
# ----------------------------------------------------------------------------
def to_clock(s, freq="1s", max_stale_s=60, session_gap_s=1800):
    """Last tick at or before each clock time; stale beyond max_stale_s -> NaN.
    Returns a list of per-session Series on a regular grid."""
    out = []
    for sess in split_sessions(s, session_gap_s):
        grid = pd.date_range(sess.index[0].ceil(freq), sess.index[-1].floor(freq), freq=freq)
        c = sess.groupby(sess.index.ceil(freq)).last().reindex(grid)
        c = c.ffill(limit=int(max_stale_s / pd.Timedelta(freq).total_seconds()))
        out.append(c.dropna())
    return out


# ----------------------------------------------------------------------------
# A. Rolling re-fit, publish endpoint
# ----------------------------------------------------------------------------
def rolling_refit(clock, lam, window_s=900, every_s=1, min_jump=0.10, min_dur_s=15):
    """Causal: value at t comes from a fit on (t - window_s, t]."""
    y = clock.values.astype(float)
    n = len(y)
    live = np.full(n, np.nan)
    last = np.nan
    for i in range(n):
        if i % every_s == 0:
            a = max(0, i + 1 - window_s)
            yy, ww = y[a:i + 1], np.ones(i + 1 - a)
            x = lad_fused_lasso(yy, lam, ww)
            x = prune(yy, polish(yy, x, ww), ww, min_jump, min_dur_s)
            last = x[-1]
        live[i] = last
    return pd.Series(live, index=clock.index)


# ----------------------------------------------------------------------------
# B. Streaming filter
# ----------------------------------------------------------------------------
class OnlineLADFilter:
    """Causal piecewise-flat robust level tracker.

    level   = median of the current segment (last `level_window` seconds of it)
    up/down = one-sided CUSUMs on sign(y - level -/+ delta), delta = min_jump/2.
              With no change P(y > level + delta) < 1/2, so the CUSUM drifts to 0
              (no false alarms from noise); after a jump > delta it climbs ~1/s.
    A segment opens when a CUSUM exceeds h; its start is back-dated to where the
    CUSUM last left zero, and the new level is the median since then. The change
    is accepted only if it is >= min_jump.
    """

    def __init__(self, h=30.0, min_jump=0.10, level_window=900):
        self.h, self.delta, self.min_jump = h, min_jump / 2, min_jump
        self.level_window = level_window
        self.reset()

    def reset(self):
        self.level = np.nan
        self.seg = deque()            # values of current segment, arrival order
        self.sorted = []              # same values, sorted (for O(log n) median)
        self.hist = deque()           # recent values for back-dating the new segment
        self.up = self.dn = 0.0
        self.up_len = self.dn_len = 0 # how far back the CUSUM run goes
        self.jumps = []               # (detect_index, backdated_start_index, old, new)
        self.i = -1

    def _push(self, v):
        self.seg.append(v)
        insort(self.sorted, v)
        if len(self.seg) > self.level_window:
            old = self.seg.popleft()
            del self.sorted[bisect_left(self.sorted, old)]
        self.level = self.sorted[len(self.sorted) // 2]

    def _restart(self, values):
        self.seg.clear(); self.sorted.clear()
        for v in values:
            self._push(v)
        self.up = self.dn = 0.0
        self.up_len = self.dn_len = 0

    def update(self, y):
        self.i += 1
        self.hist.append(y)
        if len(self.hist) > self.level_window:
            self.hist.popleft()
        if np.isnan(self.level):
            self._push(y)
            return self.level

        su = 1.0 if y > self.level + self.delta else -1.0
        sd = 1.0 if y < self.level - self.delta else -1.0
        self.up, self.up_len = (self.up + su, self.up_len + 1) if self.up + su > 0 else (0.0, 0)
        self.dn, self.dn_len = (self.dn + sd, self.dn_len + 1) if self.dn + sd > 0 else (0.0, 0)

        for stat, run in ((self.up, self.up_len), (self.dn, self.dn_len)):
            if stat > self.h:
                # level from the most recent ~h seconds of the run: these are
                # (almost) all post-jump, the start of the run is often pre-jump noise
                recent = list(self.hist)[-min(run, int(self.h)):]
                new = float(np.median(recent))
                if abs(new - self.level) >= self.min_jump:
                    self.jumps.append((self.i, self.i - run + 1, self.level, new))
                    self._restart(recent)
                    return self.level
                self.up = self.dn = 0.0           # evidence too small: discard it
                self.up_len = self.dn_len = 0

        self._push(y)
        return self.level


def online_filter(clock, h, min_jump, level_window=900):
    f = OnlineLADFilter(h=h, min_jump=min_jump, level_window=level_window)
    live = np.array([f.update(v) for v in clock.values])
    return pd.Series(live, index=clock.index), f.jumps


# ----------------------------------------------------------------------------
# Evaluation helpers (need a ground-truth column, i.e. demo data)
# ----------------------------------------------------------------------------
def detection_stats(est, truth, min_jump, horizon_s=300, early_s=30):
    """Delay = seconds after a true jump until est is within 25% of the jump of
    the new level. False alarm = est step >= min_jump not in [-early_s, +horizon_s]
    around a true jump."""
    tv = truth.values; ev = est.values; idx = est.index
    tj = np.flatnonzero(np.abs(np.diff(tv)) > 1e-9) + 1
    delays = []
    for k, j in enumerate(tj):
        end = tj[k + 1] if k + 1 < len(tj) else len(tv)
        size = tv[j] - tv[j - 1]
        ok = np.flatnonzero(np.abs(ev[j:end] - tv[j]) < 0.25 * abs(size))
        delays.append((idx[j], size, ok[0] if len(ok) else np.nan))
    ej = np.flatnonzero(np.abs(np.diff(ev)) >= min_jump) + 1
    false = sum(1 for e in ej if not np.any((e - tj >= -early_s) & (e - tj <= horizon_s)))
    return pd.DataFrame(delays, columns=["true_jump", "size", "delay_s"]), false


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--time-col"); ap.add_argument("--value-col")
    ap.add_argument("--lam", type=float, default=30.0)
    ap.add_argument("--min-jump", type=float, default=0.10)
    ap.add_argument("--min-dur", type=float, default=15.0)
    ap.add_argument("--window", type=int, default=900, help="rolling re-fit window, s")
    ap.add_argument("--every", type=int, default=5, help="re-fit every N seconds (1 = every tick)")
    ap.add_argument("--out-prefix", default="live")
    a = ap.parse_args()

    s = load_csv(a.csv, a.time_col, a.value_col)
    clocks = to_clock(s)
    batch, _ = fit_series(s, a.lam, a.min_jump, a.min_dur, 60, 1800, 20000)  # look-ahead!

    raw = pd.read_csv(a.csv)
    truth_ticks = None
    if "true" in raw.columns:
        truth_ticks = (raw.assign(timestamp=pd.to_datetime(raw.timestamp))
                          .drop_duplicates("timestamp", keep="last")
                          .set_index("timestamp")["true"])

    rows, plots = [], []
    for c in clocks:
        t0 = time.time(); A = rolling_refit(c, a.lam, a.window, a.every, a.min_jump, a.min_dur); tA = time.time() - t0
        t0 = time.time(); B, jumpsB = online_filter(c, a.lam, a.min_jump); tB = time.time() - t0
        bt = batch.reindex(c.index, method="ffill")
        plots.append((c, A, B, bt))
        print(f"\nsession {c.index[0]:%Y-%m-%d}: {len(c):,} clock seconds | "
              f"rolling re-fit {tA:.1f}s ({1e3 * tA / (len(c) / a.every):.0f}ms/solve), "
              f"online filter {tB:.2f}s ({1e6 * tB / len(c):.0f}µs/tick)")
        if truth_ticks is not None:
            tr = truth_ticks.reindex(c.index, method="ffill")
            for name, est in (("batch (look-ahead)", bt), ("A rolling re-fit", A), ("B online filter", B)):
                d, fa = detection_stats(est, tr, a.min_jump)
                rows.append({"session": f"{c.index[0]:%m-%d}", "method": name,
                             "MAE bp": np.mean(np.abs(est - tr)),
                             "median delay s": d.delay_s.median(),
                             "max delay s": d.delay_s.max(),
                             "missed": int(d.delay_s.isna().sum()),
                             "false jumps": fa})
            if c is clocks[0]:
                dA, _ = detection_stats(A, tr, a.min_jump); dB, _ = detection_stats(B, tr, a.min_jump)
                print(dA.rename(columns={"delay_s": "delay A"}).assign(**{"delay B": dB.delay_s})
                        .to_string(index=False, float_format="{:.3f}".format))

    if rows:
        print("\n" + pd.DataFrame(rows).to_string(index=False, float_format="{:.4f}".format))

    # ---- chart: session 1 overview + zoom on a jump
    c, A, B, bt = plots[0]
    fig, ax = plt.subplots(2, 1, figsize=(13, 8), gridspec_kw={"height_ratios": [2, 1.3]})
    j = np.flatnonzero(np.abs(np.diff(bt.values)) >= a.min_jump)
    zoom = (j[len(j) // 2] if len(j) else len(c) // 2)
    for axi, sl in ((ax[0], slice(None)), (ax[1], slice(max(0, zoom - 240), zoom + 360))):
        axi.plot(c.index[sl], c.values[sl], ".", ms=1.5, color="0.7", label="1s clock (ffill)")
        axi.step(bt.index[sl], bt.values[sl], where="post", color="k", ls="--", lw=1.2,
                 label="batch fit (uses future data)")
        axi.step(A.index[sl], A.values[sl], where="post", color="tab:blue", lw=1.8,
                 label=f"A: rolling re-fit endpoint ({a.window}s window)")
        axi.step(B.index[sl], B.values[sl], where="post", color="tab:red", lw=1.4,
                 label="B: online sign-CUSUM filter")
        lo, hi = np.nanmin(bt.values[sl]), np.nanmax(bt.values[sl])
        axi.set_ylim(lo - 0.6, hi + 0.6); axi.set_ylabel("basis (bp)")
    ax[0].set_title(f"Causal LAD smoothing, λ = h = {a.lam:g}s")
    ax[1].set_title("zoom around a jump: live estimates lag by ≈ λ seconds")
    ax[0].legend(loc="upper left", fontsize=8)
    plt.tight_layout(); plt.savefig(f"{a.out_prefix}.png", dpi=120)

    pd.concat([pd.DataFrame({"timestamp": c.index, "clock": c.values, "live_refit": A.values,
                             "live_filter": B.values}) for c, A, B, _ in plots]
              ).to_csv(f"{a.out_prefix}_live.csv", index=False)
    print(f"\nwrote {a.out_prefix}.png, {a.out_prefix}_live.csv")


if __name__ == "__main__":
    main()
