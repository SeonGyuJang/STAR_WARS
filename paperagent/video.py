"""The closing card as a short MP4.

Every carousel ends with the same animated slide: the words of the closing
slide rise into place one after another, then the lines below fade up.
cardnews renders the frames (CSS animations stepped with the Web Animations
API in headless Chromium) and this module encodes them with ffmpeg as H.264
(yuv420p, 30 fps, no sound) - the format Instagram takes for a video slide in
a carousel. Every other slide stays a still image.
"""
import logging
import re
import shutil
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger(__name__)
FPS = 30
OUTRO_SECONDS = 6.0


def ffmpeg_exe():
    """System ffmpeg, else the one bundled with imageio-ffmpeg (Heroku), else None."""
    if config.FFMPEG_EXECUTABLE:
        return config.FFMPEG_EXECUTABLE
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def available() -> bool:
    return config.VIDEO_CARDS and ffmpeg_exe() is not None


def media_seconds(path) -> float | None:
    """Length of a clip as ffmpeg reports it, or None (e.g. some GIFs)."""
    try:
        out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=30).stderr
    except Exception:
        return None
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) or None


def encode(frames_dir: Path, out_path: Path, fps: int = FPS) -> Path:
    """frames_dir/0000.jpg, 0001.jpg, ... -> out_path (MP4)."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-framerate", str(fps), "-i", str(Path(frames_dir) / "%04d.jpg"),
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high",
           "-movflags", "+faststart", "-an", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-600:]}")
    return Path(out_path)
