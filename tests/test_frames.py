from pathlib import Path

import cv2
import numpy as np

from motiongrammetry.frames import FrameStats, choose_keyframes, extract_keyframes


def _make_video(path: Path, blurred: set[int], pause: range, n: int = 120) -> None:
    rng = np.random.default_rng(0)
    texture = cv2.GaussianBlur((rng.random((480, 2400)) * 255).astype(np.uint8), (0, 0), 1.5)
    texture = cv2.cvtColor(texture, cv2.COLOR_GRAY2BGR)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (640, 480))
    x = 0.0
    for i in range(n):
        if i not in pause:
            x += 12
        frame = texture[:, int(x) : int(x) + 640].copy()
        if i in blurred:
            frame = cv2.blur(frame, (25, 1))
        writer.write(frame)
    writer.release()


def test_choose_keyframes_skips_pause_and_blur():
    stats = []
    for i in range(100):
        motion = 0.0 if 40 <= i < 70 else 0.01
        sharp = 5.0 if i % 10 == 3 else 100.0
        stats.append(FrameStats(i, sharp, motion))
    chosen = choose_keyframes(stats, num_frames=20)
    idx = [s.index for s in chosen]
    # Equal-motion spacing: the 30-sample pause holds at most one keyframe.
    assert sum(40 <= i < 70 for i in idx) <= 1
    assert all(i % 10 != 3 for i in idx)
    assert idx == sorted(idx)


def test_extract_keyframes_end_to_end(tmp_path):
    video = tmp_path / "v.mp4"
    _make_video(video, blurred=set(range(0, 120, 7)), pause=range(50, 80))
    info = extract_keyframes(video, tmp_path / "frames", num_frames=20, sample_fps=30)
    files = sorted((tmp_path / "frames").glob("*.jpg"))
    assert len(files) == info["keyframes"] >= 15
    assert not set(info["source_indices"]) & set(range(0, 120, 7))
    assert sum(50 <= i < 80 for i in info["source_indices"]) <= 2
