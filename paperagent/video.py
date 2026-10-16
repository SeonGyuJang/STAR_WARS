"""Video card slides: the card frame stays still and only one window moves.

A slide is rendered twice by cardnews: once as the normal card (the frame) and
once as a mask that is white only where the window is. ffmpeg then scales the
moving media into the window, cuts it to the mask (so rounded corners stay
round) and lays it over the frame:

    frame.png ──────────────────────────────┐
    media ── fit to window ── pad ── alpha ─┴─ overlay ── H.264 MP4 (1080x1350)
    mask.png ──────────────────────┘

The media is either a real clip (a demo GIF/MP4 from a project's README) or a
still image - a paper figure or a project image - shown with a slow zoom and
pan across it, which starts and ends on the full view so the loop is seamless.

Instagram carousels take MP4 (H.264, yuv420p) slides of up to 60 seconds
between photos; clips here are 6-15 seconds.
"""
import logging
import math
import re
import shutil
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger(__name__)
W, H = 1080, 1350
FPS = 30
STILL_SECONDS = 9
VIDEO_EXT = (".mp4", ".webm", ".mov", ".gif", ".m4v")


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


def is_video(path) -> bool:
    return str(path).lower().endswith(VIDEO_EXT)


def media_seconds(path) -> float | None:
    """Length of a clip as ffmpeg reports it, or None (e.g. some GIFs)."""
    try:
        out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=30).stderr
    except Exception:
        return None
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        return None
    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return secs or None


def clip_seconds(media) -> float:
    """Stills run STILL_SECONDS; a clip plays whole (looped to at least 6s), at most 15s."""
    if not is_video(media):
        return STILL_SECONDS
    secs = media_seconds(media)
    if not secs:
        return 8.0
    if secs >= 6:
        return min(secs, 15.0)
    return min(secs * math.ceil(6 / secs), 15.0)


def _ease(u: str) -> str:
    return f"(({u})*({u})*(3-2*({u})))"


def ken_burns(w: int, h: int, frames: int) -> str:
    """zoompan filter: full view, zoom in on the left, pan to the right, zoom back out."""
    t = f"(on/{frames})"
    a, b, c = 0.12, 0.34, 0.78
    zmax = 1.55
    up = _ease(f"({t}-{a})/{b - a}")
    across = _ease(f"({t}-{b})/{c - b}")
    down = _ease(f"({t}-{c})/{1 - c}")
    z = f"if(lt({t},{a}),1,if(lt({t},{b}),1+{zmax - 1}*{up},if(lt({t},{c}),{zmax},{zmax}-{zmax - 1}*{down})))"
    px = f"if(lt({t},{b}),0,if(lt({t},{c}),{across},1-0.5*{down}))"
    return (f"zoompan=z='{z}':x='(iw-iw/zoom)*({px})':y='(ih-ih/zoom)/2':d={frames}:s={w}x{h}:fps={FPS}")


def _even(n) -> int:
    return max(2, int(round(n)) // 2 * 2)


def compose(frame_png, mask_png, media, box, out_path, pad_color="white", seconds=None) -> Path:
    """Write the MP4 slide. box = (x, y, w, h) of the window inside the 1080x1350 card."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    x, y = int(round(box[0])), int(round(box[1]))
    w, h = _even(box[2]), _even(box[3])
    seconds = seconds or clip_seconds(media)
    frames = int(seconds * FPS)
    fit = f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={pad_color}"
    if is_video(media):
        media_in = ["-stream_loop", "-1", "-i", str(media)]
        if str(media).lower().endswith(".gif"):
            media_in = ["-ignore_loop", "0", "-i", str(media)]
        moving = f"[1:v]fps={FPS},{fit},setsar=1"
    else:
        # zoom on a 2x canvas so the pan moves smoothly instead of in whole pixels
        big = f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=decrease:flags=lanczos,pad={w * 2}:{h * 2}:(ow-iw)/2:(oh-ih)/2:color={pad_color}"
        media_in = ["-i", str(media)]
        moving = f"[1:v]{big},setsar=1,{ken_burns(w, h, frames)}"
    graph = (f"{moving},pad={W}:{H}:{x}:{y}:color=black,format=rgba[m];"
             f"[2:v]format=gray,scale={W}:{H}[a];[m][a]alphamerge[ma];"
             f"[0:v]scale={W}:{H},format=rgba[f];[f][ma]overlay=0:0:format=auto,format=yuv420p[out]")
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-framerate", str(FPS), "-i", str(frame_png),
           *media_in,
           "-loop", "1", "-framerate", str(FPS), "-i", str(mask_png),
           "-filter_complex", graph, "-map", "[out]", "-t", f"{seconds:.2f}", "-r", str(FPS),
           "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p", "-profile:v", "high",
           "-movflags", "+faststart", "-an", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-600:]}")
    return Path(out_path)
