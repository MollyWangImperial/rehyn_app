"""With ELEVENLABS_VOICE_SCOPE=all, one ElevenLabs voice speaks every line as Alira."""

import asyncio
import base64
import os
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "axonai_alira_voice_test")

from backend import server


def enable_alira_voice(monkeypatch, tmp_path, api_key="test-key"):
    monkeypatch.setattr(server, "INSTRUCTION_TTS_PROVIDER", "elevenlabs")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_SCOPE", "all")
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", api_key)
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "alira-voice-id")
    monkeypatch.setattr(server, "ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2")
    monkeypatch.setattr(server, "ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
    monkeypatch.setattr(server, "TTS_CACHE_DIR", tmp_path / "tts")
    monkeypatch.setattr(server, "PREPARED_TTS_DIR", tmp_path / "prepared")
    monkeypatch.setattr(server, "_alira_live_voice_retry_at", 0.0)
    (tmp_path / "prepared").mkdir()
    server._tts_memory_cache.clear()
    server._tts_inflight.clear()


def fake_openai(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(read=lambda: b"general-voice-audio")

    monkeypatch.setattr(server, "openai_tts_client", SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create))))
    return calls


def test_the_same_voice_and_delivery_speak_instructions_and_general_lines(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path)
    requests = []

    def fake_post(url, **kwargs):
        requests.append((url, kwargs))
        return SimpleNamespace(content=b"ID3-alira-audio", raise_for_status=lambda: None)

    monkeypatch.setattr(server.httpx, "post", fake_post)
    instruction = server._tts_request_config("instruction", "nova")
    general = server._tts_request_config("general", "nova")
    assert instruction == general
    assert instruction["provider"] == "elevenlabs"
    assert instruction["voice"] == "alira-voice-id"
    assert instruction["public_voice"] == "alira"
    # One line is one recording, whichever part of the app asks for it.
    assert server._tts_cache_key("Wonderful. Here we go.", "nova", "instruction") == server._tts_cache_key(
        "Wonderful. Here we go.", "nova", "general"
    )

    assert server._synthesize_tts_audio_bytes("Lovely work.", "nova", "general") == b"ID3-alira-audio"
    assert server._synthesize_tts_audio_bytes("Hold still.", "nova", "instruction") == b"ID3-alira-audio"
    assert all(url.endswith("/alira-voice-id") for url, _ in requests)
    assert [kwargs["json"]["voice_settings"] for _, kwargs in requests] == [server.ALIRA_VOICE_SETTINGS] * 2


