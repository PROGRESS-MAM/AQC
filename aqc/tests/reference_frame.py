# --------- IMPORTS ---------
from functools import partial
from pathlib import Path
from typing import Callable
import math
import numpy as np
from ..aqc import Clip, Failure, Outcome, ProgressFn, Range, Test, ffmpeg_chunks, load_config, make_test, parse_range


# --------- CONFIG ---------
config_keys = ("frames", "ranges", "require", "threshold")
require_modes = ("any", "all")
hash_size = 32              # frames are scaled to hash_size x hash_size gray
low_size = 8                # low frequency block -> 64 bit pHash
chunk_frames = 256


# --------- STATIC ---------
frame_bytes = hash_size * hash_size
scale_filter = f"scale={hash_size}:{hash_size}:flags=area,format=gray"


def _dct_matrix(size: int) -> np.ndarray:
    rows, cols = np.arange(size)[:, None], np.arange(size)[None, :]
    matrix = np.sqrt(2 / size) * np.cos(np.pi * (2 * cols + 1) * rows / (2 * size))
    matrix[0] /= np.sqrt(2)
    return matrix.astype(np.float32)


dct = _dct_matrix(hash_size)


# --------- FUNC HASH ---------
def _phash(pixels: np.ndarray) -> np.ndarray:
    """(n, 32, 32) uint8 gray frames -> (n, 64) bool hashes."""
    low = (dct @ pixels.astype(np.float32) @ dct.T)[:, :low_size, :low_size].reshape(len(pixels), -1)
    return low > np.median(low[:, 1:], axis=1, keepdims=True)


def _frames(chunk: bytes) -> np.ndarray:
    usable = len(chunk) // frame_bytes * frame_bytes
    return np.frombuffer(chunk[:usable], np.uint8).reshape(-1, hash_size, hash_size)


def _image_hash(path: Path) -> np.ndarray:
    if not path.is_file():
        raise FileNotFoundError(f"Referenzbild nicht gefunden: {path}")
    data = b"".join(ffmpeg_chunks(["-i", str(path), "-frames:v", "1", "-vf", scale_filter, "-f", "rawvideo", "-"],
                                  frame_bytes))
    if len(data) < frame_bytes:
        raise ValueError(f"Referenzbild nicht lesbar: {path}")
    return _phash(_frames(data[:frame_bytes]))[0]


# --------- FUNC RUN ---------
def _scan_range(clip: Clip, start: float, end: float, hashes: np.ndarray,
                report: Callable[[int], None]) -> tuple[np.ndarray, np.ndarray, int]:
    """Best similarity and frame number per reference image inside one range."""
    first, stop = math.ceil(start * clip.fps - 1e-6), math.ceil(end * clip.fps - 1e-6)
    scores, frames, seen = np.full(len(hashes), -1.0), np.zeros(len(hashes), dtype=np.int64), 0
    args = ["-ss", f"{float(first / clip.fps):.6f}", "-i", str(clip.path), "-map", f"0:{clip.video['index']}",
            "-frames:v", str(max(1, stop - first)), "-vf", scale_filter, "-f", "rawvideo", "-"]
    for chunk in ffmpeg_chunks(args, chunk_frames * frame_bytes):
        pixels = _frames(chunk)
        similarity = 1 - (_phash(pixels)[:, None, :] != hashes[None]).mean(axis=2)
        top = similarity.argmax(axis=0)
        best = similarity[top, np.arange(len(hashes))]
        better = best > scores
        scores, frames = np.where(better, best, scores), np.where(better, first + seen + top, frames)
        seen += len(pixels)
        report(seen)
    return scores, frames, seen


def _run(names: tuple[str, ...], hashes: np.ndarray, ranges: tuple[Range, ...], require: str, threshold: float,
         clip: Clip, progress: ProgressFn) -> Outcome:
    if clip.video is None or clip.fps is None:
        raise ValueError("Clip hat keinen Videostream mit lesbarer Bildrate.")
    spans = [item.resolve(clip) for item in ranges]
    total = sum(end - start for start, end in spans)
    hits, done_media, done_frames = [], 0.0, 0
    for item, (start, end) in zip(ranges, spans):
        scores, frames, seen = _scan_range(clip, start, end, hashes, lambda count: progress(
            done_media + min(count / float(clip.fps), end - start), total, done_frames + count, "frames"))
        if not seen:
            raise RuntimeError(f"Keine Bilder im Bereich '{item.text}' gelesen.")
        hits += [(float(score), clip.tc(float(frame / clip.fps)), name, item.text)
                 for score, frame, name in zip(scores, frames, names)]
        done_media, done_frames = done_media + end - start, done_frames + seen
    found = [score >= threshold for score, *_ in hits]
    passed = any(found) if require == "any" else all(found)
    describe = lambda hit: f"{hit[0]:.3f} mit {hit[2]} im Bereich {hit[3]}"
    best = max(hits, key=lambda hit: hit[0])
    info = [f"beste Ähnlichkeit {describe(best)} bei {best[1]}"]
    info += [f"{describe(hit)} bei {hit[1]}" for hit in hits if hit is not best]
    expected = f">= {threshold:.2f} (require {require})"
    misses = [best] if require == "any" else [hit for hit, ok in zip(hits, found) if not ok]
    failures = [] if passed else [Failure("threshold", expected, describe(hit), tc=hit[1]) for hit in misses]
    return Outcome(passed, failures, info, total)


# --------- FUNC CONFIG ---------
def _text_list(path: Path, key: str, value) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"Config {path}: {key} muss eine nicht leere Liste von Texten sein.")
    return value


def reference_frame(config: str | Path, option: object = None) -> Test:
    """Test whether reference images appear in the given ranges (pHash similarity).

    config: name of configs/<name>.toml or a path; TOML with tool = "reference_frame", frames, ranges, require ("any"/"all") and threshold (0..1).
    Reference images are relative to the config and hashed now. option: None or must_fail.
    """
    path, data = load_config(config, "reference_frame", config_keys, config_keys)
    names = tuple(_text_list(path, "frames", data["frames"]))
    ranges = tuple(parse_range(item) for item in _text_list(path, "ranges", data["ranges"]))
    if data["require"] not in require_modes:
        raise ValueError(f"Config {path}: require muss {' oder '.join(require_modes)} sein.")
    threshold = data["threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        raise ValueError(f"Config {path}: threshold muss eine Zahl von 0 bis 1 sein.")
    hashes = np.stack([_image_hash(path.parent / name) for name in names])
    return make_test("reference_frame", path, option,
                     partial(_run, names, hashes, ranges, data["require"], float(threshold)))
