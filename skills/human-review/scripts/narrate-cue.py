#!/usr/bin/env python3
"""Synthesise one narration cue: a .wav, and the time every word starts inside it.

Wraps `tts-cue.swift` (AVSpeechSynthesizer — offline, on this machine, no API key and no
narration text leaving the laptop) and turns its UTF-16 word marks into times against the
caller's own whitespace tokens, which is what the karaoke captions are drawn from.

The synthesizer marks words, not punctuation, so a token like an em dash inherits the mark
before it; runs that share a mark are spread evenly across the gap to the next one, so two
words never pop on the same frame.

Usage:
    narrate-cue.py --text "..." --out cue03.wav [--voice Samantha] [--rate 0.5]
                   [--max-seconds N]

Prints {"duration":…, "voice":…, "words":[{"w":…, "t":…}, …]} on stdout. Exits 3 with
{"error":…} — never a traceback — when the machine cannot synthesise, so callers can go on
without narration.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "tts-cue.swift"
# Compiling takes ~2s and running the source through `swift` pays it every cue, so the binary
# is cached outside the repo and keyed by the source it was built from.
CACHE = Path.home() / "Library" / "Caches" / "human-review"


def binary() -> Path:
  digest = hashlib.sha256(SRC.read_bytes()).hexdigest()[:12]
  exe = CACHE / f"tts-cue-{digest}"
  if exe.is_file():
    return exe
  if not shutil.which("swiftc"):
    raise RuntimeError("swiftc not found — install the Xcode command line tools")
  CACHE.mkdir(parents=True, exist_ok=True)
  for stale in CACHE.glob("tts-cue-*"):
    stale.unlink(missing_ok=True)
  subprocess.run(["swiftc", "-O", str(SRC), "-o", str(exe)], check=True,
      capture_output=True, text=True)
  return exe


def utf16_starts(text: str) -> list[tuple[str, int]]:
  """Each whitespace token with its UTF-16 offset — the unit AVSpeechSynthesizer marks in."""
  out, off = [], 0
  for chunk in text.split(" "):
    if chunk:
      out.append((chunk, off))
    off += len(chunk.encode("utf-16-le")) // 2 + 1
  return out


def align(text: str, marks: list[dict], duration: float) -> list[dict]:
  tokens = utf16_starts(text)
  marks = sorted(marks, key=lambda m: m["location"])
  times: list[float | None] = []
  for _, start in tokens:
    hit = [m["t"] for m in marks if m["location"] <= start]
    times.append(hit[-1] if hit else 0.0)

  # Tokens the synthesizer did not mark separately (punctuation, hyphenated leftovers) share
  # the previous token's time; spread each such run over the gap so they arrive one by one.
  i = 0
  while i < len(times):
    j = i
    while j + 1 < len(times) and times[j + 1] == times[i]:
      j += 1
    if j > i:
      nxt = times[j + 1] if j + 1 < len(times) else duration
      span = max(0.0, (nxt - times[i])) * 0.6
      for k in range(i + 1, j + 1):
        times[k] = times[i] + span * (k - i) / (j - i + 1)
    i = j + 1
  return [{"w": tok, "t": round(t, 3)} for (tok, _), t in zip(tokens, times)]


def synthesize(text: str, out: Path, voice: str, rate: float) -> dict:
  proc = subprocess.run([str(binary()), text, str(out), voice, str(rate)],
      check=True, capture_output=True, text=True)
  payload = json.loads(proc.stdout)
  return {
    "duration": payload["duration"],
    "voice": payload["voice"],
    "words": align(text, payload["marks"], payload["duration"]),
  }


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--text", required=True)
  ap.add_argument("--out", required=True)
  ap.add_argument("--voice", default="Samantha")
  ap.add_argument("--rate", type=float, default=0.5,
      help="AVSpeechUtterance rate, 0..1; 0.5 is the system default")
  ap.add_argument("--max-seconds", type=float, default=0.0,
      help="if the cue would run longer, re-speak it faster (never beyond 1.35x)")
  args = ap.parse_args()

  try:
    result = synthesize(args.text, Path(args.out), args.voice, args.rate)
    if args.max_seconds and result["duration"] > args.max_seconds:
      # Speeding a cue up is the lesser evil against narration bleeding over the next shot,
      # but only up to the point where the voice still sounds like it is explaining something.
      faster = min(args.rate * 1.35, args.rate * result["duration"] / args.max_seconds)
      result = synthesize(args.text, Path(args.out), args.voice, faster)
  except Exception as exc:                                  # noqa: BLE001 — reported, not raised
    detail = getattr(exc, "stderr", "") or str(exc)
    print(json.dumps({"error": detail.strip()[:400]}))
    return 3
  print(json.dumps(result))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
