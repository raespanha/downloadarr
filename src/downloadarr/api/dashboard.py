import hmac
import json
import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
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
    form = await request.form()
    _require_ui_mutation(request, form)
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
        "csrf_token": request.app.state.auth_sessions.csrf(request.cookies.get("SID")),
    })


@router.get("/ui/api/jobs", dependencies=[Depends(require_auth)])
async def jobs(request: Request) -> JSONResponse:
    values = await request.app.state.job_service.jobs()
    return JSONResponse([_job_json(job) for job in values])


@router.get("/ui/api/performance", dependencies=[Depends(require_auth)])
async def performance(request: Request, range: str = "7d", service: str = "all",
                      indexer: str = "all") -> JSONResponse:
    now = datetime.now(timezone.utc)
    if range == "7d":
        since, bucket = now - timedelta(days=7), "day"
    elif range == "30d":
        since, bucket = now - timedelta(days=30), "day"
    elif range == "all":
        since, bucket = None, "month"
    else:
        raise HTTPException(status_code=400, detail="range must be 7d, 30d, or all")
    if len(service) > 32 or len(indexer) > 255:
        raise HTTPException(status_code=400, detail="invalid performance filter")
    values = await request.app.state.job_service.transfer_history(since)
    failures = await request.app.state.job_service.failure_history(since)
    available = {
        "services": sorted({item.service for item in [*values, *failures]}),
        "indexers": sorted({item.indexer for item in [*values, *failures]}),
    }
    if service != "all":
        values = [item for item in values if item.service == service]
        failures = [item for item in failures if item.service == service]
    if indexer != "all":
        values = [item for item in values if item.indexer == indexer]
        failures = [item for item in failures if item.indexer == indexer]
    return JSONResponse(_performance_json(
        values, failures, range, bucket, service, indexer, available))


@router.get("/ui/api/monitoring", dependencies=[Depends(require_auth)])
async def monitoring(request: Request, range: str = "7d", service: str = "all",
                     indexer: str = "all") -> JSONResponse:
    since, _ = _range(range)
    events = await request.app.state.job_service.lifecycle_history(
        since, service=None if service == "all" else service,
        indexer=None if indexer == "all" else indexer, limit=5000)
    alerts = await request.app.state.job_service.alerts()
    if service != "all":
        alerts = [item for item in alerts if item.service == service]
    if indexer != "all":
        alerts = [item for item in alerts if item.indexer == indexer]
    status = await request.app.state.job_service.monitor_status()
    phases = defaultdict(lambda: {"transitions": 0, "samples": 0, "wall_seconds": 0.0})
    for event in events:
        if event.event_type != "phase_transition" or not event.from_phase:
            continue
        item = phases[event.from_phase]
        item["transitions"] += 1
        if event.duration_seconds is not None and not event.partial_history:
            item["samples"] += 1
            item["wall_seconds"] += event.duration_seconds
    phase_rows = []
    for phase, item in sorted(phases.items()):
        phase_rows.append({"phase": phase, **item,
                           "average_wall_seconds": (item["wall_seconds"] / item["samples"]
                                                    if item["samples"] else None)})
    pause_started = {}
    paused_seconds = 0.0
    for event in events:
        if event.event_type == "control_pause":
            pause_started[event.job_id] = _aware_datetime(event.occurred_at)
        elif event.event_type == "control_resume" and event.job_id in pause_started:
            paused_seconds += max(0.0, (_aware_datetime(event.occurred_at)
                                       - pause_started.pop(event.job_id)).total_seconds())
    paused_seconds += sum(max(0.0, (datetime.now(timezone.utc) - value).total_seconds())
                          for value in pause_started.values())
    heartbeat = status.last_evaluated_at if status else None
    heartbeat_stale = (heartbeat is None or
                       datetime.now(timezone.utc) - _aware_datetime(heartbeat) > timedelta(seconds=90))
    return JSONResponse({
        "schema_version": 1, "range": range,
        "semantics": {"phase_time": "observed wall-clock time between Downloadarr polls; includes pause overlays",
                      "http_time": "transfer history elapsed is actual local HTTP session time",
                      "paused_time": "reported separately and never treated as a stalled alert",
                      "cleanup": "client cleanup request; Arr import is unverified"},
        "monitor": {"last_evaluated_at": heartbeat.isoformat() if heartbeat else None,
                    "stale": heartbeat_stale, "last_error": status.last_error if status else None},
        "summary": {"events": len(events), "paused_seconds": paused_seconds,
                    "open_incidents": sum(
            item.status != "resolved" for item in alerts),
                    "terminal_incidents": sum(item.rule == "terminal_failure"
                                              and item.status != "resolved" for item in alerts)},
        "phases": phase_rows,
        "alerts": [_alert_json(item) for item in alerts[:100]],
        "recent_events": [_event_json(item, include_identifiers=True)
                          for item in reversed(events[-100:])],
    }, headers={"Cache-Control": "no-store"})


