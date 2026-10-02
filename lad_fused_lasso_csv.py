"""
LAD fused lasso smoother for a futures-FRA basis series read from a CSV file.

    minimise   sum_t w_t * |y_t - x_t|  +  lam * sum_t |x_{t+1} - x_t|

Expected CSV: one timestamp column + one basis column (bp), e.g.

    timestamp,basis
    2026-10-01 08:00:00,-2.41
    2026-10-01 08:00:01,-2.39
    ...

Usage
    python lad_fused_lasso_csv.py basis.csv
    python lad_fused_lasso_csv.py basis.csv --time-col ts --value-col fut_fra_bp --lam 30 \
        --min-jump 0.10 --min-dur 15 --out-prefix out/basis
    python lad_fused_lasso_csv.py --demo demo_basis.csv     # write a synthetic CSV to try it on

Outputs
    <prefix>_fitted.csv    timestamp, raw, fitted, segment_id
    <prefix>_segments.csv  one row per flat segment (start, end, duration, level, jump)
    <prefix>.png           chart
"""
import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp
from scipy.optimize import linprog
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def load_csv(path, time_col=None, value_col=None, scale=1.0):
    """Read the CSV, return a clean, sorted, de-duplicated Series indexed by time.
    If columns aren't named, the first column is time and the second is the basis."""
    df = pd.read_csv(path)
    time_col = time_col or df.columns[0]
    value_col = value_col or df.columns[1]
    s = pd.Series(pd.to_numeric(df[value_col], errors="coerce").values * scale,
                  index=pd.to_datetime(df[time_col]), name="basis")
    s = s[~s.index.isna()].dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]           # several prints in one stamp -> last
    if s.empty:
        raise ValueError("no valid rows after parsing")
    return s


def _seconds(idx):
    """Seconds since first stamp; independent of the index's datetime unit (ns/us/s)."""
    return np.asarray((idx - idx[0]).total_seconds(), float)


def tick_weights(idx, max_gap_s):
    """Weight each tick by how long it was 'live' (seconds until next tick), capped.
    For a regular 1s grid every weight is 1; for print-on-change data a level that
    sat unchanged for 40s counts 40x a level that lasted 1s."""
    dt = np.diff(_seconds(idx))
    w = np.r_[dt, np.median(dt) if len(dt) else 1.0]
    return np.clip(w, 1e-3, max_gap_s)


def split_sessions(s, gap_s):
    """Split at gaps longer than gap_s (overnight, lunch, feed outage).
    No jump penalty is charged across a session break."""
    gaps = np.flatnonzero(np.diff(_seconds(s.index)) > gap_s) + 1
    return [s.iloc[a:b] for a, b in zip(np.r_[0, gaps], np.r_[gaps, len(s)])]