def test_the_default_scope_keeps_a_clone_to_instructions(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_SCOPE", "instruction")
    monkeypatch.setattr(server, "openai_tts_client", object())
    assert not server._alira_voice_everywhere()
    assert server._tts_request_config("instruction", "nova")["public_voice"] == "custom-cloned-voice"
    assert server._tts_request_config("general", "nova")["provider"] == "openai-direct"


def test_delivery_settings_are_part_of_the_recording_identity(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path)
    before = server._tts_cache_key("Hold still.", "nova", "instruction")
    monkeypatch.setattr(server, "ALIRA_VOICE_SETTINGS", {**server.ALIRA_VOICE_SETTINGS, "speed": 1.0})
    assert server._tts_cache_key("Hold still.", "nova", "instruction") != before


def test_a_recorded_line_plays_without_a_key_or_a_provider_call(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path, api_key="")
    calls = fake_openai(monkeypatch)

    def refuse(*args, **kwargs):
        raise AssertionError("a recorded line must not reach ElevenLabs")

    monkeypatch.setattr(server.httpx, "post", refuse)
    line = "Hold your hand steadily at the forward target for a moment."
    key = server._tts_cache_key(line, "nova", "instruction")
    (tmp_path / "prepared" / f"{key}.mp3").write_bytes(b"ID3-recorded-alira")

    audio = asyncio.run(server._generate_tts_audio_base64(line, "nova", "instruction"))
    assert base64.b64decode(audio) == b"ID3-recorded-alira"
    assert asyncio.run(server._generate_tts_audio_base64(line, "nova", "general")) == audio
    assert calls == []


def test_alira_is_not_replaced_when_credits_run_out(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path)
    calls = fake_openai(monkeypatch)
    attempts = []

    def out_of_credits(url, **kwargs):
        attempts.append(url)
        request = httpx.Request("POST", url)
        raise httpx.HTTPStatusError("quota", request=request, response=httpx.Response(401, request=request))

    monkeypatch.setattr(server.httpx, "post", out_of_credits)
    with pytest.raises(server.HTTPException) as failure:
        asyncio.run(server._generate_tts_audio_base64("Try a slightly smaller movement.", "nova", "general"))
    assert failure.value.status_code == 503
    assert not list((tmp_path / "tts").glob("*.b64"))
    assert not server._tts_memory_cache
    with pytest.raises(server.HTTPException):
        asyncio.run(server._generate_tts_audio_base64("Nicely done.", "nova", "general"))
    assert len(attempts) == 1 and calls == []
    # Once the pause is over Alira's own voice is tried again, and kept.
    monkeypatch.setattr(server, "_alira_live_voice_retry_at", 0.0)
    monkeypatch.setattr(
        server.httpx, "post", lambda url, **kwargs: SimpleNamespace(content=b"ID3-alira-audio", raise_for_status=lambda: None)
    )
    again = asyncio.run(server._generate_tts_audio_base64("Nicely done.", "nova", "general"))
    assert base64.b64decode(again) == b"ID3-alira-audio"
    assert list((tmp_path / "tts").glob("*.b64"))


def test_without_any_voice_the_provider_error_is_reported(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path, api_key="")
    monkeypatch.setattr(server, "openai_tts_client", None)
    monkeypatch.setattr(server, "tts_client", None)
    with pytest.raises(server.HTTPException):
        asyncio.run(server._generate_tts_audio_base64("An unrecorded line.", "nova", "general"))


def test_alira_may_speak_any_line_because_she_is_not_a_cloned_person(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path)
    spoken = []

    async def fake_generate(text, voice, purpose="general"):
        spoken.append((text, purpose))
        return "bXAz"

    monkeypatch.setattr(server, "_generate_tts_audio_base64", fake_generate)
    reply = "That sounds like a tiring day. Shall we keep today's session short?"
    result = asyncio.run(server.generate_tts(server.TTSRequest(text=reply, purpose="instruction")))
    assert result.audio_b64 == "bXAz" and spoken == [(reply, "instruction")]


def test_render_is_configured_for_one_alira_voice():
    render = (server.ROOT_DIR.parent / "render.yaml").read_text(encoding="utf-8")
    assert "ELEVENLABS_VOICE_SCOPE" in render
    scope = render.split("ELEVENLABS_VOICE_SCOPE", 1)[1].splitlines()[1]
    assert scope.strip() == "value: all"


def test_every_authored_assessment_line_uses_the_shipped_alira_recording(monkeypatch, tmp_path):
    from backend.bake_alira_voice import line_groups

    prepared = server.ROOT_DIR.parent / "frontend/public/audio/prepared"
    enable_alira_voice(monkeypatch, tmp_path, api_key="")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", server.ALIRA_ELEVENLABS_VOICE_ID)
    monkeypatch.setattr(server, "PREPARED_TTS_DIR", prepared)

    def refuse(*args, **kwargs):
        raise AssertionError("Assessment instructions must use the shipped Alira pack")

    monkeypatch.setattr(server, "_synthesize_tts_audio_bytes", refuse)
    monkeypatch.setattr(server, "_synthesize_openai_tts_audio_bytes", refuse)
    checked = 0
    for group, lines in line_groups():
        if group != "initial" and not group.startswith("assessment:"):
            continue
        for line in lines:
            key = server._tts_cache_key(line, "nova", "instruction")
            expected = (prepared / f"{key}.mp3").read_bytes()
            assert len(expected) > 1000
            actual = asyncio.run(server.generate_tts(server.TTSRequest(text=line, purpose="instruction")))
            assert base64.b64decode(actual.audio_b64) == expected
            assert actual.provider == "elevenlabs" and actual.voice == "alira"
            # General corrections share the same designed voice/cache identity.
            assert asyncio.run(server._generate_tts_audio_base64(line, "nova", "general")) == actual.audio_b64
            checked += 1
    assert checked == sum(len(lines) for group, lines in line_groups()
                          if group == "initial" or group.startswith("assessment:"))
    assert checked > 99  # Includes the current companion ladder and transitions.


def test_health_probes_real_alira_audio_without_a_live_key(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path, api_key="")
    line = "Hold your hand steadily at the forward target for a moment."
    key = server._tts_cache_key(line, "nova", "instruction")
    (tmp_path / "prepared" / f"{key}.mp3").write_bytes(b"ID3-recorded-alira")
    result = asyncio.run(server.tts_health())
    assert result["provider"] == "elevenlabs" and result["voice"] == "alira"
    assert result["audio_source"] == "prepared" and not result["fallback_used"]
    assert not result["alira_live_voice_available"]


def test_health_reports_unavailable_alira_without_trying_another_speaker(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path, api_key="")
    calls = fake_openai(monkeypatch)
    result = asyncio.run(server.tts_health())
    assert not result["ok"] and result["provider"] == "elevenlabs"
    assert calls == []


def test_rate_limit_retries_keep_alira_and_do_not_disable_later_prompts(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path)
    calls = fake_openai(monkeypatch)
    attempts, delays = [], []

    def limited_then_ready(*args, **kwargs):
        attempts.append(kwargs["json"]["text"])
        if len(attempts) == 1:
            request = httpx.Request("POST", "https://api.elevenlabs.io")
            raise httpx.HTTPStatusError("busy", request=request, response=httpx.Response(429, request=request))
        return SimpleNamespace(content=b"ID3-alira-audio", raise_for_status=lambda: None)

    async def no_wait(delay):
        delays.append(delay)

    monkeypatch.setattr(server.httpx, "post", limited_then_ready)
    monkeypatch.setattr(server.asyncio, "sleep", no_wait)
    actual = asyncio.run(server._generate_tts_audio_base64("Let your hand rest back on your lap.", "nova", "instruction"))
    assert base64.b64decode(actual) == b"ID3-alira-audio"
    assert len(attempts) == 2 and delays == [2] and calls == []
    assert server._alira_live_voice_retry_at == 0


def test_prefetch_burst_serializes_distinct_live_lines(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path)
    active, maximum, generated = 0, 0, []
    lock = threading.Lock()

    def generate(text, *args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(.01)
        with lock:
            active -= 1
            generated.append(text)
        return b"ID3-alira-audio"

    monkeypatch.setattr(server, "_synthesize_tts_audio_bytes", generate)

    async def run():
        return await asyncio.gather(*[
            server._generate_tts_audio_base64(f"Alira line {i}.", "nova", "instruction") for i in range(6)
        ])

    results = asyncio.run(run())
    assert maximum == 1 and len(generated) == 6 and len(results) == 6


def test_exhausted_rate_limit_is_retryable_and_never_cached_as_alira(monkeypatch, tmp_path):
    enable_alira_voice(monkeypatch, tmp_path)
    fallback = fake_openai(monkeypatch)
    attempts = []

    def busy(*args, **kwargs):
        attempts.append(True)
        request = httpx.Request("POST", "https://api.elevenlabs.io")
        raise httpx.HTTPStatusError("busy", request=request, response=httpx.Response(429, request=request))

    async def no_wait(*args):
        pass

    monkeypatch.setattr(server.httpx, "post", busy)
    monkeypatch.setattr(server.asyncio, "sleep", no_wait)
    with pytest.raises(server.HTTPException) as failure:
        asyncio.run(server.generate_tts(server.TTSRequest(text="A new correction.", purpose="instruction")))
    assert failure.value.status_code == 503
    assert len(attempts) == 3 and fallback == []
    assert server._alira_live_voice_retry_at == 0 and not server._tts_memory_cache


def test_current_lap_opening_and_pinch_cues_are_in_the_recorded_catalog():
    import json
    catalog = json.loads((server.ROOT_DIR / "companion_review_voice_lines.json").read_text())
    assert {
        "Let your hand rest back on your lap.",
        "Keep your palm facing the camera at the same circle. Open your fingers wide and hold.",
        "Keep your palm facing the camera at the same circle. Open your fingers as far as is comfortable and hold.",
        "Touch your thumb to your index finger and hold the pinch.",
    } <= set(catalog["lines"])
