#!/usr/bin/env python3
"""Burn karaoke narration, a spotlight on the element each cue is about, and the spoken
narration track into the raw clip.

`record-feature-video.sh` films the app but films it *silently*: Playwright does not capture
the mouse pointer, so a reviewer watching the raw .webm cannot tell which of the forty widgets
on screen just changed. The recorder therefore writes, per cue, the on-screen box of the
element the narration is talking about; this turns those boxes into something visible.

Per boxed cue: a bright rectangle just outside the element, the rest of the frame dimmed a
little so the rectangle reads as a spotlight, both stepped down over ~1.8s so the emphasis
fades instead of snapping off (ffmpeg has no alpha-animated drawbox — the fade is a handful
of `enable`-gated drawboxes with decreasing alpha).

The captions are karaoke, not a paragraph in a box. A whole sentence dumped on screen at once
is read in the first half-second and then ignored for the four seconds it stays up, which is
exactly when the thing it describes actually happens. So each cue is cut into short spoken
chunks and the words arrive one at a time — already-said words white, the word being said
yellow, the rest not yet there — which paces the reader to the demo instead of racing ahead
of it. When `record-feature-video.sh` synthesised narration, the arrival times are the real
per-word times out of the speech synthesizer, so the highlight tracks the voice exactly;
without narration they are spread across the cue at a readable rate.

Each word carries its own black outline instead of the whole line sitting on a translucent
slab: a slab is a second rectangle competing with the app's own boxes for attention, and it
has to be sized for text that has not arrived yet. An outline is per-glyph, so it costs no
layout and reads over both the white forms and the dark table headers underneath.

The narration is rendered to PNG frames with PIL and composited with `overlay`, not drawn with
`drawtext`: this ffmpeg has neither freetype nor libass (no drawtext, no subtitles filter),
and even where it does, feeding curly quotes and em dashes through a filtergraph is where
these scripts break. The frames go through the concat demuxer into one transparent overlay
track rather than becoming one ffmpeg input each — a word-level caption is a few hundred
states, and a few hundred open inputs hits the open-file limit before it hits ffmpeg's.

Usage:
    annotate-feature-video.py <raw.webm> <cues.json> <out.webm> [--offset SECONDS]
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ACCENT = (255, 45, 85)
CAPTION_BOTTOM_MARGIN = 26
BAND_PAD_Y = 14
FONT_SIZE = 34
LINE_H = 44
SPOKEN_FG = (255, 255, 255, 250)
LEADING_FG = (255, 214, 10, 255)
# Thick enough to survive over a white form field, thin enough not to close up the counters
# of the glyphs at this size.
STROKE = 5
# The outlines eat into the gap between words: a plain space is narrower than two strokes,
# so without this the line reads as one long word.
WORD_GAP = STROKE * 1.3
STROKE_FG = (0, 0, 0, 245)
STROKE_WARN = (122, 6, 24, 250)
FONTS = [
  "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
  "/System/Library/Fonts/Supplemental/Arial.ttf",
  "/System/Library/Fonts/Helvetica.ttc",
  "/Library/Fonts/Arial.ttf",
]

# One line, few words: a chunk has to be takeable in a glance, because by design the reader
# only ever sees the part of it that has already been said.
CHUNK_MAX_W = 780
CHUNK_MAX_WORDS = 5
CHUNK_MIN_WORDS = 2
BREAK_AFTER = (".", ",", ";", ":", "!", "?", "—", "…")

# The leading word lands slightly oversized for a beat. It is what separates a karaoke caption
# from a teleprompter, and it costs one extra rendered state per word.
POP_SCALE = 1.14
POP_DUR = 0.11
MIN_SEGMENT = 0.04

# Without synthesized narration, words are spread over the cue at a rate a person can read.
READ_FIXED = 0.13
READ_PER_CHAR = 0.033
READ_FILL = 0.88

# Box padding keeps the rectangle off the element, and the thickness grows inward from that
# padded rect — so even the fattest step sits in the gutter rather than over the widget.
BOX_PAD = 6
# (duration, dim alpha of the surround, outline thickness, outline alpha)
PHASES = [
  (0.90, 0.24, 5, 1.00),
  (0.30, 0.17, 4, 0.78),
  (0.30, 0.10, 3, 0.52),
  (0.30, 0.05, 2, 0.28),
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
  for path in FONTS:
    if Path(path).is_file():
      return ImageFont.truetype(path, size)
  raise SystemExit("no usable TTF found for the captions")


def spoken_text(text: str) -> tuple[str, bool]:
  """The warning glyph has no place in Arial — it would render as tofu. The red outline says it."""
  warn = text.startswith("⚠")
  return (text.lstrip("⚠ ").strip() if warn else text), warn


def chunk_words(words: list[dict], measure) -> list[list[dict]]:
  """Cut a cue into one-line karaoke chunks, breaking on punctuation where it can."""
  chunks, current = [], []
  for word in words:
    probe = current + [word]
    too_wide = measure([w["w"] for w in probe]) > CHUNK_MAX_W
    if current and (too_wide or len(probe) > CHUNK_MAX_WORDS):
      chunks.append(current)
      current = [word]
      continue
    current = probe
    if len(current) >= CHUNK_MIN_WORDS and word["w"].endswith(BREAK_AFTER):
      chunks.append(current)
      current = []
  if current:
    chunks.append(current)
  return chunks


def read_times(words: list[str], start: float, end: float) -> list[float]:
  """Fallback pacing: proportional to word length, stretched to fill most of the window."""
  costs = [READ_FIXED + READ_PER_CHAR * len(w) for w in words]
  total = sum(costs)
  span = (end - start) * READ_FILL
  scale = min(1.0, span / total) if total else 1.0
  times, t = [], start
  for cost in costs:
    times.append(t)
    t += cost * scale
  return times


def caption_y(box: dict | None, band_h: int, frame_h: int) -> int:
  """Bottom of the frame, unless the words would sit on top of the element being spotlit."""
  bottom = frame_h - band_h - CAPTION_BOTTOM_MARGIN
  top = CAPTION_BOTTOM_MARGIN
  if not box:
    return bottom
  hits_bottom = box["y"] + box["height"] + BOX_PAD > bottom - 10
  hits_top = box["y"] - BOX_PAD < top + band_h + 10
  return top if hits_bottom and not hits_top else bottom


class Renderer:
  """Draws one caption state as a full transparent frame, and remembers what it drew."""

  def __init__(self, width: int, height: int):
    self.w, self.h = width, height
    self.font = load_font(FONT_SIZE)
    self.pop_font = load_font(int(round(FONT_SIZE * POP_SCALE)))
    self.probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    self.blank: Path | None = None
    self.cache: dict[tuple, Path] = {}

  def measure(self, text: str) -> float:
    return self.probe.textlength(text, font=self.font)

  def line_width(self, words: list[str]) -> float:
    return self.layout(words)[1]

  def layout(self, words: list[str]) -> tuple[list[float], float]:
    """Where each word of a chunk sits, and how wide the finished line is.

    Every word keeps the slot it will have when the line is complete, and the line is placed
    so that finished line lands centred — it fills rightward from a fixed left edge.
    Re-centring on what is currently visible would slide every word leftward on each new
    arrival, which is unreadable at speaking speed.
    """
    space = self.measure(" ") + WORD_GAP
    xs, x = [], 0.0
    for word in words:
      xs.append(x)
      x += self.measure(word) + space
    return xs, x - space

  def frame(self, tmp: Path, key: tuple, words: list[str], upto: int, pop: bool,
      warn: bool, box: dict | None) -> Path:
    cached = self.cache.get(key)
    if cached:
      return cached
    xs, line_w = self.layout(words)
    band_h = LINE_H + 2 * BAND_PAD_Y
    left = (self.w - line_w) / 2
    y = caption_y(box, band_h, self.h) + BAND_PAD_Y + (LINE_H - FONT_SIZE) / 2 - 3
    stroke = STROKE_WARN if warn else STROKE_FG

    img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Two passes, and the arriving word deliberately in the second one: it is the only word
    # that changes size, so it is the only one whose outline can land on a neighbour — and it
    # has to land ON it. Drawing the line in one pass leaves that to the ordering by accident.
    for i in range(upto):
      d.text((left + xs[i], y), words[i], font=self.font, fill=SPOKEN_FG,
          stroke_width=STROKE, stroke_fill=stroke)
    if pop:
      # The oversized glyphs are centred on the slot the normal-size word would occupy, so
      # the line does not shift when the pop ends.
      grow = self.probe.textlength(words[upto], font=self.pop_font) - self.measure(words[upto])
      d.text((left + xs[upto] - grow / 2, y - (POP_SCALE - 1) * FONT_SIZE / 2), words[upto],
          font=self.pop_font, fill=LEADING_FG, stroke_width=STROKE, stroke_fill=stroke)
    else:
      d.text((left + xs[upto], y), words[upto], font=self.font, fill=LEADING_FG,
          stroke_width=STROKE, stroke_fill=stroke)

    path = tmp / f"cap{len(self.cache):04d}.png"
    img.save(path)
    self.cache[key] = path
    return path

  def transparent(self, tmp: Path) -> Path:
    if self.blank is None:
      self.blank = tmp / "blank.png"
      Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0)).save(self.blank)
    return self.blank


def probe_video(video: Path) -> tuple[int, int, float]:
  cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
      "stream=width,height:format=duration", "-of", "json", str(video)]
  out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
  meta = json.loads(out)
  stream = meta["streams"][0]
  return stream["width"], stream["height"], float(meta["format"]["duration"])


def spotlight(box: dict, w: int, h: int, start: float, budget: float) -> list[str]:
  """Rectangle + dimmed surround, stepped down over `budget` seconds."""
  x0 = max(0, int(box["x"]) - BOX_PAD)
  y0 = max(0, int(box["y"]) - BOX_PAD)
  x1 = min(w, int(box["x"] + box["width"]) + BOX_PAD)
  y1 = min(h, int(box["y"] + box["height"]) + BOX_PAD)
  if x1 - x0 < 4 or y1 - y0 < 4:
    return []
  filters, t = [], start
  span = sum(p[0] for p in PHASES)
  for dur, dim, thick, alpha in PHASES:
    end = min(t + dur * budget / span, start + budget)
    if end - t < 0.02:
      break
    gate = f"enable=between(t\\,{t:.3f}\\,{end:.3f})"
    for bx, by, bw, bh in (
        (0, 0, w, y0),
        (0, y1, w, h - y1),
        (0, y0, x0, y1 - y0),
        (x1, y0, w - x1, y1 - y0)):
      if bw > 0 and bh > 0:
        filters.append(
            f"drawbox=x={bx}:y={by}:w={bw}:h={bh}:color=black@{dim}:t=fill:{gate}")
    filters.append(
        f"drawbox=x={x0}:y={y0}:w={x1 - x0}:h={y1 - y0}:"
        f"color=0x{ACCENT[0]:02x}{ACCENT[1]:02x}{ACCENT[2]:02x}@{alpha}:t={thick}:{gate}")
    t = end
  return filters


def caption_segments(cues: list[dict], windows: list[tuple[float, float]], r: Renderer,
    tmp: Path) -> list[tuple[float, float, Path]]:
  """Every (start, end, frame) the caption track is made of, in order, gaps included."""
  segments: list[tuple[float, float, Path]] = []
  for cue, (start, end) in zip(cues, windows):
    text, warn = spoken_text(cue["text"])
    tokens = [w for w in text.split(" ") if w]
    if not tokens:
      continue
    if cue.get("words"):
      # Real times from the synthesizer, on the same clock as the audio that will be mixed in.
      times = [start + w["t"] for w in cue["words"]]
      tokens = [w["w"] for w in cue["words"]]
    else:
      times = read_times(tokens, start, end)
    words = [{"w": w, "t": t} for w, t in zip(tokens, times) if t < end]
    if not words:
      continue

    chunks = chunk_words(words, r.line_width)
    for c, chunk in enumerate(chunks):
      texts = [w["w"] for w in chunk]
      chunk_end = chunks[c + 1][0]["t"] if c + 1 < len(chunks) else end
      chunk_end = min(chunk_end, end)
      for j, word in enumerate(chunk):
        w_start = max(word["t"], start)
        w_end = chunk[j + 1]["t"] if j + 1 < len(chunk) else chunk_end
        w_end = min(w_end, chunk_end)
        if w_end - w_start < MIN_SEGMENT:
          continue
        key_base = (id(cue), c, j)
        pop_end = min(w_start + POP_DUR, w_end)
        if pop_end - w_start >= MIN_SEGMENT:
          segments.append((w_start, pop_end, r.frame(tmp, key_base + (True,), texts, j, True,
              warn, cue.get("box"))))
          w_start = pop_end
        if w_end - w_start >= MIN_SEGMENT:
          segments.append((w_start, w_end, r.frame(tmp, key_base + (False,), texts, j, False,
              warn, cue.get("box"))))
  return segments


def caption_track(segments: list[tuple[float, float, Path]], duration: float, r: Renderer,
    tmp: Path) -> Path:
  """One transparent video of the whole caption track, blanks and all."""
  blank = r.transparent(tmp)
  timeline: list[tuple[Path, float]] = []
  cursor = 0.0
  for start, end, png in segments:
    if start - cursor > 0.001:
      timeline.append((blank, start - cursor))
    timeline.append((png, max(MIN_SEGMENT, end - start)))
    cursor = end
  if duration - cursor > 0.001:
    timeline.append((blank, duration - cursor))

  listing = tmp / "captions.txt"
  lines = []
  for png, dur in timeline:
    lines.append(f"file '{png.name}'")
    lines.append(f"duration {dur:.3f}")
  lines.append(f"file '{timeline[-1][0].name}'")   # the concat demuxer drops the last duration
  listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

  track = tmp / "captions.mov"
  subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
      "-i", str(listing), "-fps_mode", "vfr", "-c:v", "qtrle", "-pix_fmt", "argb",
      str(track)], check=True, cwd=tmp)
  return track


def audio_args(cues: list[dict], windows: list[tuple[float, float]], base: Path,
    duration: float, first_input: int) -> tuple[list[str], list[str], str | None]:
  """Place each cue's synthesised .wav at the moment its caption starts."""
  inputs, graph, labels = [], [], []
  for cue, (start, _) in zip(cues, windows):
    wav = cue.get("audio")
    if not wav:
      continue
    path = Path(wav)
    if not path.is_absolute():
      path = base / wav
    if not path.is_file():
      continue
    idx = first_input + len(labels)
    inputs += ["-i", str(path)]
    graph.append(f"[{idx}:a]adelay={int(start * 1000)}:all=1[na{len(labels)}]")
    labels.append(f"[na{len(labels)}]")
  if not labels:
    return [], [], None
  graph.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:dropout_transition=0,"
      f"atrim=0:{duration:.3f},aresample=48000[aout]")
  return inputs, graph, "[aout]"


