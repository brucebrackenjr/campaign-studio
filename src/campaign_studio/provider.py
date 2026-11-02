"""Explicitly injected OpenAI image transport. No environment credentials or retries."""

import asyncio
import base64
import io
import json
from contextlib import suppress
from dataclasses import dataclass

import httpx
from openai import APIStatusError, AsyncOpenAI

from campaign_studio.generation import GenerationRequest
from campaign_studio.imaging import ImageProblem, load_image
from campaign_studio.storage import Storage, StorageError


@dataclass(frozen=True)
class Receipt:
    request_id: str | None


class ProviderFailure(Exception):
    def __init__(self, outcome: str, kind: str):
        super().__init__(kind)
        self.outcome = outcome
        self.kind = kind


def status_failure_kind(error: APIStatusError) -> str:
    """Persist only recognized limit codes, never a provider's diagnostic text."""
    allowed = {
        "project_spend_limit_exceeded",
        "organization_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "credit_balance_exhausted",
        "insufficient_quota",
        "rate_limit_error",
        "rate_limit_exceeded",
    }
    if error.status_code == 429:
        try:
            body = error.response.json()
        except ValueError:
            body = None
        detail = body.get("error") if isinstance(body, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if isinstance(code, str) and code in allowed:
            return code
    return f"http_{error.status_code}"


class OpenAIImages:
    """A call is possible only when its owner supplies an explicit credential.

    The confirmed application action supplies its local credential. Tests inject
    a fake credential and mocked HTTP transport; construction itself never
    checks credentials or makes an image request.
    """

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise ValueError("An explicit credential is required.")
        self._api_key = api_key
        self.transport = transport

    async def generate(
        self, request: GenerationRequest, storage: Storage, response_ref: str
    ) -> Receipt:
        network_timeout = httpx.Timeout(connect=10, read=60, write=30, pool=10)
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.transport,
                    timeout=network_timeout,
                    trust_env=False,
                    follow_redirects=False,
                ) as http,
                AsyncOpenAI(
                    api_key=self._api_key,
                    admin_api_key="",
                    organization="",
                    project="",
                    webhook_secret="",
                    base_url="https://api.openai.com/v1",
                    http_client=http,
                    max_retries=request.max_retries,
                    timeout=network_timeout,
                ) as client,
            ):
                async with asyncio.timeout(request.deadline_seconds):
                    response = await client.images.with_raw_response.generate(
                        model=request.model,
                        prompt=request.prompt,
                        n=request.n,
                        size=request.size,
                        quality=request.quality,
                        background=request.background,
                        output_format=request.output_format,
                    )
                    # Persist bytes before parsing, decoding, or composing anything.
                    storage.write(response_ref, response.content)
                    return Receipt(request_id=response.request_id)
        except APIStatusError as error:
            # A diagnostic-write failure cannot turn a known HTTP refusal into
            # a saved-image recovery claim. Record the refusal in either case.
            with suppress(StorageError, OSError):
                storage.write(response_ref, error.response.content)
            raise ProviderFailure("failed", status_failure_kind(error)) from None
        except TimeoutError:
            raise ProviderFailure("unknown", "overall_deadline") from None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Request text, keys, provider diagnostics, and headers never enter status output.
            raise ProviderFailure("unknown", type(error).__name__) from None


def read_saved_response(storage: Storage, response_ref: str) -> dict:
    """Read returned metadata independently of whether its image can be processed."""
    payload = storage.path(response_ref).read_bytes()
    try:
        body = json.loads(payload)
        if not isinstance(body, dict):
            raise ValueError("The saved response must be an object.")
        return body
    except ValueError as error:
        raise ImageProblem("The saved response could not be read for local recovery.") from error


def recover_source(storage: Storage, body: dict, source_ref: str) -> None:
    """Decode saved bytes locally, after the caller has recorded returned usage."""
    try:
        if "error" in body:
            raise ImageProblem("The saved response is not a successful image response.")
        images = body.get("data")
        if (
            not isinstance(images, list)
            or len(images) != 1
            or not isinstance(images[0], dict)
            or not isinstance(images[0].get("b64_json"), str)
        ):
            raise ImageProblem("The saved response must contain one base64 image.")
        payload = base64.b64decode(images[0]["b64_json"], validate=True)
        image, kind = load_image(io.BytesIO(payload))
        try:
            if kind != "PNG" or image.size != (1920, 1920):
                raise ImageProblem("The saved source does not match the planned PNG master size.")
            if "A" in image.getbands() and image.getchannel("A").getextrema()[0] < 255:
                raise ImageProblem("The planned master must be opaque.")
        finally:
            image.close()
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise ImageProblem("The saved response does not contain a usable image.") from error

    # Validate returned bytes before publication; a storage failure remains recoverable.
    storage.write(source_ref, payload)
