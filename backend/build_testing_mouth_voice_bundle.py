"""Generate the private T3 Molly bundle with the existing local clone."""
import argparse
from pathlib import Path
from backend.build_testing_reach_voice_bundle import build_bundle
from backend.testing_mouth_voice_lines import LINES

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "voice_samples" / "mouth-molly-bundle.json")
    args = parser.parse_args()
    build_bundle(args.reference, args.output, lines=LINES)