# ----------------------------------------------------------------------------
# Solver + post-processing
# ----------------------------------------------------------------------------
def lad_fused_lasso(y, lam, w=None):
    """Exact LAD fused lasso as a sparse LP (HiGHS).
    Variables x free; p,q,r,s >= 0 with y - x = p - q and Dx = r - s."""
    y = np.asarray(y, float)
    n = len(y)
    if n == 1:
        return y.copy()
    w = np.ones(n) if w is None else np.asarray(w, float)
    I = sp.identity(n, format="csr")
    Im = sp.identity(n - 1, format="csr")
    D = sp.diags([-np.ones(n - 1), np.ones(n - 1)], [0, 1], shape=(n - 1, n), format="csr")
    Z1, Z2 = sp.csr_matrix((n, n - 1)), sp.csr_matrix((n - 1, n))
    A_eq = sp.vstack([sp.hstack([I, I, -I, Z1, Z1]),
                      sp.hstack([D, Z2, Z2, -Im, Im])], format="csc")
    b_eq = np.r_[y, np.zeros(n - 1)]
    c = np.r_[np.zeros(n), w, w, lam * np.ones(2 * (n - 1))]
    bounds = [(None, None)] * n + [(0, None)] * (2 * n + 2 * (n - 1))
    res = linprog(c, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if res.status != 0:
        raise RuntimeError(res.message)
    return res.x[:n]


def lad_fused_lasso_chunked(y, lam, w, chunk_n=20000, overlap_n=1200):
    """Solve long sessions in overlapping chunks; keep each chunk's interior."""
    n = len(y)
    if n <= chunk_n:
        return lad_fused_lasso(y, lam, w)
    x = np.empty(n)
    step = chunk_n - 2 * overlap_n
    for core_a in range(0, n, step):
        core_b = min(core_a + step, n)
        a, b = max(0, core_a - overlap_n), min(n, core_b + overlap_n)
        xc = lad_fused_lasso(y[a:b], lam, w[a:b])
        x[core_a:core_b] = xc[core_a - a: core_b - a]
    return x


def to_segments(x, tol=1e-9):
    jumps = np.flatnonzero(np.abs(np.diff(x)) > tol) + 1
    starts, ends = np.r_[0, jumps], np.r_[jumps, len(x)]
    return starts, ends


def wmedian(v, w):
    o = np.argsort(v)
    cw = np.cumsum(w[o])
    return v[o][np.searchsorted(cw, 0.5 * cw[-1])]


def polish(y, x, w):
    """Reset each segment level to its weighted median (removes lasso shrinkage)."""
    out = x.copy()
    for a, b in zip(*to_segments(x)):
        out[a:b] = wmedian(y[a:b], w[a:b])
    return out


def prune(y, x, w, min_jump, min_dur_s):
    """Absorb segments shorter than min_dur_s (time-based, via weights) into the
    neighbour closest in level, then merge neighbours closer than min_jump.
    Levels are re-set to weighted medians after each merge."""
    x = x.copy()
    while True:
        starts, ends = to_segments(x)
        k = len(starts)
        if k == 1:
            return x
        lv = x[starts]
        dur = np.array([w[a:b].sum() for a, b in zip(starts, ends)])
        short = np.flatnonzero(dur < min_dur_s)
        if len(short):
            i = short[np.argmin(dur[short])]
            nb = [j for j in (i - 1, i + 1) if 0 <= j < k]
            j = min(nb, key=lambda j: abs(lv[j] - lv[i]))
        else:
            gaps = np.abs(np.diff(lv))
            i = int(np.argmin(gaps))
            if gaps[i] >= min_jump:
                return x
            j = i + 1
        a, b = starts[min(i, j)], ends[max(i, j)]
        x[a:b] = wmedian(y[a:b], w[a:b])


def fit_series(s, lam, min_jump, min_dur_s, max_gap_s, session_gap_s, chunk_n):
    """Fit every session independently, return fitted values aligned to s."""
    fitted = []
    for sess in split_sessions(s, session_gap_s):
        y = sess.values.astype(float)
        w = tick_weights(sess.index, max_gap_s)
        x = lad_fused_lasso_chunked(y, lam, w, chunk_n=chunk_n)
        x = prune(y, polish(y, x, w), w, min_jump, min_dur_s)
        fitted.append(pd.Series(x, index=sess.index))
    return pd.concat(fitted), split_sessions(s, session_gap_s)


def segment_table(s, fit, session_gap_s, max_gap_s):
    rows, seg_id = [], np.empty(len(s), int)
    sid, pos = 0, 0
    for sess in split_sessions(s, session_gap_s):
        n = len(sess)
        x = fit.values[pos:pos + n]
        w = tick_weights(sess.index, max_gap_s)
        for k, (a, b) in enumerate(zip(*to_segments(x))):
            seg_id[pos + a: pos + b] = sid
            rows.append({
                "segment_id": sid,
                "start": sess.index[a],
                "end": sess.index[b - 1],
                "duration_s": round(float(w[a:b].sum()), 1),
                "n_ticks": b - a,
                "level": x[a],
                "jump": np.nan if k == 0 else x[a] - x[a - 1],   # NaN at session start
            })
            sid += 1
        pos += n
    return pd.DataFrame(rows), seg_id


# ----------------------------------------------------------------------------
# Demo data (writes a CSV in the expected format)
# ----------------------------------------------------------------------------
def write_demo_csv(path, seed=7):
    rng = np.random.default_rng(seed)
    frames = []
    for day in ["2026-10-01", "2026-10-02"]:
        n = 4 * 3600
        t = pd.date_range(f"{day} 08:00:00", periods=n, freq="s")
        jt = np.sort(rng.choice(np.arange(300, n - 300), 8, replace=False))
        js = rng.choice([-1, 1], 8) * rng.uniform(0.15, 0.8, 8)
        true = np.full(n, -2.4 + rng.normal(0, 0.3))
        for a, d in zip(jt, js):
            true[a:] += d
        y = true + 0.05 * rng.integers(-2, 3, n) + 0.04 * rng.standard_t(2.5, n)
        for a in rng.choice(n - 30, 50, replace=False):
            y[a:a + rng.integers(2, 20)] += rng.choice([-1, 1]) * rng.uniform(0.6, 2.0)
        y = np.round(y, 3)
        keep = np.r_[True, np.diff(y) != 0] & (rng.random(n) > 0.15)   # print-on-change + drops
        frames.append(pd.DataFrame({"timestamp": t[keep], "basis": y[keep], "true": true[keep]}))
    pd.concat(frames).to_csv(path, index=False)
    print(f"demo CSV written to {path}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="input CSV (or output path with --demo)")
    ap.add_argument("--demo", action="store_true", help="write a synthetic CSV to `csv` and fit it")
    ap.add_argument("--time-col", default=None, help="timestamp column (default: 1st column)")
    ap.add_argument("--value-col", default=None, help="basis column (default: 2nd column)")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply values, e.g. 100 if file is in %%")
    ap.add_argument("--lam", type=float, default=30.0, help="≈ half the shortest regime to keep, in seconds")
    ap.add_argument("--min-jump", type=float, default=0.10, help="smallest jump kept, bp")
    ap.add_argument("--min-dur", type=float, default=15.0, help="shortest segment kept, seconds")
    ap.add_argument("--max-gap", type=float, default=60.0, help="cap on per-tick weight, seconds")
    ap.add_argument("--session-gap", type=float, default=1800.0, help="gap (s) that starts a new session")
    ap.add_argument("--chunk", type=int, default=20000, help="max ticks per LP solve")
    ap.add_argument("--out-prefix", default=None, help="output path prefix (default: next to input)")
    a = ap.parse_args()

    if a.demo:
        write_demo_csv(a.csv)

    s = load_csv(a.csv, a.time_col, a.value_col, a.scale)
    prefix = Path(a.out_prefix) if a.out_prefix else Path(a.csv).with_suffix("")
    prefix.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    fit, sessions = fit_series(s, a.lam, a.min_jump, a.min_dur, a.max_gap, a.session_gap, a.chunk)
    elapsed = time.time() - t0
    segs, seg_id = segment_table(s, fit, a.session_gap, a.max_gap)

    out = pd.DataFrame({"timestamp": s.index, "raw": s.values,
                        "fitted": fit.values, "segment_id": seg_id})
    out.to_csv(f"{prefix}_fitted.csv", index=False)
    segs.to_csv(f"{prefix}_segments.csv", index=False)

    print(f"{len(s):,} ticks, {len(sessions)} session(s), "
          f"{s.index[0]} -> {s.index[-1]}, fit in {elapsed:.1f}s")
    print(f"{len(segs)} segments, {segs.jump.notna().sum()} jumps\n")
    with pd.option_context("display.width", 140, "display.float_format", "{:.3f}".format):
        print(segs.drop(columns="segment_id").to_string(index=False))

    # optional check if the file carries a ground-truth column (demo only)
    raw = pd.read_csv(a.csv)
    if "true" in raw.columns:
        tr = raw.assign(timestamp=pd.to_datetime(raw.timestamp)).drop_duplicates(
            "timestamp", keep="last").set_index("timestamp")["true"].reindex(s.index)
        print(f"\nMAE vs truth: raw {np.mean(np.abs(s - tr)):.4f}bp, "
              f"fitted {np.mean(np.abs(fit - tr)):.4f}bp")

    # chart: one panel per session (up to 4)
    show = sessions[:4]
    fig, axes = plt.subplots(len(show), 1, figsize=(13, 3.6 * len(show)), squeeze=False)
    for ax, sess in zip(axes[:, 0], show):
        f = fit.loc[sess.index]
        ax.plot(sess.index, sess.values, ".", ms=1, color="0.7", label="ticks")
        ax.step(f.index, f.values, where="post", color="tab:blue", lw=2,
                label=f"LAD fused lasso (λ={a.lam:g})")
        lo, hi = f.min(), f.max()
        ax.set_ylim(lo - 0.6, hi + 0.6)
        ax.set_ylabel("basis (bp)")
        ax.set_title(f"session {sess.index[0]:%Y-%m-%d %H:%M} – {sess.index[-1]:%H:%M}")
        ax.legend(loc="upper left", fontsize=8)
    plt.tight_layout()
    plt.savefig(f"{prefix}.png", dpi=120)
    print(f"\nwrote {prefix}_fitted.csv, {prefix}_segments.csv, {prefix}.png")


if __name__ == "__main__":
    main()
