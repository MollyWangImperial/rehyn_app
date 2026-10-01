# Alira assessment voice

The hosted assessment uses the same designed ElevenLabs voice as the recovery
companion: `WeMiVLMEQeVXN5PFqfo3`, model `eleven_multilingual_v2`, MP3 44.1 kHz.
Its stability, similarity, style, speaker boost and speed match the companion.

The production entry point defaults to `INSTRUCTION_TTS_PROVIDER=elevenlabs`
and `ELEVENLABS_VOICE_SCOPE=all`. Explicit environment values take precedence.
The 99 shipped recordings cover every authored assessment task instruction,
calibration instruction, task celebration and other fixed runner lines found by
`python -m backend.bake_alira_voice --dry-run --only initial --only assessment`.
They play without an API key or additional ElevenLabs credits.

Speech assembled during a session, such as a corrective hint, requires
`ELEVENLABS_API_KEY` from the ElevenLabs workspace owning this voice. Store it
only as a Render secret. Without a working key/credits, the existing OpenAI
fallback remains available for those unrecorded lines and is never cached as
Alira. `/api/tts/health` probes a shipped assessment line and reports the actual
provider, exact configured voice ID, audio source and fallback status.

Private Molly audio bundles used by Settings → Testing remain separate and
unchanged. No private reference recordings, API keys or patient data are
included in this release.

Validation: 30 focused checks cover voice selection/settings/cache identity,
all 99 MP3 payloads, no-key playback, truthful fallback diagnostics, existing
instruction-only clone restrictions, private Testing bundles and mobile audio
unlock contracts. The separate Chatterbox synthesis suite requires PyTorch,
which is not installed in the release-check Python environment.
