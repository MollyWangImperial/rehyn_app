"""Build the private, finite Molly voice bundle for the Testing reach flow.

Run locally with a readable Molly reference recording and Chatterbox Nano.
The generated JSON contains voice data and must never be committed to Git.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from backend.chatterbox_nano_tts import synthesize_mp3
from backend.testing_reach_voice_lines import LINES


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_bundle(reference: Path, output: Path, *, max_cues: int | None = None, lines=LINES) -> None:
    if not reference.is_file():
        raise FileNotFoundError("Molly reference recording not found")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required")
    if output.is_file():
        bundle = json.loads(output.read_text(encoding="ascii"))
        if bundle.get("version") != 1 or bundle.get("voice") != "Molly":
            raise ValueError("Existing bundle has the wrong format")
    else:
        bundle = {"version": 1, "voice": "Molly", "entries": {}}
    entries = bundle["entries"]
    for index, line in enumerate(lines, 1):
        if max_cues is not None and index > max_cues:
            break
        key = _key(line)
        if key in entries:
            print(f"{index}/{len(lines)} cached", flush=True)
            continue
        source = synthesize_mp3(line, reference, ffmpeg)
        encoded = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame", "-b:a", "32k",
             "-f", "mp3", "pipe:1"],
            input=source, capture_output=True, check=True,
        ).stdout
        if not 1000 <= len(encoded) <= 200_000:
            raise RuntimeError(f"Cue {index} has an unexpected encoded size")
        entries[key] = base64.b64encode(encoded).decode("ascii")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(bundle, separators=(",", ":")), encoding="ascii")
        temporary.replace(output)
        print(f"{index}/{len(lines)} generated ({len(encoded)} bytes)", flush=True)
    print(f"bundle_bytes={output.stat().st_size} cues={len(entries)}/{len(lines)}", flush=True)
    if max_cues is None and output.stat().st_size >= 1_000_000:
        raise RuntimeError("Bundle exceeds the private Render secret-file limit")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, default=Path(os.environ.get("MOLLY_VOICE_SAMPLE", "")))
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "voice_samples" / "reach-molly-bundle.json")
    parser.add_argument("--max-cues", type=int)
    arguments = parser.parse_args()
    build_bundle(arguments.reference, arguments.output, max_cues=arguments.max_cues)