@router.get("/ui/api/export", dependencies=[Depends(require_auth)])
async def export_telemetry(request: Request, dataset: str = "lifecycle",
                           format: str = "json", range: str = "30d",
                           service: str = "all", indexer: str = "all",
                           include_identifiers: bool = False,
                           limit: int | None = None) -> StreamingResponse:
    if dataset not in {"lifecycle", "transfers", "failures", "alerts"}:
        raise HTTPException(status_code=400, detail="invalid export dataset")
    if format not in {"json", "csv"}:
        raise HTTPException(status_code=400, detail="format must be json or csv")
    since, _ = _range(range)
    maximum = request.app.state.settings.telemetry.export_max_rows
    row_limit = min(maximum, max(1, limit or maximum))
    rows = await _export_rows(request, dataset, since, service, indexer,
                              include_identifiers, row_limit)
    suffix = "json" if format == "json" else "csv"
    headers = {"Content-Disposition": f'attachment; filename="downloadarr-{dataset}.{suffix}"',
               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if format == "json":
        payload = json.dumps({"schema_version": 1, "dataset": dataset,
                              "units": {"bytes": "bytes", "speed": "bytes_per_second",
                                        "timestamps": "UTC ISO-8601"},
                              "truncated": len(rows) >= row_limit, "rows": rows},
                             ensure_ascii=False, separators=(",", ":"))
        return StreamingResponse(iter([payload]), media_type="application/json", headers=headers)
    output = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({key: _csv_safe(value) for key, value in row.items()} for row in rows)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers=headers)


@router.post("/ui/alerts/{alert_id}/acknowledge", dependencies=[Depends(require_auth)])
async def acknowledge_alert(alert_id: str, request: Request) -> Response:
    form = await request.form()
    _require_ui_mutation(request, form)
    if not await request.app.state.job_service.acknowledge_alert(alert_id):
        raise HTTPException(status_code=404, detail="Alert not found")
    return RedirectResponse("/", status_code=303)


@router.get("/ui/api/retention", dependencies=[Depends(require_auth)])
async def retention_preview(request: Request) -> JSONResponse:
    return JSONResponse(await request.app.state.job_service.prune_telemetry(
        request.app.state.settings.telemetry.retention_days, dry_run=True),
        headers={"Cache-Control": "no-store"})


