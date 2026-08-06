import hmac
import time
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from ..db.models import Job, JobState
from ..jobs.service import JobService
from ..magnets import MagnetError, parse_magnet
from .auth import require_auth

router = APIRouter()


def service(request: Request) -> JobService:
    return request.app.state.job_service


@router.post("/api/v2/auth/login")
async def login(request: Request) -> Response:
    form = await request.form()
    settings = request.app.state.settings
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    valid = hmac.compare_digest(username, settings.username) and hmac.compare_digest(
        password, settings.password.get_secret_value())
    if not valid:
        return PlainTextResponse("Fails.", status_code=403)
    sid = request.app.state.auth_sessions.create()
    response = PlainTextResponse("Ok.")
    response.set_cookie("SID", sid, httponly=True, samesite="strict")
    return response


@router.post("/api/v2/auth/logout", dependencies=[Depends(require_auth)])
async def logout(request: Request) -> Response:
    request.app.state.auth_sessions.remove(request.cookies.get("SID"))
    response = Response(status_code=200)
    response.delete_cookie("SID")
    return response


@router.get("/api/v2/app/webapiVersion")
async def webapi_version(request: Request) -> PlainTextResponse:
    return PlainTextResponse(request.app.state.settings.qbittorrent.webapi_version)


@router.get("/api/v2/app/version", dependencies=[Depends(require_auth)])
async def app_version(request: Request) -> PlainTextResponse:
    return PlainTextResponse(request.app.state.settings.qbittorrent.application_version)


@router.get("/api/v2/app/preferences", dependencies=[Depends(require_auth)])
async def preferences(request: Request) -> dict:
    return {"save_path": request.app.state.settings.download_path.as_posix(), "dht": True,
            "max_ratio_enabled": False, "max_ratio": -1, "max_ratio_act": 0,
            "max_seeding_time_enabled": False, "max_seeding_time": -1}


@router.get("/api/v2/app/buildInfo", dependencies=[Depends(require_auth)])
async def build_info() -> dict[str, str]:
    return {"qt": "6.7.2", "libtorrent": "2.0.10.0", "boost": "1.85.0",
            "openssl": "3.3.1", "bitness": "64"}


@router.get("/api/v2/app/defaultSavePath", dependencies=[Depends(require_auth)])
async def default_save_path(request: Request) -> PlainTextResponse:
    return PlainTextResponse(request.app.state.settings.download_path.as_posix())


@router.get("/api/v2/transfer/info", dependencies=[Depends(require_auth)])
async def transfer_info(job_service: JobService = Depends(service)) -> dict:
    jobs = await job_service.jobs()
    speed = sum(job.download_speed for job in jobs if job.state == JobState.DELIVERING.value)
    downloaded = sum(min(job.size or 0, int((job.size or 0) * job.progress)) for job in jobs)
    return {"connection_status": "connected", "dl_info_speed": speed,
            "dl_info_data": downloaded, "up_info_speed": 0, "up_info_data": 0,
            "dl_rate_limit": 0, "up_rate_limit": 0, "dht_nodes": 0}


@router.get("/api/v2/torrents/categories", dependencies=[Depends(require_auth)])
async def categories(job_service: JobService = Depends(service)) -> dict:
    values = await job_service.categories()
    return {item.name: {"name": item.name, "savePath": item.save_path} for item in values}


@router.post("/api/v2/torrents/createCategory", dependencies=[Depends(require_auth)])
async def create_category(request: Request, job_service: JobService = Depends(service)) -> Response:
    form = await request.form()
    name = str(form.get("category", ""))
    save_path = str(form.get("savePath") or request.app.state.settings.download_path.as_posix())
    try:
        await job_service.create_category(name, save_path)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return Response(status_code=200)


@router.post("/api/v2/torrents/add", dependencies=[Depends(require_auth)])
async def add_torrent(request: Request, job_service: JobService = Depends(service)) -> Response:
    form = await request.form()
    source = form.get("urls")
    if source is None or not isinstance(source, str):
        return PlainTextResponse("Fails.", status_code=400)
    # This vertical supports exactly one magnet. Binary torrent uploads follow next.
    sources = source.strip().splitlines()
    if len(sources) != 1:
        return PlainTextResponse("Fails.", status_code=400)
    source = sources[0]
    try:
        magnet = parse_magnet(source)
        await job_service.add_magnet(magnet, str(form.get("category")) if form.get("category") else None)
    except (MagnetError, ValueError):
        return PlainTextResponse("Fails.", status_code=400)
    return PlainTextResponse("Ok.")


