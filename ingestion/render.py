"""Page images of outside PDFs, drawn apart from anything worth taking.

The workbench shows reviewers the pages of official PDFs fetched from government websites. pdftoppm
draws them, and a crafted PDF could take it over; drawn in the workbench, it would find the workbench's
database URLs, sign-in secrets and cloud token beside it. Deployed, the workbench sends the PDF to this
service instead and asks for one page (ALVARY_RENDER=service). The service holds no secrets and no role,
and on Google Cloud it has no way out to any network (deployment/gcp-terraform/env/render.tf); inside it,
pdftoppm still runs under the limits of ingestion.tools. It will not start with anything in its
environment named as this project names its secrets and settings.

    POST /pages?page=3&dpi=150   the PDF as the body   ->  200 image/jpeg
    GET  /healthz

Hypercorn serves it (deployment/render.Dockerfile), speaking HTTP/2 without TLS as Cloud Run sends it
end to end: over HTTP/1, Cloud Run takes no request larger than 32 MiB, and a Gazette issue can be.

`draw` is the same call where the workbench's own container is the sandbox (ALVARY_RENDER=direct), and
`fetch` is the workbench's side of the service.
"""

import os
import re
import subprocess
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from ingestion.tools import GIB, MIB, run_tool

DPI = 150  # legible, for comparing a reading with its page
LOWEST_DPI, HIGHEST_DPI = 36, 300
TIMEOUT = 120
# One page at 150 dpi, on an instance of 2 GiB: a file that wants more fails, and the instance stays up.
MEMORY = 3 * GIB // 2
WRITTEN = 64 * MIB  # /tmp is memory there too; no page image larger is taken either side
# Above the largest Gazette issue fetched so far, several times over.
LARGEST_PDF = 256 * MIB
JPEG = b"\xff\xd8\xff"

# Names this project gives its secrets and settings, and those clouds give credentials. The service needs
# none of them, so none may reach it: one added by mistake stops it starting rather than sitting beside
# pdftoppm.
SECRET = re.compile(
    r"^(ALVARY|OPEN_ALVARY|RESEND|AZURE|GOOGLE)_|SECRET|PASSWORD|TOKEN|PEPPER|CREDENTIAL|DATABASE|_KEYS?$",
    re.I,
)
# What the image and the platform set themselves. GPG_KEY is the Python image's: the id of the public
# key its Python was signed with. The rest are Container Apps' endpoint for an app's identities: the
# renderer's only identity pulls its image and is given to no container (deployment/azure/env/render.tf),
# so the endpoint has nothing to hand out.
PLATFORM = {"GPG_KEY", "IDENTITY_ENDPOINT", "IDENTITY_HEADER", "MSI_ENDPOINT", "MSI_SECRET"}


class Failed(RuntimeError):
    """The page could not be drawn, or what came back was not a page image."""


def draw(pdf: Path, page: int, out: Path, dpi: int = DPI) -> Path:
    """One page of `pdf` (numbered from 1) as a JPEG at `out`, by pdftoppm under the tool limits. A page
    the file does not have, or a file pdftoppm cannot read, raises CalledProcessError."""
    run_tool(
        ["pdftoppm", "-r", str(dpi), "-jpeg", "-jpegopt", "quality=85", "-singlefile"]
        + ["-f", str(page), "-l", str(page), str(pdf), str(out.with_suffix(""))],
        check=True,
        capture_output=True,
        timeout=TIMEOUT,
        memory=MEMORY,
        written=WRITTEN,
    )
    return out


def secrets_in(environ) -> list[str]:
    """The names in an environment that this service must not be given."""
    return sorted(name for name in environ if SECRET.search(name) and name not in PLATFORM)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if found := secrets_in(os.environ):
        raise RuntimeError(f"The page renderer holds no secrets or settings: remove {', '.join(found)}")
    yield


