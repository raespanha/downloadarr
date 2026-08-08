from aiohttp import ClientSession, web

from downloadarr.arr_metadata import ArrMetadataResolver
from downloadarr.settings import IntegrationsSettings


async def test_arr_history_enrichment_uses_exact_uppercase_download_id():
    seen = {}

    async def history(request):
        seen["download_id"] = request.query.get("downloadId")
        seen["api_key"] = request.headers.get("X-Api-Key")
        return web.json_response({"records": [{
            "eventType": "grabbed",
            "data": {"indexer": "1337x (Prowlarr)", "indexerId": "12"},
        }]})

    application = web.Application()
    application.router.add_get("/api/v3/history", history)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        configured = IntegrationsSettings.model_validate({
            "sonarr": {
                "url": f"http://127.0.0.1:{port}",
                "api_key": "secret",
                "category": "tv-sonarr",
            }
        })
        async with ClientSession() as session:
            resolver = ArrMetadataResolver(configured, session)
            result = await resolver.resolve("tv-sonarr", "abcdef1234")
        assert result.service == "sonarr"
        assert result.indexer == "1337x (Prowlarr)"
        assert result.indexer_id == 12
        assert seen == {"download_id": "ABCDEF1234", "api_key": "secret"}
    finally:
        await runner.cleanup()


async def test_disabled_or_unknown_arr_source_never_uses_network():
    configured = IntegrationsSettings()
    resolver = ArrMetadataResolver(configured)
    try:
        assert (await resolver.resolve("tv-sonarr", "abc")).service == "sonarr"
        assert (await resolver.resolve("manual", "abc")).service == "other"
    finally:
        await resolver.close()
