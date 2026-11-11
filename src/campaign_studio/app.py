"""The local four-task interface with explicit mutation and provider boundaries."""

import asyncio
import secrets
import sqlite3
from contextlib import asynccontextmanager
from importlib.resources import files
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from campaign_studio import __version__
from campaign_studio.config import Settings
from campaign_studio.credentials import BODY_LIMIT, CredentialError, LocalCredentials
from campaign_studio.imaging import FontProblem
from campaign_studio.previews import DraftPreviews
from campaign_studio.provider import OpenAIImages
from campaign_studio.runner import Runner
from campaign_studio.state import State, StateError
from campaign_studio.storage import CapacityError, Storage, StorageError
from campaign_studio.validation import issue_messages, validation_issues
from campaign_studio.web import routes
from campaign_studio.workspace import ActionError, Workspace


def create_app(
    settings: Settings | None = None,
    *,
    provider_factory=None,
    key_available=None,
    credentials=None,
    seed_examples=False,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    if credentials is None:
        # Mocked/provider-free services cannot write the normal launch .env.
        destination = (
            settings._credential_file
            if provider_factory is None and key_available is None
            else None
        )
        credentials = LocalCredentials(
            destination, managed_by_environment=settings._credential_environment
        )
    storage = Storage(
        settings.data_dir,
        location_notice=settings.location_notice,
        budget_bytes=settings.storage_budget_bytes,
        reserve_bytes=settings.disk_reserve_bytes,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        fresh = not storage.root.exists() or not any(storage.root.iterdir())
        initialized = storage.path("private/state.sqlite3").is_file()
        state = State(storage)
        from campaign_studio.cleanup import Cleanup

        await asyncio.to_thread(Cleanup(state).resume_all)
        app.state.storage = storage
        app.state.workspace = Workspace(state)
        app.state.runner = Runner(state)
        app.state.previews = DraftPreviews(state)
        app.state.operations = {}
        app.state.operation_errors = {}
        app.state.operation_times = {}
        app.state.csrf = secrets.token_urlsafe(32)
        app.state.credentials = credentials
        app.state.provider_factory = provider_factory or (
            lambda: OpenAIImages(credentials.current_key())
        )
        app.state.key_available = key_available or (lambda: credentials.status()["available"])
        if seed_examples:
            from campaign_studio.bundled import install_campaigns

            await install_campaigns(
                app.state.workspace,
                product_limit=settings.product_limit,
                allow_new=fresh or initialized,
            )
        yield
        active = [task for task in app.state.operations.values() if not task.done()]
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        for job in list(state.__dict__.get("delivery_jobs", {}).values()):
            job.cancel()

    async def campaign_identity(request: Request):
        campaign_id = request.path_params.get("campaign_id")
        if campaign_id is not None:
            from campaign_studio.cleanup import (
                UPGRADE_RECOVERY_MESSAGE,
                require_campaign_registered,
                upgrade_pending,
            )

            storage = request.app.state.storage
            require_campaign_registered(storage, campaign_id)
            if not request.url.path.startswith(f"/api/campaigns/{campaign_id}/cleanup/"):
                with storage.connect() as db:
                    if upgrade_pending(db, campaign_id):
                        raise StateError(UPGRADE_RECOVERY_MESSAGE, code="workspace_upgrade")

    app = FastAPI(
        title="Motif – Campaign Studio",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(campaign_identity)],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        expected_origin = f"http://{request.headers.get('host', '')}"
        # The README links to these two pages. An ordinary top-level visit can
        # come from another site, while the page's own API calls are same-origin.
        document_link = (
            request.method == "GET"
            and request.url.path in {"/", "/health"}
            and request.headers.get("sec-fetch-mode") == "navigate"
            and request.headers.get("sec-fetch-dest") == "document"
        )
        if (origin is not None and origin != expected_origin) or (
            request.headers.get("sec-fetch-site") == "cross-site" and not document_link
        ):
            return JSONResponse(
                {"code": "origin", "message": "Cross-origin access is not allowed."},
                status_code=403,
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            token = request.headers.get("x-campaign-token", "")
            if not token or not secrets.compare_digest(token, app.state.csrf):
                return JSONResponse(
                    {"code": "session", "message": "Refresh this page before taking an action."},
                    status_code=403,
                )
            uploading = request.url.path in {"/api/uploads", "/api/uploads/validate"}
            adding_photo = request.url.path.startswith(
                "/api/motif/campaigns/"
            ) and request.url.path.endswith("/products/photo")
            content_type = request.headers.get("content-type", "").split(";", 1)[0]
            # Upload bytes have their own bounded decoder and actual-type check.
            # Browser MIME is only a hint; it cannot reject valid supported artwork.
            if not uploading and content_type != "application/json":
                return JSONResponse(
                    {"code": "content_type", "message": "Send a JSON action from this workspace."},
                    status_code=415,
                )
            authored = request.url.path.startswith(
                "/api/motif/campaigns/"
            ) and request.url.path.endswith("/authored")
            limit = (
                BODY_LIMIT
                if request.url.path == "/api/openai-key"
                else ((20 * 1024 * 1024 + 2) // 3) * 4 + 131072
                if adding_photo
                else 20 * 1024 * 1024
                if uploading
                else 1_048_576
                if authored
                else 131072
            )
            received = bytearray()
            async for chunk in request.stream():
                received.extend(chunk)
                if len(received) > limit:
                    break
            request._body = bytes(received)
            if len(received) > limit:
                return JSONResponse(
                    {
                        "code": "action_size",
                        **(
                            {
                                "input_image": {
                                    "kind": "encoded_bytes",
                                    "limit_bytes": limit,
                                    "received_bytes": len(received),
                                }
                            }
                            if uploading
                            else {}
                        ),
                        "message": "Choose an image up to 20 MiB."
                        if uploading
                        else "This action is too large. Shorten the supplied fields.",
                    },
                    status_code=413,
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob:; font-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        # The portable report intentionally contains its own CSS and no script.
        if request.url.path.startswith("/reports/") and response.status_code == 200:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'"
            )
        # Only successful, explicitly versioned visual responses opt in. Mutable
        # state, errors, credentials, and request accounting remain uncached.
        visual = (
            request.method == "GET"
            and response.status_code == 200
            and "/composition/" in request.url.path
            and any(
                part in request.url.path
                for part in ("/images/", "/thumbnails/", "/source-thumbnails/")
            )
            and response.headers.get("content-type", "").startswith("image/")
        )
        if not visual:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ActionError)
    async def action_error(request, error):
        if request.method == "GET" and not request.url.path.startswith("/api/"):
            current_images = None
            campaign_id = request.path_params.get("campaign_id")
            if error.code == "retired_revision" and campaign_id:
                try:
                    run = app.state.workspace.run_for(campaign_id)
                except ActionError:
                    run = None  # Pending deletion returns to the workbench.
                if run:
                    current_images = f"/campaigns/{quote(campaign_id, safe='')}/studio"
            return templates.TemplateResponse(
                request=request,
                name="motif-error.html",
                context={
                    "message": error.message,
                    "not_found": error.status == 404,
                    "current_images": current_images,
                },
                status_code=error.status,
            )
        content = {"code": error.code, "message": error.message}
        if error.field_errors:
            content["field_errors"] = error.field_errors
        if error.revision_number is not None:
            content["revision_number"] = error.revision_number
        if error.input_image is not None:
            content["input_image"] = error.input_image
        if error.proposal is not None:
            content["proposal"] = error.proposal
        if error.project_state is not None:
            content["project_state"] = error.project_state
        return JSONResponse(content, status_code=error.status)

    @app.exception_handler(CredentialError)
    async def credential_error(request, error):
        return JSONResponse({"code": error.code, "message": str(error)}, status_code=error.status)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        campaign_not_found = False
        pieces = request.url.path.split("/")
        if error.status_code == 404 and len(pieces) > 2 and pieces[1] == "campaigns":
            from campaign_studio.cleanup import require_campaign_registered

            try:
                require_campaign_registered(request.app.state.storage, pieces[2])
            except StateError as missing:
                campaign_not_found = missing.code == "not_found"
        if (
            error.status_code == 404
            and request.method == "GET"
            and not request.url.path.startswith("/api/")
        ):
            return templates.TemplateResponse(
                request=request,
                name="motif-error.html",
                context={"not_found": True, "campaign_not_found": campaign_not_found},
                status_code=404,
            )
        return await http_exception_handler(request, error)

    @app.exception_handler(CapacityError)
    async def capacity_error(request, error):
        return JSONResponse(
            {"code": "storage_capacity", "message": str(error), "capacity": error.details},
            status_code=409,
        )

    @app.exception_handler(FontProblem)
    async def font_error(request, error):
        return JSONResponse({"code": "font_unavailable", "message": str(error)}, status_code=503)

    @app.exception_handler(ValueError)
    async def input_error(request, error):
        return JSONResponse(
            {
                "code": "invalid_action",
                "message": "The action could not be read. Refresh the page and check its fields.",
            },
            status_code=400,
        )

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request, error):
        details = validation_issues(error)
        return JSONResponse(
            {
                "code": "validation",
                "message": "Check the supplied fields.",
                "fields": issue_messages(details),
                "field_errors": details,
            },
            status_code=422,
        )

    @app.exception_handler(StateError)
    @app.exception_handler(StorageError)
    async def state_error(request, error):
        if error.code == "workspace_upgrade" and not request.url.path.startswith("/api/"):
            return templates.TemplateResponse(
                request=request,
                name="motif-error.html",
                context={"message": str(error), "not_found": False},
                status_code=409,
            )
        if error.code == "stale_report" and request.url.path.startswith("/reports/preview/"):
            return templates.TemplateResponse(
                request=request,
                name="motif-error.html",
                context={
                    "message": str(error),
                    "not_found": False,
                    "current_images": f"/campaigns/{request.path_params['campaign_id']}/handoff",
                },
                status_code=409,
            )
        if error.code == "report_not_found":
            return templates.TemplateResponse(
                request=request,
                name="motif-error.html",
                context={"not_found": True, "report_not_found": True},
                status_code=404,
            )
        if error.code == "not_found":
            if request.method == "GET" and not request.url.path.startswith("/api/"):
                return templates.TemplateResponse(
                    request=request,
                    name="motif-error.html",
                    context={"not_found": True, "campaign_not_found": True},
                    status_code=404,
                )
            return JSONResponse({"code": "not_found", "message": str(error)}, status_code=404)
        return JSONResponse({"code": error.code, "message": str(error)}, status_code=409)

    @app.exception_handler(OSError)
    @app.exception_handler(sqlite3.Error)
    async def local_error(request, error):
        return JSONResponse(
            {
                "code": "local_storage",
                "message": (
                    "The local operation could not finish. Check storage space and permissions. "
                    "Saved work is preserved."
                ),
            },
            status_code=503,
        )

    resources = files("campaign_studio")
    templates = Jinja2Templates(directory=str(resources.joinpath("templates")))
    app.mount("/static", StaticFiles(directory=str(resources.joinpath("static"))), name="static")
    from campaign_studio.motif import routes as motif_routes

    app.include_router(motif_routes(templates, settings))
    app.include_router(routes())

    @app.get("/health")
    async def health():
        return {"status": "ok", "stage": "workflow"}

    return app
