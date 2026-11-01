"""Video cards.

* The closing card of every carousel: the same animation each time - the
  letters S T A R W A R S stand from the first frame, the meaning of each
  unfolds out of its letter, then the claim rises and the lines below fade up.
* A TECH post's demo slide, only when the project's source has a demo clip
  that shows what the post is about: the card stays still and the clip plays
  in the picture window. Every other slide is a still image.


cardnews renders the closing frames (CSS animations stepped with the Web
Animations API in headless Chromium) and this module encodes them with ffmpeg as
H.264 (yuv420p, 30 fps, no sound) - the format Instagram takes for a video slide
in a carousel.
"""
import logging
import re
import shutil
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger(__name__)
FPS = 30
OUTRO_SECONDS = 7.0


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


CLIP_MIN, CLIP_MAX = 6.0, 15.0
VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v", ".gif")


def clip_seconds(clip) -> float:
    """A demo plays whole, looped up to at least 6 s, cut at 15 s."""
    secs = media_seconds(clip)
    if not secs:
        return 8.0
    if secs >= CLIP_MIN:
        return min(secs, CLIP_MAX)
    loops = int(CLIP_MIN // secs) + 1
    return min(secs * loops, CLIP_MAX)


def compose_clip(frame_png, mask_png, clip, box, out_path, pad_color="#16171b") -> Path:
    """A TECH demo slide: the card stays still and the project's own demo clip plays
    in its picture window. box = (x, y, w, h) of the window in the 1080x1350 card;
    mask_png is white where the window is (keeps its rounded corners)."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    x, y = int(round(box[0])), int(round(box[1]))
    w, h = max(2, int(box[2]) // 2 * 2), max(2, int(box[3]) // 2 * 2)
    seconds = clip_seconds(clip)
    clip_in = ["-ignore_loop", "0", "-i", str(clip)] if str(clip).lower().endswith(".gif") else ["-stream_loop", "-1", "-i", str(clip)]
    graph = (f"[1:v]fps={FPS},scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={pad_color},setsar=1,pad=1080:1350:{x}:{y}:color=black,format=rgba[m];"
             f"[2:v]format=gray,scale=1080:1350[a];[m][a]alphamerge[ma];"
             f"[0:v]scale=1080:1350,format=rgba[f];[f][ma]overlay=0:0:format=auto,format=yuv420p[out]")
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(frame_png), *clip_in,
           "-loop", "1", "-framerate", str(FPS), "-i", str(mask_png), "-filter_complex", graph, "-map", "[out]",
           "-t", f"{seconds:.2f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
           "-profile:v", "high", "-movflags", "+faststart", "-an", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-600:]}")
    return Path(out_path)