@router.post("/ui/jobs/{info_hash}/remove", dependencies=[Depends(require_auth)])
async def remove_job(info_hash: str, request: Request) -> Response:
    form = await request.form()
    _require_ui_mutation(request, form)
    delete_files = str(form.get("deleteFiles", "false")).lower() == "true"
    try:
        await request.app.state.job_service.remove([info_hash], delete_files, actor="dashboard")
    except (ProviderError, OSError, ValueError):
        return RedirectResponse("/?error=remove_failed", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.post("/ui/jobs/{info_hash}/{command}", dependencies=[Depends(require_auth)])
async def control_job(info_hash: str, command: str, request: Request) -> Response:
    if command not in {"pause", "resume", "retry"}:
        raise HTTPException(status_code=404, detail="Unknown control")
    form = await request.form()
    _require_ui_mutation(request, form)
    await getattr(request.app.state.job_service, command)([info_hash], actor="dashboard")
    return RedirectResponse("/", status_code=303)


@router.post("/ui/settings", dependencies=[Depends(require_auth)])
async def save_settings(request: Request) -> Response:
    form = await request.form()
    _require_ui_mutation(request, form)
    current: Settings = request.app.state.settings
    values = current.storage_dict()
    try:
        values["download"]["path"] = str(form.get("download_path", "")).strip()
        values["download"]["connections"] = int(str(form.get("connections", "")))
        values["download"]["provider_max_connections"] = int(
            str(form.get("provider_max_connections", "")))
        values["download"]["minimum_file_size_mb"] = int(
            str(form.get("minimum_file_size_mb", "0")))
        values["download"]["transfer_mode"] = str(form.get("transfer_mode", ""))
        values["telemetry"]["retention_days"] = int(str(form.get("retention_days", "0")))
        values["telemetry"]["export_max_rows"] = int(str(
            form.get("export_max_rows", "5000")))
        categories = json.loads(str(form.get("categories", "{}")))
        if not isinstance(categories, dict):
            raise ValueError("categories must be an object")
        values["download"]["categories"] = categories
        token = str(form.get("torbox_token", "")).strip()
        if token and token != "********":
            values["torbox"]["api_token"] = token
        for name in ("sonarr", "radarr"):
            if f"{name}_url" in form:
                values["integrations"][name]["url"] = str(
                    form.get(f"{name}_url", "")).strip()
            if f"{name}_category" in form:
                values["integrations"][name]["category"] = str(
                    form.get(f"{name}_category", "")).strip()
            api_key = str(form.get(f"{name}_api_key", "")).strip()
            if api_key and api_key != "********":
                values["integrations"][name]["api_key"] = api_key
        updated = Settings.model_validate(values)
        await request.app.state.settings_service.save(updated)
    except (OSError, TypeError, ValueError):
        return RedirectResponse("/?error=settings_invalid", status_code=303)
    return RedirectResponse("/?saved=1", status_code=303)


def _job_json(job: Job) -> dict:
    size = job.size or 0
    progress = min(max(job.progress, 0.0), 1.0)
    files = [{
        "name": item.relative_path,
        "size": item.size,
        "downloaded": min(item.size, item.downloaded),
        "progress": min(max(item.downloaded / item.size if item.size else 1.0, 0.0), 1.0),
        "state": item.state,
        "error": item.error_message,
    } for item in job.delivery_files]
    downloaded = min(size, sum(item["downloaded"] for item in files)) if files else min(
        size, int(size * progress))
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
        "control_state": job.control_state,
        "control_scope": job.control_scope,
        "control_error": job.control_error,
        "created_at": job.created_at.isoformat(),
        "files": files,
    }


