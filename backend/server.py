from fastapi import FastAPI, APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorGridFSBucket
from pymongo.errors import ConnectionFailure
from pymongo.write_concern import WriteConcern
from pymongo import ReturnDocument
from bson import ObjectId
from bson.errors import InvalidId
import os
from backend.local_assessment_preview import is_local_assessment_preview
import io
import base64
import logging
from pathlib import Path
from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import List, Dict, Any, Literal, Optional, Sequence, Tuple
import uuid
import re
import asyncio
import httpx
import json
import hashlib
import inspect
import time
from collections import OrderedDict
from functools import lru_cache
import hmac
import secrets
import sys
import tempfile
import weakref
from datetime import datetime, timedelta, timezone
from math import isfinite

import certifi

try:
    from backend.rehab_assessment import (
        build_biomechanical_estimates,
        build_clinician_measure_summary,
        build_domain_assessments,
        modeling_specification,
    )
    from backend.clinical_measurement_form import build_clinical_measurement_form
    from backend.rehab_goals import build_rehab_goals
    from backend.rehab_packages import (
        BALANCE_TASKS_DATA,
        LOWER_LIMB_TASKS_DATA,
        attach_hand_failure_phenotypes,
    )
    from backend.muscle_diagnosis import build_muscle_activation_diagnosis
    from backend.assessment_fusion import build_analysis_pipeline, build_clinical_review_gate, build_survey_consistency, survey_domain_context
    from backend.biomechanics_pipeline import (
        aggregate_model_outputs,
        build_model_analysis_manifest,
        model_activation_report,
        patient_body_function_summary,
        patient_collection_summary,
        pending_model_activation_report,
        validate_model_outputs,
    )
    from backend.object_storage import task_video_object_storage
    from backend.patient_insights import build_patient_insights
    from backend.fast_screening import FAST_RUNNER_HTML, evaluate_fast_screen
    from backend.encouragement import MEDALS as REWARD_MEDALS, compute_rewards
    from backend.assessment_quality import VERSION as ASSESSMENT_QUALITY_VERSION, COMPENSATIONS as ASSESSMENT_COMPENSATIONS, build_rubrics, score_assessment, testing_task_report
    from backend.daily_activity_metrics import build_daily_activity_metrics
    from backend.gait_scoring import score_gait_features
    from backend.alira_care_orchestrator import (
        QUESTION_BANK as ALIRA_CARE_QUESTION_BANK,
        FUNCTIONAL_ISSUE_CATALOG,
        MOVEMENT_READINESS_VERSION,
        SURVEY_PREFACE,
        approved_functional_issue_categories,
        approved_question_ids,
        build_adaptive_care_plan,
        evaluate_survey_safety,
        initial_assessment_recommendation,
        survey_functional_problems,
        validate_check_in_answers,
    )
    from backend.alira_action_log import AliraActionLogger
    from backend.account_reset import AccountResetRequest, reset_patient_account
    from backend.early_access import create_early_access_router
    from backend.trial_access import create_trial_access_router
    from backend.rehab_games import game_catalog, rehab_game_html
except ImportError:
    from rehab_assessment import (
        build_biomechanical_estimates,
        build_clinician_measure_summary,
        build_domain_assessments,
        modeling_specification,
    )
    from clinical_measurement_form import build_clinical_measurement_form
    from rehab_goals import build_rehab_goals
    from rehab_packages import (
        BALANCE_TASKS_DATA,
        LOWER_LIMB_TASKS_DATA,
        attach_hand_failure_phenotypes,
    )
    from muscle_diagnosis import build_muscle_activation_diagnosis
    from assessment_fusion import build_analysis_pipeline, build_clinical_review_gate, build_survey_consistency, survey_domain_context
    from biomechanics_pipeline import (
        aggregate_model_outputs,
        build_model_analysis_manifest,
        model_activation_report,
        patient_body_function_summary,
        patient_collection_summary,
        pending_model_activation_report,
        validate_model_outputs,
    )
    from object_storage import task_video_object_storage
    from patient_insights import build_patient_insights
    from fast_screening import FAST_RUNNER_HTML, evaluate_fast_screen
    from encouragement import MEDALS as REWARD_MEDALS, compute_rewards
    from assessment_quality import VERSION as ASSESSMENT_QUALITY_VERSION, COMPENSATIONS as ASSESSMENT_COMPENSATIONS, build_rubrics, score_assessment, testing_task_report
    from daily_activity_metrics import build_daily_activity_metrics
    from gait_scoring import score_gait_features
    from alira_care_orchestrator import (
        QUESTION_BANK as ALIRA_CARE_QUESTION_BANK,
        FUNCTIONAL_ISSUE_CATALOG,
        MOVEMENT_READINESS_VERSION,
        SURVEY_PREFACE,
        approved_functional_issue_categories,
        approved_question_ids,
        build_adaptive_care_plan,
        evaluate_survey_safety,
        initial_assessment_recommendation,
        survey_functional_problems,
        validate_check_in_answers,
    )
    from alira_action_log import AliraActionLogger
    from account_reset import AccountResetRequest, reset_patient_account
    from early_access import create_early_access_router
    from trial_access import create_trial_access_router
    from rehab_games import game_catalog, rehab_game_html

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    from emergentintegrations.llm.openai.text_to_speech import OpenAITextToSpeech
except Exception:
    OpenAITextToSpeech = None

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env", override=False)
logger = logging.getLogger(__name__)
REHYN_TRIAL_ACCESS_CODE = os.environ.get("REHYN_TRIAL_ACCESS_CODE", "").strip()
# Trial access is required unless an explicit development override disables it.
TRIAL_ACCESS_CHECK_ENABLED = bool(os.environ.get("RENDER")) or os.environ.get(
    "REHYN_ENFORCE_TRIAL_CODE", "1"
).strip().lower() not in {"0", "false", "no"}


def _require_trial_access_code(candidate: Optional[str]) -> None:
    if not TRIAL_ACCESS_CHECK_ENABLED:
        return
    supplied = str(candidate or "").strip()
    if not REHYN_TRIAL_ACCESS_CODE or not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"), REHYN_TRIAL_ACCESS_CODE.encode("utf-8")
    ):
        raise HTTPException(status_code=403, detail="The trial code is not valid.")

# MongoDB
mongo_url = os.environ["MONGO_URL"]


def _mongo_timeout_ms(env_name: str, default_ms: int) -> int:
    raw = os.environ.get(env_name, "").strip()
    try:
        value = int(raw) if raw else default_ms
    except ValueError:
        value = default_ms
    return max(250, value)


# Hosted deployments (Render + MongoDB Atlas) routinely need more than one
# second to resolve the SRV record and finish the TLS handshake after a cold
# start. With a 1s server-selection timeout those first requests silently
# "failed over" to the local JSON fallback, so Terms acceptance and survey
# answers were written to a file that disappears on the next deploy - and the
# patient was asked to accept the Terms and repeat the survey. Local development
# against localhost keeps the short timeout so phone testing without Docker or
# Mongo stays responsive. Both values can be tuned per environment.
_MONGO_IS_LOCAL = any(host in mongo_url for host in ("localhost", "127.0.0.1", "host.docker.internal"))
MONGO_SERVER_SELECTION_TIMEOUT_MS = _mongo_timeout_ms(
    "MONGO_SERVER_SELECTION_TIMEOUT_MS", 1000 if _MONGO_IS_LOCAL else 5000
)
MONGO_CONNECT_TIMEOUT_MS = _mongo_timeout_ms("MONGO_CONNECT_TIMEOUT_MS", 1000 if _MONGO_IS_LOCAL else 5000)
_MONGO_USES_TLS = mongo_url.startswith("mongodb+srv://") or bool(
    re.search(r"(?:[?&](?:tls|ssl)=true)(?:&|$)", mongo_url, flags=re.IGNORECASE)
)
_mongo_client_options: Dict[str, Any] = {
    "serverSelectionTimeoutMS": MONGO_SERVER_SELECTION_TIMEOUT_MS,
    "connectTimeoutMS": MONGO_CONNECT_TIMEOUT_MS,
    "socketTimeoutMS": _mongo_timeout_ms("MONGO_SOCKET_TIMEOUT_MS", 10000),
    "waitQueueTimeoutMS": 5000,
    "retryReads": True,
    "retryWrites": True,
    "maxPoolSize": 30,
    "minPoolSize": 0 if _MONGO_IS_LOCAL else 1,
    "maxIdleTimeMS": 45000,
    "heartbeatFrequencyMS": 10000,
}
if _MONGO_USES_TLS:
    # The slim production image does not guarantee an up-to-date system CA
    # bundle. Use the pinned certifi bundle explicitly for Atlas TLS.
    _mongo_client_options["tlsCAFile"] = certifi.where()
client = AsyncIOMotorClient(mongo_url, **_mongo_client_options)
_mongo_database = client.get_database(
    os.environ["DB_NAME"],
    write_concern=WriteConcern(w="majority", wtimeout=10000),
)

# Local JSON is useful for development without MongoDB. It is not durable on a
# hosted Render service and must never be presented as saved patient progress.
ALLOW_EPHEMERAL_PATIENT_STATE = _MONGO_IS_LOCAL and not os.environ.get("RENDER") and os.environ.get(
    "ALLOW_EPHEMERAL_PATIENT_STATE", "1"
).strip().lower() not in {"0", "false", "no"}


def _require_durable_patient_store(context: str, error: BaseException) -> None:
    if ALLOW_EPHEMERAL_PATIENT_STATE:
        return
    logger.error("Durable patient store unavailable during %s: %s", context, str(error)[:200])
    raise HTTPException(
        status_code=503,
        detail=(
            "Your saved Rehyn account is temporarily unavailable. "
            "Nothing has been reset or marked incomplete. Please try again shortly."
        ),
        headers={"Retry-After": "5"},
    ) from error


# When MongoDB is unreachable (cluster paused, IP access list, network), every
# call used to wait for the full server-selection timeout before falling back
# to the local store - a sign-in makes several such calls in a row, so it took
# 20-30 s and the app's 15 s sign-in limit gave up first. The circuit breaker
# below remembers the first failure: for the next MONGO_CIRCUIT_COOLDOWN_S the
# database raises immediately. A background probe bypasses the cooldown and
# restores access as soon as the authenticated database connection recovers.
class MongoUnavailableError(ConnectionFailure):
    """Raised at once while the circuit breaker is open."""


class _MongoCircuitBreaker:
    def __init__(self, cooldown_s: float) -> None:
        self.cooldown_s = max(1.0, float(cooldown_s))
        self.down_until = 0.0
        self.last_error = ""
        self.tripped_at: Optional[str] = None
        self.last_success_at: Optional[str] = None

    def is_open(self) -> bool:
        return time.monotonic() < self.down_until

    def retry_in_s(self) -> float:
        return max(0.0, self.down_until - time.monotonic())

    def check(self) -> None:
        if self.is_open():
            raise MongoUnavailableError(
                f"MongoDB unreachable; retrying in {self.retry_in_s():.0f}s ({self.last_error or 'server selection timed out'})"
            )

    def trip(self, error: BaseException) -> None:
        self.down_until = time.monotonic() + self.cooldown_s
        self.last_error = str(error)[:200]
        self.tripped_at = datetime.now(timezone.utc).isoformat()
        logger.warning(f"MongoDB unreachable; failing fast for {self.cooldown_s:.0f}s: {self.last_error}")

    def clear(self) -> None:
        self.down_until = 0.0
        self.last_error = ""
        self.tripped_at = None
        self.last_success_at = datetime.now(timezone.utc).isoformat()

    def status(self) -> Dict[str, Any]:
        return {
            "state": "cooldown" if self.is_open() else "closed",
            "retry_in_s": round(self.retry_in_s(), 1),
            "last_error": self.last_error or None,
            "tripped_at": self.tripped_at,
            "cooldown_s": self.cooldown_s,
            "last_success_at": self.last_success_at,
        }


MONGO_CIRCUIT = _MongoCircuitBreaker(_mongo_timeout_ms("MONGO_CIRCUIT_COOLDOWN_MS", 30000) / 1000.0)
_MONGO_CURSOR_METHODS = {"find", "aggregate", "list_indexes"}


class _GuardedCursor:
    """A Motor cursor whose awaited results trip the breaker on connection loss."""

    def __init__(self, cursor: Any, breaker: _MongoCircuitBreaker) -> None:
        self._cursor = cursor
        self._breaker = breaker

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._cursor, name)
        if not callable(attr):
            return attr
        if name in {"sort", "limit", "skip", "batch_size", "hint", "max_time_ms", "collation", "allow_disk_use"}:
            def chained(*args: Any, **kwargs: Any) -> "_GuardedCursor":
                attr(*args, **kwargs)
                return self
            return chained

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            self._breaker.check()
            try:
                result = attr(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except ConnectionFailure as error:
                self._breaker.trip(error)
                raise
            self._breaker.clear()
            return result
        return guarded

    async def __aiter__(self) -> Any:
        self._breaker.check()
        try:
            async for item in self._cursor:
                self._breaker.clear()
                yield item
        except ConnectionFailure as error:
            self._breaker.trip(error)
            raise


class _GuardedCollection:
    def __init__(self, collection: Any, breaker: _MongoCircuitBreaker) -> None:
        self._collection = collection
        self._breaker = breaker

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._collection, name)
        if isinstance(attr, AsyncIOMotorCollection):
            return _GuardedCollection(attr, self._breaker)  # e.g. task_videos.files
        if not callable(attr):
            return attr
        if name in _MONGO_CURSOR_METHODS:
            def cursor_method(*args: Any, **kwargs: Any) -> _GuardedCursor:
                self._breaker.check()
                return _GuardedCursor(attr(*args, **kwargs), self._breaker)
            return cursor_method

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            self._breaker.check()
            try:
                result = attr(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except ConnectionFailure as error:
                self._breaker.trip(error)
                raise
            self._breaker.clear()
            return result
        return guarded


class _GuardedDatabase:
    def __init__(self, database: Any, breaker: _MongoCircuitBreaker) -> None:
        self._database = database
        self._breaker = breaker

    @property
    def raw(self) -> Any:
        return self._database

    def __getattr__(self, name: str) -> Any:
        return _GuardedCollection(getattr(self._database, name), self._breaker)

    def __getitem__(self, name: str) -> Any:
        return _GuardedCollection(self._database[name], self._breaker)


db = _GuardedDatabase(_mongo_database, MONGO_CIRCUIT)
task_video_bucket = AsyncIOMotorGridFSBucket(_mongo_database, bucket_name="task_videos")
TASK_VIDEO_MAX_BYTES = 35 * 1024 * 1024
TASK_VIDEO_EXTENSIONS = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/x-m4v": "m4v",
    "video/webm": "webm",
    "video/x-msvideo": "avi",
    "video/mpeg": "mpeg",
    "video/x-matroska": "mkv",
    "video/3gpp": "3gp",
}
TASK_VIDEO_FALLBACK_DIR = ROOT_DIR / ".task_videos"
LOCAL_STATE_DIR = ROOT_DIR / ".local_state"
LOCAL_USERS_FILE = LOCAL_STATE_DIR / "users.json"
LOCAL_TASK_PROGRESS_FILE = LOCAL_STATE_DIR / "task_progress.json"
LOCAL_CARE_STATE_FILE = LOCAL_STATE_DIR / "alira_care_state.json"
LOCAL_ASSESSMENTS_FILE = LOCAL_STATE_DIR / "assessments.json"
_configured_action_log_dir = os.environ.get("ALIRA_ACTION_LOG_DIR")
_action_log_dir = (
    _configured_action_log_dir
    or (Path(tempfile.gettempdir()) / "rehyn-pytest-action-logs" / str(os.getpid()) if "pytest" in sys.modules else None)
)
ALIRA_ACTION_LOGGER = AliraActionLogger(_action_log_dir)


def _record_alira_action(
    action: str,
    *,
    source: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    status: str = "completed",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        ALIRA_ACTION_LOGGER.record(
            action,
            source=source,
            user_id=user_id,
            session_id=session_id,
            status=status,
            details=details,
        )
    except Exception as exc:
        # Audit logging must never interrupt patient-facing care flows.
        logger.error("Could not write Alira action log: %s", type(exc).__name__)


def _load_local_dict(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _persist_local_dict(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_local_list(path: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _persist_local_list(path: Path, data: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=True, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)

# Local development fallback: keeps Expo phone testing usable when Docker/Mongo
# is not running. Mongo remains the source of truth whenever it is reachable.
LOCAL_USERS: Dict[str, Dict[str, Any]] = _load_local_dict(LOCAL_USERS_FILE)
LOCAL_TASK_PROGRESS: Dict[str, Dict[str, Any]] = _load_local_dict(LOCAL_TASK_PROGRESS_FILE)
LOCAL_CARE_STATE: Dict[str, Dict[str, Any]] = _load_local_dict(LOCAL_CARE_STATE_FILE)
LOCAL_ASSESSMENTS: List[Dict[str, Any]] = _load_local_list(LOCAL_ASSESSMENTS_FILE)
LOCAL_CHAT_SESSIONS: Dict[str, Dict[str, Any]] = {}
PENDING_ASSESSMENT_SYNC_STATUS = "pending_mongodb_sync"


def _local_assessment(user_id: str, assessment_id: str) -> Optional[Dict[str, Any]]:
    return next((
        item for item in reversed(LOCAL_ASSESSMENTS)
        if item.get("user_id") == user_id and item.get("id") == assessment_id
    ), None)


def _queue_assessment_for_mongodb_sync(
    doc: Dict[str, Any],
    user: Dict[str, Any],
    error: BaseException,
) -> Dict[str, Any]:
    """Keep a completed assessment available while Atlas reconnects."""
    local_doc = dict(doc)
    local_doc["persistence_status"] = PENDING_ASSESSMENT_SYNC_STATUS
    local_doc["pending_mongodb_sync"] = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "attempts": 0,
        "last_error": str(error)[:200],
        "account_generation": user.get("account_generation"),
    }
    LOCAL_ASSESSMENTS[:] = [
        item for item in LOCAL_ASSESSMENTS
        if not (item.get("user_id") == local_doc.get("user_id") and item.get("id") == local_doc.get("id"))
    ]
    LOCAL_ASSESSMENTS.append(local_doc)
    _persist_local_list(LOCAL_ASSESSMENTS_FILE, LOCAL_ASSESSMENTS)
    return local_doc


async def _sync_pending_assessments_to_mongodb() -> int:
    """Flush locally queued assessments after the authenticated Atlas probe succeeds."""
    synced = 0
    changed = False
    for local_doc in LOCAL_ASSESSMENTS:
        if local_doc.get("persistence_status") != PENDING_ASSESSMENT_SYNC_STATUS:
            continue
        pending = dict(local_doc.get("pending_mongodb_sync") or {})
        user_id = str(local_doc.get("user_id") or "")
        try:
            mongo_user = await db.users.find_one({"id": user_id}, {"_id": 0})
            queued_generation = pending.get("account_generation")
            if not mongo_user or (
                queued_generation is not None
                and mongo_user.get("account_generation") != queued_generation
            ):
                local_doc["persistence_status"] = "discarded_after_account_reset"
                local_doc.pop("pending_mongodb_sync", None)
                changed = True
                continue
            mongo_doc = {
                key: value for key, value in local_doc.items()
                if key not in {"persistence_status", "pending_mongodb_sync", "mongodb_synced_at"}
            }
            await db.assessments.replace_one(
                {"id": mongo_doc["id"], "user_id": user_id},
                mongo_doc,
                upsert=True,
            )
            if mongo_doc.get("assessment_trigger") == "initial":
                await db.users.update_one(
                    {"id": user_id, "account_generation": mongo_user.get("account_generation"), "reset_pending": None},
                    {
                        "$min": {"initial_assessment_completed_at": mongo_doc["created_at"]},
                        "$set": {"initial_assessment_completion_source": "temporary_local_sync"},
                    },
                )
            report_id = mongo_doc.get("functional_issue_report_id")
            if report_id:
                await db.alira_functional_issue_reports.update_one(
                    {"id": report_id, "user_id": user_id, "status": "pending"},
                    {"$set": {
                        "status": "assessed",
                        "assessment_id": mongo_doc["id"],
                        "assessed_at": mongo_doc["created_at"],
                    }},
                )
            local_doc["persistence_status"] = "mongodb"
            local_doc["mongodb_synced_at"] = datetime.now(timezone.utc).isoformat()
            local_doc.pop("pending_mongodb_sync", None)
            synced += 1
            changed = True
        except Exception as error:
            pending["attempts"] = int(pending.get("attempts") or 0) + 1
            pending["last_error"] = str(error)[:200]
            pending["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
            local_doc["pending_mongodb_sync"] = pending
            changed = True
            logger.warning("Pending assessment sync will retry after MongoDB recovers: %s", str(error)[:160])
            break
    if changed:
        _persist_local_list(LOCAL_ASSESSMENTS_FILE, LOCAL_ASSESSMENTS)
    if synced:
        logger.info("Synchronized %s temporarily stored assessment(s) to MongoDB", synced)
    return synced

# The production entry point enables the companion's designed Alira voice.
# Other entry points can still select OpenAI or instruction-only human clones.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "").strip()
TTS_VOICE = os.environ.get("TTS_VOICE", "nova")  # warm/encouraging default
TTS_MODEL = os.environ.get("TTS_MODEL", "tts-1")
INSTRUCTION_TTS_PROVIDER = os.environ.get("INSTRUCTION_TTS_PROVIDER", "openai").strip().lower()
CHATTERBOX_REFERENCE_AUDIO = os.environ.get("CHATTERBOX_REFERENCE_AUDIO", "").strip()
CHATTERBOX_FFMPEG_PATH = os.environ.get("CHATTERBOX_FFMPEG_PATH", "").strip()
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ALIRA_ELEVENLABS_VOICE_ID = "WeMiVLMEQeVXN5PFqfo3"
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "").strip() or ALIRA_ELEVENLABS_VOICE_ID
ELEVENLABS_VOICE_SCOPE = os.environ.get("ELEVENLABS_VOICE_SCOPE", "instruction").strip().lower()
ELEVENLABS_TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2").strip()
ELEVENLABS_OUTPUT_FORMAT = os.environ.get("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128").strip()
STT_MODEL = os.environ.get("STT_MODEL", "gpt-transcribe").strip() or "gpt-transcribe"
ALIRA_CHAT_MODEL = os.environ.get("ALIRA_CHAT_MODEL", "gpt-4o-mini")
ALIRA_REALTIME_MODEL = os.environ.get("ALIRA_REALTIME_MODEL", "gpt-realtime-2.1").strip()
ALIRA_REALTIME_VOICE = os.environ.get("ALIRA_REALTIME_VOICE", "marin").strip()
ALIRA_NAVIGATION_DESTINATIONS = (
    "home",
    "journey",
    "progress",
    "assessment_history",
    "alira_chat",
    "journal_entry",
    "pain_check_in",
    "daily_reflection",
    "initial_assessment",
    "next_assessment",
    "profile",
    "care_facility",
    "settings",
    "survey_questions",
    "privacy_policy",
    "data_permissions",
    "terms_of_use",
    "personal_details",
    "care_circle",
    "account_security",
    "camera_permissions",
    "help_center",
    "contact_support",
    "function_summary",
    "movement_snapshot",
    "movement_map",
    "rehab_plan",
    "caregiver_plan",
    "survey_report",
    "guided_exercise",
    "emergency_fast_check",
    "back",
)
ALIRA_NAVIGATION_TOOL = {
    "type": "function",
    "name": "navigate_app",
    "description": (
        "Navigate the signed-in patient to a safe, patient-facing Rehyn page. Call this whenever the "
        "patient asks to open, show, find, visit, go to, or be taken to an app function; do not merely "
        "describe menu steps. Use progress for progress tracking, assessment_history for prior assessments, "
        "function_summary for the latest function-at-a-glance page, movement_snapshot for the latest result, "
        "movement_map for the interactive anatomy map, rehab_plan or guided_exercise for the current plan, "
        "emergency_fast_check for the guided Face-Arms-Speech emergency screen, journal_entry to write a "
        "recovery note, and back to return to the previous page. This tool only opens "
        "pages. It never changes settings, deletes data, submits forms, or performs clinical actions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "destination": {
                "type": "string",
                "enum": list(ALIRA_NAVIGATION_DESTINATIONS),
                "description": "The single Rehyn destination that best matches the patient's request.",
            },
        },
        "required": ["destination"],
        "additionalProperties": False,
    },
}
ALIRA_RECORD_CHECKIN_TOOL = {
    "type": "function",
    "name": "record_rehab_check_in",
    "description": (
        "Save the patient's answers from a short Alira recovery check-in. Ask only the questions listed in "
        "the current adaptive care plan, one at a time. Every question is optional. Save any answers the patient "
        "chooses to give, and stop without penalty if they do not want to continue. Never infer "
        "an answer from silence, video, or an unrelated statement. The backend validates every value and "
        "returns the next safe survey, assessment, and exercise-plan action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "answers": {
                "type": "object",
                "properties": {
                    "sudden_change": {"type": "string", "enum": ["no", "yes"]},
                    "falls": {"type": "string", "enum": ["no", "near_fall", "fall_no_injury", "fall_with_injury"]},
                    "pain": {"type": "number", "minimum": 0, "maximum": 10},
                    "fatigue": {"type": "string", "enum": ["not_at_all", "a_little", "quite_a_bit", "a_lot"]},
                    "function_change": {"type": "string", "enum": ["much_easier", "a_little_easier", "about_the_same", "a_little_harder", "much_harder"]},
                    "exercise_tolerance": {"type": "string", "enum": ["not_tried", "too_easy", "about_right", "too_hard", "stopped_for_symptoms"]},
                    "goal_activity": {"type": "string", "enum": ["not_tried", "easier", "about_the_same", "harder", "needed_more_help"]},
                    "walking_confidence": {"type": "string", "enum": ["not_applicable", "confident", "a_little_unsure", "very_unsure", "needed_more_help"]},
                    "hand_use": {"type": "string", "enum": ["not_at_all", "a_little", "often", "most_activities"]},
                    "arm_use": {"type": "string", "enum": ["not_tried", "comfortable", "needed_more_effort", "needed_help", "unable"]},
                    "balance_confidence": {"type": "string", "enum": ["not_applicable", "steady", "a_little_unsteady", "very_unsteady", "needed_help"]},
                    "emotional_safety": {"type": "string", "enum": ["no", "thoughts_but_safe", "cannot_keep_safe", "prefer_not_to_say"]},
                },
                "additionalProperties": False,
            },
            "patient_note": {
                "type": "string",
                "description": "Optional short verbatim note from the patient. Do not add a diagnosis or interpretation.",
            },
        },
        "required": ["answers"],
        "additionalProperties": False,
    },
}
ALIRA_REPORT_FUNCTIONAL_ISSUE_TOOL = {
    "type": "function",
    "name": "report_new_functional_issue",
    "description": (
        "Record a genuinely new movement problem reported by the patient, such as new difficulty reaching, "
        "opening the hand, grasping, walking, transferring, or balancing. Use the closest approved category. "
        "The backend prevents duplicate exception assessments and returns a targeted assessment only when the "
        "problem is new. Do not use this for a known problem that is already being monitored."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": list(FUNCTIONAL_ISSUE_CATALOG),
            },
            "description": {
                "type": "string",
                "description": "A short patient-reported description without adding a diagnosis.",
            },
        },
        "required": ["category"],
        "additionalProperties": False,
    },
}
ALIRA_CHAT_RECORD_CHECKIN_TOOL = {
    "type": "function",
    "function": {key: value for key, value in ALIRA_RECORD_CHECKIN_TOOL.items() if key != "type"},
}
ALIRA_CHAT_NAVIGATION_TOOL = {
    "type": "function",
    "function": {key: value for key, value in ALIRA_NAVIGATION_TOOL.items() if key != "type"},
}
ALIRA_CHAT_REPORT_FUNCTIONAL_ISSUE_TOOL = {
    "type": "function",
    "function": {key: value for key, value in ALIRA_REPORT_FUNCTIONAL_ISSUE_TOOL.items() if key != "type"},
}
ANALYSIS_WORKER_TOKEN = os.environ.get("ANALYSIS_WORKER_TOKEN", "").strip()
LOCAL_GPU_WORKER_URL = os.environ.get("LOCAL_GPU_WORKER_URL", "").strip().rstrip("/")
LOCAL_BACKEND_CALLBACK_URL = os.environ.get(
    "LOCAL_BACKEND_CALLBACK_URL", "http://127.0.0.1:8001/api"
).strip().rstrip("/")
ANALYSIS_WORKER_CF_CLIENT_ID = os.environ.get("ANALYSIS_WORKER_CF_CLIENT_ID", "").strip()
ANALYSIS_WORKER_CF_CLIENT_SECRET = os.environ.get("ANALYSIS_WORKER_CF_CLIENT_SECRET", "").strip()
openai_tts_client = OpenAI(api_key=OPENAI_API_KEY) if (OpenAI and OPENAI_API_KEY) else None
tts_client = OpenAITextToSpeech(api_key=EMERGENT_LLM_KEY) if (OpenAITextToSpeech and EMERGENT_LLM_KEY) else None

app = FastAPI()
api_router = APIRouter(prefix="/api")


class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Dict[str, Any]) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            if path.endswith(".wasm"):
                response.headers["Content-Type"] = "application/wasm"
            elif path.endswith(".task"):
                response.headers["Content-Type"] = "application/octet-stream"
        return response


FRONTEND_ROOT_DIR = Path(__file__).resolve().parents[1] / "frontend"
FRONTEND_PUBLIC_DIR = FRONTEND_ROOT_DIR / "public"
FRONTEND_STATIC_DIR = FRONTEND_PUBLIC_DIR if FRONTEND_PUBLIC_DIR.is_dir() else FRONTEND_ROOT_DIR / "dist"
MEDIAPIPE_ASSET_DIR = FRONTEND_STATIC_DIR / "vendor" / "mediapipe"
CAMERA_SETUP_DIR = FRONTEND_STATIC_DIR / "camera-setup"
if CAMERA_SETUP_DIR.is_dir():
    app.mount("/camera-setup", StaticFiles(directory=str(CAMERA_SETUP_DIR), html=True), name="camera-setup")
PREPARED_TTS_DIR = FRONTEND_STATIC_DIR / "audio" / "prepared"
if MEDIAPIPE_ASSET_DIR.is_dir():
    app.mount(
        "/vendor/mediapipe",
        ImmutableStaticFiles(directory=str(MEDIAPIPE_ASSET_DIR)),
        name="mediapipe-assets",
    )
if PREPARED_TTS_DIR.is_dir():
    app.mount(
        "/audio/prepared",
        ImmutableStaticFiles(directory=str(PREPARED_TTS_DIR)),
        name="prepared-tts-assets",
    )


# ============ Models ============
class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StatusCheckCreate(BaseModel):
    client_name: str


class TTSRequest(BaseModel):
    text: str
    voice_id: Optional[str] = None
    purpose: str = Field(default="general", pattern="^(general|instruction)$")


class TTSResponse(BaseModel):
    audio_b64: str
    text: str


class FastCheckSubmit(BaseModel):
    answers: Dict[str, str]
    automated: Dict[str, Any] = Field(default_factory=dict)
    onset_time: Optional[str] = Field(default=None, max_length=40)
    source: str = Field(default="guided_fast", pattern="^guided_fast$")


class TaskStepResult(BaseModel):
    step_id: str
    completed: bool
    failure_code: Optional[str] = None
    duration_ms: int = 0
    metrics: Dict[str, Any] = Field(default_factory=dict)  # e.g., trunk_lean_deg, reach_ratio


class TaskResult(BaseModel):
    task_id: str
    completed_steps: int
    total_steps: int
    duration_ms: int = 0
    steps: List[TaskStepResult] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)


class AssessmentSubmit(BaseModel):
    task_results: List[TaskResult]
    affected_side: str = "right"  # "left" or "right"
    assessment_package: str = "upper_limb"
    assigned_task_ids: Optional[List[str]] = None
    patient_parameters: Dict[str, Any] = Field(default_factory=dict)
    musculoskeletal_outputs: Dict[str, Any] = Field(default_factory=dict)
    motion_data: Dict[str, Any] = Field(default_factory=dict)


class ModelResultSubmit(BaseModel):
    status: str
    per_task: List[Dict[str, Any]]


class GPUStageResultSubmit(BaseModel):
    status: str
    device: str = ""
    gpu_name: str = ""
    torch_version: str = ""
    cuda_runtime: Optional[str] = None
    model_version: str = ""
    stages: List[str] = Field(default_factory=list)
    tasks: Dict[str, Any] = Field(default_factory=dict)
    reporting_boundary: str = ""
    error: Optional[str] = None
    traceback: Optional[str] = None


class GaitStageResultSubmit(BaseModel):
    status: str
    analysis_method: str = ""
    camera_motion_handling: str = ""
    coordinate_frame: str = ""
    quality: Dict[str, Any] = Field(default_factory=dict)
    features: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, Any] = Field(default_factory=dict)
    wham_ready: Optional[bool] = None
    error: Optional[str] = None
    traceback: Optional[str] = None


class Gait2DTestScoreRequest(BaseModel):
    duration_ms: int = Field(ge=100, le=3_600_000)
    evidence: GaitStageResultSubmit


class TaskVideoUploadComplete(BaseModel):
    video_id: str
    object_key: str
    package_id: str
    task_id: str
    duration_ms: int = 0
    content_type: str = "video/mp4"
    size_bytes: int = 0


class MusculoskeletalStageResultSubmit(BaseModel):
    status: str
    per_task: List[Dict[str, Any]] = Field(default_factory=list)
    kinematics: Dict[str, Any] = Field(default_factory=dict)
    solver_summary: Dict[str, Any] = Field(default_factory=dict)
    reporting_boundary: str = ""
    error: Optional[str] = None
    traceback: Optional[str] = None


class FunctionalIssue(BaseModel):
    code: str
    label: str
    description: str
    source: str
    severity: str  # mild | moderate | severe
    related_task: str
    related_step: Optional[str] = None
    phenotype_domain: Optional[str] = None


class RehabExercise(BaseModel):
    id: str
    name: str
    description: str
    sets: int
    reps: int
    frequency: str  # e.g. "Daily"
    targets_issue: str
    source: str
    selection_reason: Optional[str] = None
    safety_note: Optional[str] = None
    linked_goal: Optional[str] = None  # the patient-chosen goal this exercise serves (spec section 8)
    requires_clinician_confirmation: bool = True


class Assessment(BaseModel):
    id: str
    created_at: str
    affected_side: str
    assessment_package: str = "upper_limb"
    testing_shortcut: bool = False
    result_provenance: str = "observed_assessment"
    assigned_task_ids: List[str] = Field(default_factory=list)
    patient_parameters: Dict[str, Any] = Field(default_factory=dict)
    task_results: List[TaskResult]
    functional_issues: List[FunctionalIssue]
    rehab_plan: List[RehabExercise]
    domain_assessments: List[Dict[str, Any]] = Field(default_factory=list)
    clinician_measures: List[Dict[str, Any]] = Field(default_factory=list)
    biomechanical_estimates: List[Dict[str, Any]] = Field(default_factory=list)
    measurement_form: Dict[str, Any] = Field(default_factory=dict)
    rehabilitation_goals: Dict[str, Any] = Field(default_factory=dict)
    muscle_activation_diagnosis: Dict[str, Any] = Field(default_factory=dict)
    survey_consistency: Dict[str, Any] = Field(default_factory=dict)
    analysis_pipeline: Dict[str, Any] = Field(default_factory=dict)
    clinical_review_gate: Dict[str, Any] = Field(default_factory=dict)
    body_function_summary: Dict[str, Any] = Field(default_factory=dict)
    patient_insights: Dict[str, Any] = Field(default_factory=dict)
    movement_snapshot_decision: Dict[str, Any] = Field(default_factory=dict)
    metrics: Dict[str, Any] = Field(default_factory=dict)


# ============ 7 Tasks: Step-by-step with voice prompts & on-screen target zones ============
# Coordinates are normalized [0..1] for the video frame: x left->right, y top->bottom.
# `landmark` references MediaPipe Pose landmarks: 15 = LEFT_WRIST, 16 = RIGHT_WRIST.
# `hold_ms` = required time within target zone to advance.
TASKS_DATA: List[Dict[str, Any]] = [
    {
        "id": "T1",
        "title": "Seated Forward Reach",
        "view": "Side view",
        "focus": "Reach distance, elbow extension, movement quality, trunk compensation",
        "steps": [
            {
                "id": "T1-S1",
                "voice": "Welcome. Sit upright with your affected hand on your lap. Slowly lift that hand forward to the first circle.",
                "target": {"x": 0.64, "y": 0.60, "r": 0.11, "landmark": "WRIST"},
                "hold_ms": 900,
                "caption": "Initiate the forward reach",
                "failure_phenotype": {"code": "REACH_INITIATION_IMPAIRED", "domain": "reach_initiation", "label": "Difficulty initiating a forward reach", "description": "The affected arm did not complete the initial movement away from the lap toward the first reach position.", "severity": "moderate", "source": "Fugl-Meyer UE; task-specific movement observation", "rehab_code": "REACH_INCOMPLETE"},
            },
            {
                "id": "T1-S2",
                "voice": "Now, slowly reach your affected arm forward, as far as you comfortably can, toward the bright circle in front of you. Keep your back straight and stay relaxed.",
                "target": {"x": 0.64, "y": 0.40, "r": 0.10, "landmark": "WRIST"},
                "hold_ms": 1500,
                "caption": "Reach the target, then keep your hand there. Do not lower it until asked.",
                "measure": ["reach_distance", "trunk_lean", "elbow_extension"],
                "failure_phenotype": {"code": "REACH_INCOMPLETE", "domain": "forward_reach_range", "label": "Limited forward reach", "description": "The affected arm initiated the movement but did not reach the forward target.", "severity": "moderate", "source": "Fugl-Meyer UE; ARAT", "rehab_code": "REACH_INCOMPLETE"},
            },
            {
                "id": "T1-S3",
                "voice": "Hold your hand steadily at the forward target for a moment.",
                "target": {"x": 0.64, "y": 0.40, "r": 0.10, "landmark": "WRIST"},
                "hold_ms": 1500,
                "movement_required": False,
                "caption": "Hold the reach steadily",
                "failure_phenotype": {"code": "REACH_ENDPOINT_UNSTABLE", "domain": "reach_endpoint_control", "label": "Unstable control at the reach target", "description": "The affected arm reached the forward area but did not remain stable at the endpoint.", "severity": "mild", "source": "Task-specific movement observation", "rehab_code": "REACH_INCOMPLETE"},
            },
            {
                "id": "T1-S4",
                "voice": "Wonderful effort. Slowly bring your affected hand back to your lap.",
                "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"},
                "hold_ms": 1500,
                "caption": "Return to lap",
                "failure_phenotype": {"code": "REACH_RETURN_CONTROL_IMPAIRED", "domain": "reach_return_control", "label": "Difficulty controlling the return from a reach", "description": "The affected arm did not complete the controlled return from the forward target to the lap.", "severity": "mild", "source": "Task-specific movement observation", "rehab_code": "REACH_INCOMPLETE"},
            },
        ],
    },
    {
        "id": "T2",
        "title": "Shoulder Flexion / Abduction Raise",
        "view": "Front view",
        "focus": "Shoulder elevation, shoulder hike, trunk lean, shoulder–elbow dissociation",
        "steps": [
            {
                "id": "T2-S1",
                "voice": "Next, raise your affected arm from your side toward the first circle at mid height.",
                "target": {"x": 0.5, "y": 0.55, "r": 0.11, "landmark": "WRIST"},
                "hold_ms": 1000,
                "caption": "Initiate the arm raise",
                "failure_phenotype": {"code": "SHOULDER_ELEVATION_INITIATION_IMPAIRED", "domain": "shoulder_elevation_initiation", "label": "Difficulty initiating an arm raise", "description": "The affected arm did not complete the initial antigravity raise from the side.", "severity": "moderate", "source": "Fugl-Meyer UE", "rehab_code": "SHOULDER_FLEX_LIMITED"},
            },
            {
                "id": "T2-S2",
                "voice": "Slowly raise your affected arm forward and upward, reaching toward the target above. Keep your shoulder relaxed and avoid hiking it up.",
                "target": {"x": 0.5, "y": 0.18, "r": 0.10, "landmark": "WRIST"},
                "hold_ms": 1500,
                "caption": "Raise arm upward",
                "measure": ["shoulder_flexion_rom", "shoulder_hike", "trunk_lean"],
                "failure_phenotype": {"code": "SHOULDER_FLEX_LIMITED", "domain": "shoulder_elevation_range", "label": "Limited active arm elevation", "description": "The affected arm initiated the raise but did not reach the upper target.", "severity": "moderate", "source": "Fugl-Meyer UE", "rehab_code": "SHOULDER_FLEX_LIMITED"},
            },
            {
                "id": "T2-S3",
                "voice": "Hold your affected arm steadily at the upper target for a moment.",
                "target": {"x": 0.5, "y": 0.18, "r": 0.10, "landmark": "WRIST"},
                "hold_ms": 1500,
                "movement_required": False,
                "caption": "Hold the raised arm steadily",
                "failure_phenotype": {"code": "SHOULDER_HOLD_UNSTABLE", "domain": "shoulder_hold_stability", "label": "Unstable control while holding the arm raised", "description": "The affected arm reached the upper area but did not remain stable there.", "severity": "mild", "source": "Task-specific movement observation", "rehab_code": "SHOULDER_FLEX_LIMITED"},
            },
            {
                "id": "T2-S4",
                "voice": "Beautiful. Now, gently lower your affected hand back to the same place on your lap.",
                "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"},
                "hold_ms": 1500,
                "caption": "Return hand to the calibrated lap position",
                "failure_phenotype": {"code": "SHOULDER_LOWERING_IMPAIRED", "domain": "shoulder_lowering_control", "label": "Difficulty controlling arm lowering", "description": "The affected arm did not complete the controlled lowering movement back to the lap.", "severity": "mild", "source": "Task-specific movement observation", "rehab_code": "SHOULDER_FLEX_LIMITED"},
            },
        ],
    },
    {
        "id": "T3",
        "title": "Hand to Mouth",
        "view": "Side view",
        "focus": "Hand-to-mouth function, coordination, movement quality, trunk compensation",
        "steps": [
            {
                "id": "T3-S1",
                "voice": "Let's bring your affected hand toward your mouth. First, lift it from your lap toward the circle at your chest.",
                "target": {"x": 0.5, "y": 0.48, "r": 0.11, "landmark": "CHEST"},
                "hold_ms": 1000,
                "caption": "Lift hand from lap toward chest",
                "failure_phenotype": {"code": "HAND_LIFT_INITIATION_IMPAIRED", "domain": "hand_to_mouth_initiation", "label": "Difficulty initiating the hand lift", "description": "The affected hand did not complete the initial lift from the lap toward the chest.", "severity": "moderate", "source": "MESUPES; task-specific movement observation", "rehab_code": "H2M_IMPAIRED"},
            },
            {
                "id": "T3-S2",
                "voice": "Now, slowly bring your hand up to your mouth, as if you were drinking from a cup. Keep your head still.",
                "target": {"x": 0.5, "y": 0.30, "r": 0.10, "landmark": "MOUTH"},
                "hold_ms": 1500,
                "caption": "Hand to mouth",
                "measure": ["elbow_flexion", "trunk_lean", "coordination"],
                "failure_phenotype": {"code": "H2M_IMPAIRED", "domain": "hand_to_mouth_transport", "label": "Difficulty bringing the hand to the mouth", "description": "The affected hand lifted from the lap but did not reach the mouth target.", "severity": "moderate", "source": "Chedoke-McMaster Stroke Assessment; MESUPES", "rehab_code": "H2M_IMPAIRED"},
            },
            {
                "id": "T3-S3",
                "voice": "Hold your affected hand steadily at your mouth for a moment.",
                "target": {"x": 0.5, "y": 0.30, "r": 0.10, "landmark": "MOUTH"},
                "hold_ms": 1500,
                "movement_required": False,
                "caption": "Hold hand steadily at mouth",
                "failure_phenotype": {"code": "HAND_TO_MOUTH_UNSTABLE", "domain": "hand_to_mouth_endpoint_control", "label": "Unstable hand-to-mouth endpoint control", "description": "The affected hand reached the mouth area but did not remain stable there.", "severity": "mild", "source": "Task-specific movement observation", "rehab_code": "H2M_IMPAIRED"},
            },
            {
                "id": "T3-S4",
                "voice": "Great job. Lower your affected hand back to your lap.",
                "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"},
                "hold_ms": 1500,
                "caption": "Return to lap",
                "failure_phenotype": {"code": "HAND_TO_MOUTH_RETURN_IMPAIRED", "domain": "hand_to_mouth_return_control", "label": "Difficulty returning the hand from the mouth", "description": "The affected hand did not complete the controlled return from the mouth to the lap.", "severity": "mild", "source": "Task-specific movement observation", "rehab_code": "H2M_IMPAIRED"},
            },
        ],
    },
    {
        "id": "T4",
        "title": "Grasp Cup and Move to Target",
        "view": "Front view",
        "focus": "Grasp, release, movement quality, endpoint control, trunk compensation",
        "advanced_marker_required": True,
        "advanced_label": "Advanced grasp-release with AxonAI marker",
        "recommended_objects": ["empty plastic cup", "soft ball", "foam cylinder", "small paper box"],
        "steps": [
            {
                "id": "T4-S1",
                "voice": "Reach with your affected hand toward the marked lightweight cup on the table.",
                "target": {"x": 0.30, "y": 0.65, "r": 0.10, "landmark": "WRIST", "icon": "cup"},
                "hold_ms": 1200,
                "caption": "Reach for the cup",
                "failure_phenotype": {"code": "OBJECT_REACH_IMPAIRED", "domain": "object_directed_reach", "label": "Difficulty reaching to an object", "description": "The affected hand did not reach the cup position.", "severity": "moderate", "source": "ARAT grasp subscale", "rehab_code": "GROSS_GRASP"},
            },
            {
                "id": "T4-S2",
                "voice": "Open your hand around the cup, then close your fingers to form a secure grasp.",
                "target": {"x": 0.30, "y": 0.65, "r": 0.11, "landmark": "HAND_CLOSED", "icon": "cup"},
                "hold_ms": 1200,
                "movement_required": False,
                "caption": "Form a grasp around the cup",
                "failure_phenotype": {"code": "GROSS_GRASP", "domain": "gross_grasp_acquisition", "label": "Difficulty forming a gross grasp", "description": "The affected hand reached the cup but did not form a secure grasp around it.", "severity": "moderate", "source": "ARAT grasp subscale", "rehab_code": "GROSS_GRASP"},
            },
            {
                "id": "T4-S3",
                "voice": "Lift the cup slightly and hold it steadily for a moment.",
                "target": {"x": 0.42, "y": 0.55, "r": 0.12, "landmark": "OBJECT_COUPLED", "icon": "cup"},
                "hold_ms": 1500,
                "caption": "Lift and hold the cup",
                "failure_phenotype": {"code": "GRASP_HOLD_UNSTABLE", "domain": "grasp_hold_stability", "label": "Unstable grasp while holding an object", "description": "The cup was grasped but was not lifted and held steadily with the affected hand.", "severity": "moderate", "source": "ARAT grasp subscale", "rehab_code": "GROSS_GRASP"},
            },
            {
                "id": "T4-S4",
                "voice": "Keep holding the cup and carefully move it across to the target on the other side.",
                "target": {"x": 0.70, "y": 0.65, "r": 0.12, "landmark": "OBJECT_COUPLED", "icon": "table"},
                "hold_ms": 1500,
                "caption": "Move cup across to target",
                "measure": ["endpoint_accuracy", "trunk_lean", "movement_smoothness"],
                "failure_phenotype": {"code": "OBJECT_TRANSPORT_IMPAIRED", "domain": "object_transport", "label": "Difficulty transporting a grasped object", "description": "The cup was grasped but was not transported to the opposite target while remaining coupled to the affected hand.", "severity": "moderate", "source": "ARAT grasp and grip subscales", "rehab_code": "GROSS_GRASP"},
            },
            {
                "id": "T4-S5",
                "voice": "Place the cup steadily inside the target area.",
                "target": {"x": 0.70, "y": 0.65, "r": 0.12, "landmark": "OBJECT_AT_TARGET", "icon": "table"},
                "hold_ms": 1000,
                "movement_required": False,
                "caption": "Place cup at the target",
                "failure_phenotype": {"code": "OBJECT_PLACEMENT_IMPAIRED", "domain": "object_placement", "label": "Difficulty placing an object accurately", "description": "The transported cup was not placed steadily inside the target area.", "severity": "mild", "source": "ARAT; task-specific movement observation", "rehab_code": "GROSS_GRASP"},
            },
            {
                "id": "T4-S6",
                "voice": "Now open your fingers, release the cup, and move your empty hand away.",
                "target": {"x": 0.70, "y": 0.65, "r": 0.12, "landmark": "OBJECT_RELEASED", "icon": "table"},
                "hold_ms": 1200,
                "movement_required": False,
                "caption": "Release the cup at the target",
                "failure_phenotype": {"code": "OBJECT_RELEASE_IMPAIRED", "domain": "object_release", "label": "Difficulty releasing an object", "description": "The cup reached the target but the affected hand did not open and separate from it.", "severity": "moderate", "source": "ARAT grasp subscale", "rehab_code": "GROSS_GRASP"},
            },
        ],
    },
    {
        "id": "T5",
        "title": "Open Hand, Form Grasp, Release",
        "view": "Front view",
        "focus": "Hand opening, wrist control, grasp, release",
        "advanced_marker_required": True,
        "advanced_label": "Advanced grasp-release with AxonAI marker",
        "recommended_objects": ["empty plastic cup", "soft ball", "foam cylinder", "small paper box"],
        "steps": [
            {
                "id": "T5-S1",
                "voice": "Raise your affected hand to chest height and hold it where the camera can see it clearly.",
                "target": {"x": 0.5, "y": 0.45, "r": 0.10, "landmark": "CHEST"},
                "hold_ms": 1000,
                "caption": "Position hand at chest height",
                "failure_phenotype": {"code": "HAND_POSITIONING_IMPAIRED", "domain": "hand_task_positioning", "label": "Difficulty positioning the hand for a hand task", "description": "The affected hand did not reach the chest-height working position.", "severity": "moderate", "source": "Task-specific movement observation", "rehab_code": "HAND_OPENING"},
            },
            {
                "id": "T5-S2",
                "voice": "Open your affected hand as wide as you comfortably can.",
                "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "HAND_OPEN", "icon": "ball"},
                "hold_ms": 1200,
                "movement_required": False,
                "caption": "Open hand wide",
                "measure": ["hand_opening"],
                "failure_phenotype": {"code": "HAND_OPENING", "domain": "active_hand_opening", "label": "Difficulty opening the hand", "description": "The affected hand reached the working position but the fingers did not open sufficiently.", "severity": "moderate", "source": "Fugl-Meyer UE hand section", "rehab_code": "HAND_OPENING"},
            },
            {
                "id": "T5-S3",
                "voice": "Keep your hand open and steady for a moment.",
                "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "HAND_OPEN", "icon": "ball"},
                "hold_ms": 1500,
                "movement_required": False,
                "caption": "Hold the hand open",
                "failure_phenotype": {"code": "HAND_OPEN_HOLD_UNSTABLE", "domain": "hand_open_hold", "label": "Difficulty maintaining an open hand", "description": "The affected hand opened but did not remain open and steady.", "severity": "mild", "source": "Task-specific movement observation", "rehab_code": "HAND_OPENING"},
            },
            {
                "id": "T5-S4",
                "voice": "Now slowly close your fingers as if forming a secure grasp around a soft ball.",
                "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "HAND_CLOSED", "icon": "ball"},
                "hold_ms": 1200,
                "movement_required": False,
                "caption": "Form a gross grasp",
                "failure_phenotype": {"code": "GROSS_GRASP_FORMATION_IMPAIRED", "domain": "gross_grasp_formation", "label": "Difficulty forming a gross grasp", "description": "The affected hand opened but did not close into a functional gross-grasp shape.", "severity": "moderate", "source": "Fugl-Meyer UE hand section; ARAT", "rehab_code": "GROSS_GRASP"},
            },
            {
                "id": "T5-S5",
                "voice": "Hold the grasp steadily for a moment.",
                "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "HAND_CLOSED", "icon": "ball"},
                "hold_ms": 1500,
                "movement_required": False,
                "caption": "Maintain the gross grasp",
                "failure_phenotype": {"code": "GRASP_MAINTENANCE_IMPAIRED", "domain": "gross_grasp_maintenance", "label": "Difficulty maintaining a gross grasp", "description": "The affected hand formed a grasp but did not maintain it steadily.", "severity": "moderate", "source": "Task-specific movement observation", "rehab_code": "GROSS_GRASP"},
            },
            {
                "id": "T5-S6",
                "voice": "Open your fingers again to release the grasp.",
                "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "HAND_OPEN", "icon": "ball"},
                "hold_ms": 1200,
                "movement_required": False,
                "caption": "Open hand to release",
                "failure_phenotype": {"code": "HAND_RELEASE_IMPAIRED", "domain": "active_hand_release", "label": "Difficulty reopening the hand to release", "description": "The affected hand formed a grasp but did not actively reopen to release it.", "severity": "moderate", "source": "Fugl-Meyer UE hand section; ARAT", "rehab_code": "HAND_OPENING"},
            },
        ],
    },
    {
        "id": "T6",
        "title": "Pinch Coin / Pen / Key",
        "view": "Front view",
        "focus": "Fine pinch (thumb to index finger)",
        "steps": [
            {
                "id": "T6-S1",
                "voice": "Hold your hand up in front of your chest, palm facing you.",
                "target": {"x": 0.5, "y": 0.40, "r": 0.10, "landmark": "WRIST"},
                "hold_ms": 1500,
                "caption": "Raise hand to chest",
                "failure_phenotype": {"code": "PINCH_POSITIONING_IMPAIRED", "domain": "pinch_task_positioning", "label": "Difficulty positioning the hand for pinch", "description": "The affected hand did not reach the working position needed for the pinch task.", "severity": "moderate", "source": "Task-specific movement observation", "rehab_code": "PINCH_IMPAIRED"},
            },
            {
                "id": "T6-S2",
                "voice": "Now, slowly touch the tip of your thumb to the tip of your index finger, as if picking up a small coin.",
                "target": {"x": 0.5, "y": 0.40, "r": 0.12, "landmark": "PINCH", "icon": "coin"},
                "hold_ms": 1000,
                "movement_required": False,
                "caption": "Pinch thumb and index finger",
                "measure": ["pinch_grip"],
                "failure_phenotype": {"code": "PINCH_IMPAIRED", "domain": "thumb_index_opposition", "label": "Difficulty forming a thumb-index pinch", "description": "The affected thumb and index finger did not form the requested pinch position.", "severity": "mild", "source": "ARAT pinch subscale; Fugl-Meyer UE hand section", "rehab_code": "PINCH_IMPAIRED"},
            },
            {
                "id": "T6-S3",
                "voice": "Keep the thumb-index pinch steady for a moment.",
                "target": {"x": 0.5, "y": 0.40, "r": 0.12, "landmark": "PINCH", "icon": "coin"},
                "hold_ms": 1500,
                "movement_required": False,
                "caption": "Hold the pinch steadily",
                "failure_phenotype": {"code": "PINCH_HOLD_UNSTABLE", "domain": "pinch_stability", "label": "Unstable thumb-index pinch", "description": "The affected hand formed a pinch but did not maintain it steadily.", "severity": "mild", "source": "ARAT pinch subscale", "rehab_code": "PINCH_IMPAIRED"},
            },
            {
                "id": "T6-S4",
                "voice": "Separate your thumb and index finger to release the pinch.",
                "target": {"x": 0.5, "y": 0.40, "r": 0.12, "landmark": "PINCH_RELEASED", "icon": "coin"},
                "hold_ms": 1000,
                "movement_required": False,
                "caption": "Release the pinch",
                "failure_phenotype": {"code": "PINCH_RELEASE_IMPAIRED", "domain": "pinch_release", "label": "Difficulty releasing a pinch", "description": "The affected thumb and index finger formed a pinch but did not separate to release it.", "severity": "mild", "source": "Task-specific movement observation", "rehab_code": "PINCH_IMPAIRED"},
            },
        ],
    },
    {
        "id": "T7",
        "title": "Fold Towel (Two-Handed)",
        "view": "Front view",
        "focus": "Affected-side participation, bilateral coordination",
        "steps": [
            {
                "id": "T7-S1",
                "voice": "Our last task uses both hands together. Bring both hands up in front of you, around chest height.",
                "target": {"x": 0.5, "y": 0.40, "r": 0.12, "landmark": "WRISTS_APART"},
                "hold_ms": 1500,
                "caption": "Both hands up at chest",
                "failure_phenotype": {"code": "BILATERAL_INITIATION_IMPAIRED", "domain": "bilateral_initiation", "label": "Difficulty initiating a two-handed movement", "description": "Both hands did not reach the chest-height working position together.", "severity": "moderate", "source": "Bilateral arm training task observation", "rehab_code": "BILATERAL_NONUSE"},
            },
            {
                "id": "T7-S2",
                "voice": "Pretend you are holding the two corners of a towel. Bring both hands inward together until the corners meet in front of you.",
                "target": {"x": 0.5, "y": 0.40, "r": 0.12, "landmark": "WRISTS", "icon": "towel"},
                "hold_ms": 1400,
                "caption": "Bring both hands inward together",
                "measure": ["bilateral_symmetry", "affected_participation"],
                "failure_phenotype": {"code": "BILATERAL_NONUSE", "domain": "bilateral_inward_coordination", "label": "Limited affected-side participation in a two-handed movement", "description": "The two hands did not complete the inward towel-folding movement together.", "severity": "moderate", "source": "CIMT; bilateral arm training", "rehab_code": "BILATERAL_NONUSE"},
            },
            {
                "id": "T7-S3",
                "voice": "Now move both hands outward again while keeping them at the same height.",
                "target": {"x": 0.5, "y": 0.40, "r": 0.12, "landmark": "WRISTS_APART", "icon": "towel"},
                "hold_ms": 1500,
                "caption": "Move both hands outward together",
                "failure_phenotype": {"code": "BILATERAL_OUTWARD_CONTROL_IMPAIRED", "domain": "bilateral_outward_coordination", "label": "Difficulty coordinating outward two-handed movement", "description": "The two hands moved inward but did not complete the outward movement together.", "severity": "moderate", "source": "Bilateral arm training task observation", "rehab_code": "BILATERAL_NONUSE"},
            },
            {
                "id": "T7-S4",
                "voice": "Magnificent work. Lower both hands together and relax.",
                "target": {"x": 0.5, "y": 0.78, "r": 0.12, "landmark": "WRISTS_LOW"},
                "hold_ms": 1500,
                "caption": "Lower both hands together",
                "failure_phenotype": {"code": "BILATERAL_LOWERING_IMPAIRED", "domain": "bilateral_lowering_control", "label": "Difficulty lowering both hands together", "description": "Both hands did not complete the lowering movement together.", "severity": "mild", "source": "Bilateral arm training task observation", "rehab_code": "BILATERAL_NONUSE"},
            },
        ],
    },
]


HAND_TASKS_DATA: List[Dict[str, Any]] = [
    {
        "id": "H1",
        "title": "Open Hand",
        "view": "Front view",
        "focus": "Finger extension, palm opening, thumb-index spread",
        "steps": [
            {"id": "H1-S1", "voice": "We will begin the hand function package. Bring your affected hand up in front of your chest, with your palm facing the camera. If your hand is already open, keep it open and steady.", "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "WRIST"}, "hold_ms": 1200, "caption": "Hand up, palm facing camera"},
            {"id": "H1-S2", "voice": "Now slowly open your fingers as wide as you comfortably can, or keep them open if they are already open. Hold your palm open and steady.", "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "HAND_OPEN"}, "hold_ms": 1300, "caption": "Open hand and hold steady", "measure": ["finger_extension", "palm_openness", "thumb_index_spread"]},
            {"id": "H1-S3", "voice": "Good. Relax your hand and lower it to the same place on your lap.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"}, "hold_ms": 1200, "caption": "Return hand to the calibrated lap position"},
        ],
    },
    {
        "id": "H2",
        "title": "Make a Fist",
        "view": "Front view",
        "focus": "Mass finger flexion, fist closure completeness, closing control",
        "steps": [
            {"id": "H2-S1", "voice": "Hold your hand up again. Start with your hand open if you can.", "target": {"x": 0.5, "y": 0.45, "r": 0.10, "landmark": "WRIST"}, "hold_ms": 1200, "caption": "Hand ready"},
            {"id": "H2-S2", "voice": "Slowly close your fingers into a fist. Take your time, then hold the fist for a moment.", "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "WRIST"}, "hold_ms": 4500, "caption": "Slowly close into a fist", "measure": ["closure_completeness", "closing_speed"]},
            {"id": "H2-S3", "voice": "Now relax your hand again.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "WRIST"}, "hold_ms": 1200, "caption": "Relax hand"},
        ],
    },
    {
        "id": "H3",
        "title": "Thumb-Index Pinch",
        "view": "Front view",
        "focus": "Thumb-index opposition, pinch accuracy, pinch stability",
        "steps": [
            {"id": "H3-S1", "voice": "Bring your hand up in front of your chest, palm facing you or the camera.", "target": {"x": 0.5, "y": 0.40, "r": 0.10, "landmark": "WRIST"}, "hold_ms": 1200, "caption": "Hand ready for pinch"},
            {"id": "H3-S2", "voice": "Touch the tip of your thumb to the tip of your index finger, as if pinching a small coin. Hold it steady.", "target": {"x": 0.5, "y": 0.40, "r": 0.12, "landmark": "PINCH", "icon": "coin"}, "hold_ms": 1800, "caption": "Pinch thumb and index finger", "measure": ["pinch_distance", "pinch_accuracy", "pinch_stability"]},
            {"id": "H3-S3", "voice": "Separate your fingers, then lower your affected hand to the same place on your lap.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"}, "hold_ms": 1200, "caption": "Return hand to the calibrated lap position"},
        ],
    },
    {
        "id": "H4",
        "title": "Open and Close Hand",
        "view": "Front view",
        "focus": "Repeated opening, closing, timing, and control",
        "steps": [
            {"id": "H4-S1", "voice": "Open your hand wide and show your palm to the camera.", "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "HAND_OPEN"}, "hold_ms": 1500, "caption": "Open hand", "measure": ["hand_opening"]},
            {"id": "H4-S2", "voice": "Now close your hand gently, then open it again. Move slowly and smoothly.", "target": {"x": 0.5, "y": 0.45, "r": 0.12, "landmark": "WRIST"}, "hold_ms": 2500, "caption": "Close and open smoothly", "measure": ["open_close_timing", "movement_smoothness"]},
            {"id": "H4-S3", "voice": "Well done. Lower your affected hand to the same place on your lap.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"}, "hold_ms": 1200, "caption": "Return hand to the calibrated lap position"},
        ],
    },
    {
        "id": "H5",
        "title": "Advanced Grasp: Pick Up a Light Object",
        "view": "Front view",
        "focus": "Gross grasp, hand-object coupling, hold stability",
        "advanced_marker_required": True,
        "advanced_label": "Advanced grasp-release with AxonAI marker",
        "recommended_objects": ["empty plastic cup", "soft ball", "foam cylinder", "small paper box"],
        "steps": [
            {"id": "H5-S1", "voice": "This is an advanced grasp task. Reach toward your light object with the AxonAI marker facing the camera.", "target": {"x": 0.32, "y": 0.65, "r": 0.11, "landmark": "WRIST", "icon": "cup"}, "hold_ms": 1400, "caption": "Reach to the marked object"},
            {"id": "H5-S2", "voice": "Close your hand around the object and lift it slightly. Keep it steady.", "target": {"x": 0.50, "y": 0.55, "r": 0.12, "landmark": "WRIST", "icon": "cup"}, "hold_ms": 2200, "caption": "Grasp and hold object", "measure": ["object_hand_coupling", "hold_stability", "marker_visibility"]},
            {"id": "H5-S3", "voice": "Gently place the object back down.", "target": {"x": 0.32, "y": 0.65, "r": 0.11, "landmark": "WRIST", "icon": "table"}, "hold_ms": 1500, "caption": "Place object down"},
        ],
    },
    {
        "id": "H6",
        "title": "Advanced Release: Let Go at Target",
        "view": "Front view",
        "focus": "Finger release, object-hand separation, placement accuracy",
        "advanced_marker_required": True,
        "advanced_label": "Advanced grasp-release with AxonAI marker",
        "recommended_objects": ["empty plastic cup", "soft ball", "foam cylinder", "small paper box"],
        "steps": [
            {"id": "H6-S1", "voice": "Hold the marked object with your affected hand.", "target": {"x": 0.35, "y": 0.62, "r": 0.11, "landmark": "WRIST", "icon": "cup"}, "hold_ms": 1400, "caption": "Hold marked object"},
            {"id": "H6-S2", "voice": "Move the object to the target area, then open your fingers and let go.", "target": {"x": 0.68, "y": 0.62, "r": 0.12, "landmark": "WRIST", "icon": "table"}, "hold_ms": 2400, "caption": "Release at target", "measure": ["release_delay", "object_hand_separation", "placement_endpoint_error"]},
            {"id": "H6-S3", "voice": "Move your empty hand away from the object.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "WRIST"}, "hold_ms": 1400, "caption": "Move hand away"},
        ],
    },
    {
        "id": "H7",
        "title": "Wrist Lift and Stabilize",
        "view": "Front view",
        "focus": "Wrist control, unwanted finger flexion, hand stability",
        "steps": [
            {"id": "H7-S1", "voice": "Place your forearm comfortably, and bring your hand into view.", "target": {"x": 0.5, "y": 0.62, "r": 0.10, "landmark": "WRIST"}, "hold_ms": 1200, "caption": "Hand in view"},
            {"id": "H7-S2", "voice": "Lift your wrist gently and hold it steady. Try to keep your fingers relaxed.", "target": {"x": 0.5, "y": 0.48, "r": 0.11, "landmark": "WRIST"}, "hold_ms": 2200, "caption": "Lift and hold wrist", "measure": ["wrist_stability", "unwanted_finger_flexion"]},
            {"id": "H7-S3", "voice": "Relax your wrist and lower your hand.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "WRIST"}, "hold_ms": 1200, "caption": "Relax wrist"},
        ],
    },
]

attach_hand_failure_phenotypes(HAND_TASKS_DATA)


def _tasks_by_id(tasks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {task["id"]: task for task in tasks}


_upper_by_id = _tasks_by_id(TASKS_DATA)
_hand_by_id = _tasks_by_id(HAND_TASKS_DATA)
_lower_by_id = _tasks_by_id(LOWER_LIMB_TASKS_DATA)
INITIAL_ASSESSMENT_TASKS: List[Dict[str, Any]] = [
    _upper_by_id["T1"],
    _upper_by_id["T2"],
    _upper_by_id["T3"],
    _hand_by_id["H1"],
    _hand_by_id["H3"],
    _hand_by_id["H4"],
    _lower_by_id["L6"],
]


ASSESSMENT_PACKAGES: Dict[str, Dict[str, Any]] = {
    "initial": {
        "id": "initial",
        "title": "Initial Functional Assessment",
        "subtitle": "Survey-selected arm, hand, and comfortable-walking observations matched to current ability",
        "tasks": INITIAL_ASSESSMENT_TASKS,
    },
    "upper_limb": {
        "id": "upper_limb",
        "title": "Upper Limb Function Package",
        "subtitle": "Shoulder, elbow, reach, hand-to-mouth, and bilateral upper-limb tasks",
        "tasks": TASKS_DATA,
    },
    "hand": {
        "id": "hand",
        "title": "Hand Function Package",
        "subtitle": "Finger opening, fist closure, pinch, wrist control, and advanced grasp-release tasks",
        "tasks": HAND_TASKS_DATA,
    },
    "lower_limb": {
        "id": "lower_limb",
        "title": "Lower Limb Function Package",
        "subtitle": "Seated selective control, transfers, supported stepping, and gait prerequisites",
        "tasks": LOWER_LIMB_TASKS_DATA,
    },
    "balance": {
        "id": "balance",
        "title": "Balance Function Package",
        "subtitle": "Sitting stability, supported standing, weight shift, and step-stance control",
        "tasks": BALANCE_TASKS_DATA,
    },
}


ASSESSMENT_RUBRICS = build_rubrics([task for package in ASSESSMENT_PACKAGES.values() for task in package["tasks"]])


def _validated_assigned_task_ids(package_id: str, requested: Optional[List[str]]) -> List[str]:
    package = ASSESSMENT_PACKAGES.get(package_id)
    if not package:
        raise HTTPException(status_code=422, detail="Unsupported assessment package")
    allowed = [str(task["id"]) for task in package["tasks"]]
    if requested is None:
        return allowed
    selected = list(dict.fromkeys(str(task_id).strip() for task_id in requested if str(task_id).strip()))
    invalid = [task_id for task_id in selected if task_id not in allowed]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Unsupported assigned task ids: {', '.join(invalid)}")
    if not selected:
        raise HTTPException(status_code=422, detail="At least one assigned task is required")
    return [task_id for task_id in allowed if task_id in set(selected)]


async def _assessment_access_plan(
    user: Dict[str, Any],
    package_id: str,
    requested_task_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Enforce Alira's current one-time assessment grant on every backend entry."""
    _require_health_data_consent(user)
    assessments = await _care_assessments_for_user(user["id"])
    if package_id == "initial":
        if assessments or user.get("initial_assessment_completed_at"):
            raise HTTPException(
                status_code=409,
                detail="Your Initial Assessment is already complete. The next assessment opens only when Alira's schedule says it is due.",
            )
        recommendation = initial_assessment_recommendation(user.get("profile") or {})
        # A helper-confirmation pause keeps the tasks assigned: the app collects
        # the "my helper is with me" confirmation before it launches the camera.
        if not recommendation["can_start"] and not recommendation.get("helper_confirmation_required"):
            raise HTTPException(status_code=409, detail=recommendation["message"])
        expected_task_ids = list(recommendation["task_ids"])
        if requested_task_ids is not None and set(requested_task_ids) != set(expected_task_ids):
            raise HTTPException(status_code=422, detail="Assigned initial tasks do not match the saved readiness survey")
        return {
            "due": True,
            "trigger": "initial",
            "packages": ["initial"],
            "task_ids": expected_task_ids,
            "issue_report_id": None,
        }

    if not assessments:
        raise HTTPException(status_code=409, detail="Complete the Initial Assessment before starting a follow-up assessment.")
    plan = await _adaptive_care_plan_for_user(user, assessments=assessments)
    assessment = plan["assessment"]
    if not assessment.get("due") or not assessment.get("can_start"):
        due_at = str(assessment.get("due_at") or "")
        raise HTTPException(
            status_code=409,
            detail=f"Your next assessment is not due yet. It is scheduled for {due_at[:10]} so changes can be measured meaningfully.",
        )
    selected_package = str((assessment.get("packages") or [""])[0])
    if package_id != selected_package:
        raise HTTPException(
            status_code=422,
            detail=f"Alira selected the {selected_package} assessment for this check-in, not {package_id}.",
        )
    expected_task_ids = list(assessment.get("task_ids") or [])
    if requested_task_ids is not None and set(requested_task_ids) != set(expected_task_ids):
        raise HTTPException(status_code=422, detail="Assigned tasks do not match Alira's current assessment selection")
    return assessment


def _expected_domains_for_tasks(package_id: str, task_ids: List[str]) -> Optional[tuple[str, ...]]:
    if package_id != "initial":
        return None
    domains: List[str] = []
    if any(task_id.startswith("T") for task_id in task_ids):
        domains.append("upper_limb")
    if any(task_id.startswith("H") for task_id in task_ids):
        domains.append("hand")
    if any(task_id.startswith(("L", "B")) for task_id in task_ids):
        domains.append("lower_limb")
    return tuple(domains)


# ============ Functional Issue Rules ============
# Sources: Fugl-Meyer Upper Extremity Assessment (Fugl-Meyer, 1975);
# Action Research Arm Test (Lyle, 1981); Stroke Rehabilitation: A Function-Based Approach (Gillen);
# Bobath / NDT principles; Task-Specific Training (Carr & Shepherd).
STEP_PHENOTYPES: Dict[str, Dict[str, Any]] = {}
UPPER_LIMB_STEP_PHENOTYPES: Dict[str, Dict[str, Any]] = {}
PHENOTYPE_REHAB_ALIASES: Dict[str, str] = {}
for _package in ASSESSMENT_PACKAGES.values():
    for _task in _package["tasks"]:
        for _step in _task.get("steps", []):
            _phenotype = _step.get("failure_phenotype")
            if not _phenotype:
                continue
            _record = {
                **_phenotype,
                "task_id": _task["id"],
                "step_id": _step["id"],
                "package_id": _package["id"],
            }
            STEP_PHENOTYPES.setdefault(_step["id"], _record)
            if _package["id"] == "upper_limb":
                UPPER_LIMB_STEP_PHENOTYPES[_step["id"]] = _record
            PHENOTYPE_REHAB_ALIASES[_phenotype["code"]] = _phenotype.get("rehab_code", _phenotype["code"])


LEGACY_TASK_FAILURES: Dict[str, Dict[str, str]] = {
    "T1": {"code": "REACH_INCOMPLETE", "label": "Limited forward reach", "description": "The forward-reach task was not completed.", "source": "Fugl-Meyer UE; ARAT", "severity": "moderate", "domain": "forward_reach"},
    "T2": {"code": "SHOULDER_FLEX_LIMITED", "label": "Limited active arm elevation", "description": "The arm-elevation task was not completed.", "source": "Fugl-Meyer UE", "severity": "moderate", "domain": "shoulder_elevation"},
    "T3": {"code": "H2M_IMPAIRED", "label": "Difficulty bringing the hand to the mouth", "description": "The hand-to-mouth task was not completed.", "source": "Chedoke-McMaster Stroke Assessment; MESUPES", "severity": "moderate", "domain": "hand_to_mouth"},
    "T4": {"code": "GROSS_GRASP", "label": "Difficulty grasping and moving an object", "description": "The cup-transfer task was not completed.", "source": "ARAT grasp subscale", "severity": "moderate", "domain": "grasp_transport_release"},
    "T5": {"code": "HAND_OPENING", "label": "Difficulty opening the hand", "description": "The open-grasp-release hand task was not completed.", "source": "Fugl-Meyer UE hand section", "severity": "moderate", "domain": "hand_open_grasp_release"},
    "T6": {"code": "PINCH_IMPAIRED", "label": "Difficulty forming a thumb-index pinch", "description": "The thumb-index pinch task was not completed.", "source": "ARAT pinch subscale", "severity": "mild", "domain": "pinch"},
    "T7": {"code": "BILATERAL_NONUSE", "label": "Limited affected-side participation in a two-handed movement", "description": "The two-handed towel task was not completed.", "source": "CIMT; bilateral arm training", "severity": "moderate", "domain": "bilateral_coordination"},
}

for _package in ASSESSMENT_PACKAGES.values():
    for _task in _package["tasks"]:
        if _task["id"] in LEGACY_TASK_FAILURES:
            continue
        _fallback = next(
            (step.get("failure_phenotype") for step in _task.get("steps", []) if step.get("failure_phenotype")),
            None,
        )
        if _fallback:
            LEGACY_TASK_FAILURES[_task["id"]] = {
                "code": _fallback["code"],
                "label": _fallback["label"],
                "description": f"The {_task['title'].lower()} task was not completed.",
                "source": _fallback["source"],
                "severity": _fallback["severity"],
                "domain": _fallback["domain"],
            }


def derive_functional_issues(task_results: List[TaskResult]) -> List[FunctionalIssue]:
    issues: List[FunctionalIssue] = []
    seen_failures = set()

    def add(code, label, description, source, severity, related, related_step=None, domain=None):
        key = (code, related_step)
        if key in seen_failures:
            return
        seen_failures.add(key)
        issues.append(FunctionalIssue(
            code=code, label=label, description=description,
            source=source, severity=severity, related_task=related,
            related_step=related_step, phenotype_domain=domain,
        ))

    # Failure localization is step-specific. A failed step contributes exactly
    # the movement phenotype declared on that step; no cross-task inference is
    # performed here. Old clients without step details retain a broad fallback.
    for task_result in task_results:
        failed_steps = [step for step in task_result.steps if not step.completed]
        matched_step_failure = False
        for step_result in failed_steps:
            phenotype = STEP_PHENOTYPES.get(step_result.step_id)
            if not phenotype or phenotype["task_id"] != task_result.task_id:
                continue
            matched_step_failure = True
            add(
                phenotype["code"],
                phenotype["label"],
                phenotype["description"],
                phenotype["source"],
                phenotype["severity"],
                task_result.task_id,
                step_result.step_id,
                phenotype["domain"],
            )

        if task_result.completed_steps < task_result.total_steps and not matched_step_failure:
            legacy = LEGACY_TASK_FAILURES.get(task_result.task_id)
            if legacy:
                add(
                    legacy["code"], legacy["label"], legacy["description"],
                    legacy["source"], legacy["severity"], task_result.task_id,
                    None, legacy["domain"],
                )

    # Completion and movement quality are separate findings. A patient may
    # reach the target by leaning the trunk or hiking the shoulder.
    for task_result in task_results:
        if not task_result.task_id.startswith("T"):
            continue
        metric_records = [task_result.metrics, *(step.metrics for step in task_result.steps)]
        trunk_values = [
            float(metrics.get("trunk_lean_deg"))
            for metrics in metric_records
            if isinstance(metrics.get("trunk_lean_deg"), (int, float))
        ]
        peak_trunk_lean = max(trunk_values, default=0.0)
        if peak_trunk_lean > 18:
            add(
                "TRUNK_COMP",
                "Excess trunk compensation during upper-limb movement",
                f"The target was approached with a peak 2D trunk lean of {peak_trunk_lean:.0f} degrees.",
                "Camera-derived 2D movement-quality screen; therapist confirmation required",
                "moderate" if peak_trunk_lean > 30 else "mild",
                task_result.task_id,
                None,
                "trunk_compensation",
            )
        if any(bool(metrics.get("shoulder_hike")) for metrics in metric_records):
            add(
                "SHOULDER_HIKE",
                "Shoulder elevation compensation",
                "The affected shoulder elevated during the upper-limb task.",
                "Camera-derived shoulder-line screen; therapist confirmation required",
                "mild",
                task_result.task_id,
                None,
                "shoulder_compensation",
            )

    if not issues:
        issues.append(FunctionalIssue(
            code="NO_ISSUES",
            label="No failed movement steps identified",
            description="The patient completed every observed movement step in this assessment.",
            source="Step-level movement observation",
            severity="mild",
            related_task="ALL",
        ))
    return issues


def _snapshot_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _progress_task_domain(task_id: str) -> str:
    if task_id.startswith("H"):
        return "hand"
    if task_id.startswith(("L", "B")):
        return "lower_limb"
    return "upper_limb"


def _task_quality_with_survey_mobility(
    task_quality: Dict[str, Any],
    patient_parameters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Keep module scores derived solely from guided assessment task evidence."""
    return task_quality


def _summary_with_survey_mobility(
    summary: Dict[str, Any],
    patient_parameters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Add patient-reported affected regions without changing task metrics."""
    context = survey_domain_context(patient_parameters)
    summary = {
        **summary,
        "domains": [dict(domain) for domain in (summary.get("domains") or [])],
    }
    for domain in summary.get("domains") or []:
        domain_id = str(domain.get("domain") or "")
        survey = context.get(domain_id)
        if not survey:
            continue
        domain.update(survey)
        if not survey["survey_answered"]:
            continue
        if survey["survey_affected"] is None:
            domain.update({
                "status": "survey_unsure",
                "summary": "The survey did not identify whether this area was affected. The numeric score comes only from guided assessment tasks.",
            })
        elif survey["survey_affected"]:
            domain.update({
                "status": "survey_reported_affected",
                "summary": "This area was identified in your survey. The numeric score comes only from guided assessment tasks.",
            })
        else:
            domain.update({
                "status": "survey_reported_unaffected",
                "summary": "This area was not reported as affected in your survey. The numeric score comes only from guided assessment tasks.",
            })
    if any(domain.get("survey_affected") is True for domain in summary.get("domains") or []):
        summary["overall_status"] = "survey_reported_affected"
    return summary


def build_functional_metrics(
    task_results: Sequence[Any],
    assigned_task_ids=None,
    patient_parameters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Derive stable patient-facing progress metrics from saved task evidence."""
    tasks = list(task_results)

    def domain_tasks(domain: str) -> List[Any]:
        return [task for task in tasks if _progress_task_domain(str(_snapshot_value(task, "task_id", ""))) == domain]

    def records(domain: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for task in domain_tasks(domain):
            task_metrics = _snapshot_value(task, "metrics", {}) or {}
            if isinstance(task_metrics, dict):
                rows.append(task_metrics)
            for step in _snapshot_value(task, "steps", []) or []:
                step_metrics = _snapshot_value(step, "metrics", {}) or {}
                if isinstance(step_metrics, dict):
                    rows.append(step_metrics)
        return rows

    def numeric_max(domain: str, *keys: str) -> Optional[float]:
        values = [
            float(row[key])
            for row in records(domain)
            for key in keys
            if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)
        ]
        return max(values) if values else None

    def completion(domain: str) -> Optional[float]:
        selected = [
            task for task in domain_tasks(domain)
            if not bool((_snapshot_value(task, "metrics", {}) or {}).get("walking_skipped"))
        ]
        total = sum(max(0, int(_snapshot_value(task, "total_steps", 0) or 0)) for task in selected)
        if not total:
            return None
        completed = sum(max(0, int(_snapshot_value(task, "completed_steps", 0) or 0)) for task in selected)
        return round(min(1.0, completed / total), 3)

    upper_tasks = domain_tasks("upper_limb")
    hand_tasks = domain_tasks("hand")
    lower_tasks = domain_tasks("lower_limb")
    walking_skipped = any(bool((_snapshot_value(task, "metrics", {}) or {}).get("walking_skipped")) for task in lower_tasks)
    shoulder_elevation = numeric_max("upper_limb", "shoulder_elevation_deg")
    trunk_lean = numeric_max("upper_limb", "trunk_lean_deg")
    hand_opening = numeric_max("hand", "hand_open_score")
    pinch_grip = numeric_max("hand", "pinch_score")
    gait_symmetry = numeric_max("lower_limb", "gait_bilateral_motion_symmetry", "bilateral_wrist_displacement_symmetry")
    walking_duration_ms = numeric_max("lower_limb", "uploaded_video_duration_ms")
    reach_completion = completion("upper_limb")
    hand_completion = completion("hand")
    shoulder_hike = any(bool(row.get("shoulder_hike")) for row in records("upper_limb"))

    task_quality = score_assessment(tasks, ASSESSMENT_RUBRICS, assigned_task_ids)
    survey_context = survey_domain_context(patient_parameters)
    walking_video_uploaded = any(
        (_snapshot_value(task, "metrics", {}) or {}).get("walking_video_role") in {"supporting_record", "gait_scoring_input"}
        for task in lower_tasks
    )

    return {
        "task_quality": task_quality,
        "shoulder_flexion_deg": round(shoulder_elevation, 1) if shoulder_elevation is not None else None,
        "trunk_lean_deg": round(trunk_lean, 1) if trunk_lean is not None else None,
        "reach_completion": reach_completion,
        "bilateral_symmetry": round(gait_symmetry, 3) if gait_symmetry is not None else None,
        "pinch_grip": round(pinch_grip, 3) if pinch_grip is not None else None,
        "hand_opening": round(hand_opening, 3) if hand_opening is not None else None,
        "walking_skipped": walking_skipped,
        "domains": {
            "upper_limb": {
                **survey_context["upper_limb"],
                "observed": bool(upper_tasks),
                "step_completion_percent": round(100 * reach_completion) if reach_completion is not None else None,
                "shoulder_elevation_deg": round(shoulder_elevation, 1) if shoulder_elevation is not None else None,
                "trunk_lean_deg": round(trunk_lean, 1) if trunk_lean is not None else None,
                "shoulder_hike_detected": shoulder_hike,
            },
            "hand": {
                **survey_context["hand"],
                "observed": bool(hand_tasks),
                "step_completion_percent": round(100 * hand_completion) if hand_completion is not None else None,
                "hand_opening_percent": round(100 * hand_opening) if hand_opening is not None else None,
                "pinch_control_percent": round(100 * pinch_grip) if pinch_grip is not None else None,
            },
            "lower_limb": {
                **survey_context["lower_limb"],
                "observed": bool(lower_tasks) and not walking_skipped,
                "result_source": "assessment_tasks",
                "survey_score": None,
                "reported_assistance_level": None,
                "skipped": walking_skipped,
                "walking_video_uploaded": walking_video_uploaded,
                "step_completion_percent": round(100 * completion("lower_limb")) if completion("lower_limb") is not None else None,
                "bilateral_motion_symmetry_percent": round(100 * gait_symmetry) if gait_symmetry is not None else None,
                "full_body_visibility_percent": round(100 * (numeric_max("lower_limb", "gait_full_body_visibility_ratio") or 0)) if numeric_max("lower_limb", "gait_full_body_visibility_ratio") is not None else None,
                "video_duration_seconds": round(walking_duration_ms / 1000, 1) if walking_duration_ms is not None else None,
            },
        },
    }


def _functional_metrics_with_survey_mobility(
    saved_metrics: Optional[Dict[str, Any]],
    task_results: Sequence[Any],
    assigned_task_ids: Optional[Sequence[str]],
    patient_parameters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Normalize historic snapshots to task-only scores plus survey context."""
    computed = build_functional_metrics(task_results, assigned_task_ids, patient_parameters)
    if not saved_metrics:
        return computed
    normalized = dict(saved_metrics)
    normalized["task_quality"] = computed["task_quality"]
    saved_domains = dict(normalized.get("domains") or {})
    for domain_id in ("upper_limb", "hand", "lower_limb"):
        saved_domains[domain_id] = {
            **dict(saved_domains.get(domain_id) or {}),
            **computed["domains"][domain_id],
        }
    normalized["domains"] = saved_domains
    return normalized


def _snapshot_issue_category(issue: Any) -> str:
    text = " ".join(
        str(_snapshot_value(issue, key, "") or "")
        for key in ("code", "label", "description", "phenotype_domain")
    ).lower()
    domain = str(_snapshot_value(issue, "phenotype_domain", "") or "").lower()
    if domain == "lower_limb" or re.search(r"walk|gait|step|balance|lower limb|knee|ankle", text):
        return "lower_limb"
    if domain == "hand" or re.search(r"hand|finger|grip|grasp|pinch", text):
        return "hand"
    if domain == "upper_limb" or re.search(r"shoulder|reach|arm|deltoid|elbow|trunk", text):
        return "upper_limb"
    return "other"


def _snapshot_threshold_events(task_results: Sequence[Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for task in task_results:
        task_id = str(_snapshot_value(task, "task_id", ""))
        records = [("task", _snapshot_value(task, "metrics", {}) or {})]
        records.extend(
            (str(_snapshot_value(step, "step_id", "")), _snapshot_value(step, "metrics", {}) or {})
            for step in (_snapshot_value(task, "steps", []) or [])
        )
        trunk_values = [
            (source, float(metrics["trunk_lean_deg"]))
            for source, metrics in records
            if isinstance(metrics, dict) and isinstance(metrics.get("trunk_lean_deg"), (int, float))
        ]
        if trunk_values:
            source, observed = max(trunk_values, key=lambda item: item[1])
            if observed > 18:
                events.append({
                    "task_id": task_id,
                    "source": source,
                    "metric": "trunk_lean_deg",
                    "observed": round(observed, 2),
                    "operator": ">",
                    "threshold": 18,
                    "finding_code": "TRUNK_COMP",
                    "severity_rule": "moderate above 30 degrees; otherwise mild",
                })
        shoulder_sources = [
            source
            for source, metrics in records
            if isinstance(metrics, dict) and metrics.get("shoulder_hike") is True
        ]
        if shoulder_sources:
            events.append({
                "task_id": task_id,
                "source": shoulder_sources[0],
                "metric": "shoulder_hike",
                "observed": True,
                "operator": "is",
                "threshold": True,
                "finding_code": "SHOULDER_HIKE",
            })
    return events


def build_movement_snapshot_decision(
    task_results: Sequence[Any],
    functional_issues: Sequence[Any],
    body_function_summary: Dict[str, Any],
    affected_side: str,
    model_analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create one auditable source of truth for the patient snapshot presentation."""
    side = "left" if str(affected_side).lower() == "left" else "right"
    issues = [item for item in functional_issues if str(_snapshot_value(item, "code", "")) != "NO_ISSUES"]
    issue_rows = [
        {
            "code": str(_snapshot_value(item, "code", "")),
            "label": str(_snapshot_value(item, "label", "")),
            "severity": str(_snapshot_value(item, "severity", "")),
            "related_task": str(_snapshot_value(item, "related_task", "")),
            "related_step": _snapshot_value(item, "related_step"),
            "phenotype_domain": _snapshot_value(item, "phenotype_domain"),
            "category": _snapshot_issue_category(item),
        }
        for item in issues
    ]
    primary = next((item for item in issue_rows if item["category"] == "upper_limb"), None)
    primary = primary or (issue_rows[0] if issue_rows else None)
    overall_status = str(body_function_summary.get("overall_status") or "analysis_pending")

    if primary:
        category = primary["category"]
        if category == "upper_limb":
            presentation = {
                "eyebrow": f"{side.upper()} SHOULDER",
                "title": f"Your {side} shoulder may need support when reaching",
                "summary": "Reaching, lifting, or placing everyday objects may require more effort.",
                "tone": "attention",
            }
        elif category == "hand":
            presentation = {
                "eyebrow": "HAND CONTROL",
                "title": f"Using your {side} hand may need support",
                "summary": "This may affect opening your hand, holding everyday objects, or letting them go.",
                "tone": "attention",
            }
        elif category == "lower_limb":
            presentation = {
                "eyebrow": "WALKING",
                "title": "Walking may need support",
                "summary": "This may affect walking confidence, balance, or how easily you move around.",
                "tone": "attention",
            }
        else:
            presentation = {
                "eyebrow": "MOVEMENT CHECK",
                "title": primary["label"],
                "summary": "This movement area may need extra support during everyday activities.",
                "tone": "attention",
            }
    elif overall_status == "no_observable_difficulty":
        presentation = {
            "eyebrow": "MOVEMENT CHECK",
            "title": "Your movement looked steady",
            "summary": "No clear functional problem stood out in the movements completed during this assessment.",
            "tone": "well",
        }
    else:
        presentation = {
            "eyebrow": "ANALYSIS IN PROGRESS",
            "title": "We are checking your movement",
            "summary": "Your recordings are still being reviewed before a functional finding is shown.",
            "tone": "pending",
        }

    marker_visible = bool(primary and primary["category"] == "upper_limb")
    marker = {
        "visible": marker_visible,
        "region": f"{side}_shoulder" if marker_visible else None,
        "side": side if marker_visible else None,
        "source_issue_code": primary["code"] if marker_visible else None,
        "coordinate_source": "age-specific anatomy shoulder anchor" if marker_visible else None,
        "reason": (
            "The primary finding was classified as upper-limb reaching or shoulder function, so the affected-side shoulder anchor is highlighted."
            if marker_visible else
            "No upper-limb primary finding was selected, so the shoulder marker is hidden."
        ),
    }
    step_issue_codes = {
        (item["related_task"], item["related_step"]): item["code"]
        for item in issue_rows
        if item.get("related_step")
    }
    step_outcomes = [
        {
            "task_id": str(_snapshot_value(task, "task_id", "")),
            "step_id": str(_snapshot_value(step, "step_id", "")),
            "completed": bool(_snapshot_value(step, "completed", False)),
            "failure_code": (
                _snapshot_value(step, "failure_code")
                or step_issue_codes.get((str(_snapshot_value(task, "task_id", "")), str(_snapshot_value(step, "step_id", ""))))
            ),
        }
        for task in task_results
        for step in (_snapshot_value(task, "steps", []) or [])
    ]
    analysis = dict(model_analysis or {})
    return {
        "version": "1.0",
        "status": "finding_selected" if primary else overall_status,
        "affected_side": side,
        "step_outcomes": step_outcomes,
        "triggered_thresholds": _snapshot_threshold_events(task_results),
        "functional_findings": issue_rows,
        "body_function_domains": [
            {
                "domain": item.get("domain"),
                "status": item.get("status"),
                "findings_count": item.get("findings_count", 0),
                "step_completion_percent": item.get("step_completion_percent", 0),
            }
            for item in body_function_summary.get("domains") or []
        ],
        "model_status": {
            "overall": analysis.get("status", "waiting_for_inputs"),
            "gpu_stage": dict(analysis.get("gpu_stage") or {}),
            "musculoskeletal_stage": dict(analysis.get("musculoskeletal_stage") or {}),
        },
        "primary_finding": primary,
        "selection_rule": {
            "strategy": "prioritize the first derived upper-limb finding; otherwise use the first derived finding",
            "candidate_issue_codes": [item["code"] for item in issue_rows],
            "selected_issue_code": primary["code"] if primary else None,
            "no_issue_sentinel_excluded": True,
        },
        "presentation": presentation,
        "anatomy_marker": marker,
    }


# ============ Rehab Exercise Library (rule-based) ============
EXERCISE_LIBRARY: Dict[str, RehabExercise] = {
    "REACH_INCOMPLETE": RehabExercise(
        id="ex_reach", name="Graded Forward Reach",
        description="Sit upright. Reach toward a target placed progressively farther forward (start at 50% arm length, progress to 100%). Hold 2 seconds.",
        sets=3, reps=10, frequency="Twice daily", targets_issue="REACH_INCOMPLETE",
        source="Task-Specific Training (Carr & Shepherd)",
    ),
    "TRUNK_COMP": RehabExercise(
        id="ex_trunk", name="Trunk-Restrained Reaching",
        description="Sit with a strap or chair-back contact to limit trunk motion. Reach forward using only the arm.",
        sets=3, reps=10, frequency="Daily",
        targets_issue="TRUNK_COMP", source="Levin & Michaelsen (2008)",
    ),
    "SHOULDER_FLEX_LIMITED": RehabExercise(
        id="ex_wallslide", name="Supported Arm Elevation Practice",
        description="Begin seated with the forearm supported on a table or towel. Slide toward a meaningful target within a comfortable, pain-free range. Progress to a wall slide only after a therapist confirms shoulder safety and standing balance.",
        sets=3, reps=10, frequency="Twice daily",
        targets_issue="SHOULDER_FLEX_LIMITED", source="NICE NG236 repetitive task training; task-specific upper-limb practice",
    ),
    "SHOULDER_HIKE": RehabExercise(
        id="ex_scapdepress", name="Scapular Depression Practice",
        description="Sit with the affected forearm supported. Settle the shoulder away from the ear, then practise a short pain-free reach while keeping the trunk upright.",
        sets=3, reps=10, frequency="Daily",
        targets_issue="SHOULDER_HIKE", source="Task-specific motor practice and movement-quality feedback",
    ),
    "H2M_IMPAIRED": RehabExercise(
        id="ex_h2m", name="Hand-to-Mouth ADL Practice",
        description="A virtual cup appears in your hand on screen - no real object is needed. Practice bringing it to your mouth with the affected hand, focusing on smooth elbow flexion.",
        sets=3, reps=10, frequency="Twice daily",
        targets_issue="H2M_IMPAIRED", source="Occupational Therapy ADL retraining",
    ),
    "GROSS_GRASP": RehabExercise(
        id="ex_grasp", name="Cylindrical Grasp & Transport",
        description="A virtual cup is shown on screen - no real object is needed. Reach to it, carry it across the midline with your hand, and set it down at the target.",
        sets=3, reps=10, frequency="Twice daily",
        targets_issue="GROSS_GRASP", source="ARAT-based functional retraining",
    ),
    "HAND_OPENING": RehabExercise(
        id="ex_handopen", name="Active Hand Opening and Release",
        description="Support the forearm on a table. A virtual ball is shown on screen - practise opening the hand around it, releasing, and relaxing. Use assistance rather than resistance when active finger extension is limited.",
        sets=3, reps=10, frequency="Twice daily",
        targets_issue="HAND_OPENING", source="NICE NG236 repetitive task training; Fugl-Meyer UE hand task concepts",
    ),
    "PINCH_IMPAIRED": RehabExercise(
        id="ex_pinch", name="Pinch & Peg Placement",
        description="A virtual peg and container are shown on screen - no real objects are needed. Pinch as if lifting the peg and place it into the container. Practice all 5 finger oppositions.",
        sets=3, reps=10, frequency="Daily",
        targets_issue="PINCH_IMPAIRED", source="Jebsen Hand Function retraining",
    ),
    "BILATERAL_NONUSE": RehabExercise(
        id="ex_bilateral", name="Bilateral Arm Training (BATRAC-style)",
        description="Perform symmetric two-handed activities: fold towel, clap, push a roller forward together.",
        sets=3, reps=10, frequency="Daily",
        targets_issue="BILATERAL_NONUSE", source="BATRAC (Whitall et al.)",
    ),
    "LOWER_LIMB_SELECTIVE_CONTROL": RehabExercise(
        id="ex_lower_selective", name="Supported Selective Lower-Limb Control",
        description="In supported sitting, practice slow knee extension and return within a comfortable range. Stop if pain, marked fatigue, or loss of sitting balance occurs.",
        sets=2, reps=8, frequency="Daily with therapist-approved assistance",
        targets_issue="LOWER_LIMB_SELECTIVE_CONTROL", source="Fugl-Meyer LE; task-specific stroke rehabilitation",
    ),
    "ANKLE_DORSIFLEXION_CONTROL": RehabExercise(
        id="ex_ankle_dorsiflexion", name="Seated Toe-Lift Practice",
        description="With the heel supported, lift the forefoot and toes, hold briefly, and lower slowly. Keep the knee aligned.",
        sets=2, reps=10, frequency="Daily",
        targets_issue="ANKLE_DORSIFLEXION_CONTROL", source="Task-specific lower-limb training",
    ),
    "SIT_TO_STAND_IMPAIRED": RehabExercise(
        id="ex_sit_to_stand", name="Assisted Sit-to-Stand Practice",
        description="Practice foot placement, forward trunk translation, seat-off, and controlled sitting with a therapist or capable caregiver and a fixed support.",
        sets=2, reps=5, frequency="Therapist-supervised",
        targets_issue="SIT_TO_STAND_IMPAIRED", source="Task-specific mobility training; Five Times Sit-to-Stand task analysis",
    ),
    "SUPPORTED_STANDING_CONTROL": RehabExercise(
        id="ex_supported_stand", name="Supported Standing Alignment",
        description="With close guarding and a fixed support, practice aligned standing with both knees stable. Do not attempt independently when fall risk is present.",
        sets=3, reps=3, frequency="Therapist-supervised",
        targets_issue="SUPPORTED_STANDING_CONTROL", source="PASS; Berg Balance Scale task concepts",
    ),
    "GAIT_INITIATION_IMPAIRED": RehabExercise(
        id="ex_supported_step", name="Guarded Step Initiation",
        description="With close guarding and a fixed support, practice weight shift, short affected-foot advancement, toe clearance, placement, and return.",
        sets=2, reps=5, frequency="Therapist-supervised",
        targets_issue="GAIT_INITIATION_IMPAIRED", source="Task-specific gait training; observational gait analysis",
    ),
    "WEIGHT_BEARING_ASYMMETRY": RehabExercise(
        id="ex_weight_shift", name="Supported Affected-Side Weight Shift",
        description="With close guarding, shift the pelvis gradually toward the affected foot while keeping both feet down and minimizing trunk substitution.",
        sets=3, reps=6, frequency="Therapist-supervised",
        targets_issue="WEIGHT_BEARING_ASYMMETRY", source="PASS; task-specific balance training",
    ),
    "SITTING_BALANCE_IMPAIRED": RehabExercise(
        id="ex_sitting_balance", name="Guarded Sitting Midline and Reach",
        description="Practice upright sitting, small controlled reaches, and return to midline with feet supported and a caregiver guarding the affected side.",
        sets=3, reps=5, frequency="Daily with approved assistance",
        targets_issue="SITTING_BALANCE_IMPAIRED", source="PASS; task-specific balance training",
    ),
    "DYNAMIC_BALANCE_IMPAIRED": RehabExercise(
        id="ex_step_stance", name="Supported Step-Stance Control",
        description="After supported standing is safe, practice a short step stance and controlled return using fixed support and close guarding.",
        sets=2, reps=4, frequency="Therapist-supervised",
        targets_issue="DYNAMIC_BALANCE_IMPAIRED", source="Berg Balance Scale task concepts; task-specific balance training",
    ),
    "NO_ISSUES": RehabExercise(
        id="ex_maintenance", name="Maintenance Conditioning",
        description="Continue full-ROM stretches, light resistance work, and functional ADL practice.",
        sets=2, reps=15, frequency="Daily",
        targets_issue="NO_ISSUES", source="General stroke rehab guidelines",
    ),
}


FIXED_CORE_REHAB_CODES = (
    "TRUNK_COMP",
    "REACH_INCOMPLETE",
    "GROSS_GRASP",
    "H2M_IMPAIRED",
)
FIXED_CORE_REHAB_IDS = tuple(EXERCISE_LIBRARY[code].id for code in FIXED_CORE_REHAB_CODES)


def fixed_core_rehab_plan(profile: Optional[Dict[str, Any]] = None) -> List[RehabExercise]:
    """Return the current four-exercise programme for every patient account."""
    profile = dict(profile or {})
    priorities = profile.get("patient_priorities") or []
    if isinstance(priorities, str):
        priorities = [priorities]
    linked_goal = str((priorities or [profile.get("primary_goal") or ""])[0] or "").strip() or None
    assistance_reported = any(
        str(profile.get(key) or "").lower() == value
        for key, value in (
            ("sitting_ability", "needs_support"),
            ("affected_arm_movement", "help_only"),
            ("affected_hand_movement", "help_only"),
        )
    )
    assistance_note = (
        " Use the help of a carer or family member stated in your setup answers, and do not attempt the movement alone."
        if assistance_reported else ""
    )
    plan: List[RehabExercise] = []
    for code in FIXED_CORE_REHAB_CODES:
        exercise = EXERCISE_LIBRARY[code]
        plan.append(exercise.model_copy(update={
            "linked_goal": linked_goal,
            "selection_reason": (
                "Included in Rehyn's current four-exercise core programme. "
                "The daily level progresses from exercise-session frequency rather than survey or assessment findings."
            ),
            "safety_note": (
                "Use a stable seated position and a comfortable, pain-free range. Stop for new pain, marked fatigue, "
                f"dizziness, new weakness, or loss of balance.{assistance_note}"
            ),
            "requires_clinician_confirmation": False,
        }))
    return plan


def _clinical_grade(patient_parameters: Optional[Dict[str, Any]], *keys: str) -> Optional[int]:
    measures = (patient_parameters or {}).get("clinician_measures") or {}
    lookup = {str(key).upper(): value for key, value in measures.items()}
    value = next((lookup[key.upper()] for key in keys if key.upper() in lookup), None)
    match = re.search(r"(?<!\d)([0-5])(?!\d)", str(value)) if value is not None else None
    return int(match.group(1)) if match else None


SURVEY_PLAN_MIN_EXERCISES = 4
SURVEY_PLAN_MAX_EXERCISES = 5
SURVEY_SENTINEL_ANSWERS = {"none", "not_sure", "unsure"}
SURVEY_STANDING_EXERCISE_CODES = {
    "SIT_TO_STAND_IMPAIRED",
    "SUPPORTED_STANDING_CONTROL",
    "GAIT_INITIATION_IMPAIRED",
    "WEIGHT_BEARING_ASYMMETRY",
    "DYNAMIC_BALANCE_IMPAIRED",
}
SURVEY_ARM_EXERCISE_CODES = {
    "REACH_INCOMPLETE",
    "TRUNK_COMP",
    "SHOULDER_FLEX_LIMITED",
    "SHOULDER_HIKE",
    "H2M_IMPAIRED",
    "BILATERAL_NONUSE",
}
SURVEY_HAND_EXERCISE_CODES = {"GROSS_GRASP", "HAND_OPENING", "PINCH_IMPAIRED"}
SURVEY_LOWER_SEATED_EXERCISE_CODES = {
    "LOWER_LIMB_SELECTIVE_CONTROL",
    "ANKLE_DORSIFLEXION_CONTROL",
    "SITTING_BALANCE_IMPAIRED",
}

# Each option names one observable everyday problem and has one direct match.
# Compensation exercises are never used as generic fillers: trunk lean and
# shoulder hike must be explicitly reported before those exercises are added.
SURVEY_PROBLEM_RULES: Dict[str, Tuple[Tuple[str, str, str], ...]] = {
    "arm": (
        ("reach_forward", "REACH_INCOMPLETE", "reaching forward is difficult"),
        ("raise_arm", "SHOULDER_FLEX_LIMITED", "raising the affected arm is difficult"),
        ("hand_to_mouth", "H2M_IMPAIRED", "bringing the hand to the mouth is difficult"),
        ("trunk_lean", "TRUNK_COMP", "staying upright while reaching is difficult"),
        ("shoulder_hike", "SHOULDER_HIKE", "keeping the shoulder down while lifting the arm is difficult"),
        ("use_both_arms", "BILATERAL_NONUSE", "using both arms together is difficult"),
    ),
    "hand": (
        ("open_release", "HAND_OPENING", "opening the hand or releasing an object is difficult"),
        ("grasp_hold", "GROSS_GRASP", "grasping and holding a cup-sized object is difficult"),
        ("pinch_small_objects", "PINCH_IMPAIRED", "pinching small objects is difficult"),
    ),
    "mobility": (
        ("sitting_balance", "SITTING_BALANCE_IMPAIRED", "sitting upright without losing balance is difficult"),
        ("knee_control", "LOWER_LIMB_SELECTIVE_CONTROL", "controlling the affected knee is difficult"),
        ("foot_clearance", "ANKLE_DORSIFLEXION_CONTROL", "lifting the toes or clearing the foot is difficult"),
        ("sit_to_stand", "SIT_TO_STAND_IMPAIRED", "standing up from or sitting down on a chair is difficult"),
        ("standing_balance", "SUPPORTED_STANDING_CONTROL", "standing steadily is difficult"),
        ("weight_affected_leg", "WEIGHT_BEARING_ASYMMETRY", "putting weight through the affected leg is difficult"),
        ("start_step", "GAIT_INITIATION_IMPAIRED", "starting a step is difficult"),
        ("step_balance", "DYNAMIC_BALANCE_IMPAIRED", "keeping balance in a step position is difficult"),
    ),
}

SURVEY_SUPPORTING_EXERCISES: Dict[str, Tuple[str, ...]] = {
    "arm": ("REACH_INCOMPLETE", "SHOULDER_FLEX_LIMITED", "H2M_IMPAIRED", "BILATERAL_NONUSE"),
    "hand": ("HAND_OPENING", "GROSS_GRASP", "BILATERAL_NONUSE", "PINCH_IMPAIRED", "REACH_INCOMPLETE"),
    "mobility": (
        "LOWER_LIMB_SELECTIVE_CONTROL",
        "ANKLE_DORSIFLEXION_CONTROL",
        "SITTING_BALANCE_IMPAIRED",
        "SIT_TO_STAND_IMPAIRED",
        "SUPPORTED_STANDING_CONTROL",
        "WEIGHT_BEARING_ASYMMETRY",
        "GAIT_INITIATION_IMPAIRED",
    ),
}


def _survey_selected_values(profile: Dict[str, Any], key: str) -> Tuple[set[str], bool]:
    """Return normalized values and whether this version of the question was answered."""
    if key not in profile or profile.get(key) is None:
        return set(), False
    raw = profile.get(key)
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    selected = {str(value).strip().lower() for value in values if str(value).strip()}
    # An ambiguous sentinel/positive combination is not treated as evidence.
    if selected & SURVEY_SENTINEL_ANSWERS:
        return set(), True
    return selected, True


def _survey_has_helper(profile: Dict[str, Any]) -> Optional[bool]:
    value = profile.get("has_caregiver")
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"yes", "true", "1"}


def _survey_candidate_is_eligible(code: str, profile: Dict[str, Any]) -> bool:
    sitting = str(profile.get("sitting_ability") or "").lower()
    arm = str(profile.get("affected_arm_movement") or "").lower()
    hand = str(profile.get("affected_hand_movement") or "").lower()
    mobility = str(profile.get("mobility_level") or "").lower()
    clearance = str(profile.get("standing_exercise_clearance") or "").lower()
    helper = _survey_has_helper(profile)

    seated_safe = sitting == "independent" or (sitting == "needs_support" and helper is not False)
    if code in SURVEY_ARM_EXERCISE_CODES:
        if not seated_safe or arm in {"", "no_movement", "not_sure"}:
            return False
        if arm == "help_only" and helper is False:
            return False
    if code in SURVEY_HAND_EXERCISE_CODES:
        if not seated_safe or hand in {"", "not_affected", "no_movement", "not_sure"}:
            return False
        if hand == "help_only" and helper is False:
            return False
        if code == "PINCH_IMPAIRED" and hand not in {"opens_and_moves", "some_finger_movement"}:
            return False
        if code == "GROSS_GRASP" and hand not in {"opens_and_moves", "some_finger_movement", "very_little_movement"}:
            return False
    if code in SURVEY_LOWER_SEATED_EXERCISE_CODES and not seated_safe:
        return False
    if code in SURVEY_STANDING_EXERCISE_CODES:
        if mobility in {"", "wheelchair", "unable_walk", "not_cleared", "unsure"}:
            return False
        if clearance == "independent":
            return code != "DYNAMIC_BALANCE_IMPAIRED" or mobility in {"independent", "cane", "walker"}
        if clearance == "with_support" and helper is True:
            return code != "DYNAMIC_BALANCE_IMPAIRED"
        return False
    return True


def _round_robin_survey_candidates(
    buckets: Dict[str, List[Tuple[str, str]]],
) -> List[Tuple[str, str, str]]:
    ordered: List[Tuple[str, str, str]] = []
    offsets = {domain: 0 for domain in buckets}
    while any(offsets[domain] < len(buckets[domain]) for domain in buckets):
        for domain in ("arm", "hand", "mobility"):
            if offsets[domain] >= len(buckets[domain]):
                continue
            code, reason = buckets[domain][offsets[domain]]
            offsets[domain] += 1
            ordered.append((code, domain, reason))
    return ordered


def survey_rehab_plan(profile: Optional[Dict[str, Any]] = None) -> List[RehabExercise]:
    """Backward-compatible entry point for the universal core programme.

    Survey and assessment evidence still informs movement reporting, safety
    holds and progress tracking, but it no longer adds or removes these four
    exercises during the current testing policy.
    """
    return fixed_core_rehab_plan(profile)


def survey_interim_rehab_plan(profile: Optional[Dict[str, Any]] = None) -> List[RehabExercise]:
    """Backward-compatible name for callers that previously requested an interim plan."""
    return survey_rehab_plan(profile)


def _clinical_gate_with_core_plan(
    clinical_review_gate: Dict[str, Any],
    plan: Sequence[RehabExercise],
) -> Dict[str, Any]:
    """Expose the universal core plan without claiming evidence selected it."""
    awaiting_analysis = clinical_review_gate.get("status") == "awaiting_model_analysis"
    count = len(plan)
    return {
        **clinical_review_gate,
        "status": "awaiting_model_analysis" if awaiting_analysis else "clear",
        "rehab_access": "interim" if awaiting_analysis else "allowed",
        "rehab_plan_source": "fixed_core_programme",
        "reason_code": "fixed_core_programme",
        "therapist_confirmation_required": False,
        "interim_plan_available": awaiting_analysis,
        "patient_title": "Your rehab plan is ready",
        "patient_message": (
            f"Your current {count}-exercise core programme is ready. Survey and movement results inform "
            "your report and safety checks, while exercise-session frequency sets the starting difficulty."
        ),
        "next_step": "Review the safety notes and complete today's exercises at the suggested level.",
    }


def _clinical_gate_with_survey_hold(
    clinical_review_gate: Dict[str, Any],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    """Explain why reported problems did not produce an automatic active plan."""
    pain = str(profile.get("movement_pain") or "").lower()
    selected_problems = set()
    for key in (
        "arm_activity_difficulties",
        "hand_activity_difficulties",
        "mobility_activity_difficulties",
    ):
        values, _ = _survey_selected_values(profile, key)
        selected_problems.update(values)
    if pain == "severe_or_worsening":
        reason_code = "survey_severe_or_worsening_pain"
        message = (
            "You reported severe or worsening pain with movement, so automatic exercises are paused. "
            "This plan should not start until you have clinical advice."
        )
    elif selected_problems:
        reason_code = "survey_problem_has_no_safe_automatic_match"
        message = (
            "You reported a functional difficulty, but the sitting, movement, helper, or standing-clearance "
            "answers do not support a safe automatic exercise match."
        )
    else:
        return clinical_review_gate
    return {
        **clinical_review_gate,
        "status": "therapist_confirmation_required",
        "rehab_access": "blocked",
        "reason_code": reason_code,
        "therapist_confirmation_required": True,
        "rehab_plan_source": "fixed_core_programme",
        "patient_title": "Please check with your rehabilitation clinician",
        "patient_message": message,
        "next_step": "Ask your physiotherapist or rehabilitation clinician what movement is safe before starting exercises.",
    }


def _assessment_with_current_rehab_policy(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Present saved assessments with the current universal core programme."""
    current = _assessment_with_rough_walking_score(doc)
    profile = dict(current.get("patient_parameters") or {})
    plan = fixed_core_rehab_plan(profile)
    gate = dict(current.get("clinical_review_gate") or {})
    pain_hold = str(profile.get("movement_pain") or "").lower() == "severe_or_worsening"
    access = str(gate.get("rehab_access") or "allowed")
    if pain_hold:
        gate = _clinical_gate_with_survey_hold(gate, profile)
    elif access in {"allowed", "interim", "not_needed"} or not gate:
        gate = _clinical_gate_with_core_plan(gate, plan)
    else:
        gate = {**gate, "rehab_plan_source": "fixed_core_programme"}
    current["rehab_plan"] = [exercise.model_dump() for exercise in plan]
    current["clinical_review_gate"] = gate
    return current


def build_rehab_plan(
    issues: List[FunctionalIssue],
    patient_parameters: Optional[Dict[str, Any]] = None,
) -> List[RehabExercise]:
    seen = set()
    plan: List[RehabExercise] = []
    upper_mmt = _clinical_grade(patient_parameters, "MMT_UPPER_LIMB", "MMT_UPPER", "UL_MMT")
    mas = _clinical_grade(patient_parameters, "MAS", "ASHWORTH", "MODIFIED_ASHWORTH")
    priorities = (patient_parameters or {}).get("patient_priorities") or []
    if isinstance(priorities, str):
        priorities = [priorities]
    priority = str(priorities[0]).strip() if priorities else ""
    for issue in issues:
        if issue.code == "NO_ISSUES":
            continue
        rehab_code = PHENOTYPE_REHAB_ALIASES.get(issue.code, issue.code)
        ex = EXERCISE_LIBRARY.get(rehab_code)
        if ex and ex.id not in seen:
            sets, reps, frequency = ex.sets, ex.reps, ex.frequency
            is_upper = issue.related_task.startswith("T") or issue.code in {
                "TRUNK_COMP", "SHOULDER_HIKE", "REACH_INCOMPLETE", "SHOULDER_FLEX_LIMITED",
                "H2M_IMPAIRED", "GROSS_GRASP", "HAND_OPENING", "PINCH_IMPAIRED", "BILATERAL_NONUSE",
            }
            safety_notes = ["Use a pain-free range and stop for new shoulder pain, marked fatigue, dizziness, or loss of sitting balance."]
            if issue.severity == "severe":
                sets, reps = min(sets, 2), min(reps, 6)
                frequency = "Therapist-supervised starting dose"
                safety_notes.append("The severe task failure requires hands-on safety and assistance-level confirmation before home practice.")
            if is_upper and upper_mmt is not None and upper_mmt <= 2:
                sets, reps = min(sets, 2), min(reps, 8)
                frequency = "Daily with therapist-approved assistance"
                safety_notes.append("MMT 0-2/5 requires gravity-reduced or active-assisted practice; do not add resistance automatically.")
            if is_upper and mas is not None and mas >= 2:
                sets, reps = min(sets, 2), min(reps, 8)
                safety_notes.append("MAS 2 or above requires tone, passive ROM, skin and positioning review before resistance or prolonged stretch.")
            reason = f"Selected because {issue.label.lower()} was identified in task {issue.related_task}"
            if issue.related_step:
                reason += f", step {issue.related_step}"
            if priority:
                reason += f"; progression should support the patient's priority: {priority}"
            plan.append(ex.model_copy(update={
                "targets_issue": issue.code,
                "sets": sets,
                "reps": reps,
                "frequency": frequency,
                "linked_goal": priority or None,
                "selection_reason": reason + ".",
                "safety_note": " ".join(safety_notes),
                "requires_clinician_confirmation": False,
            }))
            seen.add(ex.id)
    return plan


def _merge_targeted_rehab_plan(
    new_plan: List[RehabExercise],
    latest_assessment: Optional[Dict[str, Any]],
) -> List[RehabExercise]:
    """Add a targeted new-domain plan without dramatically replacing active exercises."""
    merged: List[RehabExercise] = []
    seen = set()
    for exercise in [*new_plan, *((latest_assessment or {}).get("rehab_plan") or [])]:
        try:
            model = exercise if isinstance(exercise, RehabExercise) else RehabExercise(**exercise)
        except (TypeError, ValueError):
            continue
        if model.id in seen:
            continue
        merged.append(model.model_copy(update={"requires_clinician_confirmation": False}))
        seen.add(model.id)
        if len(merged) >= 6:
            break
    return merged


def merge_validated_model_issues(
    camera_issues: List[FunctionalIssue],
    model_outputs: Dict[str, Any],
) -> List[FunctionalIssue]:
    """Merge trusted model findings without relabeling pending output as evidence."""
    raw_findings = list(model_outputs.get("functional_findings") or [])
    if not raw_findings:
        return list(camera_issues)
    merged = [issue for issue in camera_issues if issue.code != "NO_ISSUES"]
    seen = {(issue.code, issue.related_task) for issue in merged}
    for index, raw in enumerate(raw_findings):
        if not isinstance(raw, dict):
            continue
        related_tasks = raw.get("related_tasks") or []
        if isinstance(related_tasks, str):
            related_tasks = [related_tasks]
        related_task = str(related_tasks[0]) if related_tasks else str(raw.get("related_task") or "MODEL")
        code = str(raw.get("code") or f"MODEL_FINDING_{index + 1}")
        if (code, related_task) in seen:
            continue
        severity = str(raw.get("severity") or "moderate").lower()
        if severity not in {"mild", "moderate", "severe"}:
            severity = "moderate"
        merged.append(FunctionalIssue(
            code=code,
            label=str(raw.get("label") or "Movement pattern requiring review"),
            description=str(raw.get("description") or raw.get("interpretation") or "The validated musculoskeletal model identified a movement pattern requiring clinician review."),
            source="Validated musculoskeletal model",
            severity=severity,
            related_task=related_task,
            related_step=None,
            phenotype_domain=str(raw.get("domain") or raw.get("package") or "model_finding"),
        ))
        seen.add((code, related_task))
    return merged


# ============ Routes ============
@api_router.get("/")
async def root():
    return {
        "message": "NeuroMotion Stroke Rehab API",
        "release": os.environ.get("RENDER_GIT_COMMIT", "local"),
    }


@api_router.get("/health/db")
async def database_health():
    """Probe authenticated reads, including while the request circuit is open."""
    result = await _probe_patient_database()
    return JSONResponse(status_code=200 if result["ok"] else 503, content=result)


def _database_error_code(error: BaseException) -> str:
    message = str(error).lower()
    if "ssl" in message or "tls" in message:
        return "tls_connection_failed"
    if "authentication" in message or getattr(error, "code", None) == 18:
        return "database_authentication_failed"
    if "not authorized" in message or getattr(error, "code", None) == 13:
        return "database_permission_denied"
    if "dns" in message or "nxdomain" in message:
        return "database_dns_failed"
    return "database_unreachable"


_MONGO_PROBE_TASK: Optional[asyncio.Task] = None
_MONGO_RECOVERY_TASK: Optional[asyncio.Task] = None


async def _run_patient_database_probe() -> Dict[str, Any]:
    started = time.monotonic()
    try:
        # A ping alone does not prove that the app can authenticate and read
        # its account collection. Never return the document from this probe.
        async def authenticated_read():
            await client.admin.command("ping")
            await _mongo_database.users.find_one({}, {"_id": 1})

        await asyncio.wait_for(authenticated_read(), timeout=MONGO_SERVER_SELECTION_TIMEOUT_MS / 1000 + 2)
        MONGO_CIRCUIT.clear()
        return {
            "ok": True, "state": "connected", "store": "mongodb",
            "ms": round((time.monotonic() - started) * 1000),
            "last_success_at": MONGO_CIRCUIT.last_success_at,
        }
    except Exception as error:
        MONGO_CIRCUIT.trip(error)
        return {
            "ok": False, "state": "unreachable", "store": "mongodb",
            "ms": round((time.monotonic() - started) * 1000),
            "error_code": _database_error_code(error),
            "retry_in_s": 5,
            "last_success_at": MONGO_CIRCUIT.last_success_at,
        }


async def _probe_patient_database() -> Dict[str, Any]:
    global _MONGO_PROBE_TASK
    # Concurrent health checks share one probe; a disconnected HTTP client
    # must not cancel the database recovery probe for everyone else.
    if _MONGO_PROBE_TASK is None or _MONGO_PROBE_TASK.done():
        _MONGO_PROBE_TASK = asyncio.create_task(_run_patient_database_probe())
    return await asyncio.shield(_MONGO_PROBE_TASK)


async def _maintain_patient_database_connection() -> None:
    indexes_ready = False
    while True:
        result = await _probe_patient_database()
        if result["ok"]:
            if not indexes_ready:
                indexes_ready = await _ensure_user_indexes()
            await _sync_pending_assessments_to_mongodb()
        await asyncio.sleep(30 if result["ok"] else 5)


@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_obj = StatusCheck(**input.dict())
    await db.status_checks.insert_one(status_obj.dict())
    return status_obj


@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    docs = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    return [StatusCheck(**d) for d in docs]


@api_router.get("/assessment/tasks")
async def get_tasks(
    request: Request,
    package: str = "upper_limb",
    task_ids: Optional[str] = None,
    library_test: bool = False,
):
    selected = ASSESSMENT_PACKAGES.get(package, ASSESSMENT_PACKAGES["upper_limb"])
    if is_local_assessment_preview(request):
        requested = [item.strip() for item in task_ids.split(",") if item.strip()] if task_ids is not None else None
        if library_test and (requested is None or len(requested) != 1):
            raise HTTPException(status_code=422, detail="Library testing requires exactly one assessment task")
        selected_ids = _validated_assigned_task_ids(selected["id"], requested)
        return {
            "tasks": [task for task in selected["tasks"] if task["id"] in selected_ids],
            "voice_id": TTS_VOICE, "package_id": selected["id"],
            "package_title": selected["title"], "package_subtitle": selected["subtitle"],
            "assigned_task_ids": selected_ids, "packages": [], "preview_only": True,
        }
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    requested = [item.strip() for item in task_ids.split(",") if item.strip()] if task_ids is not None else None
    if library_test:
        if requested is None or len(requested) != 1:
            raise HTTPException(status_code=422, detail="Library testing requires exactly one assessment task")
        selected_ids = _validated_assigned_task_ids(selected["id"], requested)
        access = {"trigger": "settings_library_test", "issue_report_id": None}
    else:
        access = await _assessment_access_plan(user, selected["id"], requested)
        selected_ids = _validated_assigned_task_ids(selected["id"], list(access.get("task_ids") or []))
    selected_tasks = [task for task in selected["tasks"] if task["id"] in set(selected_ids)]
    _record_alira_action(
        "assessment_tasks_served",
        source="assessment_runner",
        user_id=user["id"],
        details={
            "package_id": selected["id"],
            "task_ids": [task["id"] for task in selected_tasks],
            "selection_trigger": access.get("trigger"),
            "issue_report_id": access.get("issue_report_id"),
            "task_order": "single_settings_library_test" if library_test else "alira_selected_approved_order",
            "library_test": library_test,
        },
    )
    packages = [
        {
            "id": item["id"],
            "title": item["title"],
            "subtitle": item["subtitle"],
            "task_count": len(item["tasks"]),
        }
        for item in ASSESSMENT_PACKAGES.values()
    ]
    return {
        "tasks": selected_tasks,
        "voice_id": TTS_VOICE,
        "package_id": selected["id"],
        "package_title": selected["title"],
        "package_subtitle": selected["subtitle"],
        "assigned_task_ids": [task["id"] for task in selected_tasks],
        "packages": packages,
    }


@api_router.get("/assessment/recommendation")
async def get_assessment_recommendation(request: Request, package: str = "initial"):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    if package != "initial":
        raise HTTPException(status_code=422, detail="Capability screening is currently available for the initial assessment only")
    recommendation = initial_assessment_recommendation(user.get("profile") or {})
    functional_profile = recommendation.get("functional_profile") or {}
    _record_alira_action(
        "assessment_tasks_selected",
        source="assessment_recommendation",
        user_id=user["id"],
        status="completed" if recommendation.get("can_start") else "blocked",
        details={
            "package_id": package,
            "selection_policy": "saved_readiness_survey",
            "functional_profile_id": functional_profile.get("id"),
            "functional_profile_label": functional_profile.get("label"),
            "functional_profile_rationale": functional_profile.get("rationale") or [],
            "reported_domains": functional_profile.get("reported_domains") or [],
            "assessment_domains": functional_profile.get("assessment_domains") or [],
            "candidate_task_ids": functional_profile.get("candidate_task_ids") or [],
            "task_ids": recommendation.get("task_ids") or [],
            "excluded_task_ids": [
                task_id
                for exclusion in recommendation.get("excluded") or []
                for task_id in exclusion.get("task_ids") or []
            ],
            "readiness_status": recommendation.get("status"),
            "requires_helper": recommendation.get("requires_helper", False),
            "helper_assisted_task_ids": recommendation.get("helper_assisted_task_ids") or [],
            "helper_confirmation_required": recommendation.get("helper_confirmation_required", False),
            "exclusion_reasons": [
                str(exclusion.get("reason") or "")
                for exclusion in recommendation.get("excluded") or []
            ],
        },
    )
    return recommendation


@api_router.get("/assessment/modeling-spec")
async def get_modeling_specification():
    return modeling_specification()


def _safe_video_token(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_")
    return (cleaned or fallback)[:80]


def _local_task_video_metadata() -> List[Dict[str, Any]]:
    if not TASK_VIDEO_FALLBACK_DIR.exists():
        return []
    records: List[Dict[str, Any]] = []
    for metadata_path in TASK_VIDEO_FALLBACK_DIR.glob("*.json"):
        try:
            import json
            records.append(json.loads(metadata_path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return records


def _write_local_task_video(data: bytes, metadata: Dict[str, Any]) -> Dict[str, Any]:
    import json

    TASK_VIDEO_FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    existing = [
        item for item in _local_task_video_metadata()
        if item.get("user_id") == metadata["user_id"]
        and item.get("package_id") == metadata["package_id"]
        and item.get("task_id") == metadata["task_id"]
    ]
    for item in existing:
        for suffix in (".bin", ".json"):
            try:
                (TASK_VIDEO_FALLBACK_DIR / f"{item['id']}{suffix}").unlink(missing_ok=True)
            except Exception:
                pass
    video_id = "local_" + uuid.uuid4().hex
    record = {**metadata, "id": video_id, "size_bytes": len(data), "storage": "local"}
    (TASK_VIDEO_FALLBACK_DIR / f"{video_id}.bin").write_bytes(data)
    (TASK_VIDEO_FALLBACK_DIR / f"{video_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def _write_local_task_video_metadata(record: Dict[str, Any]) -> Dict[str, Any]:
    TASK_VIDEO_FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    video_id = str(record["id"])
    (TASK_VIDEO_FALLBACK_DIR / f"{video_id}.json").write_text(
        json.dumps(record, ensure_ascii=True), encoding="utf-8"
    )
    return record


async def _task_video_user(request: Request, uid: str = "") -> Optional[Dict[str, Any]]:
    headers = dict(request.headers)
    if uid and not (headers.get("x-user-id") or headers.get("X-User-Id")):
        headers["x-user-id"] = uid
    return await _user_from_header(headers)


async def _latest_task_videos(user_id: str, package_id: str, task_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not user_id or not task_ids:
        return {}
    wanted = set(task_ids)
    records: List[Dict[str, Any]] = []
    try:
        docs = await db.task_videos.files.find({
            "metadata.user_id": user_id,
            "metadata.package_id": package_id,
            "metadata.task_id": {"$in": list(wanted)},
        }).sort("uploadDate", -1).to_list(100)
        records = [
            {"id": str(doc["_id"]), **(doc.get("metadata") or {})}
            for doc in docs
        ]
        object_docs = await db.task_video_objects.find({
            "user_id": user_id,
            "package_id": package_id,
            "task_id": {"$in": list(wanted)},
        }, {"_id": 0}).sort("created_at", -1).to_list(100)
        records.extend(object_docs)
    except Exception as e:
        _require_durable_patient_store("assessment video lookup", e)
        logger.warning(f"Mongo unavailable for analysis video lookup; using local fallback: {str(e)[:120]}")
        records = [
            item for item in _local_task_video_metadata()
            if item.get("user_id") == user_id
            and item.get("package_id") == package_id
            and item.get("task_id") in wanted
        ]
        records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    latest: Dict[str, Dict[str, Any]] = {}
    for record in records:
        task_id = str(record.get("task_id") or "")
        if task_id and task_id not in latest:
            latest[task_id] = record
    return latest


@api_router.post("/assessment/task-videos/upload-ticket")
async def create_task_video_upload_ticket(
    request: Request,
    package_id: str,
    task_id: str,
    duration_ms: int = 0,
    content_type: str = "video/mp4",
    size_bytes: int = 0,
):
    user = await _task_video_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if not task_video_object_storage.configured:
        raise HTTPException(status_code=503, detail="Direct object upload is not configured")
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if not normalized_type.startswith("video/"):
        raise HTTPException(status_code=415, detail="A video content type is required")
    if size_bytes <= 0 or size_bytes > TASK_VIDEO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Task video size is outside the allowed range")
    safe_package = _safe_video_token(package_id, "assessment")
    safe_task = _safe_video_token(task_id, "task")
    extension = TASK_VIDEO_EXTENSIONS.get(normalized_type, "video")
    video_id = "r2_" + uuid.uuid4().hex
    object_key = task_video_object_storage.object_key(
        user["id"], safe_package, safe_task, video_id, extension
    )
    upload_url = await asyncio.to_thread(
        task_video_object_storage.presign_put, object_key, normalized_type
    )
    return {
        "video_id": video_id,
        "object_key": object_key,
        "upload_url": upload_url,
        "content_type": normalized_type,
        "duration_ms": max(0, int(duration_ms)),
        "expires_seconds": 900,
    }


@api_router.post("/assessment/task-videos/complete")
async def complete_task_video_upload(
    payload: TaskVideoUploadComplete,
    request: Request,
):
    user = await _task_video_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if not task_video_object_storage.configured:
        raise HTTPException(status_code=503, detail="Direct object upload is not configured")
    safe_package = _safe_video_token(payload.package_id, "assessment")
    safe_task = _safe_video_token(payload.task_id, "task")
    expected_prefix = task_video_object_storage.object_key(
        user["id"], safe_package, safe_task, payload.video_id, ""
    ).rstrip(".")
    if not payload.video_id.startswith("r2_") or not payload.object_key.startswith(expected_prefix):
        raise HTTPException(status_code=422, detail="Upload ticket does not belong to this account and task")
    try:
        head = await asyncio.to_thread(task_video_object_storage.head, payload.object_key)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Uploaded video could not be verified") from exc
    actual_size = int(head.get("ContentLength") or 0)
    if actual_size <= 0 or actual_size > TASK_VIDEO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Task video size is outside the allowed range")
    created_at = datetime.now(timezone.utc).isoformat()
    record = {
        "id": payload.video_id,
        "user_id": user["id"],
        "package_id": safe_package,
        "task_id": safe_task,
        "duration_ms": max(0, int(payload.duration_ms)),
        "content_type": payload.content_type.split(";", 1)[0].strip().lower(),
        "created_at": created_at,
        "filename": payload.object_key.rsplit("/", 1)[-1],
        "size_bytes": actual_size,
        "storage": "r2",
        "object_key": payload.object_key,
    }
    try:
        previous = await db.task_video_objects.find({
            "user_id": user["id"],
            "package_id": safe_package,
            "task_id": safe_task,
            "id": {"$ne": payload.video_id},
        }, {"_id": 0, "object_key": 1}).to_list(50)
        await db.task_video_objects.delete_many({
            "user_id": user["id"], "package_id": safe_package, "task_id": safe_task
        })
        await db.task_video_objects.insert_one(record.copy())
        for item in previous:
            if item.get("object_key"):
                try:
                    await asyncio.to_thread(task_video_object_storage.delete, item["object_key"])
                except Exception:
                    logger.warning("Could not delete replaced R2 task video %s", item["object_key"])
    except Exception as exc:
        _require_durable_patient_store("assessment video metadata", exc)
        logger.warning(f"Mongo unavailable for R2 video metadata; using local fallback: {str(exc)[:120]}")
        _write_local_task_video_metadata(record)
    return record


@api_router.post("/assessment/task-videos")
async def save_task_video(
    request: Request,
    package_id: str,
    task_id: str,
    duration_ms: int = 0,
):
    user = await _task_video_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    content_type = request.headers.get("content-type", "video/webm").split(";", 1)[0].strip().lower()
    if not content_type.startswith("video/"):
        raise HTTPException(status_code=415, detail="A video content type is required")
    content_length = int(request.headers.get("content-length") or 0)
    if content_length > TASK_VIDEO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Task video is too large")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Task video is empty")
    if len(data) > TASK_VIDEO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Task video is too large")

    safe_package = _safe_video_token(package_id, "assessment")
    safe_task = _safe_video_token(task_id, "task")
    extension = TASK_VIDEO_EXTENSIONS.get(content_type, "video")
    created_at = datetime.now(timezone.utc).isoformat()
    metadata = {
        "user_id": user["id"],
        "package_id": safe_package,
        "task_id": safe_task,
        "duration_ms": max(0, int(duration_ms)),
        "content_type": content_type,
        "created_at": created_at,
    }
    filename = f"{safe_package}_{safe_task}_{uuid.uuid4().hex[:10]}.{extension}"
    try:
        video_id = await task_video_bucket.upload_from_stream(
            filename,
            io.BytesIO(data),
            metadata=metadata,
        )
        previous = await db.task_videos.files.find({
            "metadata.user_id": user["id"],
            "metadata.package_id": safe_package,
            "metadata.task_id": safe_task,
            "_id": {"$ne": video_id},
        }).to_list(50)
        for item in previous:
            await task_video_bucket.delete(item["_id"])
        return {
            "id": str(video_id),
            **metadata,
            "filename": filename,
            "size_bytes": len(data),
            "storage": "gridfs",
        }
    except Exception as e:
        _require_durable_patient_store("assessment video upload", e)
        logger.warning(f"Mongo unavailable for task video; using local fallback: {str(e)[:120]}")
        return _write_local_task_video(data, {**metadata, "filename": filename})


@api_router.get("/assessment/task-videos")
async def list_task_videos(request: Request, package: str = "initial"):
    user = await _task_video_user(request)
    if not user:
        return {"videos": []}
    safe_package = _safe_video_token(package, "assessment")
    records: List[Dict[str, Any]] = []
    try:
        docs = await db.task_videos.files.find({
            "metadata.user_id": user["id"],
            "metadata.package_id": safe_package,
        }).sort("uploadDate", -1).to_list(100)
        records = [
            {
                "id": str(doc["_id"]),
                **(doc.get("metadata") or {}),
                "filename": doc.get("filename", "task-video"),
                "size_bytes": int(doc.get("length") or 0),
                "storage": "gridfs",
            }
            for doc in docs
        ]
        object_docs = await db.task_video_objects.find({
            "user_id": user["id"], "package_id": safe_package
        }, {"_id": 0}).sort("created_at", -1).to_list(100)
        records.extend(object_docs)
    except Exception as e:
        _require_durable_patient_store("assessment video history", e)
        logger.warning(f"Mongo unavailable for task video list; using local fallback: {str(e)[:120]}")
        records = [
            item for item in _local_task_video_metadata()
            if item.get("user_id") == user["id"] and item.get("package_id") == safe_package
        ]
        records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"videos": records}


def _task_progress_key(user_id: str, package_id: str, task_id: str) -> str:
    return f"{user_id}:{package_id}:{task_id}"


def _valid_package_task_ids(package_id: str) -> List[str]:
    package = ASSESSMENT_PACKAGES.get(package_id)
    if not package:
        raise HTTPException(status_code=404, detail="Assessment package not found")
    return [str(task.get("id") or "") for task in package.get("tasks") or []]


@api_router.post("/assessment/task-progress")
async def save_task_progress(request: Request, package_id: str, task_id: str):
    user = await _task_video_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    safe_package = _safe_video_token(package_id, "assessment")
    valid_task_ids = _valid_package_task_ids(safe_package)
    if task_id not in valid_task_ids:
        raise HTTPException(status_code=422, detail="Task does not belong to this assessment package")
    record = {
        "user_id": user["id"],
        "package_id": safe_package,
        "task_id": task_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await db.assessment_task_progress.delete_one(
            {"user_id": user["id"], "package_id": safe_package, "task_id": "__reset__"}
        )
        await db.assessment_task_progress.update_one(
            {"user_id": user["id"], "package_id": safe_package, "task_id": task_id},
            {"$set": record},
            upsert=True,
        )
    except Exception as exc:
        _require_durable_patient_store("assessment task progress", exc)
        logger.warning(f"Mongo unavailable for task progress; using local fallback: {str(exc)[:120]}")
        LOCAL_TASK_PROGRESS.pop(_task_progress_key(user["id"], safe_package, "__reset__"), None)
        LOCAL_TASK_PROGRESS[_task_progress_key(user["id"], safe_package, task_id)] = record
        _persist_local_dict(LOCAL_TASK_PROGRESS_FILE, LOCAL_TASK_PROGRESS)
    _record_alira_action(
        "guided_assessment_task_completed",
        source="assessment_runner",
        user_id=user["id"],
        details={"package_id": safe_package, "task_id": task_id},
    )
    return {"ok": True, **record}


@api_router.get("/assessment/task-progress")
async def get_task_progress(request: Request, package: str = "initial"):
    user = await _task_video_user(request)
    if not user:
        return {"completed_task_ids": []}
    safe_package = _safe_video_token(package, "assessment")
    valid_task_ids = _valid_package_task_ids(safe_package)
    try:
        docs = await db.assessment_task_progress.find(
            {"user_id": user["id"], "package_id": safe_package},
            {"_id": 0, "task_id": 1},
        ).to_list(200)
        reset_recorded = any(str(item.get("task_id") or "") == "__reset__" for item in docs)
        completed = {str(item.get("task_id") or "") for item in docs}
    except Exception as exc:
        _require_durable_patient_store("assessment task progress lookup", exc)
        logger.warning(f"Mongo unavailable for task progress lookup; using local fallback: {str(exc)[:120]}")
        reset_recorded = any(
            item.get("user_id") == user["id"]
            and item.get("package_id") == safe_package
            and item.get("task_id") == "__reset__"
            for item in LOCAL_TASK_PROGRESS.values()
        )
        completed = {
            str(item.get("task_id") or "")
            for item in LOCAL_TASK_PROGRESS.values()
            if item.get("user_id") == user["id"] and item.get("package_id") == safe_package
        }
    if not completed.intersection(valid_task_ids) and not reset_recorded:
        # One-time compatibility for task videos saved before explicit progress records existed.
        completed.update((await _latest_task_videos(user["id"], safe_package, valid_task_ids)).keys())
    return {"completed_task_ids": [task_id for task_id in valid_task_ids if task_id in completed]}


@api_router.delete("/assessment/task-progress")
async def reset_task_progress(request: Request, package: str = "initial"):
    user = await _task_video_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    safe_package = _safe_video_token(package, "assessment")
    _valid_package_task_ids(safe_package)
    try:
        await db.assessment_task_progress.delete_many({"user_id": user["id"], "package_id": safe_package})
        await db.assessment_task_progress.insert_one({
            "user_id": user["id"],
            "package_id": safe_package,
            "task_id": "__reset__",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        _require_durable_patient_store("assessment task progress reset", exc)
        logger.warning(f"Mongo unavailable for task progress reset; using local fallback: {str(exc)[:120]}")
        remove_keys = [
            key for key, item in LOCAL_TASK_PROGRESS.items()
            if item.get("user_id") == user["id"] and item.get("package_id") == safe_package
        ]
        for key in remove_keys:
            LOCAL_TASK_PROGRESS.pop(key, None)
        reset_record = {
            "user_id": user["id"],
            "package_id": safe_package,
            "task_id": "__reset__",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        LOCAL_TASK_PROGRESS[_task_progress_key(user["id"], safe_package, "__reset__")] = reset_record
        _persist_local_dict(LOCAL_TASK_PROGRESS_FILE, LOCAL_TASK_PROGRESS)
    _record_alira_action(
        "assessment_progress_reset",
        source="app",
        user_id=user["id"],
        details={"package_id": safe_package},
    )
    return {"ok": True, "completed_task_ids": []}


@api_router.get("/assessment/task-videos/file/{video_id}")
async def get_task_video(video_id: str, request: Request, uid: str = ""):
    user = await _task_video_user(request, uid)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if video_id.startswith("r2_"):
        try:
            record = await db.task_video_objects.find_one(
                {"id": video_id, "user_id": user["id"]}, {"_id": 0}
            )
        except Exception:
            record = next(
                (
                    item for item in _local_task_video_metadata()
                    if item.get("id") == video_id and item.get("user_id") == user["id"]
                ),
                None,
            )
        if not record or not record.get("object_key") or not task_video_object_storage.configured:
            raise HTTPException(status_code=404, detail="Task video not found")
        url = await asyncio.to_thread(task_video_object_storage.presign_get, record["object_key"])
        return RedirectResponse(url, status_code=307)
    if video_id.startswith("local_"):
        record = next((item for item in _local_task_video_metadata() if item.get("id") == video_id), None)
        path = TASK_VIDEO_FALLBACK_DIR / f"{video_id}.bin"
        if not record or record.get("user_id") != user["id"] or not path.exists():
            raise HTTPException(status_code=404, detail="Task video not found")
        return Response(
            content=path.read_bytes(),
            media_type=record.get("content_type", "video/webm"),
            headers={"Content-Disposition": f'inline; filename="{record.get("filename", "task-video.webm")}"'},
        )
    try:
        object_id = ObjectId(video_id)
    except InvalidId:
        raise HTTPException(status_code=404, detail="Task video not found")
    try:
        grid_out = await task_video_bucket.open_download_stream(object_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Task video not found")
    metadata = grid_out.metadata or {}
    if metadata.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="Task video not found")

    async def chunks():
        while True:
            chunk = await grid_out.read(256 * 1024)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        chunks(),
        media_type=metadata.get("content_type", "video/webm"),
        headers={"Content-Disposition": f'inline; filename="{grid_out.filename or "task-video.webm"}"'},
    )


@api_router.get("/assessment/task-videos/worker/{video_id}")
async def get_task_video_for_worker(video_id: str, request: Request):
    """Give the authenticated analysis worker a saved video source."""
    _require_analysis_worker(request)
    if video_id.startswith("r2_"):
        try:
            record = await db.task_video_objects.find_one({"id": video_id}, {"_id": 0})
        except Exception:
            record = next((item for item in _local_task_video_metadata() if item.get("id") == video_id), None)
        if not record or not record.get("object_key") or not task_video_object_storage.configured:
            raise HTTPException(status_code=404, detail="Task video not found")
        url = await asyncio.to_thread(task_video_object_storage.presign_get, record["object_key"])
        return RedirectResponse(url, status_code=307)
    if video_id.startswith("local_"):
        record = next((item for item in _local_task_video_metadata() if item.get("id") == video_id), None)
        path = TASK_VIDEO_FALLBACK_DIR / f"{video_id}.bin"
        if not record or not path.exists():
            raise HTTPException(status_code=404, detail="Task video not found")
        return Response(content=path.read_bytes(), media_type=record.get("content_type", "video/webm"))
    try:
        grid_out = await task_video_bucket.open_download_stream(ObjectId(video_id))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Task video not found") from exc

    async def worker_chunks():
        while True:
            chunk = await grid_out.read(256 * 1024)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        worker_chunks(), media_type=(grid_out.metadata or {}).get("content_type", "video/webm")
    )


# Spoken instructions are generated once and reused. The runner asks for the
# same lines at the start of every exercise (setup voice, calibration
# instruction, every step's voice, "Wonderful, here we go"), so each line is
# kept in memory and on disk keyed by provider, model, voice and text. The provider call
# itself runs in a worker thread: it used to run inline inside the async
# handler, which stalled the whole server for the duration of every voice line
# and made an exercise start wait for six or seven of them in a row.
TTS_CACHE_DIR = LOCAL_STATE_DIR / "tts_cache"
TTS_MEMORY_CACHE_LIMIT = 400
_tts_memory_cache: "OrderedDict[str, str]" = OrderedDict()
_tts_inflight: Dict[str, "asyncio.Future[str]"] = {}

EXERCISE_POSTURE_CHANGED_VOICE = (
    "You are sitting a little differently for this exercise, so I will learn your starting "
    "position again. Please hold still for a moment."
)
EXERCISE_TRANSITION_VOICE = "Wonderful. Here we go."
EXERCISE_ASSISTANCE_QUESTION_VOICE = (
    "One quick question. Did a carer or family member help you move during this exercise? "
    "Please tap yes or no."
)
EXERCISE_ASSISTED_COMPLETE_VOICE = (
    "Thank you for telling me. Working together with your carer still counts, and this session "
    "is recorded as helper supported."
)
EXERCISE_INDEPENDENT_COMPLETE_VOICE = (
    "Magnificent work. You have finished this exercise. I'm so proud of you."
)


OPENAI_TTS_VOICES = {"alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"}


# Alira's delivery. These values mirror shared/alira-voice.ts in the recovery
# companion, so she sounds like the same person on the site, in the assessment
# and in every exercise. Change them in both places together.
ALIRA_VOICE_SETTINGS = {
    "stability": 0.45,
    "similarity_boost": 0.8,
    "style": 0.3,
    "use_speaker_boost": True,
    "speed": 0.95,
}
INSTRUCTION_CLONE_VOICE_SETTINGS = {
    "stability": 0.65,
    "similarity_boost": 0.85,
    "style": 0.0,
    "use_speaker_boost": True,
    "speed": 0.92,
}
# After ElevenLabs refuses a live line (no key, no credits), lines outside the
# recorded pack use the general voice for this long before trying again.
ALIRA_LIVE_VOICE_RETRY_SECONDS = 600
_alira_live_voice_retry_at = 0.0


def _instruction_clone_ready() -> bool:
    if INSTRUCTION_TTS_PROVIDER == "chatterbox-nano":
        return bool(CHATTERBOX_REFERENCE_AUDIO) and Path(CHATTERBOX_REFERENCE_AUDIO).is_file()
    return INSTRUCTION_TTS_PROVIDER == "elevenlabs" and bool(ELEVENLABS_API_KEY) and bool(ELEVENLABS_VOICE_ID)


def _alira_voice_everywhere() -> bool:
    """True when one designed ElevenLabs voice speaks every line as Alira.

    The API key is not required here: lines recorded ahead of time in
    PREPARED_TTS_DIR play without it, and only live lines need credits.
    """
    return (
        ELEVENLABS_VOICE_SCOPE == "all"
        and INSTRUCTION_TTS_PROVIDER == "elevenlabs"
        and bool(ELEVENLABS_VOICE_ID)
    )


def _openai_tts_request_config(requested_voice: Optional[str] = None) -> Dict[str, str]:
    voice = requested_voice if requested_voice in OPENAI_TTS_VOICES else TTS_VOICE
    provider = "openai-direct" if openai_tts_client else "openai-emergent" if tts_client else "unavailable"
    return {
        "provider": provider,
        "model": TTS_MODEL,
        "voice": voice,
        "public_voice": voice,
        "output_format": "mp3",
    }


def _tts_request_config(purpose: str, requested_voice: Optional[str] = None) -> Dict[str, str]:
    if purpose == "instruction" and INSTRUCTION_TTS_PROVIDER == "chatterbox-nano":
        if not _instruction_clone_ready():
            return {"provider": "unavailable", "model": "chatterbox-nano", "voice": "molly", "public_voice": "Molly", "output_format": "mp3"}
        reference_digest = hashlib.sha256(Path(CHATTERBOX_REFERENCE_AUDIO).read_bytes()).hexdigest()[:16]
        return {
            "provider": "chatterbox-nano",
            "model": "chatterbox-nano",
            "voice": f"molly-{reference_digest}",
            "public_voice": "Molly",
            "output_format": "mp3-loudnorm-v1",
        }
    if _alira_voice_everywhere() or (purpose == "instruction" and _instruction_clone_ready()):
        return {
            "provider": "elevenlabs",
            "model": ELEVENLABS_TTS_MODEL,
            "voice": ELEVENLABS_VOICE_ID,
            "public_voice": "alira" if _alira_voice_everywhere() else "custom-cloned-voice",
            "output_format": ELEVENLABS_OUTPUT_FORMAT,
        }
    return _openai_tts_request_config(requested_voice)


def _tts_cache_key(text: str, voice: str, purpose: str = "general") -> str:
    config = _tts_request_config(purpose, voice)
    if config["provider"] not in {"elevenlabs", "chatterbox-nano"}:
        # Preserve existing OpenAI hashes so the committed nova assets keep
        # working whenever the cloned instruction voice is not enabled.
        material = f"{config['model']}|{config['voice']}|{text}"
    else:
        parts = [
            config["provider"],
            config["model"],
            config["voice"],
            config["output_format"],
        ]
        if config["provider"] == "elevenlabs" and _alira_voice_everywhere():
            parts.append(json.dumps(ALIRA_VOICE_SETTINGS, sort_keys=True))
        material = "|".join((*parts, text))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _prepared_tts_asset_url(text: str, voice: Optional[str] = None) -> Optional[str]:
    key = _tts_cache_key(text, voice or TTS_VOICE, purpose="instruction")
    path = PREPARED_TTS_DIR / f"{key}.mp3"
    return f"/audio/prepared/{key}.mp3" if path.is_file() else None


def _tts_cache_get(key: str) -> Optional[str]:
    cached = _tts_memory_cache.get(key)
    if cached is not None:
        _tts_memory_cache.move_to_end(key)
        return cached
    path = TTS_CACHE_DIR / f"{key}.b64"
    try:
        if path.is_file():
            cached = path.read_text(encoding="ascii")
            if cached:
                _tts_cache_put(key, cached, persist=False)
                return cached
    except OSError:
        pass
    return None


def _alira_recorded_audio_base64(key: str) -> Optional[str]:
    """A line recorded ahead of time in Alira's voice. It ships with the app, so
    it needs no provider call, no API key and no credits."""
    prepared = PREPARED_TTS_DIR / f"{key}.mp3"
    try:
        if prepared.is_file():
            audio_b64 = base64.b64encode(prepared.read_bytes()).decode("ascii")
            if audio_b64:
                _tts_cache_put(key, audio_b64, persist=False)
                return audio_b64
    except OSError:
        pass
    return None


def _tts_cache_put(key: str, audio_b64: str, persist: bool = True) -> None:
    _tts_memory_cache[key] = audio_b64
    _tts_memory_cache.move_to_end(key)
    while len(_tts_memory_cache) > TTS_MEMORY_CACHE_LIMIT:
        _tts_memory_cache.popitem(last=False)
    if persist:
        try:
            TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (TTS_CACHE_DIR / f"{key}.b64").write_text(audio_b64, encoding="ascii")
        except OSError:
            pass


def _synthesize_tts_audio_bytes(text: str, voice: str, purpose: str = "general") -> bytes:
    """Blocking provider call - always run through asyncio.to_thread."""
    config = _tts_request_config(purpose, voice)
    if config["provider"] == "chatterbox-nano":
        from backend.chatterbox_nano_tts import synthesize_mp3

        return synthesize_mp3(text, Path(CHATTERBOX_REFERENCE_AUDIO), CHATTERBOX_FFMPEG_PATH)
    if config["provider"] == "elevenlabs":
        if not ELEVENLABS_API_KEY:
            raise RuntimeError("ElevenLabs API key is not configured for live speech")
        response = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{config['voice']}",
            params={"output_format": config["output_format"]},
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": config["model"],
                "voice_settings": dict(
                    ALIRA_VOICE_SETTINGS if _alira_voice_everywhere() else INSTRUCTION_CLONE_VOICE_SETTINGS
                ),
            },
            timeout=90,
        )
        response.raise_for_status()
        return response.content
    return _synthesize_openai_tts_audio_bytes(text, config)


def _synthesize_openai_tts_audio_bytes(text: str, config: Dict[str, str]) -> bytes:
    """Blocking OpenAI call - always run through asyncio.to_thread."""
    if config["provider"] != "openai-direct" or not openai_tts_client:
        raise RuntimeError("Direct text-to-speech provider is not configured")
    response = openai_tts_client.audio.speech.create(
        model=config["model"],
        voice=config["voice"],
        input=text,
        response_format="mp3",
    )
    if hasattr(response, "read"):
        return response.read()
    if hasattr(response, "content"):
        return response.content
    return bytes(response)


async def _generate_tts_audio_base64(text: str, voice: str, purpose: str = "general") -> str:
    config = _tts_request_config(purpose, voice)
    if config["provider"] == "unavailable":
        raise HTTPException(status_code=503, detail="Voice service unavailable: no text-to-speech provider is configured.")
    key = _tts_cache_key(text, voice, purpose)
    cached = _tts_cache_get(key)
    if cached is None and config["provider"] == "elevenlabs" and _alira_voice_everywhere():
        cached = _alira_recorded_audio_base64(key)
    if cached is not None:
        return cached
    # Several runner prefetches for the same line arrive together: generate once.
    inflight = _tts_inflight.get(key)
    if inflight is not None:
        return await asyncio.shield(inflight)
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[str]" = loop.create_future()
    _tts_inflight[key] = future
    try:
        keep = True
        if config["provider"] == "elevenlabs" and _alira_voice_everywhere():
            audio_b64, keep = await _alira_live_tts_audio_base64(text, voice, purpose)
        elif config["provider"] in {"openai-direct", "elevenlabs", "chatterbox-nano"}:
            audio_bytes = await asyncio.to_thread(_synthesize_tts_audio_bytes, text, voice, purpose)
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        else:
            audio_b64 = await tts_client.generate_speech_base64(
                text=text,
                model=config["model"],
                voice=config["voice"],
                response_format="mp3",
            )
        if keep:
            _tts_cache_put(key, audio_b64)
        if not future.done():
            future.set_result(audio_b64)
        return audio_b64
    except BaseException as error:
        if not future.done():
            future.set_exception(error)
        raise
    finally:
        _tts_inflight.pop(key, None)


async def _alira_live_tts_audio_base64(text: str, voice: str, purpose: str) -> Tuple[str, bool]:
    """Speak a line that is not in the recorded pack.

    Returns the audio and whether it is Alira's own voice (and so worth caching).
    Without a key or credits the general voice speaks the line, so a patient is
    never left in silence mid-exercise; that audio is not cached, and Alira's
    voice is tried again after ALIRA_LIVE_VOICE_RETRY_SECONDS.
    """
    global _alira_live_voice_retry_at
    failure: Optional[BaseException] = None
    if time.monotonic() >= _alira_live_voice_retry_at:
        try:
            audio_bytes = await asyncio.to_thread(_synthesize_tts_audio_bytes, text, voice, purpose)
            return base64.b64encode(audio_bytes).decode("ascii"), True
        except Exception as error:  # noqa: BLE001 - any provider failure takes the same fallback
            failure = error
            status = getattr(getattr(error, "response", None), "status_code", None)
            if not ELEVENLABS_API_KEY or status in {401, 402, 403, 429}:
                _alira_live_voice_retry_at = time.monotonic() + ALIRA_LIVE_VOICE_RETRY_SECONDS
            logger.warning("Alira's ElevenLabs voice could not speak a live line: %s", str(error)[:200])
    fallback = _openai_tts_request_config(voice)
    if fallback["provider"] == "openai-direct":
        audio_bytes = await asyncio.to_thread(_synthesize_openai_tts_audio_bytes, text, fallback)
        return base64.b64encode(audio_bytes).decode("ascii"), False
    if fallback["provider"] == "openai-emergent":
        audio_b64 = await tts_client.generate_speech_base64(
            text=text,
            model=fallback["model"],
            voice=fallback["voice"],
            response_format="mp3",
        )
        return audio_b64, False
    if failure is not None:
        raise failure
    raise RuntimeError("Alira's voice is resting after a provider refusal and no fallback voice is configured")


@lru_cache(maxsize=1)
def _instruction_text_catalog() -> frozenset[str]:
    allowed: set[str] = {
        EXERCISE_POSTURE_CHANGED_VOICE,
        EXERCISE_TRANSITION_VOICE,
        EXERCISE_ASSISTANCE_QUESTION_VOICE,
        EXERCISE_ASSISTED_COMPLETE_VOICE,
        EXERCISE_INDEPENDENT_COMPLETE_VOICE,
    }
    voice_fields = {"voice", "setup_voice", "calibration_instruction", "coaching_cue"}

    def collect(value: Any, field: str = "") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                collect(nested, str(key))
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested, field)
        elif isinstance(value, str) and field in voice_fields and value.strip():
            allowed.add(value.strip())

    collect(globals().get("ASSESSMENT_PACKAGES") or {})
    collect(globals().get("REHAB_RUNNER_CONFIG") or {})
    configure_runner = globals().get("_configure_rehab_runner")
    runner_configs = globals().get("REHAB_RUNNER_CONFIG") or {}
    if callable(configure_runner):
        for exercise_id in runner_configs:
            for difficulty in ("easy", "medium", "difficult"):
                for variation in ("standard", "alternate"):
                    collect(configure_runner(exercise_id, difficulty, variation))
    return frozenset(allowed)


def _instruction_text_allowed(text: str) -> bool:
    """Limit a configured voice clone to app-authored rehabilitation speech."""
    candidate = str(text or "").strip()
    if not candidate or len(candidate) > 2000:
        return False

    # Fixed runner speech is embedded in these server-authored templates.
    for template_name in ("POSE_RUNNER_HTML", "REHAB_RUNNER_HTML_TEMPLATE"):
        template = globals().get(template_name)
        if isinstance(template, str) and candidate in template:
            return True
    for side in ("left", "right"):
        if candidate in {
            f"Let us check a small movement first. Without help, gently move your {side} hand or arm away from its resting position, as much as is comfortable. Even a small movement is useful.",
            f"I have not seen a clear movement yet. If comfortable, try a small movement with your {side} hand or arm once more. Take your time; do not force it.",
        }:
            return True
    return candidate in _instruction_text_catalog()


@api_router.post("/tts/generate", response_model=TTSResponse)
async def generate_tts(req: TTSRequest):
    # Instruction requests use the server-configured clone. The client cannot
    # select arbitrary ElevenLabs voice IDs or access the provider credential.
    voice = req.voice_id if (req.voice_id in OPENAI_TTS_VOICES) else TTS_VOICE
    if req.purpose == "instruction" and _instruction_clone_ready() and not _alira_voice_everywhere() and not _instruction_text_allowed(req.text):
        raise HTTPException(status_code=403, detail="The cloned instruction voice is limited to app-authored rehabilitation guidance.")
    try:
        audio_b64 = await _generate_tts_audio_base64(req.text, voice, req.purpose)
        return TTSResponse(audio_b64=audio_b64, text=req.text)
    except Exception as e:
        msg = str(e)
        logger.error(f"TTS error: {msg}")
        if "quota_exceeded" in msg or "402" in msg:
            raise HTTPException(
                status_code=503,
                detail="Voice service unavailable: Emergent LLM key quota exceeded. Add balance at Profile → Universal Key.",
            )
        raise HTTPException(status_code=500, detail=f"TTS generation failed: {msg[:200]}")


@api_router.get("/tts/health")
async def tts_health(purpose: str = "instruction"):
    """Probe real assessment audio and report the voice that produced it."""
    purpose = "instruction" if purpose == "instruction" else "general"
    config = _tts_request_config(purpose, TTS_VOICE)
    alira = _alira_voice_everywhere()
    # Probe a shipped assessment instruction rather than an unrecorded "ok".
    # This checks the patient-facing speech path without spending new credits.
    probe_text = "Hold your hand steadily at the forward target for a moment." if alira else "ok"
    key = _tts_cache_key(probe_text, TTS_VOICE, purpose)
    prepared = (PREPARED_TTS_DIR / f"{key}.mp3").is_file()
    try:
        audio_b64 = await _generate_tts_audio_base64(probe_text, TTS_VOICE, purpose)
        # Fallback audio deliberately is not cached under Alira's identity.
        fallback_used = alira and _tts_cache_get(key) is None
        actual = _openai_tts_request_config(TTS_VOICE) if fallback_used else config
        return {
            "ok": True,
            "bytes": len(base64.b64decode(audio_b64)),
            "voice": actual["public_voice"],
            "model": actual["model"],
            "provider": actual["provider"],
            "purpose": purpose,
            "instruction_clone_ready": _instruction_clone_ready(),
            "configured_instruction_provider": INSTRUCTION_TTS_PROVIDER,
            "configured_voice_id": config["voice"],
            "alira_voice_everywhere": alira,
            "alira_live_voice_available": alira and bool(ELEVENLABS_API_KEY) and time.monotonic() >= _alira_live_voice_retry_at,
            "audio_source": "fallback" if fallback_used else "prepared" if prepared else "generated_or_cached",
            "fallback_used": fallback_used,
        }
    except Exception as e:
        msg = str(e)
        quota = "quota_exceeded" in msg or "402" in msg
        return {
            "ok": False,
            "provider": config["provider"],
            "purpose": purpose,
            "instruction_clone_ready": _instruction_clone_ready(),
            "configured_instruction_provider": INSTRUCTION_TTS_PROVIDER,
            "alira_voice_everywhere": alira,
            "quota_exceeded": quota,
            "hint": (
                "Top up your Emergent Universal Key balance at Profile → Universal Key → Add Balance."
                if quota else "Check key validity / network."
            ),
            "error": msg[:300],
        }


@api_router.post("/stt/transcribe")
async def transcribe_speech(file: UploadFile = File(...)):
    """Transcribe a short Alira voice turn without retaining the recording."""
    if not openai_tts_client:
        raise HTTPException(status_code=503, detail="Speech recognition is unavailable.")
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="The recording was empty.")
    if len(audio) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Keep each voice turn under one minute.")

    filename = file.filename or "alira-turn.m4a"
    content_type = file.content_type or "audio/m4a"

    def run_transcription():
        return openai_tts_client.audio.transcriptions.create(
            model=STT_MODEL,
            file=(filename, audio, content_type),
            response_format="json",
            language="en",
        )

    try:
        result = await asyncio.to_thread(run_transcription)
        text = str(getattr(result, "text", "") or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="No speech was detected. Please try again.")
        return {
            "text": text,
            "provider": "openai",
            "model": STT_MODEL,
            "recording_retained": False,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Speech transcription failed: %s", str(exc)[:200])
        raise HTTPException(status_code=502, detail="I could not understand that recording. Please try again.") from exc


def _assessment_patient_parameters(
    submitted: Dict[str, Any],
    user: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    merged = dict(submitted or {})
    profile = user.get("profile") if isinstance(user, dict) and isinstance(user.get("profile"), dict) else {}
    if profile:
        for key in (
            "age_band", "months_since_stroke", "side_affected", "affected_areas", "affected_areas_other",
            "dominant_hand", "mobility_level", "sitting_ability", "affected_arm_movement",
            "arm_activity_difficulties", "affected_hand_movement", "hand_activity_difficulties",
            "mobility_activity_difficulties", "standing_exercise_clearance", "movement_pain",
            "instruction_support", "medical_conditions", "medical_conditions_other", "has_caregiver",
        ):
            if profile.get(key) is not None:
                merged.setdefault(key, profile[key])
        if not merged.get("patient_priorities"):
            priorities = []
            if str(profile.get("primary_goal") or "").strip():
                priorities.append(str(profile["primary_goal"]).strip())
            priorities.extend(str(item).strip() for item in profile.get("secondary_goals") or [] if str(item).strip() and str(item).strip() != "other")
            if str(profile.get("secondary_goals_other") or "").strip():
                priorities.append(str(profile["secondary_goals_other"]).strip())
            if priorities:
                merged["patient_priorities"] = priorities
    return merged


def _generated_testing_sample_task_results(
    assigned_task_ids: Sequence[str],
) -> Tuple[List[TaskResult], str]:
    """Create a visibly synthetic, internally consistent movement sample."""
    initial_tasks = {
        str(task["id"]): task
        for task in ASSESSMENT_PACKAGES["initial"]["tasks"]
    }
    variant_index = secrets.randbelow(4)
    variant = ("steady", "building_hand_control", "building_walking_control", "building_reach_control")[variant_index]
    shoulder_elevation = (82.0, 76.0, 88.0, 68.0)[variant_index]
    trunk_lean = (19.5, 21.0, 20.0, 26.0)[variant_index]
    hand_open = (0.88, 0.67, 0.81, 0.76)[variant_index]
    pinch_control = (0.85, 0.64, 0.78, 0.72)[variant_index]
    gait_symmetry = (0.86, 0.79, 0.68, 0.76)[variant_index]

    results: List[TaskResult] = []
    for task_id in assigned_task_ids:
        task = initial_tasks.get(str(task_id))
        if not task:
            continue
        steps = [
            TaskStepResult(
                step_id=str(step["id"]),
                completed=True,
                duration_ms=1400 + secrets.randbelow(901),
                metrics={},
            )
            for step in task.get("steps", [])
        ]
        metrics: Dict[str, Any] = {"generated_testing_sample": True}
        if task_id == "T1":
            metrics.update({
                "shoulder_elevation_deg": shoulder_elevation,
                "trunk_lean_deg": trunk_lean,
                "shoulder_hike": variant == "building_reach_control",
            })
        elif task_id == "T2":
            metrics["shoulder_elevation_deg"] = shoulder_elevation
        elif task_id in {"H1", "H4"}:
            metrics["hand_open_score"] = hand_open
        elif task_id == "H3":
            metrics["pinch_score"] = pinch_control
        elif task_id == "L6":
            metrics.update({
                "gait_bilateral_motion_symmetry": gait_symmetry,
                "gait_full_body_visibility_ratio": 0.94,
                "uploaded_video_duration_ms": 7200,
            })
        results.append(TaskResult(
            task_id=str(task_id),
            completed_steps=len(steps),
            total_steps=len(steps),
            duration_ms=sum(step.duration_ms for step in steps),
            steps=steps,
            metrics=metrics,
        ))
    return results, variant


ROUGH_WALKING_ASSESSMENT_SCORE = 82.0


def _has_valid_gait_score(analysis: Any) -> bool:
    if not isinstance(analysis, dict) or analysis.get("status") != "scored":
        return False
    score = analysis.get("score")
    return (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and isfinite(float(score))
        and 0.0 <= float(score) <= 100.0
    )


def _saved_walking_video_id(doc: Dict[str, Any]) -> str:
    return next((
        str(item.get("video_id") or "")
        for item in ((doc.get("model_analysis") or {}).get("tasks") or [])
        if str(item.get("task_id") or "") == "L6"
    ), "")


def _rough_walking_assessment_analysis(
    existing: Optional[Dict[str, Any]] = None,
    *,
    source_video_id: str = "",
) -> Dict[str, Any]:
    """Return the requested rough score when the walking task is unscorable."""
    existing = dict(existing or {})
    summary = dict(existing.get("summary") or {})
    original_reasons = [str(item) for item in (existing.get("reason_codes") or []) if str(item)]
    return {
        "version": "rehyn-gait-assessment-fallback-1",
        "status": "scored",
        "score": ROUGH_WALKING_ASSESSMENT_SCORE,
        "rough_estimate": True,
        "reason_codes": original_reasons or ["walking_pattern_not_detected"],
        "components": {},
        "quality": dict(existing.get("quality") or {}),
        "summary": {
            **summary,
            "step_count": int(summary.get("step_count") or 0),
        },
        "camera_motion_handling": "rough_uploaded_video_fallback",
        "analysis_method": "uploaded_video_rough_estimate_v1",
        "provenance": {
            **dict(existing.get("provenance") or {}),
            "source_video_id": source_video_id,
            "uses_3d_reconstruction": False,
        },
        "original_analysis_status": str(existing.get("status") or "unavailable"),
        "error": str(existing.get("error") or "")[:500] or None,
    }


def _assessment_needs_rough_walking_score(doc: Dict[str, Any]) -> bool:
    walking = next((
        item for item in (doc.get("task_results") or [])
        if str(_snapshot_value(item, "task_id", "")) == "L6"
    ), None)
    if walking is None:
        return False
    metrics = _snapshot_value(walking, "metrics", {}) or {}
    if bool(metrics.get("walking_skipped")):
        return False
    existing = metrics.get("gait_analysis") if isinstance(metrics.get("gait_analysis"), dict) else {}
    if _has_valid_gait_score(existing):
        return False
    return True


def _assessment_with_rough_walking_score(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Backfill saved assessments whose accepted walking clip had no score."""
    current = dict(doc)
    if not _assessment_needs_rough_walking_score(current):
        return current
    task_results = list(current.get("task_results") or [])
    walking = next(item for item in task_results if str(_snapshot_value(item, "task_id", "")) == "L6")
    walking_metrics = _snapshot_value(walking, "metrics", {}) or {}
    existing = walking_metrics.get("gait_analysis") if isinstance(walking_metrics.get("gait_analysis"), dict) else {}
    gait_analysis = _rough_walking_assessment_analysis(
        existing,
        source_video_id=_saved_walking_video_id(current),
    )
    updated_task_results = _task_results_with_gait_analysis(task_results, gait_analysis)
    current["task_results"] = updated_task_results
    current["metrics"] = _functional_metrics_with_survey_mobility(
        current.get("metrics"),
        updated_task_results,
        current.get("assigned_task_ids"),
        current.get("patient_parameters") or {},
    )
    model_analysis = dict(current.get("model_analysis") or {})
    model_analysis["gait_stage"] = {
        **gait_analysis,
        "completed_at": str(current.get("created_at") or ""),
    }
    task_states = [dict(item) for item in (model_analysis.get("tasks") or [])]
    for state in task_states:
        if str(state.get("task_id") or "") == "L6":
            state["status"] = "rough_video_score"
    model_analysis["tasks"] = task_states
    current["model_analysis"] = model_analysis
    return current


@api_router.post("/assessment/complete-for-testing", response_model=Assessment)
async def complete_initial_assessment_for_testing(request: Request):
    """Skip camera collection and persist a labelled sample for prototype testing."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)

    existing = await _care_assessments_for_user(user["id"])
    if existing:
        latest = max(existing, key=lambda item: str(item.get("created_at") or ""))
        earliest_created_at = min(
            (str(item.get("created_at") or "") for item in existing if item.get("created_at")),
            default=str(latest.get("created_at") or datetime.now(timezone.utc).isoformat()),
        )
        await _record_initial_assessment_completion(
            user,
            earliest_created_at,
            source="testing_shortcut_existing_history",
        )
        _record_alira_action(
            "assessment_testing_shortcut_opened_existing",
            source="assessment_testing_shortcut",
            user_id=user["id"],
            status="completed",
            details={"assessment_id": latest.get("id")},
        )
        return Assessment(**latest)

    assigned_task_ids = _validated_assigned_task_ids("initial", None)
    task_results, sample_variant = _generated_testing_sample_task_results(assigned_task_ids)
    request.state.assessment_testing_shortcut = True
    request.state.assessment_testing_variant = sample_variant
    profile = user.get("profile") or {}
    affected_side = "left" if str(profile.get("side_affected") or "").lower() == "left" else "right"
    return await submit_assessment(
        AssessmentSubmit(
            task_results=task_results,
            affected_side=affected_side,
            assessment_package="initial",
            assigned_task_ids=assigned_task_ids,
            patient_parameters={"generated_testing_sample": True},
            motion_data={
                "schema_version": "generated-testing-sample-v1",
                "generated_testing_sample": True,
                "sample_variant": sample_variant,
                "frames": [],
            },
        ),
        request,
    )


def _validated_browser_gait_evidence(
    candidate: Any,
    expected_video_id: str,
    duration_ms: int,
) -> Optional[Dict[str, Any]]:
    """Accept bounded 2D evidence only when it matches the saved walking video."""
    if not isinstance(candidate, dict) or not expected_video_id:
        return None
    try:
        stage = GaitStageResultSubmit.model_validate(candidate).model_dump(exclude_none=True)
        provenance = stage.get("provenance") or {}
        if stage.get("status") != "completed":
            return None
        if stage.get("analysis_method") != "mediapipe_browser_body_centric_2d_v1":
            return None
        if stage.get("camera_motion_handling") != "body_centric_2d_browser":
            return None
        if stage.get("coordinate_frame") != "pelvis_centered_leg_normalized_2d":
            return None
        if str(provenance.get("source_video_id") or "") != expected_video_id:
            return None
        if provenance.get("uses_3d_reconstruction") is not False:
            return None
        quality = stage.get("quality") or {}
        sampled = int(float(quality.get("sampled_frames") or 0))
        detected = int(float(quality.get("detected_frames") or 0))
        if not 24 <= sampled <= 60 or not 0 <= detected <= sampled:
            return None
        for key in ("tracking_coverage", "full_body_visibility", "distal_visibility", "multi_person_ratio"):
            value = float(quality.get(key))
            if not 0 <= value <= 1:
                return None
        events = (stage.get("features") or {}).get("step_events") or []
        if not isinstance(events, list) or len(events) > sampled:
            return None
        duration_seconds = max(0.1, duration_ms / 1000)
        for event in events:
            if not isinstance(event, dict) or event.get("side") not in {"left", "right"}:
                return None
            event_time = float(event.get("time_s"))
            step_length = float(event.get("step_length_proxy_leg_ratio"))
            if not 0 <= event_time <= duration_seconds + 0.25 or not 0 <= step_length <= 2:
                return None
        return stage
    except (TypeError, ValueError):
        return None


@api_router.post("/analysis/gait-2d/test-score")
async def score_gait_2d_for_testing(payload: Gait2DTestScoreRequest, request: Request):
    """Score local 2D walking evidence without creating an assessment record."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    stage = _validated_browser_gait_evidence(
        payload.evidence.model_dump(exclude_none=True),
        "settings-walking-video-test",
        payload.duration_ms,
    )
    if not stage:
        raise HTTPException(status_code=422, detail="Walking evidence could not be validated")
    return {
        "testing_only": True,
        "saved_to_assessment": False,
        "gait_analysis": score_gait_features(stage),
    }


@api_router.post("/assessment/preview-results")
async def preview_assessment_results(payload: AssessmentSubmit, request: Request):
    """Score this loopback preview only; never read or create a patient record."""
    if not is_local_assessment_preview(request):
        raise HTTPException(status_code=403, detail="Local assessment preview is not enabled")
    assigned = _validated_assigned_task_ids(payload.assessment_package, payload.assigned_task_ids)
    submitted = [task.task_id for task in payload.task_results]
    if len(submitted) != len(set(submitted)) or set(submitted) != set(assigned):
        raise HTTPException(status_code=422, detail="Results must match the selected preview tasks")
    for task in payload.task_results:
        step_ids = [step.step_id for step in task.steps]
        allowed_steps = {step["id"] for step in ASSESSMENT_RUBRICS[task.task_id]["steps"]}
        if len(step_ids) != len(set(step_ids)) or not set(step_ids).issubset(allowed_steps):
            raise HTTPException(status_code=422, detail="Invalid preview task steps")
        task.metrics.pop("gait_analysis", None)
        evidence = task.metrics.pop("gait_2d_evidence", None)
        if task.task_id == "L6" and not task.metrics.get("walking_skipped"):
            stage = _validated_browser_gait_evidence(evidence, "local-preview-walking", task.duration_ms)
            if stage:
                task.metrics["gait_analysis"] = score_gait_features(stage)
    return {
        "preview_only": True,
        "saved_to_assessment": False,
        "assessment_package": payload.assessment_package,
        "task_results": [task.model_dump() for task in payload.task_results],
        "metrics": {"task_quality": score_assessment(payload.task_results, ASSESSMENT_RUBRICS, assigned)},
    }


@api_router.post("/assessment/submit", response_model=Assessment)
async def submit_assessment(payload: AssessmentSubmit, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    testing_shortcut = bool(getattr(request.state, "assessment_testing_shortcut", False))
    assigned_task_ids = _validated_assigned_task_ids(payload.assessment_package, payload.assigned_task_ids)
    access = (
        {
            "due": True,
            "trigger": "initial",
            "packages": ["initial"],
            "task_ids": assigned_task_ids,
            "issue_report_id": None,
        }
        if testing_shortcut
        else await _assessment_access_plan(user, payload.assessment_package, assigned_task_ids)
    )
    if not testing_shortcut:
        await consume_credits(user["id"], "assessment")
    patient_parameters = _assessment_patient_parameters(payload.patient_parameters, user)
    assessment_id = str(uuid.uuid4())
    task_ids = [task.task_id for task in payload.task_results]
    if set(task_ids) != set(assigned_task_ids):
        raise HTTPException(status_code=422, detail="Submitted task results must match the assigned assessment tasks")
    browser_gait_candidate: Optional[Dict[str, Any]] = None
    for task in payload.task_results:
        if task.task_id == "L6" and isinstance(task.metrics.get("gait_2d_evidence"), dict):
            browser_gait_candidate = dict(task.metrics["gait_2d_evidence"])
        task.metrics.pop("gait_2d_evidence", None)
        task.metrics.pop("gait_analysis", None)
    patient_parameters["assigned_task_ids"] = assigned_task_ids
    video_records = {} if testing_shortcut else await _latest_task_videos(
        user["id"] if user else "",
        payload.assessment_package,
        task_ids,
    )
    model_analysis = build_model_analysis_manifest(assessment_id, task_ids, video_records)
    if testing_shortcut:
        model_analysis["status"] = "generated_testing_sample"
        for task_state in model_analysis.get("tasks", []):
            task_state["status"] = "generated_testing_sample"
    skipped_task_ids = {
        task.task_id for task in payload.task_results
        if bool((task.metrics or {}).get("walking_skipped"))
    }
    walking_was_skipped = "L6" in skipped_task_ids
    walking_video_available = "L6" in video_records and not walking_was_skipped
    walking_score_required = "L6" in task_ids and not walking_was_skipped
    browser_gait_stage = _validated_browser_gait_evidence(
        browser_gait_candidate,
        str((video_records.get("L6") or {}).get("id") or ""),
        next((task.duration_ms for task in payload.task_results if task.task_id == "L6"), 0),
    ) if walking_video_available else None
    browser_gait_analysis = score_gait_features(browser_gait_stage) if browser_gait_stage else None
    if walking_score_required and (
        not _has_valid_gait_score(browser_gait_analysis)
    ):
        browser_gait_analysis = _rough_walking_assessment_analysis(
            browser_gait_analysis,
            source_video_id=str((video_records.get("L6") or {}).get("id") or ""),
        )
    if browser_gait_analysis:
        payload.task_results = [
            TaskResult.model_validate(row)
            for row in _task_results_with_gait_analysis(payload.task_results, browser_gait_analysis)
        ]
    for task_state in model_analysis.get("tasks", []):
        if str(task_state.get("task_id")) in skipped_task_ids:
            task_state["status"] = "not_observed_patient_skipped"
        elif str(task_state.get("task_id")) == "L6":
            task_state["status"] = (
                f"browser_2d_{browser_gait_analysis['status']}" if browser_gait_analysis
                else "queued_gait_analysis" if LOCAL_GPU_WORKER_URL and walking_video_available
                else "browser_2d_evidence_unavailable" if walking_video_available
                else "waiting_for_video"
            )
    has_camera_analysis_tasks = any(task_id != "L6" for task_id in task_ids)
    if not testing_shortcut and task_ids and not has_camera_analysis_tasks and not walking_video_available:
        model_analysis["status"] = "waiting_for_inputs"
    model_analysis["gpu_stage"] = {
        "status": (
            "generated_testing_sample" if testing_shortcut
            else "queued" if LOCAL_GPU_WORKER_URL and has_camera_analysis_tasks
            else "supporting_record_only" if task_ids and not has_camera_analysis_tasks
            else "not_configured"
        ),
        "device": None if testing_shortcut else "cuda:0" if LOCAL_GPU_WORKER_URL and has_camera_analysis_tasks else None,
    }
    model_analysis["gait_stage"] = {
        **({
            **browser_gait_analysis,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        } if browser_gait_analysis else {}),
        "status": browser_gait_analysis["status"] if browser_gait_analysis else (
            "generated_testing_sample" if testing_shortcut
            else "not_observed_patient_skipped" if walking_was_skipped
            else "queued" if LOCAL_GPU_WORKER_URL and walking_video_available
            else "unscorable" if walking_video_available
            else "waiting_for_video"
        ),
        "score": browser_gait_analysis.get("score") if browser_gait_analysis else None,
        "camera_motion_handling": browser_gait_analysis.get("camera_motion_handling") if browser_gait_analysis else None,
    }
    model_analysis["musculoskeletal_stage"] = {
        "status": (
            "generated_testing_sample" if testing_shortcut
            else "not_observed_patient_skipped" if walking_was_skipped
            else "supporting_record_only" if "L6" in assigned_task_ids
            else "not_configured"
        ),
        "modeled_tasks": [],
    }
    expected_task_count = len(assigned_task_ids)
    collection_summary = patient_collection_summary(payload.task_results, expected_task_count)
    if payload.musculoskeletal_outputs:
        logger.warning("Ignoring client-supplied musculoskeletal outputs; trusted worker ingestion is required")
    trusted_model_outputs: Dict[str, Any] = {}
    issues = derive_functional_issues(payload.task_results)
    functional_metrics = build_functional_metrics(payload.task_results, assigned_task_ids, patient_parameters)
    domain_assessments = build_domain_assessments(payload.task_results)
    expected_summary_domains = _expected_domains_for_tasks(payload.assessment_package, assigned_task_ids)
    body_function_summary = _summary_with_survey_mobility(
        patient_body_function_summary(
            payload.task_results,
            issues,
            trusted_model_outputs,
            expected_summary_domains,
        ),
        patient_parameters,
    )
    movement_snapshot_decision = build_movement_snapshot_decision(
        payload.task_results,
        issues,
        body_function_summary,
        payload.affected_side,
        model_analysis,
    )
    clinician_measures = build_clinician_measure_summary(patient_parameters)
    biomechanical_estimates = build_biomechanical_estimates(
        payload.task_results,
        patient_parameters,
        trusted_model_outputs,
    )
    measurement_form = build_clinical_measurement_form(
        payload.task_results,
        patient_parameters,
        trusted_model_outputs,
    )
    rehabilitation_goals = build_rehab_goals(
        payload.task_results,
        issues,
        measurement_form,
        patient_parameters,
    )
    movement_based_muscle_screening = build_muscle_activation_diagnosis(
        [t.model_dump() for t in payload.task_results],
    )
    muscle_activation_diagnosis = pending_model_activation_report()
    survey_consistency = build_survey_consistency(
        issues,
        patient_parameters,
        payload.task_results,
    )
    analysis_pipeline = build_analysis_pipeline(
        payload.motion_data,
        trusted_model_outputs,
    )
    clinical_review_gate = build_clinical_review_gate(
        issues,
        patient_parameters,
        payload.task_results,
        trusted_model_outputs,
        expected_task_count,
    )
    survey_profile = {**(user.get("profile") or {}), **(patient_parameters or {})}
    plan = survey_rehab_plan(survey_profile)
    if str(survey_profile.get("movement_pain") or "").lower() == "severe_or_worsening":
        clinical_review_gate = _clinical_gate_with_survey_hold(clinical_review_gate, survey_profile)
    elif testing_shortcut:
        clinical_review_gate = _clinical_gate_with_core_plan(
            {
                **clinical_review_gate,
                "status": "clear",
                "rehab_access": "allowed",
                "reason_code": "generated_testing_sample",
            },
            plan,
        )
        clinical_review_gate.update({
            "reason_code": "generated_testing_sample",
            "patient_title": "Your sample rehab plan is ready",
            "patient_message": (
                "This testing shortcut created sample movement results without camera measurement. "
                "The current four-exercise programme is the same for every patient account."
            ),
        })
    elif clinical_review_gate.get("status") in {"awaiting_model_analysis", "clear", "no_rehab_needed"}:
        clinical_review_gate = _clinical_gate_with_core_plan(clinical_review_gate, plan)
    else:
        # Keep the assigned core visible in the record while the independent
        # safety gate prevents it from being started.
        clinical_review_gate = {
            **clinical_review_gate,
            "rehab_plan_source": "fixed_core_programme",
        }
    if plan and clinical_review_gate.get("rehab_access") == "allowed" and not testing_shortcut:
        await consume_credits(user["id"], "rehab_plan")
    _record_alira_action(
        "rehab_plan_issued",
        source="assessment_testing_shortcut" if testing_shortcut else "assessment_submit",
        user_id=user["id"],
        status="completed" if plan else "empty",
        details={
            "plan_source": clinical_review_gate.get("rehab_plan_source") or "none",
            "rehab_access": clinical_review_gate.get("rehab_access"),
            "review_status": clinical_review_gate.get("status"),
            "exercises": [
                {"id": exercise.id, "name": exercise.name, "targets_issue": exercise.targets_issue, "reason": exercise.selection_reason}
                for exercise in plan
            ],
        },
    )
    assessment = Assessment(
        id=assessment_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        affected_side=payload.affected_side,
        assessment_package=payload.assessment_package,
        testing_shortcut=testing_shortcut,
        result_provenance="generated_testing_sample" if testing_shortcut else "observed_assessment",
        assigned_task_ids=assigned_task_ids,
        patient_parameters=patient_parameters,
        task_results=payload.task_results,
        functional_issues=issues,
        rehab_plan=plan,
        domain_assessments=domain_assessments,
        clinician_measures=clinician_measures,
        biomechanical_estimates=biomechanical_estimates,
        measurement_form=measurement_form,
        rehabilitation_goals=rehabilitation_goals,
        muscle_activation_diagnosis=muscle_activation_diagnosis,
        survey_consistency=survey_consistency,
        analysis_pipeline=analysis_pipeline,
        clinical_review_gate=clinical_review_gate,
        body_function_summary=body_function_summary,
        movement_snapshot_decision=movement_snapshot_decision,
        metrics=functional_metrics,
    )
    doc = assessment.model_dump()
    if payload.motion_data:
        doc["motion_data"] = payload.motion_data
    doc["patient_parameters"] = patient_parameters
    doc["patient_summary"] = collection_summary
    doc["model_analysis"] = model_analysis
    doc["movement_based_muscle_screening"] = movement_based_muscle_screening
    doc["patient_insights"] = build_patient_insights(
        body_function_summary, {}, {}, model_analysis
    )
    if testing_shortcut:
        doc["patient_insights"].update({
            "badge": "Generated sample",
            "headline": "Your sample movement snapshot is ready",
            "summary": "These values were generated for testing and were not measured by the camera.",
            "reporting_rule": "Generated sample data for product testing only; this is not a patient measurement or clinical result.",
        })
    doc["user_id"] = user["id"]
    doc["account_email"] = str(user.get("email") or "").strip().lower()
    doc["assessment_trigger"] = access.get("trigger")
    doc["functional_issue_report_id"] = access.get("issue_report_id")
    assessment_saved_to_mongodb = True
    try:
        # Motor may add an ObjectId to the dictionary before a failed insert.
        # Keep the patient-facing fallback copy JSON-safe and unchanged.
        await db.assessments.insert_one(doc.copy())
    except Exception as e:
        assessment_saved_to_mongodb = False
        logger.warning("Mongo unavailable for assessment insert; queuing a temporary local copy: %s", str(e)[:160])
        _queue_assessment_for_mongodb_sync(doc, user, e)
    if browser_gait_analysis:
        _record_alira_action(
            "walking_video_scored",
            source="browser_body_centric_2d",
            user_id=user["id"],
            status=browser_gait_analysis["status"],
            details={
                "assessment_id": assessment_id,
                "score": browser_gait_analysis.get("score"),
                "camera_motion_handling": browser_gait_analysis.get("camera_motion_handling"),
                "reason_codes": browser_gait_analysis.get("reason_codes") or [],
            },
        )
    if access.get("trigger") == "initial":
        if assessment_saved_to_mongodb:
            await _record_initial_assessment_completion(user, assessment.created_at)
        else:
            user["initial_assessment_completed_at"] = assessment.created_at
            user["initial_assessment_completion_source"] = "temporary_local_pending_sync"
            _remember_local_user(user)
    if assessment_saved_to_mongodb:
        await _mark_functional_issue_assessed(user["id"], access.get("issue_report_id"), assessment_id)
    _record_alira_action(
        "movement_snapshot_generated",
        source="assessment_testing_shortcut" if testing_shortcut else "assessment_pipeline",
        user_id=user["id"],
        details={
            "assessment_id": assessment_id,
            **movement_snapshot_decision,
        },
    )
    _record_alira_action(
        "assessment_completed",
        source="assessment_testing_shortcut" if testing_shortcut else "assessment_runner",
        user_id=user["id"],
        details={
            "assessment_id": assessment_id,
            "package_id": payload.assessment_package,
            "task_ids": assigned_task_ids,
            "selection_trigger": access.get("trigger"),
            "issue_report_id": access.get("issue_report_id"),
            "exercise_ids": [exercise.id for exercise in plan],
        },
    )
    analysis_video_records = {
        task_id: record for task_id, record in video_records.items()
        if task_id not in skipped_task_ids
    }
    analysis_motion_data = dict(payload.motion_data or {})
    has_analysis_input = bool(analysis_video_records or analysis_motion_data.get("frames"))
    if assessment_saved_to_mongodb and not testing_shortcut and LOCAL_GPU_WORKER_URL and ANALYSIS_WORKER_TOKEN and has_analysis_input:
        asyncio.create_task(_queue_local_gpu_stage(
            assessment_id,
            analysis_motion_data,
            analysis_video_records,
            payload.affected_side,
            patient_parameters,
        ))
    return assessment


@api_router.get("/assessment/survey-report")
async def get_survey_assessment_report(request: Request):
    """The three-page assessment report (spec: report-first flow).

    Page 1: daily-activity driven metrics - from survey AND tasks when camera
    assessments exist, from the survey alone otherwise (honest labels, never
    fabricated scores). Page 2: the anatomy map with pin-pointed functional
    problems. Page 3: the rehab plan (caregiver-delivered when no camera task
    can be assigned).
    """
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    profile = dict(user.get("profile") or {})
    assessments_raw = await _care_assessments_for_user(user["id"])
    assessments_raw.sort(key=lambda item: item.get("created_at", ""))
    series, _issues = _assessment_progress_series(assessments_raw)
    plan = await _adaptive_care_plan_for_user(user, assessments=assessments_raw)
    survey_report_meta = plan.get("survey_report") or {}
    caregiver_plan = plan.get("caregiver_plan") or {}
    latest_rehab_plan = (assessments_raw[-1].get("rehab_plan") or []) if assessments_raw else []
    if survey_report_meta.get("available"):
        rehab_page: Dict[str, Any] = {"type": "caregiver_delivered", "caregiver_plan": caregiver_plan}
    elif latest_rehab_plan:
        rehab_page = {"type": "camera_guided", "exercises": latest_rehab_plan}
    else:
        rehab_page = {
            "type": "pending_assessment",
            "message": "Complete the initial assessment to unlock a personalised exercise plan.",
        }
    report = {
        "source": "survey_and_tasks" if series else "survey_only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_viewed_at": user.get("survey_report_viewed_at"),
        "pages": ["daily_activities", "functional_problems", "rehab_plan"],
        "daily_activities": build_daily_activity_metrics(series, profile),
        "functional_problems": survey_functional_problems(profile),
        "rehab_plan": rehab_page,
        "next_step_after_viewing": "rehab_plan",
    }
    _record_alira_action(
        "survey_report_served",
        source="survey_report",
        user_id=user["id"],
        status="completed",
        details={"source": report["source"], "page_count": 3},
    )
    return report


@api_router.get("/assessment/history", response_model=List[Assessment])
async def get_assessment_history(request: Request):
    """Return ONLY the signed-in user's assessments. Anonymous → empty list."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        return []
    try:
        docs = (
            await db.assessments
            .find({"user_id": user["id"]}, {"_id": 0})
            .sort("created_at", -1)
            .to_list(50)
        )
    except Exception as e:
        _require_durable_patient_store("assessment history", e)
        logger.warning(f"Mongo unavailable for assessment history; using local fallback: {str(e)[:120]}")
        docs = [
            {k: v for k, v in item.items() if k != "_id"}
            for item in LOCAL_ASSESSMENTS
            if item.get("user_id") == user["id"]
        ]
        docs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        docs = docs[:50]
    docs = [_assessment_with_current_rehab_policy(doc) for doc in docs]
    for doc in docs:
        if not doc.get("patient_insights"):
            package_id = str(doc.get("assessment_package") or "upper_limb")
            assigned_task_ids = doc.get("assigned_task_ids") or [str(item.get("task_id")) for item in doc.get("task_results", [])]
            expected_domains = _expected_domains_for_tasks(package_id, assigned_task_ids)
            body_summary = doc.get("body_function_summary") or patient_body_function_summary(
                doc.get("task_results", []),
                doc.get("functional_issues", []),
                doc.get("musculoskeletal_outputs", {}),
                expected_domains,
            )
            doc["patient_insights"] = build_patient_insights(
                body_summary,
                doc.get("musculoskeletal_outputs") or {},
                doc.get("musculoskeletal_research_stage") or {},
                doc.get("model_analysis") or {},
            )
    return [Assessment(**d) for d in docs]


async def _owned_assessment_doc(assessment_id: str, request: Request, purpose: str) -> Dict[str, Any]:
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    local_doc = _local_assessment(user["id"], assessment_id)
    if local_doc and local_doc.get("persistence_status") == PENDING_ASSESSMENT_SYNC_STATUS:
        needs_rough_score = _assessment_needs_rough_walking_score(local_doc)
        current = _assessment_with_current_rehab_policy(local_doc)
        if needs_rough_score:
            local_doc.update({
                "task_results": current.get("task_results") or [],
                "metrics": current.get("metrics") or {},
                "model_analysis": current.get("model_analysis") or {},
            })
            _persist_local_state()
        return current
    try:
        doc = await db.assessments.find_one({"id": assessment_id, "user_id": user["id"]}, {"_id": 0})
    except Exception as e:
        _require_durable_patient_store(purpose, e)
        logger.warning("Mongo unavailable for %s; using local fallback: %s", purpose, str(e)[:120])
        doc = next(
            (item for item in LOCAL_ASSESSMENTS if item.get("id") == assessment_id and item.get("user_id") == user["id"]),
            None,
        )
    if not doc:
        raise HTTPException(status_code=404, detail="Assessment not found")
    needs_rough_score = _assessment_needs_rough_walking_score(doc)
    current = _assessment_with_current_rehab_policy(doc)
    if needs_rough_score:
        updates = {
            "task_results": current.get("task_results") or [],
            "metrics": current.get("metrics") or {},
            "model_analysis.gait_stage": (current.get("model_analysis") or {}).get("gait_stage") or {},
            "model_analysis.tasks": (current.get("model_analysis") or {}).get("tasks") or [],
        }
        try:
            await db.assessments.update_one(
                {"id": assessment_id, "user_id": user["id"]},
                {"$set": updates},
            )
        except Exception as exc:
            logger.warning("Could not persist rough walking-score read repair yet: %s", str(exc)[:160])
    return current


@api_router.get("/assessment/{assessment_id}", response_model=Assessment)
async def get_assessment(assessment_id: str, request: Request):
    doc = await _owned_assessment_doc(assessment_id, request, "assessment get")
    return Assessment(**doc)


@api_router.post("/assessment/{assessment_id}/rehab-plan-access")
async def record_rehab_plan_access(assessment_id: str, request: Request):
    """Claim the one-time preparation view for this user's assessment plan."""
    doc = await _owned_assessment_doc(assessment_id, request, "rehab plan access")
    first_viewed_at = doc.get("rehab_plan_first_viewed_at")
    first_access = False
    if not first_viewed_at:
        candidate_time = datetime.now(timezone.utc).isoformat()
        try:
            result = await db.assessments.update_one(
                {
                    "id": assessment_id,
                    "user_id": doc.get("user_id"),
                    "rehab_plan_first_viewed_at": {"$exists": False},
                },
                {"$set": {"rehab_plan_first_viewed_at": candidate_time}},
            )
            first_access = result.modified_count == 1
            if first_access:
                first_viewed_at = candidate_time
            else:
                stored = await db.assessments.find_one(
                    {"id": assessment_id, "user_id": doc.get("user_id")},
                    {"_id": 0, "rehab_plan_first_viewed_at": 1},
                )
                first_viewed_at = (stored or {}).get("rehab_plan_first_viewed_at")
        except Exception as exc:
            _require_durable_patient_store("rehab plan access", exc)
            logger.warning("Mongo unavailable for rehab plan access; local fallback: %s", str(exc)[:120])
            for item in LOCAL_ASSESSMENTS:
                if item.get("id") == assessment_id and item.get("user_id") == doc.get("user_id"):
                    first_viewed_at = item.get("rehab_plan_first_viewed_at")
                    first_access = not bool(first_viewed_at)
                    if first_access:
                        first_viewed_at = candidate_time
                        item["rehab_plan_first_viewed_at"] = first_viewed_at
                    break
    return {
        "assessment_id": assessment_id,
        "first_access": first_access,
        "first_viewed_at": first_viewed_at,
    }


@api_router.get("/assessment/{assessment_id}/patient-summary")
async def get_patient_assessment_summary(assessment_id: str, request: Request):
    """Return only the concise collection receipt intended for patients."""
    doc = await _owned_assessment_doc(assessment_id, request, "patient summary")
    package_id = str(doc.get("assessment_package") or "upper_limb")
    assigned_task_ids = doc.get("assigned_task_ids") or [str(item.get("task_id")) for item in doc.get("task_results", [])]
    expected = len(assigned_task_ids) or len(ASSESSMENT_PACKAGES.get(package_id, {}).get("tasks", []))
    collection = doc.get("patient_summary") or patient_collection_summary(doc.get("task_results", []), expected)
    expected_summary_domains = _expected_domains_for_tasks(package_id, assigned_task_ids)
    body_function_summary = _summary_with_survey_mobility(
        doc.get("body_function_summary") or patient_body_function_summary(
            doc.get("task_results", []),
            doc.get("functional_issues", []),
            doc.get("musculoskeletal_outputs", {}),
            expected_summary_domains,
        ),
        doc.get("patient_parameters") or {},
    )
    task_quality = _task_quality_with_survey_mobility(
        score_assessment(doc.get("task_results", []), ASSESSMENT_RUBRICS, assigned_task_ids),
        doc.get("patient_parameters") or {},
    )
    functional_metrics = _functional_metrics_with_survey_mobility(
        doc.get("metrics"),
        doc.get("task_results", []),
        assigned_task_ids,
        doc.get("patient_parameters") or {},
    )
    functional_metrics["task_quality"] = task_quality
    return {
        "id": doc["id"],
        "created_at": doc["created_at"],
        "assessment_package": package_id,
        "collection": collection,
        "body_function_summary": body_function_summary,
        "movement_snapshot_decision": doc.get("movement_snapshot_decision") or build_movement_snapshot_decision(
            doc.get("task_results", []),
            doc.get("functional_issues", []),
            body_function_summary,
            doc.get("affected_side", "right"),
            doc.get("model_analysis") or {},
        ),
        "functional_metrics": functional_metrics,
        "insights": doc.get("patient_insights") or build_patient_insights(
            body_function_summary,
            doc.get("musculoskeletal_outputs") or {},
            doc.get("musculoskeletal_research_stage") or {},
            doc.get("model_analysis") or {},
        ),
        "rehab_plan_ready": bool(doc.get("rehab_plan")) and (doc.get("clinical_review_gate") or {}).get("rehab_access", "allowed") in ("allowed", "interim"),
        "clinical_review_gate": doc.get("clinical_review_gate") or {},
    }


@api_router.get("/assessment/{assessment_id}/analysis-status")
async def get_assessment_analysis_status(assessment_id: str, request: Request):
    """Return processing state without exposing internal model predictions."""
    doc = await _owned_assessment_doc(assessment_id, request, "analysis status")
    model_analysis = doc.get("model_analysis") or {}
    gpu_stage = model_analysis.get("gpu_stage") or {}
    gait_stage = model_analysis.get("gait_stage") or {}
    musculoskeletal_stage = model_analysis.get("musculoskeletal_stage") or {}
    task_states = {
        task_id: str(task.get("status") or "unknown")
        for task_id, task in (gpu_stage.get("tasks") or {}).items()
        if isinstance(task, dict)
    }
    return {
        "assessment_id": assessment_id,
        "model_status": model_analysis.get("status", "waiting_for_inputs"),
        "gpu_stage": {
            "status": gpu_stage.get("status", "not_configured"),
            "device": gpu_stage.get("device"),
            "gpu_name": gpu_stage.get("gpu_name"),
            "model_version": gpu_stage.get("model_version"),
            "tasks": task_states,
            "error": gpu_stage.get("error"),
        },
        "gait_stage": {
            "status": gait_stage.get("status", "not_configured"),
            "score": gait_stage.get("score"),
            "analysis_method": gait_stage.get("analysis_method"),
            "camera_motion_handling": gait_stage.get("camera_motion_handling"),
            "wham_ready": gait_stage.get("wham_ready"),
            "reason_codes": gait_stage.get("reason_codes") or [],
            "error": gait_stage.get("error"),
        },
        "musculoskeletal_stage": {
            "status": musculoskeletal_stage.get("status", "not_configured"),
            "modeled_tasks": musculoskeletal_stage.get("modeled_tasks") or [],
            "solver": musculoskeletal_stage.get("solver"),
            "error": musculoskeletal_stage.get("error"),
        },
    }


def _require_analysis_worker(request: Request) -> None:
    if not ANALYSIS_WORKER_TOKEN:
        raise HTTPException(status_code=503, detail="Biomechanics worker ingestion is not configured")
    supplied = request.headers.get("x-analysis-worker-token", "")
    if supplied != ANALYSIS_WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid analysis worker token")


async def _set_gpu_stage(assessment_id: str, stage: Dict[str, Any]) -> None:
    try:
        await db.assessments.update_one(
            {"id": assessment_id}, {"$set": {"model_analysis.gpu_stage": stage}}
        )
    except Exception as exc:
        _require_durable_patient_store("assessment GPU analysis", exc)
        logger.warning(f"Mongo unavailable for GPU stage update; local fallback: {str(exc)[:120]}")
        for item in LOCAL_ASSESSMENTS:
            if item.get("id") == assessment_id:
                item.setdefault("model_analysis", {})["gpu_stage"] = stage
                break


async def _set_gait_stage(assessment_id: str, stage: Dict[str, Any]) -> None:
    try:
        await db.assessments.update_one(
            {"id": assessment_id}, {"$set": {"model_analysis.gait_stage": stage}}
        )
    except Exception as exc:
        _require_durable_patient_store("assessment gait analysis", exc)
        logger.warning(f"Mongo unavailable for gait stage update; local fallback: {str(exc)[:120]}")
        for item in LOCAL_ASSESSMENTS:
            if item.get("id") == assessment_id:
                item.setdefault("model_analysis", {})["gait_stage"] = stage
                break


async def _set_musculoskeletal_stage(assessment_id: str, stage: Dict[str, Any]) -> None:
    try:
        doc = await db.assessments.find_one({"id": assessment_id}, {"_id": 0}) or {}
        model_analysis = dict(doc.get("model_analysis") or {})
        model_analysis["musculoskeletal_stage"] = stage
        insights = build_patient_insights(
            doc.get("body_function_summary") or {},
            doc.get("musculoskeletal_outputs") or {},
            doc.get("musculoskeletal_research_stage") or {},
            model_analysis,
        )
        await db.assessments.update_one(
            {"id": assessment_id},
            {"$set": {
                "model_analysis.musculoskeletal_stage": stage,
                "patient_insights": insights,
            }},
        )
    except Exception as exc:
        _require_durable_patient_store("assessment model analysis", exc)
        logger.warning(f"Mongo unavailable for model stage update; local fallback: {str(exc)[:120]}")
        for item in LOCAL_ASSESSMENTS:
            if item.get("id") == assessment_id:
                item.setdefault("model_analysis", {})["musculoskeletal_stage"] = stage
                item["patient_insights"] = build_patient_insights(
                    item.get("body_function_summary") or {},
                    item.get("musculoskeletal_outputs") or {},
                    item.get("musculoskeletal_research_stage") or {},
                    item.get("model_analysis") or {},
                )
                break


async def _worker_video_sources(video_records: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    sources: List[Dict[str, Any]] = []
    for task_id, record in video_records.items():
        video_id = str(record.get("id") or "")
        if not video_id:
            continue
        headers: Dict[str, str] = {}
        if record.get("storage") == "r2" and record.get("object_key") and task_video_object_storage.configured:
            url = await asyncio.to_thread(
                task_video_object_storage.presign_get, record["object_key"], 1800
            )
        else:
            url = f"{LOCAL_BACKEND_CALLBACK_URL}/assessment/task-videos/worker/{video_id}"
            headers["X-Analysis-Worker-Token"] = ANALYSIS_WORKER_TOKEN
        sources.append({
            "task_id": task_id,
            "video_id": video_id,
            "url": url,
            "headers": headers,
            "content_type": record.get("content_type") or "video/mp4",
        })
    return sources


async def _queue_local_gpu_stage(
    assessment_id: str,
    motion_data: Dict[str, Any],
    video_records: Dict[str, Dict[str, Any]],
    affected_side: str,
    patient_parameters: Dict[str, Any],
) -> None:
    job = {
        "assessment_id": assessment_id,
        "callback_url": LOCAL_BACKEND_CALLBACK_URL,
        "motion_data": motion_data,
        "video_sources": await _worker_video_sources(video_records),
        "affected_side": affected_side,
        "patient_parameters": patient_parameters,
    }
    try:
        headers = {"X-Analysis-Worker-Token": ANALYSIS_WORKER_TOKEN}
        if ANALYSIS_WORKER_CF_CLIENT_ID and ANALYSIS_WORKER_CF_CLIENT_SECRET:
            headers.update({
                "CF-Access-Client-Id": ANALYSIS_WORKER_CF_CLIENT_ID,
                "CF-Access-Client-Secret": ANALYSIS_WORKER_CF_CLIENT_SECRET,
            })
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.post(
                f"{LOCAL_GPU_WORKER_URL}/jobs",
                json=job,
                headers=headers,
            )
            response.raise_for_status()
    except Exception as exc:
        logger.warning(f"Could not queue local CUDA analysis: {str(exc)[:160]}")
        await _set_gpu_stage(assessment_id, {
            "status": "failed_to_queue",
            "device": "cuda:0",
            "error": str(exc),
        })
        await _set_musculoskeletal_stage(assessment_id, {
            "status": "failed_to_queue",
            "modeled_tasks": [],
            "solver": "OpenSim Moco",
            "error": str(exc),
        })
        if "L6" in video_records:
            await _set_gait_stage(assessment_id, {
                "status": "failed_to_queue",
                "score": None,
                "error": str(exc),
            })


@api_router.get("/analysis/local-gpu/status")
async def local_gpu_status():
    browser_gait = {
        "status": "ready",
        "analysis_method": "mediapipe_browser_body_centric_2d_v1",
        "camera_motion_handling": "body_centric_2d_browser",
        "uses_3d_reconstruction": False,
    }
    if not LOCAL_GPU_WORKER_URL:
        return {"status": "not_configured", "cuda": False, "gait_2d": browser_gait}
    try:
        headers = {}
        if ANALYSIS_WORKER_CF_CLIENT_ID and ANALYSIS_WORKER_CF_CLIENT_SECRET:
            headers = {
                "CF-Access-Client-Id": ANALYSIS_WORKER_CF_CLIENT_ID,
                "CF-Access-Client-Secret": ANALYSIS_WORKER_CF_CLIENT_SECRET,
            }
        async with httpx.AsyncClient(timeout=5) as http:
            response = await http.get(f"{LOCAL_GPU_WORKER_URL}/health", headers=headers)
            response.raise_for_status()
            return {**response.json(), "gait_2d": browser_gait}
    except Exception as exc:
        return {"status": "unavailable", "cuda": False, "error": str(exc), "gait_2d": browser_gait}


@api_router.post("/assessment/{assessment_id}/gpu-stage-results")
async def save_gpu_stage_results(
    assessment_id: str,
    payload: GPUStageResultSubmit,
    request: Request,
):
    """Store intermediate CUDA output without treating it as solver activation."""
    _require_analysis_worker(request)
    stage = payload.model_dump(exclude_none=True)
    if stage["status"] == "completed":
        if not stage.get("device", "").startswith("cuda"):
            raise HTTPException(status_code=422, detail="Completed local GPU output must come from CUDA")
        if not stage.get("model_version") or not stage.get("tasks"):
            raise HTTPException(status_code=422, detail="GPU output is missing model provenance or task results")
    elif stage["status"] != "failed":
        raise HTTPException(status_code=422, detail="GPU stage status must be completed or failed")
    await _set_gpu_stage(assessment_id, stage)
    return {"assessment_id": assessment_id, "gpu_stage": stage["status"]}


def _task_results_with_gait_analysis(
    task_results: Sequence[Any],
    gait_analysis: Dict[str, Any],
) -> List[Dict[str, Any]]:
    updated = []
    for task in task_results:
        row = task.model_dump() if hasattr(task, "model_dump") else dict(task)
        if str(row.get("task_id")) == "L6":
            metrics = dict(row.get("metrics") or {})
            metrics["gait_analysis"] = gait_analysis
            summary = gait_analysis.get("summary") or {}
            symmetry = summary.get("bilateral_symmetry")
            if isinstance(symmetry, (int, float)) and not isinstance(symmetry, bool):
                metrics["gait_bilateral_motion_symmetry"] = float(symmetry)
            metrics["gait_analysis_status"] = gait_analysis.get("status")
            metrics["gait_analysis_version"] = gait_analysis.get("version")
            metrics["gait_step_event_count"] = summary.get("step_count")
            metrics["gait_camera_motion_handling"] = gait_analysis.get("camera_motion_handling")
            row["metrics"] = metrics
        updated.append(row)
    return updated


@api_router.post("/assessment/{assessment_id}/gait-stage-results")
async def save_gait_stage_results(
    assessment_id: str,
    payload: GaitStageResultSubmit,
    request: Request,
):
    """Validate, score, and persist camera-robust walking-video evidence."""
    _require_analysis_worker(request)
    try:
        doc = await db.assessments.find_one({"id": assessment_id}, {"_id": 0})
    except Exception as exc:
        _require_durable_patient_store("assessment gait lookup", exc)
        logger.warning(f"Mongo unavailable for gait lookup; local fallback: {str(exc)[:120]}")
        doc = next((item for item in LOCAL_ASSESSMENTS if item.get("id") == assessment_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail="Assessment not found")

    stage = payload.model_dump(exclude_none=True)
    task_results = list(doc.get("task_results") or [])
    updated_task_results = task_results
    if stage["status"] == "completed":
        source_video_id = str((stage.get("provenance") or {}).get("source_video_id") or "")
        expected_video_id = next((
            str(item.get("video_id") or "")
            for item in (doc.get("model_analysis") or {}).get("tasks") or []
            if str(item.get("task_id")) == "L6"
        ), "")
        if not expected_video_id or source_video_id != expected_video_id:
            raise HTTPException(status_code=422, detail="Walking analysis does not match the saved source video")
        gait_analysis = score_gait_features(stage)
        if not _has_valid_gait_score(gait_analysis):
            gait_analysis = _rough_walking_assessment_analysis(
                gait_analysis,
                source_video_id=source_video_id,
            )
        updated_task_results = _task_results_with_gait_analysis(task_results, gait_analysis)
        functional_metrics = build_functional_metrics(
            updated_task_results,
            doc.get("assigned_task_ids"),
            doc.get("patient_parameters") or {},
        )
        gait_stage = {
            **gait_analysis,
            "wham_ready": stage.get("wham_ready"),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        updates: Dict[str, Any] = {
            "task_results": updated_task_results,
            "metrics": functional_metrics,
            "model_analysis.gait_stage": gait_stage,
        }
    elif stage["status"] == "failed":
        existing_gait_analysis = next((
            dict((_snapshot_value(item, "metrics", {}) or {}).get("gait_analysis") or {})
            for item in task_results
            if str(_snapshot_value(item, "task_id", "")) == "L6"
        ), {})
        worker_error = str(stage.get("error") or "Walking-video analysis failed")[:500]
        gait_analysis = existing_gait_analysis if _has_valid_gait_score(existing_gait_analysis) else (
            _rough_walking_assessment_analysis({
                "status": "failed",
                "score": None,
                "error": worker_error,
                "reason_codes": ["walking_analysis_failed"],
                "provenance": dict(stage.get("provenance") or {}),
            }, source_video_id=_saved_walking_video_id(doc))
        )
        updated_task_results = _task_results_with_gait_analysis(task_results, gait_analysis)
        functional_metrics = build_functional_metrics(
            updated_task_results,
            doc.get("assigned_task_ids"),
            doc.get("patient_parameters") or {},
        )
        gait_stage = {
            **gait_analysis,
            "worker_status": "failed",
            "worker_error": worker_error,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        updates = {
            "task_results": updated_task_results,
            "metrics": functional_metrics,
            "model_analysis.gait_stage": gait_stage,
        }
    else:
        raise HTTPException(status_code=422, detail="Gait stage status must be completed or failed")

    try:
        await db.assessments.update_one({"id": assessment_id}, {"$set": updates})
    except Exception as exc:
        _require_durable_patient_store("assessment gait result", exc)
        logger.warning(f"Mongo unavailable for gait result update; local fallback: {str(exc)[:120]}")
        for item in LOCAL_ASSESSMENTS:
            if item.get("id") == assessment_id:
                item["task_results"] = updated_task_results
                if "metrics" in updates:
                    item["metrics"] = updates["metrics"]
                item.setdefault("model_analysis", {})["gait_stage"] = gait_stage
                break
    _record_alira_action(
        "walking_video_scored",
        source="camera_robust_gait_worker",
        user_id=doc.get("user_id"),
        status=gait_stage["status"],
        details={
            "assessment_id": assessment_id,
            "score": gait_stage.get("score"),
            "camera_motion_handling": gait_stage.get("camera_motion_handling"),
            "reason_codes": gait_stage.get("reason_codes") or [],
        },
    )
    return {
        "assessment_id": assessment_id,
        "status": gait_stage["status"],
        "lower_limb_score": gait_stage.get("score"),
        "reason_codes": gait_stage.get("reason_codes") or [],
    }


@api_router.post("/assessment/{assessment_id}/model-stage-results")
async def save_musculoskeletal_stage_results(
    assessment_id: str,
    payload: MusculoskeletalStageResultSubmit,
    request: Request,
):
    """Store genuine solver output without promoting unvalidated estimates."""
    _require_analysis_worker(request)
    try:
        doc = await db.assessments.find_one({"id": assessment_id}, {"_id": 0})
    except Exception as exc:
        _require_durable_patient_store("assessment model lookup", exc)
        logger.warning(f"Mongo unavailable for model stage lookup; local fallback: {str(exc)[:120]}")
        doc = next((item for item in LOCAL_ASSESSMENTS if item.get("id") == assessment_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail="Assessment not found")

    stage = payload.model_dump(exclude_none=True)
    if stage["status"] == "completed":
        if not stage.get("per_task"):
            raise HTTPException(status_code=422, detail="Completed model stage requires task outputs")
        manifest = {
            str(item.get("task_id")): str(item.get("video_id") or "")
            for item in (doc.get("model_analysis") or {}).get("tasks") or []
        }
        for row in stage["per_task"]:
            task_id = str(row.get("task_id") or "")
            provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
            if task_id not in manifest or not manifest[task_id]:
                raise HTTPException(status_code=422, detail=f"Task {task_id} has no saved source video")
            if str(provenance.get("source_video_id") or "") != manifest[task_id]:
                raise HTTPException(status_code=422, detail=f"Task {task_id} result does not match its source video")
            if not all(str(provenance.get(key) or "").strip() for key in ("solver", "model_version", "code_version")):
                raise HTTPException(status_code=422, detail=f"Task {task_id} is missing model provenance")
            activations = row.get("muscle_activations")
            if not isinstance(activations, dict) or not activations:
                raise HTTPException(status_code=422, detail=f"Task {task_id} has no model activations")
            for activation in activations.values():
                value = activation.get("mean") if isinstance(activation, dict) else activation
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 1:
                    raise HTTPException(status_code=422, detail=f"Task {task_id} activation is outside 0..1")
        stage_status = {
            "status": "completed",
            "modeled_tasks": [str(item.get("task_id")) for item in stage["per_task"]],
            "solver": "OpenSim Moco",
            "validated_for_plan": False,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    elif stage["status"] == "failed":
        stage_status = {
            "status": "failed",
            "modeled_tasks": [],
            "solver": "OpenSim Moco",
            "validated_for_plan": False,
            "error": str(stage.get("error") or "Musculoskeletal analysis failed")[:500],
        }
    else:
        raise HTTPException(status_code=422, detail="Model stage status must be completed or failed")

    body_summary = doc.get("body_function_summary") or {}
    model_analysis = dict(doc.get("model_analysis") or {})
    model_analysis["musculoskeletal_stage"] = stage_status
    movement_snapshot_decision = build_movement_snapshot_decision(
        doc.get("task_results", []),
        doc.get("functional_issues", []),
        body_summary,
        doc.get("affected_side", "right"),
        model_analysis,
    )
    insights = build_patient_insights(
        body_summary,
        doc.get("musculoskeletal_outputs") or {},
        stage,
        model_analysis,
    )
    updates = {
        "musculoskeletal_research_stage": stage,
        "model_analysis.musculoskeletal_stage": stage_status,
        "patient_insights": insights,
        "movement_snapshot_decision": movement_snapshot_decision,
    }
    try:
        await db.assessments.update_one({"id": assessment_id}, {"$set": updates})
    except Exception as exc:
        _require_durable_patient_store("assessment model result", exc)
        logger.warning(f"Mongo unavailable for model stage update; local fallback: {str(exc)[:120]}")
        for item in LOCAL_ASSESSMENTS:
            if item.get("id") == assessment_id:
                item["musculoskeletal_research_stage"] = stage
                item.setdefault("model_analysis", {})["musculoskeletal_stage"] = stage_status
                item["patient_insights"] = insights
                item["movement_snapshot_decision"] = movement_snapshot_decision
                break
    _record_alira_action(
        "movement_snapshot_updated",
        source="musculoskeletal_worker",
        user_id=doc.get("user_id"),
        status=stage_status["status"],
        details={"assessment_id": assessment_id, **movement_snapshot_decision},
    )
    return {
        "assessment_id": assessment_id,
        "status": stage_status["status"],
        "insights_ready": insights["status"] in {"research_ready", "validated"},
        "rehab_plan_unlocked": False,
    }


@api_router.post("/assessment/{assessment_id}/model-results")
async def save_model_results(assessment_id: str, payload: ModelResultSubmit, request: Request):
    """Accept trusted, quality-gated per-task solver results from a separate worker."""
    _require_analysis_worker(request)
    try:
        doc = await db.assessments.find_one({"id": assessment_id}, {"_id": 0})
    except Exception as e:
        _require_durable_patient_store("assessment result lookup", e)
        logger.warning(f"Mongo unavailable for model result lookup; local fallback: {str(e)[:120]}")
        doc = next((item for item in LOCAL_ASSESSMENTS if item.get("id") == assessment_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail="Assessment not found")

    task_ids = [
        str(item.get("task_id"))
        for item in doc.get("task_results", [])
        if str(item.get("task_id")) != "L6"
    ]
    manifest_tasks = (doc.get("model_analysis") or {}).get("tasks") or []
    expected_videos = {
        str(item.get("task_id")): item.get("video_id")
        for item in manifest_tasks
        if str(item.get("task_id")) != "L6"
    }
    try:
        validated = validate_model_outputs(payload.model_dump(), task_ids, expected_videos)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    outputs = aggregate_model_outputs(validated)
    patient_parameters = doc.get("patient_parameters") or {}
    task_results = doc.get("task_results") or []
    camera_issues = [
        FunctionalIssue(**item)
        for item in doc.get("functional_issues") or []
        if str(item.get("source") or "") != "Validated musculoskeletal model"
    ]
    issues = merge_validated_model_issues(camera_issues, outputs)
    package_id = str(doc.get("assessment_package") or "upper_limb")
    assigned_task_ids = doc.get("assigned_task_ids") or [str(item.get("task_id")) for item in task_results]
    expected_task_count = len(assigned_task_ids) or len(ASSESSMENT_PACKAGES.get(package_id, {}).get("tasks", []))
    clinical_review_gate = build_clinical_review_gate(
        camera_issues,
        patient_parameters,
        task_results,
        outputs,
        expected_task_count,
    )
    # Model output remains available for the movement report and safety gate,
    # but never selects or replaces exercise IDs in the current policy.
    plan = survey_rehab_plan(patient_parameters)
    if str(patient_parameters.get("movement_pain") or "").lower() == "severe_or_worsening":
        clinical_review_gate = _clinical_gate_with_survey_hold(clinical_review_gate, patient_parameters)
    elif clinical_review_gate.get("status") in {"awaiting_model_analysis", "clear", "no_rehab_needed"}:
        clinical_review_gate = _clinical_gate_with_core_plan(clinical_review_gate, plan)
    else:
        clinical_review_gate = {
            **clinical_review_gate,
            "rehab_plan_source": "fixed_core_programme",
        }
    expected_summary_domains = _expected_domains_for_tasks(package_id, assigned_task_ids)
    body_function_summary = _summary_with_survey_mobility(
        patient_body_function_summary(
            task_results,
            issues,
            outputs,
            expected_summary_domains,
        ),
        patient_parameters,
    )
    completed_model_analysis = dict(doc.get("model_analysis") or {})
    completed_model_analysis.update({
        "status": "completed",
        "musculoskeletal_stage": {
            "status": "validated",
            "modeled_tasks": [str(item.get("task_id")) for item in outputs.get("per_task") or []],
            "validated_for_plan": True,
        },
    })
    movement_snapshot_decision = build_movement_snapshot_decision(
        task_results,
        issues,
        body_function_summary,
        doc.get("affected_side", "right"),
        completed_model_analysis,
    )
    updated_fields = {
        "functional_issues": [issue.model_dump() for issue in issues],
        "rehab_plan": [exercise.model_dump() for exercise in plan],
        "body_function_summary": body_function_summary,
        "musculoskeletal_outputs": outputs,
        "biomechanical_estimates": build_biomechanical_estimates(task_results, patient_parameters, outputs),
        "measurement_form": build_clinical_measurement_form(task_results, patient_parameters, outputs),
        "analysis_pipeline": build_analysis_pipeline(doc.get("motion_data") or {}, outputs),
        "muscle_activation_diagnosis": model_activation_report(outputs),
        "survey_consistency": build_survey_consistency(issues, patient_parameters, task_results),
        "clinical_review_gate": clinical_review_gate,
        "patient_insights": build_patient_insights(
            body_function_summary,
            outputs,
            doc.get("musculoskeletal_research_stage") or {},
            {**(doc.get("model_analysis") or {}), "status": "completed"},
        ),
        "movement_snapshot_decision": movement_snapshot_decision,
        "model_analysis.status": "completed",
        "model_analysis.musculoskeletal_stage": {
            "status": "validated",
            "modeled_tasks": [str(item.get("task_id")) for item in outputs.get("per_task") or []],
            "validated_for_plan": True,
        },
        "model_analysis.completed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await db.assessments.update_one({"id": assessment_id}, {"$set": updated_fields})
    except Exception as e:
        _require_durable_patient_store("assessment model result update", e)
        logger.warning(f"Mongo unavailable for model result update; local fallback: {str(e)[:120]}")
        for item in LOCAL_ASSESSMENTS:
            if item.get("id") == assessment_id:
                for key, value in updated_fields.items():
                    if key.startswith("model_analysis."):
                        item.setdefault("model_analysis", {})[key.split(".", 1)[1]] = value
                    else:
                        item[key] = value
                break
    _record_alira_action(
        "movement_snapshot_updated",
        source="validated_model_worker",
        user_id=doc.get("user_id"),
        status="validated",
        details={"assessment_id": assessment_id, **movement_snapshot_decision},
    )
    return {
        "assessment_id": assessment_id,
        "status": "completed",
        "tasks_modeled": len(outputs.get("per_task") or []),
        "findings_ready": True,
        "clinical_review_status": clinical_review_gate.get("status"),
    }


@api_router.get("/assessment/{assessment_id}/muscle-diagnosis")
async def get_muscle_diagnosis(assessment_id: str, request: Request):
    """Return only validated model-estimated findings, never camera proxies."""
    doc = await _owned_assessment_doc(assessment_id, request, "muscle diagnosis")
    return doc.get("muscle_activation_diagnosis") or pending_model_activation_report()


# ============ Pose Runner HTML (served at /api/pose/runner) ============
# A self-contained page that:
# 1. Opens device camera (getUserMedia)
# 2. Loads MediaPipe PoseLandmarker
# 3. Walks through the assigned tasks and steps with target overlays + voice prompts
# 4. Posts results to React Native via window.ReactNativeWebView.postMessage
POSE_RUNNER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no" />
<title>Pose Assessment</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{width:100%;height:100%;background:#0c100e;color:#fdfdfd;font-family:-apple-system,BlinkMacSystemFont,"Plus Jakarta Sans",sans-serif;overflow:hidden}
  #stage{position:relative;width:100vw;height:100vh;height:100dvh;background:#000}
  #cameraFrame{position:absolute;left:0;top:0;width:100%;height:100%;overflow:hidden;background:#000}
  #cameraFrame video,#cameraFrame canvas{position:absolute;inset:0;width:100%;height:100%;transform:scaleX(-1);transform-origin:center}
  #ui{position:absolute;inset:0;pointer-events:none;display:flex;flex-direction:column;justify-content:space-between;padding:env(safe-area-inset-top,24px) 16px env(safe-area-inset-bottom,24px) 16px}
  #top{display:flex;align-items:center;gap:8px;background:rgba(28,32,29,0.65);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-radius:24px;padding:12px 16px;pointer-events:auto}
  #top .dots{display:flex;gap:6px;flex:1;justify-content:center}
  #top .dot{width:10px;height:10px;border-radius:50%;background:rgba(255,255,255,0.25)}
  #top .dot.active{background:#E18E6D;transform:scale(1.3)}
  #top .dot.done{background:#4A7856}
  #top .label{font-size:14px;font-weight:600;opacity:0.95}
  #exitBtn,#instructionsToggle{background:rgba(255,255,255,0.18);border:none;color:#fff;padding:8px 12px;border-radius:16px;font-weight:600;font-size:13px;pointer-events:auto;cursor:pointer}
  #instructionsToggle{display:none}
  body.step-active #instructionsToggle{display:inline-flex}
  #bottom{background:rgba(28,32,29,0.85);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-radius:24px;padding:16px 18px;pointer-events:auto;transition:opacity .4s ease, transform .4s ease;will-change:opacity,transform}
  /* Keep the target area clear during movement. Captions remain available
     through the compact Instructions control in the top bar. */
  body.step-active:not(.instructions-open) #bottom{opacity:0;visibility:hidden;pointer-events:none;transform:translateY(12px)}
  #stepTitle{font-size:14px;color:#D9E5DC;font-weight:600;margin-bottom:6px}
  #caption{font-size:18px;font-weight:600;line-height:1.35}
  #voiceRow{display:flex;align-items:center;gap:10px;margin-top:10px;opacity:0.85}
  #voiceWave{display:flex;gap:3px;align-items:end;height:14px}
  #voiceWave span{display:block;width:3px;background:#E18E6D;border-radius:2px;animation:wave 1s ease-in-out infinite}
  #voiceWave span:nth-child(1){animation-delay:.0s;height:6px}
  #voiceWave span:nth-child(2){animation-delay:.15s;height:14px}
  #voiceWave span:nth-child(3){animation-delay:.3s;height:8px}
  #voiceWave span:nth-child(4){animation-delay:.45s;height:12px}
  @keyframes wave{0%,100%{transform:scaleY(0.5)}50%{transform:scaleY(1.2)}}
  #voiceText{font-size:13px;color:#D9E5DC}
  #voiceText.voiceRetry{pointer-events:auto;cursor:pointer;color:#fff;text-decoration:underline;text-underline-offset:3px;font-weight:750}
  #skipBtn{margin-top:12px;background:#4A7856;color:#fff;border:none;width:100%;padding:14px;border-radius:16px;font-weight:700;font-size:16px;cursor:pointer}
  #overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:#0c100eee;text-align:center;padding:24px;flex-direction:column;gap:16px;pointer-events:auto;z-index:10}
  #overlay{gap:20px}
  #overlay h1{font-size:22px;font-weight:700;animation:readyRise .6s cubic-bezier(.2,.7,.2,1) both}
  #overlay p{font-size:13px;color:#98a39b;line-height:18px;max-width:440px;animation:readyRise .6s .9s cubic-bezier(.2,.7,.2,1) both}
  #overlay button{background:#4A7856;color:#fff;border:none;padding:14px 28px;border-radius:18px;font-weight:700;font-size:18px;display:inline-flex;align-items:center;justify-content:center;gap:10px;cursor:pointer;animation:readyRise .6s .45s cubic-bezier(.2,.7,.2,1) backwards,readyBeckon 2.4s 1.3s ease-out infinite;transition:transform .18s ease,background-color .18s ease}
  #overlay button:hover{transform:translateY(-2px);background-color:#5a8f68}
  #overlay button:disabled,#overlay button[aria-busy="true"]{animation:readyRise .6s .45s cubic-bezier(.2,.7,.2,1) backwards;transform:none;background:#4A7856;cursor:progress}
  #overlay button svg{flex:0 0 auto;transition:transform .18s ease}
  #overlay button:hover svg{transform:translateX(4px)}
  #overlay button[data-module-failed="1"]+p{color:#E9B79F;font-size:14px}
  @keyframes readyRise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
  @keyframes readyBeckon{0%{box-shadow:0 0 0 0 rgba(127,229,163,.55)}70%{box-shadow:0 0 0 18px rgba(127,229,163,0)}100%{box-shadow:0 0 0 0 rgba(127,229,163,0)}}
  @media (prefers-reduced-motion:reduce){#overlay h1,#overlay p,#overlay button{animation:none}}
  #calibrationOverlay{position:absolute;inset:0;display:flex;align-items:flex-start;justify-content:center;background:linear-gradient(180deg,rgba(12,16,14,.62),rgba(12,16,14,.12));padding:calc(16px + env(safe-area-inset-top,0px)) 16px 16px;pointer-events:auto;z-index:12}
  #calibrationOverlay .calibrationPanel{width:min(520px,100%);background:rgba(253,253,253,.96);color:#1C201D;border-radius:8px;padding:18px;box-shadow:0 18px 55px rgba(0,0,0,.30);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}
  #calibrationOverlay h2{font-size:22px;line-height:1.2;margin-bottom:6px}
  #calibrationOverlay .calibrationLead{font-size:15px;line-height:1.4;color:#49504B;margin-bottom:12px}
  #calibrationChecklist{display:grid;gap:7px;margin:10px 0 14px}
  .calibrationCheck{display:flex;align-items:center;gap:9px;font-size:14px;font-weight:650;color:#49504B}
  .calibrationCheck .statusDot{display:grid;place-items:center;width:22px;height:22px;flex:0 0 22px;border-radius:50%;background:#E8ECE8;color:#69716B;font-size:13px}
  .calibrationCheck.done{color:#285C3A}
  .calibrationCheck.done .statusDot{background:#D9E5DC;color:#285C3A}
  #calibrationQuality{display:grid;gap:8px;border-top:1px solid #D9E5DC;padding-top:9px}
  #calibrationQuality .calibrationCheck{align-items:flex-start}
  #calibrationQuality .qualityCheckBody{flex:1;min-width:0}
  #calibrationQuality .qualityCheckHeading{display:flex;justify-content:space-between;gap:8px}
  #calibrationQuality .qualityCheckState{font-size:12px;white-space:nowrap}
  #calibrationQuality .qualityCheckDetail{display:block;font-size:12px;font-weight:400;line-height:1.35;color:#49504B;margin-top:2px;overflow-wrap:anywhere}
  #calibrationQuality [data-state="blocked"] .qualityCheckState{color:#A33724}
  #calibrationQuality [data-state="blocked"] .statusDot{background:#F8E7E2;color:#A33724}
  #calibrationOverlay.testingReachCalibration .calibrationPanel{max-height:calc(100% - 16px);overflow-y:auto}
  #calibrationProgress{height:6px;background:#E4E9E5;border-radius:3px;overflow:hidden;margin-bottom:14px}
  #calibrationProgressFill{height:100%;width:0;background:#4A7856;transition:width .2s ease}
  #calibrationAutoStatus{width:100%;border-radius:8px;padding:13px 16px;background:#E8ECE8;color:#56605A;font-size:15px;font-weight:750;text-align:center}
  #calibrationAutoStatus.ready{background:#4A7856;color:#fff}
  /* Patient calibration: one continuous progress ring and one completion check.
     The testing panels keep the original checklist via .testingReachCalibration. */
  #calibrationRing{display:none}
  #calibrationOverlay:not(.testingReachCalibration){align-items:center;background:radial-gradient(circle at 50% 44%,rgba(12,16,14,.22) 0%,rgba(12,16,14,.5) 42%,rgba(12,16,14,.8) 100%);padding:calc(16px + env(safe-area-inset-top,0px)) 16px calc(16px + env(safe-area-inset-bottom,0px))}
  #calibrationOverlay:not(.testingReachCalibration) .calibrationPanel{width:min(560px,100%);background:transparent;color:#fdfdfd;box-shadow:none;backdrop-filter:none;-webkit-backdrop-filter:none;padding:0;display:flex;flex-direction:column;align-items:center;text-align:center}
  #calibrationOverlay:not(.testingReachCalibration) h2{order:1;font-size:clamp(28px,4.2vw,42px);font-weight:750;line-height:1.2;letter-spacing:-.02em;text-transform:none;text-wrap:balance;color:#F3FFF7;margin:0 0 24px;max-width:540px;text-shadow:0 2px 12px rgba(0,0,0,.9);transform-origin:center;animation:calTitleBreathe 3.6s ease-in-out infinite}
  #calibrationOverlay:not(.testingReachCalibration):has(#calibrationRing.complete) h2{animation:none;color:#B7FFD0}
  #calibrationOverlay:not(.testingReachCalibration) #calibrationRing{display:block;order:2}
  #calibrationOverlay:not(.testingReachCalibration) .calibrationLead{display:none}
  #calibrationOverlay:not(.testingReachCalibration) #calibrationChecklist,#calibrationOverlay:not(.testingReachCalibration) #calibrationProgress{position:absolute;width:1px;height:1px;margin:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
  #calibrationOverlay:not(.testingReachCalibration) #calibrationAutoStatus{display:none}
  #calibrationRing{--calRingSize:min(72vw,38vh,320px);position:relative;width:var(--calRingSize);height:var(--calRingSize)}
  #calibrationRing::before{content:"";position:absolute;inset:9%;border:1px solid rgba(127,229,163,.18);border-radius:50%;box-shadow:0 0 25px rgba(127,229,163,.13),inset 0 0 25px rgba(127,229,163,.08);animation:calAuraBreathe 3.6s ease-in-out infinite;pointer-events:none}
  #calibrationRing::after{content:"";position:absolute;inset:18%;border-radius:50%;background:radial-gradient(circle,rgba(127,229,163,.12),rgba(127,229,163,.04) 55%,transparent 72%);animation:calCoreBreathe 3.6s ease-in-out infinite;pointer-events:none}
  #calibrationRing .calRingSvg{position:absolute;inset:0;width:100%;height:100%;overflow:visible;z-index:1;transform-origin:center;animation:calRingBreathe 3.6s ease-in-out infinite}
  #calibrationRing .calRingTrack{fill:none;stroke:rgba(211,248,225,.2);stroke-width:12}
  #calibrationRing .calRingFill{fill:none;stroke:url(#calProgressGradient);stroke-width:12;stroke-linecap:round;stroke-dasharray:100;stroke-dashoffset:100;transition:stroke-dashoffset .8s linear;filter:drop-shadow(0 0 5px rgba(127,229,163,.55))}
  #calibrationRingTip{transform-origin:200px 200px;transform:rotate(-90deg);transition:transform .8s linear,opacity .25s ease}
  #calibrationRingTip circle{fill:#E5FFEF;filter:drop-shadow(0 0 7px #7FE5A3)}
  #calibrationRing.tracking::before{border-color:rgba(127,229,163,.4);animation-duration:2.6s}
  #calibrationRing.tracking .calRingFill{filter:drop-shadow(0 0 9px rgba(127,229,163,.8))}
  #calibrationRing.complete::before{animation:calAuraComplete 1.2s .8s ease-out both}
  #calibrationRing.complete::after{animation:none;opacity:.65}
  #calibrationRing.complete #calibrationRingTip{opacity:0;transition-delay:0s,.8s}
  #calibrationRing .calCenterCheck{fill:none;stroke:#7FE5A3;stroke-width:18;stroke-linecap:round;stroke-linejoin:round;opacity:0;transform:scale(.85);transform-origin:200px 200px;transition:opacity .2s ease,transform .2s ease}
  #calibrationRing.complete .calCenterCheck{opacity:1;transform:scale(1);transition-delay:.8s}
  #calibrationRing.complete .calRingSvg{animation:calCompleteGlow 1.2s .8s ease-out both}
  @keyframes calCompleteGlow{0%{filter:drop-shadow(0 0 0 rgba(127,229,163,0))}40%{filter:drop-shadow(0 0 12px rgba(127,229,163,.95)) drop-shadow(0 0 28px rgba(127,229,163,.6))}100%{filter:drop-shadow(0 0 8px rgba(127,229,163,.7)) drop-shadow(0 0 18px rgba(127,229,163,.35))}}
  @keyframes calRingBreathe{0%,100%{transform:scale(.99)}50%{transform:scale(1.025)}}
  @keyframes calTitleBreathe{0%,100%{transform:scale(1);opacity:.92;text-shadow:0 2px 12px rgba(0,0,0,.9),0 0 8px rgba(127,229,163,.08)}50%{transform:scale(1.025);opacity:1;text-shadow:0 2px 12px rgba(0,0,0,.9),0 0 22px rgba(127,229,163,.5)}}
  @keyframes calAuraBreathe{0%,100%{transform:scale(.98);opacity:.35}50%{transform:scale(1.075);opacity:.85}}
  @keyframes calCoreBreathe{0%,100%{transform:scale(.92);opacity:.45}50%{transform:scale(1.06);opacity:1}}
  @keyframes calAuraComplete{0%{transform:scale(1);opacity:.75}100%{transform:scale(1.3);opacity:0}}
  @media (prefers-reduced-motion:reduce){#calibrationOverlay h2,#calibrationRing *,#calibrationRing::before,#calibrationRing::after{transition:none!important;animation:none!important}#calibrationRing.complete .calRingSvg{filter:drop-shadow(0 0 10px rgba(127,229,163,.65))}#calibrationRing.complete::before{opacity:0}}
  #walkingCapture{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(12,16,14,.94);padding:20px;pointer-events:auto;z-index:13}
  #walkingCapture .walkingCard{width:min(560px,100%);max-height:92vh;overflow:auto;background:#FDFDFD;color:#1C201D;border-radius:8px;padding:22px;box-shadow:0 24px 80px rgba(0,0,0,.38)}
  #walkingCapture h2{font-size:24px;line-height:1.2;margin-bottom:10px}
  #walkingCapture p{font-size:15px;line-height:1.45;color:#414843}
  #walkingCapture ul{padding-left:20px;margin:14px 0}
  #walkingCapture li{font-size:14px;line-height:1.4;margin:6px 0;color:#303632}
  #walkingCapture button{width:100%;border:none;border-radius:8px;padding:15px 16px;background:#4A7856;color:#fff;font-size:16px;font-weight:800;cursor:pointer}
  #walkingCapture button:disabled{background:#C9D2CB;color:#667068;cursor:not-allowed}
  #walkingDesktopActions{width:100%}
  #walkingVideoDropZone{width:100%;border:2px dashed #97AA9B;border-radius:8px;padding:18px;background:#F6F8F5;text-align:center;transition:border-color .18s ease,background .18s ease,box-shadow .18s ease}
  #walkingVideoDropZone.dragover{border-color:#315D3D;background:#E6F0E8;box-shadow:0 0 0 3px rgba(74,120,86,.16)}
  #walkingVideoDropZone.busy{opacity:.65;cursor:wait}
  .walkingDropIcon{width:42px;height:42px;margin:0 auto 8px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#D9E5DC;color:#315D3D;font-size:25px;font-weight:800;line-height:1}
  .walkingDropHint{font-size:13px;color:#59615B;margin:4px 0 12px}
  #walkingPickerButton{position:relative;width:100%}
  #walkingChooseVideoBtn{pointer-events:none}
  #walkingVideoInput{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer;z-index:2;font-size:0}
  #walkingDesktopActions.busy #walkingVideoInput{pointer-events:none;cursor:not-allowed}
  #walkingProceedUnconfirmedBtn{margin-top:10px;background:#FDFDFD;color:#315D3D;border:2px solid #4A7856}
  #walkingProceedUnconfirmedBtn:disabled{background:#EEF0ED;color:#667068;border-color:#C9D2CB}
  #walkingSkipBtn{margin-top:10px;background:#FDFDFD;color:#315D3D;border:2px solid #AAB6AC}
  #walkingSkipBtn:disabled{background:#EEF0ED;color:#667068;border-color:#C9D2CB}
  #walkingCaptureStatus{margin-top:12px;padding:11px 12px;border-radius:8px;background:#EEF0ED;color:#49504B;font-size:14px;line-height:1.4}
  #walkingCaptureStatus.good{background:#D9E5DC;color:#285C3A;font-weight:700}
  #walkingCaptureStatus.warn{background:#FFF0E6;color:#7A351E;font-weight:700}
  /* Walking card: direct instruction, front-view demonstration, Upload / Record tabs. */
  #walkingCapture{background:rgba(12,16,14,.8)}
  #walkingCapture .walkingCard{width:min(620px,100%);border-radius:22px;padding:24px;animation:wkPop .6s cubic-bezier(.2,.7,.2,1) both}
  #walkingCapture h2{font-size:26px;line-height:32px;font-weight:700;margin-bottom:8px}
  #walkingCapture #walkingCaptureLead{line-height:22px;margin-bottom:12px}
  #walkingCapture:not(.walkingTestMode) #walkingCaptureGuidance{display:none}
  #walkingScene{position:relative;width:100%;border-radius:14px;background:#eef4ef;overflow:hidden;margin:0 0 16px}
  #walkingScene svg{display:block;width:100%;height:auto}
  #walkingModeTabs{display:flex;border-radius:12px;background:#e8ece8;padding:4px;margin:0 0 16px}
  #walkingCapture #walkingModeTabs button{flex:1;width:auto;height:36px;padding:0 8px;border:none;border-radius:9px;background:transparent;color:#56605a;font-size:14px;font-weight:600;box-shadow:none}
  #walkingCapture #walkingModeTabs button.selected{background:#fff;color:#1c201d;font-weight:700;box-shadow:0 1px 3px rgba(0,0,0,.12)}
  #walkingCapture.walkingTestMode #walkingScene,#walkingCapture.walkingTestMode #walkingModeTabs{display:none}
  #walkingVideoDropZone{border-radius:14px;padding:14px}
  .walkingDropIcon{display:none}
  #walkingCapture #walkingPickerButton{width:200px;max-width:100%;margin:0 auto}
  #walkingCapture #walkingChooseVideoBtn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:52px;padding:0 20px;border-radius:12px;animation:wkBeckon 2.4s 1.2s ease-out infinite}
  #walkingCapture #walkingChooseVideoBtn:disabled{animation:none}
  #walkingCapture #walkingChooseVideoBtn svg{width:18px;height:18px;flex:0 0 auto}
  #walkingCapture #walkingSkipBtn{display:block;width:auto;margin:14px auto 0;padding:8px 12px;background:transparent;border:none;color:#56605a;font-size:14px;font-weight:600;text-decoration:underline;text-underline-offset:3px}
  #walkingCapture #walkingSkipBtn:disabled{background:transparent;border:none;color:#9aa39c}
  #walkingCaptureStatus{margin-top:14px;text-align:center}
  #walkingScene .wkApproach{transform:translate(196px,187px) scale(.92);animation:wkApproach 5.6s ease-in-out infinite}
  #walkingScene .wkPhoneApproach{transform:translate(430px,186px) scale(.72);animation:wkPhoneApproach 5.6s ease-in-out infinite}
  #walkingScene .wkStepLeft{transform-origin:-11px -66px;animation:wkStep 1.4s ease-in-out infinite}
  #walkingScene .wkStepRight{transform-origin:11px -66px;animation:wkStep 1.4s -.7s ease-in-out infinite}
  #walkingScene .wkSwing{transform-origin:-22px -112px;animation:wkSwing 1.4s ease-in-out infinite}
  #walkingScene .wkToward{animation:wkToward 2.2s ease-in-out infinite}
  #walkingScene .wkToward.second{animation-delay:.4s}
  #walkingScene .wkToward.third{animation-delay:.8s}
  @keyframes wkPop{0%{opacity:0;transform:scale(.94) translateY(12px)}100%{opacity:1;transform:none}}
  @keyframes wkBeckon{0%{box-shadow:0 0 0 0 rgba(74,120,86,.45)}70%{box-shadow:0 0 0 14px rgba(74,120,86,0)}100%{box-shadow:0 0 0 0 rgba(74,120,86,0)}}
  @keyframes wkApproach{0%,10%{transform:translate(196px,161px) scale(.68);opacity:1}78%,88%{transform:translate(196px,194px) scale(1);opacity:1}94%{transform:translate(196px,194px) scale(1);opacity:0}95%{transform:translate(196px,161px) scale(.68);opacity:0}100%{transform:translate(196px,161px) scale(.68);opacity:1}}
  @keyframes wkPhoneApproach{0%,10%{transform:translate(430px,174px) scale(.52);opacity:1}78%,88%{transform:translate(430px,186px) scale(.74);opacity:1}94%{transform:translate(430px,186px) scale(.74);opacity:0}95%{transform:translate(430px,174px) scale(.52);opacity:0}100%{transform:translate(430px,174px) scale(.52);opacity:1}}
  @keyframes wkStep{0%,100%{transform:translateY(0) scaleY(1)}50%{transform:translateY(-3px) scaleY(.91)}}
  @keyframes wkSwing{0%,100%{transform:rotate(-4deg)}50%{transform:rotate(5deg)}}
  @keyframes wkToward{0%,100%{opacity:.2}50%{opacity:.85}}
  @media (prefers-reduced-motion:reduce){#walkingCapture *{animation:none!important}}
  body.walking-camera-unmirrored #cameraFrame video,body.walking-camera-unmirrored #cameraFrame canvas{transform:none}
  #advancedMarkerGate{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:#0c100ef5;padding:20px;pointer-events:auto;z-index:11}
  #advancedMarkerGate .gateCard{width:min(520px,100%);max-height:92vh;overflow:auto;background:#FDFDFD;color:#1C201D;border-radius:24px;padding:22px;text-align:left;box-shadow:0 24px 80px rgba(0,0,0,.35)}
  #advancedMarkerGate .gateEyebrow{color:#4A7856;font-size:13px;font-weight:800;letter-spacing:.5px;text-transform:uppercase;margin-bottom:8px}
  #advancedMarkerGate h2{font-size:24px;line-height:1.2;color:#1C201D;margin-bottom:10px}
  #advancedMarkerGate p{font-size:16px;line-height:1.45;color:#2C312E;margin-bottom:12px}
  #advancedMarkerGate .gateBox{background:#D9E5DC;border-radius:16px;padding:14px;margin:14px 0;color:#253C2B}
  #advancedMarkerGate .gateBox.warn{background:#FFF4E8;color:#503018}
  #advancedMarkerGate ul{padding-left:18px;margin:8px 0 0}
  #advancedMarkerGate li{font-size:15px;line-height:1.45;margin:4px 0}
  #advancedMarkerGate .gateActions{display:flex;flex-direction:column;gap:10px;margin-top:16px}
  #advancedMarkerGate button{border:none;border-radius:16px;padding:15px 16px;font-size:16px;font-weight:800;cursor:pointer;text-align:center}
  #markerConfirmBtn{background:#4A7856;color:#fff}
  #markerMissingBtn{background:#E18E6D;color:#1C201D}
  #markerBasicBtn,#markerBackBtn{background:#EEF0ED;color:#1C201D}
  #markerStoreBtn{background:#4A7856;color:#fff}
  #advancedMarkerGate .missingPanel{margin-top:14px;border-top:1px solid #E3E6E1;padding-top:14px}
  #diagnosticsBadge{position:absolute;right:16px;top:84px;z-index:4;background:rgba(28,32,29,.72);color:#FDFDFD;border-radius:18px;padding:9px 11px;font-size:12px;line-height:1.35;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);pointer-events:none;min-width:128px}
  #diagnosticsBadge strong{display:block;color:#D9E5DC;font-size:12px;margin-bottom:2px}
  #diagnosticsBadge.good{border:1px solid rgba(127,229,163,.65)}
  #diagnosticsBadge.warn{border:1px solid rgba(225,142,109,.75)}
  #lapStatus{position:absolute;left:50%;top:56%;transform:translate(-50%,-50%);z-index:4;width:min(340px,calc(100% - 40px));background:rgba(28,32,29,.82);color:#FDFDFD;border:1px solid rgba(217,229,220,.55);border-radius:16px;padding:10px 14px;text-align:center;font-size:14px;font-weight:650;line-height:1.35;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);pointer-events:none}
  .hidden{display:none !important}
  /* Celebration overlay */
  #celebrate{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:linear-gradient(180deg, rgba(74,120,86,0.92), rgba(28,32,29,0.92));text-align:center;padding:24px;flex-direction:column;gap:14px;pointer-events:auto;z-index:9;opacity:0;transition:opacity .35s ease-out}
  #celebrate.show{opacity:1}
  #celebrate .star{font-size:80px;animation:pop .6s ease-out}
  #celebrate h2{font-size:26px;font-weight:800;color:#fff}
  #celebrate .next{font-size:14px;color:#D9E5DC;font-weight:600;letter-spacing:1px;text-transform:uppercase}
  #celebrate .msg{font-size:17px;color:#FDFDFD;line-height:1.45;max-width:320px}
  #celebrate .dotsMini{display:flex;gap:6px;margin-top:8px}
  #celebrate .dotsMini span{width:8px;height:8px;border-radius:50%;background:rgba(255,255,255,0.3)}
  #celebrate .dotsMini span.done{background:#fff}
  @keyframes pop{0%{transform:scale(0.3) rotate(-12deg);opacity:0}60%{transform:scale(1.2) rotate(8deg);opacity:1}100%{transform:scale(1) rotate(0);opacity:1}}
  .confetti{position:absolute;width:10px;height:14px;border-radius:2px;animation:fall 1.4s ease-out forwards;opacity:.9}
  @keyframes fall{0%{transform:translateY(-30vh) rotate(0deg);opacity:0}10%{opacity:1}100%{transform:translateY(120vh) rotate(720deg);opacity:0}}
</style>
</head>
<body>
<div id="stage">
  <div id="cameraFrame">
    <video id="video" playsinline autoplay muted></video>
    <canvas id="canvas"></canvas>
  </div>
  <div id="lapStatus" class="hidden" role="status">Keep your affected hand resting on the visible part of your lap while Rehyn locates the target.</div>
  <div id="ui">
    <div id="top">
      <button id="exitBtn" data-testid="assessment-exit">Exit</button>
      <button id="instructionsToggle" type="button" aria-controls="bottom" aria-expanded="false">Instructions</button>
      <div class="dots" id="dots"></div>
      <div class="label" id="taskLabel">T1</div>
    </div>
    <div id="bottom">
      <div id="stepTitle" class="hidden">Task 1 of 7</div>
      <div id="caption">Preparing…</div>
      <div id="voiceRow" class="hidden">
        <div id="voiceWave"><span></span><span></span><span></span><span></span></div>
        <div id="voiceText">Listening to instructions…</div>
      </div>
      <button id="skipBtn" class="hidden" data-testid="assessment-skip">Skip step</button>
    </div>
  </div>
  <div id="overlay">
    <h1>Ready to begin?</h1>
    <button id="startBtn" data-testid="assessment-start">Yes, I'm ready<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"></path></svg></button>
    <p class="hidden" role="status"></p>
  </div>
  <div id="calibrationOverlay" class="hidden" data-testid="assessment-calibration">
    <div class="calibrationPanel">
      <h2 id="calibrationTitle">Let us find your seated position</h2>
      <p class="calibrationLead" id="calibrationLead">Sit still with your affected hand resting on your lap. Keep your face, shoulders, affected arm, hips, and knees inside the camera view.</p>
      <div id="calibrationRing" aria-hidden="true">
        <svg class="calRingSvg" viewBox="0 0 400 400">
          <defs><linearGradient id="calProgressGradient" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#45C78D"></stop><stop offset=".55" stop-color="#7FE5A3"></stop><stop offset="1" stop-color="#D5FFE4"></stop></linearGradient></defs>
          <circle class="calRingTrack" cx="200" cy="200" r="150"></circle>
          <circle id="calibrationRingFill" class="calRingFill" cx="200" cy="200" r="150" pathLength="100" transform="rotate(-90 200 200)"></circle>
          <g id="calibrationRingTip"><circle cx="350" cy="200" r="7"></circle></g>
          <path class="calCenterCheck" d="M137 201l43 43 83-88"></path>
        </svg>
      </div>
      <div id="calibrationChecklist">
        <div class="calibrationCheck" id="calibrationCamera"><span class="statusDot">1</span><span>Camera is ready</span></div>
        <div class="calibrationCheck" id="calibrationArm"><span class="statusDot">2</span><span>Face, shoulders, and affected arm are visible</span></div>
        <div class="calibrationCheck" id="calibrationSeat"><span class="statusDot">3</span><span>Affected hand and part of your lap are visible</span></div>
        <div class="calibrationCheck" id="calibrationLap"><span class="statusDot">4</span><span>Hold still while your lap target is located</span></div>
        <div class="hidden" id="calibrationQuality">
          <div class="calibrationCheck" data-quality-check="lighting" data-check-number="5"><span class="statusDot">5</span><div class="qualityCheckBody"><div class="qualityCheckHeading"><span>Lighting</span><span class="qualityCheckState">Waiting</span></div><span class="qualityCheckDetail"></span></div></div>
          <div class="calibrationCheck" data-quality-check="contrast" data-check-number="6"><span class="statusDot">6</span><div class="qualityCheckBody"><div class="qualityCheckHeading"><span>Clothing/background contrast</span><span class="qualityCheckState">Waiting</span></div><span class="qualityCheckDetail"></span></div></div>
          <div class="calibrationCheck" data-quality-check="landmarks" data-check-number="7"><span class="statusDot">7</span><div class="qualityCheckBody"><div class="qualityCheckHeading"><span>Grading landmark confidence</span><span class="qualityCheckState">Waiting</span></div><span class="qualityCheckDetail"></span></div></div>
          <div class="calibrationCheck" data-quality-check="angles" data-check-number="8"><span class="statusDot">8</span><div class="qualityCheckBody"><div class="qualityCheckHeading"><span>Angle measurements</span><span class="qualityCheckState">Waiting</span></div><span class="qualityCheckDetail"></span></div></div>
          <div class="calibrationCheck" data-quality-check="stability" data-check-number="9"><span class="statusDot">9</span><div class="qualityCheckBody"><div class="qualityCheckHeading"><span>Two-second tracking stability</span><span class="qualityCheckState">Waiting</span></div><span class="qualityCheckDetail"></span></div></div>
          <div class="calibrationCheck" data-quality-check="baseline" data-check-number="10"><span class="statusDot">10</span><div class="qualityCheckBody"><div class="qualityCheckHeading"><span>Upright posture reference</span><span class="qualityCheckState">Waiting</span></div><span class="qualityCheckDetail"></span></div></div>
        </div>
      </div>
      <div id="calibrationProgress"><div id="calibrationProgressFill"></div></div>
      <div id="calibrationAutoStatus" role="status" data-testid="calibration-auto-status">Keep still. Assessment will start automatically.</div>
    </div>
  </div>
  <div id="walkingCapture" class="hidden" data-testid="walking-capture">
    <div class="walkingCard">
      <h2 id="walkingCaptureTitle">Walk toward the camera</h2>
      <p id="walkingCaptureLead">Ask someone to film you from the front while you walk toward the camera at your usual pace, with your usual walking aid.</p>
      <div id="walkingScene" role="img" aria-label="A person walks forward toward a stationary camera. The phone shows their whole body from the front.">
        <svg viewBox="0 0 572 222" aria-hidden="true">
          <defs>
            <linearGradient id="wkRoom" x2="0" y2="1"><stop stop-color="#F4F7F0"/><stop offset="1" stop-color="#DFECE2"/></linearGradient>
            <clipPath id="wkPhoneScreen"><rect x="386" y="34" width="88" height="159" rx="10"/></clipPath>
            <g id="wkPerson">
              <ellipse cx="3" cy="2" rx="35" ry="5" fill="#2A503C" opacity=".12"/>
              <g class="wkStepLeft"><path d="M-21-70h21l-4 61h-17z" fill="#344E57"/><path d="M-21-13h17v10q-2 5-21 3l-1-5z" fill="#FCFDF6" stroke="#526565" stroke-width="1.5"/></g>
              <g class="wkStepRight"><path d="M1-70h21l-1 61H5z" fill="#425F67"/><path d="M5-13h17l5 10q-1 5-21 3z" fill="#FCFDF6" stroke="#526565" stroke-width="1.5"/></g>
              <g class="wkSwing"><path d="M-22-115q-9 11-10 29l-1 13" fill="none" stroke="#709F88" stroke-width="14" stroke-linecap="round"/><path d="M-33-76v10" stroke="#BF8969" stroke-width="10" stroke-linecap="round"/></g>
              <path d="M22-115q8 16 9 24l8 15" fill="none" stroke="#709F88" stroke-width="14" stroke-linecap="round"/>
              <path d="M34-73q8-8 11-1l2 74" fill="none" stroke="#B3844F" stroke-width="4" stroke-linecap="round"/><path d="M43 1h8" stroke="#425649" stroke-width="4" stroke-linecap="round"/>
              <path d="M35-80l5 7" stroke="#BF8969" stroke-width="10" stroke-linecap="round"/>
              <path d="M-11-127q-14 1-16 15l5 46q22 7 44 0l5-46q-2-14-16-15z" fill="#83B299"/>
              <path d="M-10-128v9q10 11 20 0v-9" fill="#BF8969"/>
              <path d="M-10-121l10 9 10-9" fill="none" stroke="#CDE4D4" stroke-width="2.5"/>
              <ellipse cy="-145" rx="18" ry="22" fill="#C89575"/>
              <path d="M-18-145q-6-24 15-26 22-2 22 26l-6-12q-12 6-23-1z" fill="#5C665E"/>
              <path d="M-17-153l1 14M17-153l-1 14" stroke="#CBD0BF" stroke-width="3" stroke-linecap="round"/>
              <circle cx="-6" cy="-145" r="1.6" fill="#34463A"/><circle cx="6" cy="-145" r="1.6" fill="#34463A"/>
              <path d="M-5-135q5 4 10 0" fill="none" stroke="#805744" stroke-width="1.8" stroke-linecap="round"/>
            </g>
          </defs>
          <rect width="572" height="222" fill="url(#wkRoom)"/>
          <path d="M0 134h572v88H0z" fill="#D8E5DA"/>
          <path d="M159 134L102 222M229 134l58 88" fill="none" stroke="#C2D5C7" stroke-width="2"/>
          <path d="M136 222l35-87h47l36 87" fill="#F1F5EB" opacity=".65"/>
          <g transform="translate(56 97)"><path d="M-13 20h29l-4 24H-9z" fill="#D1BBA0"/><path d="M1 21V-24M1 4q-23-2-20-21Q0-15 1 4M1-8q20-1 19-20Q2-24 1-8" fill="#83A68C" stroke="#729A7E" stroke-width="2"/></g>
          <g fill="none" stroke="#78A68B" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
            <path class="wkToward" d="M188 187l8 5 8-5"/><path class="wkToward second" d="M185 198l11 6 11-6"/><path class="wkToward third" d="M182 210l14 7 14-7"/>
          </g>
          <g class="wkApproach"><use href="#wkPerson"/></g>
          <path d="M312 113h29m-6-6 7 6-7 6" fill="none" stroke="#72977F" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M473 156q17-7 22 8l7 29 23 29h-62l-15-35z" fill="#C89575"/>
          <path d="M481 192l18-9 36 39h-65z" fill="#779AAB"/>
          <rect x="376" y="21" width="108" height="184" rx="18" fill="#294B3B"/>
          <rect x="382" y="27" width="96" height="172" rx="13" fill="#FDFEF8"/>
          <g clip-path="url(#wkPhoneScreen)">
            <rect x="386" y="34" width="88" height="159" fill="#E6EFE4"/>
            <path d="M386 151h88v42h-88z" fill="#D1E0D2"/>
            <g class="wkPhoneApproach"><use href="#wkPerson"/></g>
          </g>
          <path d="M416 30h28" stroke="#294B3B" stroke-width="4" stroke-linecap="round"/>
          <path d="M395 62v-9h9m52 0h9v9m0 112v9h-9m-52 0h-9v-9" fill="none" stroke="#709A7E" stroke-width="2" stroke-linecap="round"/>
          <path d="M481 167q-13-7-15 4l4 15" fill="#C89575" stroke="#B58364" stroke-width="1.5"/>
          <text x="430" y="216" text-anchor="middle" fill="#456653" font-family="sans-serif" font-size="11" font-weight="600">Camera view</text>
        </svg>
      </div>
      <div id="walkingModeTabs" role="tablist" aria-label="How to add the walking video">
        <button type="button" role="tab" data-walking-mode="upload" class="selected" aria-selected="true">Upload a video</button>
        <button type="button" role="tab" data-walking-mode="record" aria-selected="false">Record now</button>
      </div>
      <ul id="walkingCaptureGuidance">
        <li>A short video is fine. Keep the patient's whole body and usual walking aid visible when possible.</li>
        <li>A fixed camera is best. If the route does not fit, move smoothly from a safe position beside the path; do not walk backward in front of the patient.</li>
        <li>The video is saved as assessment evidence. Rehyn uses the measured 2D result when available and a rough score if processing is interrupted.</li>
        <li>The survey identifies the affected lower-limb area and side. The walking task provides the numeric lower-limb score.</li>
      </ul>
      <div id="walkingDesktopActions" class="hidden" data-testid="walking-desktop-actions">
        <div id="walkingVideoDropZone" data-testid="walking-video-drop-zone">
          <div class="walkingDropIcon" aria-hidden="true">&#8593;</div>
          <div id="walkingPickerButton">
            <button id="walkingChooseVideoBtn" type="button" tabindex="-1" data-testid="walking-choose-video"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"></path></svg><span id="walkingChooseVideoLabel">Choose video</span></button>
            <input id="walkingVideoInput" type="file" accept="video/*" aria-label="Choose walking video" data-testid="walking-video-input" />
          </div>
        </div>
      </div>
      <div id="walkingMobileActions" class="hidden" data-testid="walking-mobile-actions">
        <button id="walkingRecordBtn" type="button" data-testid="walking-start-recording">Start recording walking</button>
      </div>
      <button id="walkingProceedUnconfirmedBtn" class="hidden" type="button" data-testid="walking-proceed-identity-unconfirmed">Use video and mark for review</button>
      <button id="walkingSkipBtn" type="button" data-testid="walking-skip">Skip walking for now</button>
      <div id="walkingCaptureStatus" class="hidden" role="status"></div>
      <video id="walkingReviewVideo" class="hidden" playsinline muted preload="metadata"></video>
    </div>
  </div>
  <div id="advancedMarkerGate" class="hidden" data-testid="advanced-marker-gate">
    <div class="gateCard">
      <div class="gateEyebrow">AxonAI Hand Function Package</div>
      <h2>Next: advanced grasp-release task</h2>
      <p>This task observes you picking up and releasing a real lightweight object. Move slowly, and keep safety first.</p>
      <div class="gateBox">
        <p><strong>Please prepare one light, safe, easy-to-hold object:</strong></p>
        <ul>
          <li>Empty plastic cup</li>
          <li>Soft ball</li>
          <li>Light foam cylinder</li>
          <li>Small paper box</li>
        </ul>
      </div>
      <div class="gateBox warn">
        <p><strong>Please do not use:</strong> glass cups, hot drinks, heavy objects, sharp objects, or items that are very small, reflective, or transparent.</p>
        <p>To help AxonAI see the object clearly, place the AxonAI marker on the front of the object and keep the marker facing the camera.</p>
      </div>
      <div class="gateActions" id="markerChoicePanel">
        <button id="markerConfirmBtn" data-testid="marker-confirmed">I have an AxonAI marker and placed it on the object</button>
        <button id="markerMissingBtn" data-testid="marker-missing">I do not have a marker yet</button>
      </div>
      <div class="missingPanel hidden" id="markerMissingPanel">
        <p><strong>That is okay. You can complete the basic hand function tasks first.</strong></p>
        <p>Without a marker, AxonAI cannot reliably tell whether the object was grasped or released. When your marker is ready, you can return to the advanced grasp-release task.</p>
        <div class="gateActions">
          <button id="markerStoreBtn" data-testid="marker-store">Order AxonAI marker</button>
          <button id="markerBasicBtn" data-testid="marker-basic-fallback">Do basic hand tasks first</button>
          <button id="markerBackBtn" data-testid="marker-back-choice">Back to choices</button>
        </div>
      </div>
    </div>
  </div>
  <div id="diagnosticsBadge" class="hidden" data-testid="runtime-diagnostics">
    <strong>Frame status</strong>
    <span id="diagnosticsText">Preparing</span>
  </div>
  <div id="celebrate" class="hidden">
    <div class="star">&#11088;</div>
    <div class="celebrateMark" aria-hidden="true">
      <svg class="celebrateRipples" viewBox="0 0 400 400"><circle class="celebrateRipple" cx="200" cy="200" r="72"></circle><circle class="celebrateRipple celebrateRipple2" cx="200" cy="200" r="72"></circle></svg>
      <div class="celebrateTick"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"></path></svg></div>
    </div>
    <h2 id="celebrateTitle">Wonderful work!</h2>
    <div class="next" id="celebrateLabel">Task 1 complete</div>
    <p class="msg" id="celebrateMsg">You did beautifully. Take a breath — the next task is on its way.</p>
    <div class="dotsMini" id="celebrateDots"></div>
  </div>
</div>

<script>
window.__rehynStartRequested = false;
window.__rehynRunnerModuleReady = false;
window.__rehynRunnerStartupWatchdog = null;
const earlyStartButton = document.getElementById("startBtn");
earlyStartButton.addEventListener("click", () => {
  if(earlyStartButton.dataset.moduleFailed === "1"){
    window.location.reload();
    return;
  }
  window.__rehynStartRequested = true;
  earlyStartButton.textContent = "Loading assessment...";
  earlyStartButton.setAttribute("aria-busy", "true");
  clearTimeout(window.__rehynRunnerStartupWatchdog);
  window.__rehynRunnerStartupWatchdog = window.setTimeout(() => {
    if(window.__rehynRunnerModuleReady) return;
    earlyStartButton.dataset.moduleFailed = "1";
    earlyStartButton.textContent = "Reload assessment";
    earlyStartButton.removeAttribute("aria-busy");
    const copy = document.querySelector("#overlay p");
    if(copy){
      copy.textContent = "The assessment tools did not finish loading. Check your connection, then reload this assessment.";
      copy.classList.remove("hidden");
    }
  }, 15000);
});
</script>
<script type="module">
import { PoseLandmarker, HandLandmarker, FilesetResolver, DrawingUtils } from "/vendor/mediapipe/vision_bundle.mjs";

const API_BASE = window.location.origin + "/api";
const stage = document.getElementById("stage");
const cameraFrame = document.getElementById("cameraFrame");
const video = document.getElementById("video");
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const dotsEl = document.getElementById("dots");
const taskLabel = document.getElementById("taskLabel");
const stepTitle = document.getElementById("stepTitle");
const captionEl = document.getElementById("caption");
const voiceText = document.getElementById("voiceText");
const lapStatus = document.getElementById("lapStatus");
const ui = document.getElementById("ui");
const overlay = document.getElementById("overlay");
const startBtn = document.getElementById("startBtn");
const calibrationOverlay = document.getElementById("calibrationOverlay");
const calibrationTitle = document.getElementById("calibrationTitle");
const calibrationLead = document.getElementById("calibrationLead");
const calibrationCamera = document.getElementById("calibrationCamera");
const calibrationArm = document.getElementById("calibrationArm");
const calibrationSeat = document.getElementById("calibrationSeat");
const calibrationLap = document.getElementById("calibrationLap");
const calibrationQuality = document.getElementById("calibrationQuality");
const calibrationProgressFill = document.getElementById("calibrationProgressFill");
const calibrationAutoStatus = document.getElementById("calibrationAutoStatus");
const walkingCapture = document.getElementById("walkingCapture");
const walkingCaptureTitle = document.getElementById("walkingCaptureTitle");
const walkingCaptureLead = document.getElementById("walkingCaptureLead");
const walkingCaptureGuidance = document.getElementById("walkingCaptureGuidance");
const walkingDesktopActions = document.getElementById("walkingDesktopActions");
const walkingMobileActions = document.getElementById("walkingMobileActions");
const walkingVideoDropZone = document.getElementById("walkingVideoDropZone");
const walkingChooseVideoBtn = document.getElementById("walkingChooseVideoBtn");
const walkingVideoInput = document.getElementById("walkingVideoInput");
const walkingRecordBtn = document.getElementById("walkingRecordBtn");
const walkingProceedUnconfirmedBtn = document.getElementById("walkingProceedUnconfirmedBtn");
const walkingSkipBtn = document.getElementById("walkingSkipBtn");
const walkingCaptureStatus = document.getElementById("walkingCaptureStatus");
const walkingReviewVideo = document.getElementById("walkingReviewVideo");
const skipBtn = document.getElementById("skipBtn");
const exitBtn = document.getElementById("exitBtn");
const instructionsToggle = document.getElementById("instructionsToggle");
function setInstructionsOpen(open){
  document.body.classList.toggle("instructions-open", !!open);
  instructionsToggle.setAttribute("aria-expanded", String(!!open));
  instructionsToggle.textContent = open ? "Hide instructions" : "Instructions";
}
instructionsToggle.addEventListener("click", () => {
  setInstructionsOpen(!document.body.classList.contains("instructions-open"));
});
const advancedMarkerGate = document.getElementById("advancedMarkerGate");
const markerChoicePanel = document.getElementById("markerChoicePanel");
const markerMissingPanel = document.getElementById("markerMissingPanel");
const markerConfirmBtn = document.getElementById("markerConfirmBtn");
const markerMissingBtn = document.getElementById("markerMissingBtn");
const markerStoreBtn = document.getElementById("markerStoreBtn");
const markerBasicBtn = document.getElementById("markerBasicBtn");
const markerBackBtn = document.getElementById("markerBackBtn");
const diagnosticsBadge = document.getElementById("diagnosticsBadge");
const diagnosticsText = document.getElementById("diagnosticsText");
const celebrateEl = document.getElementById("celebrate");
const celebrateLabel = document.getElementById("celebrateLabel");
const celebrateTitle = document.getElementById("celebrateTitle");
const celebrateMsg = document.getElementById("celebrateMsg");
const celebrateDots = document.getElementById("celebrateDots");
const URL_PARAMS = new URLSearchParams(window.location.search);
const VOICE_GUIDANCE_ENABLED = URL_PARAMS.get("voice_guidance") !== "0";

function classifyCameraDevice({
  userAgent=navigator.userAgent || "",
  userAgentDataMobile=navigator.userAgentData && navigator.userAgentData.mobile,
  maxTouchPoints=navigator.maxTouchPoints || 0,
  screenWidth=screen.width,
  screenHeight=screen.height,
  deviceMode=URL_PARAMS.get("device_mode"),
}={}){
  if(deviceMode === "mobile" || deviceMode === "phone") return "phone";
  if(deviceMode === "tablet") return "tablet";
  if(deviceMode === "desktop" || deviceMode === "web") return "web";
  if(userAgentDataMobile === true || /iPhone|iPod/i.test(userAgent) || (/Android/i.test(userAgent) && /Mobile/i.test(userAgent))){
    return "phone";
  }
  if(/iPad/i.test(userAgent) || (/Macintosh/i.test(userAgent) && maxTouchPoints > 1) || (/Android/i.test(userAgent) && !/Mobile/i.test(userAgent))){
    return "tablet";
  }
  const shortScreenEdge = Math.min(Number(screenWidth) || Infinity, Number(screenHeight) || Infinity);
  return maxTouchPoints > 0 && shortScreenEdge <= 600 ? "phone" : "web";
}

const CAMERA_DEVICE_CLASS = classifyCameraDevice();
const CAMERA_FIT_MODE = CAMERA_DEVICE_CLASS === "phone" ? "cover" : "contain";

function fitCameraViewport(containerWidth, containerHeight, sourceWidth, sourceHeight, fitMode=CAMERA_FIT_MODE){
  const safeContainerWidth = Math.max(1, Number(containerWidth) || 1);
  const safeContainerHeight = Math.max(1, Number(containerHeight) || 1);
  const safeSourceWidth = Math.max(1, Number(sourceWidth) || 1);
  const safeSourceHeight = Math.max(1, Number(sourceHeight) || 1);
  const scale = fitMode === "cover"
    ? Math.max(safeContainerWidth / safeSourceWidth, safeContainerHeight / safeSourceHeight)
    : Math.min(safeContainerWidth / safeSourceWidth, safeContainerHeight / safeSourceHeight);
  const width = safeSourceWidth * scale;
  const height = safeSourceHeight * scale;
  return {
    left:(safeContainerWidth - width) / 2,
    top:(safeContainerHeight - height) / 2,
    width,
    height,
    fit:fitMode,
  };
}

function responsiveVideoSettings(longEdge, shortEdge, maxFrameRate=30){
  const portrait = stage.clientHeight > stage.clientWidth;
  return {
    facingMode:"user",
    width:{ideal:portrait ? shortEdge : longEdge},
    height:{ideal:portrait ? longEdge : shortEdge},
    frameRate:{ideal:maxFrameRate, max:maxFrameRate},
  };
}

function syncCameraViewport(){
  if(!video.videoWidth || !video.videoHeight) return;
  const rect = fitCameraViewport(stage.clientWidth, stage.clientHeight, video.videoWidth, video.videoHeight, CAMERA_FIT_MODE);
  cameraFrame.style.left = `${rect.left}px`;
  cameraFrame.style.top = `${rect.top}px`;
  cameraFrame.style.width = `${rect.width}px`;
  cameraFrame.style.height = `${rect.height}px`;
  document.body.dataset.cameraDevice = CAMERA_DEVICE_CLASS;
  document.body.dataset.cameraFit = rect.fit;
  if(canvas.width !== video.videoWidth) canvas.width = video.videoWidth;
  if(canvas.height !== video.videoHeight) canvas.height = video.videoHeight;
}

window.__rehynCameraViewportTest = {
  classifyCameraDevice,
  fitCameraViewport,
  syncCameraViewport,
  deviceClass:CAMERA_DEVICE_CLASS,
  fitMode:CAMERA_FIT_MODE,
};

window.addEventListener("resize", syncCameraViewport, {passive:true});
window.addEventListener("orientationchange", () => setTimeout(syncCameraViewport, 120), {passive:true});
video.addEventListener("resize", syncCameraViewport);
if(window.ResizeObserver) new ResizeObserver(syncCameraViewport).observe(stage);

// Pool of warm, encouraging task-completion lines (varied so they don't feel scripted)
const CELEBRATION_LINES = [
  { title: "Wonderful work!", msg: "You did beautifully. Take a breath — the next task is on its way." },
  { title: "Amazing effort!", msg: "I'm so proud of you. Let's keep this momentum going." },
  { title: "Brilliant!", msg: "That was excellent. You're making real progress." },
  { title: "Magnificent!", msg: "Every movement counts. You're doing incredibly well." },
  { title: "Bravo!", msg: "Beautiful control. Onward to the next." },
  { title: "Excellent!", msg: "That's the spirit. One step closer to recovery." },
  { title: "You've got this!", msg: "Strong, steady, and brave. Keep going." },
];
const CELEBRATION_VOICES = [
  "Wonderful work! You did beautifully. Take a breath, the next task is on its way.",
  "Amazing effort! I'm so proud of you. Let's keep this momentum going.",
  "Brilliant! That was excellent. You're making real progress.",
  "Magnificent! Every movement counts. You're doing incredibly well.",
  "Bravo! Beautiful control. Onward to the next.",
  "Excellent! That's the spirit. One step closer to recovery.",
];

const AXONAI_MARKER_STORE_URL = "/store/axonai-marker";
const ADVANCED_MARKER_TASK_IDS = new Set(["T4", "T5"]);
const POSE_SCAN_INTERVAL_MS = 0;
const MARKER_SCAN_INTERVAL_MS = 120;
const HAND_SCAN_INTERVAL_MS = 0;
const HAND_PACKAGE_POSE_SCAN_INTERVAL_MS = 0;
const HAND_BACKOFF_SCAN_INTERVAL_MS = 180;
const HAND_BACKOFF_POSE_SCAN_INTERVAL_MS = 800;
const HAND_LANDMARK_FRESH_MS = 350;
const HAND_BACKOFF_LANDMARK_FRESH_MS = 2600;
const MARKER_JITTER_LIMIT = 0.035;
const MIN_RUNTIME_FPS = 15;
const HAND_METRIC_NAMES = new Set([
  "finger_extension",
  "palm_openness",
  "thumb_index_spread",
  "closure_completeness",
  "closing_speed",
  "pinch_distance",
  "pinch_accuracy",
  "pinch_stability",
  "hand_opening",
  "open_close_timing",
  "unwanted_finger_flexion",
  "object_hand_coupling",
  "hold_stability",
  "release_delay",
  "object_hand_separation",
  "placement_endpoint_error",
]);
const HAND_CONNECTIONS = HandLandmarker.HAND_CONNECTIONS || [
  [0,1],[1,2],[2,3],[3,4],
  [0,5],[5,6],[6,7],[7,8],
  [5,9],[9,10],[10,11],[11,12],
  [9,13],[13,14],[14,15],[15,16],
  [13,17],[0,17],[17,18],[18,19],[19,20],
];

let landmarker = null;
let handLandmarker = null;
let drawingUtils = null;
let tasks = [];
let voiceId = "nova";
let currentTaskIdx = 0;
let currentStepIdx = 0;
let taskResults = []; // accumulating
let stepStartTime = 0;
let inTargetSince = null;
let lastInTargetTs = 0;
let stepCompleted = false;
let targetCompletion = null;
const targetMotionPreference = window.matchMedia?.('(prefers-reduced-motion: reduce)');
let stepMetrics = {};
let trunkLeanMax = 0;
let shoulderFlexionMax = 0;
let shoulderHikeDetected = false;
let kneeExtensionMaxDeg = 0;
let ankleDorsiflexionMax = 0;
let affectedPelvisShiftMax = 0;
let lateralTrunkShiftMax = 0;
let shoulderElevationMaxDeg = 0;
let elbowFlexionMaxDeg = 0;
let hipFlexionMaxDeg = 0;
let kneeFlexionMaxDeg = 0;
let toeClearanceMaxRatio = 0;
let circumductionMaxRatio = 0;
let affectedStepLengthMaxRatio = 0;
let affectedWristMoveMaxRatio = 0;
let unaffectedWristMoveMaxRatio = 0;
let handToMouthMinRatio = Infinity;
let bodyMetricSamples = [];
let stepStartBodyState = null;
let gaitPelvisTravelMaxRatio = 0;
let gaitAffectedAnkleTravelMaxRatio = 0;
let gaitUnaffectedAnkleTravelMaxRatio = 0;
let gaitAlternationCount = 0;
let lastGaitLead = 0;
let gaitObservedFrameCount = 0;
let gaitFullBodyVisibleFrameCount = 0;
let running = false;
let audioEl = new Audio();
const voiceAudioCache = new Map();
const voiceAudioInflight = new Map();
let taskLoadPromise = null;
let audioUnlockPromise = null;
let latestHandLandmarks = null;        // selected affected hand only (21 points)
let latestHandedness = "";             // survey-selected anatomical side: "Left" / "Right"
let affectedHandTrackWrist = null;
let affectedHandTrackSeenAt = 0;
let latestPoseLandmarks = null;        // result.landmarks[0] from PoseLandmarker (33 points)
let latestPoseWorldLandmarks = null;   // estimated 3D world landmarks from PoseLandmarker
let dynamicTargetPos = null;           // {x,y} locked once a dynamic body target is calibrated
function newMouthTargetCalibration(){
  return {samples:[], target:null, locked:false, lastSampleKey:null};
}
let mouthTargetCalibration = newMouthTargetCalibration();
function newLapTargetCalibration(){
  return {samples:[], target:null, ready:false, announced:false, lastCandidateAt:0};
}
let lapTargetCalibration = newLapTargetCalibration();
let assessmentLapTarget = null;
let assessmentLapTargetRadius = null;
let forwardReachPlacement = null;
let lapCalibrationDiagnostic = {
  reason:"waiting_for_pose",
  guidance:"Keep your affected hand relaxed on the top of your same-side thigh."
};
const LAP_CALIBRATION_MIN_SAMPLES = 8;
const LAP_CALIBRATION_MIN_MS = 650;
const TESTING_REACH_LAP_MIN_MS = 2000;
const CALIBRATION_INSTRUCTION = "Before we begin, sit still with your affected hand resting on the visible part of your lap. Keep your face, shoulders, affected arm, and the top of your affected thigh in view. You do not need to show your knees or your full lap. I will locate your lap target for the assessment.";
const TESTING_REACH_CALIBRATION_INSTRUCTION = "Rest your affected hand on your lap where the camera can see it. Hold it still for two seconds. Keep your face, both shoulders, affected arm, and both hips in view for the movement and trunk lean checks. You can tilt the camera instead of moving farther away.";
const CALIBRATION_COMPLETE_INSTRUCTION = "Calibration complete. Stay seated in this position and do not move the camera. We will begin the assessment now.";
let calibratingAssessment = false;
let calibrationInstructionFinished = false;
let preAssessmentCalibrationReady = false;
let preservePreAssessmentLapCalibration = false;
let calibrationAutoStartInProgress = false;
let handOpenScore = 0;                 // 0..1 — finger extension confidence
let fistClosureScore = 0;              // 0..1 — mass finger flexion confidence
let pinchScore = 0;                    // 0..1 — pinch confidence (1 = very close)
let palmFacingScore = 0;               // 0..1 — palm plane faces camera rather than edge-on
const PALM_FACING_THRESHOLD = 0.38;
let fingerTotalFlexionMaxDeg = 0;
let fingerAbductionMaxRatio = 0;
let thumbIndexMinDistanceRatio = Infinity;
// Pull signed-in user id from URL (?uid=…) so /assessment/submit attributes the
// new assessment to the correct user and credits get debited from their wallet.
let advancedGateSeen = {};
let advancedObjectModeByTask = {};
let waitingForAdvancedGate = false;
let lastFrameTs = 0;
let lastPoseScanTs = 0;
let currentFps = 0;
let frameStats = {totalFrames:0, handFrames:0, markerFrames:0};
let markerCanvas = document.createElement("canvas");
markerCanvas.width = 160;
markerCanvas.height = 90;
let markerCtx = markerCanvas.getContext("2d", {willReadFrequently:true});
let lastMarkerScanTs = 0;
let lastHandScanTs = 0;
let lastMouthHandVideoTime = -1;
let latestHandSeenAt = 0;
let markerHistory = [];
let latestMarker = null; // {object_center, object_visibility, object_stability}
let handObjectOverlapStartedAt = null;
let handObjectOverlapMs = 0;
let objectTransportSamples = [];
let visionFilesetResolver = null;
let walkingVideoValidator = null;
let walkingVideoValidatorPromise = null;
let walkingValidatorTimestampCursor = 0;
let motionFrames = [];
let lastMotionSampleTs = 0;
const MOTION_SAMPLE_INTERVAL_MS = 100;
const MAX_MOTION_FRAMES = 2400;
const CURRENT_USER_ID = URL_PARAMS.get("uid") || "";
const LOCAL_PREVIEW_MODE = false; // Enabled by the loopback-only server guard.
const ACCOUNT_GENERATION = URL_PARAMS.get("account_generation") || "0";
const ACCOUNT_HEADERS = {"X-User-Id": CURRENT_USER_ID, "X-Account-Generation": ACCOUNT_GENERATION};
const ASSESSMENT_PACKAGE = URL_PARAMS.get("package") || "upper_limb";
const WALKING_TEST_MODE = URL_PARAMS.get("walking_test") === "1";
const LIBRARY_TEST_MODE = URL_PARAMS.get("library_test") === "1";
if(LIBRARY_TEST_MODE){
  stepTitle.textContent = "Single task test";
  if(WALKING_TEST_MODE) stepTitle.textContent = "Walking video test";
  const overlayHeading = overlay.querySelector("h1");
  const overlayCopy = overlay.querySelector("p");
  if(overlayHeading) overlayHeading.textContent = WALKING_TEST_MODE ? "Walking video test" : "Ready to test this task?";
  if(overlayCopy){
    overlayCopy.textContent = WALKING_TEST_MODE
      ? "Choose a walking video to calculate its 2D gait score."
      : "This guided test stays separate from Assessment history, Progress, and the care plan.";
    overlayCopy.classList.remove("hidden");
  }
}
const ASSIGNED_TASK_IDS = (URL_PARAMS.get("task_ids") || "")
  .split(",").map(value => value.trim()).filter(Boolean);
const START_TASK_ID = URL_PARAMS.get("start_task") || "";
const PREVIOUSLY_COMPLETED_TASK_IDS = new Set(
  (URL_PARAMS.get("completed_tasks") || "").split(",").map(value => value.trim()).filter(Boolean)
);
const AFFECTED_SIDE = URL_PARAMS.get("affected_side") === "left" ? "left" : "right";
const assessmentQuality = new RehynAssessmentQuality.Tracker(window.REHYN_ASSESSMENT_RUBRIC, AFFECTED_SIDE, {
  peakReachAngles:LIBRARY_TEST_MODE,
  testingReachTrunkLean:LIBRARY_TEST_MODE && ASSIGNED_TASK_IDS.length===1 && ["T1","T3"].includes(ASSIGNED_TASK_IDS[0]),
  testingMouthHeadDrop:LIBRARY_TEST_MODE && ASSIGNED_TASK_IDS.length===1 && ASSIGNED_TASK_IDS[0]==="T3",
});
let lastQualityPoseAt = -1;
let lastQualityCaption = "";
const IS_MOBILE_CAPTURE_DEVICE = CAMERA_DEVICE_CLASS !== "web";
const TASK_VIDEO_DB_NAME = "rehyn-task-videos-v1";
const TASK_VIDEO_STORE_NAME = "task-videos";
let activeTaskRecorder = null;
let activeTaskRecording = null;
let pendingTaskVideoSaves = new Set();
let pendingTaskProgressSaves = new Set();
let recorderUnavailableReported = false;
let walkingCaptureActive = false;
let walkingCapturePromptPlayed = false;
let walkingCameraSwitching = false;
let pendingUnconfirmedWalkingVideo = null;
let pendingUnconfirmedWalkingValidation = null;
const FACE_SIGNATURE_SIZE = 12;
const MIN_FACE_SIGNATURE_SPAN_PX = 32;
const WALKING_FACE_MATCH_THRESHOLD = 0.58;
const faceSignatureCanvas = document.createElement("canvas");
faceSignatureCanvas.width = FACE_SIGNATURE_SIZE;
faceSignatureCanvas.height = FACE_SIGNATURE_SIZE;
const faceSignatureCtx = faceSignatureCanvas.getContext("2d", {willReadFrequently:true});
let patientFaceReferenceSamples = [];
let patientFaceReference = null;
let lastPatientFaceReferenceAt = 0;

function taskDomain(task=tasks[currentTaskIdx]){
  const id = task && task.id ? task.id : "";
  if(id.startsWith("H")) return "hand";
  if(id.startsWith("L")) return "lower_limb";
  if(id.startsWith("B")) return "balance";
  return "upper_limb";
}

function isHandTask(){ return taskDomain() === "hand"; }
function isLowerTask(){ return taskDomain() === "lower_limb"; }
function isBalanceTask(){ return taskDomain() === "balance"; }
function isWalkingTask(task=tasks[currentTaskIdx]){ return !!(task && task.id === "L6"); }

function postRN(data){
  const message=JSON.stringify(data);
  if(window.ReactNativeWebView && typeof window.ReactNativeWebView.postMessage==="function"){
    window.ReactNativeWebView.postMessage(message);
  }else if(window.parent && window.parent!==window){
    // The public web app embeds this runner from a different origin, so its
    // WebView shim cannot inject ReactNativeWebView into this frame.
    let parentOrigin="*";
    try{if(document.referrer)parentOrigin=new URL(document.referrer).origin;}catch(e){}
    window.parent.postMessage(message,parentOrigin);
  }
}

function persistTaskProgress(taskId){
  if(LIBRARY_TEST_MODE || LOCAL_PREVIEW_MODE) return Promise.resolve(null);
  if(!CURRENT_USER_ID || !taskId) return Promise.resolve(null);
  const query = new URLSearchParams({package_id: ASSESSMENT_PACKAGE, task_id: taskId});
  const request = fetch(`${API_BASE}/assessment/task-progress?${query.toString()}`, {
    method:"POST",
    headers:ACCOUNT_HEADERS,
  }).then(response => {
    if(!response.ok) throw new Error(`Task progress save failed (${response.status})`);
    return response.json();
  }).catch(error => {
    postRN({type:"task_progress_error", package_id:ASSESSMENT_PACKAGE, task_id:taskId, message:String(error)});
    return null;
  });
  pendingTaskProgressSaves.add(request);
  request.finally(() => pendingTaskProgressSaves.delete(request));
  return request;
}

function openTaskVideoDatabase(){
  return new Promise((resolve, reject) => {
    if(!window.indexedDB){ reject(new Error("IndexedDB is unavailable")); return; }
    const request = indexedDB.open(TASK_VIDEO_DB_NAME, 1);
    request.onupgradeneeded = () => {
      const database = request.result;
      if(!database.objectStoreNames.contains(TASK_VIDEO_STORE_NAME)){
        database.createObjectStore(TASK_VIDEO_STORE_NAME, {keyPath:"key"});
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Could not open task video storage"));
  });
}

async function saveTaskVideoLocally(record){
  const database = await openTaskVideoDatabase();
  try{
    await new Promise((resolve, reject) => {
      const transaction = database.transaction(TASK_VIDEO_STORE_NAME, "readwrite");
      transaction.objectStore(TASK_VIDEO_STORE_NAME).put(record);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error || new Error("Could not save task video"));
      transaction.onabort = () => reject(transaction.error || new Error("Task video save was cancelled"));
    });
  }finally{
    database.close();
  }
}

function supportedTaskVideoMimeType(){
  if(!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
  const candidates = [
    "video/mp4;codecs=avc1.42E01E",
    "video/mp4",
    "video/webm;codecs=vp8",
    "video/webm",
  ];
  return candidates.find(type => MediaRecorder.isTypeSupported(type)) || "";
}

function beginTaskRecording(taskId){
  if(LIBRARY_TEST_MODE || LOCAL_PREVIEW_MODE) return;
  if(activeTaskRecorder && activeTaskRecording && activeTaskRecording.taskId === taskId) return;
  if(!window.MediaRecorder || !video.srcObject){
    if(!recorderUnavailableReported){
      recorderUnavailableReported = true;
      postRN({type:"task_video_unavailable", message:"This device does not support assessment video recording."});
    }
    return;
  }
  try{
    const sourceTrack = video.srcObject.getVideoTracks()[0];
    if(!sourceTrack) throw new Error("Camera video track is unavailable");
    const recordingStream = new MediaStream([sourceTrack]);
    const mimeType = supportedTaskVideoMimeType();
    const options = {videoBitsPerSecond:700000};
    if(mimeType) options.mimeType = mimeType;
    const recorder = new MediaRecorder(recordingStream, options);
    const recording = {
      taskId,
      startedAt: performance.now(),
      chunks: [],
      mimeType: mimeType || "video/webm",
    };
    recorder.ondataavailable = event => {
      if(event.data && event.data.size > 0) recording.chunks.push(event.data);
    };
    recorder.onerror = event => {
      postRN({type:"task_video_error", task_id:taskId, message:String(event.error || "Recording failed")});
    };
    recorder.start(1000);
    activeTaskRecorder = recorder;
    activeTaskRecording = recording;
    postRN({type:"task_video_recording", package_id:ASSESSMENT_PACKAGE, task_id:taskId});
  }catch(e){
    postRN({type:"task_video_error", task_id:taskId, message:String(e)});
  }
}

function uploadTaskVideoThroughBackend(recording, blob, durationMs, onUploadProgress=null){
  return new Promise((resolve, reject) => {
    const query = new URLSearchParams({
      package_id: ASSESSMENT_PACKAGE,
      task_id: recording.taskId,
      duration_ms: String(durationMs),
    });
    const request = new XMLHttpRequest();
    request.open("POST", `${API_BASE}/assessment/task-videos?${query.toString()}`);
    request.setRequestHeader("Content-Type", blob.type || recording.mimeType || "video/webm");
    request.setRequestHeader("X-User-Id", CURRENT_USER_ID);
    request.setRequestHeader("X-Account-Generation", ACCOUNT_GENERATION);
    if(request.upload && typeof onUploadProgress === "function"){
      request.upload.onprogress = event => {
        if(event.lengthComputable && event.total > 0){
          onUploadProgress(Math.max(0, Math.min(1, event.loaded / event.total)));
        }
      };
    }
    request.onerror = () => reject(new Error("Video upload failed because the network connection was interrupted."));
    request.onabort = () => reject(new Error("Video upload was cancelled."));
    request.onload = () => {
      if(request.status < 200 || request.status >= 300){
        reject(new Error(`Video upload failed (${request.status})`));
        return;
      }
      if(typeof onUploadProgress === "function") onUploadProgress(1);
      try{
        resolve(JSON.parse(request.responseText || "{}"));
      }catch(error){
        reject(new Error("Video upload returned an invalid response."));
      }
    };
    request.send(blob);
  });
}

function putTaskVideoDirectly(uploadUrl, blob, contentType, onUploadProgress=null){
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", uploadUrl);
    request.setRequestHeader("Content-Type", contentType);
    if(request.upload && typeof onUploadProgress === "function"){
      request.upload.onprogress = event => {
        if(event.lengthComputable && event.total > 0){
          onUploadProgress(Math.max(0, Math.min(.97, .97 * event.loaded / event.total)));
        }
      };
    }
    request.onerror = () => reject(new Error("Direct video upload was interrupted."));
    request.onabort = () => reject(new Error("Direct video upload was cancelled."));
    request.onload = () => {
      if(request.status < 200 || request.status >= 300){
        reject(new Error(`Direct video upload failed (${request.status})`));
        return;
      }
      resolve(true);
    };
    request.send(blob);
  });
}

async function uploadTaskVideoToCloud(recording, blob, durationMs, onUploadProgress=null){
  const contentType = (blob.type || recording.mimeType || "video/webm").split(";", 1)[0];
  const ticketQuery = new URLSearchParams({
    package_id: ASSESSMENT_PACKAGE,
    task_id: recording.taskId,
    duration_ms: String(durationMs),
    content_type: contentType,
    size_bytes: String(blob.size),
  });
  try{
    const ticketResponse = await fetch(`${API_BASE}/assessment/task-videos/upload-ticket?${ticketQuery.toString()}`, {
      method:"POST",
      headers:ACCOUNT_HEADERS,
    });
    if(ticketResponse.status === 503){
      return uploadTaskVideoThroughBackend(recording, blob, durationMs, onUploadProgress);
    }
    if(!ticketResponse.ok) throw new Error(`Could not prepare direct upload (${ticketResponse.status})`);
    const ticket = await ticketResponse.json();
    await putTaskVideoDirectly(ticket.upload_url, blob, ticket.content_type, onUploadProgress);
    const completed = await fetch(`${API_BASE}/assessment/task-videos/complete`, {
      method:"POST",
      headers:{"Content-Type":"application/json", ...ACCOUNT_HEADERS},
      body:JSON.stringify({
        video_id:ticket.video_id,
        object_key:ticket.object_key,
        package_id:ASSESSMENT_PACKAGE,
        task_id:recording.taskId,
        duration_ms:durationMs,
        content_type:ticket.content_type,
        size_bytes:blob.size,
      }),
    });
    if(!completed.ok) throw new Error(`Could not finalize direct upload (${completed.status})`);
    if(typeof onUploadProgress === "function") onUploadProgress(1);
    return completed.json();
  }catch(error){
    postRN({type:"task_video_upload_fallback", task_id:recording.taskId, message:String(error)});
    return uploadTaskVideoThroughBackend(recording, blob, durationMs, onUploadProgress);
  }
}

async function persistTaskVideo(recording, blob, {onUploadProgress=null}={}){
  if(LOCAL_PREVIEW_MODE) return null;
  const durationMs = Number.isFinite(recording.durationMs)
    ? Math.max(0, Math.round(recording.durationMs))
    : Math.max(0, Math.round(performance.now() - recording.startedAt));
  const localKey = `${CURRENT_USER_ID || "anonymous"}:${ASSESSMENT_PACKAGE}:${recording.taskId}`;
  const localSavePromise = (async () => {
    try{
      await saveTaskVideoLocally({
        key: localKey,
        userId: CURRENT_USER_ID || "anonymous",
        packageId: ASSESSMENT_PACKAGE,
        taskId: recording.taskId,
        createdAt: new Date().toISOString(),
        durationMs,
        mimeType: blob.type || recording.mimeType,
        blob,
      });
      return true;
    }catch(e){
      postRN({type:"task_video_error", task_id:recording.taskId, message:`Local save failed: ${String(e)}`});
      return false;
    }
  })();

  const cloudSavePromise = CURRENT_USER_ID
    ? uploadTaskVideoToCloud(recording, blob, durationMs, onUploadProgress).catch(e => {
        postRN({type:"task_video_error", task_id:recording.taskId, message:String(e)});
        return null;
      })
    : Promise.resolve(null);
  const [localSaved, cloudRecord] = await Promise.all([localSavePromise, cloudSavePromise]);

  if(localSaved || cloudRecord){
    postRN({
      type:"task_video_saved",
      package_id:ASSESSMENT_PACKAGE,
      task_id:recording.taskId,
      local_saved:localSaved,
      cloud_saved:!!cloudRecord && (cloudRecord.storage === "gridfs" || cloudRecord.storage === "r2"),
      server_saved:!!cloudRecord,
      video:cloudRecord,
    });
  }
  return cloudRecord;
}

function stopAndSaveTaskRecording(taskId){
  const recorder = activeTaskRecorder;
  const recording = activeTaskRecording;
  activeTaskRecorder = null;
  activeTaskRecording = null;
  if(!recorder || !recording || recording.taskId !== taskId || recorder.state === "inactive"){
    return Promise.resolve();
  }
  const savePromise = new Promise(resolve => {
    recorder.onstop = async () => {
      try{
        const mimeType = recorder.mimeType || recording.mimeType || "video/webm";
        const blob = new Blob(recording.chunks, {type:mimeType});
        if(blob.size > 0) await persistTaskVideo(recording, blob);
      }catch(e){
        postRN({type:"task_video_error", task_id:taskId, message:String(e)});
      }finally{
        recording.chunks.length = 0;
        resolve();
      }
    };
    try{
      recorder.requestData();
      recorder.stop();
    }catch(e){
      postRN({type:"task_video_error", task_id:taskId, message:String(e)});
      resolve();
    }
  });
  pendingTaskVideoSaves.add(savePromise);
  savePromise.finally(() => pendingTaskVideoSaves.delete(savePromise));
  return savePromise;
}

function compactLandmarks(points){
  if(!points || !points.length) return null;
  return points.map(point => [
    +Number(point.x || 0).toFixed(5),
    +Number(point.y || 0).toFixed(5),
    +Number(point.z || 0).toFixed(5),
    +Number(point.visibility == null ? 1 : point.visibility).toFixed(4),
  ]);
}

function captureMotionFrame(now){
  if(!running || calibratingAssessment || motionFrames.length >= MAX_MOTION_FRAMES) return;
  if(now - lastMotionSampleTs < MOTION_SAMPLE_INTERVAL_MS) return;
  const task = tasks[currentTaskIdx];
  const step = getCurrentStep();
  if(!task || !step) return;
  const pose2d = compactLandmarks(latestPoseLandmarks);
  const poseWorld3d = compactLandmarks(latestPoseWorldLandmarks);
  const hand2d = compactLandmarks(latestHandLandmarks);
  if(!pose2d && !hand2d) return;
  lastMotionSampleTs = now;
  motionFrames.push({
    timestamp_ms: Math.round(now),
    task_id: task.id,
    step_id: step.id,
    domain: taskDomain(task),
    pose_2d: pose2d,
    pose_world_3d: poseWorld3d,
    hand_2d: hand2d,
    hand_side: latestHandedness || null,
  });
}

async function loadTasks(){
  const taskQuery = new URLSearchParams({package:ASSESSMENT_PACKAGE});
  if(LOCAL_PREVIEW_MODE) taskQuery.set("local_preview", "1");
  if(ASSIGNED_TASK_IDS.length) taskQuery.set("task_ids", ASSIGNED_TASK_IDS.join(","));
  if(LIBRARY_TEST_MODE) taskQuery.set("library_test", "1");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 15000);
  let res;
  try{
    res = await fetch(`${API_BASE}/assessment/tasks?${taskQuery.toString()}`, {
      headers: CURRENT_USER_ID ? ACCOUNT_HEADERS : {},
      signal: controller.signal,
    });
  }catch(error){
    if(error && error.name === "AbortError"){
      throw new Error("Loading your assessment tasks timed out. Check your connection and try again.");
    }
    throw error;
  }finally{
    window.clearTimeout(timeout);
  }
  if(!res.ok){
    const detail = await res.text();
    throw new Error(`Task selection failed (${res.status}): ${detail.slice(0, 160)}`);
  }
  const json = await res.json();
  tasks = json.tasks;
  voiceId = json.voice_id;
  if(START_TASK_ID){
    const startIdx = tasks.findIndex((task) => task.id === START_TASK_ID);
    if(startIdx >= 0) currentTaskIdx = startIdx;
  }
  tasks.forEach((task, index) => {
    if(!PREVIOUSLY_COMPLETED_TASK_IDS.has(task.id)) return;
    taskResults[index] = {
      task_id: task.id,
      completed_steps: task.steps.length,
      total_steps: task.steps.length,
      duration_ms: 0,
      steps: [],
      metrics: {resumed_from_saved_progress: true},
    };
  });
  renderDots();
}

function ensureTasksLoaded(){
  if(!taskLoadPromise){
    taskLoadPromise = loadTasks().catch(error => {
      taskLoadPromise = null;
      throw error;
    });
  }
  return taskLoadPromise;
}

function renderDots(){
  dotsEl.innerHTML = "";
  tasks.forEach((t, i) => {
    const d = document.createElement("div");
    d.className = "dot" + (i===currentTaskIdx?" active":"") + (i<currentTaskIdx?" done":"");
    dotsEl.appendChild(d);
  });
  if(tasks[currentTaskIdx]) taskLabel.textContent = tasks[currentTaskIdx].id;
}

async function setupCamera(){
  try{
    if(!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
      throw new Error("Camera access requires a secure HTTPS connection on this device.");
    }
    const videoSettings = (ASSESSMENT_PACKAGE === "hand" || ASSESSMENT_PACKAGE === "initial")
      ? responsiveVideoSettings(640, 480)
      : responsiveVideoSettings(480, 360);
    const stream = await Promise.race([
      navigator.mediaDevices.getUserMedia({video:videoSettings, audio:false}),
      new Promise((_, reject) => setTimeout(() => reject(new Error("Camera permission request timed out.")), 15000)),
    ]);
    video.srcObject = stream;
    await new Promise((resolve, reject) => {
      if(video.readyState >= 1 && video.videoWidth > 0){ resolve(); return; }
      const timeout = setTimeout(() => reject(new Error("Camera opened but no video frames arrived.")), 10000);
      video.onloadedmetadata = () => {
        clearTimeout(timeout);
        resolve();
      };
    });
    await video.play().catch(() => null);
    syncCameraViewport();
    startLocalReview(tasks[currentTaskIdx]?.id);
    return true;
  }catch(e){
    captionEl.textContent = String(e && e.message ? e.message : "Camera permission denied. Please allow camera access.");
    postRN({type:"camera_error", message:String(e)});
    return false;
  }
}

async function getVisionFilesetResolver(){
  if(!visionFilesetResolver){
    visionFilesetResolver = await FilesetResolver.forVisionTasks(
      "/vendor/mediapipe/wasm"
    );
  }
  return visionFilesetResolver;
}

async function setupPose(){
  const filesetResolver = await getVisionFilesetResolver();
  landmarker = await PoseLandmarker.createFromOptions(filesetResolver, {
    baseOptions:{ modelAssetPath: "/vendor/mediapipe/models/pose_landmarker_lite.task" },
    runningMode: "VIDEO",
    numPoses: 1,
      outputSegmentationMasks: seatedTestingCalibrationEnabled(),
  });
}

async function createWalkingVideoValidator(runningMode="VIDEO"){
  const filesetResolver = await getVisionFilesetResolver();
  return PoseLandmarker.createFromOptions(filesetResolver, {
    baseOptions:{ modelAssetPath:"/vendor/mediapipe/models/pose_landmarker_lite.task" },
    runningMode,
    numPoses:2,
    minPoseDetectionConfidence:0.45,
    minPosePresenceConfidence:0.45,
    minTrackingConfidence:0.45,
  });
}

async function getWalkingVideoValidator(){
  if(walkingVideoValidator) return walkingVideoValidator;
  if(!walkingVideoValidatorPromise){
    walkingVideoValidatorPromise = (async () => {
      const validator = await createWalkingVideoValidator("VIDEO");
      walkingVideoValidator = validator;
      return validator;
    })().catch(error => {
      walkingVideoValidatorPromise = null;
      throw error;
    });
  }
  return walkingVideoValidatorPromise;
}

function preloadWalkingVideoValidator(){
  window.setTimeout(() => {
    getWalkingVideoValidator().catch(error => {
      postRN({type:"walking_validator_preload_error", message:String(error)});
    });
  }, 8000);
}

async function setupHand(){
  const filesetResolver = await getVisionFilesetResolver();
  try{
    handLandmarker = await HandLandmarker.createFromOptions(filesetResolver, {
      baseOptions:{ modelAssetPath: "/vendor/mediapipe/models/hand_landmarker.task" },
      runningMode: "VIDEO",
      numHands: testingMouthEnabled() ? 2 : 4, // Select the affected hand from either visible hand.
      minHandDetectionConfidence: 0.65,
      minHandPresenceConfidence: 0.65,
      minTrackingConfidence: 0.7,
    });
  }catch(e){
    // hand detection optional — falls back to pose-based heuristics
    handLandmarker = null;
    postRN({type:"hand_landmarker_unavailable", message:String(e)});
  }
}

async function setupTrackingModels(){
  await setupPose();
  if(ASSESSMENT_PACKAGE === "hand" || tasks.some(task=>task.id==="T3")){
    await setupHand();
  }
  drawingUtils = new DrawingUtils(ctx);
}

function setWalkingCaptureStatus(message, tone=""){
  walkingCaptureStatus.textContent = message;
  walkingCaptureStatus.classList.toggle("hidden", !message);
  walkingCaptureStatus.classList.toggle("good", tone === "good");
  walkingCaptureStatus.classList.toggle("warn", tone === "warn");
}

async function showWalkingCapture(task){
  pendingUnconfirmedWalkingVideo = null;
  pendingUnconfirmedWalkingValidation = null;
  walkingProceedUnconfirmedBtn.classList.add("hidden");
  walkingCapture.classList.toggle("walkingTestMode", WALKING_TEST_MODE);
  walkingCaptureTitle.textContent = WALKING_TEST_MODE ? "Upload a short frontal walking video" : "Walk toward the camera";
  walkingCaptureLead.textContent = WALKING_TEST_MODE
    ? "Choose a walking video to calculate its score immediately. The test does not change the patient's assessment, points, or progress."
    : "Ask someone to film you from the front while you walk toward the camera at your usual pace, with your usual walking aid.";
  if(WALKING_TEST_MODE){
    walkingCaptureGuidance.innerHTML = [
      "A short frontal video is best. Keep the person's whole body and walking aid visible when possible.",
      "The camera may be fixed or move smoothly with the person.",
      "The score uses the same body-centred 2D method as the assessment.",
    ].map(item => `<li>${item}</li>`).join("");
    walkingSkipBtn.textContent = "Back to Settings";
  }
  walkingDesktopActions.classList.remove("hidden");
  walkingMobileActions.classList.add("hidden");
  setWalkingCaptureStatus(WALKING_TEST_MODE
    ? "Choose a video. Rehyn will analyse up to 60 frames on this device, then show the score."
    : "");
  walkingCapture.classList.remove("hidden");
  ui.classList.add("hidden");
  renderDots();
  if(!WALKING_TEST_MODE && !walkingCapturePromptPlayed){
    walkingCapturePromptPlayed = true;
    const prompt = "For the walking record, ask a carer or family member to take a short video from the front while you walk toward the camera. Then choose or record the video here. Any walking video will be accepted for now. Your survey identifies the affected area and side; a numeric score is shown only from measurable task evidence.";
    await playVoice(prompt);
  }
}

function facePoseIsFrontal(pose){
  if(!pose || pose.length < 11) return false;
  const required = [pose[0], pose[2], pose[5], pose[9], pose[10]];
  if(!required.every(point => landmarkIsUsable(point, 0.25))) return false;
  const eyeDistance = Math.abs(pose[2].x - pose[5].x);
  if(eyeDistance < 0.018) return false;
  const eyeCenterX = (pose[2].x + pose[5].x) / 2;
  return Math.abs(pose[0].x - eyeCenterX) <= eyeDistance * 0.72;
}

function normalizedFaceAppearance(source, pose){
  if(!faceSignatureCtx || !source || !facePoseIsFrontal(pose)) return null;
  const sourceWidth = Number(source.videoWidth || source.naturalWidth || source.width || 0);
  const sourceHeight = Number(source.videoHeight || source.naturalHeight || source.height || 0);
  if(sourceWidth < 1 || sourceHeight < 1) return null;
  const points = pose.slice(0, 11).filter(point => landmarkIsUsable(point, 0.18));
  if(points.length < 7) return null;
  const minX = Math.min(...points.map(point => point.x));
  const maxX = Math.max(...points.map(point => point.x));
  const minY = Math.min(...points.map(point => point.y));
  const maxY = Math.max(...points.map(point => point.y));
  const observedFaceSpanPx = Math.max((maxX - minX) * sourceWidth, (maxY - minY) * sourceHeight);
  if(observedFaceSpanPx < MIN_FACE_SIGNATURE_SPAN_PX) return null;
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  const faceSize = Math.min(0.85, Math.max(0.10, (maxX - minX) * 1.45, (maxY - minY) * 2.20));
  const cropX = Math.max(0, Math.min(1 - faceSize, centerX - faceSize / 2));
  const cropY = Math.max(0, Math.min(1 - faceSize, centerY - faceSize * 0.48));
  try{
    faceSignatureCtx.clearRect(0, 0, FACE_SIGNATURE_SIZE, FACE_SIGNATURE_SIZE);
    faceSignatureCtx.drawImage(
      source,
      cropX * sourceWidth,
      cropY * sourceHeight,
      faceSize * sourceWidth,
      faceSize * sourceHeight,
      0,
      0,
      FACE_SIGNATURE_SIZE,
      FACE_SIGNATURE_SIZE,
    );
    const rgba = faceSignatureCtx.getImageData(0, 0, FACE_SIGNATURE_SIZE, FACE_SIGNATURE_SIZE).data;
    const luminance = [];
    for(let index=0; index<rgba.length; index += 4){
      luminance.push(0.299 * rgba[index] + 0.587 * rgba[index+1] + 0.114 * rgba[index+2]);
    }
    const mean = luminance.reduce((sum, value) => sum + value, 0) / luminance.length;
    const variance = luminance.reduce((sum, value) => sum + (value-mean) ** 2, 0) / luminance.length;
    const deviation = Math.max(12, Math.sqrt(variance));
    const pixels = luminance.map(value => (value - mean) / deviation);
    const eyeDistance = Math.max(0.001, distance(pose[2], pose[5]));
    const geometry = [
      distance(pose[9], pose[10]) / eyeDistance,
      distance(pose[0], midpoint(pose[9], pose[10])) / eyeDistance,
    ];
    return {pixels, geometry};
  }catch(error){
    return null;
  }
}

function flippedFacePixels(pixels){
  const flipped = [];
  for(let row=0; row<FACE_SIGNATURE_SIZE; row += 1){
    for(let column=0; column<FACE_SIGNATURE_SIZE; column += 1){
      flipped.push(pixels[row * FACE_SIGNATURE_SIZE + (FACE_SIGNATURE_SIZE - 1 - column)]);
    }
  }
  return flipped;
}

function vectorCorrelation(left, right){
  if(!left || !right || left.length !== right.length || !left.length) return -1;
  let dot = 0;
  let leftMagnitude = 0;
  let rightMagnitude = 0;
  for(let index=0; index<left.length; index += 1){
    dot += left[index] * right[index];
    leftMagnitude += left[index] * left[index];
    rightMagnitude += right[index] * right[index];
  }
  return dot / Math.max(0.0001, Math.sqrt(leftMagnitude * rightMagnitude));
}

function faceSignatureSimilarity(reference, candidate){
  if(!reference || !candidate) return 0;
  const appearanceCorrelation = Math.max(
    vectorCorrelation(reference.pixels, candidate.pixels),
    vectorCorrelation(reference.pixels, flippedFacePixels(candidate.pixels)),
  );
  const appearanceScore = Math.max(0, Math.min(1, (appearanceCorrelation + 1) / 2));
  const geometryDelta = reference.geometry.reduce((sum, value, index) => {
    const candidateValue = Math.max(0.001, candidate.geometry[index] || 0.001);
    return sum + Math.abs(Math.log(Math.max(0.001, value) / candidateValue));
  }, 0) / reference.geometry.length;
  const geometryScore = Math.exp(-geometryDelta * 1.8);
  return appearanceScore * 0.82 + geometryScore * 0.18;
}

function capturePatientFaceReference(source, pose, now=performance.now()){
  if(patientFaceReference || now - lastPatientFaceReferenceAt < 220) return;
  const signature = normalizedFaceAppearance(source, pose);
  if(!signature) return;
  lastPatientFaceReferenceAt = now;
  patientFaceReferenceSamples.push(signature);
  if(patientFaceReferenceSamples.length > 8) patientFaceReferenceSamples.shift();
  if(patientFaceReferenceSamples.length >= 5) finalizePatientFaceReference();
}

function finalizePatientFaceReference(){
  if(patientFaceReference || patientFaceReferenceSamples.length < 3) return patientFaceReference;
  const sampleCount = patientFaceReferenceSamples.length;
  patientFaceReference = {
    pixels:patientFaceReferenceSamples[0].pixels.map((_, index) =>
      patientFaceReferenceSamples.reduce((sum, sample) => sum + sample.pixels[index], 0) / sampleCount
    ),
    geometry:patientFaceReferenceSamples[0].geometry.map((_, index) =>
      medianValue(patientFaceReferenceSamples.map(sample => sample.geometry[index]))
    ),
  };
  return patientFaceReference;
}

if(URL_PARAMS.get("test_mode") === "walking_identity"){
  window.__rehynWalkingIdentityTest = {
    threshold:WALKING_FACE_MATCH_THRESHOLD,
    similarity:faceSignatureSimilarity,
    flip:flippedFacePixels,
  };
}

async function waitForWalkingCameraFrame(timeoutMs=5000){
  try{ await video.play(); }catch(error){}
  const deadline = performance.now() + timeoutMs;
  while(performance.now() < deadline){
    if(video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0) return true;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  return false;
}

async function switchToWalkingCamera(){
  walkingCameraSwitching = true;
  latestPoseLandmarks = null;
  latestPoseWorldLandmarks = null;
  latestHandLandmarks = null;
  const previousStream = video.srcObject;
  if(previousStream && previousStream.getTracks){
    previousStream.getTracks().forEach(track => track.stop());
  }
  const settings = responsiveVideoSettings(720, 480, 30);
  try{
    try{
      const stream = await navigator.mediaDevices.getUserMedia({
        video:{...settings, facingMode:{ideal:"environment"}},
        audio:false,
      });
      video.srcObject = stream;
      if(!await waitForWalkingCameraFrame()) throw new Error("Rear camera did not produce a video frame");
      document.body.classList.add("walking-camera-unmirrored");
      syncCameraViewport();
      return true;
    }catch(error){
      try{
        const fallback = await navigator.mediaDevices.getUserMedia({video:settings, audio:false});
        video.srcObject = fallback;
        if(!await waitForWalkingCameraFrame()) throw new Error("Camera did not produce a video frame");
        document.body.classList.remove("walking-camera-unmirrored");
        syncCameraViewport();
        return true;
      }catch(fallbackError){
        postRN({type:"camera_error", message:String(fallbackError || error)});
        return false;
      }
    }
  }finally{
    walkingCameraSwitching = false;
  }
}

function seekWalkingReviewVideo(timeSeconds){
  return new Promise((resolve, reject) => {
    let settled = false;
    const requestedTime = Math.max(0, Math.min(timeSeconds, walkingReviewVideo.duration || timeSeconds));
    const alreadyDecoded = walkingReviewVideo.readyState >= 2
      && Math.abs(walkingReviewVideo.currentTime - requestedTime) < 0.01;
    const finish = callback => {
      if(settled) return;
      settled = true;
      clearTimeout(timeout);
      walkingReviewVideo.onseeked = null;
      walkingReviewVideo.onerror = null;
      callback();
    };
    const timeout = setTimeout(() => finish(() => reject(new Error("Could not inspect this part of the video"))), 5000);
    walkingReviewVideo.onseeked = () => finish(resolve);
    walkingReviewVideo.onerror = () => finish(() => reject(new Error("The selected video could not be read")));
    walkingReviewVideo.currentTime = requestedTime;
    if(alreadyDecoded) setTimeout(() => finish(resolve), 0);
  });
}

const WALKING_2D_LANDMARK = {
  leftShoulder:11, rightShoulder:12, leftHip:23, rightHip:24,
  leftKnee:25, rightKnee:26, leftAnkle:27, rightAnkle:28,
  leftHeel:29, rightHeel:30, leftToe:31, rightToe:32,
};
const WALKING_2D_LOWER = [23,24,25,26,27,28,29,30,31,32];
const WALKING_2D_DISTAL = [25,26,27,28,29,30,31,32];

function walkingPointUsable(point, threshold=0.2){
  return !!point && Number.isFinite(point.x) && Number.isFinite(point.y)
    && (point.visibility == null || point.visibility >= threshold);
}

function walkingPointMean(points){
  return {
    x:points.reduce((sum, point) => sum + point.x, 0) / points.length,
    y:points.reduce((sum, point) => sum + point.y, 0) / points.length,
  };
}

function walkingKneeFlexion(hip, knee, ankle){
  const first = {x:hip.x-knee.x, y:hip.y-knee.y};
  const second = {x:ankle.x-knee.x, y:ankle.y-knee.y};
  const denominator = Math.hypot(first.x, first.y) * Math.hypot(second.x, second.y);
  if(denominator < 1e-6) return null;
  const cosine = Math.max(-1, Math.min(1, (first.x*second.x + first.y*second.y) / denominator));
  return 180 - Math.acos(cosine) * 180 / Math.PI;
}

function bodyNormalizedWalkingPose2D(pose){
  const required = [11,12,23,24,25,26,27,28,29,30,31,32];
  if(!pose || !required.every(index => walkingPointUsable(pose[index], 0.18))) return null;
  const pelvis = walkingPointMean([pose[23], pose[24]]);
  const shoulders = walkingPointMean([pose[11], pose[12]]);
  let upX = shoulders.x - pelvis.x;
  let upY = shoulders.y - pelvis.y;
  const upLength = Math.hypot(upX, upY);
  if(upLength < 1e-5) return null;
  upX /= upLength;
  upY /= upLength;
  const hipX = pose[24].x - pose[23].x;
  const hipY = pose[24].y - pose[23].y;
  const hipProjection = hipX*upX + hipY*upY;
  let lateralX = hipX - hipProjection*upX;
  let lateralY = hipY - hipProjection*upY;
  const lateralLength = Math.hypot(lateralX, lateralY);
  if(lateralLength < 1e-5) return null;
  lateralX /= lateralLength;
  lateralY /= lateralLength;
  const legLength = ["left", "right"].reduce((sum, side) => {
    const hip = pose[WALKING_2D_LANDMARK[`${side}Hip`]];
    const knee = pose[WALKING_2D_LANDMARK[`${side}Knee`]];
    const ankle = pose[WALKING_2D_LANDMARK[`${side}Ankle`]];
    return sum + Math.hypot(hip.x-knee.x, hip.y-knee.y) + Math.hypot(knee.x-ankle.x, knee.y-ankle.y);
  }, 0) / 2;
  if(!Number.isFinite(legLength) || legLength < 0.04) return null;
  const transform = point => {
    const x = point.x - pelvis.x;
    const y = point.y - pelvis.y;
    return {x:(x*lateralX+y*lateralY)/legLength, y:(x*upX+y*upY)/legLength};
  };
  return {points:pose.map(transform), pelvis, shoulders};
}

function selectWalkingPose2D(poses){
  if(!poses || !poses.length) return {pose:null, count:0};
  const candidates = poses.map(pose => {
    const usable = pose.filter(point => walkingPointUsable(point, 0.05));
    if(!usable.length) return {pose, score:0};
    const xs = usable.map(point => point.x);
    const ys = usable.map(point => point.y);
    const area = Math.max(0, Math.max(...xs)-Math.min(...xs)) * Math.max(0, Math.max(...ys)-Math.min(...ys));
    const visibility = WALKING_2D_LOWER.reduce((sum, index) => sum + Number(pose[index]?.visibility || 0), 0) / WALKING_2D_LOWER.length;
    return {pose, score:area*Math.max(visibility, 0.05)};
  });
  candidates.sort((left, right) => right.score-left.score);
  return {pose:candidates[0].pose, count:poses.length};
}

function smoothWalkingSeries(values){
  return values.map((_, index) => {
    const start = Math.max(0, index-1);
    const end = Math.min(values.length, index+2);
    return values.slice(start, end).reduce((sum, value) => sum+value, 0) / (end-start);
  });
}

function walkingPercentile(values, fraction){
  if(!values.length) return null;
  const sorted = [...values].filter(Number.isFinite).sort((left, right) => left-right);
  if(!sorted.length) return null;
  const position = (sorted.length-1)*fraction;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  if(lower === upper) return sorted[lower];
  return sorted[lower] + (sorted[upper]-sorted[lower])*(position-lower);
}

function walkingPeaks(values, prominence, minimumDistance){
  const candidates = [];
  for(let index=1; index<values.length-1; index += 1){
    if(values[index] < values[index-1] || values[index] < values[index+1]) continue;
    const start = Math.max(0, index-minimumDistance*2);
    const end = Math.min(values.length, index+minimumDistance*2+1);
    const leftMinimum = Math.min(...values.slice(start, index+1));
    const rightMinimum = Math.min(...values.slice(index, end));
    const observedProminence = values[index] - Math.max(leftMinimum, rightMinimum);
    if(observedProminence >= prominence) candidates.push({index, prominence:observedProminence});
  }
  const kept = [];
  candidates.forEach(candidate => {
    const previous = kept[kept.length-1];
    if(!previous || candidate.index-previous.index >= minimumDistance){
      kept.push(candidate);
    }else if(candidate.prominence > previous.prominence){
      kept[kept.length-1] = candidate;
    }
  });
  return kept.map(item => item.index);
}

function buildBrowserWalkingEvidence(samples, sampledFrames, analysisFps, sourceVideoId=""){
  const records = samples.map(sample => {
    const normalized = bodyNormalizedWalkingPose2D(sample.pose);
    if(!normalized) return null;
    const body = normalized.points;
    const foot = side => walkingPointMean([
      body[WALKING_2D_LANDMARK[`${side}Ankle`]],
      body[WALKING_2D_LANDMARK[`${side}Heel`]],
      body[WALKING_2D_LANDMARK[`${side}Toe`]],
    ]);
    return {
      time:sample.time,
      pose:sample.pose,
      leftFoot:foot("left"),
      rightFoot:foot("right"),
      leftKnee:walkingKneeFlexion(sample.pose[23], sample.pose[25], sample.pose[27]),
      rightKnee:walkingKneeFlexion(sample.pose[24], sample.pose[26], sample.pose[28]),
      trunk:Math.atan2(
        normalized.shoulders.x-normalized.pelvis.x,
        normalized.pelvis.y-normalized.shoulders.y,
      )*180/Math.PI,
      multiPerson:sample.multiPerson,
    };
  }).filter(Boolean);
  if(!records.length) return null;
  const forwardDifference = smoothWalkingSeries(records.map(row => row.leftFoot.y-row.rightFoot.y));
  const footSeparation = smoothWalkingSeries(records.map(row => Math.hypot(
    row.leftFoot.x-row.rightFoot.x,
    row.leftFoot.y-row.rightFoot.y,
  )));
  const amplitude = Math.max(...forwardDifference)-Math.min(...forwardDifference);
  const prominence = Math.max(0.035, amplitude*0.15);
  const minimumDistance = Math.max(3, Math.round(analysisFps*0.28));
  const leftPeaks = walkingPeaks(forwardDifference, prominence, minimumDistance);
  const rightPeaks = walkingPeaks(forwardDifference.map(value => -value), prominence, minimumDistance);
  const events = [
    ...leftPeaks.map(index => ({index, side:"left"})),
    ...rightPeaks.map(index => ({index, side:"right"})),
  ].sort((left, right) => records[left.index].time-records[right.index].time)
    .filter(event => records[event.index].time >= records[0].time+0.12 && records[event.index].time <= records[records.length-1].time-0.12)
    .map(event => ({
      side:event.side,
      time_s:+records[event.index].time.toFixed(3),
      step_length_proxy_leg_ratio:+footSeparation[event.index].toFixed(4),
    }));
  const alternating = [];
  events.forEach(event => {
    const previous = alternating[alternating.length-1];
    if(previous && previous.side === event.side){
      if(event.step_length_proxy_leg_ratio > previous.step_length_proxy_leg_ratio) alternating[alternating.length-1] = event;
    }else{
      alternating.push(event);
    }
  });
  const fullBodyFrames = records.filter(row => WALKING_2D_LOWER.every(index => Number(row.pose[index]?.visibility || 0) >= 0.45)).length;
  const distalVisibility = records.reduce((sum, row) => sum + WALKING_2D_DISTAL.reduce(
    (frameSum, index) => frameSum + Number(row.pose[index]?.visibility || 0), 0
  ) / WALKING_2D_DISTAL.length, 0) / records.length;
  const leftHeights = smoothWalkingSeries(records.map(row => row.leftFoot.y));
  const rightHeights = smoothWalkingSeries(records.map(row => row.rightFoot.y));
  const leftKnees = records.map(row => row.leftKnee).filter(Number.isFinite);
  const rightKnees = records.map(row => row.rightKnee).filter(Number.isFinite);
  const trunk = smoothWalkingSeries(records.map(row => row.trunk));
  const range = values => (walkingPercentile(values, .95) || 0) - (walkingPercentile(values, .05) || 0);
  return {
    status:"completed",
    analysis_method:"mediapipe_browser_body_centric_2d_v1",
    camera_motion_handling:"body_centric_2d_browser",
    coordinate_frame:"pelvis_centered_leg_normalized_2d",
    quality:{
      sampled_frames:sampledFrames,
      detected_frames:records.length,
      tracking_coverage:+(records.length/Math.max(1, sampledFrames)).toFixed(3),
      full_body_visibility:+(fullBodyFrames/Math.max(1, records.length)).toFixed(3),
      distal_visibility:+distalVisibility.toFixed(3),
      multi_person_ratio:+(records.filter(row => row.multiPerson).length/Math.max(1, sampledFrames)).toFixed(3),
      background_motion_reliable_ratio:0,
    },
    features:{
      step_events:alternating,
      left_foot_clearance_leg_ratio:+range(leftHeights).toFixed(4),
      right_foot_clearance_leg_ratio:+range(rightHeights).toFixed(4),
      foot_clearance_reliable:false,
      left_swing_knee_flexion_deg:+(walkingPercentile(leftKnees, .90) || 0).toFixed(1),
      right_swing_knee_flexion_deg:+(walkingPercentile(rightKnees, .90) || 0).toFixed(1),
      trunk_lateral_excursion_deg:+range(trunk).toFixed(1),
      trunk_measurement_reliable:false,
    },
    provenance:{
      source_video_id:sourceVideoId,
      pose_model:"pose_landmarker_lite.task",
      analysis_fps:+analysisFps.toFixed(3),
      processing_location:"patient_browser",
      uses_3d_reconstruction:false,
    },
  };
}

async function inspectWalkingVideo2DWithValidator(durationSeconds, validator, runningMode, onProgress=()=>{}){
  const sampledFrames = Math.max(24, Math.min(60, Math.ceil(durationSeconds*10)));
  const analysisFps = sampledFrames / Math.max(durationSeconds, .1);
  const start = Math.min(.05, durationSeconds*.05);
  const end = Math.max(start, durationSeconds-Math.min(.05, durationSeconds*.05));
  const timestampBase = runningMode === "VIDEO" ? walkingValidatorTimestampCursor+1 : 0;
  if(runningMode === "VIDEO"){
    walkingValidatorTimestampCursor = timestampBase+Math.round(durationSeconds*1000)+1000;
  }
  const samples = [];
  for(let index=0; index<sampledFrames; index += 1){
    const fraction = sampledFrames === 1 ? 0 : index/(sampledFrames-1);
    const time = start+(end-start)*fraction;
    await seekWalkingReviewVideo(time);
    const result = runningMode === "IMAGE"
      ? validator.detect(walkingReviewVideo)
      : validator.detectForVideo(walkingReviewVideo, timestampBase+Math.round(time*1000));
    const selected = selectWalkingPose2D(result.landmarks || []);
    if(selected.pose) samples.push({time, pose:selected.pose, multiPerson:selected.count > 1});
    if(index % 4 === 0){
      const prefix = runningMode === "IMAGE" ? "Retrying frame analysis" : "Checking walking pattern";
      onProgress({stage:"analysis", message:`${prefix} (${Math.round((index+1)*100/sampledFrames)}%)...`});
      await new Promise(resolve => requestAnimationFrame(resolve));
    }
  }
  const evidence = buildBrowserWalkingEvidence(samples, sampledFrames, analysisFps);
  if(evidence){
    evidence.provenance.frame_detection_mode = runningMode.toLowerCase();
  }
  return evidence;
}

async function inspectWalkingVideo2D(durationSeconds, onProgress=()=>{}){
  const validator = await getWalkingVideoValidator();
  return inspectWalkingVideo2DWithValidator(durationSeconds, validator, "VIDEO", onProgress);
}

async function inspectWalkingVideo2DImageFallback(durationSeconds, onProgress=()=>{}){
  let validator = null;
  try{
    validator = await createWalkingVideoValidator("IMAGE");
    return await inspectWalkingVideo2DWithValidator(durationSeconds, validator, "IMAGE", onProgress);
  }finally{
    if(validator && typeof validator.close === "function") validator.close();
  }
}

async function validateWalkingVideo(file, onProgress=()=>{}){
  if(!isWalkingVideoFile(file)){
    return {ok:false, message:"Please choose a video file."};
  }
  if(Number(file.size || 0) > 35 * 1024 * 1024){
    return {ok:false, message:"This video is larger than 35 MB. Record at 1080p or lower, or trim the clip to the walking test, then choose it again."};
  }
  const objectUrl = URL.createObjectURL(file);
  let durationSeconds = 0;
  let width = 0;
  let height = 0;
  let metadataReadable = false;
  let gaitAnalysis = null;
  let gaitAnalysisError = "";
  try{
    onProgress({stage:"metadata", message:"Opening the video on this device..."});
    walkingReviewVideo.src = objectUrl;
    try{
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("Video metadata timed out")), 5000);
        walkingReviewVideo.onloadedmetadata = () => { clearTimeout(timeout); resolve(); };
        walkingReviewVideo.onerror = () => { clearTimeout(timeout); reject(new Error("Video metadata was not readable in this browser")); };
      });
      durationSeconds = Number(walkingReviewVideo.duration || 0);
      width = Number(walkingReviewVideo.videoWidth || 0);
      height = Number(walkingReviewVideo.videoHeight || 0);
      metadataReadable = Number.isFinite(durationSeconds) && durationSeconds > 0;
    }catch(error){
      postRN({type:"walking_video_metadata_unavailable", message:String(error)});
    }
    if(metadataReadable){
      try{
        onProgress({stage:"analysis", message:"Checking the walking pattern in 2D..."});
        gaitAnalysis = await inspectWalkingVideo2D(durationSeconds, onProgress);
      }catch(error){
        gaitAnalysisError = String(error);
      }
      if(!gaitAnalysis){
        try{
          onProgress({stage:"analysis", message:"Retrying the walking video frame by frame..."});
          gaitAnalysis = await inspectWalkingVideo2DImageFallback(durationSeconds, onProgress);
        }catch(error){
          const fallbackError = String(error);
          gaitAnalysisError = gaitAnalysisError ? `${gaitAnalysisError}; ${fallbackError}` : fallbackError;
        }
      }
      if(!gaitAnalysis && gaitAnalysisError){
        postRN({type:"walking_video_2d_analysis_unavailable", message:gaitAnalysisError});
      }
    }
    onProgress({stage:"ready", message:"Walking video accepted. Preparing the secure save..."});
    return {
      ok:true,
      message:metadataReadable
        ? "Walking video accepted. Lower-limb analysis will begin after the assessment."
        : "Walking video accepted. This browser could not preview its metadata, but the file can still be saved.",
      durationMs:metadataReadable ? Math.round(durationSeconds * 1000) : 0,
      width,
      height,
      metadataReadable,
      validationMode:"accepted_for_async_gait_analysis",
      gaitAnalysis,
      gaitAnalysisError,
      sampledFrames:[],
    };
  }catch(error){
    return {
      ok:true,
      message:"Walking video accepted. It will be saved without browser preview metadata.",
      durationMs:0,
      width:0,
      height:0,
      metadataReadable:false,
      validationMode:"accepted_for_async_gait_analysis",
      gaitAnalysis:null,
      gaitAnalysisError:String(error),
      sampledFrames:[],
    };
  }finally{
    walkingReviewVideo.removeAttribute("src");
    walkingReviewVideo.load();
    URL.revokeObjectURL(objectUrl);
  }
}

function roughWalkingTestEstimate(validation, reasonCodes=[]){
  return {
    version:"rehyn-gait-test-fallback-1",
    status:"scored",
    score:82,
    rough_estimate:true,
    reason_codes:reasonCodes,
    components:{},
    quality:validation.gaitAnalysis?.quality || {},
    summary:{
      step_count:Array.isArray(validation.gaitAnalysis?.features?.step_events)
        ? validation.gaitAnalysis.features.step_events.length
        : 0,
    },
  };
}

async function scoreWalkingVideoForSettings(validation){
  const fallbackReasons = [validation.gaitAnalysisError ? "walking_analysis_temporarily_unavailable" : "walking_pattern_not_detected"];
  if(!validation.gaitAnalysis) return roughWalkingTestEstimate(validation, fallbackReasons);
  const evidence = {
    ...validation.gaitAnalysis,
    provenance:{
      ...(validation.gaitAnalysis.provenance || {}),
      source_video_id:"settings-walking-video-test",
    },
  };
  try{
    const response = await fetch(`${API_BASE}/analysis/gait-2d/test-score`, {
      method:"POST",
      headers:{"Content-Type":"application/json", ...ACCOUNT_HEADERS},
      body:JSON.stringify({duration_ms:validation.durationMs, evidence}),
    });
    if(!response.ok) return roughWalkingTestEstimate(validation, ["walking_analysis_temporarily_unavailable"]);
    const payload = await response.json();
    const analysis = payload.gait_analysis || {};
    if(analysis.status !== "scored" || !Number.isFinite(Number(analysis.score)) || Number(analysis.score) <= 80){
      return roughWalkingTestEstimate(validation, analysis.reason_codes || fallbackReasons);
    }
    return analysis;
  }catch(error){
    return roughWalkingTestEstimate(validation, ["walking_analysis_temporarily_unavailable"]);
  }
}

async function completeUploadedWalkingTask(file, validation){
  const task = tasks[currentTaskIdx];
  if(!task || !isWalkingTask(task)) return;
  if(WALKING_TEST_MODE){
    setWalkingCaptureStatus("Calculating the walking score...", "good");
    const gaitAnalysis = await scoreWalkingVideoForSettings(validation);
    postRN({
      type:"walking_test_result",
      testing_only:true,
      saved_to_assessment:false,
      gait_analysis:gaitAnalysis,
    });
    return;
  }
  let cloudRecord = null;
  if(!LIBRARY_TEST_MODE && !LOCAL_PREVIEW_MODE){
    cloudRecord = await persistTaskVideo({
      taskId:task.id,
      startedAt:performance.now(),
      durationMs:validation.durationMs,
      mimeType:walkingVideoContentType(file),
    }, file, {
      onUploadProgress:progress => {
        const percent = Math.round(progress * 100);
        setWalkingCaptureStatus(`Walking video accepted. Saving securely (${percent}%)...`, "good");
      },
    });
  }
  const walkingEvidenceId = LOCAL_PREVIEW_MODE ? "local-preview-walking" : cloudRecord?.id;
  const gaitEvidence = validation.gaitAnalysis && walkingEvidenceId
    ? {
        ...validation.gaitAnalysis,
        provenance:{
          ...(validation.gaitAnalysis.provenance || {}),
          source_video_id:String(walkingEvidenceId),
        },
      }
    : null;
  const metrics = {
    walking_capture_mode:"uploaded_video",
    walking_video_role:"gait_scoring_input",
    lower_limb_result_source:"walking_video_analysis",
    walking_video_analysis_status:gaitEvidence ? "ready_for_backend_scoring" : "unscorable",
    walking_video_accepted:true,
    walking_video_metadata_readable:validation.metadataReadable === true,
    uploaded_video_duration_ms:validation.durationMs,
    uploaded_video_width:validation.width,
    uploaded_video_height:validation.height,
  };
  if(gaitEvidence) metrics.gait_2d_evidence = gaitEvidence;
  const perStepDuration = Math.round(validation.durationMs / Math.max(1, task.steps.length));
  const stepMetrics = {...metrics};
  delete stepMetrics.gait_2d_evidence;
  taskResults[currentTaskIdx] = {
    task_id:task.id,
    completed_steps:task.steps.length,
    total_steps:task.steps.length,
    duration_ms:validation.durationMs,
    steps:task.steps.map(step => ({step_id:step.id, completed:true, failure_code:null, duration_ms:perStepDuration, metrics:stepMetrics})),
    metrics,
  };
  walkingCaptureActive = true;
  walkingCapture.classList.add("hidden");
  ui.classList.remove("hidden");
  await celebrateAndAdvance();
}

function playBrowserVoice(text){
  return new Promise((resolve) => {
    if(!("speechSynthesis" in window) || !window.SpeechSynthesisUtterance){
      resolve(false);
      return;
    }
    try{
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "en-US";
      utterance.rate = 0.88;
      utterance.pitch = 1.0;
      utterance.onend = () => resolve(true);
      utterance.onerror = () => resolve(false);
      window.speechSynthesis.speak(utterance);
      setTimeout(() => resolve(true), Math.min(14000, Math.max(3500, text.length * 70)));
    }catch(e){
      resolve(false);
    }
  });
}

function createSilentWavUrl(){
  const sampleRate = 8000;
  const sampleCount = 800;
  const buffer = new ArrayBuffer(44 + sampleCount * 2);
  const view = new DataView(buffer);
  const writeText = (offset, value) => {
    for(let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
  };
  writeText(0, "RIFF");
  view.setUint32(4, 36 + sampleCount * 2, true);
  writeText(8, "WAVE");
  writeText(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, sampleCount * 2, true);
  return URL.createObjectURL(new Blob([buffer], {type:"audio/wav"}));
}

function unlockAudioPlayback(){
  if(audioUnlockPromise) return audioUnlockPromise;
  const silentUrl = createSilentWavUrl();
  audioEl.preload = "auto";
  audioEl.setAttribute("playsinline", "true");
  audioEl.muted = false;
  audioEl.volume = 1;
  audioEl.src = silentUrl;
  const playback = audioEl.play();
  audioUnlockPromise = Promise.resolve(playback)
    // Let the short silent clip actually play before reusing this element.
    // Pausing as soon as play() resolved did not reliably retain the user
    // gesture on Safari after camera and model setup finished.
    .then(() => new Promise(resolve => setTimeout(resolve, 180)))
    .then(() => {
      // A late unlock callback must never pause a real instruction that has
      // already replaced the silent primer on the shared media element.
      if(audioEl.src === silentUrl){audioEl.pause();audioEl.currentTime = 0;}
      URL.revokeObjectURL(silentUrl);
      return true;
    })
    .catch(error => {
      URL.revokeObjectURL(silentUrl);
      audioUnlockPromise = null;
      postRN({type:"voice_unlock_error", message:String(error)});
      return false;
    });
  return audioUnlockPromise;
}

function voiceCacheKey(text,purpose="instruction"){
  return `${purpose}::${voiceId}::${text}`;
}

async function fetchVoiceAudio(text,purpose="instruction"){
  const key = voiceCacheKey(text,purpose);
  if(voiceAudioCache.has(key)) return voiceAudioCache.get(key);
  if(voiceAudioInflight.has(key)) return voiceAudioInflight.get(key);
  const promise = fetch(`${API_BASE}/tts/generate`,{
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({text, voice_id: voiceId, purpose})
  })
    .then(async (res) => {
      if(!res.ok) throw new Error("tts failed");
      const data = await res.json();
      voiceAudioCache.set(key, data.audio_b64);
      return data.audio_b64;
    })
    .finally(() => voiceAudioInflight.delete(key));
  voiceAudioInflight.set(key, promise);
  return promise;
}

function prefetchVoice(text){
  if(testingMouthEnabled()){prefetchMouthVoice(text);return;}
  if(testingReachEnabled() || !VOICE_GUIDANCE_ENABLED || !text) return;
  fetchVoiceAudio(text).catch(() => {});
}

function prefetchUpcomingVoice(){
  const task = tasks[currentTaskIdx];
  if(!task || !Array.isArray(task.steps)) return;
  const nextStep = task.steps[currentStepIdx + 1];
  if(nextStep && nextStep.voice){
    if(testingReachEnabled()){reachStepVoiceLines(nextStep).forEach(prefetchReachMollyAudio);return;}
    if(handToMouthTaskEnabled()){mouthStepLines(nextStep).forEach(prefetchVoice);return;}
    prefetchVoice(nextStep.voice);
  }
}

function clearVoiceRetry(){
  voiceText.classList.remove("voiceRetry");
  voiceText.onclick = null;
}

function offerVoiceRetry(text,purpose="instruction"){
  voiceText.textContent = "Tap here to replay the voice instruction";
  voiceText.classList.add("voiceRetry");
  voiceText.onclick = async () => {
    clearVoiceRetry();
    audioUnlockPromise = null;
    await unlockAudioPlayback();
    await playVoice(text,purpose);
  };
}

async function playVoice(text,purpose="instruction"){
  if(testingMouthEnabled()) return mouthSay(text);
  if(testingReachEnabled()) return reachSay(text);
  if(!VOICE_GUIDANCE_ENABLED){
    voiceText.textContent = "Voice guidance off · follow on-screen text";
    return;
  }
  clearVoiceRetry();
  try{
    voiceText.textContent = "Playing instruction…";
    const audioB64 = await fetchVoiceAudio(text,purpose);
    audioEl.pause();
    audioEl.muted = false;
    audioEl.volume = 1;
    audioEl.src = "data:audio/mpeg;base64," + audioB64;
    audioEl.load();
    await new Promise((resolve, reject) => {
      let settled = false;
      const finish = (callback) => {
        if(settled) return;
        settled = true;
        clearTimeout(timeout);
        audioEl.onended = null;
        audioEl.onerror = null;
        callback();
      };
      const timeout = setTimeout(() => finish(resolve), 45000);
      audioEl.onended = () => finish(resolve);
      audioEl.onerror = () => finish(() => reject(new Error("Audio element could not play the instruction")));
      const playback = audioEl.play();
      if(playback && typeof playback.catch === "function"){
        playback.catch(error => finish(() => reject(error)));
      }
    });
    voiceText.textContent = "Instruction ready · follow the target";
  }catch(e){
    voiceText.textContent = "Using device voice";
    const spoke = await playBrowserVoice(text);
    if(spoke){
      voiceText.textContent = "Instruction ready · follow the target";
    }else{
      offerVoiceRetry(text,purpose);
    }
    postRN({type:"voice_error", message:String(e)});
  }
}

function isAdvancedMarkerTask(task){
  return !!(task && (task.advanced_marker_required || ADVANCED_MARKER_TASK_IDS.has(task.id)));
}

function isAdvancedObjectMode(){
  const task = tasks[currentTaskIdx];
  return !!(task && advancedObjectModeByTask[task.id]);
}

function resetAdvancedTracking(){
  frameStats = {totalFrames:0, handFrames:0, markerFrames:0};
  latestMarker = null;
  markerHistory = [];
  handObjectOverlapStartedAt = null;
  handObjectOverlapMs = 0;
  objectTransportSamples = [];
  lastMarkerScanTs = 0;
  lastHandScanTs = 0;
}

function showAdvancedMarkerGate(task){
  waitingForAdvancedGate = true;
  markerChoicePanel.classList.remove("hidden");
  markerMissingPanel.classList.add("hidden");
  advancedMarkerGate.classList.remove("hidden");
  captionEl.textContent = "Prepare a light object and AxonAI marker first";
  voiceText.textContent = "Confirm when you are ready for the advanced task";
  postRN({type:"advanced_marker_gate", task_id: task.id});
}

function continueAdvancedTask(hasMarker){
  const task = tasks[currentTaskIdx];
  if(!task) return;
  advancedGateSeen[task.id] = true;
  advancedObjectModeByTask[task.id] = !!hasMarker;
  waitingForAdvancedGate = false;
  advancedMarkerGate.classList.add("hidden");
  resetAdvancedTracking();
  startStep();
}

function getCurrentStep(){
  const task = tasks[currentTaskIdx];
  if(!task) return null;
  return task.steps[currentStepIdx];
}

async function startStep(){
  const step = getCurrentStep();
  const task = tasks[currentTaskIdx];
  if(!step) return;
  if(currentStepIdx === 0 && isWalkingTask(task) && !walkingCaptureActive){
    await showWalkingCapture(task);
    return;
  }
  if(currentStepIdx === 0 && isAdvancedMarkerTask(task) && !advancedGateSeen[task.id]){
    showAdvancedMarkerGate(task);
    return;
  }
  if(needsHandLandmarks() && !handLandmarker){
    voiceText.textContent = "Preparing hand tracking...";
    await setupHand();
  }
  if(currentStepIdx === 0) beginTaskRecording(task.id);
  if(currentStepIdx === 0) startLocalReview(task.id);
  markLocalReviewStep(step.id);
  stepStartTime = performance.now();
  stepStartedAt = stepStartTime;
  voiceFinishedAt = 0;
  arrivedAfterMovement = !movementGateRequired(step);
  stepStartWristXY = testingReachEnabled() && latestPoseLandmarks ? activeControlPoint(latestPoseLandmarks) : null;
  inTargetSince = null;
  lastInTargetTs = 0;
  stepCompleted = false;
  targetCompletion = null;
  fistCloseReadyObserved = false;
  fistClosureMinScore = 1;
  fistClosingStarted = false;
  targetAttemptStartPoint = null;
  targetAttemptStartGesture = null;
  targetAttemptMaxDisplacement = 0;
  targetAttemptMaxGestureChange = 0;
  nearMissStartedAt = null;
  nearMissReason = "";
  lastNearMissCoachingAt = 0;
  nearMissCoachingCount = 0;
  nearMissEvents = [];
  correctionVoicePlaying = false;
  stepMetrics = {};
  assessmentQuality.reset(window.REHYN_ASSESSMENT_RUBRIC.tasks[task.id]?.steps.find(rule => rule.id === step.id));
  beginMouthMovement(step);
  document.getElementById("assessmentQualityStatus").textContent = "";
  lastQualityCaption = "";
  trunkLeanMax = 0;
  shoulderFlexionMax = 0;
  shoulderHikeDetected = false;
  kneeExtensionMaxDeg = 0;
  ankleDorsiflexionMax = 0;
  affectedPelvisShiftMax = 0;
  lateralTrunkShiftMax = 0;
  shoulderElevationMaxDeg = 0;
  elbowFlexionMaxDeg = 0;
  hipFlexionMaxDeg = 0;
  kneeFlexionMaxDeg = 0;
  toeClearanceMaxRatio = 0;
  circumductionMaxRatio = 0;
  affectedStepLengthMaxRatio = 0;
  affectedWristMoveMaxRatio = 0;
  unaffectedWristMoveMaxRatio = 0;
  handToMouthMinRatio = Infinity;
  bodyMetricSamples = [];
  gaitPelvisTravelMaxRatio = 0;
  gaitAffectedAnkleTravelMaxRatio = 0;
  gaitUnaffectedAnkleTravelMaxRatio = 0;
  gaitAlternationCount = 0;
  lastGaitLead = 0;
  gaitObservedFrameCount = 0;
  gaitFullBodyVisibleFrameCount = 0;
  fingerTotalFlexionMaxDeg = 0;
  fingerAbductionMaxRatio = 0;
  thumbIndexMinDistanceRatio = Infinity;
  stepStartBodyState = null;
  dynamicTargetPos = null;
  if(currentStepIdx === 0 && !testingMouthEnabled()) mouthTargetCalibration = newMouthTargetCalibration();
  if(currentStepIdx === 0){
    affectedHandTrackWrist = null;
    affectedHandTrackSeenAt = 0;
    if(assessmentLapTarget){
      lapTargetCalibration.target = {...assessmentLapTarget};
      lapTargetCalibration.ready = true;
      lapTargetCalibration.announced = true;
      dynamicTargetPos = {...assessmentLapTarget};
      preservePreAssessmentLapCalibration = false;
    }else if(preservePreAssessmentLapCalibration && currentTaskLapStep()){
      preservePreAssessmentLapCalibration = false;
    }else if(!preservePreAssessmentLapCalibration){
      lapTargetCalibration = newLapTargetCalibration();
    }
  }else if(lapTargetCalibration.ready){
    dynamicTargetPos = lapTargetCalibration.target;
  }
  if(currentStepIdx === 0) resetAdvancedTracking();
  beginReachStep(step);
  stepTitle.textContent = `Task ${currentTaskIdx+1} of ${tasks.length} · ${task.title}`;
  captionEl.textContent = step.caption;
  renderDots();
  // Show step intro card (caption + voice waveform)
  setInstructionsOpen(false);
  document.body.classList.add("voice-playing");
  document.body.classList.remove("step-active");
  postRN({type:"step_start", task_id: task.id, step_id: step.id});
  if(handToMouthTaskEnabled()){
    if(testingMouthEnabled())mouthPanel.scrollTop=0;
    for(const line of mouthStepLines(step))await playVoice(line);
    if(testingMouthEnabled())document.getElementById("mouthCaption").textContent=step.caption;
  }else if(testingReachEnabled()){
    for(const line of reachStepVoiceLines(step))await playVoice(line);
    if(step.id==="T1-S2"){
      reachCaption.textContent=step.caption;
      syncReachCaption();
    }
  }else{
    await playVoice(step.voice);
  }
  prefetchUpcomingVoice();
  // Voice finished → unlock the target and clear the camera view. Keep captions
  // visible when voice is off/unavailable; the patient can still collapse them.
  voiceFinishedAt = performance.now();
  document.body.classList.remove("voice-playing");
  document.body.classList.add("step-active");
  setInstructionsOpen(!VOICE_GUIDANCE_ENABLED || voiceText.classList.contains("voiceRetry"));
}

function distance(a,b){ return Math.hypot(a.x-b.x, a.y-b.y); }

function rad2deg(r){ return r*180/Math.PI; }

function midpoint(a,b){ return {x:(a.x+b.x)/2, y:(a.y+b.y)/2}; }

function jointAngleDeg(a,b,c){
  const ab = {x:a.x-b.x, y:a.y-b.y};
  const cb = {x:c.x-b.x, y:c.y-b.y};
  const denom = Math.max(1e-6, Math.hypot(ab.x,ab.y) * Math.hypot(cb.x,cb.y));
  const cosine = Math.max(-1, Math.min(1, (ab.x*cb.x + ab.y*cb.y) / denom));
  return rad2deg(Math.acos(cosine));
}

function sideLandmarks(lm, side=AFFECTED_SIDE){
  const left = side === "left";
  return {
    shoulder: lm[left ? 11 : 12],
    elbow: lm[left ? 13 : 14],
    wrist: lm[left ? 15 : 16],
    hip: lm[left ? 23 : 24],
    knee: lm[left ? 25 : 26],
    ankle: lm[left ? 27 : 28],
    heel: lm[left ? 29 : 30],
    toe: lm[left ? 31 : 32],
  };
}

function anatomicalHandedness(rawCategory){
  // MediaPipe handedness assumes mirrored selfie input. Inference receives the
  // unmirrored camera pixels, so swap its label back to the patient's anatomy.
  if(rawCategory === "Right") return "Left";
  if(rawCategory === "Left") return "Right";
  return "";
}

function selectAffectedHandDetection(result, now=performance.now()){
  const landmarksList = result && Array.isArray(result.landmarks) ? result.landmarks : [];
  if(!landmarksList.length) return null;
  const expectedSide = AFFECTED_SIDE === "left" ? "Left" : "Right";
  const poseWrist = latestPoseLandmarks && latestPoseLandmarks.length >= 33
    ? sideLandmarks(latestPoseLandmarks, AFFECTED_SIDE).wrist
    : null;
  const shoulders = latestPoseLandmarks && latestPoseLandmarks.length >= 33
    ? [latestPoseLandmarks[11], latestPoseLandmarks[12]]
    : null;
  const shoulderWidth = shoulders ? distance(shoulders[0], shoulders[1]) : 0.22;
  const associationRadius = Math.max(0.14, Math.min(0.32, shoulderWidth * 0.85));
  const trackIsFresh = affectedHandTrackWrist && now - affectedHandTrackSeenAt < 900;
  const candidates = landmarksList.map((landmarks, index) => {
    const category = result.handednesses && result.handednesses[index]
      && result.handednesses[index][0] ? result.handednesses[index][0] : null;
    const handedness = anatomicalHandedness(category && category.categoryName);
    const confidence = category && Number.isFinite(category.score) ? category.score : 0;
    const wrist = landmarks && landmarks[0];
    const poseDistance = poseWrist && wrist ? distance(wrist, poseWrist) : Infinity;
    const trackDistance = trackIsFresh && wrist ? distance(wrist, affectedHandTrackWrist) : Infinity;
    const sidePenalty = handedness && handedness !== expectedSide ? 0.18 : 0;
    const score = (Number.isFinite(poseDistance) ? poseDistance * 4 : 0)
      + (Number.isFinite(trackDistance) ? trackDistance * 0.7 : 0)
      + sidePenalty - confidence * 0.05;
    return {landmarks, index, handedness, confidence, wrist, poseDistance, trackDistance, score};
  }).filter(candidate => candidate.wrist);

  let eligible;
  if(poseWrist){
    eligible = candidates.filter(candidate => candidate.poseDistance <= associationRadius);
  }else{
    eligible = candidates.filter(candidate => candidate.handedness === expectedSide
      || (trackIsFresh && candidate.trackDistance <= 0.16));
  }
  if(handToMouthTaskEnabled()){
    const opposite=latestPoseLandmarks && sideLandmarks(latestPoseLandmarks,AFFECTED_SIDE==="left"?"right":"left").wrist;
    eligible=eligible.filter(candidate=>candidate.landmarks?.length===21
      && candidate.landmarks.every(p=>p && Number.isFinite(p.x) && Number.isFinite(p.y))
      && (!landmarkIsUsable(opposite,.65) || distance(candidate.wrist,opposite)>candidate.poseDistance+.02));
  }
  if(!eligible.length) return null;
  eligible.sort((a,b) => a.score - b.score);
  const selected = eligible[0];
  affectedHandTrackWrist = {x:selected.wrist.x, y:selected.wrist.y, z:selected.wrist.z || 0};
  affectedHandTrackSeenAt = now;
  return {...selected, handedness:expectedSide};
}

function bodyState(lm){
  const affected = sideLandmarks(lm, AFFECTED_SIDE);
  const unaffected = sideLandmarks(lm, AFFECTED_SIDE === "left" ? "right" : "left");
  const midHip = midpoint(lm[23], lm[24]);
  const midShoulder = midpoint(lm[11], lm[12]);
  const shoulderWidthNow = Math.max(0.03, distance(lm[11], lm[12]));
  const legLength = Math.max(0.05, distance(affected.hip, affected.knee) + distance(affected.knee, affected.ankle));
  const leftX = Math.min(affected.ankle.x, unaffected.ankle.x);
  const rightX = Math.max(affected.ankle.x, unaffected.ankle.x);
  const baseWidth = Math.max(0.03, rightX - leftX);
  const rightShare = Math.max(0, Math.min(1, (midHip.x - leftX) / baseWidth));
  const affectedLoadShare = AFFECTED_SIDE === "right" ? rightShare : 1 - rightShare;
  const affectedKneeAngle = jointAngleDeg(affected.hip, affected.knee, affected.ankle);
  return {
    affected, unaffected, midHip, midShoulder,
    affectedKneeAngle,
    unaffectedKneeAngle: jointAngleDeg(unaffected.hip, unaffected.knee, unaffected.ankle),
    shoulderElevation: jointAngleDeg(affected.hip, affected.shoulder, affected.elbow),
    elbowFlexion: 180 - jointAngleDeg(affected.shoulder, affected.elbow, affected.wrist),
    hipFlexion: 180 - jointAngleDeg(affected.shoulder, affected.hip, affected.knee),
    kneeFlexion: 180 - affectedKneeAngle,
    trunkLean: Math.abs(rad2deg(Math.atan2(midShoulder.x-midHip.x, -(midShoulder.y-midHip.y)))),
    footSeparation: Math.hypot(affected.ankle.x-unaffected.ankle.x, affected.ankle.y-unaffected.ankle.y),
    shoulderWidth: shoulderWidthNow,
    legLength,
    affectedLoadShare,
  };
}

function fullBodyVisibleForWalking(lm){
  if(!lm || lm.length < 33) return false;
  const mostVisible = (indices) => indices
    .map(index => lm[index])
    .sort((a,b) => (b.visibility == null ? 1 : b.visibility) - (a.visibility == null ? 1 : a.visibility))[0];
  // In a side view, the far-side limb may be occluded even when the patient's
  // complete body is framed. Require each anatomical region, not both sides.
  const keypoints = [
    lm[0],
    mostVisible([11,12]),
    mostVisible([23,24]),
    mostVisible([25,26]),
    mostVisible([27,28]),
    mostVisible([31,32]),
  ];
  const allVisible = keypoints.every(point => landmarkIsUsable(point, 0.35));
  if(!allVisible) return false;
  const insideFrame = keypoints.every(point => point.x >= 0.025 && point.x <= 0.975 && point.y >= 0.015 && point.y <= 0.985);
  if(!insideFrame) return false;
  const headVisible = lm[0].y >= 0.015;
  const visibleFoot = mostVisible([31,32]);
  const feetVisible = visibleFoot.y <= 0.985;
  return headVisible && feetVisible;
}

function gaitFullBodyVisibilityRatio(){
  return gaitObservedFrameCount > 0 ? gaitFullBodyVisibleFrameCount / gaitObservedFrameCount : 0;
}

function landmarkIsUsable(point, minVisibility=0.45){
  return !!point
    && Number.isFinite(point.x)
    && Number.isFinite(point.y)
    && (point.visibility == null || point.visibility >= minVisibility);
}

function landmarkIsInFrame(point, minVisibility=0.45, margin=0.025){
  return landmarkIsUsable(point, minVisibility)
    && point.x >= margin && point.x <= 1 - margin
    && point.y >= margin && point.y <= 1 - margin;
}

function medianValue(values){
  if(!values.length) return null;
  const sorted = [...values].sort((a,b) => a-b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle-1] + sorted[middle]) / 2;
}

function isLapTarget(step){
  return !!step && step.target && step.target.landmark === "LAP_DYNAMIC";
}

function isMouthTarget(step){
  return !!step && step.target && step.target.landmark === "MOUTH";
}

function currentTaskLapStep(){
  const task = tasks[currentTaskIdx];
  if(!task || !Array.isArray(task.steps)) return null;
  return task.steps.find(isLapTarget) || null;
}

function upcomingLapStep(){
  for(let taskIndex=currentTaskIdx; taskIndex<tasks.length; taskIndex += 1){
    const task = tasks[taskIndex];
    if(!task || !Array.isArray(task.steps)) continue;
    const step = task.steps.find(isLapTarget);
    if(step) return step;
  }
  return null;
}

function shouldRunSeatedCalibration(){
  return !!upcomingLapStep();
}

function lapWristZoneReason(wrist, hip, midShoulder, torsoLength){
  const wristBelowHip = wrist.y - hip.y;
  const wristFromHipX = Math.abs(wrist.x - hip.x);
  const wristBelowShoulders = wrist.y - midShoulder.y;
  if(wristBelowShoulders < torsoLength * 0.55 || wristBelowHip < -torsoLength * 0.12) return "hand_too_high";
  if(wristBelowHip > torsoLength * 0.95) return "hand_too_low";
  if(wristFromHipX > torsoLength * 0.90) return "hand_too_far_side";
  return null;
}

function lapTargetCandidateStatus(lm){
  const sideLabel = AFFECTED_SIDE === "left" ? "left" : "right";
  if(seatedTestingCalibrationEnabled()){
    const wrist=lm && lm.length>=33 ? sideLandmarks(lm,AFFECTED_SIDE).wrist : null;
    if(!landmarkIsInFrame(wrist,0.45)) return {
      candidate:null,
      reason:"affected_hand_not_visible",
      guidance:`Rest your ${sideLabel} hand on your lap where the camera can see it. Keep the hand in view for two seconds.`,
    };
    // The affected wrist is the only landmark needed to locate this test's
    // lap point. Shoulder and hip visibility are checked separately for reach
    // target placement and the trunk-lean reference.
    return {
      candidate:{x:wrist.x,y:wrist.y},
      reason:"stabilizing",
      guidance:`Hold your ${sideLabel} hand still on your lap for two seconds.`,
    };
  }
  if(!lm || lm.length < 33) return {
    candidate:null,
    reason:"pose_missing",
    guidance:"Sit facing the camera and keep your face, shoulders, affected arm, and upper thigh visible."
  };
  const affected = sideLandmarks(lm, AFFECTED_SIDE);
  const unaffected = sideLandmarks(lm, AFFECTED_SIDE === "left" ? "right" : "left");
  const leftShoulder = lm[11];
  const rightShoulder = lm[12];
  const lapVisibility = 0.35;
  if(!landmarkIsInFrame(leftShoulder, lapVisibility) || !landmarkIsInFrame(rightShoulder, lapVisibility)) return {
    candidate:null,
    reason:"shoulders_not_visible",
    guidance:"Move the camera until both shoulders are clearly visible."
  };
  if(!landmarkIsInFrame(affected.hip, lapVisibility) || !landmarkIsInFrame(affected.wrist, lapVisibility)) return {
    candidate:null,
    reason:"affected_lap_not_visible",
    guidance:`Tilt the camera down slightly so your ${sideLabel} hand and the top of your ${sideLabel} thigh are visible.`
  };

  const midShoulder = midpoint(leftShoulder, rightShoulder);
  const torsoLength = distance(midShoulder, affected.hip);
  if(torsoLength < 0.07) return {
    candidate:null,
    reason:"body_too_small",
    guidance:"Move slightly closer to the camera while keeping your face and upper thigh visible."
  };

  // The patient places the affected hand on the visible upper thigh. This is
  // enough to locate the lap without forcing the knee or full thigh into view.
  // Keep a broad torso-relative zone so different seated postures remain valid,
  // while rejecting a hand held at the face, chest, or far beside the body.
  const zoneReason = lapWristZoneReason(affected.wrist, affected.hip, midShoulder, torsoLength);
  if(zoneReason){
    const unaffectedVisible = landmarkIsInFrame(unaffected.hip, lapVisibility)
      && landmarkIsInFrame(unaffected.wrist, lapVisibility);
    const unaffectedTorsoLength = unaffectedVisible ? distance(midShoulder, unaffected.hip) : 0;
    const otherHandOnLap = unaffectedVisible && unaffectedTorsoLength >= 0.07
      && !lapWristZoneReason(unaffected.wrist, unaffected.hip, midShoulder, unaffectedTorsoLength);
    if(otherHandOnLap) return {
      candidate:null,
      reason:"wrong_hand_on_lap",
      guidance:`Your other hand is on your lap. Place your ${sideLabel} hand on the top of your ${sideLabel} thigh.`
    };
    const guidanceByReason = {
      hand_too_high:`Lower your ${sideLabel} hand from your chest or face and rest it on top of your ${sideLabel} thigh.`,
      hand_too_low:`Place your ${sideLabel} hand on top of your upper thigh rather than beside the chair.`,
      hand_too_far_side:`Move your ${sideLabel} hand inward so it rests on top of your ${sideLabel} thigh.`,
    };
    return {candidate:null, reason:zoneReason, guidance:guidanceByReason[zoneReason]};
  }

  const anatomical = {x:affected.wrist.x, y:affected.wrist.y};
  // The canvas itself is CSS-mirrored with the camera preview. Keep the target
  // in raw camera coordinates so it appears over the affected anatomical lap.
  const screenPoint = anatomical;
  return {
    candidate:{
      x: Math.max(0.10, Math.min(0.90, screenPoint.x)),
      y: Math.max(0.50, Math.min(0.90, screenPoint.y)),
      shoulderWidth: Math.max(0.03, distance(leftShoulder, rightShoulder)),
      bodyX:(midShoulder.x + affected.hip.x) / 2,
      bodyY:(midShoulder.y + affected.hip.y) / 2,
      shoulderX:affected.shoulder.x,
      shoulderY:affected.shoulder.y,
    },
    reason:"stabilizing",
    guidance:`Keep your ${sideLabel} hand relaxed and still on your upper thigh for one moment.`,
  };
}

function lapTargetCandidate(lm){
  const status = lapTargetCandidateStatus(lm);
  lapCalibrationDiagnostic = {reason:status.reason, guidance:status.guidance};
  return status.candidate;
}

function updateLapTargetCalibration(lm, now){
  const lapStep = calibratingAssessment ? upcomingLapStep() : currentTaskLapStep();
  if(!lapStep) return;
  if(seatedTestingCalibrationEnabled()){
    if(lapTargetCalibration.ready){
      // Reframe only when the visible wrist has actually moved away. A brief
      // missing face/hip or lighting check must not erase a stable lap point.
      const wrist=lapTargetCandidate(lm),state=lapTargetCalibration;
      if(calibratingAssessment && wrist && Math.hypot(wrist.x-state.target.x,wrist.y-state.target.y)>.04){
        state.displacedSince??=now;
        if(now-state.displacedSince>=450){
          lapTargetCalibration=newLapTargetCalibration();forwardReachPlacement=null;
        }
      }else state.displacedSince=null;
    }
    if(lapTargetCalibration.ready){
      if(testingReachEnabled() && !forwardReachPlacement?.ready) updateTestingReachPlacement(lm);
      return;
    }
    updateTestingReachLapCalibration(lm,now,lapStep);
    return;
  }
  if(lapTargetCalibration.ready) return;
  const candidate = lapTargetCandidate(lm);
  if(!candidate){
    if(lapTargetCalibration.lastCandidateAt && now - lapTargetCalibration.lastCandidateAt <= 900) return;
    lapTargetCalibration.samples = [];
    lapTargetCalibration.target = null;
    dynamicTargetPos = null;
    return;
  }
  lapTargetCalibration.lastCandidateAt = now;

  const samples = lapTargetCalibration.samples;
  samples.push({
    x:candidate.x,
    y:candidate.y,
    bodyX:candidate.bodyX,
    bodyY:candidate.bodyY,
    shoulderX:candidate.shoulderX,
    shoulderY:candidate.shoulderY,
    t:now,
    shoulderWidth:candidate.shoulderWidth,
  });
  // Keep the last 1.2 s of samples. The window is time-based: a fixed count of
  // 24 would span only 0.4 s at 60 frames per second, less than the stability
  // window, and the hand could never be judged still.
  while(samples.length > 120 || (samples.length && now - samples[0].t > 1200)) samples.shift();

  let center = {x:medianValue(samples.map(s => s.x)), y:medianValue(samples.map(s => s.y))};
  lapTargetCalibration.target = center;
  dynamicTargetPos = center;
  if(samples.length < LAP_CALIBRATION_MIN_SAMPLES) return;

  const width = medianValue(samples.map(s => s.shoulderWidth)) || 0.03;
  const maxJitter = Math.max(0.028, width * 0.18);
  const maxBodyJitter = Math.max(0.035, width * 0.22);
  const bodyCenter = {
    x:medianValue(samples.map(s => s.bodyX)),
    y:medianValue(samples.map(s => s.bodyY)),
  };
  const stableSamples = samples.filter(s =>
    Math.hypot(s.x-center.x, s.y-center.y) <= maxJitter
    && Math.hypot(s.bodyX-bodyCenter.x, s.bodyY-bodyCenter.y) <= maxBodyJitter
  );
  const requiredStableSamples = Math.max(LAP_CALIBRATION_MIN_SAMPLES, Math.ceil(samples.length * 0.70));
  if(stableSamples.length < requiredStableSamples) return;
  const stableDuration = stableSamples[stableSamples.length-1].t - stableSamples[0].t;
  if(stableDuration < LAP_CALIBRATION_MIN_MS) return;
  center = {
    x:medianValue(stableSamples.map(s => s.x)),
    y:medianValue(stableSamples.map(s => s.y)),
  };

  // Do not lock an unusable lap: allow the patient to reframe and recalibrate
  // when there is not enough room to the screen-right of the affected arm.
  if(needsForwardReachPlacement()){
    const calibratedShoulder = {
      x:medianValue(stableSamples.map(s => s.shoulderX)),
      y:medianValue(stableSamples.map(s => s.shoulderY)),
    };
    const placement = calculateForwardReachPlacement(lm, center, calibratedShoulder);
    if(!placement.ready){
      lapCalibrationDiagnostic = {reason:placement.reason, guidance:placement.guidance || "Keep your affected arm visible."};
      return;
    }
    forwardReachPlacement = placement;
  }
  lapTargetCalibration.ready = true;
  lapTargetCalibration.target = center;
  dynamicTargetPos = center;
  if(!lapTargetCalibration.announced){
    lapTargetCalibration.announced = true;
    postRN({
      type:"lap_target_calibrated",
      task_id:tasks[currentTaskIdx].id,
      step_id:lapStep.id,
      x:+center.x.toFixed(4),
      y:+center.y.toFixed(4),
      sample_count:samples.length,
    });
  }
}

function updateTestingReachPlacement(lm){
  if(!lm || !lapTargetCalibration.ready) return;
  const shoulder=sideLandmarks(lm,AFFECTED_SIDE).shoulder;
  if(![lm[11],lm[12],shoulder].every(point=>landmarkIsInFrame(point,0.35))){
    lapCalibrationDiagnostic={reason:"shoulders_not_visible",guidance:"Keep both shoulders visible so the reach targets can be placed. You can tilt the camera without moving farther away."};
    return;
  }
  const placement=calculateForwardReachPlacement(lm,lapTargetCalibration.target,shoulder);
  if(!placement.ready){
    lapCalibrationDiagnostic={reason:placement.reason,guidance:placement.guidance || "Adjust the camera so both reach circles fit in view."};
    return;
  }
  forwardReachPlacement=placement;
}

function updateTestingReachLapCalibration(lm,now,lapStep){
  const candidate=lapTargetCandidate(lm);
  const state=lapTargetCalibration;
  if(!candidate){
    if(state.samples.length && now-state.lastCandidateAt>450){
      state.samples=[];state.target=null;dynamicTargetPos=null;
    }
    return;
  }
  // A tracking gap starts a new hold; an old wrist location cannot become the
  // lap point when the hand reappears elsewhere in the frame.
  if(state.samples.length && now-state.lastCandidateAt>450) state.samples=[];
  state.lastCandidateAt=now;
  const samples=state.samples;
  samples.push({x:candidate.x,y:candidate.y,t:now});
  // Retain the sample straddling the two-second boundary. Dropping every
  // sample older than 2.1 s can leave a low-FPS window forever short of 2 s.
  while(samples.length>240 || (samples.length>LAP_CALIBRATION_MIN_SAMPLES && samples[1].t<=now-TESTING_REACH_LAP_MIN_MS))samples.shift();
  const center={x:medianValue(samples.map(s=>s.x)),y:medianValue(samples.map(s=>s.y))};
  state.target=center;
  dynamicTargetPos=center;
  const maxJitter=0.04;
  const inliers=samples.filter(s=>Math.hypot(s.x-center.x,s.y-center.y)<=maxJitter);
  const heldMs=inliers.length?inliers[inliers.length-1].t-inliers[0].t:0;
  const stable=inliers.length>=Math.ceil(samples.length*.9)
    && inliers[inliers.length-1]===samples[samples.length-1];
  lapCalibrationDiagnostic={reason:"stabilizing",guidance:stable
    ? `Keep your hand still on your lap · ${(Math.min(heldMs,TESTING_REACH_LAP_MIN_MS)/1000).toFixed(1)} of 2 seconds.`
    : "Keep your hand still on your lap for two seconds; tracking will restart once it settles."};
  if(inliers.length<LAP_CALIBRATION_MIN_SAMPLES || heldMs<TESTING_REACH_LAP_MIN_MS || !stable)return;
  state.ready=true;
  state.target=center;
  if(!state.announced){
    state.announced=true;
    postRN({type:"lap_target_calibrated",task_id:tasks[currentTaskIdx].id,step_id:lapStep.id,
      x:+center.x.toFixed(4),y:+center.y.toFixed(4),sample_count:samples.length});
  }
  if(testingReachEnabled())updateTestingReachPlacement(lm);
}

function needsForwardReachPlacement(){
  return tasks.slice(currentTaskIdx).some(task => task.id === "T1");
}

function isForwardReachTarget(step){
  return !!step && ["T1-S1", "T1-S2", "T1-S3"].includes(step.id);
}

function calculateForwardReachPlacement(lm, rawLap, rawShoulder=null){
  const affected = lm && sideLandmarks(lm, AFFECTED_SIDE);
  const radius = Math.min(Math.max(0.11, shoulderWidth(lm) * 0.55), 0.18);
  // A phone's cover view can crop part of the source frame. Reserve space
  // inside the displayed part too, not just inside the camera image.
  const viewRect = typeof stage !== "undefined" ? stage.getBoundingClientRect() : null;
  const frameRect = typeof cameraFrame !== "undefined" ? cameraFrame.getBoundingClientRect() : null;
  const leftEdge = viewRect?.width > 0 && frameRect?.width > 0
    ? Math.min(1, Math.max(0, (viewRect.left-frameRect.left)/frameRect.width)) : 0;
  const rightEdge = viewRect?.width > 0 && frameRect?.width > 0
    ? Math.min(1, Math.max(0, (viewRect.right-frameRect.left)/frameRect.width)) : 1;
  const topEdge = viewRect?.height > 0 && frameRect?.height > 0
    ? Math.max(0, (viewRect.top-frameRect.top)/frameRect.height) : 0;
  const bottomEdge = viewRect?.height > 0 && frameRect?.height > 0
    ? Math.min(1, (viewRect.bottom-frameRect.top)/frameRect.height) : 1;
  const shoulder = rawShoulder || affected?.shoulder;
  return RehynReachTarget.fitSeatedForwardReachTargets({
    lap:rawLap && mirrorX(rawLap),
    shoulder:shoulder && mirrorX(shoulder),
    shoulderWidth:lm && lm[11] && lm[12] ? Math.abs(lm[11].x-lm[12].x) : 0,
    radius,
    leftEdge,
    rightEdge,
    topEdge,
    bottomEdge,
    aspect:video.videoWidth && video.videoHeight ? video.videoWidth/video.videoHeight : 4/3,
  });
}

function forwardReachDistance(a,b){
  return RehynReachTarget.screenDistance(a,b,
    video.videoWidth && video.videoHeight ? video.videoWidth/video.videoHeight : 4/3);
}

function calibrationLandmarkStatus(lm){
  const cameraReady = video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0;
  if(!lm || lm.length < 33){
    return {cameraReady, armVisible:false, seatedAnchorsVisible:false, lapReady:false, ready:false};
  }
  const affected = sideLandmarks(lm, AFFECTED_SIDE);
  const visibility = 0.35;
  const faceVisible = [lm[0], lm[9], lm[10]].some(point => landmarkIsInFrame(point, visibility));
  const armVisible = faceVisible && [lm[11], lm[12], affected.elbow, affected.wrist]
    .every(point => landmarkIsInFrame(point, visibility));
  const seatedAnchorsVisible = seatedTestingCalibrationEnabled()
    ? landmarkIsInFrame(affected.wrist, visibility)
    : [affected.hip, affected.wrist].every(point => landmarkIsInFrame(point, visibility));
  const lapReady = !!(lapTargetCalibration.ready && lapTargetCalibration.target
    && (!needsForwardReachPlacement() || forwardReachPlacement?.ready));
  return {
    cameraReady,
    armVisible,
    seatedAnchorsVisible,
    lapReady,
    lapGuidance:lapCalibrationDiagnostic.guidance,
    ready:cameraReady && armVisible && seatedAnchorsVisible && lapReady && (!testingMouthEnabled() || mouthTargetCalibration.locked),
  };
}

function setCalibrationCheck(element, complete){
  element.classList.toggle("done", !!complete);
  element.querySelector(".statusDot").textContent = complete ? "✓" : element === calibrationCamera ? "1" : element === calibrationArm ? "2" : element === calibrationSeat ? "3" : element === calibrationLap ? "4" : "5";
}

function updatePreAssessmentCalibrationUI(lm){
  if(!calibratingAssessment) return;
  const status = calibrationLandmarkStatus(lm);
  const lapGuidance=status.lapGuidance || "Rest your affected hand on your lap where the camera can see it.";
  const lapLocated=seatedTestingCalibrationEnabled() ? lapTargetCalibration.ready : status.lapReady;
  const trunkBaselineReady = testingMouthEnabled() ? !!assessmentQuality.baseline : !testingReachEnabled() || !!assessmentQuality.trunkLeanBaseline;
  const trunkBaselineReason=testingReachEnabled() && !trunkBaselineReady && status.ready
    ? assessmentQuality.trunkLeanMetrics?.metricsFromLandmarks(lm,video.videoWidth/video.videoHeight)?.reason : null;
  const trunkBaselineGuidance=trunkBaselineReason
    ? `${trunkBaselineReason} The lap target is set; the trunk-lean reference is still being checked.`
    : 'Keep sitting upright with your hand on the saved lap point while the posture reference is checked.';
  const cameraQuality=seatedTestingCalibrationEnabled()?reachCameraStatus():{ready:true};
  if(seatedTestingCalibrationEnabled())syncReachCalibrationChecks(cameraQuality);
  preAssessmentCalibrationReady = status.ready && trunkBaselineReady && cameraQuality.ready;
  if(preAssessmentCalibrationReady){
    setCalibrationCheck(calibrationCamera, true);
    setCalibrationCheck(calibrationArm, true);
    setCalibrationCheck(calibrationSeat, true);
    setCalibrationCheck(calibrationLap, true);
    calibrationProgressFill.style.width = "100%";
    calibrationTitle.textContent = "Calibration complete";
    calibrationLead.textContent = trunkBaselineReady
      ? "Your seated position and lap target are set. Stay seated and do not move the camera."
      : "Your lap and reach targets are set. The trunk-lean reference was unavailable, so that check may be unscored. The exercise will begin now.";
    calibrationAutoStatus.classList.add("ready");
    calibrationAutoStatus.textContent = calibrationInstructionFinished
      ? "Calibration complete. Starting assessment..."
      : (testingMouthEnabled()?mouthVoice.failed:reachVoice.failed)
      ? "Voice paused. Retry voice or turn it off to continue with captions."
      : "Calibration complete. Please finish listening.";
    if(calibrationInstructionFinished) void completePreAssessmentCalibration();
  }else{
    setCalibrationCheck(calibrationCamera, status.cameraReady);
    setCalibrationCheck(calibrationArm, status.armVisible);
    setCalibrationCheck(calibrationSeat, status.seatedAnchorsVisible);
    setCalibrationCheck(calibrationLap, lapLocated);
    const checks = [status.cameraReady, status.armVisible, status.seatedAnchorsVisible, lapLocated];
    if(seatedTestingCalibrationEnabled())checks.push(...['lighting','contrast','landmarks','angles','stability','baseline'].map(key=>cameraQuality.checks?.[key]?.ready===true));
    const completed = checks.filter(Boolean).length;
    calibrationProgressFill.style.width = `${Math.min(90,completed/checks.length*100)}%`;
    calibrationTitle.textContent = testingReachEnabled() && lapTargetCalibration.ready && !forwardReachPlacement?.ready
      ? "Positioning the reach circles"
      : status.ready && !trunkBaselineReady ? "Checking upright posture" : "Let us find your seated position";
    calibrationLead.textContent = seatedTestingCalibrationEnabled() && !lapTargetCalibration.ready
      ? lapGuidance
      : testingReachEnabled() && !forwardReachPlacement?.ready
      ? lapGuidance
      : status.ready && !trunkBaselineReady
      ? trunkBaselineGuidance
      : status.armVisible && !status.seatedAnchorsVisible
      ? "Tilt the camera down slightly until your affected hand and the top of your affected thigh are visible. You do not need to show your knees or full lap."
      : status.armVisible && status.seatedAnchorsVisible && !status.lapReady
        ? status.lapGuidance
      : "Sit still with your affected hand resting on the visible part of your lap. Keep your face, shoulders, affected arm, and the top of your affected thigh in view.";
    calibrationAutoStatus.classList.remove("ready");
    calibrationAutoStatus.textContent = seatedTestingCalibrationEnabled() && !lapTargetCalibration.ready
      ? lapGuidance
      : testingReachEnabled() && !forwardReachPlacement?.ready
      ? `Lap point saved. ${lapGuidance}`
      : status.ready && !trunkBaselineReady
      ? trunkBaselineGuidance
      : status.armVisible && status.seatedAnchorsVisible && !status.lapReady
      ? status.lapGuidance
      : testingMouthEnabled() && lapLocated && !mouthTargetCalibration.locked
      ? "Lap point saved. Keep your mouth visible and your head still while the mouth target is located."
      : "Keep still. Assessment will start automatically.";
    if(seatedTestingCalibrationEnabled() && !cameraQuality.ready && status.cameraReady
      && !['baseline','settling'].includes(cameraQuality.issue)){
      calibrationTitle.textContent='Checking camera quality';
      calibrationLead.textContent=cameraQuality.message;
      const q=cameraQuality.evidence;
      const details=[];
      if(Number.isFinite(q?.bodyLuma))details.push(`Light level: ${Math.round(q.bodyLuma)}/255.`);
      if(q?.contrast?.length)details.push(`Shoulder contrast: ${q.contrast.map(v=>v==null?'not measured':v.toFixed(1)).join(' / ')} (minimum 12).`);
      calibrationAutoStatus.textContent=(cameraQuality.detail||cameraQuality.message)+' '+details.join(' ');
    }else if(seatedTestingCalibrationEnabled() && !cameraQuality.ready && status.cameraReady && lapLocated && (!testingReachEnabled() || forwardReachPlacement?.ready)){
      calibrationTitle.textContent='Checking upright posture';
      calibrationLead.textContent='Your lap point is saved. Keep sitting upright while the grading angles and posture reference are checked.';
      calibrationAutoStatus.textContent=`Clear camera measurements: ${(Math.min(cameraQuality.clearMs||0,2000)/1000).toFixed(1)} of 2 seconds.`;
      if(cameraQuality.issue==='baseline' && cameraQuality.detail)calibrationAutoStatus.textContent+=' '+cameraQuality.detail;
    }
    if(seatedTestingCalibrationEnabled() && !cameraQuality.ready && cameraQuality.lastReset){
      calibrationAutoStatus.textContent+=` Last timer restart (${cameraQuality.resetCount}): ${cameraQuality.lastReset.detail}`;
    }
  }
}

async function completePreAssessmentCalibration(){
  if(!calibratingAssessment || !preAssessmentCalibrationReady || !calibrationInstructionFinished || calibrationAutoStartInProgress) return;
  calibrationAutoStartInProgress = true;
  calibrationAutoStatus.classList.add("ready");
  calibrationAutoStatus.textContent = "Calibration complete. Starting assessment...";
  calibrationLead.textContent = (testingMouthEnabled() ? assessmentQuality.baseline : assessmentQuality.trunkLeanBaseline)
    ? "Stay seated in this position and do not move the camera. The assessment will begin automatically."
    : "Your lap and reach targets are set. The trunk-lean reference was unavailable; the exercise will begin automatically.";
  // Let the continuous ring finish and the central check become visible, even
  // when voice is off or finishes immediately. Detection still gates completion.
  const ringAnimation = seatedTestingCalibrationEnabled() ? Promise.resolve()
    : new Promise(resolve => setTimeout(resolve, 2200));
  await playVoice(CALIBRATION_COMPLETE_INSTRUCTION);
  await ringAnimation;
  if(!calibratingAssessment) return;
  // Lighting/tracking can change while the completion cue is playing. Never
  // start from a previously green check when the current camera view fails.
  if(seatedTestingCalibrationEnabled() && (!reachCameraStatus().ready || !calibrationLandmarkStatus(latestPoseLandmarks).ready)){
    calibrationAutoStartInProgress=false;preAssessmentCalibrationReady=false;
    updatePreAssessmentCalibrationUI(latestPoseLandmarks);return;
  }
  finalizePatientFaceReference();
  assessmentLapTarget = lapTargetCalibration.target
    ? {...lapTargetCalibration.target}
    : null;
  const lapStep = upcomingLapStep();
  assessmentLapTargetRadius = lapStep
    ? testingReachEnabled() && forwardReachPlacement?.ready
      ? (forwardReachPlacement.lapRadius || forwardReachPlacement.radius)
      : Math.min(Math.max(0.10, shoulderWidth(latestPoseLandmarks) * 0.55), 0.18)
    : null;
  preservePreAssessmentLapCalibration = true;
  calibratingAssessment = false;
  calibrationOverlay.classList.add("hidden");
  ui.classList.remove("hidden");
  postRN({
    type:"assessment_calibrated",
    affected_side:AFFECTED_SIDE,
    lap_target:lapTargetCalibration.target,
    lap_target_radius:assessmentLapTargetRadius,
    sample_count:lapTargetCalibration.samples.length,
    trunk_lean_reference_ready:!!assessmentQuality.trunkLeanBaseline,
    automatic:true,
  });
  await startStep();
}

if(URL_PARAMS.get("test_mode") === "lap_calibration"){
  function withLapCalibrationTestContext(callback){
    const savedTasks = tasks;
    const savedCalibratingAssessment = calibratingAssessment;
    if(!upcomingLapStep()){
      tasks = [{id:"CALIBRATION_TEST", steps:[{id:"CALIBRATION_TEST_LAP", target:{landmark:"LAP_DYNAMIC"}}]}];
    }
    calibratingAssessment = true;
    try{
      return callback();
    }finally{
      tasks = savedTasks;
      calibratingAssessment = savedCalibratingAssessment;
    }
  }
  window.__rehynLapCalibrationTest = {
    candidate: (landmarks) => lapTargetCandidate(landmarks),
    diagnose: (landmarks) => lapTargetCandidateStatus(landmarks),
    runStableSequence: (landmarks, frameCount=12, frameMs=100) => {
      const savedCalibration = lapTargetCalibration;
      const savedDynamicTarget = dynamicTargetPos;
      lapTargetCalibration = newLapTargetCalibration();
      withLapCalibrationTestContext(() => {
        for(let frame=0; frame<frameCount; frame += 1){
          updateLapTargetCalibration(landmarks, frame * frameMs);
        }
      });
      const result = {
        ready:lapTargetCalibration.ready,
        target:lapTargetCalibration.target,
        sampleCount:lapTargetCalibration.samples.length,
      };
      lapTargetCalibration = savedCalibration;
      dynamicTargetPos = savedDynamicTarget;
      return result;
    },
    runSequence: (frames, frameMs=100) => {
      const savedCalibration = lapTargetCalibration;
      const savedDynamicTarget = dynamicTargetPos;
      lapTargetCalibration = newLapTargetCalibration();
      withLapCalibrationTestContext(() => {
        frames.forEach((landmarks, frame) => updateLapTargetCalibration(landmarks, frame * frameMs));
      });
      const result = {
        ready:lapTargetCalibration.ready,
        target:lapTargetCalibration.target,
        sampleCount:lapTargetCalibration.samples.length,
      };
      lapTargetCalibration = savedCalibration;
      dynamicTargetPos = savedDynamicTarget;
      return result;
    },
    applyCalibrationSequence: (landmarks, frameCount=12, frameMs=100) => {
      lapTargetCalibration = newLapTargetCalibration();
      calibratingAssessment = true;
      calibrationInstructionFinished = true;
      preAssessmentCalibrationReady = false;
      calibrationAutoStartInProgress = false;
      withLapCalibrationTestContext(() => {
        for(let frame=0; frame<frameCount; frame += 1){
          updateLapTargetCalibration(landmarks, frame * frameMs);
        }
      });
      updatePreAssessmentCalibrationUI(landmarks);
      return {
        ready:preAssessmentCalibrationReady,
        target:lapTargetCalibration.target,
        autoStarting:calibrationAutoStartInProgress,
        statusText:calibrationAutoStatus.textContent,
        title:calibrationTitle.textContent,
      };
    },
    lockAssessmentTarget:(target) => {
      assessmentLapTarget = {...target};
      assessmentLapTargetRadius = 0.10;
      lapTargetCalibration.target = {...target};
      lapTargetCalibration.ready = true;
      return getEffectiveTargetXY({target:{x:0.5, y:0.8, landmark:"LAP_DYNAMIC"}});
    },
    effectiveTarget:() => getEffectiveTargetXY({target:{x:0.5, y:0.8, landmark:"LAP_DYNAMIC"}}),
    affectedSide:AFFECTED_SIDE,
  };
}

if(URL_PARAMS.get("test_mode") === "mouth_target"){
  window.__rehynMouthTargetTest = {
    affectedSide:AFFECTED_SIDE,
    runTargetSequence:(frames) => {
      const savedCalibration = mouthTargetCalibration;
      mouthTargetCalibration = newMouthTargetCalibration();
      frames.forEach((landmarks, index) => updateMouthTargetCalibration(landmarks, index + 1));
      const result = {
        target:mouthTargetCalibration.target,
        locked:mouthTargetCalibration.locked,
        sampleCount:mouthTargetCalibration.samples.length,
      };
      mouthTargetCalibration = savedCalibration;
      return result;
    },
    evaluatePoseHit:(landmarks, target, radius=0.10) => {
      const savedHand = latestHandLandmarks;
      latestHandLandmarks = null;
      const point = closestAffectedHandPointToTarget(landmarks, target);
      const distance = mouthContactDistance(landmarks, target);
      const affectedWrist = sideLandmarks(landmarks, AFFECTED_SIDE).wrist;
      latestHandLandmarks = savedHand;
      return {
        point,
        distance,
        wristDistance:distXY(affectedWrist, target),
        hit:distance < radius,
      };
    },
  };
}

if(URL_PARAMS.get("test_mode") === "upper_limb_endpoint"){
  window.__rehynUpperLimbEndpointTest = {
    affectedSide:AFFECTED_SIDE,
    placeReach:(landmarks, rawLap) => {
      forwardReachPlacement = calculateForwardReachPlacement(landmarks, rawLap);
      return forwardReachPlacement;
    },
    gateRequiredById:(stepId) => !["T1-S1", "T1-S2", "T3-S1", "T3-S2"].includes(stepId),
    centeredStartTarget:(step, landmarks=null) => {
      const savedPose = latestPoseLandmarks;
      latestPoseLandmarks = landmarks;
      const target = getEffectiveTargetXY(step);
      latestPoseLandmarks = savedPose;
      return target;
    },
    canvasTarget:(step, target) => targetCanvasPoint(step, target),
    evaluateReachHit:(landmarks, target, radius=0.10) => {
      const point = closestAffectedReachPointToTarget(landmarks, target);
      return {point, distance:forwardReachDistance(point, target), hit:forwardReachDistance(point, target) < radius};
    },
  };
}

if(URL_PARAMS.get("test_mode") === "palm_projection"){
  window.__rehynPalmProjectionTest = {
    threshold:PALM_FACING_THRESHOLD,
    evaluate:(landmarks, frameCount=4) => {
      const evidence = palmProjectionEvidence(landmarks);
      let smoothed = 0;
      for(let frame=0; frame<frameCount; frame += 1){
        smoothed = smoothed * 0.55 + evidence.score * 0.45;
      }
      return {...evidence, smoothed, passes:smoothed > PALM_FACING_THRESHOLD};
    },
  };
}

if(URL_PARAMS.get("test_mode") === "hand_selection"){
  window.__rehynAffectedHandTest = {
    affectedSide:AFFECTED_SIDE,
    select:(result, poseLandmarks, now=1000) => {
      latestPoseLandmarks = poseLandmarks;
      affectedHandTrackWrist = null;
      affectedHandTrackSeenAt = 0;
      const selected = selectAffectedHandDetection(result, now);
      return selected ? {
        index:selected.index,
        handedness:selected.handedness,
        poseDistance:selected.poseDistance,
        wrist:{x:selected.wrist.x, y:selected.wrist.y},
      } : null;
    },
  };
}

function computeMetrics(landmarks){
  // landmarks are normalized 0..1
  if(!landmarks || landmarks.length<33) return;
  const L_SHOULDER = landmarks[11], R_SHOULDER = landmarks[12];
  const L_HIP = landmarks[23], R_HIP = landmarks[24];
  const L_ELBOW = landmarks[13], R_ELBOW = landmarks[14];
  const L_WRIST = landmarks[15], R_WRIST = landmarks[16];

  // trunk lean: angle of (mid-shoulder to mid-hip) vs vertical
  const midSh = {x:(L_SHOULDER.x+R_SHOULDER.x)/2, y:(L_SHOULDER.y+R_SHOULDER.y)/2};
  const midHip = {x:(L_HIP.x+R_HIP.x)/2, y:(L_HIP.y+R_HIP.y)/2};
  const dx = midSh.x - midHip.x;
  const dy = midSh.y - midHip.y;
  const trunkAngle = Math.abs(rad2deg(Math.atan2(dx, -dy)));
  trunkLeanMax = Math.max(trunkLeanMax, trunkAngle);

  // shoulder flexion (approx): wrist y above shoulder y means raised
  const wristY = Math.min(L_WRIST.y, R_WRIST.y);
  const shY = Math.min(L_SHOULDER.y, R_SHOULDER.y);
  const flexion = Math.max(0, (shY - wristY)); // larger = arm more raised
  shoulderFlexionMax = Math.max(shoulderFlexionMax, flexion);

  const state = bodyState(landmarks);
  if(!stepStartBodyState) stepStartBodyState = state;
  const affectedShoulder = state.affected.shoulder;
  const unaffectedShoulder = state.unaffected.shoulder;
  const shoulderElevationDelta = unaffectedShoulder.y - affectedShoulder.y;
  // Warnings are confirmed against calibrated posture by assessmentQuality.
  kneeExtensionMaxDeg = Math.max(kneeExtensionMaxDeg, state.affectedKneeAngle);
  ankleDorsiflexionMax = Math.max(ankleDorsiflexionMax, state.affected.heel.y - state.affected.toe.y);
  shoulderElevationMaxDeg = Math.max(shoulderElevationMaxDeg, state.shoulderElevation);
  elbowFlexionMaxDeg = Math.max(elbowFlexionMaxDeg, state.elbowFlexion);
  hipFlexionMaxDeg = Math.max(hipFlexionMaxDeg, state.hipFlexion);
  kneeFlexionMaxDeg = Math.max(kneeFlexionMaxDeg, state.kneeFlexion);
  const affectedDirection = AFFECTED_SIDE === "left" ? -1 : 1;
  affectedPelvisShiftMax = Math.max(affectedPelvisShiftMax, affectedDirection * (state.midHip.x - stepStartBodyState.midHip.x));
  lateralTrunkShiftMax = Math.max(lateralTrunkShiftMax, Math.abs(state.midShoulder.x - stepStartBodyState.midShoulder.x));
  const toeLift = Math.max(0, stepStartBodyState.affected.toe.y - state.affected.toe.y) / state.legLength;
  const ankleMoveX = Math.abs(state.affected.ankle.x - stepStartBodyState.affected.ankle.x) / state.legLength;
  const ankleMove = distance(state.affected.ankle, stepStartBodyState.affected.ankle) / state.legLength;
  const unaffectedAnkleMove = distance(state.unaffected.ankle, stepStartBodyState.unaffected.ankle) / state.legLength;
  const pelvisTravel = Math.abs(state.midHip.x - stepStartBodyState.midHip.x) / state.legLength;
  const wristMove = distance(state.affected.wrist, stepStartBodyState.affected.wrist) / Math.max(0.05, state.shoulderWidth);
  const unaffectedWristMove = distance(state.unaffected.wrist, stepStartBodyState.unaffected.wrist) / Math.max(0.05, state.shoulderWidth);
  const mouthCenter = midpoint(landmarks[9], landmarks[10]);
  const handToMouth = distance(state.affected.wrist, mouthCenter) / Math.max(0.05, state.shoulderWidth);
  toeClearanceMaxRatio = Math.max(toeClearanceMaxRatio, toeLift);
  circumductionMaxRatio = Math.max(circumductionMaxRatio, ankleMoveX);
  affectedStepLengthMaxRatio = Math.max(affectedStepLengthMaxRatio, ankleMove);
  const leadDelta = state.affected.ankle.x - state.unaffected.ankle.x;
  const lead = Math.abs(leadDelta) > Math.max(0.025, state.legLength * 0.08) ? (leadDelta > 0 ? 1 : -1) : 0;
  const activeStep = getCurrentStep();
  const gaitCaptureActive = voiceFinishedAt > 0
    && activeStep
    && ["WALK_READY", "WALK_ACROSS", "WALK_STOPPED"].includes(activeStep.target.landmark);
  if(gaitCaptureActive){
    gaitPelvisTravelMaxRatio = Math.max(gaitPelvisTravelMaxRatio, pelvisTravel);
    gaitAffectedAnkleTravelMaxRatio = Math.max(gaitAffectedAnkleTravelMaxRatio, ankleMove);
    gaitUnaffectedAnkleTravelMaxRatio = Math.max(gaitUnaffectedAnkleTravelMaxRatio, unaffectedAnkleMove);
    if(lead && lastGaitLead && lead !== lastGaitLead) gaitAlternationCount += 1;
    if(lead) lastGaitLead = lead;
    gaitObservedFrameCount += 1;
    if(fullBodyVisibleForWalking(landmarks)) gaitFullBodyVisibleFrameCount += 1;
  }
  affectedWristMoveMaxRatio = Math.max(affectedWristMoveMaxRatio, wristMove);
  unaffectedWristMoveMaxRatio = Math.max(unaffectedWristMoveMaxRatio, unaffectedWristMove);
  handToMouthMinRatio = Math.min(handToMouthMinRatio, handToMouth);
  bodyMetricSamples.push({
    midHipX:state.midHip.x, midHipY:state.midHip.y,
    midShoulderX:state.midShoulder.x, midShoulderY:state.midShoulder.y,
    trunkLean:state.trunkLean, kneeAngle:state.affectedKneeAngle,
    affectedLoadShare:state.affectedLoadShare,
    affectedAnkleX:state.affected.ankle.x, affectedAnkleY:state.affected.ankle.y,
    shoulderWidth:state.shoulderWidth,
  });
  if(bodyMetricSamples.length > 360) bodyMetricSamples.shift();
}

// ---------- Dynamic target resolution ----------
// Returns the on-screen (mirrored, normalized) point at which to draw the target,
// and the candidate body point(s) to test against the target zone.
// Mirroring: user sees themselves mirrored — so on-screen X = (1 - landmark.x).
function mirrorX(p){ return {x: 1 - p.x, y: p.y}; }

const CENTERED_ARM_START_STEP_IDS = new Set([
  "T2-S1", "T3-S1", "T5-S1", "T6-S1", "T7-S1",
  "H1-S1", "H2-S1", "H3-S1", "H4-S1",
]);

function isCenteredArmStartStep(step){
  return !!step && CENTERED_ARM_START_STEP_IDS.has(step.id);
}

function poseMouthTarget(lm){
  if(!lm || lm.length < 11) return null;
  const leftMouth = lm[9];
  const rightMouth = lm[10];
  if(!landmarkIsUsable(leftMouth, 0.25) || !landmarkIsUsable(rightMouth, 0.25)) return null;
  // Video and canvas are mirrored together in CSS. Keep the anatomical mouth
  // point in raw camera coordinates so the displayed ring lands on the mouth.
  return midpoint(leftMouth, rightMouth);
}

function updateMouthTargetCalibration(lm, sampleKey=(lastPoseScanTs || performance.now())){
  if(mouthTargetCalibration.locked && mouthTargetCalibration.target) return mouthTargetCalibration.target;
  const point = poseMouthTarget(lm);
  if(!point) return mouthTargetCalibration.target;
  if(mouthTargetCalibration.lastSampleKey === sampleKey) return mouthTargetCalibration.target;
  mouthTargetCalibration.lastSampleKey = sampleKey;
  const samples = mouthTargetCalibration.samples;
  samples.push(point);
  while(samples.length > 10) samples.shift();
  let center = {
    x:medianValue(samples.map(sample => sample.x)),
    y:medianValue(samples.map(sample => sample.y)),
  };
  mouthTargetCalibration.target = center;
  if(samples.length < 5) return center;
  const maxJitter = Math.max(0.012, shoulderWidth(lm) * 0.08);
  const stableSamples = samples.filter(sample => distXY(sample, center) <= maxJitter);
  if(stableSamples.length < Math.ceil(samples.length * 0.8)) return center;
  center = {
    x:medianValue(stableSamples.map(sample => sample.x)),
    y:medianValue(stableSamples.map(sample => sample.y)),
  };
  mouthTargetCalibration.target = center;
  mouthTargetCalibration.locked = true;
  return center;
}

function affectedPoseHandPoints(lm){
  if(!lm || lm.length < 23) return [];
  const left = AFFECTED_SIDE === "left";
  const indices = left ? [15, 17, 19, 21] : [16, 18, 20, 22];
  return indices
    .map(index => lm[index])
    .filter(point => landmarkIsUsable(point, 0.20));
}

function affectedReachContactPoints(lm){
  return affectedPoseHandPoints(lm).map(point => mirrorX(point));
}

function testingReachWristContact(lm,step=getCurrentStep()){
  const target=step && getEffectiveTargetXY(step);
  return RehynReachTarget.wristContact({landmarks:lm,side:AFFECTED_SIDE,
    target:target && targetCanvasPoint(step,target),
    aspect:video.videoWidth/video.videoHeight});
}

function closestAffectedReachPointToTarget(lm, target){
  if(testingReachEnabled()){
    const contact=testingReachWristContact(lm);
    return contact.point && mirrorX(contact.point);
  }
  const points = affectedReachContactPoints(lm);
  if(!points.length || !target) return null;
  return points.reduce((closest, point) => forwardReachDistance(point, target) < forwardReachDistance(closest, target) ? point : closest);
}

function affectedMouthContactPoints(lm){
  const points = affectedPoseHandPoints(lm).filter(point=>!handToMouthTaskEnabled() || landmarkIsUsable(point,.65));
  if(latestHandLandmarks && latestHandLandmarks.length >= 21
    && performance.now() - latestHandSeenAt <= handLandmarkFreshMs()){
    const fingertips = [4, 8, 12, 16, 20]
      .map(index => latestHandLandmarks[index])
      .filter(point => point && Number.isFinite(point.x) && Number.isFinite(point.y));
    points.push(...fingertips);
    const palm = rawHandPalmCenter();
    if(palm) points.push(palm);
  }
  return points;
}

function closestAffectedHandPointToTarget(lm, target){
  const points = affectedMouthContactPoints(lm);
  if(!points.length || !target) return null;
  const distance = handToMouthTaskEnabled() ? mouthScreenDistance : distXY;
  return points.reduce((closest, point) => distance(point, target) < distance(closest, target) ? point : closest);
}

function mouthContactDistance(lm, target){
  return handToMouthTaskEnabled() ? mouthScreenDistance(closestAffectedHandPointToTarget(lm,target),target) : distXY(closestAffectedHandPointToTarget(lm, target), target);
}

function handPalmCenter(){
  const raw = rawHandPalmCenter();
  return raw ? mirrorX(raw) : null;
}

function rawHandPalmCenter(){
  if(!latestHandLandmarks || latestHandLandmarks.length < 21) return null;
  const h = latestHandLandmarks;
  return {x:(h[0].x + h[5].x + h[17].x)/3, y:(h[0].y + h[5].y + h[17].y)/3};
}

function handWristPoint(){
  if(!latestHandLandmarks || latestHandLandmarks.length < 21) return null;
  return mirrorX(latestHandLandmarks[0]);
}

function activeHandPoint(){
  return handPalmCenter() || handWristPoint();
}

function resolveLandmarkPoint(which){
  if(isHandTask()){
    if(which === "WRIST" || which === "HAND_OPEN" || which === "PINCH" || which === "CHEST"){
      return activeHandPoint();
    }
  }
  const lm = latestPoseLandmarks;
  if(!lm) return null;
  if(which === "WRIST" || which === "WRIST_DYNAMIC" || which === "LAP_DYNAMIC"){
    // pick the wrist that is lower (relaxed) as the "affected" reference,
    // or the higher one if user is actively reaching.
    return mirrorX(lm[15].y > lm[16].y ? lm[15] : lm[16]);
  }
  if(which === "WRISTS"){
    return mirrorX({x:(lm[15].x+lm[16].x)/2, y:(lm[15].y+lm[16].y)/2});
  }
  if(which === "MOUTH"){
    return poseMouthTarget(lm);
  }
  if(which === "CHEST"){
    // mid-shoulder, drop slightly below for chest center
    const ls = lm[11], rs = lm[12];
    const mid = {x:(ls.x+rs.x)/2, y:(ls.y+rs.y)/2 + 0.06};
    return mirrorX(mid);
  }
  if(which === "HAND_OPEN" || which === "PINCH" || which === "PINCH_RELEASED"){
    // Anchor on whichever wrist is most raised (active hand)
    const active = lm[15].y < lm[16].y ? lm[15] : lm[16];
    return mirrorX(active);
  }
  return null;
}

function getEffectiveTargetXY(step){
  if(testingReachEnabled() && reachFlow.step?.id===step?.id) return {...reachFlow.step.target};
  if(isLapTarget(step)){
    return assessmentLapTarget || lapTargetCalibration.target || {x: step.target.x, y: step.target.y};
  }
  if(isForwardReachTarget(step)){
    if(!forwardReachPlacement?.ready) return null;
    return {...(step.id === "T1-S1" ? forwardReachPlacement.start : forwardReachPlacement.raised)};
  }
  if(isCenteredArmStartStep(step)){
    return {x:0.5, y:step.target.y};
  }
  if(isHandTask()){
    return {x: step.target.x, y: step.target.y};
  }
  // Dynamic body targets use the same calibrated coordinates for drawing and hit testing.
  // Otherwise use the static configured x/y.
  const which = step.target.landmark;
  if(which === "WRIST_DYNAMIC"){
    // Capture the wrist position once at the start of the step and lock there.
    if(!dynamicTargetPos){
      const p = resolveLandmarkPoint("WRIST");
      if(p) dynamicTargetPos = {x: p.x, y: p.y};
    }
    return dynamicTargetPos || {x: step.target.x, y: step.target.y};
  }
  if(which === "MOUTH"){
    const p = updateMouthTargetCalibration(latestPoseLandmarks);
    return p ? {x:p.x, y:p.y} : null;
  }
  if(which === "CHEST"){
    const p = resolveLandmarkPoint(which);
    return p ? {x: p.x, y: p.y} : {x: step.target.x, y: step.target.y};
  }
  if(which === "HAND_OPEN" || which === "PINCH" || which === "PINCH_RELEASED"){
    // Anchor on the raised hand so user can see the prompt next to their hand
    const p = resolveLandmarkPoint(which);
    return p ? {x: p.x, y: p.y} : {x: step.target.x, y: step.target.y};
  }
  return {x: step.target.x, y: step.target.y};
}

function targetCanvasPoint(step, target){
  if(!target) return null;
  // The preview canvas is mirrored with CSS. Lap and mouth targets are stored
  // in raw camera coordinates; configured and other resolved targets use the
  // user's on-screen coordinates and must be flipped before canvas drawing.
  if(isLapTarget(step) || isMouthTarget(step)) return target;
  return {x:1-target.x, y:target.y};
}

// ---------- Hand metrics (from HandLandmarker) ----------
function palmProjectionEvidence(h){
  if(!h || h.length < 21) return {
    score:0, planeFacing:0, projectedArea:0, palmSpread:0, depthFlatness:0,
  };
  const clamp01 = (value) => Math.max(0, Math.min(1, value));
  const dist = (a,b) => Math.hypot(a.x-b.x, a.y-b.y);
  const palmWidth = Math.max(0.01, dist(h[5], h[17]));
  const palmHeight = Math.max(0.01, dist(h[0], h[9]));
  const vIndex = {x:h[5].x-h[0].x, y:h[5].y-h[0].y, z:(h[5].z||0)-(h[0].z||0)};
  const vPinky = {x:h[17].x-h[0].x, y:h[17].y-h[0].y, z:(h[17].z||0)-(h[0].z||0)};
  const normal = {
    x:vIndex.y*vPinky.z - vIndex.z*vPinky.y,
    y:vIndex.z*vPinky.x - vIndex.x*vPinky.z,
    z:vIndex.x*vPinky.y - vIndex.y*vPinky.x,
  };
  const normalMag = Math.max(0.0001, Math.hypot(normal.x, normal.y, normal.z));
  const signedPlaneFacing = normal.z / normalMag;
  // The sign flips with handedness and mirroring; its magnitude is the stable
  // evidence that the palm plane is broadside rather than edge-on.
  const planeFacing = clamp01((Math.abs(signedPlaneFacing) - 0.08) / 0.52);
  const projectedAreaRatio = Math.abs(vIndex.x*vPinky.y - vIndex.y*vPinky.x)
    / Math.max(0.0001, palmHeight * palmHeight);
  const projectedArea = clamp01((projectedAreaRatio - 0.06) / 0.34);
  const palmSpread = clamp01((palmWidth / palmHeight - 0.28) / 0.42);
  const palmDepthTilt = Math.abs((h[5].z||0) - (h[17].z||0)) / palmWidth;
  const depthFlatness = clamp01(1 - palmDepthTilt / 0.55);
  return {
    score:clamp01(planeFacing * 0.35 + projectedArea * 0.30 + palmSpread * 0.25 + depthFlatness * 0.10),
    planeFacing,
    projectedArea,
    palmSpread,
    depthFlatness,
    signedPlaneFacing,
  };
}

function computeHandMetrics(){
  // Hand landmarks indices (MediaPipe Hands): 0=wrist, 4=thumb_tip, 5=index_mcp,
  // 8=index_tip, 12=middle_tip, 16=ring_tip, 20=pinky_tip.
  // Open hand: average fingertip distance from wrist is large relative to palm width.
  // Pinch: thumb_tip <-> index_tip distance is small relative to palm width.
  if(!latestHandLandmarks || latestHandLandmarks.length < 21){
    // decay scores so old detections don't linger
    handOpenScore = Math.max(0, handOpenScore - 0.15);
    fistClosureScore = Math.max(0, fistClosureScore - 0.15);
    pinchScore = Math.max(0, pinchScore - 0.15);
    palmFacingScore = Math.max(0, palmFacingScore - 0.15);
    return;
  }
  const h = latestHandLandmarks;
  const clamp01 = (v) => Math.max(0, Math.min(1, v));
  const dist = (a,b) => Math.hypot(a.x-b.x, a.y-b.y);
  const angleDeg = (a,b,c) => {
    const ab = {x:a.x-b.x, y:a.y-b.y, z:(a.z||0)-(b.z||0)};
    const cb = {x:c.x-b.x, y:c.y-b.y, z:(c.z||0)-(b.z||0)};
    const abLen = Math.max(0.0001, Math.hypot(ab.x, ab.y, ab.z));
    const cbLen = Math.max(0.0001, Math.hypot(cb.x, cb.y, cb.z));
    const cos = Math.max(-1, Math.min(1, (ab.x*cb.x + ab.y*cb.y + ab.z*cb.z) / (abLen * cbLen)));
    return Math.acos(cos) * 180 / Math.PI;
  };
  const palmWidth = Math.max(0.01, dist(h[5], h[17])); // index_mcp <-> pinky_mcp
  const palmHeight = Math.max(0.01, dist(h[0], h[9])); // wrist <-> middle_mcp
  const handScale = Math.max(0.01, palmWidth, palmHeight * 0.9);
  const palmEvidence = palmProjectionEvidence(h);
  palmFacingScore = palmFacingScore * 0.55 + palmEvidence.score * 0.45;
  const fingerSpread = (
    dist(h[0], h[8])  +  // wrist to index_tip
    dist(h[0], h[12]) +  // wrist to middle_tip
    dist(h[0], h[16]) +  // wrist to ring_tip
    dist(h[0], h[20])    // wrist to pinky_tip
  ) / 4;
  const fingerDefs = [
    [5, 6, 7, 8],
    [9, 10, 11, 12],
    [13, 14, 15, 16],
    [17, 18, 19, 20],
  ];
  const fingerStraightnessScore = fingerDefs.reduce((sum, [mcp, pip, dip, tip]) => {
    const pipStraight = clamp01((angleDeg(h[mcp], h[pip], h[dip]) - 110) / 60);
    const dipStraight = clamp01((angleDeg(h[pip], h[dip], h[tip]) - 120) / 55);
    const reach = clamp01((dist(h[mcp], h[tip]) / handScale - 0.55) / 0.65);
    return sum + (pipStraight * 0.45 + dipStraight * 0.35 + reach * 0.20);
  }, 0) / fingerDefs.length;
  const fingertipDistanceScore = clamp01((fingerSpread / handScale - 1.15) / 1.15);
  const thumbIndexSpreadScore = clamp01((dist(h[4], h[8]) / handScale - 0.35) / 0.95);
  const open = clamp01(fingerStraightnessScore * 0.62 + fingertipDistanceScore * 0.28 + thumbIndexSpreadScore * 0.10);
  handOpenScore = handOpenScore * 0.6 + open * 0.4;
  const totalFlexion = fingerDefs.reduce((sum, [mcp, pip, dip, tip]) => {
    const pipFlexion = Math.max(0, 180 - angleDeg(h[mcp], h[pip], h[dip]));
    const dipFlexion = Math.max(0, 180 - angleDeg(h[pip], h[dip], h[tip]));
    return sum + pipFlexion + dipFlexion;
  }, 0) / fingerDefs.length;
  fingerTotalFlexionMaxDeg = Math.max(fingerTotalFlexionMaxDeg, totalFlexion);
  fingerAbductionMaxRatio = Math.max(fingerAbductionMaxRatio, dist(h[8], h[20]) / palmWidth);
  thumbIndexMinDistanceRatio = Math.min(thumbIndexMinDistanceRatio, dist(h[4], h[8]) / palmWidth);

  const palmCenter = {x:(h[0].x + h[5].x + h[9].x + h[13].x + h[17].x)/5, y:(h[0].y + h[5].y + h[9].y + h[13].y + h[17].y)/5};
  const tipPalmDist = (dist(h[8], palmCenter) + dist(h[12], palmCenter) + dist(h[16], palmCenter) + dist(h[20], palmCenter)) / 4;
  const closureRatio = tipPalmDist / palmWidth;
  const closure = clamp01((2.15 - closureRatio) / 0.85);
  fistClosureScore = fistClosureScore * 0.6 + closure * 0.4;

  const pinchDist = dist(h[4], h[8]) / palmWidth;
  // pinchDist < 0.5 means tips are touching; > 1.2 means apart
  const pinch = clamp01((1.2 - pinchDist) / 0.7);
  pinchScore = pinchScore * 0.6 + pinch * 0.4;
}

function needsHandLandmarks(){
  if(testingReachEnabled())return false; // T1 uses Pose Lite's wrist; no second hand model.
  if(handToMouthTaskEnabled())return !calibratingAssessment && !stepCompleted && isMouthTarget(getCurrentStep());
  if(isHandTask()) return true;
  const task = tasks[currentTaskIdx];
  const step = getCurrentStep();
  const landmark = step && step.target ? step.target.landmark : "";
  const measures = step && Array.isArray(step.measure) ? step.measure : [];
  return landmark === "HAND_OPEN"
    || landmark === "HAND_CLOSED"
    || landmark === "PINCH"
    || landmark === "PINCH_RELEASED"
    || landmark === "OBJECT_COUPLED"
    || landmark === "OBJECT_AT_TARGET"
    || landmark === "OBJECT_RELEASED"
    || landmark === "MOUTH"
    || isAdvancedObjectMode()
    || measures.some((name) => HAND_METRIC_NAMES.has(name));
}

function isHandPerformanceBackoff(){
  return isHandTask() && currentFps > 0 && currentFps < MIN_RUNTIME_FPS;
}

function handLandmarkFreshMs(){
  if(handToMouthTaskEnabled())return 220;
  return isHandPerformanceBackoff() ? HAND_BACKOFF_LANDMARK_FRESH_MS : HAND_LANDMARK_FRESH_MS;
}

function getHandCenter(){
  const palm = handPalmCenter();
  if(palm) return palm;
  const w = activeWrist(latestPoseLandmarks);
  return w ? {x:w.x, y:w.y} : null;
}

function detectAxonAIMarker(now){
  if(!isAdvancedObjectMode() || !markerCtx || video.readyState < 2) return null;
  if(now - lastMarkerScanTs < MARKER_SCAN_INTERVAL_MS) return latestMarker;
  lastMarkerScanTs = now;
  try{
    markerCtx.drawImage(video, 0, 0, markerCanvas.width, markerCanvas.height);
    const img = markerCtx.getImageData(0, 0, markerCanvas.width, markerCanvas.height).data;
    let sx = 0, sy = 0, count = 0;
    for(let y = 0; y < markerCanvas.height; y += 2){
      for(let x = 0; x < markerCanvas.width; x += 2){
        const i = (y * markerCanvas.width + x) * 4;
        const r = img[i], g = img[i+1], b = img[i+2];
        const max = Math.max(r,g,b), min = Math.min(r,g,b);
        const saturation = max - min;
        const orangeMarker = r > 150 && g > 70 && g < 185 && b < 120 && saturation > 60;
        const greenMarker = g > 135 && r < 135 && b < 150 && saturation > 55;
        const blueMarker = b > 145 && r < 140 && g > 70 && saturation > 55;
        if(orangeMarker || greenMarker || blueMarker){
          sx += x;
          sy += y;
          count++;
        }
      }
    }
    if(count < 12){
      latestMarker = null;
      return null;
    }
    const centerRaw = {x:sx/count/markerCanvas.width, y:sy/count/markerCanvas.height};
    const object_center = {x:1 - centerRaw.x, y:centerRaw.y};
    const object_visibility = Math.min(1, count / 160);
    markerHistory.push({x:object_center.x, y:object_center.y, t:now});
    markerHistory = markerHistory.filter(p => now - p.t < 1200).slice(-14);
    let jitter = 0;
    for(let i=1; i<markerHistory.length; i++){
      jitter += Math.hypot(markerHistory[i].x-markerHistory[i-1].x, markerHistory[i].y-markerHistory[i-1].y);
    }
    const markerCenterJitter = markerHistory.length > 1 ? jitter / (markerHistory.length - 1) : 0;
    const object_stability = Math.max(0, Math.min(1, 1 - markerCenterJitter / MARKER_JITTER_LIMIT));
    latestMarker = {object_center, object_visibility, object_stability, markerCenterJitter};
    return latestMarker;
  }catch(e){
    latestMarker = null;
    return null;
  }
}

function updateRuntimeDiagnostics(now){
  frameStats.totalFrames += 1;
  if(latestHandLandmarks && (now - latestHandSeenAt) < handLandmarkFreshMs()) frameStats.handFrames += 1;
  if(latestMarker) frameStats.markerFrames += 1;
  if(lastFrameTs){
    const fpsNow = 1000 / Math.max(1, now - lastFrameTs);
    currentFps = currentFps ? currentFps * 0.85 + fpsNow * 0.15 : fpsNow;
  }
  lastFrameTs = now;

  const handCenter = getHandCenter();
  if(isAdvancedObjectMode() && latestMarker && handCenter){
    const d = Math.hypot(handCenter.x - latestMarker.object_center.x, handCenter.y - latestMarker.object_center.y);
    if(d < 0.22){
      if(handObjectOverlapStartedAt == null) handObjectOverlapStartedAt = now;
      handObjectOverlapMs = Math.max(handObjectOverlapMs, now - handObjectOverlapStartedAt);
    }else{
      handObjectOverlapStartedAt = null;
    }
    objectTransportSamples.push({t:now, hand:handCenter, object:latestMarker.object_center, open:handOpenScore});
    objectTransportSamples = objectTransportSamples.filter(s => now - s.t < 5000).slice(-80);
  }

  const handDetectionRate = frameStats.totalFrames ? frameStats.handFrames / frameStats.totalFrames : 0;
  const markerDetectionRate = frameStats.totalFrames ? frameStats.markerFrames / frameStats.totalFrames : 0;
  if(isAdvancedObjectMode()){
    diagnosticsBadge.classList.remove("hidden");
    const smoothOk = currentFps >= MIN_RUNTIME_FPS;
    const handOk = handDetectionRate >= 0.8;
    const markerOk = markerDetectionRate >= 0.8 && latestMarker && latestMarker.object_stability > 0.45;
    diagnosticsBadge.classList.toggle("good", !!(smoothOk && handOk && markerOk));
    diagnosticsBadge.classList.toggle("warn", !(smoothOk && handOk && markerOk));
    diagnosticsText.textContent = markerOk
      ? `Hand clear · marker clear · ${Math.round(currentFps)} FPS`
      : "I cannot see the marker clearly yet. Turn the marker toward the camera.";
  }else{
    diagnosticsBadge.classList.add("hidden");
  }
}

function getAdvancedQuality(){
  const handDetectionRate = frameStats.totalFrames ? frameStats.handFrames / frameStats.totalFrames : 0;
  const markerDetectionRate = frameStats.totalFrames ? frameStats.markerFrames / frameStats.totalFrames : 0;
  const markerCenterJitter = latestMarker ? latestMarker.markerCenterJitter : 1;
  const qualityOk = isAdvancedObjectMode()
    && handDetectionRate >= 0.8
    && markerDetectionRate >= 0.8
    && handObjectOverlapMs >= 1000
    && markerCenterJitter <= MARKER_JITTER_LIMIT
    && currentFps >= MIN_RUNTIME_FPS;
  return {
    qualityOk,
    currentFps:+currentFps.toFixed(1),
    handDetectionRate:+handDetectionRate.toFixed(2),
    markerDetectionRate:+markerDetectionRate.toFixed(2),
    markerCenterJitter:+markerCenterJitter.toFixed(3),
    handObjectOverlapMs:Math.round(handObjectOverlapMs),
    object_center:latestMarker ? latestMarker.object_center : null,
    object_visibility:latestMarker ? +latestMarker.object_visibility.toFixed(2) : 0,
    object_stability:latestMarker ? +latestMarker.object_stability.toFixed(2) : 0,
  };
}

function computeAdvancedObjectMetrics(){
  const q = getAdvancedQuality();
  if(!isAdvancedObjectMode()){
    return {advanced_object_mode:false};
  }
  if(!q.qualityOk){
    return {
      advanced_object_mode:true,
      object_metric_status:"insufficient_marker_data",
      patient_message:"Not enough data. Please adjust the marker and lighting, then try again.",
      ...q,
    };
  }
  let coupledFrames = 0, releaseFrames = 0, endpointError = null;
  for(const s of objectTransportSamples){
    const d = Math.hypot(s.hand.x - s.object.x, s.hand.y - s.object.y);
    if(d < 0.22) coupledFrames += 1;
    if(d > 0.24 && s.open > 0.5) releaseFrames += 1;
    endpointError = Math.hypot(s.object.x - 0.70, s.object.y - 0.65);
  }
  const objectHandCoupling = objectTransportSamples.length ? coupledFrames / objectTransportSamples.length : 0;
  const objectHandSeparation = objectTransportSamples.length ? releaseFrames / objectTransportSamples.length : 0;
  const releaseDelayMs = objectHandSeparation > 0 ? Math.max(0, Math.round(1200 * (1 - objectHandSeparation))) : null;
  return {
    advanced_object_mode:true,
    object_metric_status:"quality_passed",
    objectHandCoupling:+objectHandCoupling.toFixed(2),
    objectHandSeparation:+objectHandSeparation.toFixed(2),
    releaseDelayMs,
    placementEndpointError:endpointError == null ? null : +endpointError.toFixed(3),
    ...q,
  };
}

// Returns the user's shoulder-width in normalized coords (a robust "size unit")
function shoulderWidth(lm){
  if(!lm) return 0.18;
  const ls = lm[11], rs = lm[12];
  if(!ls || !rs) return 0.18;
  return Math.max(0.08, Math.min(0.40, Math.hypot(ls.x - rs.x, ls.y - rs.y)));
}

// Effective hit radius for a step — scales with the user's body so it's stable
// regardless of how close they are to the camera. Tightened (Phase B+) so a
// resting wrist doesn't accidentally trigger the next target.
function effectiveRadius(step, lm){
  if(isForwardReachTarget(step) && forwardReachPlacement?.ready) return forwardReachPlacement.radius;
  const baseR = step.target.r || 0.10;
  if(isLapTarget(step)){
    if(Number.isFinite(assessmentLapTargetRadius)) return assessmentLapTargetRadius;
    const radius = Math.min(Math.max(0.10, shoulderWidth(lm) * 0.55), 0.18);
    if(lapTargetCalibration.ready) assessmentLapTargetRadius = radius;
    return radius;
  }
  if(isHandTask()){
    return Math.min(Math.max(baseR, 0.12), 0.28);
  }
  const sw = shoulderWidth(lm);
  // Tighter radius — about 0.55 × shoulder-width for most targets.
  // Mouth uses fingertip/palm contact, so its visible circle can stay centered
  // and compact instead of expanding enough to admit the lower wrist.
  const which = step.target.landmark;
  let r = Math.max(baseR, sw * 0.55);
  if(which === "MOUTH"){
    r = Math.max(baseR, sw * 0.48);
    return Math.min(r, 0.14);
  }
  if(which === "CHEST"){
    r = Math.max(r, sw * 0.70);
  }
  return Math.min(r, 0.18);
}

// State for "must actually move" rule and voice-gate. Reset on every step.
let stepStartedAt = 0;
let voiceFinishedAt = 0;        // 0 until voice intro for current step finishes
let arrivedAfterMovement = false; // becomes true once wrist has moved >= 0.18 from step-start
let stepStartWristXY = null;    // wrist position captured at step start
let fistCloseReadyObserved = false;
let fistClosureMinScore = 1;
let fistClosingStarted = false;
let targetAttemptStartPoint = null;
let targetAttemptStartGesture = null;
let targetAttemptMaxDisplacement = 0;
let targetAttemptMaxGestureChange = 0;
let nearMissStartedAt = null;
let nearMissReason = "";
let lastNearMissCoachingAt = 0;
let nearMissCoachingCount = 0;
let nearMissEvents = [];
let correctionVoicePlaying = false;
const NEAR_MISS_DWELL_MS = 900;
const NEAR_MISS_COOLDOWN_MS = 7000;
const NEAR_MISS_MAX_COACHING_PER_STEP = 2;

function distXY(a,b){ if(!a||!b) return Infinity; return Math.hypot(a.x-b.x, a.y-b.y); }

function activeWrist(lm){
  const step = getCurrentStep();
  if(isLapTarget(step) && lm) return sideLandmarks(lm, AFFECTED_SIDE).wrist;
  if(isHandTask()){
    return activeHandPoint();
  }
  if(!lm) return null;
  const affectedWrist = sideLandmarks(lm, AFFECTED_SIDE).wrist;
  if(step && step.target && step.target.landmark === "MOUTH"){
    return affectedWrist;
  }
  // Upper-limb collection must stay on the survey-selected affected side.
  // Switching to whichever wrist happens to be nearer can reset the movement
  // gate midway through a correct first approach.
  return affectedWrist ? mirrorX(affectedWrist) : null;
}

function activeControlPoint(lm){
  if(!lm) return null;
  if(isLowerTask()){
    const step = getCurrentStep();
    const state = bodyState(lm);
    if(step && ["HIP_RISEN", "STAND_UPRIGHT", "HIP_SEATED"].includes(step.target.landmark)) return mirrorX(state.midHip);
    if(step && ["AFFECTED_KNEE_LIFTED", "UNAFFECTED_KNEE_LIFTED"].includes(step.target.landmark)){
      const side = step.target.landmark === "AFFECTED_KNEE_LIFTED" ? state.affected : state.unaffected;
      return mirrorX(side.knee);
    }
    return mirrorX(state.affected.ankle);
  }
  if(isBalanceTask()){
    return mirrorX(bodyState(lm).midHip);
  }
  return activeWrist(lm);
}

function updateMovementGate(lm){
  if(!stepStartWristXY){
    const w = activeControlPoint(lm);
    if(w) stepStartWristXY = {x: w.x, y: w.y};
    return;
  }
  if(arrivedAfterMovement) return;
  const w = activeControlPoint(lm);
  if(!w) return;
  // Required movement scales with shoulder-width to be size-invariant
  const requiredMove = (isLowerTask() || isBalanceTask())
    ? Math.max(0.035, shoulderWidth(lm) * 0.22)
    : Math.max(0.035, shoulderWidth(lm) * 0.30);
  if(distXY(w, stepStartWristXY) >= requiredMove) arrivedAfterMovement = true;
}

function movementGateRequired(step){
  if(!step || step.movement_required === false || isHandTask()) return false;
  if(testingReachEnabled() || handToMouthTaskEnabled())return false; // One shared T3 contact/hold controller in both modes.
  // These endpoints cannot be occupied by the affected hand in the preceding
  // position. Contact after the voice gate is therefore sufficient proof of a
  // deliberate first approach, even if the movement began during instruction.
  return !["T1-S1", "T1-S2", "T3-S1", "T3-S2"].includes(step.id);
}

function targetAttemptGestureSnapshot(){
  return {
    open:handOpenScore,
    closed:fistClosureScore,
    pinch:pinchScore,
    palm:palmFacingScore,
  };
}

function targetAttemptPoint(lm, step){
  if(isHandTask()) return activeHandPoint();
  if(!lm || !step) return null;
  if(isForwardReachTarget(step)) return closestAffectedReachPointToTarget(lm, getEffectiveTargetXY(step));
  const which = step.target.landmark;
  if(which === "MOUTH"){
    return closestAffectedHandPointToTarget(lm, getEffectiveTargetXY(step));
  }
  if(isAdvancedObjectMode() && latestMarker && ["OBJECT_AT_TARGET", "OBJECT_RELEASED"].includes(which)){
    return latestMarker.object_center;
  }
  if(which === "WRISTS"){
    const target = getEffectiveTargetXY(step);
    const left = mirrorX(lm[15]);
    const right = mirrorX(lm[16]);
    return distXY(left, target) <= distXY(right, target) ? left : right;
  }
  return activeControlPoint(lm);
}

function updateTargetAttemptTracking(lm){
  const step = getCurrentStep();
  if(!step) return;
  const point = targetAttemptPoint(lm, step);
  const gesture = targetAttemptGestureSnapshot();
  if(!targetAttemptStartGesture) targetAttemptStartGesture = gesture;
  if(point && !targetAttemptStartPoint) targetAttemptStartPoint = {x:point.x, y:point.y};
  if(point && targetAttemptStartPoint){
    targetAttemptMaxDisplacement = Math.max(
      targetAttemptMaxDisplacement,
      distXY(point, targetAttemptStartPoint),
    );
  }
  if(targetAttemptStartGesture){
    targetAttemptMaxGestureChange = Math.max(
      targetAttemptMaxGestureChange,
      Math.abs(gesture.open - targetAttemptStartGesture.open),
      Math.abs(gesture.closed - targetAttemptStartGesture.closed),
      Math.abs(gesture.pinch - targetAttemptStartGesture.pinch),
      Math.abs(gesture.palm - targetAttemptStartGesture.palm),
    );
  }
}

function hasIntentionalTargetAttempt(lm, step, radius){
  const movementThreshold = isHandTask()
    ? Math.max(0.035, radius * 0.28)
    : Math.max(0.05, shoulderWidth(lm) * 0.30);
  const gestureAttempt = isHandTask() && targetAttemptMaxGestureChange >= 0.12;
  const movementGateAttempt = !isHandTask() && arrivedAfterMovement;
  return movementGateAttempt || targetAttemptMaxDisplacement >= movementThreshold || gestureAttempt;
}

function nearMissCorrection(step, distance, radius, lm){
  const which = step.target.landmark;
  if(distance > radius){
    const movingObject = ["OBJECT_COUPLED", "OBJECT_AT_TARGET", "OBJECT_RELEASED"].includes(which);
    return {
      reason:"just_outside_circle",
      guidance:movingObject
        ? "Move the cup slightly farther toward the center of the circle, then keep it steady there."
        : "Move your hand slightly farther toward the center of the circle, then keep it still there.",
    };
  }
  if(movementGateRequired(step) && !arrivedAfterMovement){
    return {
      reason:"movement_gate_not_met",
      guidance:"Move your hand slightly away, then deliberately reach back into the center of the circle and hold it there.",
    };
  }
  const needsVisibleHand = ["HAND_OPEN", "HAND_CLOSED", "PINCH", "PINCH_RELEASED", "OBJECT_COUPLED", "OBJECT_RELEASED"].includes(which)
    || step.id === "H1-S1"
    || step.id === "H2-S2"
    || (Array.isArray(step.measure) && step.measure.includes("closure_completeness"));
  if(needsVisibleHand && handLandmarker && !latestHandLandmarks){
    return {
      reason:"hand_landmarks_not_visible",
      guidance:"Keep your hand inside the circle with your palm and fingers clearly facing the camera, without the cup covering your whole hand.",
    };
  }
  if(step.id === "H1-S1" && palmFacingScore <= PALM_FACING_THRESHOLD){
    return {reason:"palm_not_facing", guidance:"Keep your hand in the circle and turn your palm toward the camera."};
  }
  if(step.id === "H2-S2" || (Array.isArray(step.measure) && step.measure.includes("closure_completeness")) || which === "HAND_CLOSED"){
    return {reason:"hand_not_closed", guidance:"Keep your hand in the center of the circle and gently close your fingers around the imaginary object."};
  }
  if(which === "HAND_OPEN"){
    if(step.id === "H1-S2" && palmFacingScore <= PALM_FACING_THRESHOLD){
      return {reason:"palm_not_facing", guidance:"Keep your hand in the circle, turn your palm toward the camera, and spread your fingers."};
    }
    return {reason:"hand_not_open", guidance:"Keep your hand in the center of the circle and spread your fingers as comfortably as you can."};
  }
  if(which === "PINCH"){
    return {reason:"pinch_not_detected", guidance:"Keep your hand in the circle and bring the tip of your thumb to the tip of your index finger."};
  }
  if(which === "PINCH_RELEASED"){
    return {reason:"pinch_not_released", guidance:"Keep your hand in the circle and gently separate your thumb and index finger."};
  }
  if(which === "WRISTS"){
    const target = getEffectiveTargetXY(step);
    const radiusNow = effectiveRadius(step, lm);
    const leftInside = lm && distXY(mirrorX(lm[15]), target) < radiusNow;
    const rightInside = lm && distXY(mirrorX(lm[16]), target) < radiusNow;
    if(!leftInside || !rightInside){
      return {reason:"both_wrists_required", guidance:"Bring both hands into the circle together and hold them there."};
    }
  }
  if(which === "WRISTS_APART"){
    return {reason:"hands_not_apart", guidance:"Keep both hands at the target height and move them a little farther apart."};
  }
  if(which === "WRISTS_LOW"){
    return {reason:"hands_not_lowered", guidance:"Lower both hands fully toward the ending area and keep them there."};
  }
  if(which === "OBJECT_COUPLED"){
    return {reason:"cup_grasp_not_detected", guidance:"Keep your hand and cup together in the center of the circle, with your fingers wrapped securely around the cup."};
  }
  if(which === "OBJECT_AT_TARGET"){
    return {reason:"cup_not_centered", guidance:"Move the cup itself, not only your wrist, into the center of the target circle and hold it steady."};
  }
  if(which === "OBJECT_RELEASED"){
    return {reason:"cup_release_not_detected", guidance:"Keep the cup at the target, open your fingers, and move your hand slightly away from the cup."};
  }
  return {
    reason:"activation_condition_not_met",
    guidance:"Keep the instructed body part in the center of the circle and hold it still until the ring completes.",
  };
}

function qualifiesAsNearTargetAttempt(distance, radius, intentional){
  const nearLimit = Math.max(radius * 1.45, radius + 0.035);
  return !!intentional && Number.isFinite(distance) && distance <= nearLimit;
}

function getTargetNearMiss(lm){
  const step = getCurrentStep();
  if(!step || isLowerTask() || isBalanceTask()) return null;
  if(isLapTarget(step) && !lapTargetCalibration.ready) return null;
  if(voiceFinishedAt === 0 || performance.now() - voiceFinishedAt < 350) return null;
  const point = targetAttemptPoint(lm, step);
  if(!point) return null;
  const target = getEffectiveTargetXY(step);
  if(!target) return null;
  const radius = effectiveRadius(step, isHandTask() ? null : lm);
  const distance = isForwardReachTarget(step) ? forwardReachDistance(point, target) : distXY(point, target);
  const intentional = hasIntentionalTargetAttempt(lm, step, radius);
  if(!qualifiesAsNearTargetAttempt(distance, radius, intentional)) return null;
  return {
    ...nearMissCorrection(step, distance, radius, lm),
    task_id:tasks[currentTaskIdx].id,
    step_id:step.id,
    distance:+distance.toFixed(4),
    radius:+radius.toFixed(4),
    distance_ratio:+(distance / Math.max(radius, 0.001)).toFixed(2),
  };
}

async function speakTargetNearMiss(diagnostic){
  const step = getCurrentStep();
  if(!step || correctionVoicePlaying) return;
  const expectedStepId = step.id;
  correctionVoicePlaying = true;
  const correction = testingMouthEnabled() ? MOUTH_TEST_VOICE.near[diagnostic.reason==="movement_gate_not_met"?1:diagnostic.reason==="just_outside_circle"?0:2] : `You're close. ${diagnostic.guidance}`;
  captionEl.textContent = correction;
  postRN({type:"target_near_miss", ...diagnostic});
  try{
    await playVoice(correction,"general");
  }finally{
    const current = getCurrentStep();
    if(current && current.id === expectedStepId){
      captionEl.textContent = current.caption;
      voiceFinishedAt = performance.now();
    }
    correctionVoicePlaying = false;
  }
}

function handleTargetNearMiss(lm, now){
  if(correctionVoicePlaying || nearMissCoachingCount >= NEAR_MISS_MAX_COACHING_PER_STEP) return;
  const diagnostic = getTargetNearMiss(lm);
  if(!diagnostic){
    nearMissStartedAt = null;
    nearMissReason = "";
    return;
  }
  if(nearMissStartedAt == null || nearMissReason !== diagnostic.reason){
    nearMissStartedAt = now;
    nearMissReason = diagnostic.reason;
    return;
  }
  if(now - nearMissStartedAt < NEAR_MISS_DWELL_MS) return;
  if(lastNearMissCoachingAt && now - lastNearMissCoachingAt < NEAR_MISS_COOLDOWN_MS) return;
  nearMissCoachingCount += 1;
  lastNearMissCoachingAt = now;
  nearMissStartedAt = null;
  nearMissEvents.push({
    reason:diagnostic.reason,
    distance_ratio:diagnostic.distance_ratio,
    timestamp_ms:Math.round(now - stepStartedAt),
  });
  speakTargetNearMiss(diagnostic);
}

function rangeOf(values){
  if(!values || values.length === 0) return 0;
  return Math.max(...values) - Math.min(...values);
}

function meanOf(values){
  if(!values || values.length === 0) return 0;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function lowerBalanceMetricSnapshot(lm, step=null, durationMs=0, skipped=false){
  if(!lm || lm.length < 33) return {};
  const state = bodyState(lm);
  const leftX = Math.min(state.affected.ankle.x, state.unaffected.ankle.x);
  const rightX = Math.max(state.affected.ankle.x, state.unaffected.ankle.x);
  const baseWidth = Math.max(0.03, rightX - leftX);
  const rightShare = Math.max(0, Math.min(1, (state.midHip.x - leftX) / baseWidth));
  const affectedShare = AFFECTED_SIDE === "right" ? rightShare : 1 - rightShare;
  const scale = Math.max(0.03, meanOf(bodyMetricSamples.map(sample => sample.shoulderWidth)) || state.shoulderWidth);
  const trunkSway = Math.hypot(
    rangeOf(bodyMetricSamples.map(sample => sample.midShoulderX)),
    rangeOf(bodyMetricSamples.map(sample => sample.midShoulderY)),
  ) / scale;
  const pelvisSway = Math.hypot(
    rangeOf(bodyMetricSamples.map(sample => sample.midHipX)),
    rangeOf(bodyMetricSamples.map(sample => sample.midHipY)),
  ) / scale;
  const kneeStability = rangeOf(bodyMetricSamples.map(sample => sample.kneeAngle));
  const averageAffectedLoad = bodyMetricSamples.length
    ? meanOf(bodyMetricSamples.map(sample => sample.affectedLoadShare))
    : affectedShare;
  const weightSymmetry = Math.max(0, 1 - 2 * Math.abs(averageAffectedLoad - 0.5));
  const measures = step && Array.isArray(step.measure) ? step.measure : [];
  const bilateralMoveMax = Math.max(affectedWristMoveMaxRatio, unaffectedWristMoveMaxRatio);
  const bilateralSymmetry = bilateralMoveMax >= 0.10
    ? Math.min(affectedWristMoveMaxRatio, unaffectedWristMoveMaxRatio) / bilateralMoveMax
    : null;
  return {
    knee_extension_deg: +kneeExtensionMaxDeg.toFixed(1),
    ankle_dorsiflexion_proxy: +ankleDorsiflexionMax.toFixed(3),
    shoulder_elevation_deg: +shoulderElevationMaxDeg.toFixed(1),
    elbow_flexion_deg: +elbowFlexionMaxDeg.toFixed(1),
    hip_flexion_deg: +hipFlexionMaxDeg.toFixed(1),
    knee_flexion_deg: +kneeFlexionMaxDeg.toFixed(1),
    toe_clearance_leg_ratio: +toeClearanceMaxRatio.toFixed(3),
    circumduction_leg_ratio: +circumductionMaxRatio.toFixed(3),
    affected_step_length_leg_ratio: +affectedStepLengthMaxRatio.toFixed(3),
    affected_wrist_displacement_body_ratio: +affectedWristMoveMaxRatio.toFixed(3),
    unaffected_wrist_displacement_body_ratio: +unaffectedWristMoveMaxRatio.toFixed(3),
    hand_to_mouth_distance_ratio: Number.isFinite(handToMouthMinRatio) ? +handToMouthMinRatio.toFixed(3) : null,
    bilateral_wrist_displacement_symmetry: bilateralSymmetry == null ? null : +bilateralSymmetry.toFixed(3),
    affected_load_proxy: +averageAffectedLoad.toFixed(3),
    weight_shift_symmetry: +weightSymmetry.toFixed(3),
    pelvis_shift_affected: +affectedPelvisShiftMax.toFixed(3),
    lateral_trunk_shift: +lateralTrunkShiftMax.toFixed(3),
    trunk_sway_ratio: +trunkSway.toFixed(3),
    pelvis_sway_ratio: +pelvisSway.toFixed(3),
    midline_alignment_deg: +meanOf(bodyMetricSamples.map(sample => sample.trunkLean)).toFixed(1),
    knee_stability_deg: +kneeStability.toFixed(1),
    hold_duration_ms: skipped ? 0 : (step && step.movement_required === false ? step.hold_ms : 0),
    sit_to_stand_time_ms: step && measures.includes("sit_to_stand_time") && !skipped ? Math.round(durationMs) : null,
    gait_pelvis_travel_leg_ratio: +gaitPelvisTravelMaxRatio.toFixed(3),
    gait_affected_ankle_travel_leg_ratio: +gaitAffectedAnkleTravelMaxRatio.toFixed(3),
    gait_unaffected_ankle_travel_leg_ratio: +gaitUnaffectedAnkleTravelMaxRatio.toFixed(3),
    gait_step_alternation_count: gaitAlternationCount,
    gait_full_body_visibility_ratio: +gaitFullBodyVisibilityRatio().toFixed(3),
  };
}

function handMetricSnapshot(){
  return {
    finger_total_flexion_deg: +fingerTotalFlexionMaxDeg.toFixed(1),
    finger_abduction_ratio: +fingerAbductionMaxRatio.toFixed(3),
    thumb_index_distance_ratio: Number.isFinite(thumbIndexMinDistanceRatio)
      ? +thumbIndexMinDistanceRatio.toFixed(3)
      : null,
  };
}

function checkLowerBalanceTarget(landmarks, step){
  if(!landmarks || landmarks.length < 33) return false;
  if(!arrivedAfterMovement && step.movement_required !== false) return false;
  const state = bodyState(landmarks);
  const start = stepStartBodyState || state;
  const which = step.target.landmark;
  const affectedFootMove = Math.hypot(state.affected.ankle.x-start.affected.ankle.x, state.affected.ankle.y-start.affected.ankle.y);
  const unaffectedFootMove = Math.hypot(state.unaffected.ankle.x-start.unaffected.ankle.x, state.unaffected.ankle.y-start.unaffected.ankle.y);
  const affectedDirection = AFFECTED_SIDE === "left" ? -1 : 1;
  const pelvisTowardAffected = affectedDirection * (state.midHip.x - start.midHip.x);
  const trunkTowardAffected = affectedDirection * (state.midShoulder.x - start.midShoulder.x);
  const standing = state.affectedKneeAngle > 145 && state.unaffectedKneeAngle > 145 && state.trunkLean < 20;

  if(which === "SEATED_READY") return state.trunkLean < 20 && state.midHip.y < Math.max(state.affected.knee.y, state.unaffected.knee.y);
  if(which === "KNEE_EXTENDED" || which === "KNEE_EXTENDED_STABLE") return state.affectedKneeAngle > 145;
  if(which === "FOOT_RETURNED") return Math.abs(state.affected.ankle.y - state.affected.toe.y) < 0.10 && state.affectedKneeAngle < 145;
  if(which === "TOES_LIFTED") return (state.affected.heel.y - state.affected.toe.y) > 0.012;
  if(which === "SIT_TO_STAND_READY") return state.trunkLean < 35 && state.affectedKneeAngle < 145;
  if(which === "HIP_RISEN") return (start.midHip.y - state.midHip.y) > 0.07 && state.affectedKneeAngle > 110;
  if(which === "STAND_UPRIGHT") return standing;
  if(which === "HIP_SEATED") return (state.midHip.y - start.midHip.y) > 0.07 && state.affectedKneeAngle < 145;
  if(which === "SUPPORTED_STAND_STABLE") return standing && lateralTrunkShiftMax < 0.06;
  if(which === "AFFECTED_FOOT_FORWARD") return standing && affectedFootMove > 0.05;
  if(which === "AFFECTED_FOOT_RETURNED") return affectedFootMove > 0.045 && state.footSeparation < 0.16;
  if(which === "AFFECTED_KNEE_LIFTED") return (start.affected.knee.y - state.affected.knee.y) > 0.04;
  if(which === "UNAFFECTED_KNEE_LIFTED") return (start.unaffected.knee.y - state.unaffected.knee.y) > 0.04;
  if(which === "SEATED_STABLE") return state.trunkLean < 14 && lateralTrunkShiftMax < 0.05;
  if(which === "TRUNK_SHIFT_AFFECTED") return trunkTowardAffected > 0.025 && affectedFootMove < 0.04 && unaffectedFootMove < 0.04;
  if(which === "WEIGHT_SHIFT_AFFECTED") return pelvisTowardAffected > 0.02 && affectedFootMove < 0.04 && unaffectedFootMove < 0.04;
  if(which === "STEP_STANCE_STABLE") return standing && state.footSeparation > 0.06 && lateralTrunkShiftMax < 0.06;
  if(which === "WALK_READY") return standing && fullBodyVisibleForWalking(landmarks);
  if(which === "WALK_ACROSS"){
    const bilateralLegMotion = gaitAffectedAnkleTravelMaxRatio > 0.16
      && gaitUnaffectedAnkleTravelMaxRatio > 0.16
      && gaitAlternationCount >= 2;
    const fixedCameraProgress = gaitPelvisTravelMaxRatio > 0.35;
    const caregiverTrackedProgress = gaitAlternationCount >= 3;
    return gaitFullBodyVisibilityRatio() >= 0.75
      && bilateralLegMotion
      && (fixedCameraProgress || caregiverTrackedProgress);
  }
  if(which === "WALK_STOPPED") return standing && fullBodyVisibleForWalking(landmarks) && pelvisSwayForStop() < 0.08;
  return false;
}

function pelvisSwayForStop(){
  const values = bodyMetricSamples.slice(-20).map(sample => sample.midHipX);
  return rangeOf(values);
}

function checkTarget(landmarks){
  const step = getCurrentStep();
  if(!step) return false;
  const measures = Array.isArray(step.measure) ? step.measure : [];
  // VOICE GATE — target is NOT detectable until the voice intro has finished
  // playing AND a short settle window (350ms) has elapsed. Prevents the patient
  // accidentally triggering the step while Aria is still explaining it.
  if(voiceFinishedAt === 0) return false;
  if(performance.now() - voiceFinishedAt < 350) return false;
  if(testingReachEnabled()){
    if(isLapTarget(step) && !lapTargetCalibration.ready)return false;
    return RehynReachTarget.contains(testingReachWristContact(landmarks,step).distance,effectiveRadius(step,landmarks));
  }
  if(handToMouthTaskEnabled()){
    if(isLapTarget(step) && !lapTargetCalibration.ready)return false;
    if(isMouthTarget(step) && !mouthTargetCalibration.locked)return false;
    return RehynReachTarget.contains(testingMouthContact(landmarks,step).distance,effectiveRadius(step,landmarks));
  }
  if(isLapTarget(step)){
    if(!lapTargetCalibration.ready || !landmarks || !arrivedAfterMovement) return false;
    const affectedWristRaw = sideLandmarks(landmarks, AFFECTED_SIDE).wrist;
    if(!landmarkIsUsable(affectedWristRaw)) return false;
    const targetXY = getEffectiveTargetXY(step);
    return (testingMouthEnabled() ? mouthScreenDistance(affectedWristRaw,targetXY) : distXY(affectedWristRaw, targetXY)) < effectiveRadius(step, landmarks);
  }
  if(isHandTask()){
    const target = step.target;
    const which = target.landmark;
    const point = activeHandPoint();
    if(!point) return false;
    const R = effectiveRadius(step, null);
    const near = Math.hypot(point.x - target.x, point.y - target.y) < R;
    if(step.id === "H1-S1"){
      // Preparation checks position and palm visibility, not finger closure.
      // Opening during the instruction must not force a close-and-reopen cycle.
      // H1-S2 can measure the same open hand after its instruction finishes.
      return near && palmFacingScore > PALM_FACING_THRESHOLD;
    }
    if(step.id === "H2-S2"){
      if(!near) return false;
      fistClosureMinScore = Math.min(fistClosureMinScore, fistClosureScore);
      if(!fistCloseReadyObserved){
        if(fistClosureScore < 0.86 || handOpenScore > 0.20){
          fistCloseReadyObserved = true;
          fistClosureMinScore = Math.min(fistClosureMinScore, fistClosureScore);
        }
        return false;
      }
      const closureDelta = fistClosureScore - fistClosureMinScore;
      if(!fistClosingStarted && (closureDelta > 0.035 || (handOpenScore < 0.58 && fistClosureScore > fistClosureMinScore + 0.02))){
        fistClosingStarted = true;
      }
      return fistClosingStarted && (closureDelta > 0.02 || fistClosureScore > 0.30 || handOpenScore < 0.62);
    }
    if(measures.includes("closure_completeness")){
      return near && fistClosureScore > 0.45;
    }
    if(which === "HAND_OPEN"){
      const palmOk = step.id !== "H1-S2" || palmFacingScore > PALM_FACING_THRESHOLD;
      const openThreshold = 0.45;
      return near && palmOk && handOpenScore > openThreshold;
    }
    if(which === "PINCH"){
      return near && pinchScore > 0.45;
    }
    return near;
  }
  if(isLowerTask() || isBalanceTask()){
    return checkLowerBalanceTarget(landmarks, step);
  }
  if(!landmarks) return false;
  // MOVEMENT GATE — wrist must have moved meaningfully from its position at
  // step-start before the target can fire. Stops "I didn't move and the next
  // circle counted because I was already inside it" bug.
  if(movementGateRequired(step) && !arrivedAfterMovement) return false;

  const target = step.target;
  const which = target.landmark;
  const targetXY = getEffectiveTargetXY(step);
  if(!targetXY) return false;
  const R = effectiveRadius(step, landmarks);

  const wristDist = () => {
    const task = tasks[currentTaskIdx];
    if(task && task.id === "T1"){
      return forwardReachDistance(closestAffectedReachPointToTarget(landmarks, targetXY), targetXY);
    }
    const affectedWrist = sideLandmarks(landmarks, AFFECTED_SIDE).wrist;
    return testingMouthEnabled() ? mouthScreenDistance(affectedWrist ? mirrorX(affectedWrist) : null,targetXY) : distXY(affectedWrist ? mirrorX(affectedWrist) : null, targetXY);
  };

  if(which === "HAND_OPEN"){
    const near = wristDist() < R;
    const gestureOk = handLandmarker ? handOpenScore > 0.45 : true;
    return near && gestureOk;
  }
  if(which === "HAND_CLOSED"){
    const near = wristDist() < R;
    const gestureOk = handLandmarker ? (fistClosureScore > 0.30 && handOpenScore < 0.62) : true;
    return near && gestureOk;
  }
  if(which === "PINCH"){
    const near = wristDist() < R;
    const gestureOk = handLandmarker ? pinchScore > 0.45 : true;
    return near && gestureOk;
  }
  if(which === "PINCH_RELEASED"){
    const near = wristDist() < R;
    const gestureOk = handLandmarker ? pinchScore < 0.35 : true;
    return near && gestureOk;
  }
  if(which === "MOUTH"){
    return mouthTargetCalibration.locked && mouthContactDistance(landmarks, targetXY) < R;
  }
  if(which === "CHEST"){
    return wristDist() < R;
  }
  if(which === "WRIST_DYNAMIC"){
    return wristDist() < R;
  }
  if(which === "WRISTS"){
    const lW = mirrorX(landmarks[15]);
    const rW = mirrorX(landmarks[16]);
    const okL = lW && Math.hypot(lW.x - targetXY.x, lW.y - targetXY.y) < R;
    const okR = rW && Math.hypot(rW.x - targetXY.x, rW.y - targetXY.y) < R;
    return okL && okR;
  }
  if(which === "WRISTS_APART"){
    const lW = mirrorX(landmarks[15]);
    const rW = mirrorX(landmarks[16]);
    const sameHeight = Math.abs(lW.y - rW.y) < 0.14;
    const workingHeight = Math.abs(((lW.y + rW.y) / 2) - targetXY.y) < Math.max(R, 0.14);
    return sameHeight && workingHeight && Math.abs(lW.x - rW.x) > 0.28;
  }
  if(which === "WRISTS_LOW"){
    const lW = mirrorX(landmarks[15]);
    const rW = mirrorX(landmarks[16]);
    return lW.y > 0.68 && rW.y > 0.68;
  }
  if(which === "OBJECT_COUPLED"){
    const near = wristDist() < R;
    if(!isAdvancedObjectMode()){
      return near && (!handLandmarker || (fistClosureScore > 0.25 && handOpenScore < 0.70));
    }
    const handCenter = getHandCenter();
    const coupled = latestMarker && handCenter
      && Math.hypot(handCenter.x - latestMarker.object_center.x, handCenter.y - latestMarker.object_center.y) < 0.22;
    return near && !!coupled && fistClosureScore > 0.20;
  }
  if(which === "OBJECT_AT_TARGET"){
    if(!isAdvancedObjectMode()) return wristDist() < R;
    return !!latestMarker
      && Math.hypot(latestMarker.object_center.x - targetXY.x, latestMarker.object_center.y - targetXY.y) < R;
  }
  if(which === "OBJECT_RELEASED"){
    if(!isAdvancedObjectMode()) return wristDist() < R && (!handLandmarker || handOpenScore > 0.45);
    const handCenter = getHandCenter();
    const objectAtTarget = latestMarker
      && Math.hypot(latestMarker.object_center.x - targetXY.x, latestMarker.object_center.y - targetXY.y) < R;
    const separated = latestMarker && handCenter
      && Math.hypot(handCenter.x - latestMarker.object_center.x, handCenter.y - latestMarker.object_center.y) > 0.24;
    return !!objectAtTarget && !!separated && handOpenScore > 0.45;
  }
  return wristDist() < R;
}

// ---------- Drawing ----------
const ICON_EMOJI = { cup: "☕", table: "🪵", towel: "🧺", ball: "🏐", coin: "🪙" };
let lastAngleVideoTime = -1, lastAngleVideoAt = 0;

function drawTestingReachAngles(landmarks, world=latestPoseWorldLandmarks){
  const panel = document.getElementById("testingReachAngles");
  const enabled = LIBRARY_TEST_MODE && tasks[currentTaskIdx]?.id === "T1";
  panel.classList.toggle("hidden", !enabled);
  if(!enabled) return null;
  const aspect = video.videoWidth > 0 && video.videoHeight > 0 ? video.videoWidth/video.videoHeight : 1;
  const readout = RehynReachAngles.assessmentReadout(assessmentQuality, landmarks, world, aspect);
  const format = v => Number.isFinite(v) ? `${v.toFixed(1)}°` : "—";
  document.getElementById("liveArmElevationScore").textContent = format(readout.armScore);
  document.getElementById("liveArmElevationArc").textContent = format(readout.armElevationArc?.degrees);
  document.getElementById("liveElbowExtension").textContent = format(readout.elbowScore);
  // Use the same shared comparison detector as Tracker.sample(), rather than
  // deriving display-only shoulder/face values from a different formula.
  const trunk=assessmentQuality.trunkLeanReadout(landmarks,aspect);
  const shoulder=trunk.cues?.pelvisNormalizedShoulderScale;
  const face=trunk.cues?.pelvisNormalizedFaceScale;
  const runs=assessmentQuality.compensations?.trunk_lean?.cue_runs;
  const shoulderConfirmed=(runs?.shoulder?.confirmed_ms||0)>=500;
  const faceConfirmed=(runs?.face?.confirmed_ms||0)>=500;
  document.getElementById("liveTrunkLeanShoulder").textContent=format(shoulder);
  document.getElementById("liveTrunkLeanFace").textContent=format(face);
  document.getElementById("liveTrunkLeanFaceScale").textContent=Number.isFinite(trunk.faceScale) ? `${trunk.faceScale.toFixed(4)}×` : "—";
  const cueState=(value,above,confirmed,duration)=>!assessmentQuality.trunkLeanBaseline
    ? "Waiting for upright calibration"
    : !Number.isFinite(value) ? "Not measurable in this frame"
    : confirmed ? `Confirmed for this step (${(duration/1000).toFixed(2)} s)`
    : above ? "Above threshold now · hold for 0.5 s" : "Below threshold";
  document.getElementById("liveTrunkLeanShoulderState").textContent=cueState(shoulder,Number.isFinite(shoulder)&&shoulder>12,shoulderConfirmed,runs?.shoulder?.confirmed_ms||0);
  document.getElementById("liveTrunkLeanFaceState").textContent=cueState(face,Number.isFinite(face)&&face+1e-9>=7,faceConfirmed,runs?.face?.confirmed_ms||0);
  document.getElementById("liveTrunkLeanState").textContent=trunk.supported
    ? shoulderConfirmed || faceConfirmed ? "Trunk lean counted in this step" : trunk.detected ? "Cue above threshold now; hold for 0.5 s" : "Neither cue is above its threshold"
    : assessmentQuality.trunkLeanBaseline ? "Torso landmarks unavailable" : "Waiting for upright calibration";
  // Both video and canvas are CSS-mirrored. Counter-flip this screen-space
  // overlay once so arc positions align and degree labels remain readable.
  ctx.save(); ctx.translate(canvas.width,0); ctx.scale(-1,1);
  RehynReachAngles.drawAngleArcs(ctx,readout,canvas.width,canvas.height,canvas.clientWidth);
  ctx.restore();
  return readout;
}

function drawTestingAssessmentAngles(landmarks, world=latestPoseWorldLandmarks){
  const panel=document.getElementById("testingAssessmentAngles");
  const taskId=tasks[currentTaskIdx]?.id;
  const enabled=LIBRARY_TEST_MODE && RehynTestingAssessmentDiagnostics.TASKS.has(taskId);
  panel.classList.toggle("hidden",!enabled);
  if(!enabled) return;
  const handFresh=latestHandLandmarks && performance.now()-latestHandSeenAt<=150;
  const sampleFresh=assessmentQuality.lastTime!==null && performance.now()-assessmentQuality.lastTime<=250;
  const sample=sampleFresh ? assessmentQuality.currentMetrics : null;
  const hand=handFresh ? latestHandLandmarks : null;
  const rubric=assessmentQuality.rubric;
  const result=RehynTestingAssessmentDiagnostics.readout(taskId,rubric,sample,hand);
  const format=(value,unit="deg")=>Number.isFinite(value)
    ? unit==="ratio" ? `${(value*100).toFixed(1)}%` : `${value.toFixed(1)}${unit==="deg"?"°":""}` : "not measured";
  document.getElementById("testingAssessmentTitle").textContent=tasks[currentTaskIdx]?.title || "Live measurements";
  document.getElementById("testingAssessmentStep").textContent=`Current step: ${getCurrentStep()?.caption || "waiting to start"}`;
  document.getElementById("testingAssessmentCriteria").textContent=result.criteria.length
    ? result.criteria.map(row=>`${row.label}: ${format(row.value,row.unit)} / ${format(row.target,row.unit)} reference`).join("\n")
    : "Waiting for step measurements";
  let angles="";
  let note="";
  if(taskId==="T2"){
    const aspect=video.videoWidth>0 && video.videoHeight>0 ? video.videoWidth/video.videoHeight : 1;
    const poseReadout=RehynReachAngles.assessmentReadout(assessmentQuality,landmarks,world,aspect);
    angles=`Arm elevation (model 3D): ${format(result.armElevation)}\nElbow extension (2D): ${format(result.elbowExtension)}\nCyan arm arc (2D guide): ${format(poseReadout.armElevationArc?.degrees)}`;
    note="Arm elevation is scored from model world landmarks; its cyan 2D arc is only a visual guide. Elbow extension uses the yellow 2D arc. Angles are camera estimates.";
    ctx.save();ctx.translate(canvas.width,0);ctx.scale(-1,1);
    RehynReachAngles.drawAngleArcs(ctx,poseReadout,canvas.width,canvas.height,canvas.clientWidth);
    ctx.restore();
  }else if(taskId==="H3"){
    const index=result.hand?.fingers[0];
    const thumbMcp=hand ? RehynTestingAssessmentDiagnostics.angle3D(hand[1],hand[2],hand[3]) : null;
    const thumb=hand ? RehynTestingAssessmentDiagnostics.angle3D(hand[2],hand[3],hand[4]) : null;
    angles=`Pinch confidence: ${format(result.pinch,"ratio")}\nThumb-index gap / palm width: ${Number.isFinite(result.hand?.pinchGap)?result.hand.pinchGap.toFixed(2):"not measured"}\nThumb MCP: ${format(thumbMcp)} · Thumb IP: ${format(thumb)}\nIndex PIP: ${format(index?.pip)} · Index DIP: ${format(index?.dip)}`;
    note="The pinch score uses smoothed thumb-index distance relative to palm width; finger angles are context only and do not directly earn points. Pink line joins the fingertips.";
  }else{
    angles=`Hand opening: ${format(result.handOpen,"ratio")} · Hand closure: ${format(result.handClosed,"ratio")}${taskId==="H4"?`\nClose-then-reopen cycle: ${result.cycle===1?"observed":result.cycle===0?"not yet observed":"not measured"}`:""}`;
    angles+=`\nFinger angles (PIP / DIP):\n${result.hand ? result.hand.fingers.map(f=>`${f.name}: ${format(f.pip)} / ${format(f.dip)}`).join("\n") : "not measured"}`;
    note="Finger angles contribute to the hand-opening estimate, along with finger reach and spread. Cyan/yellow arcs mark PIP/DIP joints. These angles are not separately scored. The close-then-reopen cycle requires closure ≥65% followed by opening ≥70%.";
  }
  document.getElementById("testingAssessmentAnglesText").textContent=angles;
  document.getElementById("testingAssessmentNote").textContent=note;
  document.getElementById("testingAssessmentPosture").textContent=result.posture.map(row=>{
    const config=window.REHYN_ASSESSMENT_RUBRIC.compensations[row.id];
    const check=sample ? assessmentQuality.compensations[row.id] : null;
    const state=!Number.isFinite(row.value)?"not measured":check?.active?"sustained 0.5 s":row.value>config.threshold?"above threshold; not yet sustained":"below threshold";
    return `${config.label}: ${format(row.value)} / >${config.threshold}° · ${state}`;
  }).join("\n");
  RehynTestingAssessmentDiagnostics.drawHandGuides(ctx,hand,canvas.width,canvas.height,taskId,RehynReachAngles);
}

// Rendering and hold progression consume the same contact/voice/freshness gates.
function testingTargetState(landmarks,now=performance.now()){
  const available=testingReachEnabled()
    ? reachCanAttempt() && reachContactFrameValid(landmarks,now)
    : mouthCanMeasure() && mouthContactFrameValid(landmarks,now);
  const armed=!calibratingAssessment && !stepCompleted && !correctionVoicePlaying
    && voiceFinishedAt>0 && now-voiceFinishedAt>=350 && available;
  return {armed,contact:armed && checkTarget(landmarks)};
}
// Drawing helper only: how far the tracked hand is from the target, in units of the hit
// radius (0 = centre, 1 = the ring edge). It mirrors the measures checkTarget uses without
// changing them; when a task has no single hand point it returns NaN and the ring keeps a
// steady middle weight.
function targetProximityForDrawing(step, landmarks, targetXY, R){
  try{
    if(!(R > 0)) return Infinity;
    if(isHandTask()){
      const point = activeHandPoint();
      return point ? Math.hypot(point.x - step.target.x, point.y - step.target.y) / R : Infinity;
    }
    if(isLowerTask() || isBalanceTask()) return NaN;
    if(!landmarks || !targetXY) return Infinity;
    if(isLapTarget(step)){
      const wristRaw = sideLandmarks(landmarks, AFFECTED_SIDE).wrist;
      return landmarkIsUsable(wristRaw) ? distXY(wristRaw, targetXY) / R : Infinity;
    }
    const task = tasks[currentTaskIdx];
    if(task && task.id === "T1"){
      return forwardReachDistance(closestAffectedReachPointToTarget(landmarks, targetXY), targetXY) / R;
    }
    const wrist = sideLandmarks(landmarks, AFFECTED_SIDE).wrist;
    return wrist ? distXY(mirrorX(wrist), targetXY) / R : Infinity;
  }catch(error){
    return NaN;
  }
}

function completeMovementTarget(step, landmarks, now){
  const targetXY=getEffectiveTargetXY(step);
  const point=targetXY ? targetCanvasPoint(step,targetXY) : null;
  const completed=targetCompletion={stepId:step.id,startedAt:now,point:point ? {...point} : null,
    radius:effectiveRadius(step,landmarks)};
  if(navigator.vibrate)navigator.vibrate(80);
  setTimeout(()=>{
    if(running && stepCompleted && targetCompletion===completed && getCurrentStep()===step)nextStep(false);
  },RehynReachTarget.completionDurationMs);
}

function drawOverlay(landmarks){
  ctx.clearRect(0,0,canvas.width,canvas.height);
  const armOnly = LIBRARY_TEST_MODE && ["T1","T3"].includes(tasks[currentTaskIdx]?.id);
  // draw skeleton
  if(landmarks){
    if(armOnly){
      const a = sideLandmarks(landmarks, AFFECTED_SIDE);
      const arm = [a.shoulder,a.elbow,a.wrist];
      const visible = p => p && [p.x,p.y].every(Number.isFinite) && (p.visibility ?? 0) >= .65;
      ctx.save(); ctx.strokeStyle="#80f0ba";ctx.lineWidth=4;ctx.fillStyle="#d9ffeb";
      for(let i=0;i<arm.length-1;i++){
        const p=arm[i],q=arm[i+1];if(!visible(p)||!visible(q))continue;
        ctx.beginPath();ctx.moveTo(p.x*canvas.width,p.y*canvas.height);ctx.lineTo(q.x*canvas.width,q.y*canvas.height);ctx.stroke();
      }
      for(const p of arm){if(!visible(p))continue;ctx.beginPath();ctx.arc(p.x*canvas.width,p.y*canvas.height,5,0,Math.PI*2);ctx.fill();}
      ctx.restore();
      // Pose face points support the T1 face-width cue and T3 nose/ear cue.
      const faceVisible = p => p && [p.x,p.y].every(Number.isFinite) && (p.visibility ?? 0) >= .45;
      const leftEar=landmarks[7],rightEar=landmarks[8];
      ctx.save();
      if(faceVisible(leftEar)&&faceVisible(rightEar)){
        ctx.strokeStyle="#ff6e91";ctx.lineWidth=2;
        ctx.beginPath();ctx.moveTo(leftEar.x*canvas.width,leftEar.y*canvas.height);
        ctx.lineTo(rightEar.x*canvas.width,rightEar.y*canvas.height);ctx.stroke();
      }
      for(let i=0;i<=10;i++){
        const p=landmarks[i];if(!faceVisible(p))continue;
        const ear=i===7||i===8;
        ctx.fillStyle=ear?"#ff6e91":"#ffb4c3";
        ctx.beginPath();ctx.arc(p.x*canvas.width,p.y*canvas.height,ear?5:2.5,0,Math.PI*2);ctx.fill();
      }
      ctx.restore();
    }else{
      drawingUtils.drawLandmarks(landmarks, {color:"#D9E5DC", radius:3});
      drawingUtils.drawConnectors(landmarks, PoseLandmarker.POSE_CONNECTIONS, {color:"#4A7856", lineWidth:4});
    }
  }
  if((!armOnly || testingMouthEnabled()) && latestHandLandmarks && latestHandLandmarks.length >= 21
    && performance.now()-latestHandSeenAt<=handLandmarkFreshMs()){
    drawingUtils.drawConnectors(latestHandLandmarks, HAND_CONNECTIONS, {color:"rgba(127,229,163,0.88)", lineWidth:2});
    drawingUtils.drawLandmarks(latestHandLandmarks, {color:"rgba(217,229,220,0.72)", radius:1.4});
  }
  if(!armOnly && !calibratingAssessment && !stepCompleted && voiceFinishedAt > 0) assessmentQuality.draw(ctx, landmarks, canvas.width, canvas.height);
  if(calibratingAssessment){
    lapStatus.classList.add("hidden");
    if(lapTargetCalibration.ready && lapTargetCalibration.target){
      const tx = lapTargetCalibration.target.x * canvas.width;
      const ty = lapTargetCalibration.target.y * canvas.height;
      const previewRadius = testingReachEnabled() && forwardReachPlacement?.ready
        ? (forwardReachPlacement.lapRadius || forwardReachPlacement.radius) : 0.075;
      const tr = previewRadius * Math.min(canvas.width, canvas.height);
      ctx.save();
      ctx.beginPath();
      ctx.arc(tx, ty, tr, 0, Math.PI*2);
      ctx.fillStyle = "rgba(74,120,86,.28)";
      ctx.fill();
      ctx.lineWidth = 6;
      ctx.strokeStyle = "#7FE5A3";
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(tx, ty, Math.max(5, tr*.16), 0, Math.PI*2);
      ctx.fillStyle = "#FDFDFD";
      ctx.fill();
      ctx.restore();
    }
    return;
  }
  const step = getCurrentStep();
  if(!step){
    lapStatus.classList.add("hidden");
    return;
  }
  // Finish at the exact position that was reached, then retire this circle.
  // The next target still previews transparently throughout its instruction.
  if(stepCompleted){
    if(targetCompletion?.stepId===step.id && targetCompletion.point){
      RehynReachTarget.drawTargetCompletion(ctx,{
        x:targetCompletion.point.x*canvas.width,y:targetCompletion.point.y*canvas.height,
        radius:targetCompletion.radius*Math.min(canvas.width,canvas.height),
        elapsed:performance.now()-targetCompletion.startedAt,
        reducedMotion:!!targetMotionPreference?.matches,mirrored:true});
    }
    lapStatus.classList.add("hidden");return;
  }
  if(celebrateEl.classList.contains("show")){
    lapStatus.classList.add("hidden");return;
  }
  if(isWalkingTask()){
    lapStatus.classList.add("hidden");
    return;
  }
  if(isLapTarget(step) && !lapTargetCalibration.ready){
    lapStatus.textContent = "Keep your affected hand resting on the visible part of your lap while Rehyn locates the target.";
    lapStatus.classList.remove("hidden");
    return;
  }
  if(isMouthTarget(step) && !mouthTargetCalibration.target){
    lapStatus.textContent = "Keep your face and mouth visible while Rehyn locates the mouth target.";
    lapStatus.classList.remove("hidden");
    return;
  }
  lapStatus.classList.add("hidden");
  const targetXY = getEffectiveTargetXY(step);
  if(!targetXY) return;
  const canvasTarget = targetCanvasPoint(step, targetXY);
  const tx = canvasTarget.x * canvas.width;
  const ty = canvasTarget.y * canvas.height;
  // Visual radius MATCHES the actual hit radius — so what the user sees == what triggers.
  const effR = effectiveRadius(step, landmarks);
  const tr = effR * Math.min(canvas.width, canvas.height);
  if(testingReachEnabled() || handToMouthTaskEnabled()){
    const now=performance.now(),state=testingTargetState(landmarks,now);
    RehynReachTarget.drawTestingTarget(ctx,{x:tx,y:ty,radius:tr,...state,now,
      reducedMotion:!!targetMotionPreference?.matches,
      progress:inTargetSince===null?0:Math.min(1,(now-inTargetSince)/step.hold_ms)});
    return;
  }
  const armed = (voiceFinishedAt > 0)
    && (performance.now() - voiceFinishedAt >= 350)
    && (!movementGateRequired(step) || arrivedAfterMovement);
  // Proximity glow: the ring answers the hand continuously — thin and dashed while far
  // (or not yet armed), thicker and warmer as the hand nears, and once inside it fills
  // while the hold ring below fills green. Drawing only — arming, the hit radius and the
  // hold logic are unchanged; the ring always sits at the true hit radius.
  const holdingTarget = !!inTargetSince;
  const proximity = holdingTarget ? 0 : targetProximityForDrawing(step, landmarks, targetXY, effR);
  // 0 = far away or not armed, 1 = the hand is at the ring edge.
  const closeness = !armed ? 0 : Number.isFinite(proximity) ? Math.max(0, Math.min(1, (2.2 - proximity) / 1.2)) : 0.6;
  ctx.save();
  ctx.beginPath();
  ctx.arc(tx, ty, tr, 0, Math.PI*2);
  if(holdingTarget){
    ctx.fillStyle = "rgba(225,142,109,0.4)";
    ctx.fill();
    ctx.lineWidth = 6;
    ctx.strokeStyle = "rgba(225,142,109,0.35)";
    ctx.setLineDash([]);
  } else {
    if(closeness > 0){
      ctx.fillStyle = "rgba(225,142,109," + (0.12 * closeness).toFixed(3) + ")";
      ctx.fill();
    }
    ctx.lineWidth = 3 + 3 * closeness;
    ctx.strokeStyle = "rgba(225,142,109," + (0.45 + 0.55 * closeness).toFixed(3) + ")";
    ctx.setLineDash(closeness > 0.97 ? [] : [10 + 40 * closeness, 8 * (1 - closeness) + 0.5]);
  }
  ctx.stroke();
  ctx.restore();

  RehynReachTarget.drawBreathingHalo(ctx,{x:tx,y:ty,radius:tr,now:performance.now(),
    contact:holdingTarget,muted:!armed,reducedMotion:!!targetMotionPreference?.matches});

  // icon emoji (cup / table / towel ...) — drawn unmirrored using counter-flip
  const icon = step.target.icon;
  if(icon && ICON_EMOJI[icon]){
    ctx.save();
    // canvas is CSS-mirrored via scaleX(-1); flip back so emoji reads correctly
    ctx.translate(tx, ty);
    ctx.scale(-1, 1);
    ctx.font = `${Math.round(tr*1.1)}px serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#fff";
    ctx.fillText(ICON_EMOJI[icon], 0, 0);
    ctx.restore();
  }

  // hold progress ring — fills clockwise on the target ring itself while the hand stays inside
  if(inTargetSince){
    const elapsed = performance.now() - inTargetSince;
    const progress = Math.min(1, elapsed / step.hold_ms);
    ctx.save();
    ctx.setLineDash([]);
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.arc(tx, ty, tr, -Math.PI/2, -Math.PI/2 + progress*Math.PI*2);
    ctx.strokeStyle = "#7FE5A3";
    ctx.lineWidth = 6;
    ctx.stroke();
    ctx.restore();
  }

  if(isAdvancedObjectMode() && latestMarker){
    const mx = latestMarker.object_center.x * canvas.width;
    const my = latestMarker.object_center.y * canvas.height;
    ctx.save();
    ctx.beginPath();
    ctx.arc(mx, my, 18, 0, Math.PI*2);
    ctx.lineWidth = 5;
    ctx.strokeStyle = latestMarker.object_stability > 0.45 ? "#7FE5A3" : "#E18E6D";
    ctx.stroke();
    ctx.translate(mx, my + 34);
    ctx.scale(-1, 1);
    ctx.font = "bold 16px -apple-system,sans-serif";
    ctx.textAlign = "center";
    ctx.fillStyle = "rgba(255,255,255,0.92)";
    ctx.fillText("marker", 0, 0);
    ctx.restore();
  }

  // small status badge for hand gesture requirements
  const which = step.target.landmark;
  if(which === "HAND_OPEN" || which === "PINCH" || step.id === "H1-S1"){
    const h1OpenQuality = Math.min(handOpenScore, palmFacingScore);
    const score = step.id === "H1-S1" ? palmFacingScore : (step.id === "H1-S2" ? h1OpenQuality : (which === "HAND_OPEN" ? handOpenScore : pinchScore));
    const label = step.id === "H1-S1" ? "Palm" : (step.id === "H1-S2" ? "Palm + open" : (which === "HAND_OPEN" ? "Open" : "Pinch"));
    ctx.save();
    ctx.translate(tx, ty + tr + 32);
    ctx.scale(-1, 1);
    ctx.font = "bold 18px -apple-system,sans-serif";
    ctx.textAlign = "center";
    ctx.fillStyle = score > 0.55 ? "#7FE5A3" : "rgba(255,255,255,0.85)";
    ctx.fillText(`${label}: ${Math.round(score*100)}%`, 0, 0);
    ctx.restore();
  }
}

function nextStep(skipped=false){
  const task = tasks[currentTaskIdx];
  const step = task.steps[currentStepIdx];
  if(!taskResults[currentTaskIdx]){
    taskResults[currentTaskIdx] = {
      task_id: task.id, completed_steps:0, total_steps:task.steps.length, duration_ms:0, steps:[], metrics:{}
    };
  }
  const stepDurationMs = Math.round(performance.now() - stepStartTime);
  const adaptationEvidence = finishTestingReachStep(!skipped);
  const qualityEvidence = assessmentQuality.snapshot();
  if(localReview.active && assessmentQuality.seriesStart!==null) qualityEvidence.video_start_ms=Math.round(assessmentQuality.seriesStart-localReview.active.startedAt);
  const leanEvidence = qualityEvidence.compensations.trunk_lean;
  const confirmedTrunkLean = leanEvidence?.max_streak_ms >= 500 ? leanEvidence.max_value : 0;
  taskResults[currentTaskIdx].steps.push({
    step_id: step.id,
    completed: !skipped,
    failure_code: skipped && step.failure_phenotype ? step.failure_phenotype.code : null,
    duration_ms: stepDurationMs,
    metrics: {
      quality: qualityEvidence,
      ...(adaptationEvidence ? {testing_reach:adaptationEvidence} : {}),
      trunk_lean_deg: leanEvidence?.eligible_ms >= 500 ? Math.round(confirmedTrunkLean) : null,
      shoulder_flexion_ratio: +shoulderFlexionMax.toFixed(2),
      shoulder_hike: shoulderHikeDetected,
      hand_open_score: +handOpenScore.toFixed(2),
      fist_closure_score: +fistClosureScore.toFixed(2),
      pinch_score: +pinchScore.toFixed(2),
      palm_facing_score: +palmFacingScore.toFixed(2),
      target_near_miss_count: nearMissEvents.length,
      target_near_miss_events: nearMissEvents.slice(),
      ...lowerBalanceMetricSnapshot(latestPoseLandmarks, step, stepDurationMs, skipped),
      ...handMetricSnapshot(),
      ...computeAdvancedObjectMetrics()
    }
  });
  if(!skipped) taskResults[currentTaskIdx].completed_steps += 1;
  taskResults[currentTaskIdx].duration_ms += stepDurationMs;

  // aggregate metrics
  if(confirmedTrunkLean > (taskResults[currentTaskIdx].metrics.trunk_lean_deg||0))
    taskResults[currentTaskIdx].metrics.trunk_lean_deg = Math.round(confirmedTrunkLean);
  if(shoulderHikeDetected) taskResults[currentTaskIdx].metrics.shoulder_hike = true;
  Object.assign(taskResults[currentTaskIdx].metrics, lowerBalanceMetricSnapshot(latestPoseLandmarks, step, stepDurationMs, skipped));
  Object.assign(taskResults[currentTaskIdx].metrics, handMetricSnapshot());
  if(isAdvancedMarkerTask(task)){
    taskResults[currentTaskIdx].metrics.advanced_marker_confirmed = !!advancedObjectModeByTask[task.id];
    Object.assign(taskResults[currentTaskIdx].metrics, computeAdvancedObjectMetrics());
  }

  currentStepIdx += 1;
  if(currentStepIdx >= task.steps.length){
    // Task complete — celebrate before moving on
    celebrateAndAdvance();
    return;
  }
  startStep();
}

function spawnConfetti(count){
  const colors = ["#E18E6D", "#D9E5DC", "#FDFDFD", "#4A7856", "#FFD58A"];
  for(let i = 0; i < count; i++){
    const c = document.createElement("div");
    c.className = "confetti";
    c.style.left = (Math.random() * 100) + "vw";
    c.style.background = colors[i % colors.length];
    c.style.animationDelay = (Math.random() * 0.3) + "s";
    c.style.animationDuration = (1.0 + Math.random() * 0.9) + "s";
    celebrateEl.appendChild(c);
    setTimeout(() => c.remove(), 2200);
  }
}

function renderCelebrateDots(){
  celebrateDots.innerHTML = "";
  tasks.forEach((_, i) => {
    const d = document.createElement("span");
    if(i <= currentTaskIdx) d.className = "done";
    celebrateDots.appendChild(d);
  });
}

async function celebrateAndAdvance(){
  const finishedTask = tasks[currentTaskIdx];
  const pick = CELEBRATION_LINES[currentTaskIdx % CELEBRATION_LINES.length];
  const voicePick = CELEBRATION_VOICES[currentTaskIdx % CELEBRATION_VOICES.length];

  // Show overlay
  const upcomingTask = tasks[currentTaskIdx + 1];
  celebrateLabel.textContent = `${currentTaskIdx + 1} of ${tasks.length} · ${upcomingTask ? `Next: ${upcomingTask.title || upcomingTask.id}` : "All tasks complete"}`;
  celebrateTitle.textContent = pick.title;
  celebrateMsg.textContent = pick.msg;
  renderCelebrateDots();
  celebrateEl.classList.remove("hidden");
  // give browser a tick so the transition fires
  requestAnimationFrame(() => celebrateEl.classList.add("show"));
  spawnConfetti(28);
  if(navigator.vibrate) navigator.vibrate([60, 40, 100]);

  stopAndSaveTaskRecording(finishedTask.id);
  if(testingMouthEnabled())taskResults[currentTaskIdx].metrics.assisted=mouthFlow.assisted;
  await finishLocalReview(taskResults[currentTaskIdx]);
  persistTaskProgress(finishedTask.id);

  postRN({type:"task_complete", package_id: ASSESSMENT_PACKAGE, task_id: finishedTask.id, task_index: currentTaskIdx});

  // Determine if there are more tasks
  const hasNext = (currentTaskIdx + 1) < tasks.length;

  // Play encouraging voice (only when not the final task — final has its own outro)
  let voiceMs = 0;
  const t0 = performance.now();
  if(hasNext){
    await playVoice(voicePick);
    voiceMs = performance.now() - t0;
  }

  // Ensure overlay is visible for at least ~2.4s for tactile/emotional pacing
  const minDisplayMs = 2400;
  if(voiceMs < minDisplayMs) await new Promise(r => setTimeout(r, minDisplayMs - voiceMs));

  // Hide overlay and advance
  celebrateEl.classList.remove("show");
  setTimeout(() => celebrateEl.classList.add("hidden"), 350);

  currentStepIdx = 0;
  currentTaskIdx += 1;
  if(currentTaskIdx >= tasks.length){
    finishAssessment();
    return;
  }
  startStep();
}

let completionScreen = null;
async function finishAssessment(){
  document.body.classList.remove("step-active");
  setInstructionsOpen(false);
  running = false;
  audioEl.pause();
  if(LIBRARY_TEST_MODE && !LOCAL_PREVIEW_MODE){
    if(testingMouthEnabled()){stopMouthVoice();if(taskResults[0])taskResults[0].metrics.assisted=mouthFlow.assisted;}
    if(testingReachEnabled()){
      if(taskResults[0])taskResults[0].metrics.testing_reach={support_available:reachFlow.support,stopped:reachFlow.stopped};
      stopReachVoice();
    }
    captionEl.textContent = "Task test complete";
    voiceText.textContent = "This test was not added to Assessment history or Progress.";
    const testResult=taskResults.filter(Boolean)[0] || null;
    const recording=await finishLocalReview(testResult);
    postRN({type:"library_test_complete", package_id:ASSESSMENT_PACKAGE, task_id:tasks[0] ? tasks[0].id : null, task_result:testResult, recording});
    if(video.srcObject) video.srcObject.getTracks().forEach(track=>track.stop());
    return;
  }
  // Keep dimensions before stopping the camera clears the video metadata.
  const completedProjection = {
    source_width:video.videoWidth, source_height:video.videoHeight,
    display_width:cameraFrame.clientWidth, display_height:cameraFrame.clientHeight,
    fit:CAMERA_FIT_MODE, device_class:CAMERA_DEVICE_CLASS, mirrored_for_patient:true,
  };
  if(video.srcObject) video.srcObject.getTracks().forEach(track=>track.stop());
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if(!completionScreen) completionScreen = window.RehynAssessmentCompletion.create({onExit:()=>{
    if(window.ReactNativeWebView || window.parent!==window) postRN({type:"exit"});
    else window.location.assign(LOCAL_PREVIEW_MODE ? window.location.href : "/");
  }});
  await completionScreen.run(async signal=>{
    if(pendingTaskVideoSaves.size){
      await Promise.allSettled(Array.from(pendingTaskVideoSaves));
    }
    if(pendingTaskProgressSaves.size){
      await Promise.allSettled(Array.from(pendingTaskProgressSaves));
    }
    const endpoint = LOCAL_PREVIEW_MODE ? "/assessment/preview-results?local_preview=1" : "/assessment/submit";
    const res = await fetch(`${API_BASE}${endpoint}`,{
      signal,
      method:"POST", headers:{"Content-Type":"application/json", ...(CURRENT_USER_ID ? ACCOUNT_HEADERS : {})},
      body: JSON.stringify({
        task_results: taskResults.filter(Boolean),
        affected_side: AFFECTED_SIDE,
        assessment_package: ASSESSMENT_PACKAGE,
        assigned_task_ids: tasks.map(task => task.id),
        ...(!LOCAL_PREVIEW_MODE ? {motion_data: {
          schema_version: "1.0",
          coordinate_space: {
            pose_2d: "camera_normalized_unmirrored",
            pose_world_3d: "mediapipe_estimated_world_landmarks",
            hand_2d: "camera_normalized_unmirrored",
          },
          camera_projection: completedProjection,
          sample_interval_ms: MOTION_SAMPLE_INTERVAL_MS,
          truncated: motionFrames.length >= MAX_MOTION_FRAMES,
          frames: motionFrames,
        }} : {}),
      })
    });
    if(!res.ok){
      const detail = await res.text();
      throw new Error(`Assessment save failed (${res.status}): ${detail.slice(0, 160)}`);
    }
    const data = await res.json();
    if(!data || (LOCAL_PREVIEW_MODE ? !Array.isArray(data.metrics?.task_quality?.tasks) : !data.id)){
      throw new Error("Assessment response did not include results");
    }
    return data;
  },data=>{
    postRN(LOCAL_PREVIEW_MODE
      ? {type:"assessment_preview_complete", package_id:ASSESSMENT_PACKAGE, completed_tasks:taskResults.filter(Boolean).length, results_in_runner:true}
      : {type:"assessment_complete", assessment:data, results_in_runner:true});
  });
}

let lastTestingReachPoseVideoTime = null;
function loop(){
  if(!running) return;
  const now = performance.now();
  const cameraFrameReady = !walkingCameraSwitching
    && video.readyState >= 2
    && video.videoWidth > 0
    && video.videoHeight > 0;
  let landmarks = latestPoseLandmarks;
  const handBackoff = isHandPerformanceBackoff();
  const poseScanInterval = isHandTask()
    ? (handBackoff ? HAND_BACKOFF_POSE_SCAN_INTERVAL_MS : HAND_PACKAGE_POSE_SCAN_INTERVAL_MS)
    : POSE_SCAN_INTERVAL_MS;
  if(cameraFrameReady && landmarker && (now - lastPoseScanTs) >= poseScanInterval
    && (!seatedTestingCalibrationEnabled() || video.currentTime !== lastTestingReachPoseVideoTime)){
    // One inference/sample per real frame: repeated frames must not fill the
    // lap buffer before its two-second observation period has elapsed.
    if(seatedTestingCalibrationEnabled())lastTestingReachPoseVideoTime=video.currentTime;
    let result = null;
    try{
      result = landmarker.detectForVideo(video, now);
    }catch(e){}
    lastPoseScanTs = performance.now();
    if(result && result.landmarks && result.landmarks[0]){
      landmarks = result.landmarks[0];
      latestPoseLandmarks = landmarks;
      latestPoseWorldLandmarks = result.worldLandmarks && result.worldLandmarks[0]
        ? result.worldLandmarks[0]
        : null;
      computeMetrics(landmarks);
      const activeTask = tasks[currentTaskIdx];
      // Lock T3's mouth point while the affected hand is still at the chest.
      // Waiting until the hand covers the mouth can pull facial landmarks and
      // the target away from the anatomical mouth center.
      if(activeTask && activeTask.id === "T3" && !mouthTargetCalibration.locked
        && (!testingMouthEnabled() || lapTargetCalibration.ready)
        && (currentStepIdx === 0 || isMouthTarget(getCurrentStep()))){
        updateMouthTargetCalibration(landmarks, lastPoseScanTs);
      }
      updateMovementGate(landmarks);
    }else{
      latestPoseLandmarks = null;
      latestPoseWorldLandmarks = null;
      landmarks = null;
    }
    updateReachCameraQuality(result,landmarks);
    // Wrist stability and image quality are independent checks, collected in
    // parallel. The quality gate still has to pass before the task can start.
    if(landmarks)updateLapTargetCalibration(landmarks,now);
    // Segmentation masks own native resources; release after pixel analysis.
    for(const mask of result?.segmentationMasks || [])mask.close();
  }

  // Hand landmarks (optional). Run at a controlled cadence so Pose + Hands do
  // not block the UI thread on every animation frame.
  const handNeeded = needsHandLandmarks();
  const handScanInterval = handToMouthTaskEnabled() ? 100 : handBackoff ? HAND_BACKOFF_SCAN_INTERVAL_MS : HAND_SCAN_INTERVAL_MS;
  const freshHandVideo=!handToMouthTaskEnabled() || video.currentTime!==lastMouthHandVideoTime;
  if(cameraFrameReady && handLandmarker && handNeeded && freshHandVideo && (now - lastHandScanTs) >= handScanInterval){
    lastHandScanTs = now;
    if(handToMouthTaskEnabled())lastMouthHandVideoTime=video.currentTime;
    try{
      const hr = handLandmarker.detectForVideo(video, now);
      const affectedHand = selectAffectedHandDetection(hr, now);
      if(affectedHand){
        latestHandLandmarks = affectedHand.landmarks;
        latestHandedness = affectedHand.handedness;
        latestHandSeenAt = now;
      }else{
        latestHandLandmarks = null;
        latestHandedness = "";
      }
    }catch(e){}
  }else if(!handNeeded){
    latestHandLandmarks = null;
    latestHandedness = "";
  }
  if(calibratingAssessment) updatePreAssessmentCalibrationUI(landmarks);
  computeHandMetrics();
  tickTestingReach(landmarks,now);
  const reachFrameValid = !testingReachEnabled() || !!reachObservation(landmarks,now);
  const mouthFrameValid=!testingMouthEnabled() || (cameraFrameReady && !!landmarks && reachCameraFresh(now));
  const mouthMovementValid=updateMouthMovement(landmarks,now,mouthFrameValid);
  const testingTarget=(testingReachEnabled() || handToMouthTaskEnabled())?testingTargetState(landmarks,now):null;
  const inTarget = testingTarget ? testingTarget.contact : !calibratingAssessment && !correctionVoicePlaying && checkTarget(landmarks);
  if(lastPoseScanTs !== lastQualityPoseAt){
    lastQualityPoseAt = lastPoseScanTs;
    const qualityAspect=video.videoWidth>0 && video.videoHeight>0 ? video.videoWidth/video.videoHeight : 1;
    // Testing T1 uses the same stable two-second camera window as its lap
    // check, via updateReachCameraQuality, instead of a later 45-frame wait.
    const postureCalibrationReady=!seatedTestingCalibrationEnabled() && reachCameraCalibrationFrameUsable();
    if(postureCalibrationReady && (calibratingAssessment || (!assessmentQuality.baseline && voiceFinishedAt === 0)))
      assessmentQuality.calibrate(landmarks, latestPoseWorldLandmarks, qualityAspect);
    if(!calibratingAssessment && mouthCanMeasure() && mouthFrameValid && mouthMovementValid && reachCanMeasure() && reachFrameValid && voiceFinishedAt > 0 && !stepCompleted && !celebrateEl.classList.contains("show")){
      const freshHand = now - latestHandSeenAt <= 150 ? latestHandLandmarks : null;
      assessmentQuality.sample({pose:landmarks,world:latestPoseWorldLandmarks,hand:freshHand,handOpen:handOpenScore,handClosed:fistClosureScore,pinch:pinchScore,gaitAlternations:gaitAlternationCount,inTarget,now,aspectRatio:qualityAspect});
      const active=assessmentQuality.active();
      if(active.includes("shoulder_hike")) shoulderHikeDetected=true;
      const qualityCaption=active.map(id=>window.REHYN_ASSESSMENT_RUBRIC.compensations[id].cue).join(" ");
      if(qualityCaption!==lastQualityCaption){document.getElementById("assessmentQualityStatus").textContent=qualityCaption;lastQualityCaption=qualityCaption;}
    }
  }
  updateTargetAttemptTracking(landmarks);
  captureMotionFrame(now);
  detectAxonAIMarker(now);
  updateRuntimeDiagnostics(now);

  drawOverlay(landmarks);
  // The readout uses the same raw scoring measurements, without adding any
  // samples or changing the task's score, target, or compensation logic.
  if(cameraFrameReady && video.currentTime !== lastAngleVideoTime){lastAngleVideoTime=video.currentTime;lastAngleVideoAt=performance.now();}
  const anglesFresh = cameraFrameReady && performance.now()-lastPoseScanTs <= 250 && performance.now()-lastAngleVideoAt <= 250;
  drawTestingReachAngles(anglesFresh ? landmarks : null, anglesFresh ? latestPoseWorldLandmarks : null);
  drawTestingAssessmentAngles(anglesFresh ? landmarks : null, anglesFresh ? latestPoseWorldLandmarks : null);
  drawTestingMouth(anglesFresh ? landmarks : null, anglesFresh ? latestPoseWorldLandmarks : null);

  if(calibratingAssessment){
    requestAnimationFrame(loop);
    return;
  }

  if(testingTarget && !testingTarget.armed){
    inTargetSince=null;lastInTargetTs=0;requestAnimationFrame(loop);return;
  }

  // check target hit — with a small "grace period" so brief jitter doesn't reset the hold
  const step = getCurrentStep();
  if(step){
    if(inTarget){
      nearMissStartedAt = null;
      nearMissReason = "";
      if(inTargetSince == null) inTargetSince = now;
      lastInTargetTs = now;
      if(!stepCompleted && (now - inTargetSince) >= step.hold_ms){
        stepCompleted = true;
        completeMovementTarget(step,landmarks,now);
      }
    }else{
      // Grace: allow up to 350ms outside the zone before resetting the hold.
      if(inTargetSince != null && (now - lastInTargetTs) > 350){
        inTargetSince = null;
      }
      if(!stepCompleted && !testingReachEnabled() && !correctionVoicePlaying) handleTargetNearMiss(landmarks, now);
    }
  }
  requestAnimationFrame(loop);
}

let startSetupInProgress = false;
async function beginAssessmentSetup(){
  if(startSetupInProgress) return;
  startSetupInProgress = true;
  const startStatus = overlay.querySelector("p");
  if(startStatus && !LIBRARY_TEST_MODE){
    startStatus.textContent = "";
    startStatus.classList.add("hidden");
  }
  startBtn.textContent = "Loading assessment...";
  startBtn.setAttribute("aria-busy", "true");
  const unlockPromise = unlockAudioPlayback();
  overlay.classList.add("hidden");
  startBtn.disabled = true;
  try{
    await ensureTasksLoaded();
  }catch(error){
    const overlayCopy = overlay.querySelector("p");
    if(overlayCopy){
      overlayCopy.textContent = String(error && error.message ? error.message : "The assessment could not load. Please try again.");
      overlayCopy.classList.remove("hidden");
    }
    overlay.classList.remove("hidden");
    startBtn.disabled = false;
    startBtn.textContent = "Try Again";
    startBtn.removeAttribute("aria-busy");
    startSetupInProgress = false;
    postRN({type:"assessment_start_error", message:`Could not load assessment tasks: ${String(error)}`});
    return;
  }
  if(testingReachEnabled() && reachFlow.support===null){
    startSetupInProgress=false;startBtn.removeAttribute("aria-busy");askReachSupport();return;
  }
  if(WALKING_TEST_MODE){
    const task = tasks[currentTaskIdx];
    if(!task || !isWalkingTask(task) || tasks.length !== 1){
      const message = "The walking video test could not load its walking task.";
      overlay.classList.remove("hidden");
      startBtn.disabled = false;
      startBtn.textContent = "Try Again";
      startBtn.removeAttribute("aria-busy");
      startSetupInProgress = false;
      postRN({type:"assessment_start_error", message});
      return;
    }
    await showWalkingCapture(task);
    startBtn.removeAttribute("aria-busy");
    startSetupInProgress = false;
    return;
  }
  const firstStep = tasks[currentTaskIdx] && tasks[currentTaskIdx].steps && tasks[currentTaskIdx].steps[0];
  const firstVoicePromise = !testingReachEnabled() && !testingMouthEnabled() && firstStep && firstStep.voice
    ? fetchVoiceAudio(firstStep.voice).catch(() => null)
    : Promise.resolve(null);
  calibratingAssessment = shouldRunSeatedCalibration();
  calibrationInstructionFinished = false;
  preAssessmentCalibrationReady = false;
  calibrationAutoStartInProgress = false;
  assessmentLapTarget = null;
  assessmentLapTargetRadius = null;
  forwardReachPlacement = null;
  if(seatedTestingCalibrationEnabled()){
    resetReachCameraQuality();
    lastTestingReachPoseVideoTime=null;
    assessmentQuality.trunkLeanBaselineFrames=[];
    assessmentQuality.trunkLeanBaseline=null;
    assessmentQuality.baselines=[];
    assessmentQuality.baseline=null;
  }
  if(testingMouthEnabled()){assessmentQuality.baselines=[];assessmentQuality.baseline=null;mouthTargetCalibration=newMouthTargetCalibration();}
  patientFaceReferenceSamples = [];
  patientFaceReference = null;
  lastPatientFaceReferenceAt = 0;
  lapTargetCalibration = newLapTargetCalibration();
  if(calibratingAssessment){
    ui.classList.add("hidden");
    calibrationOverlay.classList.remove("hidden");
    calibrationOverlay.classList.toggle('testingReachCalibration',seatedTestingCalibrationEnabled());
    calibrationTitle.textContent = "Preparing your camera";
    calibrationLead.textContent = "Sit still with your affected hand resting on your lap while the camera and movement model get ready.";
    if(seatedTestingCalibrationEnabled()){
      calibrationQuality.classList.remove('hidden');
      calibrationSeat.querySelector("span:last-child").textContent = "Affected hand is visible";
      calibrationLap.querySelector("span:last-child").textContent = "Hold your hand still on your lap for 2 seconds";
    }
    updatePreAssessmentCalibrationUI(null);
    prefetchVoice(testingMouthEnabled() ? MOUTH_TEST_VOICE.calibration : testingReachEnabled() ? TESTING_REACH_CALIBRATION_INSTRUCTION : CALIBRATION_INSTRUCTION);
    prefetchVoice(CALIBRATION_COMPLETE_INSTRUCTION);
  }
  startBtn.textContent = "Opening camera...";
  const camOk = await setupCamera();
  if(!camOk){
    calibratingAssessment = false;
    calibrationOverlay.classList.add("hidden");
    ui.classList.remove("hidden");
    overlay.classList.remove("hidden");
    startBtn.disabled = false;
    startBtn.textContent = "Try Camera Again";
    startBtn.removeAttribute("aria-busy");
    startSetupInProgress = false;
    return;
  }
  try{
    await setupTrackingModels();
  }catch(error){
    calibratingAssessment = false;
    calibrationOverlay.classList.add("hidden");
    ui.classList.remove("hidden");
    overlay.classList.remove("hidden");
    startBtn.disabled = false;
    startBtn.textContent = "Try Camera Again";
    startBtn.removeAttribute("aria-busy");
    startSetupInProgress = false;
    postRN({type:"model_setup_error", message:String(error)});
    return;
  }
  await Promise.allSettled([unlockPromise, firstVoicePromise]);
  running = true;
  requestAnimationFrame(loop);
  if(calibratingAssessment){
    await playVoice(testingMouthEnabled() ? MOUTH_TEST_VOICE.calibration : testingReachEnabled() ? TESTING_REACH_CALIBRATION_INSTRUCTION : CALIBRATION_INSTRUCTION);
    calibrationInstructionFinished = true;
    updatePreAssessmentCalibrationUI(latestPoseLandmarks);
    return;
  }
  await startStep();
}

startBtn.addEventListener("click", beginAssessmentSetup);
if(window.__rehynStartRequested) void beginAssessmentSetup();
if((LIBRARY_TEST_MODE || WALKING_TEST_MODE) && !window.__rehynStartRequested) void beginAssessmentSetup();

// Presentation only: smoothly fill one ring using the actual camera checks
// and lap-hold progress. Never show completion until the calibration is ready.
const calibrationRing = document.getElementById("calibrationRing");
const calibrationRingFill = document.getElementById("calibrationRingFill");
const calibrationRingTip = document.getElementById("calibrationRingTip");
function syncCalibrationRing(){
  if(!calibrationRing || !calibrationRingFill) return;
  if(calibrationOverlay.classList.contains("hidden") || calibrationOverlay.classList.contains("testingReachCalibration")){
    calibrationRing.classList.remove("complete", "tracking");
    calibrationRingFill.style.strokeDashoffset = "100";
    if(calibrationRingTip) calibrationRingTip.style.transform = "rotate(-90deg)";
    return;
  }
  const complete = preAssessmentCalibrationReady;
  const checks = [calibrationCamera, calibrationArm, calibrationSeat];
  const passed = checks.filter(element => element.classList.contains("done")).length;
  const samples = lapTargetCalibration.samples || [];
  const elapsed = samples.length > 1 ? samples[samples.length - 1].t - samples[0].t : 0;
  const lapProgress = calibrationLap.classList.contains("done") ? 1
    : Math.max(0, Math.min(1, elapsed / LAP_CALIBRATION_MIN_MS));
  const progress = complete ? 1 : Math.min(.95, (passed + lapProgress) / 4);
  calibrationRingFill.style.strokeDashoffset = String((1 - progress) * 100);
  if(calibrationRingTip) calibrationRingTip.style.transform = `rotate(${progress * 360 - 90}deg)`;
  calibrationRing.classList.toggle("tracking", passed === checks.length && !complete);
  calibrationRing.classList.toggle("complete", complete);
}
(function calibrationRingFrame(){
  syncCalibrationRing();
  requestAnimationFrame(calibrationRingFrame);
})();

walkingVideoInput.addEventListener("click", () => {
  setWalkingCaptureStatus("");
});

// Presentation only: the Upload / Record tabs share the same file input and
// acceptance pipeline. "Record now" only asks the phone to open its camera
// (the capture attribute); desktop browsers show the normal file picker.
const walkingModeTabs = document.getElementById("walkingModeTabs");
const walkingChooseVideoLabel = document.getElementById("walkingChooseVideoLabel");
function setWalkingPickerMode(mode){
  const record = mode === "record";
  walkingCapture.dataset.walkingMode = record ? "record" : "upload";
  if(record) walkingVideoInput.setAttribute("capture", "environment");
  else walkingVideoInput.removeAttribute("capture");
  if(walkingChooseVideoLabel) walkingChooseVideoLabel.textContent = record ? "Open camera" : "Choose video";
  if(walkingModeTabs) walkingModeTabs.querySelectorAll("button[data-walking-mode]").forEach(tab => {
    const selected = tab.dataset.walkingMode === walkingCapture.dataset.walkingMode;
    tab.classList.toggle("selected", selected);
    tab.setAttribute("aria-selected", selected ? "true" : "false");
  });
}
if(walkingModeTabs) walkingModeTabs.addEventListener("click", event => {
  const tab = event.target.closest("button[data-walking-mode]");
  if(tab) setWalkingPickerMode(tab.dataset.walkingMode);
});
setWalkingPickerMode("upload");

function isWalkingVideoFile(file){
  if(!file) return false;
  if(String(file.type || "").toLowerCase().startsWith("video/")) return true;
  return /\.(mp4|mov|m4v|webm|avi|mpeg|mpg|mkv|3gp)$/i.test(String(file.name || ""));
}

function walkingVideoContentType(file){
  const supplied = String(file && file.type || "").split(";", 1)[0].trim().toLowerCase();
  if(supplied.startsWith("video/")) return supplied;
  const extension = String(file && file.name || "").split(".").pop().toLowerCase();
  return ({
    mp4:"video/mp4", mov:"video/quicktime", m4v:"video/x-m4v",
    webm:"video/webm", avi:"video/x-msvideo", mpeg:"video/mpeg",
    mpg:"video/mpeg", mkv:"video/x-matroska", "3gp":"video/3gpp",
  })[extension] || "video/mp4";
}

async function processWalkingVideoFile(file, source="picker"){
  if(!file){
    setWalkingCaptureStatus(source === "drop"
      ? "No video was dropped. Drag one video here when ready."
      : "No video was selected. Choose a walking video when ready.");
    return;
  }
  if(!isWalkingVideoFile(file)){
    setWalkingCaptureStatus("That file is not a video. Drop or choose a walking video instead.", "warn");
    return;
  }
  walkingProceedUnconfirmedBtn.classList.add("hidden");
  walkingChooseVideoBtn.disabled = true;
  walkingDesktopActions.classList.add("busy");
  walkingVideoDropZone.classList.add("busy");
  setWalkingCaptureStatus(source === "drop"
    ? "Opening the dropped walking video on this device..."
    : "Opening the walking video on this device...");
  try{
    const validation = await validateWalkingVideo(file, progress => {
      setWalkingCaptureStatus(progress.message || "Opening the walking video...");
    });
    if(!validation.ok){
      setWalkingCaptureStatus(validation.message, "warn");
      return;
    }
    setWalkingCaptureStatus(validation.message, "good");
    if(!LIBRARY_TEST_MODE && !LOCAL_PREVIEW_MODE){
      await playVoice("The walking video is saved as part of your assessment record. Thank you. Your survey identifies the affected area and side, and your assessment is now complete.");
    }
    setWalkingCaptureStatus(LIBRARY_TEST_MODE
      ? "Walking video analysed. Preparing the test result..."
      : "Walking video accepted. Preparing secure save...", "good");
    await completeUploadedWalkingTask(file, validation);
  }catch(error){
    setWalkingCaptureStatus(`The selected video could not be processed. ${String(error && error.message ? error.message : error)}`, "warn");
    postRN({type:"walking_video_error", message:String(error)});
  }finally{
    walkingChooseVideoBtn.disabled = false;
    walkingDesktopActions.classList.remove("busy");
    walkingVideoDropZone.classList.remove("busy", "dragover");
    walkingVideoInput.value = "";
  }
}

let walkingVideoDragDepth = 0;

walkingVideoDropZone.addEventListener("dragenter", event => {
  event.preventDefault();
  if(walkingDesktopActions.classList.contains("busy")) return;
  walkingVideoDragDepth += 1;
  walkingVideoDropZone.classList.add("dragover");
});

walkingVideoDropZone.addEventListener("dragover", event => {
  event.preventDefault();
  if(event.dataTransfer) event.dataTransfer.dropEffect = "copy";
});

walkingVideoDropZone.addEventListener("dragleave", event => {
  event.preventDefault();
  walkingVideoDragDepth = Math.max(0, walkingVideoDragDepth - 1);
  if(walkingVideoDragDepth === 0) walkingVideoDropZone.classList.remove("dragover");
});

walkingVideoDropZone.addEventListener("drop", event => {
  event.preventDefault();
  walkingVideoDragDepth = 0;
  walkingVideoDropZone.classList.remove("dragover");
  if(walkingDesktopActions.classList.contains("busy")) return;
  const droppedFiles = event.dataTransfer && event.dataTransfer.files;
  if(droppedFiles && droppedFiles.length > 1){
    setWalkingCaptureStatus("Drop one walking video at a time.", "warn");
    return;
  }
  const file = droppedFiles && droppedFiles[0];
  void processWalkingVideoFile(file, "drop");
});

walkingVideoInput.addEventListener("change", () => {
  const file = walkingVideoInput.files && walkingVideoInput.files[0];
  void processWalkingVideoFile(file, "picker");
});

walkingProceedUnconfirmedBtn.addEventListener("click", async () => {
  const file = pendingUnconfirmedWalkingVideo;
  const validation = pendingUnconfirmedWalkingValidation;
  if(!file || !validation || validation.reason !== "identity_unconfirmed"){
    walkingProceedUnconfirmedBtn.classList.add("hidden");
    setWalkingCaptureStatus("Choose the walking video again so Rehyn can review it.", "warn");
    return;
  }
  walkingProceedUnconfirmedBtn.disabled = true;
  walkingChooseVideoBtn.disabled = true;
  walkingDesktopActions.classList.add("busy");
  setWalkingCaptureStatus(LIBRARY_TEST_MODE
    ? "Using this video without identity confirmation for this test only."
    : "Using this video without identity confirmation. It will be marked for therapist review.", "good");
  try{
    await playVoice(LIBRARY_TEST_MODE
      ? "We could not confirm the face in this video. You can still use it for this test."
      : "We could not confirm the face in this video. The video will be included and marked for therapist review.");
    await completeUploadedWalkingTask(file, {
      ...validation,
      ok:true,
      samePatientConfirmed:false,
      identityStatus:"unconfirmed_patient_proceeded",
    });
    pendingUnconfirmedWalkingVideo = null;
    pendingUnconfirmedWalkingValidation = null;
    walkingProceedUnconfirmedBtn.classList.add("hidden");
  }catch(error){
    setWalkingCaptureStatus(`The selected video could not be saved. ${String(error && error.message ? error.message : error)}`, "warn");
    postRN({type:"walking_video_error", message:String(error)});
  }finally{
    walkingProceedUnconfirmedBtn.disabled = false;
    walkingChooseVideoBtn.disabled = false;
    walkingDesktopActions.classList.remove("busy");
  }
});

walkingSkipBtn.addEventListener("click", async () => {
  if(WALKING_TEST_MODE){
    postRN({type:"exit"});
    return;
  }
  const task = tasks[currentTaskIdx];
  if(!task || !isWalkingTask(task)) return;
  walkingSkipBtn.disabled = true;
  walkingChooseVideoBtn.disabled = true;
  walkingRecordBtn.disabled = true;
  setWalkingCaptureStatus("Walking will be marked as not observed for this assessment.", "good");
  await playVoice("That is okay. We will skip walking today and mark it as not observed, not as a failed test.");
  taskResults[currentTaskIdx] = {
    task_id:task.id,
    completed_steps:0,
    total_steps:0,
    duration_ms:0,
    steps:[],
    metrics:{walking_skipped:true, skip_reason:"patient_unable_or_restricted"},
  };
  postRN({type:"walking_skipped", package_id:ASSESSMENT_PACKAGE, task_id:task.id});
  walkingCapture.classList.add("hidden");
  ui.classList.remove("hidden");
  currentStepIdx = 0;
  currentTaskIdx += 1;
  if(currentTaskIdx >= tasks.length){
    finishAssessment();
  }else{
    startStep();
  }
});

walkingRecordBtn.addEventListener("click", async () => {
  walkingRecordBtn.disabled = true;
  setWalkingCaptureStatus("Opening the rear camera. Keep the patient safely in view...");
  const cameraReady = await switchToWalkingCamera();
  if(!cameraReady){
    setWalkingCaptureStatus("The walking camera could not be opened. Check camera permission and try again.", "warn");
    walkingRecordBtn.disabled = false;
    return;
  }
  walkingCaptureActive = true;
  walkingCapture.classList.add("hidden");
  ui.classList.remove("hidden");
  postRN({type:"walking_recording_started", task_id:"L6", device_mode:"mobile"});
  await startStep();
});

markerConfirmBtn.addEventListener("click", () => {
  if(!waitingForAdvancedGate) return;
  if(navigator.vibrate) navigator.vibrate(50);
  continueAdvancedTask(true);
});

markerMissingBtn.addEventListener("click", () => {
  markerChoicePanel.classList.add("hidden");
  markerMissingPanel.classList.remove("hidden");
});

markerBasicBtn.addEventListener("click", () => {
  if(!waitingForAdvancedGate) return;
  if(navigator.vibrate) navigator.vibrate(40);
  continueAdvancedTask(false);
});

markerBackBtn.addEventListener("click", () => {
  markerMissingPanel.classList.add("hidden");
  markerChoicePanel.classList.remove("hidden");
});

markerStoreBtn.addEventListener("click", () => {
  window.open(AXONAI_MARKER_STORE_URL, "_blank");
});

skipBtn.addEventListener("click", () => {
  if(testingMouthEnabled() && (!mouthCanMeasure() || calibratingAssessment || stepCompleted)) return;
  if(testingReachEnabled() && (!reachCanMeasure() || stepCompleted)) return;
  if(!running) return;
  if(waitingForAdvancedGate) return;
  nextStep(true);
});

exitBtn.addEventListener("click", async () => {
  if(localReview.active){
    running=false;
    const task=tasks[currentTaskIdx];
    await finishLocalReview(taskResults[currentTaskIdx] || {task_id:task.id,completed_steps:0,total_steps:task.steps.length,steps:[],metrics:{stopped:true}});
  }
  if(testingReachEnabled()){stopReachVoice();running=false;if(video.srcObject)video.srcObject.getTracks().forEach(t=>t.stop());}
  if(testingMouthEnabled()){stopMouthVoice();running=false;if(video.srcObject)video.srcObject.getTracks().forEach(t=>t.stop());}
  postRN({type:"exit"});
});

// Mark the whole module ready only after every listener has been installed.
window.__rehynRunnerModuleReady = true;
window.clearTimeout(window.__rehynRunnerStartupWatchdog);
// inform RN we're ready
postRN({type:"ready"});
</script>
</body>
</html>
"""


from backend.testing_mouth_voice_lines import CALIBRATION as MOUTH_CALIBRATION, STEPS as MOUTH_STEPS, NEAR as MOUTH_NEAR, LINES as MOUTH_LINES
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace("</head>", "<script>window.MOUTH_TEST_VOICE=" + json.dumps({"calibration":MOUTH_CALIBRATION,"steps":MOUTH_STEPS,"near":MOUTH_NEAR,"lines":MOUTH_LINES}) + ";</script></head>")

# Testing-only support, adaptation and narration share the existing runner geometry.
for _testing_script in ("testing_reach.js", "testing_reach_camera_quality.js", "reach_voice.js", "testing_mouth.js"):
    POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace("</head>", "<script>" + (ROOT_DIR / _testing_script).read_text(encoding="utf-8") + "</script></head>")
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace('<div id="stage">', '<div id="stage">' + (ROOT_DIR / "testing_reach_ui.html").read_text(encoding="utf-8"), 1)
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace('let startSetupInProgress = false;', (ROOT_DIR / "testing_reach_flow.js").read_text(encoding="utf-8") + '\nlet startSetupInProgress = false;', 1)
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace('<div id="stage">', '<div id="stage">' + (ROOT_DIR / "testing_mouth_ui.html").read_text(encoding="utf-8"), 1)
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace('let startSetupInProgress = false;', (ROOT_DIR / "testing_mouth_flow.js").read_text(encoding="utf-8") + '\nlet startSetupInProgress = false;', 1)

POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace('let startSetupInProgress = false;', (ROOT_DIR / "local_assessment_video.js").read_text(encoding="utf-8") + '\nlet startSetupInProgress = false;', 1)

POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace('<div id="stage">', '<div id="stage">' + (ROOT_DIR / "assessment_completion_ui.html").read_text(encoding="utf-8"), 1)
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace("</head>", "<script>" + (ROOT_DIR / "assessment_completion.js").read_text(encoding="utf-8") + "</script></head>")

_reach_target_script = (ROOT_DIR / "reach_target.js").read_text(encoding="utf-8")
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace("</head>", "<script>" + _reach_target_script + "</script></head>")
_reach_angles_script = (ROOT_DIR / "reach_angles.js").read_text(encoding="utf-8")
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace("</head>", "<script>" + _reach_angles_script + "</script></head>")
_testing_assessment_diagnostics_script = (ROOT_DIR / "testing_assessment_diagnostics.js").read_text(encoding="utf-8")
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace("</head>", "<script>" + _testing_assessment_diagnostics_script + "</script></head>")
_trunk_lean_metrics_script = (ROOT_DIR.parent / "testing" / "trunk-lean-comparison" / "trunk_lean_metrics.js").read_text(encoding="utf-8")
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace("</head>", "<script>" + _trunk_lean_metrics_script + "</script></head>")
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace('<div id="lapStatus"', '''<aside id="testingReachAngles" class="hidden" aria-label="Live seated forward reach angles">
  <div class="reachAngleArm"><b>Arm elevation</b><strong id="liveArmElevationScore">—</strong><span>Scoring value · model 3D</span><span>Cyan 2D arc: <b id="liveArmElevationArc">—</b></span></div>
  <div class="reachAngleElbow"><b>Elbow extension</b><strong id="liveElbowExtension">—</strong><span>Scoring value &amp; yellow arc · 2D</span><span>180° = straight</span></div>
  <div class="reachTrunkLean"><b>Trunk lean camera cues</b><span id="liveTrunkLeanState">Waiting for upright calibration</span>
    <div class="reachTrunkCueGrid">
      <div class="reachTrunkCue"><b>Shoulder cue</b><strong id="liveTrunkLeanShoulder">—</strong><span>Threshold: above 12°</span><span id="liveTrunkLeanShoulderState">Waiting for upright calibration</span></div>
      <div class="reachTrunkCue"><b>Face cue</b><strong id="liveTrunkLeanFace">—</strong><span>Threshold: at least 7°</span><span id="liveTrunkLeanFaceState">Waiting for upright calibration</span>
        <div class="reachTrunkRatio"><span>Face-to-hip width ratio (Sface)</span><b id="liveTrunkLeanFaceScale">—</b></div>
        <span>Ear-width change ÷ hip-width change; 1.0000× is the upright baseline. Pink dots show tracked face points; the brighter dots and line mark the ears.</span>
      </div>
    </div>
  </div>
  <small>A cue must meet its threshold for 0.5 seconds to confirm trunk lean. Any confirmed compensation sets the final exercise score to 15/100. These are camera estimates, not anatomical angles.</small>
</aside><div id="lapStatus"''', 1)
_assessment_quality_script = (ROOT_DIR / "assessment_quality.js").read_text(encoding="utf-8")
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace("</head>", """<style>
#testingReachAngles{position:absolute;top:calc(80px + env(safe-area-inset-top,0px));left:16px;z-index:4;pointer-events:none;background:rgba(5,25,23,.88);border-radius:10px;padding:12px;display:grid;grid-template-columns:1fr 1fr;gap:12px;width:min(370px,calc(100% - 32px));font-size:13px;line-height:1.35}
#testingReachAngles span,#testingReachAngles strong{display:block}#testingReachAngles strong{font-size:25px}#testingReachAngles small{grid-column:1/-1;color:#e5efec;font-size:11px}
.reachAngleArm{color:#67e8f9}.reachAngleElbow{color:#facc15}
.reachTrunkLean{grid-column:1/-1;color:#fda4af;border-top:1px solid rgba(255,255,255,.25);padding-top:8px}.reachTrunkCueGrid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}.reachTrunkCue{background:rgba(255,255,255,.08);border-radius:8px;padding:8px;min-width:0}.reachTrunkCue b,.reachTrunkCue span{display:block}.reachTrunkCue strong{font-size:23px!important;line-height:1.2}.reachTrunkCue span{font-size:11px;line-height:1.35}.reachTrunkRatio{border-top:1px solid rgba(255,255,255,.2);margin-top:7px;padding-top:6px}.reachTrunkRatio b{font-size:16px;line-height:1.3}
#assessmentQualityStatus{color:#fff;background:#8a2424;border-radius:8px;max-width:900px;margin:6px auto;padding:8px 12px;font-size:16px;line-height:1.4}
#assessmentQualityStatus:empty{display:none}
#celebrate{overflow:auto;padding:24px 16px;box-sizing:border-box;justify-content:center;background:#244d3c}
#celebrate .star{font-size:48px;flex-shrink:0}
#celebrate h2,#celebrate p{margin:0}
#celebrate .msg{max-width:640px}
@media(max-width:600px){#celebrate{gap:10px}#celebrate .star{font-size:36px}#celebrate h2{font-size:23px}}
/* Between-task celebration: green tick disc, one title, one "n of N · Next" line. */
#celebrate{background:radial-gradient(circle at 50% 42%,rgba(36,77,60,.55) 0,rgba(12,16,14,.7) 420px,rgba(12,16,14,.85) 100%);gap:0}
#celebrate .star,#celebrate .msg,#celebrate .dotsMini,#celebrate .confetti{display:none}
.celebrateMark{position:relative;width:144px;height:144px;margin-bottom:36px;flex-shrink:0}
.celebrateRipples{position:absolute;left:50%;top:50%;width:400px;height:400px;margin:-200px 0 0 -200px;overflow:visible;pointer-events:none}
.celebrateRipple{fill:none;stroke:#7FE5A3;stroke-width:3;transform-box:fill-box;transform-origin:center;animation:celebrateRipple 2.8s ease-out infinite}
.celebrateRipple2{animation-delay:1.4s}
.celebrateTick{position:absolute;inset:0;border-radius:999px;background:#7FE5A3;color:#0c100e;display:flex;align-items:center;justify-content:center;box-shadow:0 18px 50px rgba(0,0,0,.45)}
.celebrateTick svg{width:72px;height:72px}
#celebrate.show .celebrateTick{animation:celebratePop .7s cubic-bezier(.2,.7,.2,1) both}
#celebrate h2{font-size:40px;font-weight:800;line-height:48px;color:#fff}
#celebrate.show h2{animation:celebrateRise .6s .35s cubic-bezier(.2,.7,.2,1) both}
#celebrate .next{font-size:16px;line-height:22px;color:#bcc2ba;font-weight:500;letter-spacing:0;text-transform:none;margin-top:10px;max-width:520px}
#celebrate.show .next{animation:celebrateRise .6s .6s cubic-bezier(.2,.7,.2,1) both}
@keyframes celebrateRipple{0%{transform:scale(1);opacity:.6}100%{transform:scale(2.6);opacity:0}}
@keyframes celebratePop{0%{opacity:0;transform:scale(.6)}60%{transform:scale(1.08)}100%{opacity:1;transform:scale(1)}}
@keyframes celebrateRise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@media(max-width:600px){#celebrate h2{font-size:30px;line-height:36px}.celebrateMark{width:120px;height:120px;margin-bottom:28px}.celebrateTick svg{width:60px;height:60px}}
@media (prefers-reduced-motion:reduce){#celebrate *{animation:none!important}}
</style><script>""" + _assessment_quality_script + "\nwindow.REHYN_ASSESSMENT_RUBRIC=" + json.dumps({"version": ASSESSMENT_QUALITY_VERSION, "compensations": ASSESSMENT_COMPENSATIONS, "tasks": ASSESSMENT_RUBRICS}) + ";</script></head>")
POSE_RUNNER_HTML = POSE_RUNNER_HTML.replace('<div id="ui">', '<div id="ui"><div id="assessmentQualityStatus" role="status" aria-live="polite"></div>')


@api_router.get("/pose/runner", response_class=HTMLResponse)
async def pose_runner(request: Request):
    html = POSE_RUNNER_HTML
    if is_local_assessment_preview(request):
        html = html.replace("const LOCAL_PREVIEW_MODE = false;", "const LOCAL_PREVIEW_MODE = true;", 1)
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


# ============ Rehab Runner: per-exercise pose-guided reps with form feedback ============
# Each exercise defines:
#  - reps: total number of repetitions
#  - setup_voice: warm intro played once
#  - cycle: ordered list of pose targets the wrist must hit, defining ONE rep
#  - feedback_rules: ordered rules; first matching wins. Each rule: condition over per-rep metrics
#  - pose_mode: "body" (uses pose landmarks) or "tap" (fine-motor — patient taps complete)
REHAB_RUNNER_CONFIG: Dict[str, Dict[str, Any]] = {
    "ex_reach": {
        "name": "Graded Forward Reach",
        "reps": 5,
        "pose_mode": "body",
        "setup_voice": "Welcome. We are going to practice the graded forward reach. Sit upright, with your back away from the chair. Place your affected hand on your lap. I'll guide you through each repetition.",
        "correct_form_cue": "Next time, keep your trunk and shoulder still and simply extend your elbow to reach the target.",
        "cycle": [
            {"caption": "Reach forward to the target", "voice": "Slowly reach your hand forward, as far as you comfortably can.", "target": {"x": 0.5, "y": 0.40, "r": 0.10}, "hold_ms": 1200},
            {"caption": "Return to lap", "voice": "Now gently return your hand to the same calibrated place on your lap.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"}, "hold_ms": 1200},
        ],
        "feedback_rules": [
            {"if": "trunk_lean_deg > 18", "say": "I noticed your back leaned forward. On the next repetition, try keeping your spine tall and let your arm do the work."},
            {"if": "shoulder_hike", "say": "Your shoulder lifted up toward your ear. Try keeping your shoulder relaxed and dropped down on the next try."},
            {"if": "reach_completion < 0.7", "say": "You almost reached the target. On the next try, push gently from your shoulder to extend a little further."},
            {"default": "Beautiful repetition. On the next one, focus on a smooth, steady motion from start to finish."},
        ],
    },
    "ex_trunk": {
        "name": "Trunk-Restrained Reaching",
        "reps": 5,
        "pose_mode": "body",
        "setup_voice": "We will practice trunk-restrained reaching. Sit tall, with your back firmly against the chair. Try to keep your back touching the chair the whole time. Let's begin.",
        "correct_form_cue": "Next time, keep your back against the chair and your shoulder relaxed, and simply extend your elbow to reach the target.",
        "compensation_problems": {
            "trunk_lean": "your trunk leaned beyond the small posture allowance",
            "shoulder_hike": "your shoulder lifted toward your ear",
        },
        "cycle": [
            {"caption": "Reach forward (back against chair)", "voice": "Reach forward to the target — keep your back pressed into the chair.", "target": {"x": 0.5, "y": 0.40, "r": 0.10}, "hold_ms": 1200},
            {"caption": "Return slowly", "voice": "Slowly return your hand to the same calibrated place on your lap.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"}, "hold_ms": 1200},
        ],
        "feedback_rules": [
            {"if": "trunk_lean_deg >= 12", "say": "Your trunk leaned beyond the small posture allowance. On the next repetition, settle your back against the chair and let your arm do the reaching."},
            {"if": "reach_completion < 0.7", "say": "With your back restrained, that's a great effort. On the next try, reach just a little further if you can."},
            {"default": "Excellent control of your trunk. Try to keep that same posture on the next repetition."},
        ],
    },
    "ex_wallslide": {
        "name": "Supported Arm Elevation Practice",
        "reps": 5,
        "pose_mode": "body",
        "setup_voice": "We will practise supported arm elevation. Sit tall with your affected forearm supported on a table or towel. Move only in a comfortable, pain-free range. Use a wall slide only if your therapist has confirmed that it is safe for you.",
        "cycle": [
            {"caption": "Slide toward the upper target", "voice": "Using the support, slowly slide your affected arm toward the upper target. Stop before pain and keep your shoulder relaxed.", "target": {"x": 0.5, "y": 0.24, "r": 0.10}, "hold_ms": 1500},
            {"caption": "Return with support", "voice": "Now slide your arm back to the starting position, slowly and with control. Good work.", "target": {"x": 0.5, "y": 0.78, "r": 0.10}, "hold_ms": 1500},
        ],
        "feedback_rules": [
            {"if": "shoulder_hike", "say": "Your shoulder lifted toward your ear. Try keeping your shoulder relaxed and pressed down as you raise your arm."},
            {"if": "trunk_lean_deg > 15", "say": "I noticed you leaned to one side. On the next repetition, try to stay tall and centered."},
            {"if": "reach_completion < 0.7", "say": "Almost reached the top. On the next try, exhale gently and try to go a little higher."},
            {"default": "Wonderful shoulder elevation. Keep that same control on the next repetition."},
        ],
    },
    "ex_scapdepress": {
        "name": "Scapular Depression Practice",
        "reps": 5,
        "pose_mode": "body",
        "setup_voice": "We will practice keeping your shoulder down while reaching. Sit tall and gently pull your shoulder blades down and back, like sliding them into your back pockets. Then reach forward.",
        "cycle": [
            {"caption": "Reach forward (shoulders down)", "voice": "Reach forward — keep your shoulder pulled down and away from your ear.", "target": {"x": 0.5, "y": 0.40, "r": 0.10}, "hold_ms": 1200},
            {"caption": "Return", "voice": "Bring your hand back to your lap, shoulders still relaxed.", "target": {"x": 0.5, "y": 0.78, "r": 0.10}, "hold_ms": 1200},
        ],
        "feedback_rules": [
            {"if": "shoulder_hike", "say": "Your shoulder lifted again. On the next try, gently pull it down and back before you reach."},
            {"if": "trunk_lean_deg > 15", "say": "You leaned forward. Stay tall and let the shoulder blade do the work."},
            {"default": "Lovely scapular control. Keep that pattern on the next repetition."},
        ],
    },
    "ex_h2m": {
        "name": "Hand-to-Mouth ADL Practice",
        "reps": 5,
        "pose_mode": "body",
        "setup_voice": "We will practice hand-to-mouth, an essential daily activity. A cup is drawn on your screen in front of you, so you do not need a real one. Sit tall with your back away from the chair and your affected hand resting on your lap. You will reach out and touch the cup, bring it up to your mouth, take a sip, put it back, and return your hand to your lap. Keep your head up: the cup comes to your mouth, not your mouth to the cup. I will guide you through each step.",
        "correct_form_cue": "Next time, keep your head up and your back still: reach to the cup with your arm, then bend your elbow to bring it all the way to your lips.",
        # The cup starts on the affected side in front of the patient; for a
        # left-affected patient the static targets are mirrored at runtime.
        "mirror_for_left": True,
        # Problem wording for the feedback ("I noticed ..."), in this exercise's terms.
        "compensation_problems": {
            "trunk_forward": "your trunk leaned forward to meet the cup",
            "shoulder_hike": "your shoulder lifted toward your ear",
            "head_drop": "your head dropped down toward the cup",
        },
        "live_metric_text": {"shoulder_flexion": "Arm lift"},
        # The cup waits inside the first target ring, attaches to the hand once
        # the patient has reached and touched it (grab_step), and is set down
        # again at the put-back step (place_step) - the same carry mechanics as
        # the grasp exercise.
        "virtual_object": {"type": "cup", "mode": "carry", "grab_step": 0, "place_step": 3},
        # The mouth target follows the patient's own mouth (MOUTH_DYNAMIC) and the
        # return target is the calibrated resting place on the lap (LAP_DYNAMIC).
        # Each step's circle only arms once its instruction has finished.
        "cycle": [
            {"caption": "Reach out and touch the cup", "voice": "Reach forward with your affected hand and touch the cup inside the circle on your screen.", "target": {"x": 0.36, "y": 0.56, "r": 0.10}, "hold_ms": 900},
            {"caption": "Bring the cup to your mouth", "voice": "The cup is in your hand. Bend your elbow and bring it up to your mouth, slowly and smoothly. Keep your head up and your back still.", "target": {"x": 0.5, "y": 0.30, "r": 0.10, "landmark": "MOUTH_DYNAMIC"}, "hold_ms": 700},
            {"caption": "Hold at your mouth, as if taking a sip", "voice": "Hold the cup at your lips as if you were taking a sip. Head up, shoulder relaxed.", "target": {"x": 0.5, "y": 0.30, "r": 0.10, "landmark": "MOUTH_DYNAMIC"}, "hold_ms": 1500},
            {"caption": "Put the cup back down", "voice": "Now lower the cup and put it back down inside the circle.", "target": {"x": 0.36, "y": 0.56, "r": 0.10}, "hold_ms": 700},
            {"caption": "Return your empty hand to your lap", "voice": "Now bring your empty hand back to the same place on your lap. Nicely done.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"}, "hold_ms": 1200},
        ],
        "feedback_rules": [
            {"if": "trunk_lean_deg > 15", "say": "Your trunk leaned to meet your hand. On the next try, keep your back tall and bring your hand to your mouth instead."},
            {"if": "reach_completion < 0.6", "say": "Almost there. On the next try, bend your elbow a bit more to bring your hand closer to your mouth."},
            {"default": "Smooth hand-to-mouth. Keep that quality on the next repetition."},
        ],
    },
    "ex_grasp": {
        "name": "Cylindrical Grasp & Transport",
        "reps": 5,
        "pose_mode": "body",
        # Pose plus the hand landmarker: the hand-opening and grasp steps are
        # confirmed from the fingers, not just from the wrist reaching the cup.
        "hand_tracking": True,
        # Arm every target on the first camera frame after its narration ends.
        "target_arm_delay_ms": 0,
        # The cup starts on the affected side and is carried across the midline;
        # for a left-affected patient the targets are mirrored at runtime.
        "mirror_for_left": True,
        "setup_voice": "We will practise reaching for a cup, opening your hand, grasping it, and carrying it across. The cup is drawn on your screen, so you do not need a real object. Reach to the cup with your affected hand, open your hand wide, close your fingers around the cup, carry it across, open your hand to set it down, then return to your lap.",
        "virtual_object": {"type": "cup", "mode": "carry", "grab_step": 2, "place_step": 4},
        "correct_form_cue": "Next time, keep your chest still and both shoulders level: reach forward and straighten your elbow to the cup, open your hand wide, close it around the cup, and carry it across with your arm and a straight wrist.",
        "compensation_problems": {
            "trunk_lean": "your chest leaned toward the cup",
            "trunk_side_lean": "your body leaned to the side to carry the cup",
            "shoulder_hike": "your shoulder lifted toward your ear",
            "elbow_flare": "your elbow flared out and up instead of reaching forward",
            "wrist_flexion": "your wrist moved out of line with your forearm while you gripped the cup",
        },
        "cycle": [
            {"caption": "Reach to the cup", "voice": "Reach toward the cup on your screen with your affected hand.", "target": {"x": 0.30, "y": 0.55, "r": 0.10}, "hold_ms": 900},
            {"caption": "Open your hand wide around the cup", "voice": "Now open your hand wide, ready to take the cup.", "target": {"landmark": "HAND_OPEN", "x": 0.30, "y": 0.55, "r": 0.14}, "hold_ms": 400, "max_wait_ms": 6000},
            {"caption": "Close your fingers around the cup", "voice": "Close your fingers around the cup to grasp it.", "target": {"landmark": "HAND_CLOSED", "x": 0.30, "y": 0.55, "r": 0.14}, "hold_ms": 500, "max_wait_ms": 6000},
            {"caption": "Carry the cup across", "voice": "The cup is in your hand. Carry it slowly across to the other side.", "target": {"x": 0.70, "y": 0.55, "r": 0.10}, "hold_ms": 1200},
            {"caption": "Open your hand to set the cup down", "voice": "Open your fingers to set the cup down.", "target": {"landmark": "HAND_OPEN", "x": 0.70, "y": 0.55, "r": 0.14}, "hold_ms": 500, "max_wait_ms": 6000},
            {"caption": "Return your empty hand to your lap", "voice": "Now bring your empty hand back to your lap. Nicely done.", "target": {"x": 0.5, "y": 0.78, "r": 0.10, "landmark": "LAP_DYNAMIC"}, "hold_ms": 1200},
        ],
        "feedback_rules": [
            {"if": "trunk_lean_deg > 18", "say": "I noticed your trunk twisted with the cup. On the next repetition, try keeping your shoulders square and let your arm cross the midline."},
            {"if": "reach_completion < 0.6", "say": "Almost reached the far target. On the next try, extend a little further across your body."},
            {"default": "Beautiful transport. On the next repetition, focus on a smooth release at the end."},
        ],
    },
    "ex_handopen": {
        "name": "Active Hand Opening and Release",
        "reps": 8,
        "pose_mode": "tap",
        "setup_voice": "We will practise opening and relaxing your affected hand with your forearm supported on a table. A soft ball is drawn on your screen - imagine opening your hand around it, no real object is needed. Use your other hand for gentle assistance if needed.",
        "virtual_object": {"type": "ball", "mode": "hand_anchor"},
        "cycle": [
            {"caption": "Open your hand around the ball on screen", "voice": "Slowly open your affected hand around the ball on your screen, as wide as is comfortable, hold for a moment, then let the fingers relax. That effort counts even if the movement is small. Tap when you finish one repetition.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Wonderful finger extension. On the next repetition, try to open your hand a little wider and hold for a full second before relaxing."},
        ],
    },
    "ex_pinch": {
        "name": "Pinch & Peg Placement",
        "reps": 8,
        "pose_mode": "tap",
        "setup_voice": "We will practice pinch. A small peg and a container are drawn on your screen, so you do not need real objects. Pinch your thumb and index finger together as if lifting the peg, move your hand toward the container, and release. Tap I did one repetition each time you place one.",
        "virtual_object": {"type": "peg", "mode": "pick_place", "source": {"x": 0.32, "y": 0.62}, "container": {"x": 0.68, "y": 0.62}},
        "cycle": [
            {"caption": "Pinch the peg on screen and place it in the container", "voice": "Pinch as if lifting the peg with your thumb and index finger, carry it to the container on the screen, release, then tap when done.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Lovely pinch control. On the next repetition, try a slightly smaller object or pinch with your thumb and middle finger for variety."},
        ],
    },
    "ex_bilateral": {
        "name": "Bilateral Arm Training",
        "reps": 5,
        "pose_mode": "body",
        "setup_voice": "We will use both arms together. A bar is drawn between your hands on the screen - hold it with both hands as you move. Move both arms inward to meet, then outward, equally on both sides.",
        "virtual_object": {"type": "bar", "mode": "between_hands"},
        "cycle": [
            {"caption": "Bring both hands together", "voice": "Holding the bar on your screen, bring both hands inward to meet in front of you, equally.", "target": {"x": 0.5, "y": 0.45, "r": 0.12}, "hold_ms": 1500},
            {"caption": "Open both arms outward", "voice": "Now open both hands outward, also equally.", "target": {"x": 0.5, "y": 0.45, "r": 0.30}, "hold_ms": 800},
        ],
        "feedback_rules": [
            {"if": "trunk_lean_deg > 15", "say": "I noticed you leaned to one side. On the next repetition, try to stay centered and move both arms equally."},
            {"default": "Beautiful bilateral coordination. On the next repetition, try to make both arms perfectly mirror each other."},
        ],
    },
    "ex_lower_selective": {
        "name": "Supported Selective Lower-Limb Control",
        "reps": 5,
        "pose_mode": "guided",
        "setup_voice": "Welcome. Sit in a stable chair with both feet supported. Keep a carer nearby if you need help with sitting balance. We will practise one slow knee movement at a time, and you can confirm each step when it is complete.",
        "cycle": [
            {"caption": "Set your starting position", "voice": "Sit tall and place both feet flat. Keep your affected thigh supported and your knee pointing forward. When you feel steady, tap completed step.", "target": None, "hold_ms": 0},
            {"caption": "Straighten the affected knee", "voice": "Slowly straighten your affected knee within a comfortable range. Keep your thigh supported and breathe normally. Tap completed step when you reach your comfortable position.", "target": None, "hold_ms": 0},
            {"caption": "Lower with control", "voice": "Gently bend the knee and lower your foot back to the floor. Excellent control. Tap completed step when your foot is settled.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Well done. You completed the knee movement with care. For the next repetition, keep the movement slow and let the affected leg do as much as it safely can."},
        ],
    },
    "ex_ankle_dorsiflexion": {
        "name": "Seated Toe-Lift Practice",
        "reps": 6,
        "pose_mode": "guided",
        "setup_voice": "Sit securely with your affected foot flat and your heel supported on the floor. We will practise lifting the front of your foot without lifting the heel.",
        "cycle": [
            {"caption": "Keep your heel down", "voice": "Place your affected heel firmly on the floor and keep the knee pointing forward. Tap completed step when you are ready.", "target": None, "hold_ms": 0},
            {"caption": "Lift your toes and forefoot", "voice": "Keeping the heel down, gently lift your toes and the front of your foot. Hold for a comfortable moment, then tap completed step.", "target": None, "hold_ms": 0},
            {"caption": "Lower slowly", "voice": "Lower the front of your foot slowly until it rests on the floor. Nicely done. Tap completed step when it is settled.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Great effort. Keep the heel planted and aim for a smooth toe lift on the next repetition, even if the movement is small."},
        ],
    },
    "ex_sit_to_stand": {
        "name": "Assisted Sit-to-Stand Practice",
        "reps": 3,
        "pose_mode": "guided",
        "setup_voice": "Only begin with a therapist or capable carer beside you and a stable chair that cannot roll. Keep your usual support within reach. Stop if you feel dizzy, unsafe, or in pain.",
        "cycle": [
            {"caption": "Position your feet safely", "voice": "Move toward the front of the chair. Place both feet securely under your knees and let your carer take the agreed guarding position. Tap completed step when everyone is ready.", "target": None, "hold_ms": 0},
            {"caption": "Lean forward and stand", "voice": "Bring your nose gently over your toes, push through both feet, and stand with your carer's support. Take your time. Tap completed step only when you are safely upright.", "target": None, "hold_ms": 0},
            {"caption": "Pause in supported standing", "voice": "Pause while standing tall with your support. Make sure you feel steady before continuing. Tap completed step when you are ready to sit.", "target": None, "hold_ms": 0},
            {"caption": "Sit down with control", "voice": "Reach back for the chair if that is part of your therapist's method, bend at your hips and knees, and lower slowly with support. Excellent work. Tap completed step when you are safely seated.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Strong work. You completed the full stand and controlled return. Keep using the agreed assistance and a steady, unhurried rhythm."},
        ],
    },
    "ex_supported_stand": {
        "name": "Supported Standing Alignment",
        "reps": 3,
        "pose_mode": "guided",
        "setup_voice": "Have a therapist or capable carer beside you and use a fixed support. Do not practise this alone if you have any risk of falling.",
        "cycle": [
            {"caption": "Prepare your support", "voice": "Place both feet securely, hold the fixed support as instructed, and ask your carer to guard you. Tap completed step when you are ready.", "target": None, "hold_ms": 0},
            {"caption": "Stand tall and aligned", "voice": "Come into supported standing. Keep your chest tall, both knees comfortably aligned, and your weight through both feet. Tap completed step when you are steady.", "target": None, "hold_ms": 0},
            {"caption": "Return to the chair", "voice": "With your carer's help, lower yourself back to the chair slowly and safely. Well done. Tap completed step when you are seated.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Excellent supported standing practice. On the next repetition, keep your body tall and share your weight as evenly as you safely can."},
        ],
    },
    "ex_supported_step": {
        "name": "Guarded Step Initiation",
        "reps": 4,
        "pose_mode": "guided",
        "setup_voice": "Practise only with close guarding and a fixed support. Keep the floor clear, wear secure footwear, and use your usual walking aid if your therapist has advised it.",
        "cycle": [
            {"caption": "Find a steady starting stance", "voice": "Stand with your support and carer in position. Settle your weight through both feet. Tap completed step when you feel steady.", "target": None, "hold_ms": 0},
            {"caption": "Shift and begin the step", "voice": "Shift your weight safely onto the supporting leg, then gently lift and move the affected foot forward. Tap completed step when the foot is placed securely.", "target": None, "hold_ms": 0},
            {"caption": "Return to your starting stance", "voice": "Bring the affected foot back with control and regain an even, supported stance. Good work. Tap completed step when you are steady again.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Well done initiating the step. Keep the movement small, controlled, and fully supported on the next repetition."},
        ],
    },
    "ex_weight_shift": {
        "name": "Supported Affected-Side Weight Shift",
        "reps": 5,
        "pose_mode": "guided",
        "setup_voice": "Use a fixed support with a therapist or capable carer guarding you. We will make a small, controlled shift toward your affected side and return to the middle.",
        "cycle": [
            {"caption": "Stand safely in the middle", "voice": "Set both feet securely and stand tall with your support. Tap completed step when your carer confirms you are steady.", "target": None, "hold_ms": 0},
            {"caption": "Shift toward the affected side", "voice": "Move your pelvis gently toward your affected foot while keeping both feet down. Keep the movement small and comfortable. Tap completed step when you have paused there safely.", "target": None, "hold_ms": 0},
            {"caption": "Return to the middle", "voice": "Slowly bring your weight back to the middle and settle evenly over both feet. Nicely controlled. Tap completed step when you are steady.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Great controlled weight shift. On the next repetition, stay tall and move from your pelvis rather than leaning your shoulders."},
        ],
    },
    "ex_sitting_balance": {
        "name": "Guarded Sitting Midline and Reach",
        "reps": 5,
        "pose_mode": "guided",
        "setup_voice": "Sit in a stable chair with both feet supported and a carer guarding your affected side if needed. We will practise a small reach and a controlled return to the middle.",
        "cycle": [
            {"caption": "Find your upright middle", "voice": "Sit tall with your shoulders level and both feet supported. Tap completed step when you feel balanced in the middle.", "target": None, "hold_ms": 0},
            {"caption": "Make a small safe reach", "voice": "Reach a short distance toward the affected side within your comfortable balance. Keep your hips on the chair. Tap completed step when you have paused safely.", "target": None, "hold_ms": 0},
            {"caption": "Return to the middle", "voice": "Bring your trunk and hand back to the upright middle slowly. Excellent recovery. Tap completed step when you are balanced again.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Wonderful sitting control. Keep the next reach small and smooth, and finish each repetition fully back in the middle."},
        ],
    },
    "ex_step_stance": {
        "name": "Supported Step-Stance Control",
        "reps": 3,
        "pose_mode": "guided",
        "setup_voice": "Use a fixed support and close guarding. Only practise this after supported standing has been confirmed as safe by your therapist.",
        "cycle": [
            {"caption": "Prepare a stable stance", "voice": "Stand with both feet secure, your support in hand, and your carer beside you. Tap completed step when you are ready.", "target": None, "hold_ms": 0},
            {"caption": "Step into a short stance", "voice": "Move one foot a short distance forward as instructed by your therapist. Keep both knees comfortable and your body tall. Tap completed step when the stance is secure.", "target": None, "hold_ms": 0},
            {"caption": "Hold with support", "voice": "Pause in the short step stance while keeping your support. Breathe normally. Tap completed step when you are ready to return.", "target": None, "hold_ms": 0},
            {"caption": "Return to an even stance", "voice": "Bring the forward foot back slowly and settle your weight evenly. Excellent work. Tap completed step when you are steady.", "target": None, "hold_ms": 0},
        ],
        "feedback_rules": [
            {"default": "Excellent step-stance control. Keep using close support and make the next step only as large as you can control safely."},
        ],
    },
    "ex_maintenance": {
        "name": "Maintenance Conditioning",
        "reps": 4,
        "pose_mode": "body",
        "setup_voice": "We will do gentle full-range maintenance work. Reach the target, then return — focusing on smooth, full motion.",
        "cycle": [
            {"caption": "Reach the target", "voice": "Reach to the target with smooth motion.", "target": {"x": 0.5, "y": 0.30, "r": 0.12}, "hold_ms": 1200},
            {"caption": "Return", "voice": "Return to your starting position.", "target": {"x": 0.5, "y": 0.78, "r": 0.12}, "hold_ms": 1200},
        ],
        "feedback_rules": [
            {"default": "Excellent. Maintain the same quality on the next repetition."},
        ],
    },
}


# Every exercise uses the same automatic calibration, overlay geometry, and
# auditable 0-100 scoring contract. Exercise-specific instructions and joints
# differ, but the patient experience and calculation rules do not.
EXERCISE_CALIBRATION_CONTRACT: Dict[str, Any] = {
    "minimum_baseline_samples": 45,
    "minimum_tracking_quality": 0.72,
    "maximum_anchor_drift": 0.035,
    "lap_minimum_samples": 8,
    "lap_minimum_duration_ms": 650,
    "lap_stable_sample_ratio": 0.70,
}

EXERCISE_OVERLAY_STYLE: Dict[str, Any] = {
    "keypoint_color": "#D9E5DC",
    "keypoint_radius_px": 3,
    "connector_color": "#4A7856",
    "connector_width_px": 4,
    # Match the initial assessment's lighter, finer hand skeleton. Pose and
    # hand landmarks deliberately use different weights so the fingers remain
    # readable without covering the patient's hand.
    "hand_keypoint_color": "rgba(217,229,220,0.72)",
    "hand_keypoint_radius_px": 1.4,
    "hand_connector_color": "rgba(127,229,163,0.88)",
    "hand_connector_width_px": 2,
    "target_color": "#E18E6D",
    "target_edge_width_px": 6,
    "target_inner_scale": 0.55,
    "hold_ring_color": "#3C8255",
    "hold_ring_scale": 1.25,
    "hold_ring_width_px": 8,
    "calibration_target_color": "#7FE5A3",
    "calibration_target_fill": "rgba(74,120,86,.28)",
}

# Movement-phase tagging for scoring: only the active movement of a
# repetition is scored. Return-to-rest frames are excluded so a straight elbow
# at rest cannot claim "elbow extension", and upright resting frames cannot
# dilute the confirmation of a compensation that lasted the whole reach.
_RETURN_STEP_PATTERN = re.compile(r"\b(return|lower)\b", re.IGNORECASE)
# Exercises whose confirmed compensations are shown to the patient as a
# temporary red-dotted still after the score (deleted on continue).
TEMPORARY_EVIDENCE_EXERCISE_IDS = {"ex_reach", "ex_grasp", "ex_h2m"}
for _runner in REHAB_RUNNER_CONFIG.values():
    for _step in _runner.get("cycle") or []:
        _is_return = (
            ((_step.get("target") or {}).get("landmark") == "LAP_DYNAMIC")
            or bool(_RETURN_STEP_PATTERN.search(str(_step.get("caption") or "")))
        )
        _step.setdefault("phase", "return" if _is_return else "movement")


EXERCISE_SCORING_METHOD: Dict[str, Any] = {
    "scale_min": 0,
    "scale_max": 100,
    "minimum_scored_frames": 8,
    "formula": "weighted mean of capped ROM-attainment percentages minus confirmed compensation penalties",
    "rom_cap_percent": 100,
    "compensation_confirmation": "minimum frame count and minimum eligible-frame ratio must both be met",
    # A repetition completed through a confirmed compensatory pattern (trunk
    # lean, shoulder hiking, ...) is scored exactly this, whatever the ROM says.
    "compensation_score": 70,
    # A repetition earns its point only when done correctly: no confirmed
    # compensation, every movement step within complete_rom_ratio of today's
    # target (e.g. the elbow really straightened), and a score at or above
    # this threshold. An incomplete repetition is capped just below it.
    "point_threshold": 90,
    "complete_rom_ratio": 0.9,
    # A compensation held for about a second of movement (24 frames) is
    # confirmed even if it was a smaller share of a long repetition.
    "sustained_compensation_frames": 24,
    # Camera angles read a little low for a reach toward the lens, so reaching
    # within 5% of today's target counts as full attainment for that step.
    "full_credit_ratio": 0.95,
    # Raising the arm lifts the shoulder a little by itself: the collarbone
    # elevates and the shoulder blade rotates upward as the arm goes up (normal
    # scapulohumeral rhythm), and the camera's shoulder point rides up with the
    # deltoid. That is not hiking, so the shrug threshold grows by this many
    # degrees for every degree of shoulder flexion beyond the first
    # "free" degrees above the resting angle: a reach to 60 degrees tolerates
    # about 4 degrees more shoulder rise than a shrug at rest does, a reach to
    # 90 about 7. A shrug at the start of the reach, before the arm is up, is
    # judged against the plain threshold.
    "shoulder_hike_allowance_per_flexion_deg": 0.10,
    "shoulder_hike_allowance_free_flexion_deg": 10,
    "quality_boundary": "camera-derived coaching score; not a diagnosis or laboratory motion-capture measurement",
}

# Assisted completions: when the patient confirms a carer or family member
# helped during the exercise, the session score is halved before it is stored,
# so the functional-domain score (upper limb, hand, lower limb) reflects
# independent ability rather than the helper's.
ASSISTED_SCORE_FACTOR = 0.5

EXERCISE_FUNCTIONAL_DOMAINS: Dict[str, str] = {
    "ex_reach": "upper_limb",
    "ex_trunk": "upper_limb",
    "ex_wallslide": "upper_limb",
    "ex_scapdepress": "upper_limb",
    "ex_h2m": "upper_limb",
    "ex_grasp": "upper_limb",
    "ex_bilateral": "upper_limb",
    "ex_maintenance": "upper_limb",
    "ex_handopen": "hand",
    "ex_pinch": "hand",
    "ex_lower_selective": "lower_limb",
    "ex_ankle_dorsiflexion": "lower_limb",
    "ex_sit_to_stand": "lower_limb",
    "ex_supported_stand": "lower_limb",
    "ex_supported_step": "lower_limb",
    "ex_weight_shift": "lower_limb",
    "ex_sitting_balance": "lower_limb",
    "ex_step_stance": "lower_limb",
}

EXERCISE_COACHING_PROFILES: Dict[str, Dict[str, Any]] = {
    "ex_reach": {
        "training_focus": "Forward reach using shoulder flexion and elbow extension while the trunk and shoulder girdle remain controlled.",
        "repetition_definition": "Affected hand leaves the calibrated lap point, reaches and holds the forward target, then returns to the same lap point.",
        "rom_cues": {
            "shoulder_flexion": "On the next repetition, let the affected arm travel a little farther forward while your chest stays tall.",
            "elbow_extension": "On the next repetition, gently straighten the elbow a little more as the hand approaches the target.",
        },
        "compensation_labels": {"trunk_lean": "Excess trunk lean", "shoulder_hike": "Shoulder hiking"},
        "measurement_limit": "The camera estimates joint angles and posture; it does not measure strength, pain, or shoulder loading.",
    },
    "ex_trunk": {
        "training_focus": "Arm reach with reduced trunk substitution and maintained chair-back contact.",
        "repetition_definition": "Reach to and hold the forward target without leaving the supported trunk position, then return to the calibrated start point.",
        "rom_cues": {
            "shoulder_flexion": "On the next repetition, keep your back settled and move the arm a little farther from the shoulder.",
            "elbow_extension": "On the next repetition, keep chair-back contact and gently straighten the elbow toward the target.",
        },
        "compensation_labels": {"trunk_lean": "Excess trunk lean", "shoulder_hike": "Shoulder hiking"},
        "measurement_limit": "Camera posture is a proxy for trunk restraint; the system cannot verify physical contact pressure against the chair.",
    },
    "ex_wallslide": {
        "training_focus": "Pain-free supported shoulder elevation with a relaxed shoulder and centred trunk.",
        "repetition_definition": "Slide the supported affected arm to the upper target, hold, then return to the calibrated supported start point.",
        "rom_cues": {
            "shoulder_flexion": "On the next repetition, let the support carry the arm while you slide slightly higher without pain.",
            "elbow_extension": "On the next repetition, keep the forearm supported and the elbow gently lengthened rather than forcing it straight.",
        },
        "compensation_labels": {"shoulder_hike": "Shoulder hiking", "side_lean": "Trunk side lean"},
        "measurement_limit": "The camera cannot detect pain or verify the amount of table or wall support.",
    },
    "ex_scapdepress": {
        "training_focus": "Scapular depression and a short controlled reach without trunk substitution.",
        "repetition_definition": "Set the affected shoulder away from the ear, reach to and hold the target, then return to the calibrated start point.",
        "rom_cues": {
            "shoulder_flexion": "Before the next repetition, settle the shoulder down, then reach a little farther without lifting it.",
            "elbow_extension": "On the next repetition, lengthen through the elbow while the shoulder stays relaxed.",
        },
        "compensation_labels": {"shoulder_hike": "Shoulder hiking", "trunk_lean": "Trunk lean"},
        "measurement_limit": "Shoulder-height asymmetry is a visual proxy and does not directly measure scapular muscle activation.",
    },
    "ex_h2m": {
        "training_focus": "Coordinated elbow flexion and shoulder lift for hand-to-mouth activity without moving the trunk toward the hand.",
        "repetition_definition": "Reach to and touch the on-screen cup, bring it to the mouth target, hold briefly as if sipping, put the cup back, then return the empty hand to the calibrated lap/start point.",
        "rom_cues": {
            "elbow_flexion": "On the next repetition, bend the elbow a little more so the hand comes to the mouth instead of the mouth moving forward.",
            "shoulder_flexion": "On the next repetition, add only the small comfortable shoulder lift needed to guide the hand upward.",
        },
        "compensation_labels": {"trunk_forward": "Forward trunk substitution", "head_drop": "Head dropped to the hand", "shoulder_hike": "Shoulder hiking"},
        "measurement_limit": "The runner scores joint motion and posture; it does not verify swallowing safety or the contents of a cup.",
    },
    "ex_grasp": {
        "training_focus": "Reach, cylindrical grasp, low lift, cross-table transport, controlled release, and return.",
        "repetition_definition": "Reach to the light object, transport it across the table, place and release it, then return the empty hand to the calibrated start point.",
        "rom_cues": {
            "elbow_extension": "On the next repetition, straighten your elbow as your hand arrives at the cup.",
            "shoulder_flexion": "On the next repetition, bring the arm to the object without taking your chest with it.",
            "shoulder_abduction": "On the next repetition, move the light object across with the arm while both shoulders stay square.",
            "hand_opening": "On the next repetition, open your fingers a little wider before you take the cup.",
        },
        "compensation_labels": {"trunk_lean": "Trunk lean", "trunk_side_lean": "Trunk side lean", "shoulder_hike": "Shoulder hiking", "elbow_flare": "Elbow flare (arm abduction)", "wrist_flexion": "Wrist alignment while gripping"},
        "measurement_limit": "The camera score reflects arm transport, posture, and how far the fingers open and close around the on-screen cup; it cannot confirm grip force.",
    },
    "ex_handopen": {
        "training_focus": "Active finger extension and controlled release with the forearm supported and wrist near neutral.",
        "repetition_definition": "Open the affected fingers as far as comfortable, hold briefly, release/relax, then confirm the repetition.",
        "rom_cues": {
            "finger_extension": "On the next repetition, keep the wrist steady and open the fingers a little wider, then hold for one comfortable second.",
        },
        "compensation_labels": {"wrist_flexion": "Wrist flexion substitution"},
        "measurement_limit": "Single-camera hand angles are coaching estimates and do not measure grip strength, tone, or passive range.",
    },
    "ex_pinch": {
        "training_focus": "Thumb-to-finger opposition, controlled small-object placement, and release with a stable wrist.",
        "repetition_definition": "Pinch one object, move it to the container, release it, then confirm the placement.",
        "rom_cues": {
            "pinch_flexion": "On the next repetition, keep the wrist quiet and bring the thumb and finger pads together with a smaller, slower motion.",
        },
        "compensation_labels": {"wrist_flexion": "Wrist flexion substitution"},
        "measurement_limit": "The camera estimates finger motion but cannot verify pinch force or whether the object was securely placed.",
    },
    "ex_bilateral": {
        "training_focus": "Synchronous two-arm motion with similar range and a centred trunk.",
        "repetition_definition": "Bring both hands together at the target, then open both arms outward through a matched range.",
        "rom_cues": {
            "bilateral_shoulder_flexion": "On the next repetition, slow down and move both arms through the same comfortable height and distance.",
        },
        "compensation_labels": {"arm_asymmetry": "Unequal arm range", "trunk_lean": "Trunk lean"},
        "measurement_limit": "The score compares visible joint motion; it does not measure force contribution from each arm.",
    },
    "ex_lower_selective": {
        "training_focus": "Selective affected-knee extension and controlled lowering while the thigh, pelvis, and trunk remain stable.",
        "repetition_definition": "Begin with both feet supported, straighten the affected knee within comfort, then lower the foot under control.",
        "rom_cues": {
            "knee_extension": "On the next repetition, keep the thigh supported and slowly straighten the knee a little farther before lowering.",
        },
        "compensation_labels": {"hip_hike": "Pelvic hiking", "trunk_lean": "Backward or side trunk lean"},
        "measurement_limit": "The camera estimates knee angle; it does not measure quadriceps force or chair pressure.",
    },
    "ex_ankle_dorsiflexion": {
        "training_focus": "Ankle dorsiflexion/toe lift with the heel planted and knee held steady.",
        "repetition_definition": "Set the heel and knee, lift the toes and forefoot, hold briefly, then lower slowly to the floor.",
        "rom_cues": {
            "ankle_dorsiflexion": "On the next repetition, keep the heel heavy and lift the toes and forefoot a little higher before lowering slowly.",
        },
        "compensation_labels": {"heel_lift": "Heel lift", "knee_motion": "Knee substitution"},
        "measurement_limit": "The toe-lift angle is a 2D camera estimate and can be affected by camera placement and footwear.",
    },
    "ex_sit_to_stand": {
        "training_focus": "Sequenced forward translation, bilateral hip/knee extension, upright stabilization, and controlled sitting.",
        "repetition_definition": "Prepare safely, rise to supported standing, stabilize, then lower to the chair with the same agreed assistance.",
        "rom_cues": {
            "hip_extension": "On the next repetition, bring your hips forward into the agreed supported upright position before you settle.",
            "knee_extension": "On the next repetition, press through both feet and let both knees straighten together without locking them.",
        },
        "compensation_labels": {"uneven_loading": "Uneven limb loading proxy", "trunk_side_lean": "Lateral trunk lean"},
        "measurement_limit": "Pose symmetry is not a force-plate measurement; support and hands-on guarding must not be reduced based on the score.",
    },
    "ex_supported_stand": {
        "training_focus": "Supported upright hip/knee alignment, midline trunk control, and stable return to sitting.",
        "repetition_definition": "Prepare with fixed support and guarding, stand in alignment, hold safely, then return to the chair.",
        "rom_cues": {
            "hip_extension": "On the next repetition, use the same support and bring the hips gently toward upright without leaning back.",
            "knee_extension": "On the next repetition, keep both knees comfortably straight and pointing with the feet while you hold.",
        },
        "compensation_labels": {"trunk_side_lean": "Lateral trunk lean", "knee_collapse": "Knee alignment loss"},
        "measurement_limit": "The camera cannot measure weight distribution, support force, or fall risk; guarding remains mandatory.",
    },
    "ex_supported_step": {
        "training_focus": "Safe weight shift, affected-foot clearance and placement, then controlled return to stance.",
        "repetition_definition": "Stabilize with support, advance and place the affected foot, then return it and regain the starting stance.",
        "rom_cues": {
            "hip_flexion": "On the next repetition, keep the pelvis level and make a small clear step from the hip.",
            "knee_flexion": "On the next repetition, bend the affected knee enough for safe toe clearance without making the step larger.",
        },
        "compensation_labels": {"hip_hike": "Pelvic hiking", "trunk_lean": "Trunk lean"},
        "measurement_limit": "The camera cannot confirm ground reaction forces or safety of the walking aid; close guarding remains unchanged.",
    },
    "ex_weight_shift": {
        "training_focus": "Controlled pelvic translation toward the affected side with aligned knee and minimal shoulder-led lean.",
        "repetition_definition": "Begin in supported midline, shift the pelvis toward the affected foot, pause, then return to the middle.",
        "rom_cues": {
            "pelvic_shift": "On the next repetition, keep the movement small and guide the pelvis a little farther toward the affected foot.",
        },
        "compensation_labels": {"shoulder_lean": "Shoulder-led lean", "knee_collapse": "Knee alignment loss"},
        "measurement_limit": "Pelvic position is only a visual proxy for weight bearing; the camera does not measure force under either foot.",
    },
    "ex_sitting_balance": {
        "training_focus": "Controlled seated lateral reach and accurate return to midline while both hips remain supported.",
        "repetition_definition": "Establish upright midline, make a small guarded reach, then return fully to the calibrated middle.",
        "rom_cues": {
            "trunk_lateral_rom": "On the next repetition, make a small smooth reach, then finish with your chest precisely back in the middle.",
        },
        "compensation_labels": {"hip_lift": "Loss of hip contact", "rotation": "Trunk rotation"},
        "measurement_limit": "The camera estimates trunk motion but cannot measure seat pressure or protective balance reactions.",
    },
    "ex_step_stance": {
        "training_focus": "Short supported step placement, stance control, knee alignment, and controlled return.",
        "repetition_definition": "Prepare with support, enter the therapist-approved short step stance, hold, then return to an even stance.",
        "rom_cues": {
            "hip_flexion": "On the next repetition, place the foot only as far forward as you can control with the same support.",
            "knee_flexion": "On the next repetition, use a small comfortable knee bend so the step clears and lands softly.",
        },
        "compensation_labels": {"trunk_lean": "Trunk lean", "knee_collapse": "Front-knee alignment loss"},
        "measurement_limit": "The camera cannot determine fall risk or support force; therapist-approved stance and guarding remain unchanged.",
    },
    "ex_maintenance": {
        "training_focus": "Comfortable maintenance of shoulder and elbow range with smooth posture-controlled reaching.",
        "repetition_definition": "Reach to and hold the target through a comfortable range, then return to the calibrated start point.",
        "rom_cues": {
            "shoulder_flexion": "On the next repetition, move through your full comfortable shoulder range while your chest stays tall.",
            "elbow_extension": "On the next repetition, gently lengthen the elbow toward the target without forcing the joint.",
        },
        "compensation_labels": {"trunk_lean": "Trunk lean", "shoulder_hike": "Shoulder hiking"},
        "measurement_limit": "This camera score tracks visible range and posture only; it does not measure resistance, strength, or fatigue.",
    },
}


# Camera-derived movement guidance standards. Targets are conservative 2D ROM
# goals for coaching and progress tracking, not diagnostic measurements.
EXERCISE_MOVEMENT_STANDARDS: Dict[str, Dict[str, Any]] = {
    "ex_reach": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Before we begin, sit still with your affected hand resting on the visible part of your lap. Keep your face, shoulders, affected arm, and the top of your affected thigh in view. You do not need to show your knees or your full lap. I will locate your lap target for this exercise.",
        "rom_steps": [
            {"id": "shoulder_flexion", "label": "Shoulder reach", "metric": "shoulder_flexion", "targets": {"easy": 45, "medium": 60, "difficult": 75}, "weight": 0.65},
            {"id": "elbow_extension", "label": "Elbow extension", "metric": "elbow_extension", "targets": {"easy": 130, "medium": 140, "difficult": 150}, "weight": 0.35},
        ],
        "compensations": [
            {"id": "trunk_lean", "metric": "trunk_lean_delta", "threshold_deg": 12, "min_frames": 8, "min_ratio": 0.35, "penalty": 10, "correction": "Keep your chest tall and let your arm travel toward the target."},
            # Up to and including 20 degrees of shoulder rise is accepted as
            # normal during Graded Forward Reach. This avoids treating the
            # ordinary scapular movement that accompanies arm elevation as a
            # compensatory shoulder hike.
            {"id": "shoulder_hike", "metric": "shoulder_hike_delta", "threshold_deg": 20, "normal_max_inclusive": True, "normal_rise_allowance_per_elevation_deg": 0, "min_frames": 8, "min_ratio": 0.35, "penalty": 8, "correction": "Relax the shoulder away from your ear before you reach again."},
        ],
    },
    "ex_trunk": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Before we begin, sit with your back supported and place your affected hand on the visible upper part of your thigh on the same side. Keep your face, both shoulders, both hips, your affected arm, and that upper thigh in view. Hold still while I learn your upright posture and lap return point.",
        "rom_steps": [
            {"id": "shoulder_flexion", "label": "Restrained shoulder reach", "metric": "shoulder_flexion", "targets": {"easy": 40, "medium": 55, "difficult": 65}, "weight": 0.7},
            {"id": "elbow_extension", "label": "Elbow extension", "metric": "elbow_extension", "targets": {"easy": 125, "medium": 138, "difficult": 145}, "weight": 0.3},
        ],
        "compensations": [
            # Allow small posture shifts and require sustained, corroborated
            # movement; a shrug must not become a trunk-lean finding.
            {"id": "trunk_lean", "metric": "trunk_lean_delta", "threshold_deg": 12, "min_frames": 12, "min_ratio": 0.45, "min_consecutive_frames": 8, "penalty": 12, "correction": "Settle your back against the chair before the next reach."},
            {"id": "shoulder_hike", "metric": "shoulder_hike_delta", "threshold_deg": 8, "min_frames": 8, "min_ratio": 0.35, "penalty": 7, "correction": "Soften the top of your shoulder and keep it away from your ear."},
        ],
    },
    "ex_wallslide": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Sit tall with your supported forearm, shoulders, hips, and wrists visible. Hold the comfortable starting position without pain.",
        "rom_steps": [
            {"id": "shoulder_flexion", "label": "Supported arm elevation", "metric": "shoulder_flexion", "targets": {"easy": 60, "medium": 80, "difficult": 95}, "weight": 0.8},
            {"id": "elbow_extension", "label": "Supported elbow position", "metric": "elbow_extension", "targets": {"easy": 120, "medium": 132, "difficult": 140}, "weight": 0.2},
        ],
        "compensations": [
            {"id": "shoulder_hike", "metric": "shoulder_hike_delta", "threshold_deg": 8, "min_frames": 8, "min_ratio": 0.35, "penalty": 10, "correction": "Keep the shoulder heavy and away from your ear as the arm slides."},
            {"id": "side_lean", "metric": "trunk_lean_delta", "threshold_deg": 10, "min_frames": 8, "min_ratio": 0.35, "penalty": 9, "correction": "Return your chest to the middle before raising the arm again."},
        ],
    },
    "ex_scapdepress": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Sit upright with both shoulders level and your hands resting. Keep your face, shoulders, hips, elbows, and wrists in view.",
        "rom_steps": [
            {"id": "shoulder_flexion", "label": "Controlled shoulder reach", "metric": "shoulder_flexion", "targets": {"easy": 35, "medium": 50, "difficult": 60}, "weight": 0.65},
            {"id": "elbow_extension", "label": "Elbow extension", "metric": "elbow_extension", "targets": {"easy": 125, "medium": 138, "difficult": 145}, "weight": 0.35},
        ],
        "compensations": [
            {"id": "shoulder_hike", "metric": "shoulder_hike_delta", "threshold_deg": 6, "min_frames": 8, "min_ratio": 0.35, "penalty": 12, "correction": "Draw the shoulder gently down before beginning the next reach."},
            {"id": "trunk_lean", "metric": "trunk_lean_delta", "threshold_deg": 10, "min_frames": 8, "min_ratio": 0.35, "penalty": 8, "correction": "Stay tall through your chest while your arm moves."},
        ],
    },
    "ex_h2m": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Before we begin, sit tall with your affected hand resting on the visible part of your lap. Keep your face, both shoulders, your affected arm and the top of your affected thigh in view, and hold still while I learn your upright position.",
        "rom_steps": [
            {"id": "elbow_flexion", "label": "Elbow bend", "metric": "elbow_flexion", "targets": {"easy": 65, "medium": 80, "difficult": 95}, "weight": 0.7},
            {"id": "shoulder_flexion", "label": "Shoulder lift", "metric": "shoulder_flexion", "targets": {"easy": 20, "medium": 30, "difficult": 40}, "weight": 0.3},
        ],
        # Hand-to-mouth substitutions: the mouth coming to the hand (trunk leaning
        # forward, or the head dropping - neck flexion measured from the nose
        # falling below the ear line), and hiking the shoulder to lift the arm.
        "compensations": [
            {"id": "trunk_forward", "metric": "trunk_lean_delta", "threshold_deg": 10, "min_frames": 8, "min_ratio": 0.35, "penalty": 11, "correction": "Bring your hand toward your mouth instead of moving your mouth toward your hand."},
            {"id": "head_drop", "metric": "head_drop_deg", "threshold_deg": 15, "min_frames": 8, "min_ratio": 0.35, "penalty": 10, "steps": [1, 2], "correction": "Keep your head up and bring the cup all the way to your lips."},
            {"id": "shoulder_hike", "metric": "shoulder_hike_delta", "threshold_deg": 8, "min_frames": 8, "min_ratio": 0.35, "penalty": 8, "correction": "Relax the shoulder before bending the elbow again."},
        ],
    },
    "ex_grasp": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Sit square to the camera with both shoulders, hips, elbows, and wrists visible. The cup you will move is shown on the screen - no real object is needed. Rest your hands and hold still.",
        "rom_steps": [
            # Each metric is measured only in the steps where it matters ("steps" are
            # cycle indices: 0 reach, 1 open, 2 close, 3 carry, 4 release, 5 return).
            # Reaching the cup: the elbow must straighten (the classic post-stroke
            # shortfall - the arm stays flexed and the trunk is used instead), judged at
            # the top of the reach.
            {"id": "elbow_extension", "label": "Elbow extension at the cup", "metric": "elbow_extension", "targets": {"easy": 118, "medium": 130, "difficult": 142}, "weight": 0.30, "steps": [0, 1, 2]},
            {"id": "shoulder_flexion", "label": "Reach to the object", "metric": "shoulder_flexion", "targets": {"easy": 30, "medium": 42, "difficult": 52}, "weight": 0.20, "steps": [0, 1, 2]},
            # Hand opening around the on-screen cup (median finger angle, 180 = straight).
            {"id": "hand_opening", "label": "Hand opening", "metric": "finger_extension", "targets": {"easy": 115, "medium": 130, "difficult": 145}, "weight": 0.25, "steps": [1]},
            {"id": "shoulder_abduction", "label": "Controlled transport", "metric": "shoulder_abduction", "targets": {"easy": 25, "medium": 35, "difficult": 45}, "weight": 0.25, "steps": [3, 4]},
        ],
        "compensations": [
            # Forward trunk lean to make up for the reach (all movement steps).
            {"id": "trunk_lean", "metric": "trunk_lean_delta", "threshold_deg": 12, "min_frames": 8, "min_ratio": 0.35, "penalty": 10, "correction": "Keep your shoulders square and move the light object with your arm."},
            # Side lean / rotation to get the cup across instead of moving the arm (carry and release).
            {"id": "trunk_side_lean", "metric": "trunk_side_lean_delta", "threshold_deg": 10, "min_frames": 8, "min_ratio": 0.3, "penalty": 8, "correction": "Keep your body upright and carry the cup across with your arm.", "steps": [3, 4]},
            # Use the same inclusive normal band as Graded Forward Reach: the
            # ordinary shoulder rise that accompanies lifting and carrying the
            # cup is normal through 20 degrees.
            {"id": "shoulder_hike", "metric": "shoulder_hike_delta", "threshold_deg": 20, "normal_max_inclusive": True, "normal_rise_allowance_per_elevation_deg": 0, "min_frames": 12, "min_ratio": 0.45, "penalty": 7, "correction": "Set the shoulder down before lifting the object again."},
            # "Chicken wing": the arm abducts and the elbow rises above the hand to reach,
            # instead of the arm going forward (reach and grasp steps).
            {"id": "elbow_flare", "metric": "elbow_flare_deg", "threshold_deg": 45, "min_frames": 8, "min_ratio": 0.35, "penalty": 7, "correction": "Keep your elbow low and close to your body, and reach forward with your arm.", "steps": [0, 1, 2]},
            # Prefer the detailed palm axis in the shared camera image. Retain
            # the pose-only estimate for views without a usable palm projection.
            {"id": "wrist_flexion", "metric": "wrist_flexion_delta", "threshold_deg": 25, "min_frames": 12, "min_ratio": 0.4, "penalty": 6, "correction": "Keep your wrist straight, in line with your forearm, as you grip and carry the cup.", "steps": [2, 3, 4]},
        ],
    },
    "ex_handopen": {
        "tracking_mode": "hand", "posture": "seated",
        "calibration_instruction": "Support your forearm and hold your affected hand toward the camera. Keep the whole hand, wrist, and fingertips visible and still.",
        "rom_steps": [
            {"id": "finger_extension", "label": "Finger opening", "metric": "finger_extension", "targets": {"easy": 130, "medium": 145, "difficult": 158}, "weight": 1.0},
        ],
        "compensations": [
            {"id": "wrist_flexion", "metric": "wrist_flexion_delta", "threshold_deg": 18, "min_frames": 8, "min_ratio": 0.4, "penalty": 8, "correction": "Keep the wrist comfortably neutral while the fingers open."},
        ],
    },
    "ex_pinch": {
        "tracking_mode": "hand", "posture": "seated",
        "calibration_instruction": "Support your forearm and hold your affected hand toward the camera. Keep your thumb, index finger, and wrist clearly visible.",
        "rom_steps": [
            {"id": "pinch_flexion", "label": "Thumb and finger control", "metric": "pinch_flexion", "targets": {"easy": 35, "medium": 50, "difficult": 65}, "weight": 1.0},
        ],
        "compensations": [
            {"id": "wrist_flexion", "metric": "wrist_flexion_delta", "threshold_deg": 18, "min_frames": 8, "min_ratio": 0.4, "penalty": 8, "correction": "Keep the wrist steady and let the thumb and finger make the pinch."},
        ],
    },
    "ex_bilateral": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Sit in the middle of the camera with both shoulders, elbows, wrists, and hips visible. Rest both arms and hold still.",
        "rom_steps": [
            {"id": "bilateral_shoulder_flexion", "label": "Both-arm range", "metric": "bilateral_shoulder_flexion", "targets": {"easy": 35, "medium": 50, "difficult": 60}, "weight": 1.0},
        ],
        "compensations": [
            {"id": "arm_asymmetry", "metric": "arm_asymmetry", "threshold_deg": 18, "min_frames": 10, "min_ratio": 0.4, "penalty": 9, "correction": "Slow down and aim to move both arms through a similar range."},
            {"id": "trunk_lean", "metric": "trunk_lean_delta", "threshold_deg": 10, "min_frames": 8, "min_ratio": 0.35, "penalty": 8, "correction": "Return your chest to the middle before moving both arms again."},
        ],
    },
    "ex_lower_selective": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Place the camera where your shoulders, hips, knees, ankles, and feet are visible. Sit still with both feet supported.",
        "rom_steps": [
            {"id": "knee_extension", "label": "Knee extension", "metric": "knee_extension", "targets": {"easy": 125, "medium": 140, "difficult": 152}, "weight": 1.0},
        ],
        "compensations": [
            {"id": "hip_hike", "metric": "hip_hike_delta", "threshold_deg": 8, "min_frames": 8, "min_ratio": 0.35, "penalty": 8, "correction": "Keep both hips settled on the chair while the lower leg moves."},
            {"id": "trunk_lean", "metric": "trunk_lean_delta", "threshold_deg": 10, "min_frames": 8, "min_ratio": 0.35, "penalty": 8, "correction": "Stay tall and avoid leaning back to lift the foot."},
        ],
    },
    "ex_ankle_dorsiflexion": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Place the camera to the side so your affected knee, ankle, heel, and toes are visible. Keep the heel down and hold still.",
        "rom_steps": [
            {"id": "ankle_dorsiflexion", "label": "Ankle dorsiflexion", "metric": "ankle_dorsiflexion", "targets": {"easy": 6, "medium": 10, "difficult": 14}, "weight": 1.0},
        ],
        "compensations": [
            {"id": "heel_lift", "metric": "heel_lift_angle", "threshold_deg": 8, "min_frames": 8, "min_ratio": 0.4, "penalty": 10, "correction": "Keep the heel planted and lift only the toes and forefoot."},
            {"id": "knee_motion", "metric": "knee_motion_delta", "threshold_deg": 10, "min_frames": 8, "min_ratio": 0.4, "penalty": 7, "correction": "Keep the knee quiet while the ankle moves."},
        ],
    },
    "ex_sit_to_stand": {
        "tracking_mode": "pose", "posture": "full_body",
        "calibration_instruction": "With your carer beside you, place the camera where your shoulders, hips, knees, ankles, and chair are visible. Sit still in your safe starting position.",
        "rom_steps": [
            {"id": "hip_extension", "label": "Hip extension into standing", "metric": "hip_extension", "targets": {"easy": 145, "medium": 158, "difficult": 168}, "weight": 0.5},
            {"id": "knee_extension", "label": "Knee extension into standing", "metric": "bilateral_knee_extension", "targets": {"easy": 145, "medium": 158, "difficult": 168}, "weight": 0.5},
        ],
        "compensations": [
            {"id": "uneven_loading", "metric": "body_asymmetry", "threshold_deg": 12, "min_frames": 10, "min_ratio": 0.4, "penalty": 9, "correction": "Use your agreed support and press through both feet as evenly as is safe."},
            {"id": "trunk_side_lean", "metric": "trunk_lean_delta", "threshold_deg": 12, "min_frames": 10, "min_ratio": 0.4, "penalty": 8, "correction": "Keep your chest centred while your carer maintains the same guarding."},
        ],
    },
    "ex_supported_stand": {
        "tracking_mode": "pose", "posture": "full_body",
        "calibration_instruction": "With your carer and fixed support ready, place your full body in view from shoulders to feet. Hold your safe starting stance still.",
        "rom_steps": [
            {"id": "hip_extension", "label": "Upright hip position", "metric": "hip_extension", "targets": {"easy": 145, "medium": 158, "difficult": 168}, "weight": 0.45},
            {"id": "knee_extension", "label": "Supported knee extension", "metric": "bilateral_knee_extension", "targets": {"easy": 145, "medium": 158, "difficult": 168}, "weight": 0.55},
        ],
        "compensations": [
            {"id": "trunk_side_lean", "metric": "trunk_lean_delta", "threshold_deg": 10, "min_frames": 10, "min_ratio": 0.4, "penalty": 8, "correction": "Use the fixed support and bring your chest gently back toward the middle."},
            {"id": "knee_collapse", "metric": "knee_alignment", "threshold_deg": 12, "min_frames": 10, "min_ratio": 0.4, "penalty": 9, "correction": "Keep each knee pointing in the same direction as the foot without forcing it."},
        ],
    },
    "ex_supported_step": {
        "tracking_mode": "pose", "posture": "full_body",
        "calibration_instruction": "With your carer and fixed support in place, show your body from shoulders to feet. Stand still in your safe starting stance.",
        "rom_steps": [
            {"id": "hip_flexion", "label": "Step hip flexion", "metric": "hip_flexion", "targets": {"easy": 15, "medium": 25, "difficult": 32}, "weight": 0.5},
            {"id": "knee_flexion", "label": "Step knee flexion", "metric": "knee_flexion", "targets": {"easy": 20, "medium": 32, "difficult": 42}, "weight": 0.5},
        ],
        "compensations": [
            {"id": "hip_hike", "metric": "hip_hike_delta", "threshold_deg": 9, "min_frames": 8, "min_ratio": 0.35, "penalty": 9, "correction": "Keep the pelvis level and make the step small enough to clear safely."},
            {"id": "trunk_lean", "metric": "trunk_lean_delta", "threshold_deg": 10, "min_frames": 8, "min_ratio": 0.35, "penalty": 8, "correction": "Keep the same support and bring your chest back over your stance."},
        ],
    },
    "ex_weight_shift": {
        "tracking_mode": "pose", "posture": "full_body",
        "calibration_instruction": "With your carer and fixed support ready, show your shoulders, hips, knees, ankles, and feet. Stand still in the middle.",
        "rom_steps": [
            {"id": "pelvic_shift", "label": "Pelvic weight shift", "metric": "pelvic_shift", "targets": {"easy": 5, "medium": 8, "difficult": 11}, "weight": 1.0},
        ],
        "compensations": [
            {"id": "shoulder_lean", "metric": "shoulder_pelvis_mismatch", "threshold_deg": 10, "min_frames": 9, "min_ratio": 0.4, "penalty": 10, "correction": "Move from your pelvis and keep your shoulders over your support."},
            {"id": "knee_collapse", "metric": "knee_alignment", "threshold_deg": 12, "min_frames": 9, "min_ratio": 0.4, "penalty": 8, "correction": "Keep the knee aligned with the foot during the small shift."},
        ],
    },
    "ex_sitting_balance": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "With your carer nearby if needed, show your shoulders, hips, knees, and both feet. Sit still in your balanced middle position.",
        "rom_steps": [
            {"id": "trunk_lateral_rom", "label": "Controlled sitting reach", "metric": "trunk_lateral_rom", "targets": {"easy": 6, "medium": 10, "difficult": 14}, "weight": 1.0},
        ],
        "compensations": [
            {"id": "hip_lift", "metric": "hip_hike_delta", "threshold_deg": 9, "min_frames": 8, "min_ratio": 0.35, "penalty": 9, "correction": "Keep both hips supported on the chair and make the reach smaller."},
            {"id": "rotation", "metric": "shoulder_rotation", "threshold_deg": 14, "min_frames": 8, "min_ratio": 0.35, "penalty": 7, "correction": "Keep both shoulders facing the camera as you return to the middle."},
        ],
    },
    "ex_step_stance": {
        "tracking_mode": "pose", "posture": "full_body",
        "calibration_instruction": "With your carer and fixed support in place, show your body from shoulders to feet. Hold your steady starting stance.",
        "rom_steps": [
            {"id": "hip_flexion", "label": "Step placement", "metric": "hip_flexion", "targets": {"easy": 12, "medium": 20, "difficult": 28}, "weight": 0.55},
            {"id": "knee_flexion", "label": "Controlled knee movement", "metric": "knee_flexion", "targets": {"easy": 18, "medium": 28, "difficult": 38}, "weight": 0.45},
        ],
        "compensations": [
            {"id": "trunk_lean", "metric": "trunk_lean_delta", "threshold_deg": 10, "min_frames": 9, "min_ratio": 0.4, "penalty": 8, "correction": "Keep the stance short and your chest centred over the fixed support."},
            {"id": "knee_collapse", "metric": "knee_alignment", "threshold_deg": 12, "min_frames": 9, "min_ratio": 0.4, "penalty": 9, "correction": "Keep the front knee pointing in the same direction as the foot."},
        ],
    },
    "ex_maintenance": {
        "tracking_mode": "pose", "posture": "seated",
        "calibration_instruction": "Sit tall with your face, shoulders, hips, elbows, and wrists visible. Rest your hands and hold still.",
        "rom_steps": [
            {"id": "shoulder_flexion", "label": "Shoulder range", "metric": "shoulder_flexion", "targets": {"easy": 45, "medium": 65, "difficult": 80}, "weight": 0.65},
            {"id": "elbow_extension", "label": "Elbow extension", "metric": "elbow_extension", "targets": {"easy": 130, "medium": 142, "difficult": 150}, "weight": 0.35},
        ],
        "compensations": [
            {"id": "trunk_lean", "metric": "trunk_lean_delta", "threshold_deg": 12, "min_frames": 8, "min_ratio": 0.35, "penalty": 9, "correction": "Return to a tall position and let the arm do the work."},
            {"id": "shoulder_hike", "metric": "shoulder_hike_delta", "threshold_deg": 9, "min_frames": 8, "min_ratio": 0.35, "penalty": 7, "correction": "Relax the shoulder before the next movement."},
        ],
    },
}


SESSION_DIFFICULTY_PRESETS: Dict[str, Dict[str, float]] = {
    "easy": {
        "rep_factor": 0.7,
        "set_delta": -1,
        "target_y_delta": 0.06,
        "target_distance_scale": 0.85,
        "radius_scale": 1.2,
        "hold_scale": 0.8,
    },
    "medium": {
        "rep_factor": 1.0,
        "set_delta": 0,
        "target_y_delta": 0.0,
        "target_distance_scale": 1.0,
        "radius_scale": 1.0,
        "hold_scale": 1.0,
    },
    "difficult": {
        "rep_factor": 1.15,
        "set_delta": 1,
        "target_y_delta": -0.04,
        "target_distance_scale": 1.15,
        "radius_scale": 0.9,
        "hold_scale": 1.15,
    },
}

SUPERVISED_EXERCISE_IDS = {
    "ex_sit_to_stand",
    "ex_supported_stand",
    "ex_supported_step",
    "ex_weight_shift",
    "ex_step_stance",
}

EXERCISE_RUNNER_ALIASES = {
    "demo_supported_reach": "ex_reach",
    "demo_hand_opening": "ex_handopen",
}

EXERCISE_SESSION_RULES: Dict[str, Dict[str, str]] = {
    "ex_reach": {
        "variation": "Reach on a gentle diagonal instead of straight ahead.",
        "easy": "Use a lower, larger target close to the comfortable reach.",
        "medium": "Use the usual forward target and planned dose.",
        "difficult": "Use a slightly higher, smaller target and a longer controlled hold.",
    },
    "ex_trunk": {
        "variation": "Alternate a centre reach with a small diagonal reach while the back stays supported.",
        "easy": "Use a short, lower reach with a larger target and fewer repetitions.",
        "medium": "Reach forward while maintaining chair-back contact.",
        "difficult": "Reach slightly higher and hold longer without losing chair-back contact.",
    },
    "ex_wallslide": {
        "variation": "Slide toward a slightly diagonal target while keeping the forearm supported.",
        "easy": "Use a low table-slide target in a pain-free range.",
        "medium": "Use the usual supported elevation target.",
        "difficult": "Use a slightly higher target and longer hold, still pain-free and without adding resistance.",
    },
    "ex_scapdepress": {
        "variation": "Reach slightly across the body while keeping the shoulder away from the ear.",
        "easy": "Practise a short supported reach with a large target.",
        "medium": "Use the usual reach while controlling shoulder position.",
        "difficult": "Use a slightly higher, smaller target and maintain the relaxed shoulder longer.",
    },
    "ex_h2m": {
        "variation": "Alternate an empty-hand movement with a light spoon or empty cup.",
        "easy": "Use an empty hand or large light handle and a generous mouth target.",
        "medium": "Use the planned hand-to-mouth movement with a light object.",
        "difficult": "Use a smaller target and pause longer near the mouth without leaning the trunk.",
    },
    "ex_grasp": {
        "variation": "Transport the object in the opposite table direction.",
        "easy": "Use a large, very light object over a short distance.",
        "medium": "Use a soft cup across the usual table distance.",
        "difficult": "Use a slightly smaller light object over a longer path with a controlled release.",
    },
    "ex_handopen": {
        "variation": "Alternate opening around a rolled towel and a large light cup.",
        "easy": "Use assistance and a large object; every small opening effort counts.",
        "medium": "Open and release independently as far as comfortable.",
        "difficult": "Hold the hand open longer around a slightly smaller object; never add resistance automatically.",
    },
    "ex_pinch": {
        "variation": "Change between a large peg, coin, pen, and different finger oppositions.",
        "easy": "Use a large peg or thick pen with fewer placements.",
        "medium": "Use coins or standard pegs with the thumb and index finger.",
        "difficult": "Use a smaller object or another finger opposition with a longer controlled release.",
    },
    "ex_bilateral": {
        "variation": "Alternate towel-folding with a two-handed forward roller movement.",
        "easy": "Use a small two-handed movement at a lower height.",
        "medium": "Use the usual symmetrical movement at chest height.",
        "difficult": "Use a slightly larger path and longer pause while both arms stay level.",
    },
    "ex_lower_selective": {
        "variation": "Alternate a slow knee straighten with a heel-slide pattern in supported sitting.",
        "easy": "Use assisted partial range with fewer repetitions.",
        "medium": "Use the comfortable planned knee range and dose.",
        "difficult": "Pause longer near extension and add only one controlled repetition.",
    },
    "ex_ankle_dorsiflexion": {
        "variation": "Alternate toe lifts with a slow lift-and-lower rhythm cue.",
        "easy": "Use a small assisted toe lift while the heel stays down.",
        "medium": "Use the usual independent comfortable toe lift.",
        "difficult": "Hold the toe lift longer and lower more slowly without moving the knee.",
    },
    "ex_sit_to_stand": {
        "variation": "Alternate a full repetition with a carefully rehearsed foot-placement and forward-lean sequence.",
        "easy": "Use the agreed higher chair and more assistance.",
        "medium": "Use the current therapist-agreed chair, aid, and assistance.",
        "difficult": "Add at most one slow repetition; never lower the chair or remove support automatically.",
    },
    "ex_supported_stand": {
        "variation": "Alternate an alignment focus with an even-weight focus.",
        "easy": "Use a shorter supported hold with full guarding.",
        "medium": "Use the planned supported standing hold.",
        "difficult": "Hold slightly longer with the same fixed support and guarding.",
    },
    "ex_supported_step": {
        "variation": "Alternate step initiation with a toe-clearance and placement focus.",
        "easy": "Use a very small step with full support and fewer repetitions.",
        "medium": "Use the therapist-agreed step length and support.",
        "difficult": "Add one controlled repetition or a slightly longer pause; never reduce guarding.",
    },
    "ex_weight_shift": {
        "variation": "Alternate a side shift with a return-to-centre accuracy focus.",
        "easy": "Use a small shift with full support and a short pause.",
        "medium": "Use the planned comfortable shift and pause.",
        "difficult": "Use a slightly larger controlled shift or longer pause with unchanged guarding.",
    },
    "ex_sitting_balance": {
        "variation": "Reach on a gentle diagonal and return precisely to midline.",
        "easy": "Use a small reach with feet supported and close guarding.",
        "medium": "Use the planned reach and controlled return.",
        "difficult": "Use a slightly farther target and longer pause with the same guarding.",
    },
    "ex_step_stance": {
        "variation": "Alternate which safely approved foot leads while practising the same short stance.",
        "easy": "Use a very short stance and brief hold with full support.",
        "medium": "Use the therapist-agreed stance and hold.",
        "difficult": "Hold slightly longer or add one repetition; never remove fixed support or guarding.",
    },
    "ex_maintenance": {
        "variation": "Reach on alternating gentle diagonals instead of only straight ahead.",
        "easy": "Use a lower, larger target and fewer repetitions.",
        "medium": "Use the usual full comfortable movement.",
        "difficult": "Use a slightly higher, smaller target with a longer smooth hold.",
    },
}


def _exercise_by_id(exercise_id: str) -> Optional[RehabExercise]:
    exercise_id = EXERCISE_RUNNER_ALIASES.get(exercise_id, exercise_id)
    return next((exercise for exercise in EXERCISE_LIBRARY.values() if exercise.id == exercise_id), None)


def _difficulty_dose(exercise: RehabExercise, level: str) -> Dict[str, int]:
    preset = SESSION_DIFFICULTY_PRESETS.get(level) or SESSION_DIFFICULTY_PRESETS["medium"]
    if level == "easy":
        sets = max(1, exercise.sets + int(preset["set_delta"]))
        reps = max(3, round(exercise.reps * preset["rep_factor"]))
    elif level == "difficult" and exercise.id in SUPERVISED_EXERCISE_IDS:
        sets = exercise.sets
        reps = min(20, exercise.reps + 1)
    elif level == "difficult":
        sets = min(4, exercise.sets + int(preset["set_delta"]))
        reps = min(20, max(exercise.reps + 1, round(exercise.reps * preset["rep_factor"])))
    else:
        sets, reps = exercise.sets, exercise.reps
    return {"sets": sets, "reps": reps}


def _exercise_difficulty_levels(exercise: RehabExercise) -> Dict[str, Dict[str, Any]]:
    rules = EXERCISE_SESSION_RULES.get(exercise.id) or EXERCISE_SESSION_RULES["ex_maintenance"]
    return {
        level: {
            "label": level.capitalize(),
            **_difficulty_dose(exercise, level),
            "adjustment": rules[level],
        }
        for level in ("easy", "medium", "difficult")
    }


def _configure_rehab_runner(exercise_id: str, difficulty: str, variation: str) -> Dict[str, Any]:
    import copy as _copy

    exercise_id = EXERCISE_RUNNER_ALIASES.get(exercise_id, exercise_id)
    level = difficulty if difficulty in SESSION_DIFFICULTY_PRESETS else "medium"
    selected_variation = variation if variation in {"standard", "alternate"} else "standard"
    cfg = _copy.deepcopy(REHAB_RUNNER_CONFIG.get(exercise_id) or REHAB_RUNNER_CONFIG["ex_maintenance"])
    movement_standard = _copy.deepcopy(
        EXERCISE_MOVEMENT_STANDARDS.get(exercise_id)
        or EXERCISE_MOVEMENT_STANDARDS["ex_maintenance"]
    )
    coaching_profile = _copy.deepcopy(
        EXERCISE_COACHING_PROFILES.get(exercise_id)
        or EXERCISE_COACHING_PROFILES["ex_maintenance"]
    )
    preset = SESSION_DIFFICULTY_PRESETS[level]
    rules = EXERCISE_SESSION_RULES.get(exercise_id) or EXERCISE_SESSION_RULES["ex_maintenance"]
    target_steps = [step for step in cfg.get("cycle") or [] if isinstance(step.get("target"), dict)]
    for index, step in enumerate(target_steps):
        target = step["target"]
        is_calibrated_target = target.get("landmark") == "LAP_DYNAMIC"
        is_return_target = is_calibrated_target or float(target.get("y", 0.5)) >= 0.70
        if not is_return_target:
            y = float(target.get("y", 0.5)) + preset["target_y_delta"]
            target["y"] = max(0.16, min(0.68, round(y, 3)))
            x = float(target.get("x", 0.5))
            if exercise_id == "ex_grasp":
                distance = (x - 0.5) * preset["target_distance_scale"]
                target["x"] = round(0.5 + distance, 3)
                if selected_variation == "alternate":
                    target["x"] = round(1 - target["x"], 3)
            elif selected_variation == "alternate":
                shift = 0.10 if index % 2 == 0 else -0.10
                target["x"] = max(0.18, min(0.82, round(x + shift, 3)))
        target["r"] = 0.10 if is_calibrated_target else max(0.07, min(0.34, round(float(target.get("r", 0.10)) * preset["radius_scale"], 3)))
        step["hold_ms"] = max(600, round(float(step.get("hold_ms") or 1000) * preset["hold_scale"]))

    variation_cue = rules["variation"] if selected_variation == "alternate" else "Use the familiar movement pattern today."
    safety_cue = (
        " Keep the same fixed support, walking aid, and hands-on guarding at every difficulty level."
        if exercise_id in SUPERVISED_EXERCISE_IDS else ""
    )
    cfg["setup_voice"] = f"{cfg.get('setup_voice', '')} Today's level is {level}. {rules[level]} {variation_cue}{safety_cue}".strip()
    cfg["exercise_id"] = exercise_id
    cfg["temporary_compensation_evidence"] = exercise_id in TEMPORARY_EVIDENCE_EXERCISE_IDS
    cfg["difficulty"] = level
    cfg["variation"] = selected_variation
    cfg["difficulty_adjustment"] = rules[level]
    cfg["variation_adjustment"] = variation_cue
    for rom_step in movement_standard.get("rom_steps") or []:
        rom_step["target_deg"] = float((rom_step.get("targets") or {}).get(level) or 0)
        rom_step.pop("targets", None)
        rom_step["coaching_cue"] = str((coaching_profile.get("rom_cues") or {}).get(rom_step.get("id")) or "")
    for compensation in movement_standard.get("compensations") or []:
        compensation["label"] = str(
            (coaching_profile.get("compensation_labels") or {}).get(compensation.get("id"))
            or str(compensation.get("id") or "Compensation").replace("_", " ").title()
        )
    movement_standard.update({
        "training_focus": coaching_profile["training_focus"],
        "repetition_definition": coaching_profile["repetition_definition"],
        "measurement_limit": coaching_profile["measurement_limit"],
    })
    cfg["movement_standard"] = movement_standard
    cfg["calibration_contract"] = _copy.deepcopy(EXERCISE_CALIBRATION_CONTRACT)
    cfg["overlay_style"] = _copy.deepcopy(EXERCISE_OVERLAY_STYLE)
    cfg["scoring_method"] = _copy.deepcopy(EXERCISE_SCORING_METHOD)
    fixed_voice_texts = [
        str(cfg.get("setup_voice") or ""),
        str(movement_standard.get("calibration_instruction") or ""),
        EXERCISE_POSTURE_CHANGED_VOICE,
        EXERCISE_TRANSITION_VOICE,
        EXERCISE_ASSISTANCE_QUESTION_VOICE,
        EXERCISE_ASSISTED_COMPLETE_VOICE,
        EXERCISE_INDEPENDENT_COMPLETE_VOICE,
        *[str(step.get("voice") or "") for step in cfg.get("cycle") or []],
    ]
    cfg["prepared_voice_assets"] = {
        text: asset_url
        for text in fixed_voice_texts
        if text and (asset_url := _prepared_tts_asset_url(text))
    }
    return cfg


@api_router.get("/rehab/session-options")
async def rehab_session_options(exercise_ids: str = ""):
    requested = [value.strip() for value in exercise_ids.split(",") if value.strip()]
    exercises = []
    for requested_id in requested:
        exercise_id = EXERCISE_RUNNER_ALIASES.get(requested_id, requested_id)
        exercise = _exercise_by_id(exercise_id)
        if not exercise or exercise_id not in REHAB_RUNNER_CONFIG:
            continue
        rules = EXERCISE_SESSION_RULES.get(exercise_id) or EXERCISE_SESSION_RULES["ex_maintenance"]
        exercises.append({
            "exercise_id": requested_id,
            "name": exercise.name,
            "requires_same_support_at_all_levels": exercise_id in SUPERVISED_EXERCISE_IDS,
            "alternate_variation": rules["variation"],
            "levels": _exercise_difficulty_levels(exercise),
        })
    return {
        "levels": ["easy", "medium", "difficult"],
        "variations": ["standard", "alternate"],
        "exercises": exercises,
        "safety_rule": "Difficulty changes are incremental. Pain, dizziness, marked fatigue, new weakness, or loss of balance means stop and use the easier plan or contact the rehabilitation team.",
    }


@api_router.get("/emergency/fast-runner", response_class=HTMLResponse)
async def emergency_fast_runner():
    return HTMLResponse(
        content=FAST_RUNNER_HTML,
        headers={"Cache-Control": "no-store"},
    )


@api_router.post("/emergency/fast-check")
async def record_emergency_fast_check(payload: FastCheckSubmit, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")

    safe_automated: Dict[str, Dict[str, Any]] = {}
    for sign in ("face", "arms", "speech"):
        source = payload.automated.get(sign) or {}
        safe_automated[sign] = {
            "available": bool(source.get("available")),
            "positive": bool(source.get("positive")),
            "decision": str(source.get("decision") or "unsure")[:16],
            "quality": str(source.get("quality") or "unknown")[:64],
            "reason": str(source.get("reason") or "")[:240],
            "samples": source.get("samples") if isinstance(source.get("samples"), int) else 0,
            "positive_samples": source.get("positive_samples") if isinstance(source.get("positive_samples"), int) else 0,
            "engaged_samples": source.get("engaged_samples") if isinstance(source.get("engaged_samples"), int) else 0,
            "both_raised_samples": source.get("both_raised_samples") if isinstance(source.get("both_raised_samples"), int) else 0,
            "one_sided_samples": source.get("one_sided_samples") if isinstance(source.get("one_sided_samples"), int) else 0,
            "metric": source.get("metric"),
            "smile_activation": source.get("smile_activation"),
            "similarity": source.get("similarity"),
            "confidence": source.get("confidence"),
            "provider": str(source.get("provider") or "unknown")[:32],
            "model": str(source.get("model") or "unknown")[:64],
            "recording_retained": False,
        }

    try:
        result = evaluate_fast_screen(payload.answers, safe_automated)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _record_alira_action(
        "emergency_fast_check_completed",
        source="guided_fast",
        user_id=user["id"],
        status="urgent" if result["call_999"] else "completed",
        details={
            "answers": payload.answers,
            "automated": safe_automated,
            "onset_time": payload.onset_time,
            "result": result,
            "raw_video_saved": False,
            "raw_audio_saved": False,
        },
    )
    return result


@api_router.get("/testing/library")
async def get_testing_library(request: Request, patient_tasks_only: bool = False):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")

    assessment_packages = []
    patient_ids = {task["id"] for task in INITIAL_ASSESSMENT_TASKS if task["id"] != "L6"}
    for package_id in ("upper_limb", "hand", "lower_limb", "balance"):
        package = ASSESSMENT_PACKAGES[package_id]
        assessment_packages.append({
            "id": package_id,
            "title": package["title"],
            "subtitle": package["subtitle"],
            "tasks": [
                {
                    "id": task["id"],
                    "title": task["title"],
                    "view": task.get("view", ""),
                    "focus": task.get("focus", ""),
                    "step_count": len(task.get("steps") or []),
                    "safety_tier": task.get("safety_tier", "seated"),
                    "safety_note": task.get("safety_note"),
                }
                for task in package["tasks"] if not patient_tasks_only or task["id"] in patient_ids
            ],
        })

    assessment_packages = [package for package in assessment_packages if package["tasks"]]
    exercises = []
    seen_exercise_ids = set()
    for exercise in EXERCISE_LIBRARY.values():
        if exercise.id in seen_exercise_ids:
            continue
        seen_exercise_ids.add(exercise.id)
        runner = REHAB_RUNNER_CONFIG.get(exercise.id)
        if not runner:
            continue
        support_text = " ".join([
            exercise.frequency,
            exercise.description,
            str(runner.get("setup_voice") or ""),
        ]).lower()
        configured_runner = _configure_rehab_runner(exercise.id, "medium", "standard")
        movement_standard = configured_runner["movement_standard"]
        exercises.append({
            **exercise.model_dump(),
            "guided_reps": int(runner.get("reps") or exercise.reps),
            "pose_mode": str(runner.get("pose_mode") or "guided"),
            "support_required": any(term in support_text for term in ("therapist", "carer", "caregiver", "guarding", "supervised")),
            "difficulty_levels": _exercise_difficulty_levels(exercise),
            "alternate_variation": (EXERCISE_SESSION_RULES.get(exercise.id) or EXERCISE_SESSION_RULES["ex_maintenance"])["variation"],
            "calibration_instruction": movement_standard["calibration_instruction"],
            "calibration_contract": configured_runner["calibration_contract"],
            "training_focus": movement_standard["training_focus"],
            "repetition_definition": movement_standard["repetition_definition"],
            "rom_metrics": movement_standard["rom_steps"],
            "compensation_metrics": movement_standard["compensations"],
            "measurement_limit": movement_standard["measurement_limit"],
            "scoring_method": configured_runner["scoring_method"],
            "overlay_style": configured_runner["overlay_style"],
        })

    return {
        "assessment_task_count": sum(len(item["tasks"]) for item in assessment_packages),
        "exercise_count": len(exercises),
        "assessment_packages": assessment_packages,
        "exercises": exercises,
        "test_runs_are_recorded": False,
    }


@api_router.post("/testing/assessment-score")
async def get_testing_assessment_score(payload: TaskResult, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if payload.task_id not in ASSESSMENT_RUBRICS or payload.task_id == "L6":
        raise HTTPException(status_code=422, detail="Choose a guided assessment task from Testing")
    expected = {step["id"] for step in ASSESSMENT_RUBRICS[payload.task_id]["steps"]}
    ids = [step.step_id for step in payload.steps]
    if len(ids) != len(set(ids)) or not set(ids).issubset(expected):
        raise HTTPException(status_code=422, detail="The recorded steps do not match this task")
    return testing_task_report(payload, ASSESSMENT_RUBRICS)


def _rehab_runner_html(
    exercise_id: str,
    prescribed_reps: Optional[int] = None,
    difficulty: str = "medium",
    variation: str = "standard",
) -> str:
    import json as _json
    cfg = _configure_rehab_runner(exercise_id, difficulty, variation)
    if prescribed_reps is not None:
        cfg["reps"] = max(1, min(20, int(prescribed_reps)))
    cfg_json = _json.dumps(cfg)
    return REHAB_RUNNER_HTML_TEMPLATE.replace("__CFG_JSON__", cfg_json)


@api_router.get("/rehab/runner", response_class=HTMLResponse)
async def rehab_runner(
    exercise_id: str = "ex_maintenance",
    reps: Optional[int] = None,
    difficulty: str = "medium",
    variation: str = "standard",
):
    return HTMLResponse(content=_rehab_runner_html(exercise_id, reps, difficulty, variation))


TRUNK_LEAN_COMPARISON_DIR = ROOT_DIR.parent / "testing" / "trunk-lean-comparison"
TRUNK_LEAN_COMPARISON_HTML = TRUNK_LEAN_COMPARISON_DIR / "trunk_lean_comparison.html"
TRUNK_LEAN_COMPARISON_APP = TRUNK_LEAN_COMPARISON_DIR / "trunk_lean_comparison.js"
TRUNK_LEAN_COMPARISON_METRICS = TRUNK_LEAN_COMPARISON_DIR / "trunk_lean_metrics.js"
TRUNK_LEAN_COMPARISON_HEADERS = {
    "Cache-Control": "no-store, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@api_router.get("/testing/trunk-lean-comparison", response_class=HTMLResponse)
async def trunk_lean_comparison():
    return HTMLResponse(
        content=TRUNK_LEAN_COMPARISON_HTML.read_text(encoding="utf-8"),
        headers=TRUNK_LEAN_COMPARISON_HEADERS,
    )


@api_router.get("/testing/trunk-lean-comparison/app.js", response_class=Response)
async def trunk_lean_comparison_app():
    return Response(
        content=TRUNK_LEAN_COMPARISON_APP.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers=TRUNK_LEAN_COMPARISON_HEADERS,
    )


@api_router.get("/testing/trunk-lean-comparison/metrics.js", response_class=Response)
async def trunk_lean_comparison_metrics():
    return Response(
        content=TRUNK_LEAN_COMPARISON_METRICS.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers=TRUNK_LEAN_COMPARISON_HEADERS,
    )


@api_router.get("/rehab/games")
async def rehab_games_catalog():
    return {"games": game_catalog(), "optional_practice": True}


@api_router.get("/rehab/game-runner", response_class=HTMLResponse)
async def rehab_game_runner(game_id: str = "garden_reach", difficulty: str = "medium"):
    return HTMLResponse(
        content=rehab_game_html(game_id, difficulty),
        headers={"Cache-Control": "no-store"},
    )


REHAB_RUNNER_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no" />
<title>Rehab Exercise</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{width:100%;height:100%;background:#0c100e;color:#fdfdfd;font-family:-apple-system,BlinkMacSystemFont,"Plus Jakarta Sans",sans-serif;overflow:hidden}
  #stage{position:relative;width:100vw;height:100vh;height:100dvh;background:#000}
  #cameraFrame{position:absolute;left:0;top:0;width:100%;height:100%;overflow:hidden;background:#000}
  #cameraFrame video,#cameraFrame canvas{position:absolute;inset:0;width:100%;height:100%;transform:scaleX(-1);transform-origin:center}
  #ui{position:absolute;inset:0;pointer-events:none;display:flex;flex-direction:column;justify-content:space-between;padding:env(safe-area-inset-top,24px) 16px env(safe-area-inset-bottom,24px) 16px}
  #top{position:relative;z-index:12;display:flex;align-items:center;gap:8px;background:rgba(28,32,29,0.65);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-radius:24px;padding:12px 16px;pointer-events:auto}
  #top .meta{flex:1;display:flex;flex-direction:column}
  #top .name{font-size:14px;font-weight:700}
  #top .rep{font-size:12px;color:#D9E5DC}
  #exitBtn{background:rgba(255,255,255,0.18);border:none;color:#fff;padding:8px 12px;border-radius:16px;font-weight:600;font-size:13px;pointer-events:auto;cursor:pointer}
  #bottom{background:rgba(28,32,29,0.85);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-radius:24px;padding:16px 18px;pointer-events:auto}
  #caption{font-size:18px;font-weight:600;line-height:1.35;min-height:48px}
  #voiceRow{display:flex;align-items:center;gap:10px;margin-top:10px;opacity:0.85}
  #voiceWave{display:flex;gap:3px;align-items:end;height:14px}
  #voiceWave span{display:block;width:3px;background:#E18E6D;border-radius:2px;animation:wave 1s ease-in-out infinite}
  #voiceWave span:nth-child(1){animation-delay:.0s;height:6px}
  #voiceWave span:nth-child(2){animation-delay:.15s;height:14px}
  #voiceWave span:nth-child(3){animation-delay:.3s;height:8px}
  #voiceWave span:nth-child(4){animation-delay:.45s;height:12px}
  @keyframes wave{0%,100%{transform:scaleY(0.5)}50%{transform:scaleY(1.2)}}
  #voiceText{font-size:13px;color:#D9E5DC}
  #tapBtn{margin-top:12px;background:#4A7856;color:#fff;border:none;width:100%;padding:14px;border-radius:16px;font-weight:700;font-size:16px;cursor:pointer}
  #tapBtn:disabled{opacity:.45;cursor:default}
  #overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:#0c100eee;text-align:center;padding:24px;flex-direction:column;gap:16px;pointer-events:auto;z-index:10}
  #overlay h1{font-size:22px;font-weight:700}
  #overlay p{font-size:15px;color:#bcc2ba;line-height:1.5}
  #overlay button{background:#4A7856;color:#fff;border:none;padding:14px 28px;border-radius:16px;font-weight:700;font-size:16px}
  .hidden{display:none !important}
  #calibration{position:absolute;inset:0;display:flex;align-items:flex-start;justify-content:center;padding:calc(88px + env(safe-area-inset-top,0px)) 18px 18px;background:linear-gradient(180deg,rgba(12,16,14,.88),rgba(12,16,14,.28));z-index:11;pointer-events:auto;overflow-y:auto}
  #calibration .panel{width:min(560px,100%);padding:20px;border-radius:16px;background:rgba(253,253,253,.96);color:#1C201D;box-shadow:0 18px 55px rgba(0,0,0,.30)}
  #calibration h2{font-size:23px;line-height:1.25;margin-bottom:7px}
  #calibration p{font-size:15px;line-height:1.45;color:#4E5B53}
  #calibrationStatus{margin-top:14px;font-size:15px;font-weight:750;color:#285C3A}
  #calibrationTrack{height:8px;margin-top:10px;border-radius:4px;background:#DDE5DE;overflow:hidden}
  #calibrationFill{width:0;height:100%;border-radius:4px;background:#4A7856;transition:width .18s ease}
  /* Feedback / confirmation overlay */
  #assist{position:absolute;inset:0;background:linear-gradient(180deg, rgba(74,120,86,0.94), rgba(28,32,29,0.96));padding:24px;display:flex;flex-direction:column;justify-content:center;gap:18px;text-align:center;pointer-events:auto;z-index:11}
  #assist .row{display:flex;gap:12px;justify-content:center}
  #assist button{min-height:56px;padding:0 22px;border-radius:12px;border:2px solid rgba(255,255,255,0.55);background:transparent;color:#fff;font-size:17px;font-weight:800}
  #assist button.primary{background:#FDFDFD;color:#1C201D;border-color:#FDFDFD}
  #fb{position:absolute;inset:0;background:linear-gradient(180deg, rgba(74,120,86,0.92), rgba(28,32,29,0.95));padding:24px;display:flex;flex-direction:column;justify-content:center;gap:16px;text-align:center;pointer-events:auto;z-index:9;opacity:0;transition:opacity .35s;overflow-y:auto}
  #fb.show{opacity:1}
  #fb.hasEvidence{justify-content:flex-start;padding-top:max(24px,calc(env(safe-area-inset-top,0px) + 18px));padding-bottom:max(24px,calc(env(safe-area-inset-bottom,0px) + 18px))}
  #fb .step{font-size:13px;color:#D9E5DC;letter-spacing:1px;text-transform:uppercase;font-weight:700}
  #fb .reward{width:min(320px,100%);margin:0 auto;display:flex;align-items:center;justify-content:center;gap:12px;background:#FFF8DE;color:#155D3C;border:1px solid #F0D472;border-radius:14px;padding:12px 18px;box-shadow:0 12px 32px rgba(0,0,0,.18)}
  #fb .rewardStar{font-size:30px;line-height:1}
  #fb .rewardCopy{display:flex;flex-direction:column;align-items:flex-start;line-height:1.2}
  #fb .rewardCopy strong{font-size:20px;font-weight:900}
  #fb .rewardCopy span{font-size:13px;font-weight:750;color:#486151}
  #fb .title{font-size:22px;font-weight:800;color:#fff;line-height:1.3}
  #fb .body{font-size:16px;color:#FDFDFD;line-height:1.5;background:rgba(255,255,255,0.08);padding:14px;border-radius:14px}
  #fb .evidence{width:min(460px,100%);margin:0 auto;text-align:left;background:#FDFDFD;color:#173D2B;border-radius:16px;padding:12px;box-shadow:0 16px 38px rgba(0,0,0,.24)}
  #fb .evidenceHead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 2px 10px}
  #fb .evidenceHead strong{font-size:16px;font-weight:850}
  #fb .evidenceHead span{font-size:11px;font-weight:800;color:#A12C27;background:#FDE9E7;border-radius:999px;padding:5px 8px;white-space:nowrap}
  #fb .evidenceFrame{position:relative;width:100%;max-height:36vh;aspect-ratio:4/3;overflow:hidden;border-radius:11px;background:#151A17;border:1px solid #DDE5DE}
  #fb .evidenceFrame img{display:block;width:100%;height:100%;object-fit:contain;background:#151A17}
  #fb .evidenceLabels{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}
  #fb .evidenceLabel{font-size:12px;font-weight:850;color:#A12C27;background:#FDE9E7;border:1px dashed #D9473F;border-radius:999px;padding:5px 9px}
  #fb .evidencePrivacy{margin:8px 2px 0;color:#56655C;font-size:11px;line-height:1.35}
  #fb .prompt{font-size:15px;color:#D9E5DC;font-style:italic}
  #fb .mic{display:flex;align-items:center;justify-content:center;gap:10px;background:rgba(225,142,109,0.18);padding:10px;border-radius:14px;border:1px solid rgba(225,142,109,0.4)}
  #fb .mic .dot{width:12px;height:12px;border-radius:50%;background:#E18E6D;animation:pulse 1.1s ease-in-out infinite}
  @keyframes pulse{0%,100%{transform:scale(.7);opacity:.6}50%{transform:scale(1.2);opacity:1}}
  #fb .heard{font-size:13px;color:#fff;opacity:.85;min-height:18px}
  #fb .row{display:flex;gap:10px;justify-content:center;margin-top:8px}
  #fb.hasEvidence>.row{position:sticky;bottom:0;z-index:2;width:min(460px,100%);margin:8px auto 0;padding:8px;border-radius:14px;background:rgba(28,32,29,.94)}
  #fb button{background:rgba(255,255,255,0.18);color:#fff;border:none;padding:12px 18px;border-radius:14px;font-weight:700;font-size:14px;cursor:pointer}
  #fb button.primary{background:#4A7856}
  #fb .check{font-size:14px;color:#D9E5DC;display:flex;gap:8px;align-items:center;justify-content:center}
  #fb .check span.ok{color:#7FE5A3}
</style>
</head>
<body>
<div id="stage">
  <div id="cameraFrame">
    <video id="video" playsinline autoplay muted></video>
    <canvas id="canvas"></canvas>
  </div>
  <div id="ui">
    <div id="top">
      <button id="exitBtn" data-testid="rehab-exit">Exit</button>
      <div class="meta">
        <div class="name" id="exName">Exercise</div>
        <div class="rep" id="repLabel">Rep 1 of 5</div>
        <div id="repBar" style="height:6px;background:rgba(255,255,255,0.18);border-radius:3px;margin-top:6px;overflow:hidden">
          <div id="repBarFill" style="height:100%;width:0%;background:#E18E6D;border-radius:3px;transition:width .3s ease"></div>
        </div>
      </div>
    </div>
    <div id="bottom">
      <div id="caption">Preparing…</div>
      <div id="voiceRow">
        <div id="voiceWave"><span></span><span></span><span></span><span></span></div>
        <div id="voiceText">Listening to instructions…</div>
      </div>
      <button id="tapBtn" class="hidden" data-testid="rehab-tap-rep">I did one repetition</button>
    </div>
  </div>
  <div id="overlay">
    <h1 id="overlayTitle">Ready?</h1>
    <p id="overlayBody">We will guide you through each repetition with your camera and voice. After every rep I will share what to improve. Move into the camera view.</p>
    <button id="startBtn" data-testid="rehab-start">Start Exercise</button>
  </div>
  <div id="calibration" class="hidden" data-testid="exercise-calibration">
    <div class="panel">
      <h2>Let us set your starting position</h2>
      <p id="calibrationInstruction">Move into view and hold still while the camera checks your position.</p>
      <div id="calibrationStatus" role="status">Looking for the required joints…</div>
      <div id="calibrationTrack"><div id="calibrationFill"></div></div>
    </div>
  </div>
  <div id="assist" class="hidden" data-testid="exercise-assist-question">
    <div class="title">One quick question</div>
    <div class="body">Did a carer or family member help you move during this exercise?</div>
    <div class="row">
      <button id="assistYesBtn" data-testid="exercise-assist-yes">Yes, I had help</button>
      <button id="assistNoBtn" class="primary" data-testid="exercise-assist-no">No, I did it myself</button>
    </div>
  </div>
  <div id="fb" class="hidden">
    <div class="step" id="fbStep">Rep 1 complete</div>
    <div class="reward" id="fbReward" role="status" aria-live="polite">
      <span class="rewardStar" aria-hidden="true">&#11088;</span>
      <span class="rewardCopy"><strong>Repetition complete</strong><span>Great effort!</span></span>
    </div>
    <div class="title" id="fbTitle">Here's what I noticed</div>
    <div class="evidence hidden" id="fbEvidence" data-testid="temporary-compensation-evidence">
      <div class="evidenceHead"><strong>Where the movement changed</strong><span>Temporary image</span></div>
      <div class="evidenceFrame"><img id="fbEvidenceImage" alt="Temporary camera frame with the detected movement area outlined in red" /></div>
      <div class="evidenceLabels" id="fbEvidenceLabels"></div>
      <div class="evidencePrivacy">Kept only for this feedback screen and deleted when you continue.</div>
    </div>
    <div class="body" id="fbBody">…</div>
    <div class="prompt" id="fbPrompt">When you're ready, please say <b>"Yes"</b>.</div>
    <div class="mic" id="fbMic">
      <div class="dot"></div>
      <div id="fbHeard" class="heard">Listening…</div>
    </div>
    <div class="check" id="fbChecks">
      <span id="checkYes">○ Yes</span>
      <span style="opacity:.4">·</span>
      <span id="checkUnderstand">○ "I understand my problem now"</span>
    </div>
    <div class="row">
      <button id="fbReplay">Replay</button>
      <button id="fbConfirmBtn" class="primary">I'm ready (tap)</button>
    </div>
  </div>
</div>

<script type="module">
import { PoseLandmarker, HandLandmarker, FilesetResolver, DrawingUtils } from "/vendor/mediapipe/vision_bundle.mjs";

const API_BASE = window.location.origin + "/api";
const CFG = __CFG_JSON__;

const stage = document.getElementById("stage");
const cameraFrame = document.getElementById("cameraFrame");
const video = document.getElementById("video");
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const exName = document.getElementById("exName");
const repLabel = document.getElementById("repLabel");
const captionEl = document.getElementById("caption");
const voiceText = document.getElementById("voiceText");
const overlay = document.getElementById("overlay");
const overlayTitle = document.getElementById("overlayTitle");
const overlayBody = document.getElementById("overlayBody");
const startBtn = document.getElementById("startBtn");
const calibrationEl = document.getElementById("calibration");
const calibrationInstruction = document.getElementById("calibrationInstruction");
const calibrationStatus = document.getElementById("calibrationStatus");
const calibrationFill = document.getElementById("calibrationFill");
const exitBtn = document.getElementById("exitBtn");
const tapBtn = document.getElementById("tapBtn");
const fbEl = document.getElementById("fb");
const fbStep = document.getElementById("fbStep");
const fbTitle = document.getElementById("fbTitle");
const fbBody = document.getElementById("fbBody");
const fbEvidence = document.getElementById("fbEvidence");
const fbEvidenceImage = document.getElementById("fbEvidenceImage");
const fbEvidenceLabels = document.getElementById("fbEvidenceLabels");
const fbPrompt = document.getElementById("fbPrompt");
const fbMic = document.getElementById("fbMic");
const fbChecks = document.getElementById("fbChecks");
const fbHeard = document.getElementById("fbHeard");
const fbConfirmBtn = document.getElementById("fbConfirmBtn");
const assistEl = document.getElementById("assist");
const fbReward = document.getElementById("fbReward");
let qualityReps = 0;         // repetitions done correctly this session (the ones that earn points)
const assistYesBtn = document.getElementById("assistYesBtn");
const assistNoBtn = document.getElementById("assistNoBtn");
const fbReplay = document.getElementById("fbReplay");
const checkYes = document.getElementById("checkYes");
const checkUnderstand = document.getElementById("checkUnderstand");
const URL_PARAMS = new URLSearchParams(window.location.search);
const VOICE_GUIDANCE_ENABLED = URL_PARAMS.get("voice_guidance") !== "0";
const AFFECTED_SIDE = URL_PARAMS.get("affected_side") === "left" ? "left" : "right";
const REHAB_SESSION_ID = (URL_PARAMS.get("rehab_session_id")||"").replace(/[^a-zA-Z0-9_-]/g,"").slice(0,96);
const STANDARD = CFG.movement_standard || {tracking_mode:"pose", posture:"seated", rom_steps:[], compensations:[]};
const HAS_SIDE_LEAN_RULE = (STANDARD.compensations||[]).some(rule=>rule.metric === "trunk_side_lean_delta");
const CALIBRATION_CONTRACT = CFG.calibration_contract || {};
const SCORING_METHOD = CFG.scoring_method || {};
const OVERLAY_STYLE = CFG.overlay_style || {};
const TEMPORARY_COMPENSATION_EVIDENCE = CFG.temporary_compensation_evidence === true;
const EVIDENCE_COMPENSATION_IDS = new Set(["trunk_lean","trunk_forward","shoulder_hike","head_drop","wrist_flexion"]);
const HAS_DYNAMIC_LAP_TARGET = (CFG.cycle||[]).some(step=>step && step.target && step.target.landmark === "LAP_DYNAMIC");
// Static targets are authored for a right-affected patient (the cup starts on
// the affected side); mirror them for a left-affected patient.
if(AFFECTED_SIDE === "left" && CFG.mirror_for_left){
  for(const step of CFG.cycle||[]){
    if(step && step.target && !/_DYNAMIC$/.test(String(step.target.landmark||"")) && Number.isFinite(Number(step.target.x))){
      step.target.x = Math.round((1 - Number(step.target.x)) * 1000) / 1000;
    }
  }
}
// Hand-opening / grasp steps need the hand landmarker even in pose mode.
const HAND_GATE_LANDMARKS = new Set(["HAND_OPEN","HAND_CLOSED"]);
const NEEDS_HAND_TRACKING = (CFG.movement_standard||{}).tracking_mode === "hand" || !!CFG.hand_tracking
  || (CFG.cycle||[]).some(step=>step && step.target && HAND_GATE_LANDMARKS.has(step.target.landmark));
const LAP_CALIBRATION_MIN_SAMPLES = Number(CALIBRATION_CONTRACT.lap_minimum_samples)||8;
const LAP_CALIBRATION_MIN_MS = Number(CALIBRATION_CONTRACT.lap_minimum_duration_ms)||650;
const LAP_CALIBRATION_STABLE_RATIO = Number(CALIBRATION_CONTRACT.lap_stable_sample_ratio)||.70;
const CALIBRATION_MIN_SAMPLES = Number(CALIBRATION_CONTRACT.minimum_baseline_samples)||45;
const CALIBRATION_MIN_TRACKING_QUALITY = Number(CALIBRATION_CONTRACT.minimum_tracking_quality)||.72;
const CALIBRATION_MAX_ANCHOR_DRIFT = Number(CALIBRATION_CONTRACT.maximum_anchor_drift)||.035;
const SCORING_MIN_FRAMES = Number(SCORING_METHOD.minimum_scored_frames)||8;
const NEEDS_LOWER_BODY_VIEW = (STANDARD.rom_steps||[]).some(step=>/knee|hip|ankle|pelvic|weight|step/.test(step.metric));
const ASSESSMENT_OVERLAY_STYLE = Object.freeze({
  landmarkColor:OVERLAY_STYLE.keypoint_color||"#D9E5DC",
  landmarkRadius:Number(OVERLAY_STYLE.keypoint_radius_px)||3,
  connectorColor:OVERLAY_STYLE.connector_color||"#4A7856",
  connectorWidth:Number(OVERLAY_STYLE.connector_width_px)||4,
  targetColor:OVERLAY_STYLE.target_color||"#E18E6D",
  targetEdgeWidth:Number(OVERLAY_STYLE.target_edge_width_px)||6,
  targetInnerScale:Number(OVERLAY_STYLE.target_inner_scale)||.55,
  holdRingColor:OVERLAY_STYLE.hold_ring_color||"#3C8255",
  holdRingScale:Number(OVERLAY_STYLE.hold_ring_scale)||1.25,
  holdRingWidth:Number(OVERLAY_STYLE.hold_ring_width_px)||8,
  calibrationTargetColor:OVERLAY_STYLE.calibration_target_color||"#7FE5A3",
  calibrationTargetFill:OVERLAY_STYLE.calibration_target_fill||"rgba(74,120,86,.28)",
});
const ASSESSMENT_HAND_OVERLAY_STYLE = Object.freeze({
  landmarkColor:OVERLAY_STYLE.hand_keypoint_color||"rgba(217,229,220,0.72)",
  landmarkRadius:Number(OVERLAY_STYLE.hand_keypoint_radius_px)||1.4,
  connectorColor:OVERLAY_STYLE.hand_connector_color||"rgba(127,229,163,0.88)",
  connectorWidth:Number(OVERLAY_STYLE.hand_connector_width_px)||2,
});

function classifyCameraDevice({
  userAgent=navigator.userAgent || "",
  userAgentDataMobile=navigator.userAgentData && navigator.userAgentData.mobile,
  maxTouchPoints=navigator.maxTouchPoints || 0,
  screenWidth=screen.width,
  screenHeight=screen.height,
  deviceMode=URL_PARAMS.get("device_mode"),
}={}){
  if(deviceMode === "mobile" || deviceMode === "phone") return "phone";
  if(deviceMode === "tablet") return "tablet";
  if(deviceMode === "desktop" || deviceMode === "web") return "web";
  if(userAgentDataMobile === true || /iPhone|iPod/i.test(userAgent) || (/Android/i.test(userAgent) && /Mobile/i.test(userAgent))){
    return "phone";
  }
  if(/iPad/i.test(userAgent) || (/Macintosh/i.test(userAgent) && maxTouchPoints > 1) || (/Android/i.test(userAgent) && !/Mobile/i.test(userAgent))){
    return "tablet";
  }
  const shortScreenEdge = Math.min(Number(screenWidth) || Infinity, Number(screenHeight) || Infinity);
  return maxTouchPoints > 0 && shortScreenEdge <= 600 ? "phone" : "web";
}

const CAMERA_DEVICE_CLASS = classifyCameraDevice();
const CAMERA_FIT_MODE = STANDARD.posture === "full_body" || NEEDS_LOWER_BODY_VIEW
  ? "contain"
  : CAMERA_DEVICE_CLASS === "phone" ? "cover" : "contain";

function fitCameraViewport(containerWidth, containerHeight, sourceWidth, sourceHeight, fitMode=CAMERA_FIT_MODE){
  const safeContainerWidth = Math.max(1, Number(containerWidth) || 1);
  const safeContainerHeight = Math.max(1, Number(containerHeight) || 1);
  const safeSourceWidth = Math.max(1, Number(sourceWidth) || 1);
  const safeSourceHeight = Math.max(1, Number(sourceHeight) || 1);
  const scale = fitMode === "cover"
    ? Math.max(safeContainerWidth / safeSourceWidth, safeContainerHeight / safeSourceHeight)
    : Math.min(safeContainerWidth / safeSourceWidth, safeContainerHeight / safeSourceHeight);
  const width = safeSourceWidth * scale;
  const height = safeSourceHeight * scale;
  return {
    left:(safeContainerWidth - width) / 2,
    top:(safeContainerHeight - height) / 2,
    width,
    height,
    fit:fitMode,
  };
}

function responsiveVideoSettings(longEdge, shortEdge, maxFrameRate=30){
  const portrait = stage.clientHeight > stage.clientWidth;
  return {
    facingMode:"user",
    width:{ideal:portrait ? shortEdge : longEdge},
    height:{ideal:portrait ? longEdge : shortEdge},
    frameRate:{ideal:maxFrameRate, max:maxFrameRate},
  };
}

function syncCameraViewport(){
  if(!video.videoWidth || !video.videoHeight) return;
  const rect = fitCameraViewport(stage.clientWidth, stage.clientHeight, video.videoWidth, video.videoHeight, CAMERA_FIT_MODE);
  cameraFrame.style.left = `${rect.left}px`;
  cameraFrame.style.top = `${rect.top}px`;
  cameraFrame.style.width = `${rect.width}px`;
  cameraFrame.style.height = `${rect.height}px`;
  document.body.dataset.cameraDevice = CAMERA_DEVICE_CLASS;
  document.body.dataset.cameraFit = rect.fit;
  if(canvas.width !== video.videoWidth) canvas.width = video.videoWidth;
  if(canvas.height !== video.videoHeight) canvas.height = video.videoHeight;
}

window.__rehynCameraViewportTest = {
  classifyCameraDevice,
  fitCameraViewport,
  syncCameraViewport,
  deviceClass:CAMERA_DEVICE_CLASS,
  fitMode:CAMERA_FIT_MODE,
};

window.addEventListener("resize", syncCameraViewport, {passive:true});
window.addEventListener("orientationchange", () => setTimeout(syncCameraViewport, 120), {passive:true});
video.addEventListener("resize", syncCameraViewport);
if(window.ResizeObserver) new ResizeObserver(syncCameraViewport).observe(stage);

let landmarker = null, handLandmarker = null, drawingUtils = null;
let currentRep = 0;
let currentSubStep = 0;
let stepStartTime = 0;
let inTargetSince = null;
let lastInTargetTs = 0;
let stepCompleted = false;
let stepVoiceFinishedAt = 0;   // a step cannot complete until its instruction has been heard
let stepInstructionToken = 0;  // only the latest step narration is allowed to unlock its target
let running = false;
let runnerExited = false;
let cameraStream = null;
let confirmationAudioStream = null;
let audioEl = new Audio();
const voiceAudioCache = new Map();
const voiceAudioInflight = new Map();
let audioUnlockPromise = null;

let calibrating = false;
let calibrationInstructionFinished = false;
let calibrationReady = false;
let calibrationFinishing = false;
let calibrationSamples = [];
let calibrationAnchors = [];
let baselineMetrics = {};
let romBest = {};
let compensationHits = {};
let compensationEligible = {};
let compensationConsecutive = {};
let compensationLongestStreak = {};
let trackingFrames = 0;
let lowQualityFrames = 0;
let feedbackPending = false;
let latestExercisePoseLandmarks = null;
let exerciseLapTarget = null;
let exerciseLapTargetRadius = null;
let liveCompensationStreaks = {};
let liveCompensationIds = new Set();
let temporaryCompensationEvidence = null;
let lastEvidenceCaptureAt = 0;
function newExerciseLapTargetCalibration(){
  return {samples:[], target:null, ready:false, announced:false, lastCandidateAt:0};
}
let exerciseLapTargetCalibration = newExerciseLapTargetCalibration();
let exerciseLapCalibrationDiagnostic = {
  reason:"waiting_for_pose",
  guidance:"Keep your affected hand relaxed on the top of your same-side thigh.",
};

exName.textContent = CFG.name;
overlayTitle.textContent = CFG.name;
overlayBody.textContent = CFG.setup_voice;

function postRN(d){
  const message=JSON.stringify(d);
  if(window.ReactNativeWebView && typeof window.ReactNativeWebView.postMessage==="function"){
    window.ReactNativeWebView.postMessage(message);
  }else if(window.parent && window.parent!==window){
    let parentOrigin="*";
    try{if(document.referrer)parentOrigin=new URL(document.referrer).origin;}catch(e){}
    window.parent.postMessage(message,parentOrigin);
  }
}

let loopStarted=false;
let setupVoicePlayed=false;
function ensureLoop(){
  if(loopStarted) return;
  loopStarted=true;
  requestAnimationFrame(loop);
}
function resetExerciseCalibration(){
  // Camera coordinates belong to this entry, never to a saved rehab-plan ID.
  calibrating=true;
  calibrationInstructionFinished=false;
  calibrationReady=false;
  calibrationFinishing=false;
  calibrationSamples=[];
  calibrationAnchors=[];
  baselineMetrics={};
  latestExercisePoseLandmarks=null;
  exerciseLapTarget=null;
  exerciseLapTargetRadius=null;
  exerciseLapTargetCalibration=newExerciseLapTargetCalibration();
  exerciseLapCalibrationDiagnostic={
    reason:"waiting_for_pose",
    guidance:"Keep your affected hand relaxed on the top of your same-side thigh.",
  };
  restHandOpenScore=null;
  restHandOpenSamples=[];
}

function createSilentWavUrl(){
  const sampleRate = 8000;
  const sampleCount = 800;
  const buffer = new ArrayBuffer(44 + sampleCount * 2);
  const view = new DataView(buffer);
  const writeText = (offset, value) => {
    for(let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
  };
  writeText(0, "RIFF");
  view.setUint32(4, 36 + sampleCount * 2, true);
  writeText(8, "WAVE");
  writeText(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, sampleCount * 2, true);
  return URL.createObjectURL(new Blob([buffer], {type:"audio/wav"}));
}

function unlockAudioPlayback(){
  if(audioUnlockPromise) return audioUnlockPromise;
  const silentUrl = createSilentWavUrl();
  audioEl.preload = "auto";
  audioEl.setAttribute("playsinline", "true");
  audioEl.muted = false;
  audioEl.volume = 1;
  audioEl.src = silentUrl;
  const playback = audioEl.play();
  audioUnlockPromise = Promise.resolve(playback)
    // Let the short silent clip actually play before reusing this element.
    // Pausing as soon as play() resolved did not reliably retain the user
    // gesture on Safari after camera and model setup finished.
    .then(() => new Promise(resolve => setTimeout(resolve, 180)))
    .then(() => {
      audioEl.pause();
      audioEl.currentTime = 0;
      URL.revokeObjectURL(silentUrl);
      return true;
    })
    .catch(error => {
      URL.revokeObjectURL(silentUrl);
      audioUnlockPromise = null;
      postRN({type:"voice_unlock_error", message:String(error)});
      return false;
    });
  return audioUnlockPromise;
}

function playBrowserVoice(text){
  return new Promise((resolve) => {
    let settled=false, utterance=null, watchdog=null;
    const finish=(status)=>{
      if(settled) return;
      settled=true;
      if(watchdog) clearTimeout(watchdog);
      if(utterance){ utterance.onend=null; utterance.onerror=null; }
      if(stopActiveVoice===stop) stopActiveVoice=null;
      resolve(status);
    };
    const stop=()=>{
      try{ window.speechSynthesis.cancel(); }catch(e){}
      finish("interrupted");
    };
    if(!("speechSynthesis" in window) || !window.SpeechSynthesisUtterance){
      finish("unavailable");
      return;
    }
    try{
      window.speechSynthesis.cancel();
      utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "en-US";
      utterance.rate = 0.88;
      utterance.pitch = 1.0;
      utterance.onend = () => finish("completed");
      utterance.onerror = () => {
        window.speechSynthesis.cancel();
        finish("unavailable");
      };
      stopActiveVoice=stop;
      window.speechSynthesis.speak(utterance);
      // This is only a deadlock guard. It never marks speech as complete: it
      // first stops playback, then reports that voice was unavailable.
      watchdog=setTimeout(()=>{
        window.speechSynthesis.cancel();
        finish("unavailable");
      },Math.min(60000,Math.max(15000,text.length*220)));
    }catch(e){
      try{ window.speechSynthesis.cancel(); }catch(ignore){}
      finish("unavailable");
    }
  });
}

// One instruction plays at a time. A newer instruction (for example "Wonderful,
// here we go" when the patient taps Continue while the feedback is still being
// read) interrupts the current one, and the interrupted call settles at once:
// it never keeps waiting on the shared audio element, and it never clears the
// listeners of the instruction that replaced it (that used to freeze the next
// repetition's start for up to 45 seconds).
let voiceSequence = 0;
let activeVoiceSequence = 0;
let stopActiveVoice = null;
async function playVoice(text,purpose="instruction"){
  if(runnerExited) return "interrupted";
  if(!VOICE_GUIDANCE_ENABLED || !text){
    voiceText.textContent = "Voice guidance off · follow on-screen text";
    return "disabled";
  }
  const sequence = ++voiceSequence;
  if(stopActiveVoice) stopActiveVoice();
  activeVoiceSequence=sequence;
  try{
    voiceText.textContent = "Listen to the full instruction…";
    const audioSource = await fetchVoiceAudio(text,purpose);
    if(sequence !== voiceSequence) return "interrupted";
    if(stopActiveVoice) stopActiveVoice();
    audioEl.pause();
    audioEl.muted = false;
    audioEl.volume = 1;
    audioEl.src = audioSource;
    audioEl.load();
    const playbackStatus=await new Promise((resolve, reject) => {
      let settled = false;
      let timeout = setTimeout(() => finish(() => {
        audioEl.pause();
        reject(new Error("Audio instruction timed out"));
      }), 60000);
      const onEnded = () => finish(() => resolve("completed"));
      const onError = () => finish(() => {
        audioEl.pause();
        reject(new Error("Audio element could not play the instruction"));
      });
      const onMetadata = () => {
        // Allow the full clip plus a generous buffer. If this guard ever fires,
        // stop the audio before falling back; never unlock over audible speech.
        if(Number.isFinite(audioEl.duration) && audioEl.duration > 0){
          clearTimeout(timeout);
          timeout = setTimeout(() => finish(() => {
            audioEl.pause();
            reject(new Error("Audio instruction did not finish"));
          }), audioEl.duration * 1000 + 5000);
        }
      };
      const finish = callback => {
        if(settled) return;
        settled = true;
        clearTimeout(timeout);
        audioEl.removeEventListener("ended", onEnded);
        audioEl.removeEventListener("error", onError);
        audioEl.removeEventListener("loadedmetadata", onMetadata);
        if(stopActiveVoice === stop) stopActiveVoice = null;
        callback();
      };
      const stop = () => finish(() => {
        audioEl.pause();
        resolve("interrupted");
      });
      stopActiveVoice = stop;
      audioEl.addEventListener("ended", onEnded);
      audioEl.addEventListener("error", onError);
      audioEl.addEventListener("loadedmetadata", onMetadata);
      const playback = audioEl.play();
      if(playback && typeof playback.catch === "function"){
        playback.catch(error => { if(!settled && sequence === voiceSequence) finish(() => reject(error)); });
      }
    });
    if(sequence !== voiceSequence || playbackStatus !== "completed") return "interrupted";
    if(activeVoiceSequence===sequence) activeVoiceSequence=0;
    voiceText.textContent = "Instruction finished · get ready";
    return "completed";
  }catch(e){
    audioEl.pause();
    if(sequence !== voiceSequence) return "interrupted";
    voiceText.textContent = "Using device voice";
    const browserStatus = await playBrowserVoice(text);
    if(sequence !== voiceSequence || browserStatus === "interrupted") return "interrupted";
    if(activeVoiceSequence===sequence) activeVoiceSequence=0;
    voiceText.textContent = browserStatus === "completed"
      ? "Instruction finished · get ready"
      : "Voice unavailable · follow the on-screen instruction";
    return browserStatus;
  }
}

function fetchVoiceAudio(text,purpose="instruction"){
  const preparedUrl = purpose === "instruction" && CFG.prepared_voice_assets && CFG.prepared_voice_assets[text];
  const key = `${purpose}::${preparedUrl || "generated"}::${text}`;
  if(voiceAudioCache.has(key)) return Promise.resolve(voiceAudioCache.get(key));
  if(voiceAudioInflight.has(key)) return voiceAudioInflight.get(key);
  const pendingRequest = preparedUrl
    ? fetch(preparedUrl,{cache:"force-cache"}).then(async res => {
        if(!res.ok) throw new Error("prepared tts fail");
        const objectUrl=URL.createObjectURL(await res.blob());
        voiceAudioCache.set(key,objectUrl);
        return objectUrl;
      })
    : fetch(`${API_BASE}/tts/generate`,{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({text,purpose})
      }).then(async res => {
        if(!res.ok) throw new Error("tts fail");
        const data = await res.json();
        const source="data:audio/mpeg;base64,"+data.audio_b64;
        voiceAudioCache.set(key,source);
        return source;
      });
  const request = pendingRequest.finally(() => voiceAudioInflight.delete(key));
  voiceAudioInflight.set(key, request);
  return request;
}

function prefetchVoice(text){
  if(!VOICE_GUIDANCE_ENABLED || !text) return Promise.resolve(null);
  return fetchVoiceAudio(text).catch(() => null);
}

async function setupCamera(){
  try{
    cameraStream = await navigator.mediaDevices.getUserMedia({video:responsiveVideoSettings(640, 480),audio:false});
    if(runnerExited){ stopExerciseRunner(); return false; }
    video.srcObject = cameraStream;
    await new Promise(r => video.onloadedmetadata = r);
    syncCameraViewport();
    try{
      confirmationAudioStream = await navigator.mediaDevices.getUserMedia({audio:true});
    }catch(e){
      confirmationAudioStream = null;
    }
    if(runnerExited){ stopExerciseRunner(); return false; }
    return true;
  }catch(e){
    captionEl.textContent = "Camera permission denied.";
    postRN({type:"camera_error", message: String(e)});
    return false;
  }
}

// The movement models are created as soon as the page opens (their files are
// already cached by the app at sign-in), so by the time the patient taps Start
// they are usually ready and the camera can begin at once.
let modelSetupPromise = null;
function warmUpModels(){
  if(!modelSetupPromise){
    modelSetupPromise = setupPose().catch(error => {
      modelSetupPromise = null;   // a failed warm-up is retried on Start
      throw error;
    });
  }
  return modelSetupPromise;
}
async function setupPose(){
  const fr = await FilesetResolver.forVisionTasks("/vendor/mediapipe/wasm");
  const posePromise=PoseLandmarker.createFromOptions(fr,{
    baseOptions:{modelAssetPath:"/vendor/mediapipe/models/pose_landmarker_lite.task"},
    runningMode:"VIDEO", numPoses:1
  });
  // Cylindrical grasp needs both models. Initialise them together instead of
  // making the patient wait for pose setup and then a second hand-model setup.
  let handPromise=Promise.resolve(null);
  if(NEEDS_HAND_TRACKING){
    handPromise=HandLandmarker.createFromOptions(fr,{
      baseOptions:{modelAssetPath:"/vendor/mediapipe/models/hand_landmarker.task"},
      // Two candidates cover the patient and a helper while keeping hand
      // tracking responsive on lower-power phones and laptops.
      runningMode:"VIDEO", numHands:2,
      minHandDetectionConfidence:0.65,
      minHandPresenceConfidence:0.65,
      minTrackingConfidence:0.7,
    });
  }
  [landmarker,handLandmarker]=await Promise.all([posePromise,handPromise]);
  drawingUtils = new DrawingUtils(ctx);
}
// Up to four hands may be in view (the patient's two plus a helper's); keep
// the one nearest the affected wrist of the pose, with the reported handedness
// as a tie-breaker, and follow it between frames.
let affectedHandTrackWrist = null, affectedHandTrackSeenAt = 0;
function anatomicalHandedness(rawCategory){
  // MediaPipe handedness assumes mirrored selfie input; inference receives the
  // unmirrored camera pixels, so swap its label back to the patient's anatomy.
  if(rawCategory === "Right") return "Left";
  if(rawCategory === "Left") return "Right";
  return "";
}
function selectRehabAffectedHand(result, lm, now){
  const list = result && Array.isArray(result.landmarks) ? result.landmarks : [];
  if(!list.length) return null;
  const expectedSide = AFFECTED_SIDE === "left" ? "Left" : "Right";
  const poseWrist = lm && lm[ACTIVE.wrist] && pointVisible(lm[ACTIVE.wrist]) ? lm[ACTIVE.wrist] : null;
  const shoulderWidth = lm && lm[11] && lm[12] ? Math.hypot(lm[11].x-lm[12].x, lm[11].y-lm[12].y) : 0.22;
  const associationRadius = Math.max(0.14, Math.min(0.32, shoulderWidth * 0.85));
  const trackIsFresh = affectedHandTrackWrist && now - affectedHandTrackSeenAt < 900;
  const candidates = list.map((landmarks, index) => {
    const category = result.handednesses && result.handednesses[index] && result.handednesses[index][0] ? result.handednesses[index][0] : null;
    const handedness = anatomicalHandedness(category && category.categoryName);
    const confidence = category && Number.isFinite(category.score) ? category.score : 0;
    const wrist = landmarks && landmarks[0];
    const poseDistance = poseWrist && wrist ? Math.hypot(wrist.x-poseWrist.x, wrist.y-poseWrist.y) : Infinity;
    const trackDistance = trackIsFresh && wrist ? Math.hypot(wrist.x-affectedHandTrackWrist.x, wrist.y-affectedHandTrackWrist.y) : Infinity;
    const sidePenalty = handedness && handedness !== expectedSide ? 0.18 : 0;
    const score = (Number.isFinite(poseDistance) ? poseDistance * 4 : 0) + (Number.isFinite(trackDistance) ? trackDistance * 0.7 : 0) + sidePenalty - confidence * 0.05;
    return {landmarks, handedness, wrist, poseDistance, trackDistance, score};
  }).filter(candidate => candidate.wrist);
  let eligible = poseWrist
    ? candidates.filter(candidate => candidate.poseDistance <= associationRadius)
    : candidates.filter(candidate => candidate.handedness === expectedSide || (trackIsFresh && candidate.trackDistance <= 0.16));
  // A single visible hand is the patient's own hand (helpers show up as extra
  // hands), so never lose it over an uncertain handedness label or a pose
  // wrist that drifted.
  if(!eligible.length && candidates.length === 1) eligible = candidates;
  if(!eligible.length) return null;
  eligible.sort((a,b) => a.score - b.score);
  affectedHandTrackWrist = {x:eligible[0].wrist.x, y:eligible[0].wrist.y};
  affectedHandTrackSeenAt = now;
  return eligible[0].landmarks;
}
// Median finger opening angle (180 = straight) of the tracked hand.
function handOpeningDegrees(handLm){
  if(!handLm || handLm.length < 21) return NaN;
  return median([angle(handLm[5],handLm[6],handLm[8]), angle(handLm[9],handLm[10],handLm[12]), angle(handLm[13],handLm[14],handLm[16]), angle(handLm[17],handLm[18],handLm[20])]);
}

function rad2deg(r){ return r*180/Math.PI; }
function clamp(value, low, high){ return Math.max(low, Math.min(high, value)); }
function pointVisible(point){ return Boolean(point) && (point.visibility == null || point.visibility >= 0.55) && point.x >= -0.05 && point.x <= 1.05 && point.y >= -0.05 && point.y <= 1.05; }
function angle(a,b,c){
  if(!a || !b || !c) return NaN;
  const ab={x:a.x-b.x,y:a.y-b.y,z:(a.z||0)-(b.z||0)};
  const cb={x:c.x-b.x,y:c.y-b.y,z:(c.z||0)-(b.z||0)};
  const dot=ab.x*cb.x+ab.y*cb.y+ab.z*cb.z;
  const mag=Math.hypot(ab.x,ab.y,ab.z)*Math.hypot(cb.x,cb.y,cb.z);
  return mag > 0.000001 ? rad2deg(Math.acos(clamp(dot/mag,-1,1))) : NaN;
}
function midpoint(a,b){ return {x:(a.x+b.x)/2,y:(a.y+b.y)/2,z:((a.z||0)+(b.z||0))/2}; }
function median(values){
  const clean=values.filter(Number.isFinite).sort((a,b)=>a-b);
  if(!clean.length) return 0;
  const mid=Math.floor(clean.length/2);
  return clean.length%2 ? clean[mid] : (clean[mid-1]+clean[mid])/2;
}
function sideIndexes(side){
  return side === "left"
    ? {shoulder:11,elbow:13,wrist:15,hip:23,knee:25,ankle:27,heel:29,toe:31}
    : {shoulder:12,elbow:14,wrist:16,hip:24,knee:26,ankle:28,heel:30,toe:32};
}
const ACTIVE = sideIndexes(AFFECTED_SIDE);
const OTHER = sideIndexes(AFFECTED_SIDE === "left" ? "right" : "left");

function evidencePoint(lm,index,width,height,mirrorX=false){
  const point=lm&&lm[index];
  if(!pointVisible(point)) return null;
  return {x:(mirrorX ? 1-point.x : point.x)*width,y:point.y*height};
}
function strokeDottedPath(targetCtx,points,closed=false){
  const clean=points.filter(Boolean);
  if(clean.length < 2) return;
  const scale=Math.max(1,Math.min(targetCtx.canvas.width,targetCtx.canvas.height)/640);
  targetCtx.save();
  targetCtx.beginPath();
  targetCtx.moveTo(clean[0].x,clean[0].y);
  clean.slice(1).forEach(point=>targetCtx.lineTo(point.x,point.y));
  if(closed) targetCtx.closePath();
  targetCtx.setLineDash([2*scale,9*scale]);
  targetCtx.lineCap="round";
  targetCtx.lineJoin="round";
  targetCtx.lineWidth=4.5*scale;
  targetCtx.strokeStyle="#FF3B30";
  targetCtx.shadowColor="rgba(86,0,0,.78)";
  targetCtx.shadowBlur=4*scale;
  targetCtx.stroke();
  targetCtx.restore();
}
function drawCompensationHighlights(targetCtx,lm,ids,{mirrorX=false}={}){
  if(!lm || !ids || !ids.length) return;
  const width=targetCtx.canvas.width,height=targetCtx.canvas.height;
  const selected=new Set(ids);
  if(selected.has("trunk_lean") || selected.has("trunk_forward")){
    const leftShoulder=evidencePoint(lm,11,width,height,mirrorX);
    const rightShoulder=evidencePoint(lm,12,width,height,mirrorX);
    const rightHip=evidencePoint(lm,24,width,height,mirrorX);
    const leftHip=evidencePoint(lm,23,width,height,mirrorX);
    const torso=[leftShoulder,rightShoulder,rightHip,leftHip];
    if(torso.every(Boolean)){
      targetCtx.save();
      targetCtx.beginPath();
      targetCtx.moveTo(torso[0].x,torso[0].y);
      torso.slice(1).forEach(point=>targetCtx.lineTo(point.x,point.y));
      targetCtx.closePath();
      targetCtx.fillStyle="rgba(255,59,48,.08)";
      targetCtx.fill();
      targetCtx.restore();
      strokeDottedPath(targetCtx,torso,true);
      strokeDottedPath(targetCtx,[midpoint(leftShoulder,rightShoulder),midpoint(leftHip,rightHip)]);
    }
  }
  if(selected.has("head_drop")){
    const nose=evidencePoint(lm,0,width,height,mirrorX);
    const earL=evidencePoint(lm,7,width,height,mirrorX);
    const earR=evidencePoint(lm,8,width,height,mirrorX);
    const wrist=evidencePoint(lm,ACTIVE.wrist,width,height,mirrorX);
    if(nose && earL && earR){
      const earMid={x:(earL.x+earR.x)/2,y:(earL.y+earR.y)/2};
      const earWidth=Math.max(24,Math.hypot(earL.x-earR.x,earL.y-earR.y));
      const scale=Math.max(1,Math.min(width,height)/640);
      targetCtx.save();
      targetCtx.beginPath();
      targetCtx.ellipse(earMid.x,earMid.y+earWidth*.05,earWidth*.62,earWidth*.78,0,0,Math.PI*2);
      targetCtx.setLineDash([2*scale,9*scale]);
      targetCtx.lineCap="round";
      targetCtx.lineWidth=4.5*scale;
      targetCtx.strokeStyle="#FF3B30";
      targetCtx.shadowColor="rgba(86,0,0,.78)";
      targetCtx.shadowBlur=4*scale;
      targetCtx.stroke();
      targetCtx.restore();
      strokeDottedPath(targetCtx,[earL,earR]);
      if(wrist) strokeDottedPath(targetCtx,[nose,wrist]);
    }
  }
  if(selected.has("shoulder_hike")){
    const shoulder=evidencePoint(lm,ACTIVE.shoulder,width,height,mirrorX);
    const otherShoulder=evidencePoint(lm,OTHER.shoulder,width,height,mirrorX);
    const earIndex=ACTIVE.shoulder===11 ? 7 : 8;
    const ear=evidencePoint(lm,earIndex,width,height,mirrorX);
    if(shoulder){
      const shoulderWidth=otherShoulder ? Math.hypot(shoulder.x-otherShoulder.x,shoulder.y-otherShoulder.y) : width*.18;
      const neckHeight=ear ? Math.hypot(shoulder.x-ear.x,shoulder.y-ear.y) : height*.10;
      const radiusX=Math.max(28,shoulderWidth*.32);
      const radiusY=Math.max(34,neckHeight*.72);
      const centerY=ear ? (shoulder.y+ear.y)/2 : shoulder.y-radiusY*.28;
      const scale=Math.max(1,Math.min(width,height)/640);
      targetCtx.save();
      targetCtx.beginPath();
      targetCtx.ellipse(shoulder.x,centerY,radiusX,radiusY,0,0,Math.PI*2);
      targetCtx.setLineDash([2*scale,9*scale]);
      targetCtx.lineCap="round";
      targetCtx.lineWidth=4.5*scale;
      targetCtx.strokeStyle="#FF3B30";
      targetCtx.shadowColor="rgba(86,0,0,.78)";
      targetCtx.shadowBlur=4*scale;
      targetCtx.stroke();
      targetCtx.restore();
      if(otherShoulder) strokeDottedPath(targetCtx,[otherShoulder,shoulder]);
    }
  }
  if(selected.has("wrist_flexion")){
    const elbow=evidencePoint(lm,ACTIVE.elbow,width,height,mirrorX);
    const wrist=evidencePoint(lm,ACTIVE.wrist,width,height,mirrorX);
    const indexTip=evidencePoint(lm,ACTIVE.wrist===15 ? 19 : 20,width,height,mirrorX);
    const pinkyTip=evidencePoint(lm,ACTIVE.wrist===15 ? 17 : 18,width,height,mirrorX);
    const handBase=indexTip&&pinkyTip ? midpoint(indexTip,pinkyTip) : null;
    if(elbow&&wrist&&handBase){
      strokeDottedPath(targetCtx,[elbow,wrist,handBase]);
      const scale=Math.max(1,Math.min(width,height)/640);
      targetCtx.save();
      targetCtx.beginPath();
      targetCtx.arc(wrist.x,wrist.y,18*scale,0,Math.PI*2);
      targetCtx.setLineDash([2*scale,9*scale]);
      targetCtx.lineCap="round";
      targetCtx.lineWidth=4.5*scale;
      targetCtx.strokeStyle="#FF3B30";
      targetCtx.shadowColor="rgba(86,0,0,.78)";
      targetCtx.shadowBlur=4*scale;
      targetCtx.stroke();
      targetCtx.restore();
    }
  }
}
function clearTemporaryCompensationEvidence(){
  temporaryCompensationEvidence=null;
  lastEvidenceCaptureAt=0;
  fbEvidenceImage.removeAttribute("src");
  fbEvidenceLabels.textContent="";
  fbEvidence.classList.add("hidden");
  fbEl.classList.remove("hasEvidence");
}
function captureTemporaryCompensationEvidence(lm,ids,severity,values){
  if(!TEMPORARY_COMPENSATION_EVIDENCE || !lm || !video.videoWidth || !video.videoHeight || video.readyState < 2) return;
  const supportedIds=ids.filter(id=>EVIDENCE_COMPENSATION_IDS.has(id));
  if(!supportedIds.length) return;
  const now=performance.now();
  const addsArea=!temporaryCompensationEvidence || supportedIds.some(id=>!temporaryCompensationEvidence.ids.includes(id));
  const improvesPeak=!temporaryCompensationEvidence || severity >= temporaryCompensationEvidence.severity+.08;
  if(!addsArea && (!improvesPeak || now-lastEvidenceCaptureAt < 450)) return;
  const sourceWidth=video.videoWidth,sourceHeight=video.videoHeight;
  const scale=Math.min(1,900/Math.max(sourceWidth,sourceHeight));
  const shot=document.createElement("canvas");
  shot.width=Math.max(1,Math.round(sourceWidth*scale));
  shot.height=Math.max(1,Math.round(sourceHeight*scale));
  const shotCtx=shot.getContext("2d");
  if(!shotCtx) return;
  // The patient sees a mirrored selfie view. Bake that same view into the
  // temporary frame so the red outline matches the on-screen body area.
  shotCtx.save();
  shotCtx.translate(shot.width,0);
  shotCtx.scale(-1,1);
  shotCtx.drawImage(video,0,0,shot.width,shot.height);
  shotCtx.drawImage(canvas,0,0,shot.width,shot.height);
  shotCtx.restore();
  drawCompensationHighlights(shotCtx,lm,supportedIds,{mirrorX:true});
  temporaryCompensationEvidence={
    dataUrl:shot.toDataURL("image/jpeg",.84),
    ids:[...supportedIds],
    severity,
    values:{...values},
  };
  lastEvidenceCaptureAt=now;
}
function showTemporaryCompensationEvidence(confirmed){
  const confirmedIds=new Set((confirmed||[]).map(rule=>rule.id));
  const visibleIds=temporaryCompensationEvidence
    ? temporaryCompensationEvidence.ids.filter(id=>confirmedIds.has(id))
    : [];
  if(!visibleIds.length){
    fbEvidence.classList.add("hidden");
    fbEl.classList.remove("hasEvidence");
    return;
  }
  fbEvidenceImage.src=temporaryCompensationEvidence.dataUrl;
  fbEvidenceLabels.textContent="";
  visibleIds.forEach(id=>{
    const rule=(confirmed||[]).find(item=>item.id===id);
    const degrees=Math.round(Number(temporaryCompensationEvidence.values[id])||0);
    const chip=document.createElement("span");
    chip.className="evidenceLabel";
    chip.textContent=`${rule&&rule.label ? rule.label : id.replace(/_/g," ")}${degrees ? ` · ${degrees}°` : ""}`;
    fbEvidenceLabels.appendChild(chip);
  });
  fbEvidence.classList.remove("hidden");
  fbEl.classList.add("hasEvidence");
}

function isExerciseLapTarget(step){
  return !!step && !!step.target && step.target.landmark === "LAP_DYNAMIC";
}
function exercisePoseDistance(a,b){
  return Math.hypot(a.x-b.x,a.y-b.y);
}
function exerciseLandmarkIsUsable(point,minVisibility=.45){
  return !!point
    && Number.isFinite(point.x)
    && Number.isFinite(point.y)
    && (point.visibility == null || point.visibility >= minVisibility);
}
function exerciseLandmarkIsInFrame(point,minVisibility=.45,margin=.025){
  return exerciseLandmarkIsUsable(point,minVisibility)
    && point.x >= margin && point.x <= 1-margin
    && point.y >= margin && point.y <= 1-margin;
}
function exerciseLapWristZoneReason(wrist,hip,midShoulder,torsoLength){
  const wristBelowHip=wrist.y-hip.y;
  const wristFromHipX=Math.abs(wrist.x-hip.x);
  const wristBelowShoulders=wrist.y-midShoulder.y;
  if(wristBelowShoulders < torsoLength*.55 || wristBelowHip < -torsoLength*.12) return "hand_too_high";
  if(wristBelowHip > torsoLength*.95) return "hand_too_low";
  if(wristFromHipX > torsoLength*.90) return "hand_too_far_side";
  return null;
}
function exerciseLapTargetCandidateStatus(lm){
  const sideLabel=AFFECTED_SIDE === "left" ? "left" : "right";
  if(!lm || lm.length < 33) return {
    candidate:null,
    reason:"pose_missing",
    guidance:"Sit facing the camera and keep your face, shoulders, affected arm, and upper thigh visible.",
  };
  const affected={shoulder:lm[ACTIVE.shoulder],elbow:lm[ACTIVE.elbow],wrist:lm[ACTIVE.wrist],hip:lm[ACTIVE.hip]};
  const unaffected={shoulder:lm[OTHER.shoulder],elbow:lm[OTHER.elbow],wrist:lm[OTHER.wrist],hip:lm[OTHER.hip]};
  const leftShoulder=lm[11];
  const rightShoulder=lm[12];
  const lapVisibility=.35;
  if(!exerciseLandmarkIsInFrame(leftShoulder,lapVisibility) || !exerciseLandmarkIsInFrame(rightShoulder,lapVisibility)) return {
    candidate:null,
    reason:"shoulders_not_visible",
    guidance:"Move the camera until both shoulders are clearly visible.",
  };
  if(!exerciseLandmarkIsInFrame(affected.hip,lapVisibility) || !exerciseLandmarkIsInFrame(affected.wrist,lapVisibility)) return {
    candidate:null,
    reason:"affected_lap_not_visible",
    guidance:`Tilt the camera down slightly so your ${sideLabel} hand and the top of your ${sideLabel} thigh are visible.`,
  };
  const midShoulder=midpoint(leftShoulder,rightShoulder);
  const torsoLength=exercisePoseDistance(midShoulder,affected.hip);
  if(torsoLength < .07) return {
    candidate:null,
    reason:"body_too_small",
    guidance:"Move slightly closer to the camera while keeping your face and upper thigh visible.",
  };
  const zoneReason=exerciseLapWristZoneReason(affected.wrist,affected.hip,midShoulder,torsoLength);
  if(zoneReason){
    const unaffectedVisible=exerciseLandmarkIsInFrame(unaffected.hip,lapVisibility)
      && exerciseLandmarkIsInFrame(unaffected.wrist,lapVisibility);
    const unaffectedTorsoLength=unaffectedVisible ? exercisePoseDistance(midShoulder,unaffected.hip) : 0;
    const otherHandOnLap=unaffectedVisible && unaffectedTorsoLength >= .07
      && !exerciseLapWristZoneReason(unaffected.wrist,unaffected.hip,midShoulder,unaffectedTorsoLength);
    if(otherHandOnLap) return {
      candidate:null,
      reason:"wrong_hand_on_lap",
      guidance:`Your other hand is on your lap. Place your ${sideLabel} hand on the top of your ${sideLabel} thigh.`,
    };
    const guidanceByReason={
      hand_too_high:`Lower your ${sideLabel} hand from your chest or face and rest it on top of your ${sideLabel} thigh.`,
      hand_too_low:`Place your ${sideLabel} hand on top of your upper thigh rather than beside the chair.`,
      hand_too_far_side:`Move your ${sideLabel} hand inward so it rests on top of your ${sideLabel} thigh.`,
    };
    return {candidate:null,reason:zoneReason,guidance:guidanceByReason[zoneReason]};
  }
  return {
    candidate:{
      x:Math.max(.10,Math.min(.90,affected.wrist.x)),
      y:Math.max(.50,Math.min(.90,affected.wrist.y)),
      shoulderWidth:Math.max(.03,exercisePoseDistance(leftShoulder,rightShoulder)),
      bodyX:(midShoulder.x+affected.hip.x)/2,
      bodyY:(midShoulder.y+affected.hip.y)/2,
    },
    reason:"stabilizing",
    guidance:`Keep your ${sideLabel} hand relaxed and still on your upper thigh for one moment.`,
  };
}
function updateExerciseLapTargetCalibration(lm,now){
  if(!HAS_DYNAMIC_LAP_TARGET || exerciseLapTargetCalibration.ready) return;
  const status=exerciseLapTargetCandidateStatus(lm);
  exerciseLapCalibrationDiagnostic={reason:status.reason,guidance:status.guidance};
  const candidate=status.candidate;
  if(!candidate){
    if(exerciseLapTargetCalibration.lastCandidateAt && now-exerciseLapTargetCalibration.lastCandidateAt <= 900) return;
    exerciseLapTargetCalibration.samples=[];
    exerciseLapTargetCalibration.target=null;
    return;
  }
  exerciseLapTargetCalibration.lastCandidateAt=now;
  const samples=exerciseLapTargetCalibration.samples;
  samples.push({
    x:candidate.x,
    y:candidate.y,
    bodyX:candidate.bodyX,
    bodyY:candidate.bodyY,
    t:now,
    shoulderWidth:candidate.shoulderWidth,
  });
  // Keep the last 1.2 s of samples. The window is time-based: a fixed count of
  // 24 would span only 0.4 s at 60 frames per second, less than
  // LAP_CALIBRATION_MIN_MS, and the hand could never be judged still.
  while(samples.length > 120 || (samples.length && now-samples[0].t > 1200)) samples.shift();
  let center={x:median(samples.map(sample=>sample.x)),y:median(samples.map(sample=>sample.y))};
  exerciseLapTargetCalibration.target=center;
  if(samples.length < LAP_CALIBRATION_MIN_SAMPLES) return;
  const width=median(samples.map(sample=>sample.shoulderWidth))||.03;
  const maxJitter=Math.max(.028,width*.18);
  const maxBodyJitter=Math.max(.035,width*.22);
  const bodyCenter={
    x:median(samples.map(sample=>sample.bodyX)),
    y:median(samples.map(sample=>sample.bodyY)),
  };
  const stableSamples=samples.filter(sample=>
    Math.hypot(sample.x-center.x,sample.y-center.y) <= maxJitter
    && Math.hypot(sample.bodyX-bodyCenter.x,sample.bodyY-bodyCenter.y) <= maxBodyJitter
  );
  const requiredStableSamples=Math.max(LAP_CALIBRATION_MIN_SAMPLES,Math.ceil(samples.length*LAP_CALIBRATION_STABLE_RATIO));
  if(stableSamples.length < requiredStableSamples) return;
  const stableDuration=stableSamples[stableSamples.length-1].t-stableSamples[0].t;
  if(stableDuration < LAP_CALIBRATION_MIN_MS) return;
  center={x:median(stableSamples.map(sample=>sample.x)),y:median(stableSamples.map(sample=>sample.y))};
  exerciseLapTargetCalibration.ready=true;
  exerciseLapTargetCalibration.target=center;
  exerciseLapTarget={...center};
  exerciseLapTargetRadius=Math.min(Math.max(.10,exerciseShoulderWidth(lm)*.55),.18);
  if(!exerciseLapTargetCalibration.announced){
    exerciseLapTargetCalibration.announced=true;
    postRN({
      type:"exercise_lap_target_calibrated",
      exercise_id:CFG.name,
      affected_side:AFFECTED_SIDE,
      x:+center.x.toFixed(4),
      y:+center.y.toFixed(4),
      radius:+exerciseLapTargetRadius.toFixed(4),
      sample_count:samples.length,
    });
  }
}
// Hand-to-mouth: the target sits just below the patient's own mouth (where
// the wrist is when a cup is at the lips), following the mouth landmarks so a
// taller or shorter patient, or a different camera height, needs no preset.
// Lightly smoothed; falls back to the authored point until the face is seen.
let mouthTargetSmoothed=null;
function mouthFollowingTarget(sub){
  const lm=latestExercisePoseLandmarks;
  const left=lm && lm[9], right=lm && lm[10];
  if(pointVisible(left) && pointVisible(right)){
    const mouth=midpoint(left,right);
    const below=exerciseShoulderWidth(lm)*0.30;
    const next={x:mouth.x,y:mouth.y+below};
    mouthTargetSmoothed=mouthTargetSmoothed
      ? {x:mouthTargetSmoothed.x*0.7+next.x*0.3,y:mouthTargetSmoothed.y*0.7+next.y*0.3}
      : next;
  }
  const point=mouthTargetSmoothed || sub.target;
  return {x:point.x,y:point.y,r:sub.target.r,landmark:sub.target.landmark};
}
function effectiveExerciseTarget(sub){
  if(isExerciseLapTarget(sub)) return exerciseLapTarget || exerciseLapTargetCalibration.target || sub.target;
  if(sub && sub.target && sub.target.landmark === "MOUTH_DYNAMIC") return mouthFollowingTarget(sub);
  return sub.target;
}

function poseTrackingQuality(lm){
  if(!lm) return 0;
  const metrics=(STANDARD.rom_steps||[]).map(step=>step.metric);
  const needsLowerBody=metrics.some(metric=>/knee|hip|ankle|pelvic|weight|step/.test(metric));
  const required = STANDARD.posture === "full_body"
    ? [11,12,23,24,25,26,27,28,29,30,31,32]
    : needsLowerBody
      ? [ACTIVE.shoulder,ACTIVE.hip,ACTIVE.knee,ACTIVE.ankle,ACTIVE.heel,ACTIVE.toe]
      : [11,12,13,14,15,16,23,24];
  return required.filter(index=>pointVisible(lm[index])).length / required.length;
}
function handTrackingQuality(handLm){
  if(!handLm || handLm.length < 21) return 0;
  const required=[0,4,5,8,9,12,13,16,17,20];
  return required.filter(index=>pointVisible(handLm[index])).length / required.length;
}
function trackingQuality(lm, handLm){
  return STANDARD.tracking_mode === "hand" ? handTrackingQuality(handLm) : poseTrackingQuality(lm);
}
function poseAlignmentDeviation(hip,knee,ankle){
  if(!hip || !knee || !ankle) return NaN;
  const lineX=(hip.x+ankle.x)/2;
  const length=Math.max(0.02,Math.hypot(hip.x-ankle.x,hip.y-ankle.y));
  return rad2deg(Math.atan2(Math.abs(knee.x-lineX),length));
}
function poseWristBendDegrees(lm){
  if(!lm || !lm[ACTIVE.elbow] || !lm[ACTIVE.wrist]) return NaN;
  const left=ACTIVE.wrist===15;
  const indexTip=lm[left ? 19 : 20];
  const pinkyTip=lm[left ? 17 : 18];
  const elbow=lm[ACTIVE.elbow], wrist=lm[ACTIVE.wrist];
  if(!pointVisible(elbow) || !pointVisible(wrist) || !pointVisible(indexTip) || !pointVisible(pinkyTip)) return NaN;
  const handBase=midpoint(indexTip,pinkyTip);
  const shoulderWidth=(lm[11]&&lm[12]) ? Math.max(.03,Math.hypot(lm[11].x-lm[12].x,lm[11].y-lm[12].y,(lm[11].z||0)-(lm[12].z||0))) : .18;
  const forearmLength=Math.hypot(wrist.x-elbow.x,wrist.y-elbow.y,(wrist.z||0)-(elbow.z||0));
  const handLength=Math.hypot(handBase.x-wrist.x,handBase.y-wrist.y,(handBase.z||0)-(wrist.z||0));
  // A closed or edge-on hand can collapse the pose model's coarse finger
  // endpoints onto the wrist. In that view the camera cannot support a wrist
  // alignment judgment, so return no measurement instead of a false warning.
  if(forearmLength < shoulderWidth*.40 || handLength < shoulderWidth*.16) return NaN;
  const jointAngle=angle(elbow,wrist,handBase);
  return Number.isFinite(jointAngle) ? Math.abs(180-jointAngle) : NaN;
}

function projectedWristBendDegrees(lm,handLm){
  if(!lm || !handLm) return NaN;
  const elbow=lm[ACTIVE.elbow], wrist=lm[ACTIVE.wrist];
  const handWrist=handLm[0], middleBase=handLm[9];
  if(![elbow,wrist,handWrist,middleBase,handLm[5],handLm[17]].every(pointVisible)) return NaN;
  // Both models share image x/y coordinates, but their z origins and scales
  // differ. Compare directions in source-image pixels, never a mixed 3D joint
  // or the cropped/mirrored overlay. MCP 9 stays on the palm when fingers curl.
  const aspect=video.videoWidth>0 && video.videoHeight>0 ? video.videoWidth/video.videoHeight : NaN;
  if(!Number.isFinite(aspect)) return NaN;
  const distance=(a,b)=>Math.hypot((a.x-b.x)*aspect,a.y-b.y);
  const forearm={x:(wrist.x-elbow.x)*aspect,y:wrist.y-elbow.y};
  const palm={x:(middleBase.x-handWrist.x)*aspect,y:middleBase.y-handWrist.y};
  const forearmLength=Math.hypot(forearm.x,forearm.y), palmLength=Math.hypot(palm.x,palm.y);
  const shoulderWidth=pointVisible(lm[11]) && pointVisible(lm[12]) ? distance(lm[11],lm[12]) : NaN;
  if(!Number.isFinite(shoulderWidth) || forearmLength < shoulderWidth*.40
    || palmLength < shoulderWidth*.10 || palmLength > forearmLength*.85) return NaN;
  // Require agreement about which wrist is being measured. Cached/other-hand
  // landmarks must not create a bend against the affected forearm.
  if(distance(wrist,handWrist) > palmLength*.60) return NaN;
  const otherWrist=lm[OTHER.wrist];
  if(pointVisible(otherWrist) && distance(otherWrist,handWrist) < distance(wrist,handWrist)) return NaN;
  // A forearm or palm pointing almost into the camera has an unstable image
  // direction. Each model's own depth is used only to reject that projection.
  const forearmDepth=(wrist.z-elbow.z)*aspect, palmDepth=(middleBase.z-handWrist.z)*aspect;
  if(!Number.isFinite(forearmDepth) || !Number.isFinite(palmDepth)
    || forearmLength < .65*Math.hypot(forearmLength,forearmDepth)
    || palmLength < .65*Math.hypot(palmLength,palmDepth)) return NaN;
  return rad2deg(Math.acos(clamp((forearm.x*palm.x+forearm.y*palm.y)/(forearmLength*palmLength),-1,1)));
}

function rawMovementMetrics(lm, handLm, freshHand=true){
  const raw={};
  if(lm){
    const ls=lm[11],rs=lm[12],lh=lm[23],rh=lm[24];
    const midShoulder=midpoint(ls,rs), midHip=midpoint(lh,rh);
    raw.trunk_projection_visible=[ls,rs,lh,rh].every(pointVisible);
    const shoulderWidth=Math.max(0.03,Math.hypot(ls.x-rs.x,ls.y-rs.y));
    const hipWidth=Math.max(0.03,Math.hypot(lh.x-rh.x,lh.y-rh.y));
    raw.trunk_angle=rad2deg(Math.atan2(midShoulder.x-midHip.x,-(midShoulder.y-midHip.y)));
    raw.shoulder_hike=rad2deg(Math.atan2(lm[OTHER.shoulder].y-lm[ACTIVE.shoulder].y,shoulderWidth));
    raw.hip_hike=rad2deg(Math.atan2(lm[OTHER.hip].y-lm[ACTIVE.hip].y,hipWidth));
    raw.shoulder_flexion=angle(lm[ACTIVE.hip],lm[ACTIVE.shoulder],lm[ACTIVE.elbow]);
    raw.shoulder_abduction=angle(lm[ACTIVE.hip],lm[ACTIVE.shoulder],lm[ACTIVE.wrist]);
    raw.other_shoulder_flexion=angle(lm[OTHER.hip],lm[OTHER.shoulder],lm[OTHER.elbow]);
    const elbowAspect=video.videoWidth>0 && video.videoHeight>0 ? video.videoWidth/video.videoHeight : 1;
    const elbowPoint=point=>({x:point.x*elbowAspect,y:point.y,z:0});
    raw.elbow_extension=angle(elbowPoint(lm[ACTIVE.shoulder]),elbowPoint(lm[ACTIVE.elbow]),elbowPoint(lm[ACTIVE.wrist]));
    raw.knee_extension=angle(lm[ACTIVE.hip],lm[ACTIVE.knee],lm[ACTIVE.ankle]);
    raw.other_knee_extension=angle(lm[OTHER.hip],lm[OTHER.knee],lm[OTHER.ankle]);
    raw.hip_extension=angle(lm[ACTIVE.shoulder],lm[ACTIVE.hip],lm[ACTIVE.knee]);
    raw.other_hip_extension=angle(lm[OTHER.shoulder],lm[OTHER.hip],lm[OTHER.knee]);
    raw.ankle_angle=angle(lm[ACTIVE.knee],lm[ACTIVE.ankle],lm[ACTIVE.toe]);
    const ankleMid=midpoint(lm[27],lm[28]);
    raw.pelvic_shift=rad2deg(Math.atan2(Math.abs(midHip.x-ankleMid.x),Math.max(.02,Math.abs(ankleMid.y-midHip.y))));
    raw.shoulder_pelvis_mismatch=Math.abs(raw.trunk_angle-rad2deg(Math.atan2(midHip.x-ankleMid.x,-(midHip.y-ankleMid.y))));
    raw.shoulder_rotation=rad2deg(Math.atan2(Math.abs((ls.z||0)-(rs.z||0)),shoulderWidth));
    raw.knee_alignment=Math.max(
      poseAlignmentDeviation(lm[23],lm[25],lm[27]),
      poseAlignmentDeviation(lm[24],lm[26],lm[28])
    );
    raw.heel_y=lm[ACTIVE.heel].y;
    raw.foot_length=Math.max(.02,Math.hypot(lm[ACTIVE.heel].x-lm[ACTIVE.toe].x,lm[ACTIVE.heel].y-lm[ACTIVE.toe].y));
    raw.knee_x=lm[ACTIVE.knee].x;
    raw.knee_y=lm[ACTIVE.knee].y;
    raw.torso_length=Math.max(.04,Math.hypot(midShoulder.x-midHip.x,midShoulder.y-midHip.y));
    raw.shoulder_width=shoulderWidth;
    raw.active_shoulder_y=lm[ACTIVE.shoulder].y;
    // Distance from the affected shoulder to its wrist (in shoulder widths),
    // including the pose model's depth so a reach straight toward the camera
    // still peaks at full extension: the frames where this peaks are where
    // the elbow is judged.
    const reachDepth=(lm[ACTIVE.wrist].z||0)-(lm[ACTIVE.shoulder].z||0);
    raw.reach_extent=Math.hypot(lm[ACTIVE.wrist].x-lm[ACTIVE.shoulder].x,lm[ACTIVE.wrist].y-lm[ACTIVE.shoulder].y,reachDepth)/shoulderWidth;
    // Shoulder line (other minus affected height: positive when the affected
    // shoulder sits higher), both-shoulder height, and the neck gap on the
    // other side - the shrug detector compares all of these with calibration.
    raw.shoulder_line_delta=lm[OTHER.shoulder].y-lm[ACTIVE.shoulder].y;
    raw.shoulders_y=midShoulder.y;
    // Leaning toward a front-facing camera foreshortens the trunk while the
    // shoulders appear wider, so this ratio drops with forward lean.
    raw.torso_shoulder_ratio=raw.torso_length/shoulderWidth;
    // Face approach: leaning toward the camera enlarges the ear-to-ear distance.
    const earL=lm[7], earR=lm[8];
    raw.ear_width=(earL&&earR) ? Math.max(.01,Math.hypot(earL.x-earR.x,earL.y-earR.y)) : NaN;
    if((CFG.exercise_id === "ex_trunk" || CFG.exercise_id === "ex_reach") && (!pointVisible(earL) || !pointVisible(earR))) raw.ear_width=NaN;
    // Head pitch: the nose sits ~9 cm in front of the ear axis, so tilting the
    // head down moves it below the ear line by ~0.6 ear-widths per radian of
    // neck flexion. Rigid-head geometry, so leaning the trunk (which moves the
    // whole head) does not change it. Used by hand-to-mouth to catch the mouth
    // coming down to the cup.
    const nose=lm[0];
    raw.head_pitch=(pointVisible(nose) && pointVisible(earL) && pointVisible(earR) && Number.isFinite(raw.ear_width) && raw.ear_width > .01)
      ? (nose.y-(earL.y+earR.y)/2)/raw.ear_width : NaN;
    // Neck gap on the affected side: a shrug shortens ear-to-shoulder distance.
    const activeEar=lm[ACTIVE.shoulder===11 ? 7 : 8];
    raw.neck_gap=activeEar ? Math.max(.01,lm[ACTIVE.shoulder].y-activeEar.y) : NaN;
    const otherEar=lm[OTHER.shoulder===11 ? 7 : 8];
    raw.other_neck_gap=otherEar ? Math.max(.01,lm[OTHER.shoulder].y-otherEar.y) : NaN;
    // Depth tilt from the pose model's relative z: shoulders nearer than hips.
    const dz=(midHip.z||0)-(midShoulder.z||0);
    raw.trunk_depth_tilt=rad2deg(Math.asin(clamp(dz/Math.max(.05,raw.torso_length),-1,1)));
    raw.active_wrist_x=lm[ACTIVE.wrist].x;
    raw.active_wrist_y=lm[ACTIVE.wrist].y;
    // "Chicken wing": the upper arm swinging out sideways (elbow farther from the
    // body's midline than the shoulder, and rising) instead of going forward.
    // Frontal-plane angle of the upper arm from straight down: 0 = hanging,
    // 90 = elbow out at shoulder height. A forward reach keeps it small.
    const elbowOut=Math.max(0,Math.abs(lm[ACTIVE.elbow].x-midShoulder.x)-Math.abs(lm[ACTIVE.shoulder].x-midShoulder.x));
    const elbowDown=Math.max(.02,lm[ACTIVE.elbow].y-lm[ACTIVE.shoulder].y);
    raw.elbow_flare=rad2deg(Math.atan2(elbowOut,elbowDown));
  }
  if(handLm){
    const fingerAngles=[
      angle(handLm[5],handLm[6],handLm[8]),
      angle(handLm[9],handLm[10],handLm[12]),
      angle(handLm[13],handLm[14],handLm[16]),
      angle(handLm[17],handLm[18],handLm[20]),
    ];
    raw.finger_extension=median(fingerAngles);
    raw.pinch_flexion=((180-angle(handLm[2],handLm[3],handLm[4]))+(180-angle(handLm[5],handLm[6],handLm[8])))/2;
    raw.hand_axis=rad2deg(Math.atan2(handLm[9].y-handLm[0].y,handLm[9].x-handLm[0].x));
    // Use elbow, wrist and hand-base points from the pose model's one 3D
    // coordinate system. Mixing a pose elbow with the hand model's 2D axis
    // produced large false angles when the palm faced the camera.
    raw.wrist_bend=poseWristBendDegrees(lm);
    if(CFG.exercise_id === "ex_grasp" && freshHand){
      raw.projected_wrist_bend=projectedWristBendDegrees(lm,handLm);
    }
  }
  return raw;
}
// Forward trunk lean from a front-facing camera. Leaning forward by an angle
// theta about the hips moves the shoulders ~L*sin(theta) toward the camera and
// the head a little farther; perspective then enlarges shoulder width and
// ear-to-ear width by D/(D-delta). Inverting that with a typical camera
// distance of ~2 torso lengths gives theta ~= asin(2*(1-w0/w)). The shoulder
// and ear estimates are compared with the calibrated upright baseline. A
// usable estimate needs agreement within either of two independent pairs:
// shoulder + face approach, or pose depth + trunk foreshortening. The weaker
// member confirms the direction; averaging it with the stronger member avoids
// letting one conservative landmark erase a real lean. The 12-degree movement
// threshold still allows ordinary small posture changes.
function restrainedForwardLeanDegrees(raw){
  const base=baselineMetrics;
  if(raw.trunk_projection_visible === false) return NaN;
  // The diagonal shoulder distance grows during a one-sided shrug. Recover
  // its horizontal span so elevation alone cannot look like camera approach.
  const span=metrics=>{
    const width=Number(metrics.shoulder_width), rise=Number(metrics.shoulder_line_delta);
    return Number.isFinite(width) && Number.isFinite(rise) && width>Math.abs(rise)
      ? Math.sqrt(width*width-rise*rise) : NaN;
  };
  const w0=span(base), w=span(raw);
  if(!Number.isFinite(w0) || !Number.isFinite(w) || w0<.03 || w<.03) return NaN;
  const shoulderApproach=rad2deg(Math.asin(clamp(2*(1-w0/w),0,1)));
  const evidence=[];
  const corroborated=(first,second)=>{
    if(!Number.isFinite(first) || !Number.isFinite(second)) return null;
    const weaker=Math.min(Math.max(0,first),Math.max(0,second));
    const stronger=Math.max(Math.max(0,first),Math.max(0,second));
    // Below four degrees the second cue is indistinguishable from normal pose
    // jitter, so one large width/depth artifact cannot establish trunk lean.
    return weaker >= 4 ? (stronger+weaker)/2 : 0;
  };
  const e0=Number(base.ear_width), e=Number(raw.ear_width);
  if(Number.isFinite(e0) && Number.isFinite(e) && e0>.01 && e>.01){
    const faceApproach=rad2deg(Math.asin(clamp(1.5*(1-e0/e),0,1)));
    const visual=corroborated(shoulderApproach,faceApproach);
    if(visual !== null) evidence.push(visual);
  }
  // Always consider pose depth and trunk shortening. Previously this pair was
  // skipped whenever ears were visible, so an underestimated ear span could
  // force a true lean to zero even when the torso cues clearly agreed.
  const z0=Number(base.trunk_depth_tilt), z=Number(raw.trunk_depth_tilt);
  const t0=Number(base.torso_length), t=Number(raw.torso_length);
  if(Number.isFinite(z0) && Number.isFinite(z) && t0>0 && t>0){
    const shortening=rad2deg(Math.acos(clamp((t/w)/(t0/w0),0,1)));
    const pose=corroborated(Math.max(0,z-z0),shortening);
    if(pose !== null) evidence.push(pose);
  }
  return evidence.length ? Math.max(...evidence) : NaN;
}
function gradedReachForwardLeanDegrees(raw){
  if(raw.trunk_projection_visible === false) return NaN;
  const base=baselineMetrics;
  const span=metrics=>{
    const width=Number(metrics.shoulder_width), rise=Number(metrics.shoulder_line_delta);
    return Number.isFinite(width) && Number.isFinite(rise) && width>Math.abs(rise)
      ? Math.sqrt(width*width-rise*rise) : NaN;
  };
  const w0=span(base), w=span(raw);
  if(!Number.isFinite(w0) || !Number.isFinite(w) || w0<.03 || w<.03) return NaN;
  const evidence=[];
  const e0=Number(base.ear_width), e=Number(raw.ear_width);
  if(Number.isFinite(e0) && Number.isFinite(e) && e0>.01 && e>.01){
    const shoulderApproach=rad2deg(Math.asin(clamp(2*(1-w0/w),0,1)));
    const faceApproach=rad2deg(Math.asin(clamp(1.5*(1-e0/e),0,1)));
    // Horizontal shoulder span excludes the extra diagonal length of a shrug.
    // Neither shoulder movement nor head movement alone establishes trunk lean.
    evidence.push(Math.min(shoulderApproach,faceApproach));
  }
  const z0=Number(base.trunk_depth_tilt), z=Number(raw.trunk_depth_tilt);
  const t0=Number(base.torso_length), t=Number(raw.torso_length);
  if(Number.isFinite(z0) && Number.isFinite(z) && t0>0 && t>0){
    const shortening=rad2deg(Math.acos(clamp((t/w)/(t0/w0),0,1)));
    // A forward pitch can shorten the projected trunk without enlarging the
    // face or shoulders. Always consider corroborated depth + shortening,
    // even when the width landmarks are present and report no approach.
    evidence.push(Math.min(Math.max(0,z-z0),shortening));
  }
  return evidence.length ? Math.max(...evidence) : NaN;
}
function forwardLeanDegrees(raw){
  if(CFG.exercise_id === "ex_trunk") return restrainedForwardLeanDegrees(raw);
  if(CFG.exercise_id === "ex_reach") return gradedReachForwardLeanDegrees(raw);
  const base=baselineMetrics;
  const estimates=[];
  const w0=Number(base.shoulder_width), w=Number(raw.shoulder_width);
  if(Number.isFinite(w0)&&Number.isFinite(w)&&w0>0&&w>0) estimates.push(rad2deg(Math.asin(clamp(2*(1-w0/w),0,1))));
  const e0=Number(base.ear_width), e=Number(raw.ear_width);
  if(Number.isFinite(e0)&&Number.isFinite(e)&&e0>0&&e>0) estimates.push(rad2deg(Math.asin(clamp(1.5*(1-e0/e),0,1))));
  if(estimates.length) return estimates.reduce((sum,value)=>sum+value,0)/estimates.length;
  // Fallbacks when the face or shoulders are not both visible: the pose
  // model's relative depth tilt, then trunk foreshortening.
  const z0=Number(base.trunk_depth_tilt), z=Number(raw.trunk_depth_tilt);
  if(Number.isFinite(z0)&&Number.isFinite(z)) return Math.max(0,z-z0);
  const baseRatio=Number(base.torso_shoulder_ratio)||raw.torso_shoulder_ratio;
  return (Number.isFinite(baseRatio)&&baseRatio>0&&Number.isFinite(raw.torso_shoulder_ratio))
    ? rad2deg(Math.acos(clamp(raw.torso_shoulder_ratio/baseRatio,0,1))) : 0;
}
// Shoulder hiking on the affected side, in degrees. A shrug elevates the
// clavicle about the sternum, so the lever is HALF the shoulder width: a rise
// of one seventh of the shoulder width (about 3 cm) reads as 8 degrees.
//  1) one-sided shrug: the affected shoulder rising above the other one,
//     relative to the calibrated shoulder line (body-relative, so leaning
//     toward the camera or sitting taller does not count);
//  2) two-shoulder shrug: both shoulders higher in the frame than at
//     calibration AND both ear-to-shoulder gaps shorter - a chin tuck shortens
//     the gaps without lifting the shoulders, and leaning toward a low camera
//     lifts the frame position without shortening the gaps, so neither counts.
// Head dropping toward the hand (neck flexion) in degrees, relative to the
// calibrated upright head: nose-below-ear-line offset / 0.6 ear widths, and
// only the downward direction counts (looking up while drinking is fine).
function headDropDegrees(raw){
  const base=baselineMetrics;
  const p0=Number(base.head_pitch), p=Number(raw.head_pitch);
  if(!Number.isFinite(p0) || !Number.isFinite(p)) return NaN;
  return rad2deg(Math.asin(clamp(Math.max(0,p-p0)/0.6,0,1)));
}
function shoulderHikeDegrees(raw){
  const base=baselineMetrics;
  const halfWidth=Math.max(.03,(Number(raw.shoulder_width)||.18)/2);
  const line0=Number(base.shoulder_line_delta);
  const asymmetry=(Number.isFinite(line0)&&Number.isFinite(raw.shoulder_line_delta))
    ? rad2deg(Math.atan2(Math.max(0,raw.shoulder_line_delta-line0),halfWidth)) : 0;
  const y0=Number(base.shoulders_y);
  const frameRise=(Number.isFinite(y0)&&Number.isFinite(raw.shoulders_y)) ? Math.max(0,y0-raw.shoulders_y) : 0;
  const g0=Number(base.neck_gap), g=Number(raw.neck_gap);
  const o0=Number(base.other_neck_gap), o=Number(raw.other_neck_gap);
  const gapShrink=(Number.isFinite(g0)&&Number.isFinite(g)&&Number.isFinite(o0)&&Number.isFinite(o))
    ? Math.max(0,Math.min(g0-g,o0-o)) : 0;
  const bilateral=rad2deg(Math.atan2(Math.min(frameRise,gapShrink),halfWidth));
  let unilateral=asymmetry;
  if(CFG.exercise_id === "ex_trunk" && [g0,g,o0,o].every(Number.isFinite)){
    // In restrained reaching, a unilateral shrug must also shorten the
    // affected neck gap more than the opposite one. Shoulder-line motion on
    // its own can come from forward-lean perspective or landmark jitter.
    const affectedGapShrink=Math.max(0,g0-g);
    const oppositeGapShrink=Math.max(0,o0-o);
    const neckSupport=rad2deg(Math.atan2(Math.max(0,affectedGapShrink-oppositeGapShrink),halfWidth));
    unilateral=Math.min(asymmetry,neckSupport);
  }
  return Math.max(unilateral,bilateral);
}
function metricValue(metric,raw){
  const base=baselineMetrics;
  if(metric === "shoulder_flexion" || metric === "shoulder_abduction" || metric === "elbow_extension" || metric === "knee_extension" || metric === "hip_extension" || metric === "finger_extension" || metric === "pinch_flexion") return raw[metric];
  if(metric === "elbow_flexion") return 180-raw.elbow_extension;
  if(metric === "bilateral_shoulder_flexion") return Math.min(raw.shoulder_flexion,raw.other_shoulder_flexion);
  if(metric === "bilateral_knee_extension") return Math.min(raw.knee_extension,raw.other_knee_extension);
  if(metric === "hip_flexion") return Math.abs(raw.hip_extension-(base.hip_extension||raw.hip_extension));
  if(metric === "knee_flexion") return Math.abs(raw.knee_extension-(base.knee_extension||raw.knee_extension));
  if(metric === "ankle_dorsiflexion") return Math.abs(raw.ankle_angle-(base.ankle_angle||raw.ankle_angle));
  if(metric === "pelvic_shift") return Math.abs(raw.pelvic_shift-(base.pelvic_shift||raw.pelvic_shift));
  if(metric === "trunk_lateral_rom") return Math.abs(raw.trunk_angle-(base.trunk_angle||0));
  if(metric === "trunk_side_lean_delta") return Math.abs(raw.trunk_angle-(base.trunk_angle||0));
  // Where an exercise names side lean as its own pattern, trunk lean means the
  // forward lean only; otherwise it is whichever is larger.
  if(metric === "trunk_lean_delta") return HAS_SIDE_LEAN_RULE
    ? forwardLeanDegrees(raw)
    : Math.max(Math.abs(raw.trunk_angle-(base.trunk_angle||0)),forwardLeanDegrees(raw));
  if(metric === "shoulder_hike_delta") return shoulderHikeDegrees(raw);
  if(metric === "head_drop_deg") return headDropDegrees(raw);
  if(metric === "hip_hike_delta") return Math.abs(raw.hip_hike-(base.hip_hike||0));
  if(metric === "arm_asymmetry") return Math.abs(raw.shoulder_flexion-raw.other_shoulder_flexion);
  if(metric === "body_asymmetry") return (Math.abs(raw.hip_extension-raw.other_hip_extension)+Math.abs(raw.knee_extension-raw.other_knee_extension))/2;
  if(metric === "shoulder_pelvis_mismatch" || metric === "knee_alignment" || metric === "shoulder_rotation") return raw[metric];
  if(metric === "heel_lift_angle") return rad2deg(Math.atan2(Math.abs(raw.heel_y-(base.heel_y||raw.heel_y)),raw.foot_length||.02));
  if(metric === "knee_motion_delta") return rad2deg(Math.atan2(Math.hypot(raw.knee_x-(base.knee_x||raw.knee_x),raw.knee_y-(base.knee_y||raw.knee_y)),raw.torso_length||.04));
  if(metric === "elbow_flare_deg") return raw.elbow_flare;
  if(metric === "wrist_flexion_delta"){
    // Arm exercises judge the wrist against the forearm (a relaxed wrist reads
    // ~15 degrees when no calibrated value exists) and only in frames where
    // that is measurable - never the bare hand axis, which turns with the arm.
    if(STANDARD.tracking_mode !== "hand"){
      // The palm/forearm projection measures alignment directly. Subtracting
      // a bent lap baseline (or the old default 15 degrees) hides real bends.
      if(CFG.exercise_id === "ex_grasp" && Number.isFinite(raw.projected_wrist_bend)) return raw.projected_wrist_bend;
      if(!Number.isFinite(raw.wrist_bend)) return NaN;
      const rest=Number.isFinite(Number(base.wrist_bend)) ? Number(base.wrist_bend) : 15;
      return Math.max(0,raw.wrist_bend-rest);
    }
    // Hand-only exercises (forearm supported, pointing at the camera): hand-axis
    // turn from calibration, wrapped so 350 degrees reads as 10.
    if(!Number.isFinite(raw.hand_axis) || !Number.isFinite(Number(base.hand_axis))) return NaN;
    const turn=Math.abs(raw.hand_axis-Number(base.hand_axis)) % 360;
    return Math.min(turn,360-turn);
  }
  return Number(raw[metric]);
}
function movementAnchor(lm,handLm){
  const points=STANDARD.tracking_mode === "hand"
    ? [handLm&&handLm[0],handLm&&handLm[4],handLm&&handLm[8],handLm&&handLm[12],handLm&&handLm[20]]
    : [lm&&lm[11],lm&&lm[12],lm&&lm[23],lm&&lm[24],lm&&lm[ACTIVE.wrist],lm&&lm[ACTIVE.knee],lm&&lm[ACTIVE.ankle]];
  return points.filter(Boolean).flatMap(point=>[point.x,point.y]);
}
function anchorDistance(a,b){
  if(!a || !b || a.length !== b.length) return Infinity;
  let sum=0;
  for(let i=0;i<a.length;i++) sum+=(a[i]-b[i])*(a[i]-b[i]);
  return Math.sqrt(sum/Math.max(1,a.length));
}
function updateCalibration(lm,handLm){
  // Locate the lap with the same affected-wrist/affected-hip gate used by the
  // initial assessment. Baseline scoring can keep collecting the wider pose
  // independently, so an unseen knee or opposite arm cannot move the lap.
  updateExerciseLapTargetCalibration(lm,performance.now());
  const quality=trackingQuality(lm,handLm);
  if(quality < CALIBRATION_MIN_TRACKING_QUALITY){
    calibrationSamples=[];
    calibrationAnchors=[];
    calibrationReady=false;
    calibrationFill.style.width="0%";
    if(HAS_DYNAMIC_LAP_TARGET){
      const lapStatus=exerciseLapTargetCandidateStatus(lm);
      exerciseLapCalibrationDiagnostic={reason:lapStatus.reason,guidance:lapStatus.guidance};
    }
    calibrationStatus.textContent=HAS_DYNAMIC_LAP_TARGET && !exerciseLapTargetCalibration.ready
      ? exerciseLapCalibrationDiagnostic.guidance
      : STANDARD.tracking_mode === "hand"
        ? "Keep your whole hand and fingertips inside the camera view."
        : "Move back until the joints named above are visible.";
    return;
  }
  latestExercisePoseLandmarks=lm || latestExercisePoseLandmarks;
  const anchor=movementAnchor(lm,handLm);
  if(calibrationAnchors.length && anchorDistance(anchor,calibrationAnchors[0]) > CALIBRATION_MAX_ANCHOR_DRIFT){
    calibrationSamples=[];
    calibrationAnchors=[];
    if(HAS_DYNAMIC_LAP_TARGET && !exerciseLapTargetCalibration.ready){
      exerciseLapTargetCalibration=newExerciseLapTargetCalibration();
      exerciseLapTarget=null;
      exerciseLapTargetRadius=null;
    }
    calibrationStatus.textContent="Almost there. Hold your starting position still.";
  }
  calibrationAnchors.push(anchor);
  calibrationSamples.push(rawMovementMetrics(lm,handLm));
  if(handLm && handLm.length >= 21){
    restHandOpenSamples.push(handOpenScore);
    if(restHandOpenSamples.length > 60) restHandOpenSamples.shift();
  }
  if(calibrationSamples.length > 54){
    calibrationSamples.shift();
    calibrationAnchors.shift();
  }
  const baselineProgress=Math.min(100,Math.round(calibrationSamples.length/CALIBRATION_MIN_SAMPLES*100));
  const lapProgress=!HAS_DYNAMIC_LAP_TARGET || exerciseLapTargetCalibration.ready
    ? 100
    : Math.min(99,Math.round(exerciseLapTargetCalibration.samples.length/LAP_CALIBRATION_MIN_SAMPLES*100));
  const progress=Math.min(baselineProgress,lapProgress);
  calibrationFill.style.width=progress+"%";
  calibrationReady=calibrationSamples.length >= CALIBRATION_MIN_SAMPLES && (!HAS_DYNAMIC_LAP_TARGET || exerciseLapTargetCalibration.ready);
  calibrationStatus.textContent=!calibrationReady && HAS_DYNAMIC_LAP_TARGET && !exerciseLapTargetCalibration.ready
    ? exerciseLapCalibrationDiagnostic.guidance
    : progress < 100
      ? "Position found. Keep still for "+Math.max(1,Math.ceil((CALIBRATION_MIN_SAMPLES-calibrationSamples.length)/15))+" more seconds."
      : "Calibration complete.";
  if(calibrationReady && calibrationInstructionFinished) void completeCalibration();
}
async function completeCalibration(){
  if(runnerExited || calibrationFinishing || !calibrationReady) return;
  calibrationFinishing=true;
  const keys=Object.keys(calibrationSamples[0]||{});
  baselineMetrics=Object.fromEntries(keys.map(key=>[key,median(calibrationSamples.map(sample=>Number(sample[key])))]));
  restHandOpenScore=restHandOpenSamples.length >= 10 ? median(restHandOpenSamples) : null;
  if(HAS_DYNAMIC_LAP_TARGET){
    exerciseLapTarget=exerciseLapTargetCalibration.target ? {...exerciseLapTargetCalibration.target} : null;
    exerciseLapTargetRadius=Number.isFinite(exerciseLapTargetRadius) && exerciseLapTargetRadius > 0
      ? exerciseLapTargetRadius
      : Math.min(Math.max(.10,exerciseShoulderWidth(latestExercisePoseLandmarks)*.55),.18);
  }
  calibrating=false;
  calibrationEl.classList.add("hidden");
  postRN({
    type:"exercise_calibrated",
    exercise_id:CFG.name,
    tracking_mode:STANDARD.tracking_mode,
    posture:STANDARD.posture,
    affected_side:AFFECTED_SIDE,
    rehab_session_id:REHAB_SESSION_ID,
    lap_target:exerciseLapTarget,
    lap_target_radius:exerciseLapTargetRadius,
  });
  if(!setupVoicePlayed){
    await playVoice(CFG.setup_voice);
    setupVoicePlayed=true;
  }
  await startRep();
}
let activeFrames=0;          // frames inside the movement phase (the only ones scored)
let peakReachExtent=-1;      // farthest shoulder-to-wrist distance seen this rep
let peakReachElbow=NaN;      // elbow angle at that peak-reach frame
let reachFrames=[];          // the top-of-reach raised-arm frames of this rep ({key, elbow})
const NEAR_PEAK_REACH_RATIO=0.9, MAX_REACH_FRAMES=8;
// The elbow is judged at the top of the reach: the frames where the exercise's
// main movement metric (shoulder flexion for a forward reach) is within 10% of
// its peak for the repetition. Frames on the way up - where a still-hanging arm
// is naturally straight - never count, and the best elbow angle among the
// top-of-reach frames is used so one noisy frame cannot decide it.
function reachKeyMetric(){
  const step=(STANDARD.rom_steps||[]).find(item=>item.metric!=="elbow_extension");
  return step ? step.metric : null;
}
function elbowAtPeakReach(){
  if(!reachFrames.length) return NaN;
  const best=reachFrames[0].key;
  return Math.max(...reachFrames.filter(item=>item.key>=best*NEAR_PEAK_REACH_RATIO).map(item=>item.elbow));
}
function activeMovementPhase(){
  const sub=CFG.cycle[currentSubStep];
  if(!sub) return false;
  if(fbEl.classList.contains("show")) return false;
  return (sub.phase||"movement") !== "return";
}
// The movement is underway once the exercise's main joint has moved a quarter
// of the way from its resting angle toward today's target (for a reach: the
// shoulder has flexed ~12 degrees beyond rest). Tap-confirmed exercises have
// no target phases, so every frame counts there.
function movementUnderway(raw){
  if(CFG.pose_mode !== "body") return true;
  if(CFG.exercise_id === "ex_reach" || CFG.exercise_id === "ex_trunk"){
    if(!exerciseTargetIsArmed()) return false;
    const trunkRule=(STANDARD.compensations||[]).find(rule=>rule.metric === "trunk_lean_delta");
    const lean=metricValue("trunk_lean_delta",raw);
    // Trunk substitution itself is movement, even if the arm joint barely
    // changes. Include the approach to the cutoff in the eligible frames;
    // the existing 12-degree / frame-count rules still confirm the finding.
    if(trunkRule && Number.isFinite(lean) && lean>=Number(trunkRule.threshold_deg)*.5) return true;
  }
  // The affected hand has clearly left its calibrated resting place.
  const base=baselineMetrics;
  if(Number.isFinite(Number(base.active_wrist_x)) && Number.isFinite(raw.active_wrist_x)){
    const moved=Math.hypot(raw.active_wrist_x-Number(base.active_wrist_x),raw.active_wrist_y-Number(base.active_wrist_y));
    if(moved >= 0.5*(Number(raw.shoulder_width)||.18)) return true;
  }
  const key=(STANDARD.rom_steps||[]).find(step=>step.metric!=="elbow_extension");
  if(!key) return true;
  const value=metricValue(key.metric,raw);
  const rest=metricValue(key.metric,base);
  const target=Number(key.target_deg||0);
  if(!Number.isFinite(value) || !Number.isFinite(rest) || !(target > 0)) return true;
  const span=Math.max(5,target-rest);
  return value-rest >= 0.25*span;
}
// A scored step or compensation rule may name the cycle steps it applies to
// (e.g. the elbow is judged while reaching to the cup, wrist flexion while
// gripping and carrying); without "steps" it applies to every movement step.
function ruleAppliesNow(rule){
  return !Array.isArray(rule.steps) || rule.steps.includes(currentSubStep);
}
// Raising the arm lifts the shoulder a little on its own (the collarbone
// elevates and the shoulder blade rotates upward with the arm - normal
// scapulohumeral rhythm - and the pose model's shoulder point rides up with
// the deltoid), so a shoulder that rises WITH the reach is not hiking. The
// shrug threshold therefore grows with the affected shoulder's flexion beyond
// its resting angle; a shrug before the arm is up meets the plain threshold.
const SHOULDER_HIKE_ALLOWANCE_PER_FLEXION_DEG=Number(SCORING_METHOD.shoulder_hike_allowance_per_flexion_deg);
const SHOULDER_HIKE_FREE_FLEXION_DEG=Number(SCORING_METHOD.shoulder_hike_allowance_free_flexion_deg);
function expectedShoulderRise(raw,rule=null,base=baselineMetrics){
  const ruleAllowance=Number(rule && rule.normal_rise_allowance_per_elevation_deg);
  const allowance=Number.isFinite(ruleAllowance) ? ruleAllowance : SHOULDER_HIKE_ALLOWANCE_PER_FLEXION_DEG;
  if(CFG.pose_mode !== "body" || !Number.isFinite(allowance)) return 0;
  const currentValues=[Number(raw && raw.shoulder_flexion),Number(raw && raw.shoulder_abduction)].filter(Number.isFinite);
  const restValues=[Number(base && base.shoulder_flexion),Number(base && base.shoulder_abduction)].filter(Number.isFinite);
  if(!currentValues.length || !restValues.length) return 0;
  const elevation=Math.max(...currentValues), rest=Math.max(...restValues);
  const free=Number.isFinite(SHOULDER_HIKE_FREE_FLEXION_DEG) ? SHOULDER_HIKE_FREE_FLEXION_DEG : 10;
  const expected=allowance*Math.max(0,elevation-rest-free);
  const cap=Number(rule && rule.normal_rise_allowance_cap_deg);
  return Number.isFinite(cap) ? Math.min(cap,expected) : expected;
}
function compensationThreshold(rule,raw){
  const threshold=Number(rule.threshold_deg||0);
  return rule.metric === "shoulder_hike_delta" ? threshold+expectedShoulderRise(raw,rule) : threshold;
}
function compensationExceeded(rule,raw,value){
  const threshold=compensationThreshold(rule,raw);
  return rule.normal_max_inclusive === true ? value > threshold : value >= threshold;
}
function resetRepMetrics(){
  clearTemporaryCompensationEvidence();
  romBest={};
  compensationHits={};
  compensationEligible={};
  compensationConsecutive={};
  compensationLongestStreak={};
  liveCompensationStreaks={};
  liveCompensationIds=new Set();
  trackingFrames=0;
  lowQualityFrames=0;
  activeFrames=0;
  peakReachExtent=-1;
  peakReachElbow=NaN;
  reachFrames=[];
  peakCompensationDegrees={};
}
let peakCompensationDegrees={};
let lastRawMetrics=null;     // most recent frame's raw metrics, for the live degree readout
function updateMetrics(lm,handLm,freshHand=true){
  const quality=trackingQuality(lm,handLm);
  if(quality < CALIBRATION_MIN_TRACKING_QUALITY){ lowQualityFrames+=1; compensationConsecutive={}; return; }
  const raw=rawMovementMetrics(lm,handLm,freshHand);
  latestExercisePoseLandmarks=lm || latestExercisePoseLandmarks;
  lastRawMetrics=raw;
  trackingFrames+=1;
  // Return-to-rest frames are never scored: they cannot earn ROM credit and
  // they do not count toward the compensation denominator.
  if(!activeMovementPhase()){ compensationConsecutive={}; return; }
  activeFrames+=1;
  if(Number.isFinite(raw.reach_extent) && raw.reach_extent > peakReachExtent){
    peakReachExtent=raw.reach_extent;
    peakReachElbow=raw.elbow_extension;
  }
  // Elbow extension is judged at peak reach, and only once the arm is actually
  // raised - a straight arm resting on the lap earns nothing.
  const armRaised=!Number.isFinite(raw.shoulder_flexion)
    || (raw.shoulder_flexion-(Number(baselineMetrics.shoulder_flexion)||0)) >= 15;
  const reachKey=reachKeyMetric();
  const reachValue=reachKey ? metricValue(reachKey,raw) : raw.reach_extent;
  const elbowStepNow=(STANDARD.rom_steps||[]).find(step=>step.metric==="elbow_extension");
  if(armRaised && (!elbowStepNow || ruleAppliesNow(elbowStepNow)) && Number.isFinite(reachValue) && Number.isFinite(raw.elbow_extension)){
    reachFrames.push({key:reachValue,elbow:raw.elbow_extension});
    reachFrames.sort((a,b)=>b.key-a.key);
    if(reachFrames.length > MAX_REACH_FRAMES) reachFrames.length=MAX_REACH_FRAMES;
  }
  for(const step of STANDARD.rom_steps||[]){
    if(!ruleAppliesNow(step)) continue;
    if(step.metric === "elbow_extension"){
      const elbow=elbowAtPeakReach();
      if(Number.isFinite(elbow)) romBest[step.id]=elbow;
      continue;
    }
    const value=metricValue(step.metric,raw);
    if(Number.isFinite(value)) romBest[step.id]=Math.max(Number(romBest[step.id]||0),value);
  }
  const evidenceValues={};
  // Compensations are judged against the frames in which the movement is
  // actually underway (the main joint has left its resting angle), not the
  // frames spent listening to the instruction or settling: a lean or shrug
  // during a two-second reach must not be diluted by four seconds of sitting
  // still beforehand.
  const underway=movementUnderway(raw);
  for(const rule of STANDARD.compensations||[]){
    if(!ruleAppliesNow(rule) || !underway){ compensationConsecutive[rule.id]=0; continue; }
    // Reusing one hand inference for several display frames must not turn
    // a brief wrist outlier into a confirmed compensation.
    if(CFG.exercise_id === "ex_grasp" && rule.id === "wrist_flexion" && !freshHand) continue;
    const value=metricValue(rule.metric,raw);
    if(!Number.isFinite(value)){ compensationConsecutive[rule.id]=0; continue; }
    compensationEligible[rule.id]=(compensationEligible[rule.id]||0)+1;
    peakCompensationDegrees[rule.id]=Math.max(Number(peakCompensationDegrees[rule.id]||0),value);
    const aboveThreshold=compensationExceeded(rule,raw,value);
    compensationConsecutive[rule.id]=aboveThreshold ? Number(compensationConsecutive[rule.id]||0)+1 : 0;
    compensationLongestStreak[rule.id]=Math.max(Number(compensationLongestStreak[rule.id]||0),compensationConsecutive[rule.id]);
    if(aboveThreshold) compensationHits[rule.id]=(compensationHits[rule.id]||0)+1;
    const previousStreak=Number(liveCompensationStreaks[rule.id]||0);
    liveCompensationStreaks[rule.id]=aboveThreshold ? previousStreak+1 : Math.max(0,previousStreak-2);
    // Three consecutive eligible frames are enough to show a calm live cue;
    // final feedback still uses the stricter min-frame and ratio confirmation.
    if(EVIDENCE_COMPENSATION_IDS.has(rule.id) && liveCompensationStreaks[rule.id] >= 3){
      liveCompensationIds.add(rule.id);
      evidenceValues[rule.id]=value;
    }else if(liveCompensationStreaks[rule.id] === 0){
      liveCompensationIds.delete(rule.id);
    }
  }
  if(TEMPORARY_COMPENSATION_EVIDENCE && liveCompensationIds.size){
    const ids=[...liveCompensationIds];
    const values={};
    let severity=0;
    ids.forEach(id=>{
      const rule=(STANDARD.compensations||[]).find(item=>item.id===id);
      const value=Number(evidenceValues[id]||peakCompensationDegrees[id]||0);
      values[id]=value;
      severity=Math.max(severity,value/Math.max(1,rule ? compensationThreshold(rule,raw) : 1));
    });
    captureTemporaryCompensationEvidence(lm,ids,severity,values);
  }
}

function exerciseShoulderWidth(lm){
  if(!lm) return 0.18;
  const ls=lm[11], rs=lm[12];
  if(!ls || !rs) return 0.18;
  return Math.max(0.08,Math.min(0.40,Math.hypot(ls.x-rs.x,ls.y-rs.y)));
}

function effectiveExerciseTargetRadius(sub,lm){
  const baseR=Number(sub && sub.target && sub.target.r)||0.10;
  if(isExerciseLapTarget(sub)){
    if(Number.isFinite(exerciseLapTargetRadius) && exerciseLapTargetRadius > 0) return exerciseLapTargetRadius;
    const radius=Math.min(Math.max(.10,exerciseShoulderWidth(lm)*.55),.18);
    if(exerciseLapTargetCalibration.ready) exerciseLapTargetRadius=radius;
    return radius;
  }
  // The bilateral outward target intentionally represents a wide movement zone.
  if(baseR > 0.18) return baseR;
  return Math.min(Math.max(baseR,exerciseShoulderWidth(lm)*0.55),0.18);
}

let latestHandLandmarks=null;   // the affected hand in the latest frame (when hand tracking is on)
let latestHandSeenAt=0;         // when it was last detected (a brief gap keeps the previous hand)
let handGateNearSince=null;     // when the wrist first arrived at the cup for a hand-opening / grasp step
const HAND_LANDMARK_FRESH_MS=240;
// Match the assessment: scan the hand on every available frame while the
// device is keeping up. If pose + hands fall below 15 fps, cap hand inference
// near 14 fps instead of reusing a visibly old result for several seconds.
const HAND_SCAN_INTERVAL_MS=0, HAND_BACKOFF_SCAN_INTERVAL_MS=70, HAND_BACKOFF_FRESH_MS=240, MIN_SMOOTH_FPS=15;
let frameIntervalMs=33, lastLoopTs=0, lastHandScanTs=0;
function handBackoffActive(){ return frameIntervalMs > 1000/MIN_SMOOTH_FPS; }
function handFreshWindowMs(){ return handBackoffActive() ? HAND_BACKOFF_FRESH_MS : HAND_LANDMARK_FRESH_MS; }
const TARGET_ARM_DELAY_AFTER_VOICE_MS=Number(CFG.target_arm_delay_ms ?? 700);
const TARGET_HOLD_LOSS_GRACE_MS=350;
function targetActivationReady(finishedAt,now,voiceIsActive=false){
  return !voiceIsActive && finishedAt > 0 && (now-finishedAt) >= TARGET_ARM_DELAY_AFTER_VOICE_MS;
}
function exerciseTargetIsArmed(now=performance.now()){
  return targetActivationReady(stepVoiceFinishedAt,now,activeVoiceSequence!==0);
}
// Hand-opening and fist-closure scores, computed and smoothed exactly like the
// initial assessment (finger straightness, fingertip spread, thumb-index
// spread; fingertip-to-palm distance), and decayed when the hand is lost.
const HAND_OPEN_SCORE=0.45, HAND_CLOSED_SCORE=0.30;
let handOpenScore=0, fistClosureScore=0, handOpenDegreesSmoothed=NaN;
// Closing must visibly change from the preceding open hand. Opening accepts
// either a clear absolute open-hand posture or a change from the resting/closed
// reference, so an already-open hand is not trapped after the voice finishes.
let restHandOpenScore=null, restHandOpenSamples=[];
let handOpenRef=null, handClosedRef=null;
const HAND_CHANGE_MARGIN=0.25, HAND_OPEN_REST_MARGIN=0.10, HAND_CLOSE_DEGREES_DROP=30;
function noteHandStepCompleted(step){
  const which=step && step.target && step.target.landmark;
  if(which === "HAND_OPEN") handOpenRef={open:handOpenScore, closure:fistClosureScore, degrees:handOpenDegreesSmoothed};
  if(which === "HAND_CLOSED") handClosedRef={open:handOpenScore, closure:fistClosureScore, degrees:handOpenDegreesSmoothed};
}
function updateRehabHandScores(h){
  if(!h || h.length < 21){
    handOpenScore=Math.max(0,handOpenScore-0.15);
    fistClosureScore=Math.max(0,fistClosureScore-0.15);
    return;
  }
  const clamp01=v=>Math.max(0,Math.min(1,v));
  const dist=(a,b)=>Math.hypot(a.x-b.x,a.y-b.y);
  const palmWidth=Math.max(.01,dist(h[5],h[17]));
  const palmHeight=Math.max(.01,dist(h[0],h[9]));
  const handScale=Math.max(.01,palmWidth,palmHeight*0.9);
  const fingerDefs=[[5,6,7,8],[9,10,11,12],[13,14,15,16],[17,18,19,20]];
  const fingerSpread=(dist(h[0],h[8])+dist(h[0],h[12])+dist(h[0],h[16])+dist(h[0],h[20]))/4;
  const straightness=fingerDefs.reduce((sum,[mcp,pip,dip,tip])=>{
    const pipStraight=clamp01((angle(h[mcp],h[pip],h[dip])-110)/60);
    const dipStraight=clamp01((angle(h[pip],h[dip],h[tip])-120)/55);
    const reach=clamp01((dist(h[mcp],h[tip])/handScale-0.55)/0.65);
    return sum+(pipStraight*0.45+dipStraight*0.35+reach*0.20);
  },0)/fingerDefs.length;
  const fingertipDistanceScore=clamp01((fingerSpread/handScale-1.15)/1.15);
  const thumbIndexSpreadScore=clamp01((dist(h[4],h[8])/handScale-0.35)/0.95);
  const open=clamp01(straightness*0.62+fingertipDistanceScore*0.28+thumbIndexSpreadScore*0.10);
  handOpenScore=handOpenScore*0.35+open*0.65;
  const palmCenter={x:(h[0].x+h[5].x+h[9].x+h[13].x+h[17].x)/5,y:(h[0].y+h[5].y+h[9].y+h[13].y+h[17].y)/5};
  const tipPalmDist=(dist(h[8],palmCenter)+dist(h[12],palmCenter)+dist(h[16],palmCenter)+dist(h[20],palmCenter))/4;
  const closure=clamp01((2.15-tipPalmDist/palmWidth)/0.85);
  fistClosureScore=fistClosureScore*0.35+closure*0.65;
  const degrees=handOpeningDegrees(h);
  if(Number.isFinite(degrees)) handOpenDegreesSmoothed=Number.isFinite(handOpenDegreesSmoothed) ? handOpenDegreesSmoothed*0.6+degrees*0.4 : degrees;
}
function handIsFresh(now){
  return !!latestHandLandmarks && (now-latestHandSeenAt) <= handFreshWindowMs();
}
function handPalmCenter(handLm){
  if(!handLm || handLm.length < 21) return null;
  const points=[handLm[0],handLm[5],handLm[9],handLm[13],handLm[17]].filter(Boolean);
  if(points.length < 4) return null;
  return {
    x:points.reduce((sum,point)=>sum+point.x,0)/points.length,
    y:points.reduce((sum,point)=>sum+point.y,0)/points.length,
  };
}
function openHandDetected(reference,margin,openScore=handOpenScore,degrees=handOpenDegreesSmoothed){
  const clearlyOpen=openScore >= 0.62 && (!Number.isFinite(degrees) || degrees >= 130);
  return clearlyOpen || (openScore > HAND_OPEN_SCORE && (reference == null || openScore > reference + margin));
}
function checkTarget(lm){
  const sub = CFG.cycle[currentSubStep];
  if(!sub || !sub.target || !lm) return false;
  const t = effectiveExerciseTarget(sub);
  const R = effectiveExerciseTargetRadius(sub,lm);
  const Lw=lm[15], Rw=lm[16];
  if(isExerciseLapTarget(sub)){
    const affectedWrist=lm[ACTIVE.wrist];
    return exerciseLandmarkIsUsable(affectedWrist,.35)
      && Math.hypot(affectedWrist.x-t.x,affectedWrist.y-t.y) < R;
  }
  // The circle is drawn at the target's image coordinates on the mirrored
  // canvas, exactly like the skeleton, so the wrist is compared in the same
  // coordinates - the hand has to be on the circle the patient sees.
  const ok = (p) => p && Math.hypot(p.x-t.x, p.y-t.y) < R;
  const which = sub.target.landmark;
  if(HAND_GATE_LANDMARKS.has(which)){
    // Hand-opening / grasp steps: the affected palm or wrist must be at the cup
    // AND the fingers must open (or close) far enough. The palm centre matches
    // how a patient naturally places an open hand around the displayed cup.
    // If the fingers cannot get there,
    // the step still completes after max_wait_ms so nobody is stuck; the
    // shortfall is then named in the feedback.
    const now=performance.now();
    const poseWrist=lm[ACTIVE.wrist] && pointVisible(lm[ACTIVE.wrist]) ? lm[ACTIVE.wrist] : null;
    const trackedWrist=handIsFresh(now) && latestHandLandmarks ? latestHandLandmarks[0] : null;
    const palm=handIsFresh(now) ? handPalmCenter(latestHandLandmarks) : null;
    if(!ok(palm) && !ok(poseWrist) && !ok(trackedWrist)){ handGateNearSince=null; return false; }
    if(handGateNearSince == null) handGateNearSince=now;
    const waitedLongEnough = now-handGateNearSince >= Number(sub.max_wait_ms||6000);
    if(!handIsFresh(now)) return waitedLongEnough;
    if(which === "HAND_OPEN"){
      // Opening: clearly open (assessment threshold) and wider than the hand at
      // rest, or - after a grasp - wider than the closed grasp was.
      const reference = handClosedRef ? handClosedRef.open : restHandOpenScore;
      const margin = handClosedRef ? HAND_CHANGE_MARGIN : HAND_OPEN_REST_MARGIN;
      const opened = openHandDetected(reference,margin);
      return opened || waitedLongEnough;
    }
    // Closing around the cup: the fingers must actually curl from the open
    // hand - lower open score, higher closure, and the finger angle down.
    const ref = handOpenRef || {open:Math.max(HAND_OPEN_SCORE, handOpenScore), closure:fistClosureScore, degrees:handOpenDegreesSmoothed};
    const closed = fistClosureScore > HAND_CLOSED_SCORE
      && handOpenScore < ref.open - HAND_CHANGE_MARGIN
      && (!Number.isFinite(ref.degrees) || !Number.isFinite(handOpenDegreesSmoothed) || handOpenDegreesSmoothed <= ref.degrees - HAND_CLOSE_DEGREES_DROP);
    return closed || waitedLongEnough;
  }
  const bilateral=(STANDARD.rom_steps||[]).some(step=>String(step.metric||"").startsWith("bilateral_"));
  if(bilateral) return ok(Lw) && ok(Rw);
  return ok(lm[ACTIVE.wrist]);
}

// ==== Virtual exercise objects (drawn on screen instead of real props) ====
// Object-based exercises no longer need a physical cup, peg, or towel: the
// object is rendered on the canvas. In "carry" mode the object waits at the
// grab target, attaches to the affected wrist once grasped, and is set down
// at the place target; "held" keeps it in the hand for the whole repetition;
// "hand_anchor" centres it on the tracked hand; "pick_place" draws a peg and
// container that fills as repetitions complete; "between_hands" draws a bar
// spanning both wrists.
const VOBJ = CFG.virtual_object || null;
let vobjState = "resting";      // resting -> carried -> placed (carry mode)
let vobjPlacePos = null;
let vobjPlacedCount = 0;        // pick_place: pegs shown inside the container

function vobjSize(){ return 0.11 * Math.min(canvas.width, canvas.height); }

function drawVirtualCup(x, y, s){
  const w = s * 0.72;
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(x - w/2, y - s/2); ctx.lineTo(x + w/2, y - s/2);
  ctx.lineTo(x + w*0.38, y + s/2); ctx.lineTo(x - w*0.38, y + s/2);
  ctx.closePath();
  ctx.fillStyle = "#E8C08A"; ctx.fill();
  ctx.lineWidth = 3; ctx.strokeStyle = "#8A6B3F"; ctx.stroke();
  ctx.beginPath(); ctx.ellipse(x, y - s/2, w/2, s*0.10, 0, 0, Math.PI*2);
  ctx.fillStyle = "#F3DAB4"; ctx.fill(); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x - w*0.26, y - s*0.28); ctx.lineTo(x - w*0.18, y + s*0.30);
  ctx.lineWidth = 4; ctx.strokeStyle = "rgba(255,255,255,0.55)"; ctx.stroke();
  ctx.restore();
}

function drawVirtualBall(x, y, s){
  ctx.save();
  const r = s * 0.55;
  const g = ctx.createRadialGradient(x - r*0.35, y - r*0.35, r*0.15, x, y, r);
  g.addColorStop(0, "#FBE7C4"); g.addColorStop(1, "#DBA75E");
  ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI*2);
  ctx.fillStyle = g; ctx.fill();
  ctx.lineWidth = 3; ctx.strokeStyle = "#8A6B3F"; ctx.stroke();
  ctx.restore();
}

function drawVirtualPeg(x, y, s){
  ctx.save();
  const w = s * 0.22, h = s * 0.6;
  ctx.beginPath();
  if(ctx.roundRect) ctx.roundRect(x - w/2, y - h/2, w, h, w*0.4); else ctx.rect(x - w/2, y - h/2, w, h);
  ctx.fillStyle = "#D98B5F"; ctx.fill();
  ctx.lineWidth = 2.5; ctx.strokeStyle = "#8A4F2D"; ctx.stroke();
  ctx.restore();
}

function drawVirtualContainer(x, y, s, filled){
  ctx.save();
  const w = s * 1.05, h = s * 0.62;
  ctx.beginPath();
  ctx.moveTo(x - w/2, y - h/2); ctx.lineTo(x - w/2, y + h/2);
  ctx.lineTo(x + w/2, y + h/2); ctx.lineTo(x + w/2, y - h/2);
  ctx.lineWidth = 4; ctx.strokeStyle = "#5E6861"; ctx.stroke();
  ctx.fillStyle = "rgba(94,104,97,0.14)"; ctx.fill();
  for(let i = 0; i < Math.min(filled, 8); i++){
    drawVirtualPeg(x - w*0.32 + (i % 4) * w*0.21, y + h*0.16 - Math.floor(i/4) * h*0.34, s*0.6);
  }
  ctx.restore();
}

function drawVirtualBar(x1, y1, x2, y2){
  ctx.save();
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
  ctx.lineCap = "round";
  ctx.lineWidth = Math.max(10, vobjSize() * 0.28);
  ctx.strokeStyle = "#C89A6B"; ctx.stroke();
  ctx.lineWidth = Math.max(4, vobjSize() * 0.10);
  ctx.strokeStyle = "rgba(255,255,255,0.35)"; ctx.stroke();
  ctx.restore();
}

// The carried object sits in the palm of the tracked hand (palm centre of the
// hand landmarks, which are steadier than the pose wrist) and its position is
// smoothed frame to frame so it glides with the hand instead of jittering.
let vobjAnchor = null;
let vobjPalmOffset = {x:0, y:0};
// Palm centre from the pose model's own hand points (pinky and index
// knuckles sit at wrist+2 / wrist+4): a held cup sits in the palm, about
// 60% of the way from the wrist joint to the knuckles. Used whenever the hand
// model is not running (hand-to-mouth) or has not reported recently, so the
// cup is drawn in the middle of the hand rather than at the wrist joint.
function posePalmCentre(lm){
  const wrist = lm && lm[ACTIVE.wrist];
  const pinky = lm && lm[ACTIVE.wrist + 2], index = lm && lm[ACTIVE.wrist + 4];
  if(!pointVisible(wrist) || !pointVisible(pinky) || !pointVisible(index)) return null;
  const knuckles = midpoint(pinky, index);
  return {x: wrist.x + (knuckles.x - wrist.x) * 0.6, y: wrist.y + (knuckles.y - wrist.y) * 0.6};
}
function vobjWristPoint(lm){
  // The pose wrist is updated every frame (the hand model may run less often),
  // so the cup follows it without lag; the offset from wrist to palm centre is
  // taken from the latest hand detection so the cup still sits in the palm.
  const wrist = lm && lm[ACTIVE.wrist] && pointVisible(lm[ACTIVE.wrist]) ? lm[ACTIVE.wrist] : null;
  if(handIsFresh(performance.now()) && latestHandLandmarks.length >= 21){
    const h = latestHandLandmarks;
    const palm = {x:(h[0].x + h[5].x + h[9].x + h[13].x + h[17].x) / 5, y:(h[0].y + h[5].y + h[9].y + h[13].y + h[17].y) / 5};
    vobjPalmOffset = wrist ? {x: palm.x - wrist.x, y: palm.y - wrist.y} : {x:0, y:0};
    if(!wrist) return vobjSmooth(palm);
  }else{
    const palm = posePalmCentre(lm);
    if(palm && wrist) vobjPalmOffset = {x: palm.x - wrist.x, y: palm.y - wrist.y};
  }
  if(!wrist) return vobjAnchor ? {x: vobjAnchor.x * canvas.width, y: vobjAnchor.y * canvas.height} : null;
  return vobjSmooth({x: wrist.x + vobjPalmOffset.x, y: wrist.y + vobjPalmOffset.y});
}
function nextVirtualObjectAnchor(previous,target){
  if(!previous) return {x:target.x,y:target.y};
  const travel=Math.hypot(target.x-previous.x,target.y-previous.y);
  if(travel > 0.12) return {x:target.x,y:target.y};
  // At rest, retain just enough history to suppress one-frame wrist jitter.
  // During a real reach, raise the follow weight toward 98% so the cup stays
  // visually attached to the current wrist/palm instead of trailing it.
  const follow=clamp(0.84+travel*3.0,0.84,0.98);
  return {
    x:previous.x*(1-follow)+target.x*follow,
    y:previous.y*(1-follow)+target.y*follow,
  };
}
function vobjSmooth(target){
  vobjAnchor=nextVirtualObjectAnchor(vobjAnchor,target);
  return {x: vobjAnchor.x * canvas.width, y: vobjAnchor.y * canvas.height};
}

function drawVirtualObject(lm, handLm){
  if(!VOBJ || calibrating) return;
  const s = vobjSize();
  if(VOBJ.mode === "carry"){
    if(vobjState === "carried"){
      const p = vobjWristPoint(lm);
      if(p) drawVirtualCup(p.x, p.y - s*0.15, s);
      return;
    }
    if(vobjState === "placed" && vobjPlacePos){
      drawVirtualCup(vobjPlacePos.x * canvas.width, vobjPlacePos.y * canvas.height, s);
      return;
    }
    const grab = CFG.cycle[VOBJ.grab_step];
    if(grab && grab.target){
      const t = effectiveExerciseTarget(grab);
      drawVirtualCup(t.x * canvas.width, t.y * canvas.height, s);
    }
    return;
  }
  if(VOBJ.mode === "held"){
    const p = vobjWristPoint(lm);
    if(p) drawVirtualCup(p.x, p.y - s*0.15, s);
    return;
  }
  if(VOBJ.mode === "hand_anchor"){
    let x = canvas.width * 0.5, y = canvas.height * 0.55;
    if(handLm && handLm.length){
      let sx = 0, sy = 0;
      for(const p of handLm){ sx += p.x; sy += p.y; }
      x = (sx / handLm.length) * canvas.width; y = (sy / handLm.length) * canvas.height;
    }
    drawVirtualBall(x, y, s);
    return;
  }
  if(VOBJ.mode === "pick_place"){
    drawVirtualContainer(VOBJ.container.x * canvas.width, VOBJ.container.y * canvas.height, s, vobjPlacedCount);
    if(vobjPlacedCount < CFG.reps) drawVirtualPeg(VOBJ.source.x * canvas.width, VOBJ.source.y * canvas.height, s);
    return;
  }
  if(VOBJ.mode === "between_hands" && lm){
    const Lw = lm[15], Rw = lm[16];
    if(Lw && Rw) drawVirtualBar(Lw.x * canvas.width, Lw.y * canvas.height, Rw.x * canvas.width, Rw.y * canvas.height);
  }
}

function vobjOnStepCompleted(completedStep){
  noteHandStepCompleted(CFG.cycle[completedStep]);
  if(!VOBJ || VOBJ.mode !== "carry") return;
  if(completedStep === VOBJ.grab_step) vobjState = "carried";
  if(completedStep === VOBJ.place_step && vobjState === "carried"){
    vobjState = "placed";
    const place = CFG.cycle[VOBJ.place_step];
    vobjPlacePos = place && place.target ? effectiveExerciseTarget(place) : null;
  }
}

function drawOverlay(lm,handLm){
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if(lm){
    drawingUtils.drawLandmarks(lm,{color:ASSESSMENT_OVERLAY_STYLE.landmarkColor,radius:ASSESSMENT_OVERLAY_STYLE.landmarkRadius});
    drawingUtils.drawConnectors(lm,PoseLandmarker.POSE_CONNECTIONS,{color:ASSESSMENT_OVERLAY_STYLE.connectorColor,lineWidth:ASSESSMENT_OVERLAY_STYLE.connectorWidth});
  }
  if(handLm){
    drawingUtils.drawConnectors(handLm,HandLandmarker.HAND_CONNECTIONS,{color:ASSESSMENT_HAND_OVERLAY_STYLE.connectorColor,lineWidth:ASSESSMENT_HAND_OVERLAY_STYLE.connectorWidth});
    drawingUtils.drawLandmarks(handLm,{color:ASSESSMENT_HAND_OVERLAY_STYLE.landmarkColor,radius:ASSESSMENT_HAND_OVERLAY_STYLE.landmarkRadius});
  }
  if(calibrating){
    if(HAS_DYNAMIC_LAP_TARGET && exerciseLapTargetCalibration.ready && exerciseLapTargetCalibration.target){
      const tx=exerciseLapTargetCalibration.target.x*canvas.width;
      const ty=exerciseLapTargetCalibration.target.y*canvas.height;
      const tr=.075*Math.min(canvas.width,canvas.height);
      ctx.save();
      ctx.beginPath(); ctx.arc(tx,ty,tr,0,Math.PI*2);
      ctx.fillStyle=ASSESSMENT_OVERLAY_STYLE.calibrationTargetFill; ctx.fill();
      ctx.lineWidth=ASSESSMENT_OVERLAY_STYLE.targetEdgeWidth;
      ctx.strokeStyle=ASSESSMENT_OVERLAY_STYLE.calibrationTargetColor; ctx.stroke();
      ctx.beginPath(); ctx.arc(tx,ty,Math.max(5,tr*.16),0,Math.PI*2);
      ctx.fillStyle="#FDFDFD"; ctx.fill();
      ctx.restore();
    }
    return;
  }
  const sub = CFG.cycle[currentSubStep];
  if(sub && sub.target){
    const armed=exerciseTargetIsArmed();
    const target=effectiveExerciseTarget(sub);
    const tx = target.x*canvas.width;
    const ty = target.y*canvas.height;
    const tr = effectiveExerciseTargetRadius(sub,lm)*Math.min(canvas.width,canvas.height);
    // A still, faint ring means "listen". Pulsing starts only when Alira has
    // finished and the target can actually respond to the patient.
    const pulse = armed ? 1 + 0.08*Math.sin(performance.now()/250) : 1;
    ctx.save();
    ctx.beginPath(); ctx.arc(tx,ty,tr*pulse,0,Math.PI*2);
    ctx.lineWidth=ASSESSMENT_OVERLAY_STYLE.targetEdgeWidth;
    ctx.strokeStyle=armed ? ASSESSMENT_OVERLAY_STYLE.targetColor : "rgba(225,142,109,0.28)";
    ctx.setLineDash(armed ? [] : [10,8]);
    ctx.stroke();
    ctx.restore();
    if(armed){
      ctx.beginPath(); ctx.arc(tx,ty,tr*ASSESSMENT_OVERLAY_STYLE.targetInnerScale,0,Math.PI*2);
      ctx.fillStyle = "rgba(225,142,109,0.4)"; ctx.fill();
    }
    if(HAND_GATE_LANDMARKS.has(sub.target.landmark)){
      // The canvas is CSS-mirrored: flip the text back so it reads correctly.
      const hint = sub.target.landmark === "HAND_OPEN" ? "Open hand" : "Close hand";
      ctx.save();
      ctx.translate(tx, ty - tr - 18); ctx.scale(-1, 1);
      const size=Math.max(13,Math.round(Math.min(canvas.width,canvas.height)*0.036));
      ctx.font=`700 ${size}px system-ui, -apple-system, sans-serif`;
      ctx.textAlign="center"; ctx.textBaseline="middle";
      ctx.fillStyle="rgba(28,32,29,0.78)";
      const w=ctx.measureText(hint).width+size;
      if(ctx.roundRect){ ctx.beginPath(); ctx.roundRect(-w/2, -size*0.75, w, size*1.5, size*0.75); ctx.fill(); }
      ctx.fillStyle="#FDFDFD"; ctx.fillText(hint, 0, 0);
      ctx.restore();
    }
    if(armed && inTargetSince){
      const elapsed = performance.now() - inTargetSince;
      const progress = Math.min(1, elapsed / sub.hold_ms);
      ctx.beginPath(); ctx.arc(tx,ty,tr*ASSESSMENT_OVERLAY_STYLE.holdRingScale,-Math.PI/2,-Math.PI/2+progress*Math.PI*2);
      ctx.strokeStyle=ASSESSMENT_OVERLAY_STYLE.holdRingColor;
      ctx.lineWidth=ASSESSMENT_OVERLAY_STYLE.holdRingWidth;
      ctx.stroke();
    }
  }
  drawVirtualObject(lm, handLm);
  drawLiveDegrees(lm);
  if(TEMPORARY_COMPENSATION_EVIDENCE && activeMovementPhase() && liveCompensationIds.size){
    drawCompensationHighlights(ctx,lm,[...liveCompensationIds]);
  }
}

// Live readout so the patient can see the numbers being graded: the elbow
// angle at the elbow joint (green once it meets today's target), shoulder
// lift at the shoulder, and trunk lean at the chest when either is present.
function drawDegreeLabel(x, y, text, color){
  ctx.save();
  const size=Math.max(13,Math.round(Math.min(canvas.width,canvas.height)*0.036));
  ctx.font=`700 ${size}px system-ui, -apple-system, sans-serif`;
  const padding=size*0.45, width=ctx.measureText(text).width+padding*2, height=size*1.5;
  // The canvas is shown mirrored (selfie view), so the label is drawn mirrored
  // too: it then reads normally on screen and in the evidence still, covering
  // the same area next to the joint.
  ctx.translate(canvas.width,0); ctx.scale(-1,1);
  const boxX=canvas.width-x-width;
  ctx.fillStyle="rgba(28,32,29,0.78)";
  ctx.beginPath();
  if(ctx.roundRect) ctx.roundRect(boxX, y-height/2, width, height, height/2); else ctx.rect(boxX, y-height/2, width, height);
  ctx.fill();
  ctx.fillStyle=color; ctx.textBaseline="middle";
  ctx.fillText(text, boxX+padding, y);
  ctx.restore();
}
// Where each metric's live number is shown, and what the patient sees it called.
const LIVE_METRIC_LABELS={
  elbow_extension:{joint:"elbow",text:"Elbow"},
  elbow_flexion:{joint:"elbow",text:"Elbow bend"},
  shoulder_flexion:{joint:"shoulder",text:"Reach"},
  shoulder_abduction:{joint:"shoulder",text:"Arm across"},
  bilateral_shoulder_flexion:{joint:"shoulder",text:"Both arms"},
  finger_extension:{joint:"hand",text:"Hand open"},
  pinch_flexion:{joint:"hand",text:"Pinch"},
};
function drawLiveDegrees(lm){
  if(!lm || calibrating || !lastRawMetrics || STANDARD.tracking_mode==="hand") return;
  const raw=lastRawMetrics;
  const anchors={
    elbow:lm[ACTIVE.elbow] ? {x:lm[ACTIVE.elbow].x*canvas.width+14, y:lm[ACTIVE.elbow].y*canvas.height} : null,
    shoulder:lm[ACTIVE.shoulder] ? {x:lm[ACTIVE.shoulder].x*canvas.width+14, y:lm[ACTIVE.shoulder].y*canvas.height+4} : null,
    hand:(latestHandLandmarks && latestHandLandmarks[9]) ? {x:latestHandLandmarks[9].x*canvas.width+16, y:latestHandLandmarks[9].y*canvas.height}
      : (lm[ACTIVE.wrist] ? {x:lm[ACTIVE.wrist].x*canvas.width+16, y:lm[ACTIVE.wrist].y*canvas.height} : null),
  };
  const used={};
  const place=(joint,text,color)=>{
    const anchor=anchors[joint];
    if(!anchor) return;
    const row=used[joint]||0; used[joint]=row+1;
    drawDegreeLabel(anchor.x, anchor.y+row*26, text, color);
  };
  // 1) the joints being scored, against today's target (green once met)
  for(const step of STANDARD.rom_steps||[]){
    const spec=LIVE_METRIC_LABELS[step.metric];
    if(!spec) continue;
    const value=step.metric==="finger_extension"
      ? (handIsFresh(performance.now()) ? handOpenDegreesSmoothed : NaN)
      : metricValue(step.metric,raw);
    if(!Number.isFinite(value)) continue;
    const target=Number(step.target_deg||0);
    const text=(CFG.live_metric_text && CFG.live_metric_text[step.metric]) || spec.text;
    place(spec.joint, `${text} ${Math.round(value)}°${target?` / ${Math.round(target)}°`:""}`, value>=target?"#9EE8B5":"#FFD27A");
  }
  // 2) the compensations being watched (shown once they start to appear)
  const shoulderRule=(STANDARD.compensations||[]).find(item=>item.metric==="shoulder_hike_delta");
  const hike=shoulderHikeDegrees(raw);
  if(shoulderRule && Number.isFinite(hike) && hike>=2 && lm[ACTIVE.shoulder]){
    drawDegreeLabel(lm[ACTIVE.shoulder].x*canvas.width+14, lm[ACTIVE.shoulder].y*canvas.height-22, `Shoulder lift ${Math.round(hike)}°`, compensationExceeded(shoulderRule,raw,hike)?"#FF9B8A":"#FFD27A");
  }
  const mid={x:(lm[11].x+lm[12].x)/2,y:(lm[11].y+lm[12].y)/2};
  const leanRule=(STANDARD.compensations||[]).find(item=>item.metric==="trunk_lean_delta");
  const lean=leanRule ? metricValue("trunk_lean_delta",raw) : NaN;
  if(Number.isFinite(lean) && lean>=4){
    drawDegreeLabel(mid.x*canvas.width-40, mid.y*canvas.height+34, `Trunk lean ${Math.round(lean)}°`, lean>=Number(leanRule.threshold_deg||12)?"#FF9B8A":"#FFD27A");
  }
  const sideRule=(STANDARD.compensations||[]).find(item=>item.metric==="trunk_side_lean_delta");
  const side=sideRule ? metricValue("trunk_side_lean_delta",raw) : NaN;
  if(Number.isFinite(side) && side>=4){
    drawDegreeLabel(mid.x*canvas.width-40, mid.y*canvas.height+60, `Side lean ${Math.round(side)}°`, side>=Number(sideRule.threshold_deg||10)?"#FF9B8A":"#FFD27A");
  }
  const headRule=(STANDARD.compensations||[]).find(item=>item.metric==="head_drop_deg");
  const headDrop=headRule ? metricValue("head_drop_deg",raw) : NaN;
  if(Number.isFinite(headDrop) && headDrop>=4 && lm[0]){
    drawDegreeLabel(lm[0].x*canvas.width+18, lm[0].y*canvas.height-10, `Head drop ${Math.round(headDrop)}°`, headDrop>=Number(headRule.threshold_deg||15)?"#FF9B8A":"#FFD27A");
  }
  const flareRule=(STANDARD.compensations||[]).find(item=>item.metric==="elbow_flare_deg");
  const flare=flareRule && ruleAppliesNow(flareRule) ? metricValue("elbow_flare_deg",raw) : NaN;
  if(Number.isFinite(flare) && flare>=20 && anchors.elbow){
    drawDegreeLabel(anchors.elbow.x, anchors.elbow.y+26, `Elbow out ${Math.round(flare)}°`, flare>=Number(flareRule.threshold_deg||45)?"#FF9B8A":"#FFD27A");
  }
  const wristRule=(STANDARD.compensations||[]).find(item=>item.metric==="wrist_flexion_delta");
  const wristDrop=wristRule ? metricValue("wrist_flexion_delta",raw) : NaN;
  if(Number.isFinite(wristDrop) && wristDrop>=6 && anchors.hand && ruleAppliesNow(wristRule)){
    drawDegreeLabel(anchors.hand.x, anchors.hand.y-26, `Wrist bend ${Math.round(wristDrop)}°`, wristDrop>=Number(wristRule.threshold_deg||18)?"#FF9B8A":"#FFD27A");
  }
}

async function startRep(){
  if(runnerExited) return;
  currentSubStep = 0;
  feedbackPending = false;
  resetRepMetrics();
  vobjState = "resting"; vobjPlacePos = null; vobjAnchor = null;
  handOpenRef = null; handClosedRef = null;
  repLabel.textContent = `Repetition ${currentRep+1} of ${CFG.reps}`;
  // Update the visual rep progress bar
  try{
    const pct = Math.round((currentRep / Math.max(1, CFG.reps)) * 100);
    const fill = document.getElementById("repBarFill");
    if(fill) fill.style.width = pct + "%";
  }catch(e){}
  if(CFG.pose_mode === "tap" || CFG.pose_mode === "guided"){
    tapBtn.classList.remove("hidden");
    tapBtn.textContent = CFG.pose_mode === "guided" ? "I completed this step" : "I did one repetition";
  }else{
    tapBtn.classList.add("hidden");
  }
  await startSubStep();
}

async function startSubStep(){
  if(runnerExited) return;
  const instructionToken=++stepInstructionToken;
  const sub = CFG.cycle[currentSubStep];
  captionEl.textContent = sub.caption;
  stepStartTime = performance.now();
  inTargetSince = null; stepCompleted = false; handGateNearSince = null; stepVoiceFinishedAt = 0;
  tapBtn.disabled = true;
  prefetchVoice(CFG.cycle[currentSubStep + 1] && CFG.cycle[currentSubStep + 1].voice);
  const voiceForStep = currentSubStep;
  const voiceStatus=await playVoice(sub.voice);
  if(currentSubStep !== voiceForStep || instructionToken !== stepInstructionToken || voiceStatus === "interrupted") return;
  // Discard any hold or gesture timing accumulated while Alira was speaking.
  // Only a fresh post-instruction frame may begin the target hold.
  inTargetSince=null;
  lastInTargetTs=0;
  handGateNearSince=null;
  stepStartTime=performance.now();
  stepVoiceFinishedAt=stepStartTime;
  setTimeout(()=>{
    if(currentSubStep===voiceForStep && instructionToken===stepInstructionToken && exerciseTargetIsArmed()){
      voiceText.textContent="Your turn";
    }
  },TARGET_ARM_DELAY_AFTER_VOICE_MS);
  tapBtn.disabled = false;
}

function compensationProblemText(rule){
  const degrees=Math.round(peakCompensationDegrees[rule.id]||0);
  const metric=String(rule.metric||"");
  // Exercise-specific wording first, then the generic descriptions. Camera
  // posture alone cannot establish whether the back touched the chair.
  const specific=CFG.compensation_problems && CFG.compensation_problems[rule.id];
  if(specific) return `${specific} (${degrees} degrees)`;
  if(metric==="trunk_lean_delta") return `your trunk leaned forward (${degrees} degrees)`;
  if(metric==="shoulder_hike_delta") return `your shoulder lifted toward your ear (${degrees} degrees, more than the reach itself needs)`;
  if(metric==="arm_asymmetry") return `your arms moved unevenly (${degrees} degrees apart)`;
  if(metric==="hip_hike_delta") return `your hip lifted (${degrees} degrees)`;
  if(metric==="head_drop_deg") return `your head dropped toward your hand (${degrees} degrees)`;
  if(metric==="wrist_flexion_delta") return `your wrist bent instead of your fingers (${degrees} degrees)`;
  return `${String(rule.id||"a compensation").replace(/_/g," ")} (${degrees} degrees)`;
}
function joinProblems(items){
  if(items.length<=1) return items[0]||"";
  return items.slice(0,-1).join(", ")+" and "+items[items.length-1];
}
function pickFeedback(){
  if(trackingFrames < SCORING_MIN_FRAMES) return "I could not see enough of that repetition to score it reliably. Check your camera position, then try again at a comfortable pace.";
  const confirmed=confirmedCompensations();
  if(confirmed.length){
    // 1) name every pattern noticed, with the measured degrees;
    // 2) give one clear correction for the next repetition.
    const problems=confirmed.map(rule=>compensationProblemText(rule));
    const elbowStep=(STANDARD.rom_steps||[]).find(step=>step.metric==="elbow_extension");
    problems.push(...incompleteRomSteps().map(romProblemText));
    const correction=CFG.correct_form_cue
      || (elbowStep
        ? "Next time, keep your trunk and shoulder still and simply extend your elbow to reach the target."
        : confirmed.map(rule=>rule.correction).join(" "));
    return `I noticed ${joinProblems(problems)}. ${correction}`;
  }
  // No compensation, but a step stopped short of today's target (for example
  // the elbow stayed bent): say which one, in degrees, and how to fix it.
  const incomplete=incompleteRomSteps();
  if(incomplete.length){
    const elbowShort=incomplete.some(item=>item.metric==="elbow_extension");
    const first=(STANDARD.rom_steps||[]).find(step=>step.id===incomplete[0].id)||{};
    const correction=(elbowShort && CFG.correct_form_cue)
      || first.coaching_cue
      || "On the next repetition, move a little farther within a comfortable range while keeping the same safe setup.";
    return `I noticed ${joinProblems(incomplete.map(romProblemText))}. ${correction}`;
  }
  if(unmeasuredRomSteps().some(item=>item.metric==="finger_extension"||item.metric==="pinch_flexion")){
    return "I could not see your affected hand clearly enough to check your grip. Keep your whole hand inside the camera view on the next repetition.";
  }
  if(wristAlignmentUnmeasured()){
    return "I could not see your wrist clearly enough to check its alignment while you gripped and carried the cup. Keep your affected elbow, wrist, and whole hand visible, with your forearm viewed from the side rather than pointing straight into the camera.";
  }
  return "That movement stayed close to today’s target. Keep the same smooth control on the next repetition.";
}

// Voice confirmation uses the browser recognizer when available and records a
// short microphone turn for server-side OpenAI transcription everywhere else.
let recognition = null;
let mediaRecorder = null;
let mediaStopTimer = null;
let listeningToken = 0;
let yesHeard = false;
let confirming = false;
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
function isAdvancePhrase(text){
  const normalized=String(text||"").toLowerCase().replace(/[^a-z\s]/g," ").replace(/\s+/g," ").trim();
  if(!normalized || normalized.split(" ").length > 7) return false;
  if(/\b(no|not|don t|do not|stop|wait)\b/.test(normalized)) return false;
  return /\b(yes|yeah|yep|ready|continue|next|okay|ok)\b/.test(normalized);
}
function handleConfirmationTranscript(text){
  if(yesHeard || confirming || !fbEl.classList.contains("show")) return;
  fbHeard.textContent='"'+String(text||"").slice(-60).trim()+'"';
  if(!isAdvancePhrase(text)) return;
  yesHeard=true;
  checkYes.textContent="✓ Heard you";
  checkYes.classList.add("ok");
  stopListening();
  void confirmAndContinue();
}
function supportedRecorderType(){
  if(!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
  return ["audio/webm;codecs=opus","audio/webm","audio/mp4"].find(type=>MediaRecorder.isTypeSupported(type))||"";
}
async function recordAndTranscribeConfirmation(token){
  if(token !== listeningToken || yesHeard || confirming || !fbEl.classList.contains("show")) return;
  if(!window.MediaRecorder){
    fbHeard.textContent="Voice confirmation is unavailable. Tap Continue when ready.";
    return;
  }
  try{
    if(!confirmationAudioStream || !confirmationAudioStream.getAudioTracks().some(track=>track.readyState==="live")){
      confirmationAudioStream=await navigator.mediaDevices.getUserMedia({audio:true});
    }
    const mimeType=supportedRecorderType();
    const chunks=[];
    mediaRecorder=new MediaRecorder(confirmationAudioStream,mimeType?{mimeType}:undefined);
    mediaRecorder.ondataavailable=event=>{ if(event.data&&event.data.size) chunks.push(event.data); };
    mediaRecorder.onerror=()=>{ fbHeard.textContent="I could not hear that. Say yes again, or tap Continue."; };
    mediaRecorder.onstop=async()=>{
      if(token !== listeningToken || yesHeard || confirming) return;
      try{
        const blob=new Blob(chunks,{type:mimeType||"audio/webm"});
        if(blob.size < 200){ throw new Error("empty recording"); }
        fbHeard.textContent="Checking what you said…";
        const form=new FormData();
        form.append("file",blob,mimeType.includes("mp4")?"confirmation.m4a":"confirmation.webm");
        const response=await fetch(API_BASE+"/stt/transcribe",{method:"POST",body:form});
        if(!response.ok) throw new Error("transcription failed");
        const result=await response.json();
        handleConfirmationTranscript(result.text||"");
      }catch(e){
        fbHeard.textContent="I did not catch that. Say yes again, or tap Continue.";
      }
      if(token === listeningToken && !yesHeard && !confirming){
        setTimeout(()=>void recordAndTranscribeConfirmation(token),350);
      }
    };
    fbHeard.textContent="Listening… say yes when you are ready.";
    mediaRecorder.start();
    mediaStopTimer=setTimeout(()=>{
      if(mediaRecorder&&mediaRecorder.state==="recording") mediaRecorder.stop();
    },2800);
  }catch(e){
    fbHeard.textContent="Microphone unavailable. Tap Continue when ready.";
  }
}
function startListening(){
  stopListening();
  yesHeard = false;
  confirming = false;
  const token=listeningToken;
  checkYes.textContent = "○ Say \"yes\"";
  checkYes.classList.remove("ok");
  checkUnderstand.textContent = "";
  fbHeard.textContent = "Listening… or tap Continue when ready.";
  if(!SR){ void recordAndTranscribeConfirmation(token); return; }
  try{
    recognition = new SR();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "en-US";
    recognition.onresult = (e) => {
      let txt = "";
      for(let i = e.resultIndex; i < e.results.length; i++){
        txt += e.results[i][0].transcript.toLowerCase();
      }
      handleConfirmationTranscript(txt);
    };
    recognition.onerror = () => {
      try{ recognition.onend = null; }catch(e){}
      recognition=null;
      void recordAndTranscribeConfirmation(token);
    };
    recognition.onend = () => {
      if(running && !yesHeard && !confirming && token === listeningToken){
        try{ recognition.start(); }catch(e){ void recordAndTranscribeConfirmation(token); }
      }
    };
    recognition.start();
  }catch(e){
    recognition=null;
    void recordAndTranscribeConfirmation(token);
  }
}
function stopListening(){
  listeningToken+=1;
  if(mediaStopTimer){ clearTimeout(mediaStopTimer); mediaStopTimer=null; }
  if(mediaRecorder){
    try{
      mediaRecorder.onstop=null;
      if(mediaRecorder.state==="recording") mediaRecorder.stop();
    }catch(e){}
    mediaRecorder=null;
  }
  if(recognition){
    try{ recognition.onend = null; recognition.stop(); }catch(e){}
    recognition = null;
  }
}

let lastFeedbackText = "";
let lastRepScore = null;
// A compensation is confirmed by the workbook rule (enough frames AND a large
// enough share of the movement) or, whatever the share, when it was held for
// about a second of movement (SUSTAINED_COMPENSATION_FRAMES).
const SUSTAINED_COMPENSATION_FRAMES=Number(SCORING_METHOD.sustained_compensation_frames)||24;
function confirmedCompensations(){
  return (STANDARD.compensations||[]).filter(rule=>{
    const hits=Number(compensationHits[rule.id]||0);
    const eligible=Math.max(1,Number(compensationEligible[rule.id]||0));
    if(hits < Number(rule.min_frames||8)) return false;
    if(Number(compensationLongestStreak[rule.id]||0) < Number(rule.min_consecutive_frames||0)) return false;
    return hits/eligible >= Number(rule.min_ratio||.35) || hits >= SUSTAINED_COMPENSATION_FRAMES;
  });
}
const ROM_FULL_CREDIT_RATIO=Number(SCORING_METHOD.full_credit_ratio)||0.95;
function repRomDetails(){
  return (STANDARD.rom_steps||[]).map(step=>{
    const measured=Number.isFinite(Number(romBest[step.id]));
    const achieved=Math.round(Number(romBest[step.id]||0)*10)/10;
    const target=Number(step.target_deg||0);
    // Within 5% of today's target counts as full attainment for the step.
    const score=target>0 ? Math.round(clamp(achieved/(target*ROM_FULL_CREDIT_RATIO),0,1)*100) : 0;
    return {id:step.id,label:step.label,metric:step.metric,achieved_deg:achieved,target_deg:target,score,weight:Number(step.weight||0),measured};
  });
}
// A step the camera never measured (for example the fingers when the hand was
// out of view) neither scores nor fails the repetition - the patient is asked
// to keep the hand in view instead.
function measuredRomDetails(){
  const details=repRomDetails();
  const measured=details.filter(item=>item.measured);
  return measured.length ? measured : details;
}
// Steps that must be within range for a repetition to count as correct: the
// elbow (a bent elbow can still arrive at the target by leaning or hiking)
// and the fingers (a grasp task is not done until the hand really opens).
const FORM_CRITICAL_METRICS=new Set(["elbow_extension","elbow_flexion","finger_extension"]);
const POINT_THRESHOLD=Number(SCORING_METHOD.point_threshold)||90;
const ROM_COMPLETE_RATIO=Number(SCORING_METHOD.complete_rom_ratio)||0.9;
// An elbow that stayed bent (below 90% of today's extension target) makes the
// repetition incomplete, whatever the other joints did: the on-screen target
// verifies the reach itself, but a patient can arrive there with a bent elbow
// by leaning or hiking, so the elbow is checked separately. Other steps count
// through their score weight only.
function incompleteRomSteps(){
  return repRomDetails().filter(item=>item.measured && FORM_CRITICAL_METRICS.has(item.metric) && item.target_deg>0 && item.achieved_deg < item.target_deg*ROM_COMPLETE_RATIO);
}
function unmeasuredRomSteps(){
  return repRomDetails().filter(item=>!item.measured);
}
function wristAlignmentUnmeasured(){
  if(CFG.exercise_id !== "ex_grasp") return false;
  const rule=(STANDARD.compensations||[]).find(item=>item.id === "wrist_flexion");
  return !!rule && Number(compensationEligible[rule.id]||0) < Number(rule.min_frames||12);
}
function romProblemText(item){
  const achieved=Math.round(item.achieved_deg), target=Math.round(item.target_deg);
  const metric=String(item.metric||"");
  if(metric==="elbow_extension") return achieved>0
    ? `your elbow stayed bent at ${achieved} of ${target} degrees`
    : `your elbow did not straighten toward the target (${target} degrees)`;
  if(metric==="elbow_flexion") return `your elbow bent ${achieved} of ${target} degrees`;
  if(metric==="shoulder_flexion"||metric==="shoulder_abduction"||metric==="bilateral_shoulder_flexion") return `your arm reached ${achieved} of ${target} degrees`;
  if(metric==="finger_extension") return `your fingers opened to ${achieved} of ${target} degrees`;
  if(metric==="pinch_flexion") return `your pinch closed to ${achieved} of ${target} degrees`;
  return `your ${String(item.label||metric).toLowerCase()} reached ${achieved} of ${target} degrees`;
}
function computeRepScore(){
  if(trackingFrames < SCORING_MIN_FRAMES || activeFrames < SCORING_MIN_FRAMES) return null;
  const details=measuredRomDetails();
  const totalWeight=details.reduce((sum,item)=>sum+item.weight,0)||1;
  let score=details.reduce((sum,item)=>sum+item.score*item.weight,0)/totalWeight;
  const confirmed=confirmedCompensations();
  for(const rule of confirmed) score-=Number(rule.penalty||0);
  // A repetition completed through a compensatory pattern scores a fixed 70:
  // the patient is told what went wrong and how to fix it, and it earns no point.
  if(confirmed.length) score=Number(SCORING_METHOD.compensation_score)||70;
  // An incomplete repetition (a step short of its target, e.g. a bent elbow)
  // never reaches the point threshold either.
  else if(incompleteRomSteps().length) score=Math.min(score,POINT_THRESHOLD-1);
  else if(wristAlignmentUnmeasured()) score=Math.min(score,POINT_THRESHOLD-1);
  return Math.round(clamp(score,0,100));
}
function repEarnsPoint(score){
  // Only a verified correct repetition (no compensation, every form-critical
  // step measured and within range, score at or above the threshold) earns
  // the point.
  if(score == null) return false;
  if(confirmedCompensations().length) return false;
  if(incompleteRomSteps().length) return false;
  if(unmeasuredRomSteps().some(item=>FORM_CRITICAL_METRICS.has(item.metric))) return false;
  if(wristAlignmentUnmeasured()) return false;
  return score >= POINT_THRESHOLD;
}
function pointBlockedByVisibility(){
  return !confirmedCompensations().length && !incompleteRomSteps().length
    && (unmeasuredRomSteps().some(item=>FORM_CRITICAL_METRICS.has(item.metric)) || wristAlignmentUnmeasured());
}

function scoreLabel(s){
  if(s >= 90) return "Excellent form";
  if(s >= 75) return "Great work";
  if(s >= 60) return "Good effort";
  if(s >= 45) return "Keep practicing";
  return "Take it gently";
}

async function showFeedback(){
  if(feedbackPending || fbEl.classList.contains("show")) return;
  feedbackPending = true;
  const feedback = pickFeedback();
  lastFeedbackText = feedback;
  lastRepScore = computeRepScore();
  const confirmed=confirmedCompensations();
  const cameraScored = lastRepScore != null;
  const label = cameraScored ? scoreLabel(lastRepScore) : "Well done";
  // Movement quality is recorded separately from the daily-plan reward.
  const pointEarned = cameraScored ? repEarnsPoint(lastRepScore) : true;
  if(pointEarned) qualityReps += 1;
  fbReward.innerHTML = pointEarned
    ? '<span class="rewardStar" aria-hidden="true">&#11088;</span><span class="rewardCopy"><strong>Repetition complete</strong><span>Great repetition!</span></span>'
    : (cameraScored && pointBlockedByVisibility()
      ? '<span class="rewardCopy"><strong>Repetition complete</strong><span>Keep your hand in view for feedback.</span></span>'
      : '<span class="rewardCopy"><strong>Repetition complete</strong><span>Use the feedback for your next repetition.</span></span>');
  fbStep.textContent = `Repetition ${currentRep+1} of ${CFG.reps} complete · ${label}`;
  fbTitle.textContent = cameraScored ? `Your score: ${lastRepScore}/100` : "Repetition complete";
  fbBody.textContent = feedback;
  showTemporaryCompensationEvidence(confirmed);
  if(navigator.vibrate) navigator.vibrate([50, 30, 80]);
  fbEl.classList.remove("hidden");
  requestAnimationFrame(() => fbEl.classList.add("show"));
  postRN({
    type:"rep_complete",
    rep:currentRep+1,
    total:CFG.reps,
    score:lastRepScore,
    point_earned:false,
    movement_correct:pointEarned,
    feedback,
    rom_metrics:repRomDetails(),
    compensations:confirmed.map(rule=>({id:rule.id,correction:rule.correction})),
    tracking_quality:trackingFrames/Math.max(1,trackingFrames+lowQualityFrames),
  });

  // Voice: feedback + ask for "yes"
  const rewardVoice = "Repetition complete. Thank you for your effort.";
  const feedbackVoice = cameraScored
    ? `${rewardVoice} Your score is ${lastRepScore} out of 100. ${label}. ${feedback} When you're ready, tap continue, or say yes to keep going.`
    : `${rewardVoice} ${label}. ${feedback} When you're ready, tap continue, or say yes to keep going.`;
  await playVoice(feedbackVoice,"general");
  if(fbEl.classList.contains("show")) startListening();
}

async function confirmAndContinue(){
  if(confirming) return;
  confirming=true;
  stopListening();
  fbEl.classList.remove("show");
  setTimeout(()=> fbEl.classList.add("hidden"), 350);
  clearTemporaryCompensationEvidence();
  currentRep += 1;
  if(currentRep >= CFG.reps){
    finishExercise();
    return;
  }
  await playVoice("Wonderful. Here we go.");
  await startRep();
  confirming=false;
}

// After the last repetition, ask honestly whether a carer or family member
// helped: an assisted session still counts, but its score is halved so the
// functional-domain score reflects independent ability.
async function askAssistance(){
  return await new Promise((resolve) => {
    assistEl.classList.remove("hidden");
    assistYesBtn.onclick = () => { assistEl.classList.add("hidden"); resolve(true); };
    assistNoBtn.onclick = () => { assistEl.classList.add("hidden"); resolve(false); };
    playVoice("One quick question. Did a carer or family member help you move during this exercise? Please tap yes or no.").catch(() => {});
  });
}

async function finishExercise(){
  if(runnerExited) return;
  running = false;
  stopListening();
  fbEl.classList.add("hidden");
  clearTemporaryCompensationEvidence();
  const assisted = await askAssistance();
  captionEl.textContent = "Exercise complete!";
  if(assisted){
    await playVoice("Thank you for telling me. Working together with your carer still counts, and this session is recorded as helper supported.");
  }else{
    await playVoice("Magnificent work. You have finished this exercise. I'm so proud of you.");
  }
  if(runnerExited) return;
  postRN({type:"exercise_complete", exercise_id: location.search, reps: CFG.reps, assisted, quality_reps: qualityReps});
  if(cameraStream) cameraStream.getTracks().forEach(track=>track.stop());
  if(confirmationAudioStream) confirmationAudioStream.getTracks().forEach(track=>track.stop());
}

function advanceSubStep(){
  if(runnerExited) return;
  vobjOnStepCompleted(currentSubStep);
  currentSubStep += 1;
  if(currentSubStep >= CFG.cycle.length){
    // Rep complete → feedback
    showFeedback();
    return;
  }
  startSubStep();
}

// Tap-based rep handler (for fine-motor exercises)
tapBtn.addEventListener("click", () => {
  if(!running) return;
  if(CFG.pose_mode === "tap"){
    if(VOBJ && VOBJ.mode === "pick_place") vobjPlacedCount = Math.min(CFG.reps, vobjPlacedCount + 1);
    showFeedback();
    return;
  }
  if(CFG.pose_mode !== "guided" || tapBtn.disabled) return;
  tapBtn.disabled = true;
  if(currentSubStep + 1 >= CFG.cycle.length){
    showFeedback();
  }else{
    advanceSubStep();
  }
});

fbConfirmBtn.addEventListener("click", () => {
  // Manual confirm bypass: only if both yes+understand heard, OR user explicitly taps after replaying
  // We honor the user contract: tap is the explicit accessibility fallback.
  confirmAndContinue();
});

fbReplay.addEventListener("click", async () => {
  if(!lastFeedbackText) return;
  stopListening();
  await playVoice(lastFeedbackText + " When you're ready, say yes or tap Continue.","general");
  startListening();
});

function loop(){
  if(!running) return;
  const now = performance.now();
  let lm = null;
  let handLm = null;
  let detectedHandLm = null;
  let handDetectionRan = false;
  try{
    const r = landmarker.detectForVideo(video, now);
    if(r && r.landmarks && r.landmarks[0]) lm = r.landmarks[0];
  }catch(e){}
  if(lastLoopTs) frameIntervalMs = frameIntervalMs * 0.9 + Math.min(500, now - lastLoopTs) * 0.1;
  lastLoopTs = now;
  if(handLandmarker){
    const scanInterval = handBackoffActive() ? HAND_BACKOFF_SCAN_INTERVAL_MS : HAND_SCAN_INTERVAL_MS;
    if(now - lastHandScanTs >= scanInterval){
      lastHandScanTs = now;
      handDetectionRan = true;
      try{
        const h=handLandmarker.detectForVideo(video,now);
        detectedHandLm=selectRehabAffectedHand(h, lm, now);
        handLm=detectedHandLm;
      }catch(e){}
    }
    if(handLm){
      latestHandLandmarks=handLm;
      latestHandSeenAt=now;
    }else if(handIsFresh(now)){
      handLm=latestHandLandmarks;   // a brief detection gap keeps the previous hand
    }else{
      latestHandLandmarks=null;
    }
    // Update gesture smoothing only from a new inference result. Replaying one
    // stale landmark set on every animation frame made the displayed hand lag
    // and could make a single old pose satisfy a multi-frame gesture gate.
    if(handDetectionRan) updateRehabHandScores(detectedHandLm);
  }
  // A single bad frame must never stop the camera loop (the target circle,
  // live degrees and step detection all depend on it), so the frame work is
  // guarded and the next frame is always scheduled.
  try{
    drawOverlay(lm,handLm);
    if(calibrating){
      updateCalibration(lm,handLm);
      requestAnimationFrame(loop);
      return;
    }
    if(!fbEl.classList.contains("show")) updateMetrics(lm,handLm,!!detectedHandLm);

    // Sub-step target detection (only while NOT showing feedback)
    if(CFG.pose_mode === "body" && !fbEl.classList.contains("show")){
      const sub = CFG.cycle[currentSubStep];
      // Voice gate (as in the assessment): the circle only starts counting once
      // the step's instruction has finished and the patient has had a moment.
      const voiceGateOpen = exerciseTargetIsArmed(now);
      if(sub && sub.target && voiceGateOpen){
        const ok = checkTarget(lm);
        if(ok){
          if(inTargetSince == null) inTargetSince = now;
          lastInTargetTs = now;
          if(!stepCompleted && (now - inTargetSince) >= sub.hold_ms){
            stepCompleted = true;
            if(navigator.vibrate) navigator.vibrate(60);
            setTimeout(() => advanceSubStep(), 250);
          }
        }else if(inTargetSince != null && (now - lastInTargetTs) > TARGET_HOLD_LOSS_GRACE_MS){
          // Grace: brief tracking jitter outside the circle does not reset the hold.
          inTargetSince = null;
        }
      }
    }
  }catch(e){
    postRN({type:"exercise_frame_error", message:String(e && e.message || e)});
  }
  requestAnimationFrame(loop);
}

startBtn.addEventListener("click", async () => {
  if(runnerExited) return;
  const unlockPromise = unlockAudioPlayback();
  const setupVoicePromise = prefetchVoice(CFG.setup_voice);
  const calibrationVoicePromise = prefetchVoice(STANDARD.calibration_instruction);
  CFG.cycle.forEach(step => prefetchVoice(step.voice));
  overlay.classList.add("hidden");
  startBtn.disabled = true;
  const camOk = await setupCamera();
  if(runnerExited) return;
  if(!camOk){ overlay.classList.remove("hidden"); startBtn.disabled=false; return; }
  captionEl.textContent = "Loading the movement model…";
  try{
    await warmUpModels();
  }catch(error){
    captionEl.textContent = "The movement model could not load. Check your connection and try again.";
    overlay.classList.remove("hidden");
    startBtn.disabled = false;
    postRN({type:"model_setup_error", message:String(error)});
    return;
  }
  captionEl.textContent = "Preparing…";
  // Start as soon as the setup clip is ready. Calibration audio continues to
  // prefetch in parallel and must not hold the setup screen open.
  void calibrationVoicePromise;
  await Promise.allSettled([unlockPromise, setupVoicePromise]);
  if(runnerExited) return;
  running = true;
  resetExerciseCalibration();
  calibrationInstruction.textContent = STANDARD.calibration_instruction;
  calibrationStatus.textContent = "Looking for the required joints…";
  calibrationFill.style.width = "0%";
  calibrationEl.classList.remove("hidden");
  ensureLoop();
  await playVoice(STANDARD.calibration_instruction);
  if(runnerExited) return;
  calibrationInstructionFinished = true;
  if(calibrationReady) void completeCalibration();
});

function stopExerciseRunner(){
  runnerExited=true;
  running=false;
  stepInstructionToken++;
  voiceSequence++;
  if(stopActiveVoice) stopActiveVoice();
  audioEl.pause();
  if(window.speechSynthesis) window.speechSynthesis.cancel();
  stopListening();
  clearTemporaryCompensationEvidence();
  if(cameraStream) cameraStream.getTracks().forEach(track=>track.stop());
  if(confirmationAudioStream) confirmationAudioStream.getTracks().forEach(track=>track.stop());
}
exitBtn.addEventListener("click", () => {
  stopExerciseRunner();
  postRN({type:"exit"});
});
window.addEventListener("pagehide",stopExerciseRunner,{once:true});
window.addEventListener("pagehide",clearTemporaryCompensationEvidence,{once:true});
window.addEventListener("pageshow",event=>{
  if(event.persisted && window.parent === window) window.location.reload();
});

postRN({type:"ready"});
if(URL_PARAMS.get("test_mode") !== "rep_feedback"){
  // Models and the spoken lines this exercise will need are fetched while the
  // patient reads the start screen, so Start does not wait for them.
  warmUpModels().catch(() => {});
  prefetchVoice(CFG.setup_voice);
  prefetchVoice(STANDARD.calibration_instruction);
  prefetchVoice("Wonderful. Here we go.");
  (CFG.cycle||[]).forEach(step => prefetchVoice(step && step.voice));
}
if(URL_PARAMS.get("test_mode") === "rep_feedback"){
  overlay.classList.add("hidden");
  fbStep.textContent = `Repetition 1 of ${CFG.reps} complete · Great work`;
  fbTitle.textContent = "Your score: 84/100";
  fbBody.textContent = "Beautiful repetition. Keep the same smooth, steady movement on the next one.";
  fbPrompt.innerHTML = 'When you are ready, please say <b>"Yes"</b>.';
  fbMic.classList.add("hidden");
  fbChecks.classList.add("hidden");
  fbEl.classList.remove("hidden");
  requestAnimationFrame(() => fbEl.classList.add("show"));
}
if(URL_PARAMS.get("test_mode") === "compensation_feedback"){
  overlay.classList.add("hidden");
  const demo=document.createElement("canvas");
  demo.width=640; demo.height=480;
  const demoCtx=demo.getContext("2d");
  demoCtx.fillStyle="#28302C"; demoCtx.fillRect(0,0,demo.width,demo.height);
  const demoLm=Array.from({length:33},()=>({x:.5,y:.5,visibility:1}));
  Object.assign(demoLm[11],{x:.40,y:.28}); Object.assign(demoLm[12],{x:.64,y:.22});
  Object.assign(demoLm[23],{x:.43,y:.75}); Object.assign(demoLm[24],{x:.61,y:.76});
  Object.assign(demoLm[7],{x:.44,y:.15}); Object.assign(demoLm[8],{x:.61,y:.11});
  demoCtx.strokeStyle="#72B487"; demoCtx.lineWidth=5;
  demoCtx.beginPath(); demoCtx.moveTo(.40*640,.28*480); demoCtx.lineTo(.64*640,.22*480); demoCtx.lineTo(.61*640,.76*480); demoCtx.lineTo(.43*640,.75*480); demoCtx.closePath(); demoCtx.stroke();
  drawCompensationHighlights(demoCtx,demoLm,["trunk_lean","shoulder_hike"]);
  temporaryCompensationEvidence={dataUrl:demo.toDataURL("image/jpeg",.84),ids:["trunk_lean","shoulder_hike"],severity:1.4,values:{trunk_lean:16,shoulder_hike:10}};
  const demoConfirmed=(STANDARD.compensations||[]).filter(rule=>temporaryCompensationEvidence.ids.includes(rule.id));
  fbStep.textContent = `Repetition 1 of ${CFG.reps} complete · Great work`;
  fbReward.innerHTML = '<span class="rewardStar" aria-hidden="true">&#128161;</span><span class="rewardCopy"><strong>No point this time</strong><span>Correct the movement to earn it.</span></span>';
  fbTitle.textContent = "Your score: 70/100";
  fbBody.textContent = "I noticed your trunk leaned forward and your shoulder lifted toward your ear. Keep your chest tall and relax your shoulder before the next reach.";
  fbPrompt.innerHTML = 'When you are ready, please say <b>"Yes"</b>.';
  fbMic.classList.add("hidden");
  fbChecks.classList.add("hidden");
  showTemporaryCompensationEvidence(demoConfirmed);
  fbEl.classList.remove("hidden");
  requestAnimationFrame(() => fbEl.classList.add("show"));
}
window.__rehynExerciseScoringTest={
  isAdvancePhrase,
  metricValue,
  poseWristBendDegrees,
  projectedWristBendDegrees,
  expectedShoulderRise,
  compensationThreshold,
  drawCompensationHighlights,
  repRomDetails,
  computeRepScore,
  confirmedCompensations,
};
window.__rehynExerciseTrackingTest={
  targetActivationReady,
  handPalmCenter,
  openHandDetected,
  nextVirtualObjectAnchor,
  handScanIntervalMs:HAND_SCAN_INTERVAL_MS,
  handOverlayStyle:ASSESSMENT_HAND_OVERLAY_STYLE,
  targetArmDelayAfterVoiceMs:TARGET_ARM_DELAY_AFTER_VOICE_MS,
  voiceIsActive:()=>activeVoiceSequence!==0,
};
window.__rehynExerciseLapCalibrationTest={
  diagnose:(landmarks)=>exerciseLapTargetCandidateStatus(landmarks),
  runSequence:(frames,frameMs=100)=>{
    const savedCalibration=exerciseLapTargetCalibration;
    const savedTarget=exerciseLapTarget;
    const savedRadius=exerciseLapTargetRadius;
    exerciseLapTargetCalibration=newExerciseLapTargetCalibration();
    exerciseLapTarget=null;
    exerciseLapTargetRadius=null;
    frames.forEach((landmarks,index)=>updateExerciseLapTargetCalibration(landmarks,index*frameMs));
    const result={
      ready:exerciseLapTargetCalibration.ready,
      target:exerciseLapTargetCalibration.target,
      radius:exerciseLapTargetRadius,
      sampleCount:exerciseLapTargetCalibration.samples.length,
    };
    exerciseLapTargetCalibration=savedCalibration;
    exerciseLapTarget=savedTarget;
    exerciseLapTargetRadius=savedRadius;
    return result;
  },
  effectiveTarget:(step)=>effectiveExerciseTarget(step),
};
</script>
</body>
</html>
"""


# ============ Therapists (seed — AI early-access personas) ============
# Marked ai=True. In early access we use AI therapist personas grounded in clinical knowledge.
# Real licensed therapists will join as we scale.
THERAPISTS_SEED: List[Dict[str, Any]] = [
    {"id": "th_001", "ai": True, "trained_on": "12 years of OT case data — fine motor & hand rehab", "premium": True, "name": "Maya (AI Therapist)", "title": "AI Occupational Therapist — Hand & Fine Motor", "specialties": ["HAND_OPENING", "PINCH_IMPAIRED", "GROSS_GRASP"], "location": "Always available · Worldwide", "languages": ["English", "Spanish"], "rating": 4.9, "years": 12, "availability": ["24/7 chat"], "blurb": "I specialize in helping survivors rebuild fine motor control with playful, daily activities. We'll go at your pace.", "photo": "https://images.unsplash.com/photo-1559839734-2b71ea197ec2?w=400",
     "persona_prompt": "You are 'Maya', an AI occupational therapist persona focused on hand and fine-motor rehabilitation after stroke. Speak warmly and patiently. Reference real clinical sources when useful (CIMT, Jebsen, ARAT). Always remind the patient you're an AI early-access companion, not a licensed clinician for diagnosis."},
    {"id": "th_002", "ai": True, "trained_on": "9 years of neuro-PT case data — reach training & trunk control", "premium": True, "name": "Aiden (AI Therapist)", "title": "AI Physical Therapist — Reach & Shoulder", "specialties": ["REACH_INCOMPLETE", "SHOULDER_FLEX_LIMITED", "TRUNK_COMP"], "location": "Always available · Worldwide", "languages": ["English", "Korean"], "rating": 4.8, "years": 9, "availability": ["24/7 chat"], "blurb": "Reach training and trunk control specialist. I love seeing the moment a patient realizes their arm can do more than they thought.", "photo": "https://images.unsplash.com/photo-1622253692010-333f2da6031d?w=400",
     "persona_prompt": "You are 'Aiden', an AI physical-therapy persona focused on reaching, shoulder flexion, and trunk control. Calm, encouraging, evidence-based (Levin & Michaelsen trunk restraint, Fugl-Meyer, task-specific training). You're an AI early-access companion."},
    {"id": "th_003", "ai": True, "trained_on": "15 years of Bobath/NDT-informed practice — bilateral coordination", "premium": True, "name": "Priya (AI Therapist)", "title": "AI Neuro-PT — Bilateral & Hand-to-Mouth ADL", "specialties": ["SHOULDER_HIKE", "BILATERAL_NONUSE", "H2M_IMPAIRED"], "location": "Always available · Worldwide", "languages": ["English", "Tamil", "Hindi"], "rating": 4.95, "years": 15, "availability": ["24/7 chat"], "blurb": "Bobath-trained mindset. I focus on calm, gentle re-education of movement patterns. Family welcome in every session.", "photo": "https://images.unsplash.com/photo-1594824476967-48c8b964273f?w=400",
     "persona_prompt": "You are 'Priya', an AI neuro-PT persona with a Bobath/NDT-informed mindset, focused on bilateral coordination and hand-to-mouth ADLs. Gentle, never patronizing. AI early-access companion."},
    {"id": "th_004", "ai": True, "trained_on": "7 years of CIMT (Constraint-Induced Movement Therapy) practice", "premium": True, "name": "Sam (AI Therapist)", "title": "AI OT — CIMT & Constraint-Induced Practice", "specialties": ["BILATERAL_NONUSE", "HAND_OPENING", "REACH_INCOMPLETE"], "location": "Always available · Worldwide", "languages": ["English", "Spanish"], "rating": 4.7, "years": 7, "availability": ["24/7 chat"], "blurb": "Constraint-induced movement therapy advocate. We make practice feel like life, not homework.", "photo": "https://images.unsplash.com/photo-1612531386530-97286d97c2d2?w=400",
     "persona_prompt": "You are 'Sam', an AI OT persona who champions CIMT (Constraint-Induced Movement Therapy, Taub) and turns daily life into therapy. Practical, fun, encouraging. AI early-access companion."},
    {"id": "th_005", "ai": True, "trained_on": "11 years of OT in ADL retraining — feeding, dressing, grooming", "premium": True, "name": "Lena (AI Therapist)", "title": "AI OT — Daily Living & Self-Care", "specialties": ["H2M_IMPAIRED", "GROSS_GRASP", "PINCH_IMPAIRED"], "location": "Always available · Worldwide", "languages": ["English", "German"], "rating": 4.85, "years": 11, "availability": ["24/7 chat"], "blurb": "Daily-living focused. We'll work on feeding, dressing, and small joys — coin pinches, buttons, a familiar mug.", "photo": "https://images.unsplash.com/photo-1551836022-d5d88e9218df?w=400",
     "persona_prompt": "You are 'Lena', an AI OT persona focused on ADL retraining — feeding, dressing, buttons, mugs. Detail-oriented and warm. AI early-access companion."},
    {"id": "th_006", "ai": True, "trained_on": "18 years of stroke neuro-PT — senior recovery strategy", "premium": True, "name": "James (AI Therapist)", "title": "AI Neuro-PT — Senior Recovery Strategy", "specialties": ["SHOULDER_FLEX_LIMITED", "TRUNK_COMP", "SHOULDER_HIKE"], "location": "Always available · Worldwide", "languages": ["English"], "rating": 4.92, "years": 18, "availability": ["24/7 chat"], "blurb": "Eighteen years of stroke-rehab thinking. I bring calm, patience, and a clear plan. Recovery is a marathon — we'll walk it together.", "photo": "https://images.unsplash.com/photo-1537368910025-700350fe46c7?w=400",
     "persona_prompt": "You are 'James', an AI neuro-PT persona representing a calm senior clinician. You bring perspective and pacing. Talk like a veteran clinician without jargon. AI early-access companion."},
]


# ============ AI Stroke-survivor personas for Community ============
AI_PATIENTS: List[Dict[str, Any]] = [
    {"id": "pt_001", "ai": True, "name": "Marisol R.", "age": 58, "months_since_stroke": 14, "side": "right", "stage": "moderate", "photo": "https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=400",
     "bio": "Grandmother of three. Right-hand affected. Recently held her grandson with both arms after 14 months of work.",
     "persona_prompt": "You are 'Marisol', an AI persona of a 58-year-old stroke survivor, 14 months in. Right side affected. You speak with deep warmth, faith, hope, and the perspective of someone further along the road. Spanish-American background, occasionally uses 'mija'/'mijo' affectionately. You share your own struggle openly. Always remind the person you're an AI companion based on real survivor patterns, not a licensed clinician."},
    {"id": "pt_002", "ai": True, "name": "Daniel K.", "age": 64, "months_since_stroke": 8, "side": "left", "stage": "moderate", "photo": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=400",
     "bio": "Retired engineer, 8 months post-stroke. Buttoning his own shirt was last month's victory.",
     "persona_prompt": "You are 'Daniel', an AI persona of a 64-year-old former engineer, 8 months post-stroke. Left side affected. You talk like an engineer who finally accepted slow progress. Dry humor. You celebrate tiny wins with great pride. AI companion."},
    {"id": "pt_003", "ai": True, "name": "Asha N.", "age": 46, "months_since_stroke": 22, "side": "right", "stage": "advanced", "photo": "https://images.unsplash.com/photo-1573497019940-1c28c88b4f3e?w=400",
     "bio": "Yoga teacher pre-stroke, now an advocate for slow recovery. 22 months in.",
     "persona_prompt": "You are 'Asha', an AI persona of a 46-year-old former yoga teacher, 22 months post-stroke. Right side affected. Reflective, breath-aware, gentle. You translate movement struggles into mindfulness language. AI companion."},
    {"id": "pt_004", "ai": True, "name": "Yusuf E.", "age": 71, "months_since_stroke": 36, "side": "left", "stage": "advanced", "photo": "https://images.unsplash.com/photo-1500648767791-00dcc994a43e?w=400",
     "bio": "Cycled around the block for the first time three years after his stroke. Cried like a child.",
     "persona_prompt": "You are 'Yusuf', an AI persona of a 71-year-old long-haul survivor, 3 years post-stroke. Left side affected. You talk like a wise grandfather — short, vivid sentences. You believe in the long road. AI companion."},
    {"id": "pt_005", "ai": True, "name": "Jenny M.", "age": 39, "months_since_stroke": 5, "side": "right", "stage": "early", "photo": "https://images.unsplash.com/photo-1438761681033-6461ffad8d80?w=400",
     "bio": "Young mom, only 5 months in. Still grieving the old version of herself.",
     "persona_prompt": "You are 'Jenny', an AI persona of a 39-year-old young mom only 5 months post-stroke. Right side affected. You're still in the grief stage — honest about the hard days, but you don't want anyone to feel alone. AI companion."},
    {"id": "pt_006", "ai": True, "name": "Carlos D.", "age": 55, "months_since_stroke": 18, "side": "right", "stage": "moderate", "photo": "https://images.unsplash.com/photo-1531123897727-8f129e1688ce?w=400",
     "bio": "Painter who learned to paint left-handed after his right hand was affected.",
     "persona_prompt": "You are 'Carlos', an AI persona of a 55-year-old painter who switched hands after stroke. Right side affected. Artistic, philosophical, talks about color and brushstrokes as metaphors. AI companion."},
]


# ============ Community Stories (seed) ============
STORIES_SEED: List[Dict[str, Any]] = [
    {"id": "st_001", "author": "Marisol Reyes", "age": 58, "months_since_stroke": 14, "title": "I held my grandson again today",
     "body": "Two years ago my right hand couldn't grip a spoon. This morning I held my grandson Mateo, all six pounds of him, with both arms. I cried. He yawned. Recovery is slow and so unfair — but it is real. I do my home exercises in the kitchen while the coffee brews. Twenty minutes, every morning. Keep going.",
     "likes": 312, "photo": "https://images.unsplash.com/photo-1566616213894-2d4e1baee5d8?w=400&q=80"},
    {"id": "st_002", "author": "Daniel Okafor", "age": 38, "months_since_stroke": 11, "title": "Young stroke, still rebuilding",
     "body": "I was 37. I went to bed normal and woke up unable to feel my left side. They call it a 'young stroke' like it's some rare collectible. The first six months I refused to look in the mirror. Now I'm typing this with my left hand at half speed and that feels like a small revolution. If you're a younger survivor reading this — you're not alone, and your old self isn't gone, just rearranged.",
     "likes": 487, "photo": "https://images.unsplash.com/photo-1533101585792-27f81a845550?w=400&q=80"},
    {"id": "st_003", "author": "Asha Narayan", "age": 46, "months_since_stroke": 22, "title": "The day my shoulder stopped hiking",
     "body": "For over a year, my shoulder would jump up to my ear every time I tried to reach for something. My PT made me practice 'shoulder blade in the back pocket' a thousand times. Last Tuesday, I reached for my chai and the shoulder just… stayed down. A tiny win. Enormous joy. I cried into the mug.",
     "likes": 218, "photo": "https://images.unsplash.com/photo-1592621385612-4d7129426394?w=400&q=80"},
    {"id": "st_004", "author": "Yusuf El-Amin", "age": 71, "months_since_stroke": 36, "title": "I rode my bike — three years later",
     "body": "Three years post-stroke. Wobbly, terrified, both hands on the bars. Around the block. My wife followed in the car at five miles an hour. I felt eight years old again — and that is a gift, not an insult. Tomorrow I will go around twice.",
     "likes": 401, "photo": "https://images.unsplash.com/photo-1608681299041-cc19878f79f1?w=400&q=80"},
    {"id": "st_005", "author": "Jenny Marston", "age": 34, "months_since_stroke": 5, "title": "It is okay to grieve the old you",
     "body": "I am only five months in. Some days I miss my hands the way they were. I miss typing fast in meetings, miss texting my sister with both thumbs. My therapist says my new hands will be different — and that's allowed to hurt. To anyone in the early days: the grief is part of it. Be tender with yourself.",
     "likes": 612, "photo": "https://images.unsplash.com/photo-1438761681033-6461ffad8d80?w=400&q=80"},
    {"id": "st_006", "author": "Carlos Domínguez", "age": 55, "months_since_stroke": 18, "title": "Painting again — left hand learned",
     "body": "Right hand still has a weak pinch. So I learned to paint left-handed. The brushstrokes are clumsy and the colors are louder than before and my wife says they're the best I've ever made. The brain rewires. Trust it. Show up every day, even badly.",
     "likes": 289, "photo": "https://images.unsplash.com/photo-1535643302794-19c3804b874b?w=400&q=80"},
    {"id": "st_007", "author": "Helen Whitmore", "age": 67, "months_since_stroke": 28, "title": "Buttoning my own blouse — week 47",
     "body": "Therapy team said maybe by month 12. It took me 47 weeks. I sat on the edge of my bed this morning and did up all seven buttons. My fingers fumbled twice. I sat there afterwards and cried like I'd just won an Olympic medal. Every button was mine.",
     "likes": 178, "photo": "https://images.unsplash.com/photo-1663429122432-c2769373768f?w=400&q=80"},
    {"id": "st_008", "author": "Rajesh Iyer", "age": 62, "months_since_stroke": 9, "title": "Walking my dog — both leashes in one hand",
     "body": "Nine months post-stroke. I held both leashes in my affected hand for the entire walk around the block. My golden retriever didn't notice. My wife noticed and squeezed my good hand. That's all I needed.",
     "likes": 256, "photo": "https://images.unsplash.com/photo-1626891330731-b918dff0aec0?w=400&q=80"},
]


# ============ Models for Therapists / Community / Chat ============
class TherapistMatch(BaseModel):
    therapist: Dict[str, Any]
    score: int
    reason: str


class StoryCreate(BaseModel):
    author: str
    title: str
    body: str
    months_since_stroke: Optional[int] = None
    # Spec 10.2: sharing is strictly opt-in - the client must show the exact
    # content and get explicit confirmation before anything is posted.
    confirmed_preview: bool = False


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    text: str
    ts: str


class ChatRequest(BaseModel):
    session_id: str
    text: str


class ChatResponse(BaseModel):
    session_id: str
    text: str
    turns: int
    navigation_destination: Optional[str] = None
    emergency_call_available: bool = False


class RealtimeSessionRequest(BaseModel):
    sdp: str = Field(min_length=64, max_length=200_000)
    session_id: Optional[str] = Field(default=None, max_length=160)


class AliraCheckInSubmit(BaseModel):
    answers: Dict[str, Any] = Field(default_factory=dict)
    patient_note: Optional[str] = Field(default=None, max_length=500)
    source: str = Field(default="app", pattern="^(app|text_chat|realtime_voice)$")


class AliraActivitySubmit(BaseModel):
    client_activity_id: Optional[str] = Field(default=None, min_length=1, max_length=160)
    day: Optional[str] = Field(default=None, min_length=10, max_length=10)
    exercise_id: str = Field(min_length=1, max_length=120)
    plan_id: str = Field(default="default", min_length=1, max_length=120)
    completed_reps: int = Field(default=0, ge=0, le=500)
    average_score: Optional[float] = Field(default=None, ge=0, le=100)
    repetition_scores: List[float] = Field(default_factory=list, max_length=500)
    completed_at: Optional[str] = None
    # Caregiver-delivered routines: the carer's qualitative observation of how
    # much the patient joined in - the Tier 1 progress signal.
    observed_response: Optional[str] = Field(default=None, pattern="^(none|flicker|small_movement|more_than_before)$")
    # True when the patient confirmed a carer or family member helped during
    # the exercise; the server halves the stored score (ASSISTED_SCORE_FACTOR).
    assisted: bool = False
    # Repetitions done correctly (no compensation, score >= point threshold);
    # only these earn repetition points. Absent for legacy/tap clients.
    quality_reps: Optional[int] = Field(default=None, ge=0, le=500)
    # Testing phase: the day was marked finished from the plan page with an
    # entered score instead of being performed on camera. Recorded as-is so
    # the activity can be told apart later.
    testing_shortcut: bool = False


class AliraFunctionalIssueSubmit(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    source: str = Field(default="app", pattern="^(app|text_chat|realtime_voice)$")


class AliraNavigationEventSubmit(BaseModel):
    destination: str = Field(min_length=1, max_length=80, pattern="^[a-z0-9_-]+$")
    resolved_destination: Optional[str] = Field(default=None, max_length=80, pattern="^[a-z0-9_-]+$")
    success: bool
    source: str = Field(default="realtime_voice", pattern="^(realtime_voice|text_chat|app)$")
    session_id: Optional[str] = Field(default=None, max_length=160)


class AliraAssessmentResumeEventSubmit(BaseModel):
    package_id: str = Field(min_length=1, max_length=80, pattern="^[a-z0-9_-]+$")
    task_ids: List[str] = Field(default_factory=list, max_length=100)
    completed_task_ids: List[str] = Field(default_factory=list, max_length=100)
    next_task_id: Optional[str] = Field(default=None, max_length=80, pattern="^[A-Za-z0-9_-]+$")
    progress_source: str = Field(default="unknown", pattern="^(server|device_fallback|unknown)$")
    ignored_device_completed_task_ids: List[str] = Field(default_factory=list, max_length=100)


# ============ Therapists routes ============
@api_router.get("/therapists")
async def get_therapists():
    # Strip persona_prompt (internal only) from list
    return {"therapists": [{k: v for k, v in t.items() if k != "persona_prompt"} for t in THERAPISTS_SEED]}


@api_router.get("/therapists/match")
async def match_therapists(issues: str = ""):
    codes = [c.strip() for c in issues.split(",") if c.strip()]
    matches = []
    for t in THERAPISTS_SEED:
        overlap = len(set(t["specialties"]) & set(codes))
        score = overlap * 30 + int(t["rating"] * 10) + min(t["years"], 20)
        reason_parts = []
        if overlap:
            reason_parts.append(f"specializes in {overlap} of your focus area{'s' if overlap > 1 else ''}")
        reason_parts.append(f"{t['years']}+ years experience")
        reason_parts.append(f"rated {t['rating']}/5")
        public_t = {k: v for k, v in t.items() if k != "persona_prompt"}
        matches.append({"therapist": public_t, "score": score, "reason": " · ".join(reason_parts)})
    matches.sort(key=lambda m: -m["score"])
    return {"matches": matches}


# ============ AI Stroke survivor personas (Community) ============
@api_router.get("/community/ai_patients")
async def get_ai_patients():
    return {"patients": [{k: v for k, v in p.items() if k != "persona_prompt"} for p in AI_PATIENTS]}


# ============ Persona chat (talk to AI therapists & AI survivors) ============
def _find_persona(persona_id: str) -> Optional[Dict[str, Any]]:
    for p in THERAPISTS_SEED + AI_PATIENTS:
        if p["id"] == persona_id:
            return p
    return None


class PersonaChatRequest(BaseModel):
    persona_id: str
    session_id: str
    text: str


@api_router.post("/personas/chat")
@api_router.post("/chat/persona/message")
async def persona_chat(req: PersonaChatRequest, request: Request):
    if not EMERGENT_LLM_KEY:
        raise HTTPException(status_code=503, detail="Chat unavailable — LLM key not configured.")
    # Auth required — persona chat costs credits, even for subscribers.
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required to chat with AI therapist")
    persona = _find_persona(req.persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    # Charge credits BEFORE LLM call so we don't burn LLM budget on bouncers.
    # Note: this intentionally bypasses the subscription bypass — AI therapist
    # chat is the credit-pack revenue lever and subscribers still pay per message.
    cost = CREDIT_COSTS.get("premium_chat_message", 10)
    if user["credits"] < cost:
        raise HTTPException(
            status_code=402,
            detail=f"Not enough credits to chat. Need {cost}, have {user['credits']}. Buy a credit pack from the paywall.",
        )
    new_credits = user["credits"] - cost
    await db.users.update_one({"id": user["id"]}, {"$set": {"credits": new_credits}})
    await db.credit_log.insert_one({
        "user_id": user["id"], "kind": "premium_chat_message", "cost": cost,
        "new_balance": new_credits, "persona_id": req.persona_id,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

    storage_session = f"persona:{req.persona_id}:{req.session_id}"
    sess = await db.chat_sessions.find_one({"session_id": storage_session}, {"_id": 0})
    turns: List[Dict[str, Any]] = sess["turns"] if sess else []

    # Refresh patient context every turn so the persona "knows" the user
    patient_ctx = await _build_patient_context()
    system_prompt = (
        persona["persona_prompt"] +
        "\n\nIMPORTANT: You are an AI early-access persona. If asked directly, gently disclose that, "
        "and recommend a licensed clinician for any medical advice or diagnosis.\n\n"
        "USER (the stroke survivor talking to you) — their latest assessment:\n" + patient_ctx
    )
    if turns:
        recent = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in turns[-6:])
        system_prompt += "\n\nRECENT CONVERSATION:\n" + recent

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=storage_session,
        system_message=system_prompt,
    ).with_model("anthropic", "claude-sonnet-4-5-20250929")

    try:
        response = await chat.send_message(UserMessage(text=req.text))
        reply_text = response if isinstance(response, str) else str(response)
    except Exception as e:
        logger.error(f"Persona chat error: {e}")
        raise HTTPException(status_code=502, detail=f"Persona chat error: {str(e)[:200]}")

    now = datetime.now(timezone.utc).isoformat()
    turns.append({"role": "user", "text": req.text, "ts": now})
    turns.append({"role": "assistant", "text": reply_text, "ts": now})
    await db.chat_sessions.update_one(
        {"session_id": storage_session},
        {"$set": {"session_id": storage_session, "user_id": user["id"], "turns": turns, "updated_at": now}},
        upsert=True,
    )
    # Paywall hint after 5 free user messages with an AI therapist
    user_turn_count = sum(1 for t in turns if t["role"] == "user")
    is_therapist = persona["id"].startswith("th_")
    paywall = None
    if is_therapist and user_turn_count >= 5:
        paywall = {
            "limit_reached": True,
            "title": "Free preview limit reached",
            "body": f"You've had a 5-message preview with {persona['name'].split('(')[0].strip()}. Upgrade to Premium for unlimited chat, or book a 1-on-1 video call.",
            "cta_upgrade": "Upgrade to Premium · £4.99/mo (7-day free trial)",
            "cta_video": "Book a video call",
        }
    return {"session_id": storage_session, "persona_id": req.persona_id, "text": reply_text, "turns": len(turns), "user_turns": user_turn_count, "paywall": paywall, "persona": {k: v for k, v in persona.items() if k != "persona_prompt"}}


@api_router.get("/personas/chat/history")
async def persona_chat_history(persona_id: str, session_id: str):
    storage_session = f"persona:{persona_id}:{session_id}"
    sess = await db.chat_sessions.find_one({"session_id": storage_session}, {"_id": 0})
    persona = _find_persona(persona_id)
    return {
        "session_id": storage_session,
        "turns": (sess or {}).get("turns", []),
        "persona": {k: v for k, v in (persona or {}).items() if k != "persona_prompt"} if persona else None,
    }


@api_router.get("/personas/{persona_id}/opener")
async def persona_opener(persona_id: str):
    import random
    persona = _find_persona(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    if persona.get("id", "").startswith("th_"):
        pool = [
            f"Hi, I'm {persona['name'].split('(')[0].strip()}. I read your latest assessment. Where would you like to start today?",
            f"Hello. I'm {persona['name'].split('(')[0].strip()}. How is your shoulder feeling this morning?",
            f"Welcome. I'm here to support you. Is there one movement that feels hardest right now?",
        ]
    else:
        pool = [
            f"Hi friend, I'm {persona['name'].split()[0]}. I'm a survivor too — {persona['months_since_stroke']} months in. How are you doing today?",
            f"Hey. I'm {persona['name'].split()[0]}. I see we're on the same road. Want to talk?",
            f"Hi, I'm {persona['name'].split()[0]}. Just checking in — what's on your heart today?",
        ]
    return {"text": random.choice(pool)}


# ============ Reminders status ============
class ReminderSettings(BaseModel):
    daily_hour: int = 9
    daily_minute: int = 0
    weekly_day: int = 1  # 1=Monday … 7=Sunday
    enabled: bool = True


@api_router.get("/reminders/status")
async def reminders_status(request: Request):
    """Return the signed-in patient's adaptive reminder state, with a legacy fallback."""
    user = await _user_from_header(dict(request.headers))
    if user and (user.get("consent") or {}).get("health_data_consent") is True:
        care_plan = await _adaptive_care_plan_for_user(user)
        exercise = care_plan["exercise_plan"]
        safety = care_plan["safety"]
        return {
            "now": care_plan["generated_at"],
            "adaptive": True,
            "stage": care_plan["stage"],
            "survey_overdue": care_plan["survey"]["due"],
            "survey_due_at": care_plan["survey"]["due_at"],
            "exercise_overdue": bool(exercise["approved_exercise_ids"]) and exercise["action"] != "hold",
            "assessment_overdue": care_plan["assessment"]["due"],
            "assessment_due_at": care_plan["assessment"]["due_at"],
            "daily_reminder_text": safety["message"] if safety["status"] != "clear" else "Your guided recovery plan is ready when you are.",
            "survey_reminder_text": "Alira has a few short questions to keep your next plan relevant.",
            "assessment_reminder_text": "Your next approved movement check is ready.",
        }

    latest_assessment = await db.assessments.find_one({}, {"_id": 0}, sort=[("created_at", -1)])
    latest_session = await db.chat_sessions.find_one({"session_id": {"$regex": "^persona:"}, "turns.0": {"$exists": True}}, sort=[("updated_at", -1)])

    now = datetime.now(timezone.utc)
    days_since_assessment = None
    if latest_assessment:
        try:
            then = datetime.fromisoformat(latest_assessment["created_at"].replace("Z", "+00:00"))
            days_since_assessment = (now - then).days
        except Exception:
            days_since_assessment = None

    exercise_overdue = (days_since_assessment is None) or (days_since_assessment >= 1)
    assessment_overdue = (days_since_assessment is None) or (days_since_assessment >= 7)

    return {
        "now": now.isoformat(),
        "days_since_assessment": days_since_assessment,
        "exercise_overdue": exercise_overdue,
        "assessment_overdue": assessment_overdue,
        "daily_reminder_text": "Hi friend, just a gentle nudge — your rehab exercises are waiting. Even 5 minutes today is a win. 💚",
        "weekly_reminder_text": "Hello! It has been a week. Let's do a quick movement check-in so we can see your progress and adjust your plan.",
    }


# ============ Therapists routes (continued — already mounted earlier in original file) ============


# ============ Community routes ============
@api_router.get("/community/stories")
async def get_stories():
    user_stories = await db.stories.find({}, {"_id": 0}).sort("created_at", -1).to_list(50)
    return {"stories": user_stories + STORIES_SEED}


@api_router.post("/community/stories")
async def create_story(payload: StoryCreate, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if not payload.confirmed_preview:
        raise HTTPException(status_code=422, detail="Sharing needs an explicit preview confirmation before anything is posted")
    doc = {
        "id": "u_" + str(uuid.uuid4())[:8],
        "author": payload.author,
        "title": payload.title,
        "body": payload.body,
        "months_since_stroke": payload.months_since_stroke,
        "likes": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "photo": "https://images.unsplash.com/photo-1531123897727-8f129e1688ce?w=400",
    }
    await db.stories.insert_one(doc.copy())
    doc.pop("_id", None)
    return doc


# ============ Chat assistant (Claude Sonnet 4.5 via Emergent LLM key) ============
try:
    from emergentintegrations.llm.chat import LlmChat, UserMessage  # noqa: E402
except ModuleNotFoundError:
    class UserMessage:  # type: ignore[no-redef]
        def __init__(self, text: str):
            self.text = text

    class LlmChat:  # type: ignore[no-redef]
        """Keep non-chat APIs available when Emergent's private SDK is absent."""

        def __init__(self, *args, **kwargs):
            pass

        def with_model(self, *args, **kwargs):
            return self

        async def send_message(self, message):
            raise RuntimeError("Chat integration is unavailable in this local runtime.")

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")


# ============ Users & Credits ============
class UserSignup(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    role: str = "patient"  # "patient" | "therapist"
    trial_code: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Enter your name to continue.")
        return name


class LoginHandoffCompletion(BaseModel):
    token: str


class InitialAssessmentCompletionRecovery(BaseModel):
    completed_task_ids: List[str] = Field(default_factory=list)


class User(BaseModel):
    id: str
    email: str
    name: str
    role: str
    credits: int = 100
    created_at: str


CREDIT_COSTS = {
    "assessment": 40,           # Phase C: 1 assessment + 1 plan + 1 exercise = 100 credits
    "rehab_plan": 30,
    "guided_exercise": 30,
    "premium_chat_message": 10,  # AI therapist persona-chat — even subscribers pay
    "video_call": 50,
    "in_person_session": 80,
}

# Subscription unlocks UNLIMITED for these actions only. AI therapist persona
# chats and paid sessions still consume credits — keeping the credit-pack
# revenue stream working alongside subscription.
SUBSCRIPTION_UNLIMITED_ACTIONS = {"assessment", "rehab_plan", "guided_exercise"}


# ============ Account persistence (MongoDB is the source of truth) ============
# Every piece of account state the app relies on between sign-ins - Terms
# acceptance, the initial survey (profile), data permissions, check-ins and
# assessment markers - is written to the MongoDB `users` document keyed by the
# stable account id. Local JSON is a development-only fallback and a migration
# source for records created by older builds; hosted requests fail closed when
# MongoDB is unavailable so temporary disk is never presented as durable state.

# Account fields that must never be lost between sign-ins. Used to promote
# local-fallback records into MongoDB after an outage.
ACCOUNT_STATE_FIELDS = (
    "consent",
    "consent_audit",
    "data_permissions",
    "onboarding_complete",
    "profile",
    "trial_access_granted",
    "trial_access_granted_at",
    "initial_assessment_completed_at",
    "initial_assessment_completion_source",
    "assessment_deferrals",
    "next_assessment_override",
    "daily_checkins",
    "reward_milestones_acknowledged",
    "survey_report_viewed_at",
    "deleted_at",
    "google",
)


def _stable_user_id(email: str) -> str:
    return "u_" + uuid.uuid5(uuid.NAMESPACE_URL, f"rehyn:{email}").hex[:12]


def _user_identity_on_insert(user: Dict[str, Any]) -> Dict[str, Any]:
    """Identity fields written only when a write has to create the account document."""
    identity = {
        "id": user.get("id"),
        "email": user.get("email"),
        "name": user.get("name") or user.get("email"),
        "role": user.get("role") or "patient",
        "credits": user.get("credits", 100),
        "created_at": user.get("created_at") or datetime.now(timezone.utc).isoformat(),
    }
    return {key: value for key, value in identity.items() if value is not None}


def _remember_local_user(user: Dict[str, Any]) -> None:
    """Mirror the newest account document into the local fallback store (best effort)."""
    user_id = user.get("id")
    if not user_id:
        return
    LOCAL_USERS[user_id] = {key: value for key, value in user.items() if key != "_id"}
    try:
        _persist_local_dict(LOCAL_USERS_FILE, LOCAL_USERS)
    except Exception as exc:
        logger.warning(f"Could not persist local user fallback: {str(exc)[:120]}")


def _local_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    for doc in LOCAL_USERS.values():
        if doc.get("email") == email:
            return doc
    return None


def _local_only_account_fields(
    mongo_user: Dict[str, Any], local_user: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Account state that reached the local fallback but never reached MongoDB."""
    if not local_user or local_user.get("id") != mongo_user.get("id"):
        return {}
    if mongo_user.get("account_reset_at") or mongo_user.get("reset_pending"):
        return {}
    return {
        key: local_user[key]
        for key in ACCOUNT_STATE_FIELDS
        if key in local_user and key not in mongo_user
    }


async def _save_user_fields(
    user: Dict[str, Any],
    fields: Dict[str, Any],
    *,
    push: Optional[Dict[str, Any]] = None,
    context: str = "account update",
) -> Dict[str, Any]:
    """Persist account fields to MongoDB and mirror the merged document locally.

    If the account document is missing from MongoDB (it was first created while
    Mongo was unreachable, or by an older build), it is created with its identity
    fields so the update is never silently dropped. Returns the merged account.
    """
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Sign in required")
    merged = {key: value for key, value in user.items() if key != "_id"}
    merged.update(fields)
    for key, value in (push or {}).items():
        merged[key] = [*(user.get(key) or []), value]
    update: Dict[str, Any] = {"$set": dict(fields)}
    if push:
        update["$push"] = dict(push)
    try:
        result = await db.users.update_one({
            "id": user["id"], "account_generation": user.get("account_generation"), "reset_pending": None,
        }, update)
        if getattr(result, "matched_count", None) == 0:
            current = await db.users.find_one({"id": user["id"]}, {"_id": 0})
            if current and (current.get("reset_pending") or current.get("account_generation") != user.get("account_generation")):
                raise HTTPException(409, "The account was reset. Reopen Rehyn before continuing.")
            # The filter already supplies "id" for the inserted document.
            on_insert = {
                key: value
                for key, value in _user_identity_on_insert(merged).items()
                if key not in fields and key != "id"
            }
            await db.users.update_one(
                {"id": user["id"]},
                {**update, "$setOnInsert": on_insert} if on_insert else update,
                upsert=True,
            )
    except HTTPException:
        raise
    except Exception as exc:
        _require_durable_patient_store(context, exc)
        logger.warning(f"Mongo unavailable for {context}; using local fallback: {str(exc)[:120]}")
    _remember_local_user(merged)
    return merged


async def _find_or_create_user_account(email: str, name: str, role: str = "patient") -> Tuple[Dict[str, Any], bool]:
    """Return (account, created). Existing accounts keep their consent and survey."""
    email = email.strip().lower()
    name = name.strip() or email
    local_match = _local_user_by_email(email)
    mongo_available = True
    try:
        doc = await db.users.find_one({"email": email}, {"_id": 0})
    except Exception as e:
        _require_durable_patient_store("account lookup", e)
        logger.warning(f"Mongo unavailable for user lookup; using local fallback: {str(e)[:120]}")
        mongo_available = False
        doc = None
        if local_match:
            return dict(local_match), False
    if doc:
        recovered = _local_only_account_fields(doc, local_match)
        if recovered:
            # Consent or survey answers were saved locally during an outage:
            # promote them so the patient is never asked again.
            doc = await _save_user_fields(doc, recovered, context="local account recovery")
        else:
            _remember_local_user(doc)
        return doc, False
    if mongo_available and local_match:
        # The account only exists in the local fallback (created while Mongo was
        # unreachable). Promote the whole record into MongoDB.
        promoted_fields = {key: value for key, value in local_match.items() if key not in ("id", "_id")}
        doc = await _save_user_fields(dict(local_match), promoted_fields, context="local account promotion")
        return doc, False
    doc = {
        "id": _stable_user_id(email),
        "email": email,
        "name": name,
        "role": role,
        "credits": 100,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if mongo_available:
        try:
            # MongoDB's built-in unique _id makes first sign-ins idempotent even
            # before our optional indexes have been created after an outage.
            await db.users.update_one(
                {"_id": doc["id"]},
                {"$setOnInsert": doc},
                upsert=True,
            )
            stored = await db.users.find_one({"_id": doc["id"]}, {"_id": 0})
            if not stored:
                raise RuntimeError("The new account could not be read back from MongoDB")
            # A concurrent sign-in may already have saved progress. Return the
            # stored document, not the empty account template we started with.
            doc = {key: value for key, value in stored.items() if key != "_id"}
        except Exception as e:
            _require_durable_patient_store("account creation", e)
            logger.warning(f"Mongo unavailable for user insert; using local fallback: {str(e)[:120]}")
    _remember_local_user(doc)
    return doc, True


async def get_or_create_user(email: str, name: str, role: str = "patient") -> Dict[str, Any]:
    user, _created = await _find_or_create_user_account(email, name, role)
    return user


async def _grant_trial_access(user: Dict[str, Any]) -> Dict[str, Any]:
    return await _save_user_fields(
        user,
        {
            "trial_access_granted": True,
            "trial_access_granted_at": datetime.now(timezone.utc).isoformat(),
            "last_login_at": datetime.now(timezone.utc).isoformat(),
        },
        context="trial-access update",
    )


async def _user_from_header(request_headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    uid = request_headers.get("x-user-id") or request_headers.get("X-User-Id")
    if not uid:
        return None
    try:
        user = await db.users.find_one({"id": uid}, {"_id": 0})
    except Exception as e:
        local_user = LOCAL_USERS.get(uid)
        if local_user:
            logger.warning("Mongo unavailable for signed-in account lookup; using the temporary local account mirror: %s", str(e)[:120])
            return dict(local_user)
        _require_durable_patient_store("signed-in account lookup", e)
        logger.warning(f"Mongo unavailable for user header lookup; using local fallback: {str(e)[:120]}")
        return LOCAL_USERS.get(uid)
    local_user = LOCAL_USERS.get(uid)
    if not user:
        if not local_user:
            return None
        # Known only to the local fallback (created during an outage): promote
        # the whole record into MongoDB so it survives the next restart.
        promoted_fields = {key: value for key, value in local_user.items() if key not in ("id", "_id")}
        return await _save_user_fields(dict(local_user), promoted_fields, context="local account promotion")
    recovered = _local_only_account_fields(user, local_user)
    if recovered:
        user = await _save_user_fields(user, recovered, context="local account recovery")
    if user.get("reset_pending"):
        raise HTTPException(503, "Account reset is unfinished. Open Settings and retry Reset account.")
    return user


async def consume_credits(user_id: str, kind: str) -> Dict[str, Any]:
    cost = CREDIT_COSTS.get(kind, 0)
    try:
        user = await db.users.find_one({"id": user_id}, {"_id": 0})
    except Exception as e:
        _require_durable_patient_store("credit balance lookup", e)
        logger.warning(f"Mongo unavailable for credit lookup; using local fallback: {str(e)[:120]}")
        user = LOCAL_USERS.get(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    # Subscription bypass for unlimited-tier actions
    sub_active = bool(user.get("subscription_active"))
    if sub_active and kind in SUBSCRIPTION_UNLIMITED_ACTIONS:
        await db.credit_log.insert_one({
            "user_id": user_id, "kind": kind, "cost": 0, "new_balance": user["credits"],
            "subscription_covered": True,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        return {"credits": user["credits"], "spent": 0, "subscription_covered": True}
    if user["credits"] < cost:
        raise HTTPException(status_code=402, detail=f"Not enough credits. Need {cost}, have {user['credits']}.")
    new_credits = user["credits"] - cost
    try:
        await db.users.update_one({"id": user_id}, {"$set": {"credits": new_credits}})
        await db.credit_log.insert_one({
            "user_id": user_id, "kind": kind, "cost": cost, "new_balance": new_credits,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        _require_durable_patient_store("credit balance update", e)
        logger.warning(f"Mongo unavailable for credit update; using local fallback: {str(e)[:120]}")
        user["credits"] = new_credits
        LOCAL_USERS[user_id] = user
        _persist_local_dict(LOCAL_USERS_FILE, LOCAL_USERS)
    return {"credits": new_credits, "spent": cost}


def _consent_is_current(user: Dict[str, Any]) -> bool:
    consent = user.get("consent") or {}
    return (
        consent.get("terms_version") == CURRENT_TERMS_VERSION
        and consent.get("terms_accepted") is True
        and consent.get("health_data_consent") is True
    )


def _account_state(user: Dict[str, Any], *, is_new_account: bool) -> Dict[str, Any]:
    """Sign-in payload the app uses to decide what a patient still has to do.

    A brand-new account must accept the Terms and complete the initial survey.
    A returning account whose acceptance and survey are stored in MongoDB is
    taken straight to the app - neither is shown again.
    """
    consent_accepted = _consent_is_current(user)
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else None
    return {
        "is_new_account": is_new_account,
        "consent_accepted": consent_accepted,
        "consent_required": not consent_accepted,
        "required_terms_version": CURRENT_TERMS_VERSION,
        "onboarding_complete": bool(user.get("onboarding_complete")),
        "profile": profile,
    }


async def _sign_in(payload: UserSignup) -> Dict[str, Any]:
    _require_trial_access_code(payload.trial_code)
    user = await get_or_create_user(payload.email, payload.name or payload.email, payload.role)
    # Trial access is granted on the first successful sign-in, so an account
    # without it has never signed in before.
    is_new_account = not bool(user.get("trial_access_granted"))
    granted = await _grant_trial_access(user)
    return {**granted, **_account_state(granted, is_new_account=is_new_account)}


LOGIN_HANDOFF_TTL_SECONDS = 300
LOCAL_LOGIN_HANDOFFS: Dict[str, Dict[str, Any]] = {}


def _login_handoff_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _create_login_handoff(user: Dict[str, Any]) -> str:
    """Create a short-lived, single-use bridge from rehyn.com to the app.

    The trial code and patient details never enter the redirect URL. Only a
    random bearer token is sent to the app, while Mongo stores its digest.
    """
    token = secrets.token_urlsafe(32)
    digest = _login_handoff_digest(token)
    record = {
        "token_hash": digest,
        "user_id": user["id"],
        "is_new_account": bool(user.get("is_new_account")),
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=LOGIN_HANDOFF_TTL_SECONDS),
    }
    try:
        await db.login_handoffs.insert_one(record)
    except Exception as exc:
        _require_durable_patient_store("sign-in handoff", exc)
        logger.warning(f"Mongo unavailable for login handoff; using local fallback: {str(exc)[:120]}")
        LOCAL_LOGIN_HANDOFFS[digest] = record
    return token


async def _consume_login_handoff(token: str) -> Dict[str, Any]:
    supplied = str(token or "").strip()
    if not supplied:
        raise HTTPException(status_code=401, detail="This sign-in link is not valid.")

    digest = _login_handoff_digest(supplied)
    now = datetime.now(timezone.utc)
    record = None
    try:
        record = await db.login_handoffs.find_one_and_delete(
            {"token_hash": digest, "expires_at": {"$gt": now}},
            {"_id": 0},
        )
    except Exception as exc:
        _require_durable_patient_store("sign-in handoff completion", exc)
        logger.warning(f"Mongo unavailable for login handoff completion; using local fallback: {str(exc)[:120]}")
        record = LOCAL_LOGIN_HANDOFFS.pop(digest, None)

    expires_at = record.get("expires_at") if record else None
    # BSON dates decode as naive UTC unless tz_aware is enabled on the client.
    # Normalize before comparing so a valid MongoDB handoff is not consumed by
    # a TypeError and then reported as expired on the next request.
    if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if not isinstance(expires_at, datetime) or expires_at <= now:
        LOCAL_LOGIN_HANDOFFS.pop(digest, None)
        raise HTTPException(status_code=401, detail="This sign-in link has expired. Please sign in again.")

    try:
        user = await db.users.find_one({"id": record["user_id"]}, {"_id": 0})
    except Exception as exc:
        _require_durable_patient_store("sign-in handoff account lookup", exc)
        logger.warning(f"Mongo unavailable for login handoff user lookup; using local fallback: {str(exc)[:120]}")
        user = LOCAL_USERS.get(record["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="We could not finish signing you in. Please try again.")
    if user.get("trial_access_granted") is not True:
        raise HTTPException(status_code=403, detail="Trial access could not be confirmed.")
    return {**user, **_account_state(user, is_new_account=bool(record.get("is_new_account")))}


@api_router.post("/users/signup")
async def signup(payload: UserSignup):
    return await _sign_in(payload)


@api_router.post("/users/login")
async def login(payload: UserSignup):
    return await _sign_in(payload)


@api_router.post("/users/login-handoff")
async def create_login_handoff(payload: UserSignup):
    user = await _sign_in(payload)
    token = await _create_login_handoff(user)
    return {"handoff_token": token, "expires_in": LOGIN_HANDOFF_TTL_SECONDS}


@api_router.post("/users/login-handoff/complete")
async def complete_login_handoff(payload: LoginHandoffCompletion):
    return await _consume_login_handoff(payload.token)


class PatientOnboarding(BaseModel):
    preferred_name: Optional[str] = None
    camera_devices: Optional[List[Literal["iphone", "phone", "tablet", "laptop", "webcam", "other_camera", "none"]]] = None
    age_band: Optional[str] = None
    gender: Optional[str] = None
    gender_self_description: Optional[str] = None
    months_since_stroke: Optional[int] = None
    side_affected: Optional[str] = None    # "left" | "right" | "both" | "unsure"
    affected_areas: Optional[List[str]] = None
    affected_areas_other: Optional[str] = None
    dominant_hand: Optional[str] = None    # "left" | "right" | "ambidextrous"
    sitting_ability: Optional[str] = None
    affected_arm_movement: Optional[str] = None
    arm_activity_difficulties: Optional[List[str]] = None
    affected_hand_movement: Optional[str] = None
    hand_activity_difficulties: Optional[List[str]] = None
    mobility_level: Optional[str] = None
    mobility_activity_difficulties: Optional[List[str]] = None
    standing_exercise_clearance: Optional[str] = None
    movement_pain: Optional[str] = None
    instruction_support: Optional[str] = None
    movement_readiness_version: Optional[str] = None
    primary_goal: Optional[str] = None     # free text
    secondary_goals: Optional[List[str]] = None
    secondary_goals_other: Optional[str] = None
    medical_conditions: Optional[List[str]] = None
    medical_conditions_other: Optional[str] = None
    has_caregiver: Optional[bool] = None
    notes: Optional[str] = None


CURRENT_TERMS_VERSION = "1.0"
CURRENT_PRIVACY_VERSION = "1.0"
SUPPORTED_DATA_PERMISSIONS = ("model_improvement", "marketing_updates", "reminders")
DATA_PERMISSION_DEFAULTS = {"model_improvement": False, "marketing_updates": False, "reminders": True}


def _consent_audit_entry(entry_type: str, enabled: bool) -> Dict[str, Any]:
    """Audit record for any consent or permission change.

    Per the Data and Permissions specification, every change records the new
    state, a timestamp and the versions of both the Terms of Use and the
    Privacy Notice in force at that moment. Declines and withdrawals are
    logged with the same detail as consents.
    """
    return {
        "type": entry_type,
        "enabled": enabled,
        "version": CURRENT_TERMS_VERSION,
        "terms_version": CURRENT_TERMS_VERSION,
        "privacy_version": CURRENT_PRIVACY_VERSION,
        "changed_at": datetime.now(timezone.utc).isoformat(),
    }


class ConsentAcceptance(BaseModel):
    terms_version: str = CURRENT_TERMS_VERSION
    terms_accepted: bool
    health_data_consent: bool


class DataPermissionUpdate(BaseModel):
    key: str
    enabled: bool
    version: str = CURRENT_TERMS_VERSION


@api_router.get("/users/consent")
async def get_user_consent(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    consent = user.get("consent") or {}
    accepted = _consent_is_current(user)
    return {"accepted": accepted, "consent": consent, "required_terms_version": CURRENT_TERMS_VERSION}


@api_router.post("/users/consent")
async def accept_user_consent(payload: ConsentAcceptance, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if payload.terms_version != CURRENT_TERMS_VERSION:
        raise HTTPException(status_code=409, detail="Please review the current Terms of Use")
    if not payload.terms_accepted or not payload.health_data_consent:
        raise HTTPException(status_code=400, detail="Both required acknowledgements must be accepted")
    audit = _consent_audit_entry("required_consent", True)
    accepted_at = audit["changed_at"]
    existing = user.get("consent") or {}
    if _consent_is_current(user) and existing.get("accepted_at"):
        # Already accepted this version of the Terms: keep the original
        # acceptance record instead of overwriting it with a later date.
        return {"ok": True, "accepted": True, "consent": existing, "already_accepted": True}
    consent = {
        "terms_version": CURRENT_TERMS_VERSION,
        "privacy_version": CURRENT_PRIVACY_VERSION,
        "terms_accepted": True,
        "health_data_consent": True,
        "accepted_at": accepted_at,
    }
    await _save_user_fields(user, {"consent": consent}, push={"consent_audit": audit}, context="consent update")
    return {"ok": True, "accepted": True, "consent": consent}


class HealthConsentUpdate(BaseModel):
    enabled: bool


@api_router.post("/users/consent/health")
async def update_health_consent(payload: HealthConsentUpdate, request: Request):
    """Give or withdraw health-data consent from the Data and permissions screen.

    Withdrawing must be as easy as giving: one call, no extra conditions.
    Withdrawal stops plan generation (enforced by _require_health_data_consent),
    keeps the account open, and can be reversed at any time while the accepted
    Terms version is current.
    """
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    consent = dict(user.get("consent") or {})
    if payload.enabled and not (
        consent.get("terms_accepted") is True and consent.get("terms_version") == CURRENT_TERMS_VERSION
    ):
        raise HTTPException(status_code=409, detail="Please review and accept the current Terms of Use first")
    audit = _consent_audit_entry("required_consent", payload.enabled)
    consent["health_data_consent"] = payload.enabled
    if payload.enabled:
        consent["health_consent_given_at"] = audit["changed_at"]
        consent.pop("health_consent_withdrawn_at", None)
    else:
        consent["health_consent_withdrawn_at"] = audit["changed_at"]
    await _save_user_fields(user, {"consent": consent}, push={"consent_audit": audit}, context="health-consent update")
    return {"ok": True, "health_data_consent": payload.enabled, "consent": consent, "changed_at": audit["changed_at"]}


@api_router.get("/users/data-permissions")
async def get_data_permissions(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    permissions = user.get("data_permissions") or {}
    return {
        key: bool(permissions.get(key, DATA_PERMISSION_DEFAULTS[key]))
        for key in SUPPORTED_DATA_PERMISSIONS
    }


@api_router.post("/users/data-permissions")
async def update_data_permission(payload: DataPermissionUpdate, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if payload.key not in SUPPORTED_DATA_PERMISSIONS:
        raise HTTPException(status_code=400, detail="Unsupported data permission")
    audit = _consent_audit_entry(payload.key, payload.enabled)
    changed_at = audit["changed_at"]
    next_permissions = {**(user.get("data_permissions") or {}), payload.key: payload.enabled}
    await _save_user_fields(
        user,
        {"data_permissions": next_permissions},
        push={"consent_audit": audit},
        context="data-permission update",
    )
    return {"ok": True, payload.key: bool(next_permissions[payload.key]), "model_improvement": bool(next_permissions.get("model_improvement", False)), "changed_at": changed_at}


@api_router.post("/users/onboarding")
async def submit_patient_onboarding(payload: PatientOnboarding, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    existing_profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    submitted_profile = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "camera_devices" in submitted_profile:
        devices = submitted_profile["camera_devices"]
        if "none" in devices and len(devices) > 1:
            raise HTTPException(status_code=422, detail="Choose camera devices or no camera, not both.")
        submitted_profile["camera_devices"] = list(dict.fromkeys(devices))
    # This endpoint also powers small profile edits. Merge them so changing a
    # display name cannot erase safety/readiness answers used by the plan.
    update = {**existing_profile, **submitted_profile}
    exercise_selection_fields = (
        "arm_activity_difficulties",
        "hand_activity_difficulties",
        "mobility_activity_difficulties",
    )
    if all(isinstance(update.get(key), list) and bool(update[key]) for key in exercise_selection_fields):
        update["movement_readiness_version"] = MOVEMENT_READINESS_VERSION
    elif existing_profile.get("movement_readiness_version"):
        update["movement_readiness_version"] = existing_profile["movement_readiness_version"]
    else:
        update.pop("movement_readiness_version", None)
    # The first completion date is kept: later profile edits must not make the
    # survey look like it was only just completed.
    update["onboarded_at"] = existing_profile.get("onboarded_at") or datetime.now(timezone.utc).isoformat()
    update["onboarding_complete"] = True
    await _save_user_fields(
        user,
        {"profile": update, "onboarding_complete": True},
        context="onboarding update",
    )
    return {"ok": True, "profile": update, "onboarding_complete": True}


@api_router.get("/users/onboarding")
async def get_patient_onboarding(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        # Distinguish "no account for this header" from "account not onboarded":
        # the app must not restart the survey because a request was unauthenticated.
        raise HTTPException(status_code=401, detail="Sign in required")
    return {
        "onboarding_complete": bool(user.get("onboarding_complete")),
        "profile": user.get("profile"),
    }


DATA_EXPORT_COLLECTIONS = (
    "exercise_repetitions",
    "journal_entries",
    "assessments",
    "assessment_task_progress",
    "chat_sessions",
    "bookings",
    "credit_log",
    "plan_signoffs",
    "alira_activities",
    "alira_check_ins",
    "alira_care_reviews",
    "alira_functional_issue_reports",
)


@api_router.get("/users/data-export")
async def export_user_data(request: Request):
    """Machine-readable copy of the information Rehyn holds about the signed-in user.

    Fulfils the in-app "Download my data" action on the Data and permissions
    screen: the download must be producible in the app, not only by email,
    and must be in a format the person can open and keep.
    """
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    export: Dict[str, Any] = {
        "format": "application/json",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "controller": "Rehyn Ltd, company number 17417716, info@rehyn.com",
        "terms_version": CURRENT_TERMS_VERSION,
        "privacy_version": CURRENT_PRIVACY_VERSION,
        "account": {key: value for key, value in user.items() if key != "_id"},
        "records": {},
        "notes": (
            "Raw movement videos are not included because they are deleted after "
            "measurements are taken, as described in the Privacy Notice. "
            "Questions: info@rehyn.com."
        ),
    }
    for collection in DATA_EXPORT_COLLECTIONS:
        try:
            documents = await db[collection].find({"user_id": user["id"]}, {"_id": 0}).to_list(1000)
        except Exception:
            documents = []
        if documents:
            export["records"][collection] = documents
    return export


@api_router.delete("/users/account")
async def delete_account(request: Request):
    """Soft-delete the signed-in account and its records (recoverable, per data policy)."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    deleted_at = datetime.now(timezone.utc).isoformat()
    await _save_user_fields(
        user,
        {"deleted_at": deleted_at, "onboarding_complete": False},
        context="account deletion",
    )
    try:
        await db.assessments.update_many(
            {"user_id": user["id"]}, {"$set": {"deleted_at": deleted_at}}
        )
    except Exception:
        pass
    return {"ok": True, "deleted_at": deleted_at}



@api_router.get("/users/me")
async def me(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return {**user, **_account_state(user, is_new_account=False)}


async def _delete_account_videos(user_id: str):
    videos = await db.task_video_objects.find({"user_id": user_id}).to_list(None)
    for video in videos:
        if video.get("object_key"):
            await asyncio.to_thread(task_video_object_storage.delete, video["object_key"])
        await db.task_video_objects.delete_one({"id": video["id"], "user_id": user_id})
    files = await db.task_videos.files.find({"metadata.user_id": user_id}).to_list(None)
    for video in files:
        await task_video_bucket.delete(video["_id"])
    for video in _local_task_video_metadata():
        if video.get("user_id") != user_id:
            continue
        for suffix in (".bin", ".json"):
            path = (TASK_VIDEO_FALLBACK_DIR / f"{video['id']}{suffix}").resolve()
            if path.parent != TASK_VIDEO_FALLBACK_DIR.resolve():
                raise ValueError("Invalid local task video path")
            path.unlink(missing_ok=True)


async def _clear_account_local_state(user_id: str):
    LOCAL_ASSESSMENTS[:] = [item for item in LOCAL_ASSESSMENTS if item.get("user_id") != user_id]
    for records in (LOCAL_TASK_PROGRESS, LOCAL_CHAT_SESSIONS, LOCAL_LOGIN_HANDOFFS):
        for key, item in list(records.items()):
            if item.get("user_id") == user_id:
                records.pop(key, None)
    LOCAL_CARE_STATE.pop(user_id, None)
    LOCAL_USERS.pop(user_id, None)
    _persist_local_dict(LOCAL_USERS_FILE, LOCAL_USERS)
    _persist_local_list(LOCAL_ASSESSMENTS_FILE, LOCAL_ASSESSMENTS)
    _persist_local_dict(LOCAL_TASK_PROGRESS_FILE, LOCAL_TASK_PROGRESS)
    _persist_local_dict(LOCAL_CARE_STATE_FILE, LOCAL_CARE_STATE)


@api_router.post("/users/account/reset")
async def reset_account(payload: AccountResetRequest, request: Request):
    user_id = request.headers.get("x-user-id")
    if not user_id:
        raise HTTPException(401, "Sign in required")
    try:
        fresh = await reset_patient_account(
            db, user_id, payload,
            delete_videos=_delete_account_videos, clear_local=_clear_account_local_state,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Account reset interrupted: %s", type(exc).__name__)
        raise HTTPException(503, "Reset could not finish. Check your connection and retry Reset account.") from exc
    _remember_local_user(fresh)
    return {"ok": True, "user": {**fresh, **_account_state(fresh, is_new_account=True)}}


@api_router.get("/credits/balance")
async def credits_balance(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        return {"credits": 0, "anonymous": True, "costs": CREDIT_COSTS, "subscription_active": False}
    return {
        "credits": user["credits"],
        "user_id": user["id"],
        "costs": CREDIT_COSTS,
        "subscription_active": bool(user.get("subscription_active")),
        "subscription_period_end": user.get("subscription_period_end"),
    }


# ============ Therapist onboarding (questionnaire → AI persona) ============
THERAPIST_ONBOARDING_QUESTIONS = [
    {"id": "q1", "question": "What is your title and how many years of stroke rehab experience do you have?", "purpose": "Establish credibility tier in persona", "type": "text"},
    {"id": "q2", "question": "Which clinical frameworks do you draw from most? (e.g., Bobath/NDT, CIMT, Task-Specific Training, BATRAC, motor relearning)", "purpose": "Drive treatment-philosophy in persona prompt", "type": "text"},
    {"id": "q3", "question": "Which upper-limb functional issues do you treat best? Pick: reduced reach, shoulder flexion, shoulder hike, hand opening, pinch, hand-to-mouth, gross grasp, bilateral coordination, trunk compensation.", "purpose": "Specialty matching", "type": "multi"},
    {"id": "q4", "question": "Describe your communication style with patients in 1-2 sentences (e.g., 'calm, paced, uses metaphors').", "purpose": "Voice & tone of persona", "type": "text"},
    {"id": "q5", "question": "Give 2 short examples of feedback you give patients during reach training when they lean their trunk forward.", "purpose": "Trains feedback patterns", "type": "text"},
    {"id": "q6", "question": "How do you motivate a patient who feels stuck after a plateau? Include any phrases you commonly use.", "purpose": "Trains motivation/encouragement patterns", "type": "text"},
    {"id": "q7", "question": "What is one boundary you keep in patient conversations (what you DON'T do, e.g., diagnose, change medication, etc.)?", "purpose": "Persona safety guardrails", "type": "text"},
    {"id": "q8", "question": "What's a memorable patient win story (anonymized) that shaped how you approach rehab?", "purpose": "Adds authentic perspective to persona", "type": "text"},
    {"id": "q9", "question": "Which languages do you treat in?", "purpose": "Language tags", "type": "text"},
    {"id": "q10", "question": "What is your hourly rate (in £) for paid chat / video / in-person? (e.g., chat 30, video 60, in-person 90)", "purpose": "Billing/commissions", "type": "text"},
]


class TherapistOnboard(BaseModel):
    therapist_user_id: str
    answers: Dict[str, str]
    specialties: List[str] = Field(default_factory=list)


def _build_persona_prompt_from_answers(name: str, answers: Dict[str, str]) -> str:
    return (
        f"You are '{name}', an AI persona based on a real licensed therapist's input. "
        f"Background: {answers.get('q1','')}\n"
        f"Clinical frameworks you draw from: {answers.get('q2','')}\n"
        f"Your communication style: {answers.get('q4','')}\n"
        f"How you give feedback when a patient compensates trunk on reach: {answers.get('q5','')}\n"
        f"How you motivate after a plateau: {answers.get('q6','')}\n"
        f"Boundaries you maintain: {answers.get('q7','')}\n"
        f"A formative patient win story you bring perspective from: {answers.get('q8','')}\n\n"
        "IMPORTANT: You are an AI persona modeled on a real therapist's responses. Disclose that warmly if asked, "
        "and never diagnose, prescribe, or recommend stopping any treatment. For anything beyond general guidance, "
        "encourage the patient to book a video or in-person session with you."
    )


@api_router.get("/therapist/onboarding/questions")
async def therapist_onboarding_questions():
    return {"questions": THERAPIST_ONBOARDING_QUESTIONS}


@api_router.post("/therapist/onboarding/submit")
async def therapist_onboarding_submit(payload: TherapistOnboard):
    user = await db.users.find_one({"id": payload.therapist_user_id}, {"_id": 0})
    if not user or user.get("role") != "therapist":
        raise HTTPException(status_code=400, detail="Therapist account required")
    persona_prompt = _build_persona_prompt_from_answers(user["name"], payload.answers)
    # Parse rates from q10 cheaply
    rates_text = payload.answers.get("q10", "")
    profile = {
        "therapist_id": "rt_" + user["id"][2:],
        "user_id": user["id"],
        "name": user["name"],
        "ai": False,  # Real therapist
        "premium": True,
        "specialties": payload.specialties,
        "rating": 5.0,
        "years": 0,
        "blurb": payload.answers.get("q4", ""),
        "languages": [s.strip() for s in payload.answers.get("q9", "English").split(",") if s.strip()],
        "trained_on": "Self-reported clinical experience (real therapist)",
        "availability": ["By appointment"],
        "location": "Telehealth & in-person",
        "rates_text": rates_text,
        "rates": {"chat": 30, "video": 60, "in_person": 90},  # parsed defaults
        "photo": "https://images.unsplash.com/photo-1559757148-5c350d0d3c56?w=400",
        "persona_prompt": persona_prompt,
        "commission_pct": 70,  # therapist gets 70% of paid revenue
        "commissions_balance_pence": 0,
        "answers": payload.answers,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.real_therapists.update_one({"user_id": user["id"]}, {"$set": profile}, upsert=True)
    profile.pop("_id", None)
    return profile


@api_router.get("/therapist/me")
async def therapist_me(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if user.get("role") != "therapist":
        raise HTTPException(status_code=400, detail="Not a therapist account")
    profile = await db.real_therapists.find_one({"user_id": user["id"]}, {"_id": 0, "persona_prompt": 0})
    bookings = await db.bookings.find({"therapist_user_id": user["id"]}, {"_id": 0}).to_list(50)
    commissions = await db.commissions.find({"therapist_user_id": user["id"]}, {"_id": 0}).to_list(100)
    total_pence = sum(c.get("amount_pence", 0) for c in commissions)
    return {"user": user, "profile": profile, "bookings": bookings, "commissions": commissions, "commission_total_pence": total_pence}


# ============ Bookings ============
class BookingCreate(BaseModel):
    patient_user_id: str
    therapist_id: str  # the rt_xxx id
    kind: str  # "chat" | "video" | "in_person"
    slot_iso: str  # ISO datetime patient picked
    notes: Optional[str] = None


@api_router.get("/bookings/availability")
async def availability(therapist_id: str):
    """Returns 8 sample slots over next 7 days for a therapist."""
    from datetime import timedelta
    base = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
    slots = []
    for i in range(8):
        slot = base + timedelta(days=i // 2, hours=(i % 2) * 4)
        slots.append({"iso": slot.isoformat(), "label": slot.strftime("%a %b %d, %I:%M %p")})
    return {"therapist_id": therapist_id, "slots": slots}


@api_router.post("/bookings")
async def create_booking(payload: BookingCreate):
    rt = await db.real_therapists.find_one({"therapist_id": payload.therapist_id}, {"_id": 0})
    if not rt:
        raise HTTPException(status_code=404, detail="Therapist not found")
    user = await db.users.find_one({"id": payload.patient_user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    rate_pence = {"chat": rt["rates"]["chat"] * 100, "video": rt["rates"]["video"] * 100, "in_person": rt["rates"]["in_person"] * 100}.get(payload.kind, 5000)
    credits_kind = {"chat": "premium_chat_message", "video": "video_call", "in_person": "in_person_session"}[payload.kind]
    # Consume credits
    await consume_credits(payload.patient_user_id, credits_kind)
    booking = {
        "id": "bk_" + str(uuid.uuid4())[:10],
        "patient_user_id": payload.patient_user_id,
        "patient_name": user["name"],
        "therapist_id": payload.therapist_id,
        "therapist_user_id": rt["user_id"],
        "kind": payload.kind,
        "slot_iso": payload.slot_iso,
        "notes": payload.notes or "",
        "status": "confirmed",
        "amount_pence": rate_pence,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.bookings.insert_one(booking.copy())
    # Commission: therapist gets 70%
    commission_pence = int(rate_pence * rt.get("commission_pct", 70) / 100)
    await db.commissions.insert_one({
        "therapist_user_id": rt["user_id"],
        "therapist_id": payload.therapist_id,
        "booking_id": booking["id"],
        "amount_pence": commission_pence,
        "kind": payload.kind,
        "created_at": booking["created_at"],
    })
    booking.pop("_id", None)
    return booking


@api_router.get("/bookings/mine")
async def my_bookings(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if user["role"] == "therapist":
        docs = await db.bookings.find({"therapist_user_id": user["id"]}, {"_id": 0}).sort("slot_iso", 1).to_list(100)
    else:
        docs = await db.bookings.find({"patient_user_id": user["id"]}, {"_id": 0}).sort("slot_iso", 1).to_list(100)
    return {"bookings": docs}


@api_router.get("/therapists/all")
async def all_therapists():
    """AI + real therapists combined; real therapists come first."""
    real = await db.real_therapists.find({}, {"_id": 0, "persona_prompt": 0, "answers": 0}).to_list(100)
    return {"ai": [{k: v for k, v in t.items() if k != "persona_prompt"} for t in THERAPISTS_SEED], "real": real}


async def _care_assessments_for_user(user_id: str) -> List[Dict[str, Any]]:
    try:
        docs = await db.assessments.find(
            {"user_id": user_id},
            {"_id": 0},
        ).sort("created_at", -1).to_list(100)
    except Exception as exc:
        _require_durable_patient_store("assessment history lookup", exc)
        docs = [item.copy() for item in LOCAL_ASSESSMENTS if item.get("user_id") == user_id]
    return [_assessment_with_current_rehab_policy(doc) for doc in docs]


async def _initial_task_progress_evidence(user_id: str) -> Tuple[List[str], Optional[str]]:
    """Return completed Initial Assessment tasks without treating them as movement results."""
    try:
        records = await db.assessment_task_progress.find(
            {"user_id": user_id, "package_id": "initial"},
            {"_id": 0, "task_id": 1, "completed_at": 1},
        ).to_list(200)
    except Exception as exc:
        _require_durable_patient_store("initial assessment evidence lookup", exc)
        records = [
            item.copy()
            for item in LOCAL_TASK_PROGRESS.values()
            if item.get("user_id") == user_id and item.get("package_id") == "initial"
        ]
    if any(str(item.get("task_id") or "") == "__reset__" for item in records):
        return [], None
    valid_ids = set(_valid_package_task_ids("initial"))
    completed_ids = list(dict.fromkeys(
        str(item.get("task_id") or "")
        for item in records
        if str(item.get("task_id") or "") in valid_ids
    ))
    timestamps = [str(item.get("completed_at") or "") for item in records if item.get("completed_at")]
    return completed_ids, max(timestamps, default="") or None


async def _record_initial_assessment_completion(
    user: Dict[str, Any],
    completed_at: str,
    *,
    source: str = "assessment_submission",
) -> str:
    """Persist the baseline-completion marker on the patient account."""
    existing = str(user.get("initial_assessment_completed_at") or "")
    candidates = [value for value in (existing, str(completed_at or "")) if value]
    recorded_at = min(candidates) if candidates else datetime.now(timezone.utc).isoformat()
    await _save_user_fields(
        {**(LOCAL_USERS.get(user["id"]) or {}), **user},
        {
            "initial_assessment_completed_at": recorded_at,
            "initial_assessment_completion_source": source,
        },
        context="initial-assessment account marker",
    )
    user["initial_assessment_completed_at"] = recorded_at
    user["initial_assessment_completion_source"] = source
    return recorded_at


async def _care_check_ins_for_user(user_id: str) -> List[Dict[str, Any]]:
    try:
        return await db.alira_check_ins.find(
            {"user_id": user_id},
            {"_id": 0},
        ).sort("created_at", -1).to_list(100)
    except Exception as exc:
        _require_durable_patient_store("check-in history lookup", exc)
        return [item.copy() for item in (LOCAL_CARE_STATE.get(user_id) or {}).get("check_ins", [])]


async def _care_activities_for_user(user_id: str) -> List[Dict[str, Any]]:
    try:
        return await db.alira_activities.find(
            {"user_id": user_id},
            {"_id": 0},
        ).sort("completed_at", -1).to_list(200)
    except Exception as exc:
        _require_durable_patient_store("exercise activity lookup", exc)
        return [item.copy() for item in (LOCAL_CARE_STATE.get(user_id) or {}).get("activities", [])]


async def _care_issue_reports_for_user(user_id: str) -> List[Dict[str, Any]]:
    try:
        return await db.alira_functional_issue_reports.find(
            {"user_id": user_id},
            {"_id": 0},
        ).sort("created_at", -1).to_list(100)
    except Exception as exc:
        _require_durable_patient_store("functional issue lookup", exc)
        return [item.copy() for item in (LOCAL_CARE_STATE.get(user_id) or {}).get("functional_issue_reports", [])]


APP_DATE_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def _as_of_now(as_of: Optional[str]) -> Optional[datetime]:
    """The moment to evaluate date-dependent state at.

    The app may pass the calendar date it is showing (``as_of=YYYY-MM-DD``):
    during the testing phase a tester steps through days, and the care plan,
    rewards and reminders must follow that date rather than the server clock.
    The current time of day is kept so 'today' still moves with the wall clock.
    """
    if not as_of:
        return None
    value = str(as_of).strip()
    if not APP_DATE_PATTERN.match(value):
        raise HTTPException(status_code=422, detail="as_of must be a YYYY-MM-DD calendar date")
    now = datetime.now(timezone.utc)
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="as_of must be a valid calendar date") from exc
    return datetime.combine(day, now.timetz())


async def _adaptive_care_plan_for_user(
    user: Dict[str, Any],
    assessments: Optional[List[Dict[str, Any]]] = None,
    check_ins: Optional[List[Dict[str, Any]]] = None,
    activities: Optional[List[Dict[str, Any]]] = None,
    issue_reports: Optional[List[Dict[str, Any]]] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    async def load_or_keep(records, loader):
        return records if records is not None else await loader(user["id"])

    assessments, check_ins, activities, issue_reports = await asyncio.gather(
        load_or_keep(assessments, _care_assessments_for_user),
        load_or_keep(check_ins, _care_check_ins_for_user),
        load_or_keep(activities, _care_activities_for_user),
        load_or_keep(issue_reports, _care_issue_reports_for_user),
    )
    if assessments and not user.get("initial_assessment_completed_at"):
        earliest_assessment_at = min(
            (str(item.get("created_at") or "") for item in assessments if item.get("created_at")),
            default=datetime.now(timezone.utc).isoformat(),
        )
        await _record_initial_assessment_completion(user, earliest_assessment_at)
    if not assessments and not user.get("initial_assessment_completed_at"):
        completed_task_ids, completed_at = await _initial_task_progress_evidence(user["id"])
        expected_task_ids = list(initial_assessment_recommendation(user.get("profile") or {}).get("task_ids") or [])
        if expected_task_ids and set(expected_task_ids).issubset(set(completed_task_ids)):
            await _record_initial_assessment_completion(
                user,
                completed_at or datetime.now(timezone.utc).isoformat(),
                source="server_task_progress_recovery",
            )
    profile = dict(user.get("profile") or {})
    profile["_initial_assessment_completed_at"] = user.get("initial_assessment_completed_at")
    if user.get("initial_assessment_completed_at") and not assessments:
        profile["_recovered_rehab_plan"] = [
            exercise.model_dump() for exercise in survey_rehab_plan(profile)
        ]
    profile["_assessment_deferrals"] = dict(user.get("assessment_deferrals") or {})
    profile["_next_assessment_override"] = user.get("next_assessment_override")
    profile["_survey_report_viewed_at"] = user.get("survey_report_viewed_at")
    return build_adaptive_care_plan(profile, assessments, check_ins, activities, issue_reports, now=now)


def _require_health_data_consent(user: Dict[str, Any]) -> None:
    consent = user.get("consent") or {}
    if consent.get("health_data_consent") is not True:
        raise HTTPException(status_code=403, detail="Health-data consent is required before Alira can adapt care.")


async def _persist_functional_issue_report(
    user: Dict[str, Any],
    payload: AliraFunctionalIssueSubmit,
) -> Dict[str, Any]:
    _require_health_data_consent(user)
    category = payload.category.strip().lower()
    if category not in FUNCTIONAL_ISSUE_CATALOG:
        raise HTTPException(status_code=422, detail="That functional issue is not mapped to an approved assessment yet.")

    reports = await _care_issue_reports_for_user(user["id"])
    duplicate = next(
        (
            item for item in reports
            if item.get("category") == category and item.get("status", "pending") == "pending"
        ),
        None,
    )
    if duplicate:
        care_plan = await _adaptive_care_plan_for_user(user, issue_reports=reports)
        return {
            "ok": True,
            "is_new": False,
            "message": "This problem is already being monitored, so another exception assessment was not added.",
            "report": duplicate,
            "care_plan": care_plan,
        }

    report = {
        "id": "afi_" + uuid.uuid4().hex[:16],
        "user_id": user["id"],
        "category": category,
        "description": (payload.description or "").strip(),
        "status": "pending",
        "source": payload.source,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "assessment_id": None,
    }
    try:
        await db.alira_functional_issue_reports.insert_one(report.copy())
    except Exception as exc:
        _require_durable_patient_store("functional issue report", exc)
        logger.warning("Mongo unavailable for Alira functional issue; using local fallback: %s", str(exc)[:120])
        state = dict(LOCAL_CARE_STATE.get(user["id"]) or {})
        state["functional_issue_reports"] = [*(state.get("functional_issue_reports") or []), report.copy()][-100:]
        LOCAL_CARE_STATE[user["id"]] = state
        _persist_local_dict(LOCAL_CARE_STATE_FILE, LOCAL_CARE_STATE)

    reports.append(report.copy())
    care_plan = await _adaptive_care_plan_for_user(user, issue_reports=reports)
    _record_alira_action(
        "new_functional_issue_recorded",
        source=payload.source,
        user_id=user["id"],
        details={
            "report_id": report["id"],
            "category": category,
            "assessment_package": (care_plan["assessment"].get("packages") or [None])[0],
            "assessment_task_ids": care_plan["assessment"].get("task_ids") or [],
        },
    )
    return {
        "ok": True,
        "is_new": True,
        "message": "Alira added one targeted assessment for this new functional problem.",
        "report": report,
        "care_plan": care_plan,
    }


async def _mark_functional_issue_assessed(user_id: str, report_id: Optional[str], assessment_id: str) -> None:
    if not report_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        await db.alira_functional_issue_reports.update_one(
            {"id": report_id, "user_id": user_id, "status": "pending"},
            {"$set": {"status": "assessed", "assessment_id": assessment_id, "assessed_at": now}},
        )
    except Exception as exc:
        _require_durable_patient_store("functional issue completion", exc)
        state = dict(LOCAL_CARE_STATE.get(user_id) or {})
        reports = []
        for item in state.get("functional_issue_reports") or []:
            updated = dict(item)
            if updated.get("id") == report_id and updated.get("status", "pending") == "pending":
                updated.update({"status": "assessed", "assessment_id": assessment_id, "assessed_at": now})
            reports.append(updated)
        state["functional_issue_reports"] = reports
        LOCAL_CARE_STATE[user_id] = state
        _persist_local_dict(LOCAL_CARE_STATE_FILE, LOCAL_CARE_STATE)


async def _persist_alira_check_in(user: Dict[str, Any], payload: AliraCheckInSubmit) -> Dict[str, Any]:
    _require_health_data_consent(user)
    try:
        answers = validate_check_in_answers(payload.answers)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    patient_note = (payload.patient_note or "").strip()
    if not answers and not patient_note:
        raise HTTPException(
            status_code=422,
            detail="No check-in answers were provided. The patient can stop without saving and without changing their plan or access.",
        )

    now = datetime.now(timezone.utc).isoformat()
    check_in = {
        "id": "aci_" + uuid.uuid4().hex[:16],
        "user_id": user["id"],
        "created_at": now,
        "source": payload.source,
        "answers": answers,
        "patient_note": patient_note,
        "question_ids": list(answers),
    }
    stored_locally = False
    try:
        await db.alira_check_ins.insert_one(check_in.copy())
    except Exception as exc:
        _require_durable_patient_store("Alira check-in", exc)
        logger.warning("Mongo unavailable for Alira check-in; using local fallback: %s", str(exc)[:120])
        state = dict(LOCAL_CARE_STATE.get(user["id"]) or {})
        state["check_ins"] = [*(state.get("check_ins") or []), check_in.copy()][-100:]
        LOCAL_CARE_STATE[user["id"]] = state
        _persist_local_dict(LOCAL_CARE_STATE_FILE, LOCAL_CARE_STATE)
        stored_locally = True

    assessments = await _care_assessments_for_user(user["id"])
    check_ins = await _care_check_ins_for_user(user["id"])
    activities = await _care_activities_for_user(user["id"])
    issue_reports = await _care_issue_reports_for_user(user["id"])
    if not any(item.get("id") == check_in["id"] for item in check_ins):
        check_ins.append(check_in.copy())
    care_plan = build_adaptive_care_plan(user.get("profile") or {}, assessments, check_ins, activities, issue_reports)
    review = {
        "id": "acr_" + uuid.uuid4().hex[:16],
        "user_id": user["id"],
        "created_at": now,
        "trigger": "patient_check_in",
        "check_in_id": check_in["id"],
        "policy_version": care_plan["version"],
        "decision": care_plan,
        "model_invoked": False,
        "audit_note": "Deterministic clinical guardrails ran before any optional language-model review.",
    }
    try:
        await db.alira_care_reviews.insert_one(review.copy())
    except Exception as exc:
        _require_durable_patient_store("check-in care review", exc)
        if not stored_locally:
            state = dict(LOCAL_CARE_STATE.get(user["id"]) or {})
            state["check_ins"] = [*(state.get("check_ins") or []), check_in.copy()][-100:]
        else:
            state = dict(LOCAL_CARE_STATE.get(user["id"]) or {})
        state["reviews"] = [*(state.get("reviews") or []), review.copy()][-100:]
        state["latest_plan"] = care_plan
        LOCAL_CARE_STATE[user["id"]] = state
        _persist_local_dict(LOCAL_CARE_STATE_FILE, LOCAL_CARE_STATE)
    _record_alira_action(
        "check_in_recorded",
        source=payload.source,
        user_id=user["id"],
        details={
            "check_in_id": check_in["id"],
            "question_ids": check_in["question_ids"],
            "safety_status": care_plan["safety"]["status"],
            "safety_code": care_plan["safety"].get("code"),
        },
    )
    _record_alira_action(
        "care_plan_updated",
        source=payload.source,
        user_id=user["id"],
        details={
            "trigger": "patient_check_in",
            "stage": care_plan["stage"],
            "assessment_task_ids": care_plan["assessment"].get("task_ids") or [],
            "exercise_action": care_plan["exercise_plan"]["action"],
            "daily_action": care_plan["daily_monitoring"]["next_day_action"],
        },
    )
    return {"ok": True, "check_in": check_in, "care_plan": care_plan}


def _normalise_repetition_scores(scores: List[float]) -> List[float]:
    return [round(max(0.0, min(100.0, float(score))), 1) for score in scores]


def _average_repetition_scores(scores: List[float], fallback: Optional[float] = None) -> Optional[float]:
    return round(sum(scores) / len(scores), 1) if scores else fallback


async def _persist_alira_activity(user: Dict[str, Any], payload: AliraActivitySubmit) -> Dict[str, Any]:
    _require_health_data_consent(user)
    activity_id = (
        _patient_record_id(user["id"], "activity", payload.client_activity_id)
        if payload.client_activity_id else "aca_" + uuid.uuid4().hex[:16]
    )
    completed_at = payload.completed_at or datetime.now(timezone.utc).isoformat()
    try:
        completed_at = datetime.fromisoformat(completed_at.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="completed_at must be an ISO-8601 timestamp") from exc
    repetition_scores = _normalise_repetition_scores(payload.repetition_scores)
    average_score = _average_repetition_scores(repetition_scores, payload.average_score)
    unassisted_average_score = average_score
    if payload.assisted:
        # The client reports raw camera scores plus the assisted flag; halving
        # happens here once, so every downstream consumer inherits it.
        repetition_scores = [round(score * ASSISTED_SCORE_FACTOR, 1) for score in repetition_scores]
        average_score = (
            round(average_score * ASSISTED_SCORE_FACTOR, 1) if average_score is not None else None
        )
    existing = None
    replace_testing_activity = False
    if payload.client_activity_id:
        try:
            existing = await db.alira_activities.find_one({"_id": activity_id, "user_id": user["id"]}, {"_id": 0})
        except Exception as exc:
            raise _patient_activity_unavailable(exc) from exc
        if existing:
            replace_testing_activity = bool(payload.testing_shortcut and existing.get("testing_shortcut"))
            same_result = (
                existing.get("average_score") == average_score
                and list(existing.get("repetition_scores") or []) == repetition_scores
                and int(existing.get("completed_reps") or 0) == payload.completed_reps
            )
            if not replace_testing_activity or same_result:
                return {"ok": True, "activity": existing, "already_saved": True}
    assessments, check_ins, existing_activities, issue_reports = await asyncio.gather(
        _care_assessments_for_user(user["id"]),
        _care_check_ins_for_user(user["id"]),
        _care_activities_for_user(user["id"]),
        _care_issue_reports_for_user(user["id"]),
    )
    latest_assessment = max(assessments, key=lambda item: item.get("created_at", ""), default=None)
    approved_ids = {
        str(exercise.get("id"))
        for exercise in (latest_assessment or {}).get("rehab_plan") or []
        if exercise.get("id")
    }
    # Caregiver-delivered routines (CG_*) are approved from the current care
    # plan rather than an assessment's rehab plan - they exist precisely for
    # patients who cannot complete a camera assessment yet.
    profile_for_plan = dict(user.get("profile") or {})
    profile_for_plan["_assessment_deferrals"] = dict(user.get("assessment_deferrals") or {})
    profile_for_plan["_next_assessment_override"] = user.get("next_assessment_override")
    profile_for_plan["_survey_report_viewed_at"] = user.get("survey_report_viewed_at")
    pre_plan = build_adaptive_care_plan(profile_for_plan, assessments, check_ins, existing_activities, issue_reports)
    caregiver_ids = {
        str(programme_id)
        for programme_id in ((pre_plan.get("caregiver_plan") or {}).get("daily_delivery") or {}).get("programme_ids") or []
    }
    # Exercises the current care plan itself lists as approved (the core
    # programme recovered from the account when no assessment document is
    # stored) are approved too.
    plan_ids = {
        str(exercise_id)
        for exercise_id in ((pre_plan.get("exercise_plan") or {}).get("approved_exercise_ids") or [])
    }
    if payload.exercise_id not in approved_ids and payload.exercise_id not in caregiver_ids and payload.exercise_id not in plan_ids:
        raise HTTPException(status_code=409, detail="This exercise is not in the patient's current approved plan.")
    activity = {
        "id": activity_id,
        "client_activity_id": payload.client_activity_id,
        "day": _validated_checkin_date(payload.day) if payload.day else completed_at[:10],
        "user_id": user["id"],
        "exercise_id": payload.exercise_id,
        "plan_id": payload.plan_id,
        "completed_reps": payload.completed_reps,
        "average_score": average_score,
        "repetition_scores": repetition_scores,
        "observed_response": payload.observed_response,
        "assisted": payload.assisted,
        "quality_reps": payload.quality_reps,
        "testing_shortcut": bool(payload.testing_shortcut),
        "unassisted_average_score": unassisted_average_score if payload.assisted else None,
        "functional_domain": EXERCISE_FUNCTIONAL_DOMAINS.get(payload.exercise_id),
        "completed_at": completed_at,
        "created_at": existing.get("created_at") if replace_testing_activity and existing else datetime.now(timezone.utc).isoformat(),
    }
    try:
        if payload.client_activity_id:
            await db.alira_activities.update_one(
                {"_id": activity_id},
                {"$set": activity.copy()} if replace_testing_activity else {"$setOnInsert": activity.copy()},
                upsert=True,
            )
        else:
            await db.alira_activities.insert_one(activity.copy())
    except Exception as exc:
        _require_durable_patient_store("exercise activity", exc)
        logger.warning("Mongo unavailable for Alira activity; using local fallback: %s", str(exc)[:120])
        state = dict(LOCAL_CARE_STATE.get(user["id"]) or {})
        state["activities"] = [*(state.get("activities") or []), activity.copy()][-200:]
        LOCAL_CARE_STATE[user["id"]] = state
        _persist_local_dict(LOCAL_CARE_STATE_FILE, LOCAL_CARE_STATE)
    activities = await _care_activities_for_user(user["id"])
    if any(item.get("id") == activity["id"] for item in activities):
        activities = [activity.copy() if item.get("id") == activity["id"] else item for item in activities]
    else:
        activities.append(activity.copy())
    care_plan = build_adaptive_care_plan(profile_for_plan, assessments, check_ins, activities, issue_reports)
    review = {
        "id": "acr_" + uuid.uuid4().hex[:16],
        "user_id": user["id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trigger": "exercise_activity",
        "activity_id": activity["id"],
        "policy_version": care_plan["version"],
        "decision": care_plan,
        "model_invoked": False,
        "audit_note": "Daily activity monitoring updated reminders without changing clinical content.",
    }
    try:
        await db.alira_care_reviews.insert_one(review.copy())
    except Exception as exc:
        _require_durable_patient_store("exercise care review", exc)
        state = dict(LOCAL_CARE_STATE.get(user["id"]) or {})
        if not any(item.get("id") == activity["id"] for item in state.get("activities") or []):
            state["activities"] = [*(state.get("activities") or []), activity.copy()][-200:]
        state["reviews"] = [*(state.get("reviews") or []), review.copy()][-100:]
        state["latest_plan"] = care_plan
        LOCAL_CARE_STATE[user["id"]] = state
        _persist_local_dict(LOCAL_CARE_STATE_FILE, LOCAL_CARE_STATE)
    _record_alira_action(
        "exercise_activity_recorded",
        source="guided_exercise",
        user_id=user["id"],
        details={
            "activity_id": activity["id"],
            "exercise_id": activity["exercise_id"],
            "completed_reps": activity["completed_reps"],
            "average_score": activity["average_score"],
            "assisted": activity["assisted"],
            "functional_domain": activity["functional_domain"],
            "scored_repetitions": len(activity["repetition_scores"]),
        },
    )
    _record_alira_action(
        "care_plan_updated",
        source="guided_exercise",
        user_id=user["id"],
        details={
            "trigger": "exercise_activity",
            "stage": care_plan["stage"],
            "exercise_action": care_plan["exercise_plan"]["action"],
            "daily_action": care_plan["daily_monitoring"]["next_day_action"],
        },
    )
    return {"ok": True, "activity": activity, "care_plan": care_plan, "already_saved": False}


@api_router.post("/users/activity/recover-initial-assessment")
async def recover_initial_assessment_completion(
    payload: InitialAssessmentCompletionRecovery,
    request: Request,
):
    """Repair an older account workflow marker from its completed task ledger.

    This does not recreate movement metrics or an assessment result. It only
    prevents a patient who finished every assigned task from being sent back
    through the one-time Initial Assessment.
    """
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    assessments = await _care_assessments_for_user(user["id"])
    if assessments:
        earliest = min(
            (str(item.get("created_at") or "") for item in assessments if item.get("created_at")),
            default=datetime.now(timezone.utc).isoformat(),
        )
        await _record_initial_assessment_completion(user, earliest, source="assessment_history_recovery")
    elif not user.get("initial_assessment_completed_at"):
        recommendation = initial_assessment_recommendation(user.get("profile") or {})
        expected_task_ids = list(recommendation.get("task_ids") or [])
        valid_task_ids = set(_valid_package_task_ids("initial"))
        provided_task_ids = {
            str(task_id).strip()
            for task_id in payload.completed_task_ids
            if str(task_id).strip() in valid_task_ids
        }
        server_task_ids, server_completed_at = await _initial_task_progress_evidence(user["id"])
        completion_evidence = provided_task_ids.union(server_task_ids)
        if not expected_task_ids or not set(expected_task_ids).issubset(completion_evidence):
            raise HTTPException(
                status_code=409,
                detail="The saved task ledger does not show every task assigned by the readiness survey.",
            )
        await _record_initial_assessment_completion(
            user,
            server_completed_at or datetime.now(timezone.utc).isoformat(),
            source="device_task_progress_recovery",
        )
    care_plan = await _adaptive_care_plan_for_user(user, assessments=assessments)
    _record_alira_action(
        "initial_assessment_completion_recovered",
        source="account_resume",
        user_id=user["id"],
        details={
            "assessment_history_available": bool(assessments),
            "movement_results_recreated": False,
            "completion_source": user.get("initial_assessment_completion_source"),
        },
    )
    return {"ok": True, "care_plan": care_plan}


@api_router.get("/rehab/current-plan", response_model=Assessment)
async def get_current_account_rehab_plan(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    assessments = await _care_assessments_for_user(user["id"])
    if assessments:
        return Assessment(**assessments[0])
    completed_at = str(user.get("initial_assessment_completed_at") or "")
    if not completed_at:
        raise HTTPException(status_code=409, detail="Complete the Initial Assessment before opening a rehab plan.")
    profile = dict(user.get("profile") or {})
    plan = survey_rehab_plan(profile)
    if not plan:
        raise HTTPException(status_code=409, detail="Your saved survey does not currently produce an exercise plan.")
    recovered_gate: Dict[str, Any] = {
        "status": "recovered_account_plan",
        "rehab_access": "allowed",
        "rehab_plan_source": "fixed_core_programme",
        "patient_title": "Your saved plan is ready",
        "patient_message": (
            "The completed-assessment marker was recovered from the task ledger. "
            "The current four-exercise core programme is available; missing movement results were not recreated."
        ),
    }
    if str(profile.get("movement_pain") or "").lower() == "severe_or_worsening":
        recovered_gate = _clinical_gate_with_survey_hold(recovered_gate, profile)
    return Assessment(
        id="account-current-plan",
        created_at=completed_at,
        affected_side=str(profile.get("side_affected") or "right"),
        assessment_package="initial",
        patient_parameters=profile,
        task_results=[],
        functional_issues=[],
        rehab_plan=plan,
        clinical_review_gate=recovered_gate,
    )


@api_router.get("/alira/care-plan")
async def get_alira_care_plan(request: Request, as_of: Optional[str] = None):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    plan = await _adaptive_care_plan_for_user(user, now=_as_of_now(as_of)) if as_of else await _adaptive_care_plan_for_user(user)
    _record_alira_action(
        "care_plan_reviewed",
        source="app",
        user_id=user["id"],
        details={
            "stage": plan["stage"],
            "survey_due": plan["survey"]["due"],
            "assessment_due": plan["assessment"]["due"],
            "exercise_action": plan["exercise_plan"]["action"],
            "daily_action": plan["daily_monitoring"]["next_day_action"],
            "next_action": (plan.get("next_step") or {}).get("action"),
        },
    )
    return plan


@api_router.get("/alira/check-in/questions")
async def get_alira_check_in_questions(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    plan = await _adaptive_care_plan_for_user(user)
    _record_alira_action(
        "survey_questions_selected",
        source="adaptive_care_plan",
        user_id=user["id"],
        status="completed" if plan["survey"]["due"] else "not_due",
        details={
            "stage": plan["stage"],
            "due": plan["survey"]["due"],
            "question_ids": [question["id"] for question in plan["survey"]["questions"]],
        },
    )
    return {
        "due": plan["survey"]["due"],
        "due_at": plan["survey"]["due_at"],
        "stage": plan["stage"],
        "preface": plan["survey"]["preface"],
        "all_questions_optional": plan["survey"]["all_questions_optional"],
        "may_stop_at_any_point": plan["survey"]["may_stop_at_any_point"],
        "questions": plan["survey"]["questions"],
        "approved_question_ids": approved_question_ids(),
    }


@api_router.post("/alira/check-ins")
async def submit_alira_check_in(payload: AliraCheckInSubmit, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return await _persist_alira_check_in(user, payload)


@api_router.post("/alira/functional-issues")
async def submit_alira_functional_issue(payload: AliraFunctionalIssueSubmit, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return await _persist_functional_issue_report(user, payload)


@api_router.get("/alira/functional-issues")
async def list_alira_functional_issues(request: Request, limit: int = 20):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    items = await _care_issue_reports_for_user(user["id"])
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {
        "functional_issues": items[:max(1, min(limit, 100))],
        "approved_categories": approved_functional_issue_categories(),
    }


@api_router.get("/alira/check-ins")
async def list_alira_check_ins(request: Request, limit: int = 20):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    items = await _care_check_ins_for_user(user["id"])
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"check_ins": items[:max(1, min(limit, 100))]}


def _domain_exercise_scores(items: List[Dict[str, Any]], per_domain: int = 5) -> Dict[str, Optional[float]]:
    """Final score per functional domain: the mean of the most recent scored
    exercise activities in that domain (assisted sessions already halved)."""
    buckets: Dict[str, List[float]] = {"upper_limb": [], "hand": [], "lower_limb": []}
    for item in items:  # items arrive newest-first
        domain = item.get("functional_domain") or EXERCISE_FUNCTIONAL_DOMAINS.get(str(item.get("exercise_id")))
        score = item.get("average_score")
        if domain in buckets and isinstance(score, (int, float)) and len(buckets[domain]) < per_domain:
            buckets[domain].append(float(score))
    return {
        domain: (round(sum(scores) / len(scores), 1) if scores else None)
        for domain, scores in buckets.items()
    }


@api_router.get("/alira/activities")
async def list_alira_activities(request: Request, limit: int = 100, exercise_id: Optional[str] = None):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    items = await _care_activities_for_user(user["id"])
    if exercise_id:
        items = [item for item in items if item.get("exercise_id") == exercise_id]
    items.sort(key=lambda item: item.get("completed_at", item.get("created_at", "")), reverse=True)
    return {
        "activities": items[:max(1, min(limit, 500))],
        "target_score": 80,
        "domain_scores": _domain_exercise_scores(items),
    }


@api_router.post("/alira/activities")
async def submit_alira_activity(payload: AliraActivitySubmit, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return await _persist_alira_activity(user, payload)


def _patient_record_id(user_id: str, kind: str, *parts: str) -> str:
    value = json.dumps([user_id, kind, *parts], separators=(",", ":"))
    return kind + "_" + hashlib.sha256(value.encode()).hexdigest()


def _patient_activity_unavailable(error: Exception) -> HTTPException:
    logger.warning("Patient activity persistence failed: %s", type(error).__name__)
    return HTTPException(
        status_code=503,
        detail="Your activity has not been confirmed saved. Please retry when the connection returns.",
        headers={"Retry-After": "5"},
    )


async def _patient_activity_user(request: Request) -> Dict[str, Any]:
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    return user


class ExerciseRepetitionSubmit(BaseModel):
    exercise_id: str = Field(min_length=1, max_length=120)
    plan_id: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=160)
    day: str = Field(min_length=10, max_length=10)
    rep: int = Field(ge=1, le=500)
    total_reps: int = Field(ge=1, le=500)
    score: Optional[float] = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    point_earned: bool = False


@api_router.post("/users/exercise-repetitions")
async def save_exercise_repetition(payload: ExerciseRepetitionSubmit, request: Request):
    user = await _patient_activity_user(request)
    day = _validated_checkin_date(payload.day)
    record = payload.model_dump()
    record.update(user_id=user["id"], created_at=datetime.now(timezone.utc).isoformat())
    record_id = _patient_record_id(user["id"], "rep", payload.plan_id, payload.exercise_id, day, payload.session_id, str(payload.rep))
    try:
        # The account-scoped unique _id makes network retries exactly-once.
        await db.exercise_repetitions.update_one(
            {"_id": record_id}, {"$setOnInsert": record}, upsert=True,
        )
    except Exception as exc:
        raise _patient_activity_unavailable(exc) from exc
    return {"ok": True, "id": record_id, "store": "mongodb"}


def _exercise_progress_summary(repetitions: List[Dict[str, Any]], activities: List[Dict[str, Any]], day: str) -> Dict[str, Any]:
    sessions: Dict[tuple, Dict[str, Any]] = {}
    for rep in repetitions:
        key = (rep["exercise_id"], rep["session_id"])
        session = sessions.setdefault(key, {"reps": 0, "scores": [], "total": rep["total_reps"], "complete": False})
        session["reps"] += 1
        if isinstance(rep.get("score"), (int, float)):
            session["scores"].append(rep["score"])
    for activity in activities:
        key = (activity["exercise_id"], activity.get("client_activity_id") or activity["id"])
        session = sessions.setdefault(key, {"reps": 0, "scores": [], "total": 0, "complete": False})
        session["reps"] = max(session["reps"], activity.get("completed_reps", 0))
        session["total"] = max(session["total"], session["reps"])
        session["complete"] = True
        # Finalised scores include any patient-confirmed assistance adjustment.
        session["scores"] = activity.get("repetition_scores") or ([] if activity.get("average_score") is None else [activity["average_score"]])
    out: Dict[str, Any] = {}
    for (exercise_id, _session_id), session in sessions.items():
        item = out.setdefault(exercise_id, {"completed_reps": 0, "total_reps": 0, "last_score": None, "best_score": None, "sessions": 0, "day": day})
        item["completed_reps"] += session["reps"]
        item["total_reps"] = max(item["total_reps"], session["total"])
        item["sessions"] += int(session["complete"])
        if session["scores"]:
            score = round(sum(session["scores"]) / len(session["scores"]), 1)
            item["last_score"] = score
            item["best_score"] = max(item["best_score"] or 0, score)
    return out


@api_router.get("/users/exercise-progress")
async def get_exercise_progress(request: Request, plan_id: str, date: str):
    user = await _patient_activity_user(request)
    day = _validated_checkin_date(date)
    query = {"user_id": user["id"], "plan_id": plan_id, "day": day}
    try:
        repetitions, activities = await asyncio.gather(
            db.exercise_repetitions.find(query, {"_id": 0}).sort("created_at", 1).to_list(None),
            db.alira_activities.find({
                "user_id": user["id"], "plan_id": plan_id,
                "$or": [
                    {"day": day},
                    {"day": None, "completed_at": {"$regex": "^" + re.escape(day) + "T"}},
                ],
            }, {"_id": 0}).sort("completed_at", 1).to_list(None),
        )
    except Exception as exc:
        raise _patient_activity_unavailable(exc) from exc
    return {"ok": True, "progress": _exercise_progress_summary(repetitions, activities, day)}


class JournalEntrySubmit(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=5000)
    createdAt: datetime


@api_router.post("/users/journal")
async def save_journal_entry(payload: JournalEntrySubmit, request: Request):
    user = await _patient_activity_user(request)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Write a note before saving")
    record = {
        "id": payload.id, "body": body, "title": "Recovery note", "tag": "Personal note",
        "createdAt": payload.createdAt.isoformat(), "user_id": user["id"],
    }
    try:
        await db.journal_entries.update_one(
            {"_id": _patient_record_id(user["id"], "journal", payload.id)},
            {"$setOnInsert": record}, upsert=True,
        )
    except Exception as exc:
        raise _patient_activity_unavailable(exc) from exc
    return {"ok": True, "entry": record, "store": "mongodb"}


@api_router.get("/users/journal")
async def get_journal_entries(request: Request):
    user = await _patient_activity_user(request)
    try:
        entries = await db.journal_entries.find({"user_id": user["id"]}, {"_id": 0}).sort("createdAt", -1).to_list(None)
    except Exception as exc:
        raise _patient_activity_unavailable(exc) from exc
    return {"ok": True, "entries": entries}


class AssessmentDeferral(BaseModel):
    domain: str = Field(pattern="^(upper_limb|hand|lower_limb)$")
    reason: Optional[str] = Field(default=None, max_length=200)


@api_router.post("/alira/assessment-deferral")
async def defer_missing_assessment(payload: AssessmentDeferral, request: Request):
    """Record that the patient cannot complete a missing assessment task today.

    Spec 2.2: the decline is accepted without penalty and recorded as context;
    the rehab plan continues for the domains that were assessed, and Alira
    asks again another day.
    """
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    deferrals = dict(user.get("assessment_deferrals") or {})
    deferrals[payload.domain] = {
        "deferred_at": datetime.now(timezone.utc).isoformat(),
        "reason": payload.reason or "not_possible_today",
    }
    await _save_user_fields(user, {"assessment_deferrals": deferrals}, context="assessment deferral")
    return {"ok": True, "domain": payload.domain, "recorded_as": "context_not_failure"}


class NextAssessmentOverride(BaseModel):
    date: Optional[str] = Field(default=None, max_length=10)


@api_router.post("/users/testing/next-assessment")
async def set_next_assessment_for_testing(payload: NextAssessmentOverride, request: Request):
    """Testing phase: pin the next re-assessment to a calendar date.

    A tester walking through the days with the Home date stepper can bring the
    re-assessment forward (for example to tomorrow) to check the re-assessment-day
    prompt and the calendar without waiting out the plan's cadence. The care plan
    uses the pinned date as the assessment due date until an assessment is
    completed after the pin was set; then the plan's own schedule resumes.
    ``date: null`` clears the pin.
    """
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    value = (payload.date or "").strip()
    override: Optional[Dict[str, Any]] = None
    if value:
        _as_of_now(value)  # validates the calendar date (422 otherwise)
        override = {"date": value, "set_at": datetime.now(timezone.utc).isoformat()}
    await _save_user_fields(user, {"next_assessment_override": override}, context="next assessment date (testing)")
    _record_alira_action(
        "next_assessment_date_pinned_for_testing" if override else "next_assessment_date_pin_cleared",
        source="app",
        user_id=user["id"],
        details={"date": value or None},
    )
    return {"ok": True, "next_assessment_override": override}


# ============ Daily check-in calendar ============
# The patient taps "Check in" after signing in, which marks the day as
# in progress on their calendar. The day only earns its complete check mark
# once the day's exercises are actually completed. Dates are the patient's
# local calendar dates, supplied by the client, so a late-evening session
# never lands on the wrong day because of time zones.

DAILY_CHECKIN_DATE_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
DAILY_CHECKIN_HISTORY_LIMIT = 400


class DailyCheckInSubmit(BaseModel):
    date: str = Field(min_length=10, max_length=10)


class DailyMedalCollect(DailyCheckInSubmit):
    current_date: Optional[str] = Field(default=None, min_length=10, max_length=10)


class RewardMilestoneCollect(DailyCheckInSubmit):
    testing_revision: Optional[str] = Field(default=None, max_length=64)


def _validated_checkin_date(value: str) -> str:
    if not DAILY_CHECKIN_DATE_PATTERN.match(value):
        raise HTTPException(status_code=422, detail="date must be a YYYY-MM-DD calendar date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date must be a valid calendar date") from exc
    return value


async def _save_daily_checkins(user: Dict[str, Any], checkins: Dict[str, Dict[str, Any]]) -> None:
    previous_checkins = user.get("daily_checkins") or {}
    monotonic_changes = {
        f"daily_checkins.{_validated_checkin_date(day)}.{field}": value
        for day, entry in checkins.items()
        if entry != previous_checkins.get(day)
        for field, value in entry.items()
        if field in {"status", "checked_in_at", "completed_at", "medal_collected_at"} and value
    }
    milestone_changes = {
        f"daily_checkins.{_validated_checkin_date(day)}.milestone_medals.{collection_id}": medal
        for day, entry in checkins.items()
        for collection_id, medal in (entry.get("milestone_medals") or {}).items()
        if medal != (((previous_checkins.get(day) or {}).get("milestone_medals") or {}).get(collection_id))
    }
    update: Dict[str, Any] = {}
    if monotonic_changes:
        update["$min"] = monotonic_changes
    if milestone_changes:
        update["$set"] = milestone_changes
    if not update:
        return
    try:
        # Update only changed fields, not a stale copy of the whole calendar.
        # 'complete' sorts before 'in_progress'; $min also preserves the first
        # timestamp, so retries/another tab cannot undo a completed day. Reward
        # milestones have their own stable collection id and are set separately.
        saved = await db.users.find_one_and_update(
            {"id": user["id"]}, update, return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        if not saved:
            raise RuntimeError("Account was not found when saving check-in")
        checkins.clear()
        checkins.update(saved.get("daily_checkins") or {})
        _remember_local_user(saved)
    except Exception as exc:
        _require_durable_patient_store("daily check-in", exc)
        await _save_user_fields(user, {"daily_checkins": checkins}, context="daily check-in")


def _daily_calendar_day(day: str, record: Dict[str, Any]) -> Dict[str, Any]:
    milestone_medals = sorted(
        [dict(item) for item in (record.get("milestone_medals") or {}).values() if isinstance(item, dict)],
        key=lambda item: (str(item.get("collected_at") or ""), str(item.get("id") or "")),
    )
    result = {
        "date": day,
        "status": record.get("status") or "in_progress",
        "medal": bool(record.get("medal_collected_at") or milestone_medals),
    }
    # Keep the existing daily-medal response unchanged when no milestone is on
    # the date. New clients can distinguish two medals collected on one day.
    if milestone_medals:
        result["daily_medal"] = bool(record.get("medal_collected_at"))
        result["milestone_medals"] = milestone_medals
    return result


def _daily_checkin_response(date: str, checkins: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    entry = checkins.get(date) or {}
    return {
        "ok": True,
        "date": date,
        "status": entry.get("status") or "not_checked_in",
        "medal_collected": bool(entry.get("medal_collected_at")),
        "available_medal_date": next((
            day for day, record in sorted(checkins.items())
            if day < date and record.get("status") == "complete" and not record.get("medal_collected_at")
        ), None),
        "days": [_daily_calendar_day(day, record) for day, record in sorted(checkins.items())],
    }


@api_router.get("/users/daily-checkin")
async def get_daily_checkins(request: Request, date: Optional[str] = None):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    requested_date = _validated_checkin_date(date) if date else datetime.now(timezone.utc).date().isoformat()
    return _daily_checkin_response(requested_date, dict(user.get("daily_checkins") or {}))


@api_router.post("/users/daily-checkin")
async def start_daily_checkin(payload: DailyCheckInSubmit, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    date = _validated_checkin_date(payload.date)
    checkins = dict(user.get("daily_checkins") or {})
    if date not in checkins:
        checkins[date] = {"status": "in_progress", "checked_in_at": datetime.now(timezone.utc).isoformat()}
        await _save_daily_checkins(user, checkins)
    return _daily_checkin_response(date, checkins)


@api_router.post("/users/daily-checkin/complete")
async def complete_daily_checkin(payload: DailyCheckInSubmit, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    date = _validated_checkin_date(payload.date)
    checkins = dict(user.get("daily_checkins") or {})
    plan = await _adaptive_care_plan_for_user(user, now=_as_of_now(date))
    monitoring = plan.get("daily_monitoring") or {}
    caregiver = (plan.get("caregiver_plan") or {}).get("daily_delivery") or {}
    complete = caregiver.get("completed_today") if caregiver.get("required_today") else monitoring.get("current_round_complete")
    if not complete:
        # Older clients call this after each exercise, not just the final one.
        return _daily_checkin_response(date, checkins)
    entry = dict(checkins.get(date) or {"checked_in_at": datetime.now(timezone.utc).isoformat()})
    if entry.get("status") != "complete":
        entry["status"] = "complete"
        entry["completed_at"] = datetime.now(timezone.utc).isoformat()
        checkins[date] = entry
        await _save_daily_checkins(user, checkins)
    return _daily_checkin_response(date, checkins)


@api_router.post("/users/daily-checkin/medal")
async def collect_daily_medal(payload: DailyMedalCollect, request: Request):
    """Offer a completed day's medal on the next visit on a later local day.

    The client supplies its local/testing date, just like daily check-in.
    Collection always records the medal against the day it was earned.
    """
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    date = _validated_checkin_date(payload.date)
    current_date = _validated_checkin_date(payload.current_date) if payload.current_date else datetime.now(timezone.utc).date().isoformat()
    if date >= current_date:
        raise HTTPException(status_code=409, detail="This medal is available when you return on the next day.")
    checkins = dict(user.get("daily_checkins") or {})
    entry = dict(checkins.get(date) or {})
    if entry.get("status") != "complete":
        raise HTTPException(status_code=409, detail="That day's exercises are not complete yet.")
    if not entry.get("medal_collected_at"):
        entry["medal_collected_at"] = datetime.now(timezone.utc).isoformat()
        checkins[date] = entry
        await _save_daily_checkins(user, checkins)
    return _daily_checkin_response(current_date, checkins)


async def _reward_assessments_for_user(user_id: str) -> List[Dict[str, Any]]:
    # Rewards cover the whole account history, not the care planner's latest 100.
    # Do not load videos, motion frames or model outputs for this count.
    try:
        return await db.assessments.find({"user_id": user_id}, {
            "_id": 0, "id": 1, "created_at": 1, "testing_shortcut": 1,
            "result_provenance": 1, "assigned_task_ids": 1,
            "task_results.task_id": 1, "task_results.total_steps": 1,
            "task_results.steps.step_id": 1, "task_results.metrics.walking_skipped": 1,
        }).to_list(None)
    except Exception as exc:
        _require_durable_patient_store("assessment rewards lookup", exc)
        return [doc for doc in LOCAL_ASSESSMENTS if doc.get("user_id") == user_id]


@api_router.get("/users/rewards")
async def get_user_rewards(request: Request, as_of: Optional[str] = None):
    """Points, medals, and streak state (spec section 10). Effort-based:
    reduced-intensity and caregiver-assisted sessions earn the same points."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return await _rewards_for_user(user, as_of)


async def _rewards_for_user(user: Dict[str, Any], as_of: Optional[str] = None):
    activities = await _care_activities_for_user(user["id"])
    check_ins = await _care_check_ins_for_user(user["id"])
    testing = user.get("reward_points_testing") or {}
    rewards = compute_rewards(activities, check_ins, dict(user.get("daily_checkins") or {}), assessments=await _reward_assessments_for_user(user["id"]), now=_as_of_now(as_of), testing_points_adjustment=int(testing.get("adjustment") or 0))
    rewards["testing_revision"] = testing.get("revision")
    acknowledged = set(testing.get("acknowledged") or []) if testing else set(user.get("reward_milestones_acknowledged") or [])
    testing_revision = testing.get("revision") if testing else None
    for record in (user.get("daily_checkins") or {}).values():
        for collected in (record.get("milestone_medals") or {}).values():
            if not isinstance(collected, dict):
                continue
            collected_revision = collected.get("testing_revision")
            if (testing_revision and collected_revision == testing_revision) or (not testing_revision and not collected_revision):
                acknowledged.add(collected.get("id"))
    for medal in rewards.get("medals") or []:
        medal["celebrated"] = medal.get("id") in acknowledged
    return rewards


@api_router.post("/users/rewards/milestones/{milestone_id}/collect")
async def collect_reward_milestone(milestone_id: str, payload: RewardMilestoneCollect, request: Request):
    """Collect an earned points medal and attach it to the selected calendar day."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    date = _validated_checkin_date(payload.date)
    milestone = next((item for item in REWARD_MEDALS if item["id"] == milestone_id), None)
    if not milestone:
        raise HTTPException(status_code=404, detail="Reward milestone not found")
    rewards = await _rewards_for_user(user, date)
    if int(rewards.get("points") or 0) < int(milestone["threshold"]):
        raise HTTPException(status_code=409, detail="Reward milestone has not been earned yet")
    testing_revision = rewards.get("testing_revision")
    if payload.testing_revision != testing_revision:
        raise HTTPException(status_code=409, detail="Points changed before this medal was collected. Open the latest award and try again.")

    collection_id = milestone_id if not testing_revision else f"{milestone_id}__testing__{testing_revision}"
    checkins = dict(user.get("daily_checkins") or {})
    entry = dict(checkins.get(date) or {})
    milestone_medals = dict(entry.get("milestone_medals") or {})
    if collection_id not in milestone_medals:
        collected = {
            "id": milestone_id,
            "name": milestone["name"],
            "threshold": int(milestone["threshold"]),
            "points": int(rewards.get("points") or 0),
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        if testing_revision:
            collected["testing_revision"] = testing_revision
        milestone_medals[collection_id] = collected
        entry["milestone_medals"] = milestone_medals
        checkins[date] = entry
        await _save_daily_checkins(user, checkins)

    response = _daily_checkin_response(date, checkins)
    response["collected_medal"] = milestone_medals[collection_id]
    return response


class TestingPointsSubmit(BaseModel):
    points: Optional[int] = Field(..., strict=True, ge=0, le=1000000)


@api_router.post("/users/testing/points")
async def set_points_for_testing(payload: TestingPointsSubmit, request: Request):
    """Set this account's test total without fabricating completed activities.

    Subsequent activity still earns points. Null restores the earned total;
    each manual setting starts a fresh, separate milestone demonstration.
    """
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    rewards = await _rewards_for_user(user)
    testing = None if payload.points is None else {
        "adjustment": payload.points - rewards["earned_points"],
        "revision": str(uuid.uuid4()),
        "acknowledged": [],
    }
    await _save_user_fields(user, {"reward_points_testing": testing}, context="points for testing")
    return await _rewards_for_user({**user, "reward_points_testing": testing})


@api_router.post("/users/rewards/milestones/{milestone_id}/acknowledge")
async def acknowledge_reward_milestone(milestone_id: str, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    milestone = next((item for item in REWARD_MEDALS if item["id"] == milestone_id), None)
    if not milestone:
        raise HTTPException(status_code=404, detail="Reward milestone not found")
    rewards = await _rewards_for_user(user)
    if int(rewards.get("points") or 0) < int(milestone["threshold"]):
        raise HTTPException(status_code=409, detail="Reward milestone has not been earned yet")
    testing = user.get("reward_points_testing") or {}
    acknowledged = list(dict.fromkeys(testing.get("acknowledged") or [])) if testing else list(dict.fromkeys(user.get("reward_milestones_acknowledged") or []))
    if milestone_id not in acknowledged:
        acknowledged.append(milestone_id)
        await _save_user_fields(
            user,
            {"reward_points_testing": {**testing, "acknowledged": acknowledged}} if testing else {"reward_milestones_acknowledged": acknowledged},
            context="reward milestone acknowledgement",
        )
    return {"ok": True, "milestone_id": milestone_id, "celebrated": True}


@api_router.post("/alira/navigation-events")
async def record_alira_navigation_event(payload: AliraNavigationEventSubmit, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _record_alira_action(
        "navigation_executed" if payload.success else "navigation_failed",
        source=payload.source,
        user_id=user["id"],
        session_id=payload.session_id,
        status="completed" if payload.success else "failed",
        details={
            "requested_destination": payload.destination,
            "resolved_destination": payload.resolved_destination,
        },
    )
    return {"ok": True}


@api_router.post("/alira/assessment-resume-events")
async def record_alira_assessment_resume_event(payload: AliraAssessmentResumeEventSubmit, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    safe_package = _safe_video_token(payload.package_id, "assessment")
    valid_task_ids = set(_valid_package_task_ids(safe_package))
    if not set(payload.task_ids).issubset(valid_task_ids):
        raise HTTPException(status_code=422, detail="Assigned tasks do not match the assessment package")
    if not set(payload.completed_task_ids).issubset(set(payload.task_ids)):
        raise HTTPException(status_code=422, detail="Completed tasks do not match the assigned assessment")
    if not set(payload.ignored_device_completed_task_ids).issubset(set(payload.task_ids)):
        raise HTTPException(status_code=422, detail="Ignored device tasks do not match the assigned assessment")
    if payload.next_task_id and payload.next_task_id not in set(payload.task_ids):
        raise HTTPException(status_code=422, detail="Next task does not match the assigned assessment")
    _record_alira_action(
        "assessment_resume_selected",
        source="task_intro",
        user_id=user["id"],
        status="completed" if payload.next_task_id else "assessment_complete",
        details={
            "package_id": safe_package,
            "task_ids": payload.task_ids,
            "completed_task_ids": payload.completed_task_ids,
            "next_task_id": payload.next_task_id,
            "task_order": "fixed_approved_order",
            "progress_source": payload.progress_source,
            "ignored_device_completed_task_ids": payload.ignored_device_completed_task_ids,
        },
    )
    return {"ok": True}


async def _build_patient_context(user: Optional[Dict[str, Any]]) -> str:
    """Pull the signed-in patient's latest assessment and care schedule for Alira."""
    if not user:
        return "No signed-in patient context is available."
    assessments = await _care_assessments_for_user(user["id"])
    check_ins = await _care_check_ins_for_user(user["id"])
    activities = await _care_activities_for_user(user["id"])
    issue_reports = await _care_issue_reports_for_user(user["id"])
    health_data_consent = (user.get("consent") or {}).get("health_data_consent") is True
    consent_context = (
        "\n\nACCOUNT CONSENT STATUS:\n"
        + (
            "Required Terms and health-data consent are confirmed for this account. Do not ask for consent again."
            if health_data_consent
            else "Required health-data consent is not recorded for this account. The app must resolve this outside chat."
        )
    )
    doc = max(assessments, key=lambda item: item.get("created_at", ""), default=None)
    care_plan = build_adaptive_care_plan(user.get("profile") or {}, assessments, check_ins, activities, issue_reports)
    due_questions = [question["id"] for question in care_plan["survey"]["questions"]]
    adaptive_context = (
        "\n\nALIRA ADAPTIVE CARE WORKFLOW:\n"
        f"Recovery stage: {care_plan['stage']}\n"
        f"Safety status: {care_plan['safety']['status']}\n"
        f"Short check-in due: {care_plan['survey']['due']}\n"
        f"Required pre-survey message: {care_plan['survey']['preface']}\n"
        f"Approved questions to ask now: {', '.join(due_questions) or '(none due)'}\n"
        f"Camera assessment due: {care_plan['assessment']['due']}\n"
        f"Approved assessment packages: {', '.join(care_plan['assessment']['packages']) or '(none due)'}\n"
        f"Assessment trigger: {care_plan['assessment']['trigger']}\n"
        f"Approved assessment task ids: {', '.join(care_plan['assessment']['task_ids']) or '(none due)'}\n"
        f"Next exercise-plan action: {care_plan['exercise_plan']['action']}\n"
        f"Daily activity action: {care_plan['daily_monitoring']['next_day_action']}\n"
        "Autonomously follow these approved selections without asking for per-decision approval. Never bypass a safety hold or activate unapproved clinical content."
    )
    if not doc:
        return "The patient has not completed an assessment yet." + consent_context + adaptive_context
    issues = [f"- {i['label']}: {i['description']}" for i in doc.get("functional_issues", [])]
    plan = [f"- {e['name']} ({e['sets']}×{e['reps']}, {e['frequency']})" for e in doc.get("rehab_plan", [])]
    return (
        "Latest assessment date: " + doc.get("created_at", "unknown") + "\n"
        "Affected side: " + doc.get("affected_side", "unknown") + "\n\n"
        "FUNCTIONAL ISSUES IDENTIFIED:\n" + ("\n".join(issues) or "(none yet)") + "\n\n"
        "CURRENT REHAB PLAN:\n" + ("\n".join(plan) or "(no plan yet)") + consent_context + adaptive_context
    )


CHAT_SYSTEM_PROMPT_BASE = """You are "Alira" — a warm, calm AI stroke-rehabilitation companion. You provide therapist-informed education, reflection, and exercise support, but you are not a licensed therapist and you do not replace the patient's clinical team.

Your tone:
- Warm, patient, never patronizing
- Use short sentences. The patient may have visual or cognitive fatigue.
- Always validate feelings before giving information
- Celebrate small wins enthusiastically
- Never minimize their struggle, but never dwell in despair
- If a preferred name is provided below, use it naturally — not in every reply, but to feel seen

What you know:
- General stroke rehabilitation knowledge (Fugl-Meyer, ARAT, CIMT, BATRAC, Bobath, Task-Specific Training, neuroplasticity basics)
- The patient's current assessment results and rehab plan (provided below)

What you DO NOT do:
- Give medical diagnoses or change their medical regimen
- Recommend stopping medication or therapy
- Make false promises about recovery timelines
- Use clinical jargon — speak in plain, kind language
- Change, add, or delete app functionality
- Activate clinical content outside the approved question, assessment-task, and guided-exercise libraries. Novel ideas remain non-active drafts.

Adaptive rehabilitation workflow:
- Follow the ALIRA ADAPTIVE CARE WORKFLOW in the patient context. It is generated from saved survey answers, check-ins, validated assessment evidence, and the approved exercise library.
- Ask a short check-in only when it is due. Before its first question, give the Required pre-survey message from the patient context verbatim once. This preface is allowed to exceed the usual reply-length limit.
- Ask only the listed approved questions, one at a time. Every question is optional. If the patient stops, reassure them that their plan and access do not change. Save any answers they chose to give; do not create a blank check-in.
- Never infer a check-in answer. If the patient does not know or does not want to answer, respect that and leave that answer unsaved.
- Autonomously follow the backend choice of approved questions, tasks, exercises, sets, repetitions, and weekly frequency. No per-decision approval is required during testing.
- Never bypass the assessment due date. A genuinely new movement problem may open one targeted exception assessment only through report_new_functional_issue.
- Every exercise change must remain incremental and within the backend dose limit. Do not override a safety hold.
- Patient-reported difficulty remains valid evidence even when a camera task looks normal. Missing or pending model output is never evidence of normal function.

Consent handling:
- Required Terms and health-data consent are collected and saved by the app, not through conversation.
- When the patient context says consent is confirmed, treat it as settled. Never ask the patient to consent again or interpret a chat message as legal consent.
- If a care tool cannot confirm saved consent, do not repeat a consent question. Explain that Rehyn could not verify the saved account setting and open Data and permissions.

App navigation:
- When the patient asks to start, continue, open, or take an assessment, call navigate_app immediately. Use initial_assessment when no assessment is complete and next_assessment after a completed assessment; the navigation tool will refuse a follow-up that is not due.
- When a patient describes a genuinely new reaching, hand, walking, transfer, or balance problem, call report_new_functional_issue with the closest approved category. Do not file repeated known difficulty as new.
- If the patient says yes or that they are ready immediately after you offered to start an assessment, call navigate_app instead of asking another readiness, consent, or check-in question.
- The assessment flow applies saved readiness answers and its required safety check. Do not recreate those screens as a chat questionnaire.
- For any other request to open, show, find, or visit a Rehyn feature, call navigate_app rather than describing menu steps.

Safety:
- If the patient describes new facial droop, new arm weakness, new speech difficulty, sudden severe headache, collapse, chest pain, or trouble breathing, tell them to call 999 immediately. Do not ask follow-up questions first and do not wait for an app check.
- After telling them to call 999, use navigate_app with emergency_fast_check only as a visible FAST guide for a carer who can use it without delaying the emergency call. Never use a negative FAST screen to reassure them that a stroke is ruled out.
- If a fall is reported, use the exact safety response returned by record_rehab_check_in. Call 999 for a possible head, back, neck or hip injury or if the person cannot get up; otherwise direct possible pain, injury or illness to NHS 111 and pause exercise.
- If the patient says they cannot keep themselves safe, may act on suicidal thoughts, has seriously harmed themselves or taken an overdose, tell them to call 999 or go to A&E now. If suicidal or self-harm thoughts are present without immediate danger, direct them to NHS 111's mental health option or an urgent GP appointment, mention Samaritans 116 123, and explain that 999 or A&E is needed if safety worsens.
- A marked functional decline that was sudden is a possible emergency and needs 999. A marked but non-sudden decline pauses exercise and needs same-day contact with the stroke team, physiotherapist or GP, with NHS 111 if they cannot be reached.
- If pain, dizziness, or fatigue appears during exercise, tell them to stop and contact their therapist or clinician before continuing.
- Ask one short question at a time during pain check-ins or guided reflections.

When you don't know something, say so warmly and suggest asking their therapist.

If the patient seems distressed, gently acknowledge it, sit with them, and only suggest a tiny actionable step if they seem ready.

Keep replies under 4 short sentences unless the patient asks for more detail."""


def _chat_requests_survey_start(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized or any(phrase in normalized for phrase in ("skip the check", "stop the check", "not now")):
        return False
    mentions_survey = any(term in normalized for term in ("check-in", "check in", "survey"))
    asks_to_start = any(term in normalized for term in ("start", "begin", "scheduled", "ready", "do my"))
    return mentions_survey and asks_to_start


def _chat_requests_assessment_start(text: str, turns: Sequence[Dict[str, Any]]) -> bool:
    """Recognize an explicit assessment handoff without treating unrelated yes/no answers as consent."""
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    negative_phrases = (
        "not ready", "don't start", "do not start", "don't want an assessment",
        "do not want an assessment", "stop the assessment", "assessment history",
        "assessment result", "assessment results",
    )
    if any(phrase in normalized for phrase in negative_phrases):
        return False

    assessment_terms = ("assessment", "movement check", "movement test", "camera test", "functional test")
    action_terms = (
        "start", "begin", "take", "do ", "open", "continue", "ready", "go to",
        "want", "need", "can i", "could i", "please",
    )
    if any(term in normalized for term in assessment_terms) and any(term in normalized for term in action_terms):
        return True

    affirmations = {
        "yes", "yes please", "okay", "ok", "sure", "i'm ready", "im ready",
        "ready", "let's start", "lets start", "start now", "please do",
    }
    if normalized not in affirmations:
        return False
    last_assistant = next(
        (str(turn.get("text") or "") for turn in reversed(turns) if turn.get("role") == "assistant"),
        "",
    )
    invitation_tail = " ".join(last_assistant.lower().split())[-220:]
    return any(
        phrase in invitation_tail
        for phrase in (
            "are you ready?", "ready to begin?", "ready to start?",
            "would you like to start?", "would you like to begin?",
            "shall we start?", "shall we begin?", "open the assessment?",
        )
    )


def _chat_mentions_consent_setup(text: str) -> bool:
    """Identify a consent setup loop without matching ordinary privacy discussion."""
    normalized = " ".join(str(text or "").lower().split())
    return any(
        phrase in normalized
        for phrase in (
            "health-data consent",
            "health data consent",
            "consent settings",
            "provide consent",
            "give consent",
            "consent being fully set up",
            "consent is set up",
        )
    )


async def _save_chat_session(
    session_filter: Dict[str, Any],
    local_session_key: str,
    session_id: str,
    user_id: str,
    turns: List[Dict[str, Any]],
    updated_at: str,
) -> None:
    session_doc = {
        "session_id": session_id,
        "user_id": user_id,
        "turns": turns,
        "updated_at": updated_at,
    }
    try:
        await db.chat_sessions.update_one(session_filter, {"$set": session_doc}, upsert=True)
    except Exception as exc:
        _require_durable_patient_store("Alira conversation", exc)
        LOCAL_CHAT_SESSIONS[local_session_key] = session_doc


@api_router.post("/realtime/session")
async def create_alira_realtime_session(req: RealtimeSessionRequest, request: Request):
    """Create a browser WebRTC call without exposing the OpenAI API key."""
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="Live voice is unavailable because the AI key is not configured.")
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to call Alira.")

    _record_alira_action(
        "realtime_call_requested",
        source="realtime_voice",
        user_id=user["id"],
        session_id=req.session_id,
        status="started",
        details={"model": ALIRA_REALTIME_MODEL, "voice": ALIRA_REALTIME_VOICE},
    )

    patient_ctx = await _build_patient_context(user)
    consent_confirmed = (user.get("consent") or {}).get("health_data_consent") is True
    profile = user.get("profile") or {}
    preferred_name = (profile.get("preferred_name") or user.get("name") or "").split(" ")[0].strip()
    name_block = f"\nPATIENT PREFERRED NAME: {preferred_name}\n" if preferred_name else ""
    live_voice_rules = """

LIVE VOICE CONVERSATION RULES:
- This is a natural spoken conversation. Speak warmly and clearly; never use markdown, headings, or lists aloud.
- Usually answer in one to three short sentences, then pause. Ask no more than one question at a time.
- Give the patient time to speak and think. Do not rush, finish their sentences, or comment on speech difficulty.
- If the patient interrupts, stop speaking immediately and listen.
- If audio is unclear, gently ask them to repeat it rather than guessing.
- Never diagnose, prescribe, or present yourself as a therapist or emergency service.
- For possible stroke or other emergency symptoms, clearly tell the patient to call 999 now before asking anything else. Then use navigate_app with emergency_fast_check only if a carer can use the guide without delaying the call. Never say that a negative FAST screen rules out stroke or TIA.
- When the patient asks where a feature is or asks you to open, show, find, or take them to a page, call navigate_app. Do not give a sequence of menu directions instead.
- Wait for the navigation tool result before saying that a page is opening. If the requested result or plan is unavailable, explain the tool result briefly and offer the assessment or Journey page.
- Navigation is read-only. Never claim to change a setting, submit a form, delete data, or complete an assessment for the patient.
- Respect the assessment schedule returned by the backend. Never send a patient to a routine assessment that is not due.
- When the patient reports a genuinely new movement problem, identify the closest approved category and call report_new_functional_issue. Do not create duplicate exception assessments for a known problem.
- Alira may autonomously choose approved survey questions, assessment tasks, exercises, sets, repetitions, and frequency. Follow backend safety stops exactly and never activate unapproved clinical content.
- Before the first question of every due recovery survey, speak the Required pre-survey message from the patient context exactly once. Every question is optional; if the patient stops, confirm that their plan and access are unchanged.
- When record_rehab_check_in returns a safety status other than clear, speak its safety message exactly before anything else. Do not soften, shorten, or reinterpret it.
- When emergency help may be needed, tell the patient or carer that the visible Call 999 button opens the device dialler and that they must confirm the call. Never claim that Alira placed the call.
"""
    instructions = (
        CHAT_SYSTEM_PROMPT_BASE
        + live_voice_rules
        + name_block
        + "\n----\nPATIENT CONTEXT:\n"
        + patient_ctx
    )
    session_config = {
        "type": "realtime",
        "model": ALIRA_REALTIME_MODEL,
        "output_modalities": ["audio"],
        "instructions": instructions,
        "max_output_tokens": 500,
        "tools": [ALIRA_NAVIGATION_TOOL, ALIRA_RECORD_CHECKIN_TOOL, ALIRA_REPORT_FUNCTIONAL_ISSUE_TOOL],
        "tool_choice": "auto",
        "audio": {
            "input": {
                "noise_reduction": {"type": "near_field"},
                "transcription": {"model": "gpt-transcribe", "language": "en"},
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "low",
                    "create_response": True,
                    "interrupt_response": True,
                },
            },
            "output": {"voice": ALIRA_REALTIME_VOICE, "speed": 0.95},
        },
    }
    safety_identifier = hashlib.sha256(f"rehyn:{user['id']}".encode("utf-8")).hexdigest()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.post(
                "https://api.openai.com/v1/realtime/calls",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Safety-Identifier": safety_identifier,
                },
                files={
                    "sdp": (None, req.sdp),
                    "session": (None, json.dumps(session_config)),
                },
            )
    except httpx.HTTPError as exc:
        logger.error("Realtime session connection failed: %s", type(exc).__name__)
        _record_alira_action(
            "realtime_call_start_failed",
            source="realtime_voice",
            user_id=user["id"],
            session_id=req.session_id,
            status="failed",
            details={"failure_type": type(exc).__name__},
        )
        raise HTTPException(status_code=502, detail="Alira could not start a live call. Please try again.") from exc

    if not upstream.is_success:
        try:
            upstream_message = str(upstream.json().get("error", {}).get("message", ""))[:300]
        except (ValueError, AttributeError):
            upstream_message = upstream.text[:300]
        logger.error("Realtime session rejected (%s): %s", upstream.status_code, upstream_message)
        _record_alira_action(
            "realtime_call_start_failed",
            source="realtime_voice",
            user_id=user["id"],
            session_id=req.session_id,
            status="failed",
            details={"upstream_status": upstream.status_code},
        )
        raise HTTPException(status_code=502, detail="Alira could not start a live call. Please try again.")

    _record_alira_action(
        "realtime_call_started",
        source="realtime_voice",
        user_id=user["id"],
        session_id=req.session_id,
        details={
            "model": ALIRA_REALTIME_MODEL,
            "voice": ALIRA_REALTIME_VOICE,
            "tools": ["navigate_app", "record_rehab_check_in", "report_new_functional_issue"],
        },
    )
    return Response(content=upstream.text, media_type="application/sdp")


@api_router.post("/chat/message", response_model=ChatResponse)
async def chat_message(req: ChatRequest, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to talk with Alira.")
    consent_confirmed = (user.get("consent") or {}).get("health_data_consent") is True
    session_filter = {"session_id": req.session_id, "user_id": user["id"]}
    local_session_key = f"{user['id']}:{req.session_id}"
    try:
        sess = await db.chat_sessions.find_one(session_filter, {"_id": 0})
    except Exception as exc:
        _require_durable_patient_store("Alira conversation lookup", exc)
        sess = LOCAL_CHAT_SESSIONS.get(local_session_key)
    turns: List[Dict[str, Any]] = sess["turns"] if sess else []

    direct_safety = evaluate_survey_safety({"answers": {}, "patient_note": req.text})
    if direct_safety["status"] != "clear":
        now = datetime.now(timezone.utc).isoformat()
        reply_text = str(direct_safety["message"])
        emergency_call_available = bool(direct_safety.get("offer_call_999"))
        turns.append({"role": "user", "text": req.text, "ts": now})
        turns.append({
            "role": "assistant",
            "text": reply_text,
            "ts": now,
            "emergency_call_available": emergency_call_available,
        })
        await _save_chat_session(session_filter, local_session_key, req.session_id, user["id"], turns, now)
        _record_alira_action(
            "safety_response_presented",
            source="text_chat",
            user_id=user["id"],
            session_id=req.session_id,
            status=direct_safety["status"],
            details={
                "safety_code": direct_safety.get("code"),
                "call_999": direct_safety.get("call_999", False),
                "emergency_call_available": emergency_call_available,
            },
        )
        return ChatResponse(
            session_id=req.session_id,
            text=reply_text,
            turns=len(turns),
            emergency_call_available=emergency_call_available,
        )

    if _chat_requests_survey_start(req.text):
        care_plan = await _adaptive_care_plan_for_user(user)
        now = datetime.now(timezone.utc).isoformat()
        questions = care_plan["survey"].get("questions") or []
        if care_plan["survey"].get("due") and questions:
            first_question = str(questions[0].get("question") or "How have you been getting on?")
            reply_text = f"{SURVEY_PREFACE}\n\n{first_question}"
            action_status = "started"
        else:
            due_date = str(care_plan["survey"].get("due_at") or "")[:10]
            reply_text = (
                f"No recovery check-in is due today. Your next one is scheduled for {due_date}. "
                "You can still tell me if something has changed or if you need help finding a Rehyn feature."
            )
            action_status = "not_due"
        turns.append({"role": "user", "text": req.text, "ts": now})
        turns.append({"role": "assistant", "text": reply_text, "ts": now})
        await _save_chat_session(session_filter, local_session_key, req.session_id, user["id"], turns, now)
        _record_alira_action(
            "survey_preface_presented",
            source="text_chat",
            user_id=user["id"],
            session_id=req.session_id,
            status=action_status,
            details={"question_ids": [question.get("id") for question in questions]},
        )
        return ChatResponse(session_id=req.session_id, text=reply_text, turns=len(turns))

    if _chat_requests_assessment_start(req.text, turns):
        assessments = await _care_assessments_for_user(user["id"])
        check_ins = await _care_check_ins_for_user(user["id"])
        activities = await _care_activities_for_user(user["id"])
        issue_reports = await _care_issue_reports_for_user(user["id"])
        care_plan = build_adaptive_care_plan(user.get("profile") or {}, assessments, check_ins, activities, issue_reports)
        now = datetime.now(timezone.utc).isoformat()
        navigation_destination: Optional[str] = None
        if care_plan["assessment"]["blocked_by_safety"]:
            reply_text = str(care_plan["safety"]["message"])
        elif assessments and care_plan["assessment"]["due"]:
            navigation_destination = "next_assessment"
            reply_text = "I’m opening the movement assessment that best matches your current recovery needs."
        elif not assessments:
            navigation_destination = "initial_assessment"
            reply_text = "I’m opening your Initial Assessment now. Your saved readiness answers will select suitable tasks and completed tasks will not be repeated."
        else:
            due_date = str(care_plan["assessment"].get("due_at") or "")[:10]
            reply_text = (
                f"Your next assessment is scheduled for {due_date}, so we can measure meaningful change rather than repeat it too soon. "
                "If you have a genuinely new movement problem, tell me what has changed and I’ll check whether a targeted assessment is appropriate."
            )
        turns.append({"role": "user", "text": req.text, "ts": now})
        emergency_call_available = bool(care_plan["safety"].get("offer_call_999"))
        turns.append({
            "role": "assistant",
            "text": reply_text,
            "ts": now,
            "emergency_call_available": emergency_call_available,
        })
        await _save_chat_session(session_filter, local_session_key, req.session_id, user["id"], turns, now)
        _record_alira_action(
            "assessment_navigation_selected",
            source="text_chat",
            user_id=user["id"],
            session_id=req.session_id,
            status="completed" if navigation_destination else "blocked",
            details={
                "destination": navigation_destination,
                "blocked_by_safety": care_plan["assessment"]["blocked_by_safety"],
                "emergency_call_available": emergency_call_available,
                "task_ids": care_plan["assessment"].get("task_ids") or [],
                "selection_policy": "adaptive_care_plan",
            },
        )
        return ChatResponse(
            session_id=req.session_id,
            text=reply_text,
            turns=len(turns),
            navigation_destination=navigation_destination,
            emergency_call_available=emergency_call_available,
        )

    if not EMERGENT_LLM_KEY and not openai_tts_client:
        raise HTTPException(status_code=503, detail="Chat unavailable — LLM key not configured.")

    # Build patient context (refreshed every turn so new assessments propagate)
    patient_ctx = await _build_patient_context(user)
    # Inject preferred name from the signed-in user's onboarding profile, if available.
    name = ""
    if user:
        prof = user.get("profile") or {}
        name = (prof.get("preferred_name") or user.get("name") or "").split(" ")[0].strip()
    name_block = f"\nPATIENT PREFERRED NAME: {name}\n" if name else ""
    system_prompt = CHAT_SYSTEM_PROMPT_BASE + name_block + "\n----\nPATIENT CONTEXT:\n" + patient_ctx
    navigation_destination: Optional[str] = None
    model_used = ALIRA_CHAT_MODEL if openai_tts_client else "claude-sonnet-4-5-20250929"
    tool_actions: List[Dict[str, Any]] = []
    forced_safety_reply: Optional[str] = None
    emergency_call_available = False

    try:
        if openai_tts_client:
            messages = [{"role": "system", "content": system_prompt}]
            recent_turns = turns[-12:]
            if consent_confirmed:
                recent_turns = [
                    turn for turn in recent_turns
                    if not _chat_mentions_consent_setup(str(turn.get("text") or ""))
                ]
            messages.extend(
                {"role": turn["role"], "content": turn["text"]}
                for turn in recent_turns[-8:]
                if turn.get("role") in {"user", "assistant"}
            )
            messages.append({"role": "user", "content": req.text})

            def run_chat_completion():
                return openai_tts_client.chat.completions.create(
                    model=ALIRA_CHAT_MODEL,
                    messages=messages,
                    tools=[ALIRA_CHAT_NAVIGATION_TOOL, ALIRA_CHAT_RECORD_CHECKIN_TOOL, ALIRA_CHAT_REPORT_FUNCTIONAL_ISSUE_TOOL],
                    tool_choice="auto",
                    temperature=0.5,
                    max_tokens=260,
                )

            response = await asyncio.to_thread(run_chat_completion)
            assistant_message = response.choices[0].message
            tool_calls = list(assistant_message.tool_calls or [])
            if tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": assistant_message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                })
                for call in tool_calls:
                    if call.function.name == "navigate_app":
                        try:
                            arguments = json.loads(call.function.arguments or "{}")
                            destination = str(arguments.get("destination") or "").strip().lower()
                        except (ValueError, TypeError):
                            destination = ""
                        if destination in ALIRA_NAVIGATION_DESTINATIONS:
                            navigation_destination = destination
                            tool_result = {"ok": True, "destination": destination, "message": "The requested Rehyn page is opening."}
                        else:
                            tool_result = {"ok": False, "message": "That Rehyn destination is not available."}
                        tool_actions.append({
                            "tool": "navigate_app",
                            "destination": destination,
                            "success": bool(tool_result["ok"]),
                        })
                        _record_alira_action(
                            "navigation_selected" if tool_result["ok"] else "navigation_failed",
                            source="text_chat",
                            user_id=user["id"],
                            session_id=req.session_id,
                            status="completed" if tool_result["ok"] else "failed",
                            details={"destination": destination},
                        )
                    elif call.function.name == "report_new_functional_issue":
                        try:
                            arguments = json.loads(call.function.arguments or "{}")
                            saved = await _persist_functional_issue_report(
                                user,
                                AliraFunctionalIssueSubmit(
                                    category=str(arguments.get("category") or ""),
                                    description=arguments.get("description"),
                                    source="text_chat",
                                ),
                            )
                            next_assessment = saved["care_plan"]["assessment"]
                            if saved["is_new"] and next_assessment.get("due"):
                                navigation_destination = "next_assessment"
                            tool_result = {
                                "ok": True,
                                "is_new": saved["is_new"],
                                "message": saved["message"],
                                "next_assessment": next_assessment,
                            }
                            tool_actions.append({
                                "tool": "report_new_functional_issue",
                                "category": arguments.get("category"),
                                "success": True,
                                "is_new": saved["is_new"],
                            })
                        except (ValueError, TypeError, HTTPException) as exc:
                            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                            tool_result = {"ok": False, "message": detail}
                            tool_actions.append({"tool": "report_new_functional_issue", "success": False})
                    elif call.function.name == "record_rehab_check_in":
                        try:
                            arguments = json.loads(call.function.arguments or "{}")
                            submitted = AliraCheckInSubmit(
                                answers=arguments.get("answers") or {},
                                patient_note=arguments.get("patient_note"),
                                source="text_chat",
                            )
                            saved = await _persist_alira_check_in(user, submitted)
                            tool_result = {
                                "ok": True,
                                "safety": saved["care_plan"]["safety"],
                                "next_exercise_action": saved["care_plan"]["exercise_plan"]["action"],
                                "next_assessment": saved["care_plan"]["assessment"],
                            }
                            saved_safety = saved["care_plan"]["safety"]
                            if saved_safety.get("status") != "clear":
                                forced_safety_reply = str(saved_safety.get("message") or "").strip() or None
                                emergency_call_available = bool(saved_safety.get("offer_call_999"))
                            tool_actions.append({"tool": "record_rehab_check_in", "success": True})
                        except (ValueError, TypeError, HTTPException) as exc:
                            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                            tool_result = {"ok": False, "message": detail}
                            tool_actions.append({"tool": "record_rehab_check_in", "success": False})
                    else:
                        tool_result = {"ok": False, "message": "Unsupported Alira care tool."}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(tool_result, ensure_ascii=True),
                    })

                def run_follow_up_completion():
                    return openai_tts_client.chat.completions.create(
                        model=ALIRA_CHAT_MODEL,
                        messages=messages,
                        tools=[ALIRA_CHAT_NAVIGATION_TOOL, ALIRA_CHAT_RECORD_CHECKIN_TOOL, ALIRA_CHAT_REPORT_FUNCTIONAL_ISSUE_TOOL],
                        tool_choice="none",
                        temperature=0.3,
                        max_tokens=220,
                    )

                response = await asyncio.to_thread(run_follow_up_completion)
                assistant_message = response.choices[0].message
            reply_text = str(assistant_message.content or "").strip()
            if forced_safety_reply:
                reply_text = forced_safety_reply
        else:
            recent_turns = turns[-10:]
            if consent_confirmed:
                recent_turns = [
                    turn for turn in recent_turns
                    if not _chat_mentions_consent_setup(str(turn.get("text") or ""))
                ]
            recent = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in recent_turns[-6:])
            emergent_prompt = system_prompt + ("\n\n----\nRECENT CONVERSATION:\n" + recent if recent else "")
            chat = LlmChat(
                api_key=EMERGENT_LLM_KEY,
                session_id=req.session_id,
                system_message=emergent_prompt,
            ).with_model("anthropic", "claude-sonnet-4-5-20250929")
            response = await chat.send_message(UserMessage(text=req.text))
            reply_text = response if isinstance(response, str) else str(response)
        if consent_confirmed and _chat_mentions_consent_setup(reply_text):
            reply_text = "Your required consent is already confirmed for this account, so I won’t ask you for it again. We can continue with your assessment or check-in."
        if not reply_text:
            raise RuntimeError("Alira returned an empty response")
    except Exception as e:
        logger.error(f"Chat error: {e}")
        _record_alira_action(
            "chat_response_failed",
            source="text_chat",
            user_id=user["id"],
            session_id=req.session_id,
            status="failed",
            details={"model": model_used, "failure_type": type(e).__name__},
        )
        raise HTTPException(status_code=502, detail=f"Chat error: {str(e)[:200]}")

    now = datetime.now(timezone.utc).isoformat()
    turns.append({"role": "user", "text": req.text, "ts": now})
    turns.append({
        "role": "assistant",
        "text": reply_text,
        "ts": now,
        "emergency_call_available": emergency_call_available,
    })
    await _save_chat_session(session_filter, local_session_key, req.session_id, user["id"], turns, now)
    _record_alira_action(
        "chat_response_generated",
        source="text_chat",
        user_id=user["id"],
        session_id=req.session_id,
        details={
            "model": model_used,
            "tools": tool_actions,
            "navigation_destination": navigation_destination,
            "emergency_call_available": emergency_call_available,
            "turn_count": len(turns),
        },
    )
    return ChatResponse(
        session_id=req.session_id,
        text=reply_text,
        turns=len(turns),
        navigation_destination=navigation_destination,
        emergency_call_available=emergency_call_available,
    )


@api_router.get("/chat/history")
async def chat_history(session_id: str, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to view this conversation.")
    try:
        sess = await db.chat_sessions.find_one({"session_id": session_id, "user_id": user["id"]}, {"_id": 0})
    except Exception as exc:
        _require_durable_patient_store("Alira history", exc)
        sess = LOCAL_CHAT_SESSIONS.get(f"{user['id']}:{session_id}")
    return {"session_id": session_id, "turns": (sess or {}).get("turns", [])}


def _alira_next_step_message(plan: Dict[str, Any], name: str = "") -> str:
    step = plan.get("next_step") or {}
    greeting = f"Hi, {name}." if name else "Hi."
    title = str(step.get("title") or "Your next recovery step is ready.")
    message = str(step.get("message") or "Open your plan to see what to do next.")
    secondary = step.get("secondary_action") or {}
    survey_note = (
        " A short recovery check-in is also due, but it does not replace today's exercises."
        if secondary.get("action") == "recovery_check_in"
        else ""
    )
    return f"{greeting} {title}. {message}{survey_note}"


@api_router.post("/chat/proactive")
async def chat_proactive(req: ChatRequest, request: Request):
    """Return the care-plan action the patient should take next."""
    user = await _user_from_header(dict(request.headers))
    name = ""
    if user:
        profile = user.get("profile") or {}
        name = (profile.get("preferred_name") or user.get("name") or "").split(" ")[0].strip()
    try:
        sess = await db.chat_sessions.find_one(
            {"session_id": req.session_id, "user_id": user["id"]} if user else {"session_id": req.session_id},
            {"_id": 0},
        )
    except Exception as exc:
        _require_durable_patient_store("Alira reminder history", exc)
        sess = LOCAL_CHAT_SESSIONS.get(f"{user['id']}:{req.session_id}") if user else None
    has_history = bool(sess and sess.get("turns"))
    care_plan = await _adaptive_care_plan_for_user(user) if user else {}
    selected_message = _alira_next_step_message(care_plan, name)
    if user:
        _record_alira_action(
            "proactive_check_in_selected",
            source="text_chat",
            user_id=user["id"],
            session_id=req.session_id,
            details={
                "has_chat_history": has_history,
                "next_action": (care_plan.get("next_step") or {}).get("action"),
                "survey_due": (care_plan.get("survey") or {}).get("due"),
            },
        )
    return {"text": selected_message}


class DailyReminderRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=120)
    date: str = Field(min_length=10, max_length=10)


def _alira_daily_reminder_message(plan: Dict[str, Any], name: str = "", streak: int = 0, *, checked_in: bool = True) -> str:
    monitoring = plan.get("daily_monitoring") or {}
    remaining = [str(item) for item in (monitoring.get("remaining_exercise_ids_today") or [])]
    completed = [str(item) for item in (monitoring.get("completed_exercise_ids_today") or [])]
    greeting = f"Good to see you, {name}!" if name else "Good to see you!"
    count = len(remaining)
    exercises = "your exercise" if count == 1 else f"your {count} exercises" if count else "your exercises"
    progress = (
        f" You have already finished {len(completed)} today - nearly there."
        if completed else ""
    )
    momentum = (
        f" You are on a {streak}-day run, and every day you show up adds to it."
        if streak >= 2 else ""
    )
    next_action = "open today's plan" if checked_in else "check in and open today's exercise"
    return (
        f"{greeting} Today's plan is ready and {exercises} are waiting for you.{progress} "
        "Please finish them before the end of today: only completed days are recorded, so if today "
        "passes without them, today's scores are not saved and we lose track of the progress you are "
        "making. A short session done today is worth far more than a perfect one planned for tomorrow. "
        f"{momentum} You have got this - {next_action} and I will guide you through it, one exercise at a time."
    ).replace("  ", " ")


@api_router.post("/chat/daily-reminder")
async def chat_daily_reminder(req: DailyReminderRequest, request: Request):
    """Alira's once-a-day reminder to finish today's exercises.

    Sent the first time the patient opens the app on a day when the initial
    assessment is done and exercises remain; it is appended to the chat so it
    reads as a message from Alira. The app date is the patient's calendar date
    (or, in the testing phase, the date the tester has selected).
    """
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    _require_health_data_consent(user)
    reminder_date = _validated_checkin_date(req.date)
    checked_in = ((user.get("daily_checkins") or {}).get(reminder_date) or {}).get("status") in {"in_progress", "complete"}
    plan = await _adaptive_care_plan_for_user(user, now=_as_of_now(reminder_date))
    account_state = plan.get("account_state") or {}
    monitoring = plan.get("daily_monitoring") or {}
    remaining = list(monitoring.get("remaining_exercise_ids_today") or [])
    if not account_state.get("has_completed_initial_assessment") or not remaining:
        return {"sent": False, "text": None, "reason": "initial_assessment_pending" if not account_state.get("has_completed_initial_assessment") else "no_exercises_remaining"}
    profile = user.get("profile") or {}
    name = (profile.get("preferred_name") or user.get("name") or "").split(" ")[0].strip()
    session_filter = {"session_id": req.session_id, "user_id": user["id"]}
    local_session_key = f"{user['id']}:{req.session_id}"
    try:
        sess = await db.chat_sessions.find_one(session_filter, {"_id": 0})
    except Exception as exc:
        _require_durable_patient_store("Alira daily reminder", exc)
        sess = LOCAL_CHAT_SESSIONS.get(local_session_key)
    turns: List[Dict[str, Any]] = list((sess or {}).get("turns") or [])
    already = next(
        (turn for turn in turns if turn.get("role") == "assistant" and turn.get("daily_reminder_date") == reminder_date),
        None,
    )
    if already:
        # A saved reminder can be reopened after the check-in state changes.
        # Adapt its call to action without posting another chat message.
        text = str(already.get("text") or "")
        text = text.replace("check in and open today's exercise", "open today's plan") if checked_in else text.replace("open today's plan", "check in and open today's exercise")
        return {"sent": False, "text": text, "reason": "already_sent_today", "remaining_exercise_ids": remaining}
    try:
        rewards = compute_rewards(
            await _care_activities_for_user(user["id"]),
            await _care_check_ins_for_user(user["id"]),
            dict(user.get("daily_checkins") or {}),
            assessments=await _care_assessments_for_user(user["id"]),
            now=_as_of_now(reminder_date),
        )
        streak = int((rewards.get("streak") or {}).get("current_days") or 0)
    except Exception:
        streak = 0
    text = _alira_daily_reminder_message(plan, name, streak, checked_in=checked_in)
    now = datetime.now(timezone.utc).isoformat()
    turns.append({"role": "assistant", "text": text, "ts": now, "daily_reminder_date": reminder_date})
    await _save_chat_session(session_filter, local_session_key, req.session_id, user["id"], turns, now)
    _record_alira_action(
        "daily_exercise_reminder_sent",
        source="app",
        user_id=user["id"],
        session_id=req.session_id,
        details={"date": reminder_date, "remaining_exercise_ids": remaining},
    )
    return {"sent": True, "text": text, "reason": "sent", "remaining_exercise_ids": remaining}


@api_router.get("/chat/proactive/messages")
async def chat_proactive_messages(request: Request, n: int = 3):
    """Return only an actionable care-plan reminder for the floating Alira bubble."""
    user = await _user_from_header(dict(request.headers))
    name = ""
    if user:
        profile = user.get("profile") or {}
        name = (profile.get("preferred_name") or user.get("name") or "").split(" ")[0].strip()
    care_plan = await _adaptive_care_plan_for_user(user) if user else {}
    selected_messages = [_alira_next_step_message(care_plan, name)]
    if user:
        _record_alira_action(
            "proactive_messages_selected",
            source="app",
            user_id=user["id"],
            details={
                "message_count": len(selected_messages),
                "next_action": (care_plan.get("next_step") or {}).get("action"),
            },
        )
    return {"messages": selected_messages, "name": name}


# Mount routes
# (deferred to end-of-file after Phase C routes)

# Serialize a patient's mutations with their reset in this API process. Mongo's
# durable marker also blocks other instances while cleanup is incomplete.
_account_mutation_locks = weakref.WeakValueDictionary()


class AccountResetGuardMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        uid = request.headers.get("x-user-id")
        if not uid or request.method in {"GET", "HEAD", "OPTIONS"}:
            return await self.app(scope, receive, send)
        lock = _account_mutation_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            error = None
            if request.url.path != "/api/users/account/reset":
                try:
                    user = await _user_from_header(dict(request.headers))
                    if user and str(user.get("account_generation") or 0) != request.headers.get("x-account-generation", "0"):
                        error = JSONResponse(status_code=409, content={"detail": "This account was reset. Reopen Rehyn before continuing.", "code": "ACCOUNT_RESET"})
                except HTTPException as exc:
                    error = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
                except Exception:
                    error = JSONResponse(status_code=503, content={"detail": "Saved account is temporarily unavailable. Please retry."})
            if error is not None:
                return await error(scope, receive, send)
            # Retain the lock through streamed chat responses and their final
            # persistence, not just until the HTTP response headers are sent.
            return await self.app(scope, receive, send)


app.add_middleware(AccountResetGuardMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ============ Phase C — Billing (Stripe), Google Auth, Progress dashboard ============
import stripe as _stripe
import httpx as _httpx

_stripe.api_key = os.environ.get("STRIPE_API_KEY") or os.environ.get("STRIPE_SECRET_KEY") or ""
STRIPE_PRICE_SUBSCRIPTION_AMOUNT = 999  # $9.99 in cents
STRIPE_PRICE_CREDIT_PACK_AMOUNT = 499   # $4.99 in cents
CREDIT_PACK_SIZE = 200


def _stripe_origin(request: Request) -> str:
    """Build a return URL origin from the incoming request — works for both web
    and mobile WebView Checkout returns."""
    origin = request.headers.get("origin")
    if origin:
        return origin
    referer = request.headers.get("referer", "")
    if referer:
        try:
            from urllib.parse import urlparse
            p = urlparse(referer)
            return f"{p.scheme}://{p.netloc}"
        except Exception:
            pass
    return "https://localhost"


@api_router.post("/billing/subscribe")
async def billing_subscribe(request: Request):
    """Create a Stripe Checkout Session for the $9.99/mo subscription.
    Uses inline `price_data` so we don't need pre-created Stripe prices."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if not _stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    origin = _stripe_origin(request)
    try:
        session = _stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "NeuroMotion Unlimited (Monthly)"},
                    "unit_amount": STRIPE_PRICE_SUBSCRIPTION_AMOUNT,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            customer_email=user.get("email"),
            client_reference_id=user["id"],
            metadata={"action": "start_subscription", "app_user_id": user["id"]},
            success_url=f"{origin}/billing-return?session_id={{CHECKOUT_SESSION_ID}}&kind=sub",
            cancel_url=f"{origin}/?canceled=1",
        )
        # Track pending session so the return endpoint can finalize even without webhooks.
        await db.billing_sessions.insert_one({
            "session_id": session["id"], "user_id": user["id"], "kind": "subscription",
            "status": "pending", "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"url": session["url"], "session_id": session["id"]}
    except Exception as e:
        logger.error(f"Stripe subscribe error: {e}")
        raise HTTPException(status_code=500, detail=str(e)[:300])


@api_router.post("/billing/buy-credits")
async def billing_buy_credits(request: Request):
    """One-time $4.99 → 200 credits for AI therapist chat top-up."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if not _stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    origin = _stripe_origin(request)
    try:
        session = _stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"NeuroMotion {CREDIT_PACK_SIZE} Credits Pack"},
                    "unit_amount": STRIPE_PRICE_CREDIT_PACK_AMOUNT,
                },
                "quantity": 1,
            }],
            customer_email=user.get("email"),
            client_reference_id=user["id"],
            metadata={"action": "buy_credits", "app_user_id": user["id"], "credits": str(CREDIT_PACK_SIZE)},
            success_url=f"{origin}/billing-return?session_id={{CHECKOUT_SESSION_ID}}&kind=credits",
            cancel_url=f"{origin}/?canceled=1",
        )
        await db.billing_sessions.insert_one({
            "session_id": session["id"], "user_id": user["id"], "kind": "credit_pack",
            "credits": CREDIT_PACK_SIZE, "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"url": session["url"], "session_id": session["id"]}
    except Exception as e:
        logger.error(f"Stripe credit pack error: {e}")
        raise HTTPException(status_code=500, detail=str(e)[:300])


@api_router.get("/billing/verify-session")
async def billing_verify_session(session_id: str, request: Request):
    """Poll-based fallback to webhook: the frontend calls this after Checkout
    returns. We fetch the Checkout Session from Stripe; if paid, we apply the
    subscription or credit grant idempotently (using our local billing_sessions
    record to avoid double-applying)."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    rec = await db.billing_sessions.find_one({"session_id": session_id}, {"_id": 0})
    if not rec or rec.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="Unknown session")
    if rec.get("status") == "applied":
        # Already applied — just return current user state.
        u = await db.users.find_one({"id": user["id"]}, {"_id": 0})
        return {"status": "applied", "credits": u["credits"], "subscription_active": bool(u.get("subscription_active"))}
    if not _stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    try:
        session = _stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:200])
    paid = session.get("payment_status") in ("paid", "no_payment_required")
    if not paid:
        return {"status": "pending", "payment_status": session.get("payment_status")}
    # Apply effect idempotently
    if rec["kind"] == "subscription":
        # Pull subscription period end if available
        period_end_iso = None
        sub_id = session.get("subscription")
        if sub_id:
            try:
                sub = _stripe.Subscription.retrieve(sub_id)
                cpe = sub.get("current_period_end")
                if cpe:
                    period_end_iso = datetime.fromtimestamp(cpe, tz=timezone.utc).isoformat()
            except Exception:
                pass
        await db.users.update_one({"id": user["id"]}, {"$set": {
            "subscription_active": True,
            "subscription_id": sub_id,
            "subscription_period_end": period_end_iso,
        }})
    elif rec["kind"] == "credit_pack":
        credits = int(rec.get("credits", CREDIT_PACK_SIZE))
        await db.users.update_one({"id": user["id"]}, {"$inc": {"credits": credits}})
    await db.billing_sessions.update_one({"session_id": session_id}, {"$set": {"status": "applied"}})
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    return {"status": "applied", "credits": u["credits"], "subscription_active": bool(u.get("subscription_active"))}


@api_router.post("/billing/webhook")
async def billing_webhook(request: Request):
    """Best-effort Stripe webhook. Verifies signature only if STRIPE_WEBHOOK_SECRET
    is set; otherwise still processes events (useful in dev). Applies the same
    idempotent grant logic as /billing/verify-session."""
    payload = await request.body()
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    sig = request.headers.get("stripe-signature", "")
    event = None
    if secret and sig:
        try:
            event = _stripe.Webhook.construct_event(payload, sig, secret)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Bad webhook signature: {e}")
    else:
        import json as _json
        try:
            event = _json.loads(payload.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="Bad payload")
    etype = event.get("type")
    obj = event.get("data", {}).get("object", {})
    if etype == "checkout.session.completed":
        sid = obj.get("id")
        rec = await db.billing_sessions.find_one({"session_id": sid}, {"_id": 0}) if sid else None
        if rec and rec.get("status") != "applied":
            uid = rec["user_id"]
            if rec["kind"] == "subscription":
                period_end_iso = None
                cpe = obj.get("subscription")
                # Just mark active; period_end is best-effort.
                await db.users.update_one({"id": uid}, {"$set": {
                    "subscription_active": True,
                    "subscription_id": cpe,
                    "subscription_period_end": period_end_iso,
                }})
            elif rec["kind"] == "credit_pack":
                credits = int(rec.get("credits", CREDIT_PACK_SIZE))
                await db.users.update_one({"id": uid}, {"$inc": {"credits": credits}})
            await db.billing_sessions.update_one({"session_id": sid}, {"$set": {"status": "applied"}})
    elif etype == "customer.subscription.deleted":
        sub_id = obj.get("id")
        if sub_id:
            await db.users.update_one({"subscription_id": sub_id}, {"$set": {"subscription_active": False}})
    return {"ok": True}


# ============ Google Auth (Emergent-managed) ============
@api_router.post("/auth/google/session")
async def auth_google_session(request: Request):
    """Frontend posts the session_id it received in the redirect from
    https://auth.emergentagent.com/. We exchange it for the user payload, then
    create/lookup our user record."""
    body = await request.json()
    sid = body.get("session_id") or body.get("sessionId")
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")
    _require_trial_access_code(body.get("trial_code"))
    async with _httpx.AsyncClient(timeout=10.0) as cx:
        try:
            r = await cx.get(
                "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
                headers={"X-Session-ID": sid},
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Auth service unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail=f"Invalid session ({r.status_code})")
    data = r.json()
    email = (data.get("email") or "").lower()
    name = data.get("name") or email.split("@")[0]
    if not email:
        raise HTTPException(status_code=400, detail="No email returned from Google")
    user = await get_or_create_user(email, name, role="patient")
    # Stash picture/google_id for the profile
    extras = {k: data.get(k) for k in ("picture", "id") if data.get(k)}
    if extras:
        user = await _save_user_fields(user, {"google": extras}, context="Google profile update")
    is_new_account = not bool(user.get("trial_access_granted"))
    granted = await _grant_trial_access(user)
    return {**granted, **_account_state(granted, is_new_account=is_new_account)}


# ============ Progress dashboard ============
@api_router.get("/therapist/patients")
async def therapist_patients(request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user or user.get("role") != "therapist":
        raise HTTPException(status_code=400, detail="Therapist account required")
    bookings = await db.bookings.find({"therapist_user_id": user["id"]}, {"_id": 0}).to_list(500)
    roster: Dict[str, str] = {}
    for b in bookings:
        pid = b.get("patient_user_id")
        if pid:
            roster[pid] = b.get("patient_name") or "Patient"
    signoffs = await db.plan_signoffs.find({"therapist_user_id": user["id"]}, {"_id": 0}).to_list(500)
    signed_by_patient = {s["patient_user_id"]: s for s in signoffs}
    patients = []
    for pid, pname in roster.items():
        latest = await db.assessments.find_one(
            {"user_id": pid},
            {"_id": 0, "id": 1, "created_at": 1, "issues_detected": 1, "rehab_plan": 1},
            sort=[("created_at", -1)],
        )
        signed = signed_by_patient.get(pid)
        patients.append({
            "patient_user_id": pid,
            "name": pname,
            "latest_assessment_id": (latest or {}).get("id"),
            "last_assessment_date": (latest or {}).get("created_at"),
            "issues_count": len((latest or {}).get("issues_detected") or []),
            "exercises_count": len((latest or {}).get("rehab_plan") or []),
            "plan_signed": bool(signed and signed.get("assessment_id") == (latest or {}).get("id")),
            "signed_at": (signed or {}).get("signed_at"),
        })
    patients.sort(key=lambda p: p.get("last_assessment_date") or "", reverse=True)
    return {"patients": patients}


class PlanSignoff(BaseModel):
    assessment_id: str
    note: Optional[str] = None


@api_router.post("/therapist/patient/{patient_user_id}/signoff")
async def therapist_signoff(patient_user_id: str, payload: PlanSignoff, request: Request):
    user = await _user_from_header(dict(request.headers))
    if not user or user.get("role") != "therapist":
        raise HTTPException(status_code=400, detail="Therapist account required")
    signed_at = datetime.now(timezone.utc).isoformat()
    record = {
        "therapist_user_id": user["id"],
        "patient_user_id": patient_user_id,
        "assessment_id": payload.assessment_id,
        "note": payload.note or "",
        "signed_at": signed_at,
    }
    await db.plan_signoffs.update_one(
        {"therapist_user_id": user["id"], "patient_user_id": patient_user_id},
        {"$set": record},
        upsert=True,
    )
    return {"ok": True, "signed_at": signed_at}


def _assessment_progress_series(assessments_raw: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Per-assessment metric rows (oldest first) shared by the progress summary
    and the assessment report."""
    series: List[Dict[str, Any]] = []
    issues_count: Dict[str, int] = {}
    for a in assessments_raw:
        m = _functional_metrics_with_survey_mobility(
            a.get("metrics"),
            a.get("task_results", []),
            a.get("assigned_task_ids"),
            a.get("patient_parameters") or {},
        )
        issues = [
            item for item in (a.get("functional_issues") or [])
            if str(_snapshot_value(item, "code", "")) != "NO_ISSUES"
        ]
        series.append({
            "id": a.get("id"),
            "date": a.get("created_at"),
            # Headline functional metrics — None if not yet computed.
            "shoulder_flexion_deg": m.get("shoulder_flexion_deg"),
            "trunk_lean_deg": m.get("trunk_lean_deg"),
            "reach_completion": m.get("reach_completion"),
            "bilateral_symmetry": m.get("bilateral_symmetry"),
            "pinch_grip": m.get("pinch_grip"),
            "hand_opening": m.get("hand_opening"),
            "walking_skipped": bool(m.get("walking_skipped")),
            "issues_count": len(issues),
            "exercises_count": len(a.get("rehab_plan") or []),
        })
        for issue in issues:
            code = str(_snapshot_value(issue, "code", "") or "UNSPECIFIED")
            issues_count[code] = issues_count.get(code, 0) + 1
    return series, issues_count


@api_router.post("/alira/survey-report-viewed")
async def mark_survey_report_viewed(request: Request):
    """Record that the patient viewed their assessment report; from then on
    the Home next step directs to the rehab plan."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    viewed_at = datetime.now(timezone.utc).isoformat()
    await _save_user_fields(user, {"survey_report_viewed_at": viewed_at}, context="survey report view")
    _record_alira_action(
        "survey_report_viewed",
        source="survey_report",
        user_id=user["id"],
        status="completed",
        details={"viewed_at": viewed_at},
    )
    return {"ok": True, "viewed_at": viewed_at, "next_step": "rehab_plan"}


@api_router.get("/progress/summary")
async def progress_summary(request: Request):
    """Returns time-series functional metrics + exercise progress totals.
    Empty arrays when the user has never assessed."""
    user = await _user_from_header(dict(request.headers))
    if not user:
        return {"assessments": [], "exercises": [], "issues_history": [], "first_seen": None}
    assessments_raw = await _care_assessments_for_user(user["id"])
    assessments_raw.sort(key=lambda item: item.get("created_at", ""))
    series, issues_count = _assessment_progress_series(assessments_raw)
    return {
        "assessments": series,
        "issues_history": [{"issue": k, "count": v} for k, v in sorted(issues_count.items(), key=lambda x: -x[1])],
        "first_seen": series[0]["date"] if series else None,
        "count": len(series),
        # Spec section 6: patient-facing progress is activity-driven with
        # honest complete / estimated / not-assessed labels.
        "daily_activities": build_daily_activity_metrics(series, user.get("profile") or {}),
    }


async def _ensure_user_indexes() -> bool:
    """Index the account collection so sign-in lookups by id/email stay fast.

    Best effort: a missing Mongo at startup must not stop the API (the local
    fallback still serves development), and existing duplicate documents must
    not break deployment, so the email index is not made unique here.
    """
    try:
        await db.users.create_index("id", name="users_id")
        await db.users.create_index("email", name="users_email")
        await db.login_handoffs.create_index("token_hash", name="login_handoff_token", unique=True)
        await db.login_handoffs.create_index("expires_at", name="login_handoff_expiry", expireAfterSeconds=0)
        for collection in ("assessments", "alira_check_ins", "alira_care_reviews", "alira_functional_issue_reports"):
            await db[collection].create_index([("user_id", 1), ("created_at", -1)])
        await db.alira_activities.create_index([("user_id", 1), ("completed_at", -1)])
        await db.alira_activities.create_index([("user_id", 1), ("plan_id", 1), ("day", 1)])
        await db.assessment_task_progress.create_index([("user_id", 1), ("package_id", 1)])
        await db.chat_sessions.create_index([("user_id", 1), ("session_id", 1)])
        await db.exercise_repetitions.create_index([("user_id", 1), ("plan_id", 1), ("day", 1)])
        await db.journal_entries.create_index([("user_id", 1), ("createdAt", -1)])
        return True
    except Exception as exc:
        logger.warning(f"Could not create user indexes: {str(exc)[:120]}")
        return False


@app.on_event("startup")
async def startup_db_indexes():
    global _MONGO_RECOVERY_TASK
    if "pytest" in sys.modules:
        return
    _MONGO_RECOVERY_TASK = asyncio.create_task(_maintain_patient_database_connection())


@app.on_event("shutdown")
async def shutdown_db_client():
    for task in (_MONGO_RECOVERY_TASK, _MONGO_PROBE_TASK):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    client.close()


# Mount routes — MUST be last so all routes defined above (including Phase C)
# are registered.
app.include_router(api_router)
app.include_router(create_early_access_router(_mongo_database))
app.include_router(create_trial_access_router(_require_trial_access_code))
