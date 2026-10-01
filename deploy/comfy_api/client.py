"""Minimal v2 wire client with deliberate credential and retry boundaries."""

from urllib.parse import urljoin, urlsplit

import aiohttp

from .bundle import validate_endpoint

MAX_BYTES = 100 * 1024 * 1024


class RemoteError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        if status in {401, 403}:
            message = (
                "Comfy rejected this credential or its endpoint access. Run the login command with an authorized key."
            )
        elif status == 402:
            message = "Comfy has insufficient credits for this request."
        elif code == "idempotency_key_reuse":
            message = "Comfy already claimed this submission key; recover the existing job, do not submit another."
        else:
            message = f"Comfy API request failed ({status}, {code})"
        super().__init__(message)


class V2Client:
    def __init__(self, endpoint, api_key="", session=None):
        self.endpoint = validate_endpoint(endpoint)
        self.origin = self._origin(self.endpoint)
        self.api_key = api_key
        self.session = session

    @staticmethod
    def _origin(value):
        url = urlsplit(value)
        return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)

    def link(self, value):
        # Host-relative follow-up links already include any mount prefix.
        url = urljoin(self.endpoint + "/", value)
        if self._origin(url) != self.origin or urlsplit(url).username or urlsplit(url).password:
            raise ValueError("Comfy returned a job/asset link outside the selected endpoint")
        return url

    async def json(self, method, path, **kwargs):
        url = self.link(path)
        headers = kwargs.pop("headers", {})
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        async with self.session.request(
            method, url, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=120), **kwargs
        ) as response:
            if response.status >= 300:
                try:
                    error = (await response.json()).get("error", {})
                    code = error.get("code", "request_failed") if isinstance(error, dict) else "request_failed"
                except (ValueError, aiohttp.ContentTypeError):
                    code = "request_failed"
                # Provider response text can contain secrets or infrastructure
                # details. Only a safe error code crosses the local boundary.
                if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 80:
                    code = "request_failed"
                raise RemoteError(response.status, code)
            return await response.json()

    async def upload(self, content, filename):
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename, content_type="image/png")
        form.add_field("content_type", "image/png")
        form.add_field("file_path", filename)
        asset = await self.json("POST", self.endpoint + "/api/v2/assets", data=form)
        return {"__type": "core/ASSET", "info": {"id": asset["id"]}}

    async def submit(self, workflow, key):
        # A single attempt. An ambiguous network/5xx outcome is recorded by the
        # caller. Reusing the key cannot recover a lost job ID in v2 today.
        return await self.json(
            "POST", self.endpoint + "/api/v2/jobs", headers={"Idempotency-Key": key}, json={"workflow": workflow}
        )

    async def download(self, path):
        url = self.link(path)
        for _ in range(5):
            headers = (
                {"Authorization": "Bearer " + self.api_key} if self.api_key and self._origin(url) == self.origin else {}
            )
            async with self.session.get(
                url, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    target = urljoin(url, response.headers.get("Location", ""))
                    parsed = urlsplit(target)
                    if (
                        not response.headers.get("Location")
                        or parsed.username
                        or parsed.password
                        or not (parsed.scheme == "https" or self._origin(target) == self.origin)
                    ):
                        raise ValueError("Invalid output storage redirect")
                    url = target
                    continue
                if response.status >= 300:
                    raise RemoteError(response.status, "output_download_failed")
                result = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    result.extend(chunk)
                    if len(result) > MAX_BYTES:
                        raise ValueError("Image output exceeds the bridge's 100 MiB limit")
                return bytes(result)
        raise ValueError("Too many output download redirects")