app = FastAPI(
    title="Open Alvary page renderer", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
)
# One page at a time: the memory limit is sized for one pdftoppm on the instance.
_one_at_a_time = threading.Lock()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/pages")
async def page_image(
    request: Request,
    page: int = Query(ge=1, le=100_000),
    dpi: int = Query(DPI, ge=LOWEST_DPI, le=HIGHEST_DPI),
) -> Response:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > LARGEST_PDF:
        raise HTTPException(413, f"A PDF of at most {LARGEST_PDF // MIB} MiB is drawn here")
    with tempfile.TemporaryDirectory(prefix="render-") as folder:
        pdf, out = Path(folder) / "in.pdf", Path(folder) / "page.jpg"
        size = 0
        with pdf.open("wb") as kept:
            async for chunk in request.stream():
                size += len(chunk)
                if size > LARGEST_PDF:
                    raise HTTPException(413, f"A PDF of at most {LARGEST_PDF // MIB} MiB is drawn here")
                kept.write(chunk)
        if not size:
            raise HTTPException(400, "Send the PDF as the request's body")
        try:
            await run_in_threadpool(_draw_alone, pdf, page, out, dpi)
        except subprocess.TimeoutExpired as late:
            raise HTTPException(504, f"The page took longer than {TIMEOUT} seconds") from late
        except subprocess.CalledProcessError as failed:
            raise HTTPException(
                422, f"pdftoppm could not draw page {page}: {said(failed.stderr)}"
            ) from failed
        if not out.is_file() or out.stat().st_size > WRITTEN:
            raise HTTPException(422, f"pdftoppm drew no image of page {page}")
        image = out.read_bytes()
    return Response(image, media_type="image/jpeg")


def _draw_alone(pdf: Path, page: int, out: Path, dpi: int) -> Path:
    with _one_at_a_time:
        return draw(pdf, page, out, dpi)


def said(printed: bytes | None) -> str:
    """The end of what a tool printed, as plain words, short enough to pass on."""
    words = (printed or b"").decode("utf-8", "replace")
    return " ".join("".join(c if c.isprintable() else " " for c in words).split())[-300:] or "no reason given"


def fetch(
    url: str, pdf: Path, page: int, out: Path, *, dpi: int = DPI, audience: str | None = None, client=None
) -> Path:
    """Page `page` of `pdf`, drawn by the service at `url`, written to `out`. With an `audience`, the request
    carries a Google ID token for it, as this instance's service account (Cloud Run's invoker check); on
    Azure the service is reachable only inside its environment and takes none. Whatever fails raises
    Failed: the image is never drawn here instead. `client` is an httpx.Client (a test's)."""
    import httpx  # the workbench's side only: the service's image has no HTTP client

    if pdf.stat().st_size > LARGEST_PDF:
        raise Failed(f"The page renderer draws PDFs of at most {LARGEST_PDF // MIB} MiB")
    headers = {"Content-Type": "application/pdf"}  # and its length, which httpx takes from the file
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(TIMEOUT + 60, connect=30))
    try:
        if audience:
            headers["Authorization"] = f"Bearer {id_token(audience)}"
        with pdf.open("rb") as body:
            answer = client.post(
                f"{url.rstrip('/')}/pages", params={"page": page, "dpi": dpi}, content=body, headers=headers
            )
    except httpx.HTTPError as error:
        raise Failed(f"The page renderer could not be reached: {type(error).__name__}") from error
    finally:
        if own:
            client.close()
    if answer.status_code != 200:
        raise Failed(f"The page renderer answered {answer.status_code}: {reason(answer)}")
    image = answer.content
    if not image.startswith(JPEG) or len(image) > WRITTEN:
        raise Failed("The page renderer answered with something other than a page image")
    partial = out.with_name(out.name + ".partial")
    partial.write_bytes(image)
    partial.replace(out)
    return out


def reason(answer) -> str:
    """What the service said was wrong (FastAPI's detail), in plain words."""
    try:
        found = answer.json()
    except ValueError:
        return "no reason given"
    return said(str(found.get("detail", "") if isinstance(found, dict) else "").encode())


def id_token(audience: str) -> str:
    """A Google ID token for `audience`, from the metadata server, as this instance's service account."""
    import httpx

    answer = httpx.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity",
        params={"audience": audience},
        headers={"Metadata-Flavor": "Google"},
        timeout=10,
    )
    answer.raise_for_status()
    return answer.text
