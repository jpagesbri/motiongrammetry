"""Stage wrapper: exit codes, logs, history, stamps, cancellation, presets, watchdog."""

import os
import subprocess
import sys
import time

import pytest

from motiongrammetry import pipeline
from motiongrammetry.diagnostics import EXIT_CANCELLED, EXIT_CRASH, EXIT_GATE, EXIT_OK, EXIT_PREFLIGHT
from motiongrammetry.options import resolve
from motiongrammetry.workspace import Workspace
from test_frames import _make_video


@pytest.fixture()
def ws(tmp_path):
    return Workspace(tmp_path / "w")


def _last(ws, stage):
    return [h for h in ws.history() if h["stage"] == stage][-1]


def test_preflight_failure_does_not_start_and_is_logged(ws):
    rc = pipeline.run_stage(ws, "mask", resolve({}))  # no keyframes yet
    assert rc == EXIT_PREFLIGHT
    h = _last(ws, "mask")
    assert h["status"] == "preflight-failed"
    assert any(d["code"] == "no-frames" for d in h["diagnostics"])
    text = (ws.root / h["log"]).read_text()
    assert "no keyframes yet" in text and "PREFLIGHT-FAILED" in text
    assert not (ws.root / ".stamps" / "mask").exists()


def test_success_writes_log_stamp_and_history(ws, tmp_path):
    video = tmp_path / "v.mp4"
    _make_video(video, blurred=set(), pause=range(0), n=150)
    rc = pipeline.run_stage(ws, "frames", resolve({"num": 40, "sample_fps": 30}), str(video))
    assert rc == EXIT_OK
    h = _last(ws, "frames")
    assert h["status"] in ("ok", "warning") and h["options"]["num"] == 40
    assert (ws.root / ".stamps" / "frames").exists()
    assert "keyframes" in (ws.root / h["log"]).read_text()


def test_quality_gate_failure(ws, tmp_path):
    video = tmp_path / "v.mp4"
    _make_video(video, blurred=set(), pause=range(0), n=20)
    rc = pipeline.run_stage(ws, "frames", resolve({"num": 5, "sample_fps": 30}), str(video))
    assert rc == EXIT_GATE
    h = _last(ws, "frames")
    assert h["status"] == "gate-failed" and any(d["code"] == "too-few-frames" for d in h["diagnostics"])
    assert not (ws.root / ".stamps" / "frames").exists()


def test_strict_turns_warnings_into_failure(ws, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    _make_video(video, blurred=set(), pause=range(0), n=150)
    rc = pipeline.run_stage(ws, "frames", resolve({"num": 20, "sample_fps": 30, "strict": True}), str(video))
    assert rc == EXIT_GATE  # 20 keyframes -> "few-frames" warning -> strict failure


def test_crash_is_classified_with_traceback_and_native_output_captured(ws, monkeypatch):
    def boom(ws_, o, video):
        os.write(1, b"native-library-says-hello\n")  # C/C++ style output straight to fd 1
        raise ValueError("kaboom")

    monkeypatch.setitem(pipeline.BODIES, "frames", boom)
    monkeypatch.setattr(pipeline.checks, "preflight", lambda *a, **k: [])
    rc = pipeline.run_stage(ws, "frames", resolve({}), "x.mp4")
    assert rc == EXIT_CRASH
    h = _last(ws, "frames")
    assert h["diagnostics"][-1]["code"] == "internal-error"
    text = (ws.root / h["log"]).read_text()
    assert "native-library-says-hello" in text and "Traceback" in text and "kaboom" in text


def test_cancel_is_recorded(ws, monkeypatch):
    def interrupted(ws_, o, video):
        raise KeyboardInterrupt

    monkeypatch.setitem(pipeline.BODIES, "frames", interrupted)
    monkeypatch.setattr(pipeline.checks, "preflight", lambda *a, **k: [])
    assert pipeline.run_stage(ws, "frames", resolve({}), "x.mp4") == EXIT_CANCELLED
    assert _last(ws, "frames")["status"] == "cancelled"


def test_pipeline_stops_and_marks_the_rest_skipped(ws):
    rc = pipeline.run_pipeline(ws, ["frames", "mask", "sfm"], resolve({}), video="/nonexistent.mp4")
    assert rc == EXIT_PREFLIGHT
    statuses = {h["stage"]: h["status"] for h in ws.history()}
    assert statuses == {"frames": "preflight-failed", "mask": "skipped", "sfm": "skipped"}


def test_options_precedence():
    o = resolve({"num": 77}, "low-disk")
    assert o["num"] == 77                 # explicit beats preset
    assert o["resolution_level"] == 2     # preset beats default
    assert o["sift_peak"] == 0.0015       # default
    assert resolve({"mask_sfm": "true"})["mask_sfm"] is True
    with pytest.raises(ValueError):
        resolve({}, "no-such-preset")


def test_cli_exit_code_and_logs_command(ws):
    env = dict(os.environ, PYTHONPATH="src")
    r = subprocess.run([sys.executable, "-m", "motiongrammetry.cli", "sfm", str(ws.root)], capture_output=True,
                       text=True, env=env)
    assert r.returncode == EXIT_PREFLIGHT
    r = subprocess.run([sys.executable, "-m", "motiongrammetry.cli", "logs", str(ws.root)], capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0 and "sfm preflight-failed" in r.stdout and "no-frames" in r.stdout


def test_openmvs_watchdog_detects_silence():
    from motiongrammetry.mvs import _stream_progress

    proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    t0 = time.time()
    try:
        assert _stream_progress("Fake", proc, [], idle_timeout=1.0) is False
    finally:
        proc.kill()
    assert time.time() - t0 < 10


def test_openmvs_exit_explanations(tmp_path):
    from motiongrammetry.mvs import _explain_exit

    assert _explain_exit("DensifyPointCloud", 137, [], tmp_path / "x.log").code == "out-of-memory"
    assert _explain_exit("TextureMesh", 139, [], tmp_path / "x.log").code == "openmvs-crashed"
    assert _explain_exit("X", 1, ["write: No space left on device"], tmp_path / "x.log").code == "disk-full"


def test_failed_rerun_drops_completion_stamp(ws, monkeypatch):
    ws.stamp("frames")
    monkeypatch.setitem(pipeline.BODIES, "frames", lambda *a: (_ for _ in ()).throw(RuntimeError("bad")))
    monkeypatch.setattr(pipeline.checks, "preflight", lambda *a, **k: [])
    pipeline.run_stage(ws, "frames", resolve({}), "x.mp4")
    assert not (ws.root / ".stamps" / "frames").exists()


def test_sigterm_unwinds_despite_glog_handlers():
    # pycolmap's glog kills the process on SIGTERM unless our handler wins. 'ready' is printed inside
    # the try: the signal can land while print() is still returning.
    code = ("import time, sys\n"
            "from motiongrammetry.pipeline import install_signal_handlers\n"
            "install_signal_handlers()\n"
            "try:\n    print('ready', flush=True)\n    time.sleep(30)\n"
            "except KeyboardInterrupt:\n    print('unwound', flush=True); sys.exit(130)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            env=dict(os.environ, PYTHONPATH="src"))
    assert proc.stdout.readline().strip() == "ready"
    proc.terminate()
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 130 and "unwound" in out
