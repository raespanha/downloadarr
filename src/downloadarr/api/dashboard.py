import hmac
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..db.models import Job, JobState
from ..providers.base import ProviderError
from ..settings import Settings
from .auth import require_auth


router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))


@router.get("/ui/login")
async def login_page(request: Request):
    if request.app.state.auth_sessions.valid(request.cookies.get("SID")):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={
        "error": request.query_params.get("error"),
    })


@router.post("/ui/login")
async def login(request: Request) -> Response:
    _require_same_origin(request)
    form = await request.form()
    settings: Settings = request.app.state.settings
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    valid = hmac.compare_digest(username, settings.username) and hmac.compare_digest(
        password, settings.password.get_secret_value())
    if not valid:
        return RedirectResponse("/ui/login?error=invalid", status_code=303)
    sid = request.app.state.auth_sessions.create()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("SID", sid, httponly=True, samesite="strict")
    return response


@router.post("/ui/logout", dependencies=[Depends(require_auth)])
async def logout(request: Request) -> Response:
    _require_same_origin(request)
    request.app.state.auth_sessions.remove(request.cookies.get("SID"))
    response = RedirectResponse("/ui/login", status_code=303)
    response.delete_cookie("SID")
    return response


@router.get("/")
async def dashboard(request: Request):
    if not request.app.state.auth_sessions.valid(request.cookies.get("SID")):
        return RedirectResponse("/ui/login", status_code=303)
    settings: Settings = request.app.state.settings
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "settings": settings.masked(),
        "categories_json": json.dumps(settings.download.categories, indent=2),
        "managed_fields": request.app.state.settings_service.managed_fields(),
        "saved": request.query_params.get("saved") == "1",
        "error": request.query_params.get("error"),
    })


@router.get("/ui/api/jobs", dependencies=[Depends(require_auth)])
async def jobs(request: Request) -> JSONResponse:
    values = await request.app.state.job_service.jobs()
    return JSONResponse([_job_json(job) for job in values])


@router.get("/ui/api/performance", dependencies=[Depends(require_auth)])
async def performance(request: Request, range: str = "7d") -> JSONResponse:
    now = datetime.now(timezone.utc)
    if range == "7d":
        since, bucket = now - timedelta(days=7), "day"
    elif range == "30d":
        since, bucket = now - timedelta(days=30), "day"
    elif range == "all":
        since, bucket = None, "month"
    else:
        raise HTTPException(status_code=400, detail="range must be 7d, 30d, or all")
    values = await request.app.state.job_service.transfer_history(since)
    return JSONResponse(_performance_json(values, range, bucket))


@router.post("/ui/jobs/{info_hash}/remove", dependencies=[Depends(require_auth)])
async def remove_job(info_hash: str, request: Request) -> Response:
    _require_same_origin(request)
    form = await request.form()
    delete_files = str(form.get("deleteFiles", "false")).lower() == "true"
    try:
        await request.app.state.job_service.remove([info_hash], delete_files)
    except (ProviderError, OSError, ValueError):
        return RedirectResponse("/?error=remove_failed", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.post("/ui/settings", dependencies=[Depends(require_auth)])
async def save_settings(request: Request) -> Response:
    _require_same_origin(request)
    form = await request.form()
    current: Settings = request.app.state.settings
    values = current.storage_dict()
    try:
        values["download"]["path"] = str(form.get("download_path", "")).strip()
        values["download"]["connections"] = int(str(form.get("connections", "")))
        values["download"]["provider_max_connections"] = int(
            str(form.get("provider_max_connections", "")))
        values["download"]["transfer_mode"] = str(form.get("transfer_mode", ""))
        categories = json.loads(str(form.get("categories", "{}")))
        if not isinstance(categories, dict):
            raise ValueError("categories must be an object")
        values["download"]["categories"] = categories
        token = str(form.get("torbox_token", "")).strip()
        if token and token != "********":
            values["torbox"]["api_token"] = token
        updated = Settings.model_validate(values)
        await request.app.state.settings_service.save(updated)
    except (OSError, TypeError, ValueError):
        return RedirectResponse("/?error=settings_invalid", status_code=303)
    return RedirectResponse("/?saved=1", status_code=303)


def _job_json(job: Job) -> dict:
    size = job.size or 0
    progress = min(max(job.progress, 0.0), 1.0)
    downloaded = min(size, int(size * progress))
    phase = {
        JobState.SUBMITTED.value: "Submitting",
        JobState.PROVIDER_QUEUED.value: "Queued in TorBox",
        JobState.PROVIDER_DOWNLOADING.value: "TorBox downloading",
        JobState.PROVIDER_READY.value: "Preparing local download",
        JobState.DELIVERING.value: "Local downloading",
        JobState.RETRY_WAIT.value: "Retry scheduled",
        JobState.COMPLETED.value: "Completed",
        JobState.FAILED.value: "Failed",
    }.get(job.state, job.state)
    return {
        "hash": job.info_hash,
        "name": job.name or job.info_hash,
        "category": job.category.name if job.category else "",
        "state": job.state,
        "phase": phase,
        "size": size,
        "downloaded": downloaded,
        "progress": progress,
        "speed": job.download_speed,
        "eta": job.eta,
        "error": job.error_message,
        "created_at": job.created_at.isoformat(),
    }


def _performance_json(values, selected_range: str, bucket: str) -> dict:
    groups = defaultdict(list)
    for value in values:
        completed = value.completed_at
        key = (completed.strftime("%Y-%m") if bucket == "month"
               else completed.strftime("%Y-%m-%d"))
        groups[key].append(value)

    timeline = []
    for label, items in sorted(groups.items()):
        elapsed = sum(item.elapsed for item in items)
        transferred = sum(item.transferred_bytes for item in items)
        timeline.append({
            "label": label,
            "transfers": len(items),
            "bytes": sum(item.total_bytes for item in items),
            "average_speed": int(transferred / elapsed) if elapsed else 0,
            "peak_speed": max((item.peak_speed for item in items), default=0),
            "retries": sum(item.retry_count for item in items),
        })

    total_elapsed = sum(value.elapsed for value in values)
    total_transferred = sum(value.transferred_bytes for value in values)
    recent = [{
        "info_hash": value.info_hash,
        "name": value.name,
        "category": value.category,
        "file": value.relative_path,
        "bytes": value.total_bytes,
        "transferred_bytes": value.transferred_bytes,
        "elapsed": value.elapsed,
        "average_speed": value.average_speed,
        "peak_speed": value.peak_speed,
        "connections": value.connections,
        "used_ranges": value.used_ranges,
        "range_requests": value.range_requests,
        "retries": value.retry_count,
        "resumed": value.resumed,
        "cdn_host": value.cdn_host,
        "completed_at": value.completed_at.isoformat(),
    } for value in reversed(values[-20:])]
    return {
        "range": selected_range,
        "summary": {
            "downloads": len({value.job_id for value in values}),
            "files": len(values),
            "bytes": sum(value.total_bytes for value in values),
            "average_speed": int(total_transferred / total_elapsed) if total_elapsed else 0,
            "peak_speed": max((value.peak_speed for value in values), default=0),
            "retries": sum(value.retry_count for value in values),
            "range_transfers": sum(bool(value.used_ranges) for value in values),
            "resumed": sum(bool(value.resumed) for value in values),
        },
        "timeline": timeline,
        "recent": recent,
    }


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Cross-origin form submission rejected")
