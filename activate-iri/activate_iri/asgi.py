"""ASGI entry point: the reference framework app plus a root /openapi.json alias.

The DOE validation matrix and api-validator.py fetch <base>/openapi.json; the v2 framework
publishes the document at /api/v2/openapi.json. Serving both keeps every consumer working.
"""
from app.main import APP
from fastapi.responses import JSONResponse


@APP.get("/openapi.json", include_in_schema=False)
async def openapi_alias():
    return JSONResponse(APP.openapi())


@APP.get("/", include_in_schema=False)
async def root():
    return JSONResponse({"name": APP.title, "openapi": "/api/v2/openapi.json", "docs": "/api/v2", "facility": "/api/v2/facility"})


# The reference error handler answers every 405 with "Allow: GET, HEAD". The IRI validator
# checks that the Allow header matches the route's methods, so compute it from the route table.
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

_reference_405 = APP.exception_handlers.get(405) or APP.exception_handlers.get(StarletteHTTPException)


def _allowed_methods(scope) -> list[str]:
    methods: set[str] = set()
    for route in APP.router.routes:
        match, _ = route.matches(scope)
        if match in (Match.FULL, Match.PARTIAL) and getattr(route, "methods", None):
            methods |= set(route.methods)
    if "GET" in methods:
        methods.add("HEAD")
    return sorted(methods)


async def _method_not_allowed(request, exc):
    if getattr(exc, "status_code", None) == 405:
        allowed = _allowed_methods(request.scope)
        response = await _reference_405(request, exc) if _reference_405 else JSONResponse({"detail": "Method Not Allowed"}, status_code=405)
        if allowed:
            response.headers["Allow"] = ", ".join(allowed)
        return response
    return await _reference_405(request, exc)


APP.add_exception_handler(405, _method_not_allowed)