def _performance_json(values, failures, selected_range: str, bucket: str,
                      selected_service: str, selected_indexer: str,
                      available: dict) -> dict:
    groups = defaultdict(list)
    for value in values:
        completed = value.completed_at
        key = (completed.strftime("%Y-%m") if bucket == "month"
               else completed.strftime("%Y-%m-%d"))
        groups[key].append(value)

    failure_groups = defaultdict(list)
    for value in failures:
        occurred = value.occurred_at
        key = (occurred.strftime("%Y-%m") if bucket == "month"
               else occurred.strftime("%Y-%m-%d"))
        failure_groups[key].append(value)

    timeline = []
    for label in sorted(set(groups) | set(failure_groups)):
        items = groups[label]
        failed = failure_groups[label]
        elapsed = sum(item.elapsed for item in items)
        transferred = sum(item.transferred_bytes for item in items)
        timeline.append({
            "label": label,
            "transfers": len(items),
            "bytes": sum(item.total_bytes for item in items),
            "average_speed": int(transferred / elapsed) if elapsed else 0,
            "peak_speed": max((item.peak_speed for item in items), default=0),
            "retries": sum(item.retry_count for item in items),
            "failures": len(failed),
            "unresolved_failures": sum(item.resolved_at is None for item in failed),
        })

    total_elapsed = sum(value.elapsed for value in values)
    total_transferred = sum(value.transferred_bytes for value in values)
    recent = [{
        "info_hash": value.info_hash,
        "name": value.name,
        "category": value.category,
        "service": value.service,
        "indexer": value.indexer,
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
    recent_failures = [{
        "info_hash": value.info_hash,
        "name": value.name,
        "category": value.category,
        "service": value.service,
        "indexer": value.indexer,
        "stage": value.stage,
        "code": value.error_code,
        "message": value.error_message,
        "transient": value.transient,
        "attempt": value.attempt,
        "bytes_downloaded": value.bytes_downloaded,
        "occurred_at": value.occurred_at.isoformat(),
        "resolved_at": value.resolved_at.isoformat() if value.resolved_at else None,
    } for value in reversed(failures[-20:])]
    return {
        "range": selected_range,
        "filters": {"service": selected_service, "indexer": selected_indexer},
        "available": available,
        "summary": {
            "downloads": len({value.job_id for value in values}),
            "files": len(values),
            "bytes": sum(value.total_bytes for value in values),
            "average_speed": int(total_transferred / total_elapsed) if total_elapsed else 0,
            "peak_speed": max((value.peak_speed for value in values), default=0),
            "median_speed": _percentile([value.average_speed for value in values], 0.5),
            "p95_speed": _percentile([value.average_speed for value in values], 0.95),
            "sample_files": len(values),
            "retries": sum(value.retry_count for value in values),
            "range_transfers": sum(bool(value.used_ranges) for value in values),
            "resumed": sum(bool(value.resumed) for value in values),
            "failures": len(failures),
            "failure_events": len(failures),
            "unresolved_failures": sum(value.resolved_at is None for value in failures),
            "affected_downloads": len({value.job_id for value in failures}),
        },
        "timeline": timeline,
        "recent": recent,
        "recent_failures": recent_failures,
        "segments": {
            "services": _segments(values, failures, "service"),
            "indexers": _segments(values, failures, "indexer"),
        },
    }


def _segments(values, failures, field: str) -> list[dict]:
    names = sorted({getattr(item, field) for item in [*values, *failures]})
    result = []
    for name in names:
        completed = [item for item in values if getattr(item, field) == name]
        failed = [item for item in failures if getattr(item, field) == name]
        elapsed = sum(item.elapsed for item in completed)
        transferred = sum(item.transferred_bytes for item in completed)
        result.append({
            "name": name,
            "downloads": len({item.job_id for item in completed}),
            "files": len(completed),
            "bytes": sum(item.total_bytes for item in completed),
            "average_speed": int(transferred / elapsed) if elapsed else 0,
            "peak_speed": max((item.peak_speed for item in completed), default=0),
            "failures": len(failed),
            "failure_events": len(failed),
            "unresolved_failures": sum(item.resolved_at is None for item in failed),
            "median_speed": _percentile([item.average_speed for item in completed], 0.5),
            "p95_speed": _percentile([item.average_speed for item in completed], 0.95),
        })
    return result


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Cross-origin form submission rejected")


def _require_ui_mutation(request: Request, form) -> None:
    _require_same_origin(request)
    sid = request.cookies.get("SID")
    expected = request.app.state.auth_sessions.csrf(sid)
    supplied = str(form.get("csrf_token", ""))
    if expected is None or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _range(value: str) -> tuple[datetime | None, str]:
    now = datetime.now(timezone.utc)
    if value == "7d":
        return now - timedelta(days=7), "day"
    if value == "30d":
        return now - timedelta(days=30), "day"
    if value == "all":
        return None, "month"
    raise HTTPException(status_code=400, detail="range must be 7d, 30d, or all")


def _aware_datetime(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _event_json(item, include_identifiers: bool = False) -> dict:
    result = {
        "sequence": item.sequence, "event_type": item.event_type,
        "from_phase": item.from_phase, "to_phase": item.to_phase,
        "outcome": item.outcome, "code": item.code,
        "service": item.service, "indexer": item.indexer,
        "progress": item.progress, "bytes_downloaded": item.bytes_downloaded,
        "duration_seconds": item.duration_seconds,
        "partial_history": bool(item.partial_history),
        "occurred_at": item.occurred_at.isoformat(),
    }
    if include_identifiers:
        result.update({"info_hash": item.info_hash, "name": item.name,
                       "category": item.category, "detail": item.detail})
    return result


def _alert_json(item) -> dict:
    return {"id": item.id, "rule": item.rule, "severity": item.severity,
            "status": item.status, "service": item.service, "indexer": item.indexer,
            "summary": item.summary, "action": item.action,
            "occurrences": item.occurrences,
            "first_seen_at": item.first_seen_at.isoformat(),
            "last_seen_at": item.last_seen_at.isoformat(),
            "acknowledged_at": (item.acknowledged_at.isoformat()
                                if item.acknowledged_at else None),
            "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None}


async def _export_rows(request: Request, dataset: str, since: datetime | None,
                       service: str, indexer: str, include_identifiers: bool,
                       limit: int) -> list[dict]:
    job_service = request.app.state.job_service
    if dataset == "lifecycle":
        values = await job_service.lifecycle_history(
            since, service=None if service == "all" else service,
            indexer=None if indexer == "all" else indexer, limit=limit)
        return [_event_json(item, include_identifiers) for item in values]
    if dataset == "transfers":
        values = await job_service.transfer_history(since, limit)
        if service != "all":
            values = [item for item in values if item.service == service]
        if indexer != "all":
            values = [item for item in values if item.indexer == indexer]
        rows = []
        for item in values[:limit]:
            row = {"service": item.service, "indexer": item.indexer,
                   "total_bytes": item.total_bytes,
                   "transferred_bytes": item.transferred_bytes,
                   "elapsed_seconds": item.elapsed,
                   "average_bytes_per_second": item.average_speed,
                   "peak_bytes_per_second": item.peak_speed,
                   "connections": item.connections, "retry_count": item.retry_count,
                   "resumed": bool(item.resumed), "cdn_host": item.cdn_host,
                   "completed_at": item.completed_at.isoformat()}
            if include_identifiers:
                row.update({"info_hash": item.info_hash, "name": item.name,
                            "category": item.category, "relative_path": item.relative_path})
            rows.append(row)
        return rows
    if dataset == "failures":
        values = await job_service.failure_history(since, limit)
        if service != "all":
            values = [item for item in values if item.service == service]
        if indexer != "all":
            values = [item for item in values if item.indexer == indexer]
        rows = []
        for item in values[:limit]:
            row = {"service": item.service, "indexer": item.indexer,
                   "stage": item.stage, "error_code": item.error_code,
                   "transient": bool(item.transient), "attempt": item.attempt,
                   "bytes_downloaded": item.bytes_downloaded,
                   "occurred_at": item.occurred_at.isoformat(),
                   "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None}
            if include_identifiers:
                row.update({"info_hash": item.info_hash, "name": item.name,
                            "category": item.category, "error_message": item.error_message})
            rows.append(row)
        return rows
    values = await job_service.alerts(limit)
    rows = [_alert_json(item) for item in values
            if (service == "all" or item.service == service)
            and (indexer == "all" or item.indexer == indexer)]
    return rows[:limit]


def _csv_safe(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip("\t\r\n ")
    if stripped.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return int(ordered[index])