@router.get("/api/v2/torrents/info", dependencies=[Depends(require_auth)])
async def torrents_info(request: Request, category: str | None = None, hashes: str | None = None,
                        job_service: JobService = Depends(service)) -> list[dict]:
    hash_values = hashes.split("|") if hashes else None
    jobs = await job_service.jobs(category, hash_values)
    return [_torrent_json(job, request.app.state.settings.download_path.as_posix()) for job in jobs]


@router.get("/api/v2/torrents/properties", dependencies=[Depends(require_auth)])
async def torrent_properties(request: Request, hash: str,
                             job_service: JobService = Depends(service)) -> dict:
    job = await job_service.job(hash)
    if job is None:
        raise HTTPException(status_code=404, detail="Torrent not found")
    save_path = job.category.save_path if job.category else request.app.state.settings.download_path.as_posix()
    return {"save_path": save_path, "creation_date": int(job.created_at.timestamp()),
            "completion_date": int(job.completed_at.timestamp()) if job.completed_at else -1,
            "addition_date": int(job.created_at.timestamp()),
            "total_downloaded": int((job.size or 0) * job.progress), "total_uploaded": 0,
            "seeds": 0, "peers": 0, "share_ratio": 0}


@router.get("/api/v2/torrents/files", dependencies=[Depends(require_auth)])
async def torrent_files(hash: str, job_service: JobService = Depends(service)) -> list[dict]:
    job = await job_service.job(hash)
    if job is None:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return [{"index": index, "name": item.relative_path, "size": item.size,
             "progress": item.downloaded / item.size if item.size else 1.0,
             "priority": 1, "is_seed": True, "availability": 1.0}
            for index, item in enumerate(job.delivery_files)]


def _torrent_json(job: Job, default_save_path: str) -> dict:
    size = job.size or 0
    completed = min(size, int(size * min(max(job.progress, 0), 1)))
    save_path = job.category.save_path if job.category else default_save_path
    name = job.name or job.info_hash
    state_map = {
        JobState.SUBMITTED.value: "metaDL",
        JobState.PROVIDER_QUEUED.value: "queuedDL",
        JobState.PROVIDER_DOWNLOADING.value: "downloading",
        JobState.RETRY_WAIT.value: "stalledDL",
        JobState.PROVIDER_READY.value: "queuedDL",
        JobState.DELIVERING.value: "downloading",
        JobState.COMPLETED.value: "stalledUP",
        JobState.FAILED.value: "error",
    }
    if len(job.delivery_files) == 1:
        content_path = str(PurePosixPath(save_path) / job.delivery_files[0].relative_path)
    elif job.delivery_files:
        root = PurePosixPath(job.delivery_files[0].relative_path).parts[0]
        content_path = str(PurePosixPath(save_path) / root)
    else:
        content_path = str(PurePosixPath(save_path) / name)
    return {"hash": job.info_hash, "name": name, "size": size,
            "progress": min(max(job.progress, 0), 1), "state": state_map[job.state],
            "category": job.category.name if job.category else "", "save_path": save_path,
            "content_path": content_path,
            "amount_left": size - completed, "completed": completed,
            "dlspeed": job.download_speed, "upspeed": 0,
            "eta": job.eta if job.eta is not None else 8640000,
            "ratio": 0, "added_on": int(job.created_at.timestamp()),
            "completion_on": int(job.completed_at.timestamp()) if job.completed_at else 0}


@router.post("/api/v2/torrents/delete", dependencies=[Depends(require_auth)])
async def delete_torrents(request: Request,
                          job_service: JobService = Depends(service)) -> Response:
    form = await request.form()
    raw_hashes = str(form.get("hashes", ""))
    if not raw_hashes:
        return PlainTextResponse("Fails.", status_code=400)
    if raw_hashes == "all":
        hashes = [job.info_hash for job in await job_service.jobs()]
    else:
        hashes = [value for value in raw_hashes.split("|") if value]
    delete_files = str(form.get("deleteFiles", "false")).lower() == "true"
    try:
        await job_service.remove(hashes, delete_files)
    except ValueError:
        return PlainTextResponse("Fails.", status_code=400)
    return Response(status_code=200)
