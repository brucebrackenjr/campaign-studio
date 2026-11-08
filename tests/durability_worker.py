"""Real app subprocess with synthetic transport and explicit crash checkpoints."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import uvicorn

from campaign_studio import compositions
from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.provider import OpenAIImages
from campaign_studio.storage import Storage


def serve(root: Path, port: int, mode: str):
    # Explicit Settings and injected transport never load a .env or call a provider.
    os.environ.pop("OPENAI_API_KEY", None)

    async def transport(request):
        with (root / "dispatches.jsonl").open("a") as stream:
            stream.write(json.dumps({"path": request.url.path}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if mode == "intent":
            (root / "checkpoint").write_text("dispatch entered after durable intent")
            await asyncio.Event().wait()
        return httpx.Response(200, json=json.loads((root / "response.json").read_text()))

    if mode == "source_commit":

        def pause_before_source_commit(*args, **kwargs):
            (root / "checkpoint").write_text("source decoded; current document not committed")
            while True:
                time.sleep(0.1)

        compositions.finish_sources = pause_before_source_commit

    app = create_app(
        Settings(data_dir=root / "runtime", port=port),
        key_available=lambda: True,  # Synthetic transport availability, not a user key.
        provider_factory=lambda: OpenAIImages(
            "synthetic-test-credential", transport=httpx.MockTransport(transport)
        ),
    )

    if mode == "received":
        original_write = Storage.write

        def pause_after_response(storage, reference, payload):
            result = original_write(storage, reference, payload)
            if reference.endswith("/response.json"):
                (root / "checkpoint").write_text("raw response saved; usage not recorded")
                while True:
                    time.sleep(0.1)
            return result

        Storage.write = pause_after_response

    @app.middleware("http")
    async def hold_committed_response(request, call_next):
        response = await call_next(request)
        if mode == "response" and request.headers.get("x-test-hold-response") == "yes":
            assert response.status_code == 200
            (root / "checkpoint").write_text("save committed; response not delivered")
            await asyncio.Event().wait()
        return response

    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    serve(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3])
