"""HTTP route handlers for the bot's internal API (#316).

Each module exposes plain ``async def handler(request)`` coroutines that are
registered onto the aiohttp router in ``api_server.build_app``. Credentialed
handlers wrap themselves in ``api.auth.requires_api_key``; ``healthz`` does not.
"""
