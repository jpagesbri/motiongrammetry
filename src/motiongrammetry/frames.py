"""Keyframe selection from a hand-held orbit video.

Taking every Nth frame wastes budget on the moments where the operator stops and
starves the fast parts of the orbit. Instead we:

1. sample the video at ``sample_fps`` and, on a downscaled grey copy, measure
   - sharpness: variance of the Laplacian (motion blur / defocus drops it sharply)
   - motion: median sparse optical-flow displacement against the previous sample,
     as a fraction of image width (a proxy for viewpoint change / parallax);
2. integrate motion into a cumulative "distance travelled" curve;
3. split that curve into ``num_frames`` equal-motion segments and keep the sharpest
   frame of each segment, dropping segments whose best frame is still blurry
   relative to its neighbourhood.

Pauses then collapse into a single segment, fast sweeps get more frames, and the
selected set is evenly spaced in viewpoint rather than in time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

ANALYSIS_WIDTH = 480


@dataclass
class FrameStats:
    index: int  # frame index in the source video
    sharpness: float
    motion: float  # median flow vs. previous sample, fraction of width


def _sharpness(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _motion(prev: np.ndarray, cur: np.ndarray) -> float:
    pts = cv2.goodFeaturesToTrack(prev, maxCorners=400, qualityLevel=0.01, minDistance=8)
    if pts is None or len(pts) < 10:
        # Texture-poor frame: fall back to global phase correlation.
        (dx, dy), _ = cv2.phaseCorrelate(prev.astype(np.float32), cur.astype(np.float32))
        return float(np.hypot(dx, dy)) / prev.shape[1]
    nxt, status, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None, winSize=(21, 21), maxLevel=3)
    ok = status.ravel() == 1
    if ok.sum() < 10:
        return 0.0
    disp = np.linalg.norm((nxt - pts).reshape(-1, 2)[ok], axis=1)
    return float(np.median(disp)) / prev.shape[1]


def analyze_video(video: Path, sample_fps: float = 10.0) -> tuple[list[FrameStats], float]:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(fps / sample_fps))
    stats: list[FrameStats] = []
    prev = None
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (ANALYSIS_WIDTH, round(h * ANALYSIS_WIDTH / w)), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            motion = _motion(prev, gray) if prev is not None else 0.0
            stats.append(FrameStats(idx, _sharpness(gray), motion))
            prev = gray
        idx += 1
    cap.release()
    if not stats:
        raise RuntimeError(f"no frames decoded from {video}")
    return stats, fps


def choose_keyframes(stats: list[FrameStats], num_frames: int, blur_reject: float = 0.35,
                     report: dict | None = None) -> list[FrameStats]:
    """Pick the sharpest frame in each of ``num_frames`` equal-motion segments.

    ``report`` (if given) receives the number of segments and of segments dropped as blurry.
    """
    if len(stats) <= num_frames:
        if report is not None:
            report.update(segments=len(stats), blur_rejected=0)
        return list(stats)
    motion = np.array([s.motion for s in stats])
    sharp = np.array([s.sharpness for s in stats])
    cum = np.cumsum(motion)
    total = cum[-1]
    if total <= 1e-9:
        # Static camera (e.g. turntable footage): fall back to uniform time spacing.
        bins = np.linspace(0, len(stats), num_frames + 1).astype(int)
        segments = [np.arange(a, b) for a, b in zip(bins[:-1], bins[1:]) if b > a]
    else:
        edges = np.linspace(0.0, total, num_frames + 1)
        seg_id = np.clip(np.searchsorted(edges, cum, side="right") - 1, 0, num_frames - 1)
        segments = [np.flatnonzero(seg_id == k) for k in range(num_frames)]
        segments = [s for s in segments if len(s)]

    # Local sharpness reference: lighting and texture change around an orbit, so a
    # global threshold would reject whole sides of the subject.
    win = max(5, len(stats) // 20)
    kernel = np.ones(win) / win
    local_ref = np.convolve(np.pad(sharp, win // 2, mode="edge"), kernel, mode="same")[win // 2 : win // 2 + len(sharp)]

    chosen: list[FrameStats] = []
    for seg in segments:
        # Among nearly-sharpest frames prefer the segment's motion-centre, so picks
        # from neighbouring segments don't bunch up at a shared boundary.
        good = seg[sharp[seg] >= 0.8 * sharp[seg].max()]
        centre = 0.5 * (cum[seg[0]] + cum[seg[-1]])
        best = good[np.argmin(np.abs(cum[good] - centre))]
        if sharp[best] < blur_reject * local_ref[best]:
            continue
        chosen.append(stats[best])
    if report is not None:
        report.update(segments=len(segments), blur_rejected=len(segments) - len(chosen))
    return chosen


def extract_keyframes(
    video: Path,
    out_dir: Path,
    num_frames: int = 150,
    sample_fps: float = 10.0,
    max_side: int = 2000,
    blur_reject: float = 0.35,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    stats, fps = analyze_video(video, sample_fps)
    report: dict = {}
    chosen = choose_keyframes(stats, num_frames, blur_reject, report)
    if not chosen:
        raise RuntimeError("no usable keyframes (every candidate was rejected as blurry)")
    wanted = {s.index for s in chosen}
    log.info("analyzed %d samples, keeping %d keyframes", len(stats), len(chosen))

    cap = cv2.VideoCapture(str(video))
    idx = 0
    written = 0
    last = max(wanted)
    while idx <= last:
        ok = cap.grab()
        if not ok:
            break
        if idx in wanted:
            ok, frame = cap.retrieve()
            if ok:
                h, w = frame.shape[:2]
                scale = min(1.0, max_side / max(h, w))
                if scale < 1.0:
                    frame = cv2.resize(frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
                cv2.imwrite(str(out_dir / f"frame_{written:05d}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                written += 1
        idx += 1
    cap.release()

    sharp = np.array([s.sharpness for s in stats])
    return {
        "video": str(video),
        "fps": fps,
        "samples": len(stats),
        "keyframes": written,
        "source_indices": sorted(wanted),
        "total_motion_widths": float(sum(s.motion for s in stats)),
        "median_sharpness": float(np.median(sharp)),
        **report,
    }
