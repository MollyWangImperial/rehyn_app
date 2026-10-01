"""Record Alira's fixed assessment and exercise lines once, in her one voice.

The clips are written to frontend/public/audio/prepared and committed with the
app. A recorded line then plays without an API key and without spending
ElevenLabs credits again, however often the server restarts. Lines that are not
recorded are still spoken live while credits last (see _alira_live_tts_audio_base64).

Run from the repository root. The key and the voice come from the environment
(or backend/.env); neither is written to the manifest.

    $env:ELEVENLABS_API_KEY = "..."
    $env:ELEVENLABS_VOICE_ID = "..."          # the same voice ID as the recovery companion
    python -m backend.bake_alira_voice --dry-run
    python -m backend.bake_alira_voice --only initial
    python -m backend.bake_alira_voice --max-credits 3000

Lines are recorded in the order a patient meets them: the initial assessment,
the other assessment packages, then each exercise. Run it again whenever the
wording, the voice or ALIRA_VOICE_SETTINGS changes; finished clips are skipped.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Any, Iterable

import httpx


os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rehyn_audio_build")
os.environ["INSTRUCTION_TTS_PROVIDER"] = "elevenlabs"
os.environ["ELEVENLABS_VOICE_SCOPE"] = "all"

from backend import server  # noqa: E402


MANIFEST_PATH = server.PREPARED_TTS_DIR / "alira-voice-manifest.json"
VOICE_FIELDS = {"voice", "setup_voice", "calibration_instruction", "coaching_cue"}
ELEVENLABS_API = "https://api.elevenlabs.io"


def _collect(value: Any, field: str = "", found: list[str] | None = None) -> list[str]:
    found = [] if found is None else found
    if isinstance(value, dict):
        for key, nested in value.items():
            _collect(nested, str(key), found)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _collect(nested, field, found)
    elif isinstance(value, str) and field in VOICE_FIELDS and value.strip():
        found.append(value.strip())
    return found


def _runner_template_lines(template: str) -> list[str]:
    """Fixed sentences the runner page speaks itself (not from a task definition)."""
    literal = r'"((?:[^"\\]|\\.)*)"'
    lines: list[str] = []
    for match in re.finditer(r"\bplayVoice\(", template):
        depth, index = 1, match.end()
        while index < len(template) and depth:
            depth += {"(": 1, ")": -1}.get(template[index], 0)
            index += 1
        lines += re.findall(literal, template[match.end():index])
    for match in re.finditer(r"const [A-Z0-9_]*(?:INSTRUCTION|VOICE)[A-Z0-9_]* = " + literal, template):
        lines.append(match.group(1))
    for match in re.finditer(r"const [A-Z0-9_]*VOICES = \[(.*?)\];", template, re.S):
        lines += re.findall(literal, match.group(1))
    spoken = []
    for line in lines:
        try:
            text = json.loads(f'"{line}"')
        except ValueError:
            continue
        # Skip purposes ("general"), identifiers, and fragments joined to a dynamic sentence.
        if text != text.strip():
            continue
        if len(text) >= 12 and " " in text and "${" not in text:
            spoken.append(text)
    return spoken


def line_groups() -> list[tuple[str, list[str]]]:
    groups: list[tuple[str, list[str]]] = []
    packages = server.ASSESSMENT_PACKAGES
    groups.append(("initial", _collect(packages["initial"]) + _runner_template_lines(server.POSE_RUNNER_HTML)))
    for package_id, package in packages.items():
        if package_id != "initial":
            groups.append((f"assessment:{package_id}", _collect(package)))
    groups.append((
        "exercise:shared",
        [
            server.EXERCISE_POSTURE_CHANGED_VOICE,
            server.EXERCISE_TRANSITION_VOICE,
            server.EXERCISE_ASSISTANCE_QUESTION_VOICE,
            server.EXERCISE_ASSISTED_COMPLETE_VOICE,
            server.EXERCISE_INDEPENDENT_COMPLETE_VOICE,
            *_runner_template_lines(server.REHAB_RUNNER_HTML_TEMPLATE),
        ],
    ))
    for exercise_id, base in server.REHAB_RUNNER_CONFIG.items():
        lines = _collect(base)
        for difficulty in ("easy", "medium", "difficult"):
            for variation in ("standard", "alternate"):
                lines += _collect(server._configure_rehab_runner(exercise_id, difficulty, variation))
        groups.append((f"exercise:{exercise_id}", lines))
    seen: set[str] = set()
    unique_groups = []
    for name, lines in groups:
        unique = [line for line in dict.fromkeys(lines) if line not in seen]
        seen.update(unique)
        unique_groups.append((name, unique))
    return unique_groups


def _selected(groups: Iterable[tuple[str, list[str]]], only: list[str]) -> list[tuple[str, list[str]]]:
    if not only:
        return list(groups)
    return [(name, lines) for name, lines in groups if any(name == want or name.startswith(want + ":") or name.endswith(":" + want) for want in only)]


def _elevenlabs(path: str) -> Any:
    response = httpx.get(ELEVENLABS_API + path, headers={"xi-api-key": server.ELEVENLABS_API_KEY}, timeout=60)
    response.raise_for_status()
    return response.json()


def _write_manifest(groups: list[tuple[str, list[str]]], config: dict[str, str]) -> None:
    assets = []
    for name, lines in groups:
        for text in lines:
            key = server._tts_cache_key(text, server.TTS_VOICE, purpose="instruction")
            path = server.PREPARED_TTS_DIR / f"{key}.mp3"
            if path.is_file():
                assets.append({"group": name, "key": key, "url": f"/audio/prepared/{key}.mp3", "bytes": path.stat().st_size, "text": text})
    manifest = {
        "version": 1,
        "note": "Generated by `python -m backend.bake_alira_voice`. The server finds clips by key; this list is for people.",
        "provider": config["provider"],
        "model": config["model"],
        "voice": config["public_voice"],
        "voice_settings": server.ALIRA_VOICE_SETTINGS,
        "ready": len(assets),
        "total": sum(len(lines) for _, lines in groups),
        "assets": assets,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report what is ready and what is missing; record nothing")
    parser.add_argument("--only", action="append", default=[], help="a group to record, e.g. initial, assessment, exercise, ex_reach (repeatable)")
    parser.add_argument("--max-credits", type=float, default=math.inf, help="spend at most this many ElevenLabs credits in this run")
    args = parser.parse_args()

    if not server.ELEVENLABS_VOICE_ID:
        print("Set ELEVENLABS_VOICE_ID to Alira's voice first.", file=sys.stderr)
        return 1
    config = server._tts_request_config("instruction", server.TTS_VOICE)
    all_groups = line_groups()
    groups = _selected(all_groups, args.only)
    if not groups:
        print("No group matches --only. Groups: " + ", ".join(name for name, _ in all_groups), file=sys.stderr)
        return 1

    missing: list[tuple[str, str, str]] = []
    for name, lines in groups:
        keys = [(text, server._tts_cache_key(text, server.TTS_VOICE, purpose="instruction")) for text in lines]
        absent = [(name, text, key) for text, key in keys if not (server.PREPARED_TTS_DIR / f"{key}.mp3").is_file()]
        missing += absent
        print(f"{name:32} {len(lines) - len(absent):3} of {len(lines):3} lines ready, {sum(len(text) for _, text, _ in absent):6} characters missing")
    missing_characters = sum(len(text) for _, text, _ in missing)
    print(f"Alira's voice: {config['model']}. {len(missing)} lines missing ({missing_characters} characters).")
    if args.dry_run or not missing:
        return 0
    if not server.ELEVENLABS_API_KEY:
        print("Set ELEVENLABS_API_KEY before recording new lines.", file=sys.stderr)
        return 1

    subscription = _elevenlabs("/v1/user/subscription")
    rate = next(
        (float((model.get("model_rates") or {}).get("character_cost_multiplier") or 1) for model in _elevenlabs("/v1/models") if model.get("model_id") == config["model"]),
        1.0,
    )
    available = int(subscription["character_limit"]) - int(subscription["character_count"])
    budget = min(available, args.max_credits)
    print(f"ElevenLabs has {available} credits left this month; recording everything missing needs about {math.ceil(missing_characters * rate)}.")

    server.PREPARED_TTS_DIR.mkdir(parents=True, exist_ok=True)
    recorded = skipped = 0
    for name, text, key in missing:
        cost = math.ceil(len(text) * rate)
        if cost > budget:
            skipped += 1
            continue
        audio = server._synthesize_tts_audio_bytes(text, server.TTS_VOICE, "instruction")
        if len(audio) < 1000:
            raise RuntimeError(f"ElevenLabs returned an unexpectedly small clip for a line in {name}")
        destination = server.PREPARED_TTS_DIR / f"{key}.mp3"
        temporary = destination.with_suffix(".mp3.part")
        temporary.write_bytes(audio)
        temporary.replace(destination)
        budget -= cost
        recorded += 1
        print(f"  recorded [{name}] {text[:70]}")
    _write_manifest(all_groups, config)
    print(f"Recorded {recorded} lines." + (f" {skipped} lines are still missing because the credits ran out; run this again after they renew." if skipped else " Every selected line is ready."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