def build(raw: Path, cues: list[dict], out: Path, tmp: Path, offset: float) -> None:
  w, h, duration = probe_video(raw)
  windows = []
  for i, cue in enumerate(cues):
    start = 0.0 if i == 0 else max(0.0, cue["t"] + offset)
    end = cues[i + 1]["t"] + offset if i + 1 < len(cues) else duration
    windows.append((start, min(end, duration)))

  emphasis = []
  for cue, (start, end) in zip(cues, windows):
    if not cue.get("box"):
      continue
    budget = min(1.8, max(0.6, end - max(start, cue["t"] + offset) - 0.05))
    emphasis += spotlight(cue["box"], w, h, max(start, cue["t"] + offset), budget)

  renderer = Renderer(w, h)
  segments = caption_segments(cues, windows, renderer, tmp)
  track = caption_track(segments, duration, renderer, tmp)

  graph = [f"[0:v]{','.join(['format=rgba'] + emphasis)}[emph]",
      "[emph][1:v]overlay=x=0:y=0:eof_action=pass[capped]",
      "[capped]format=yuv420p[vout]"]
  a_inputs, a_graph, a_label = audio_args(cues, windows, out.parent, duration, 2)
  graph += a_graph

  cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-i", str(track)] + a_inputs + [
      "-filter_complex", ";".join(graph), "-map", "[vout]",
      "-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0", "-row-mt", "1",
      "-deadline", "good", "-cpu-used", "4"]
  cmd += (["-map", a_label, "-c:a", "libopus", "-b:a", "72k"] if a_label else ["-an"])
  subprocess.run(cmd + [str(out)], check=True)
  print(f"[annotate] {len(segments)} caption states from {len(renderer.cache)} frames"
      f"{'' if a_label else ', no narration track'}", file=sys.stderr)


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("raw")
  ap.add_argument("cues")
  ap.add_argument("out")
  ap.add_argument("--offset", type=float, default=0.0,
      help="shift every cue against the video clock, in seconds")
  args = ap.parse_args()

  cues = json.loads(Path(args.cues).read_text(encoding="utf-8"))
  if not cues:
    print("[annotate] no cues — nothing to burn in", file=sys.stderr)
    return 1
  with tempfile.TemporaryDirectory() as td:
    build(Path(args.raw), cues, Path(args.out), Path(td), args.offset)
  boxed = sum(1 for c in cues if c.get("box"))
  spoken = sum(1 for c in cues if c.get("audio"))
  print(f"[annotate] {len(cues)} captions, {boxed} spotlights, {spoken} spoken -> {args.out}",
      file=sys.stderr)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
