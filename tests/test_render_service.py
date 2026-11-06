"""The page renderer (ingestion.render): the workbench sends a PDF and asks for a page, the service draws it
with pdftoppm under the tool limits and holds no secret. The PDFs here are made by the tests, some broken
on purpose; none comes from outside."""

import io
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from ingestion import render
from ingestion.tools import MIB
from workbench import content

ROOT = Path(__file__).resolve().parents[1]
URL = "http://render.test"
POPPLER = shutil.which("pdftoppm")
needs_poppler = pytest.mark.skipif(POPPLER is None, reason="pdftoppm is not installed")


def made_pdf(pages: int = 2, width: int = 144, height: int = 72) -> bytes:
    """A small PDF of our own: each page a black box, drawn without fonts."""
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=width, height=height)
        drawing = DecodedStreamObject()
        drawing.set_data(b"0 g 20 20 60 40 re f")
        page[NameObject("/Contents")] = writer._add_object(drawing)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def jpeg_size(data: bytes) -> tuple[int, int]:
    """A JPEG's width and height, from its frame header."""
    at = 2
    while at < len(data):
        marker, length = data[at + 1], int.from_bytes(data[at + 2 : at + 4])
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[at + 7 : at + 9]), int.from_bytes(data[at + 5 : at + 7])
        at += 2 + length
    raise ValueError("no frame header")


def fake_pdftoppm(folder: Path, body: str) -> None:
    """A pdftoppm of the test's own, first on PATH; its last argument is where the image goes."""
    tool = folder / "pdftoppm"
    tool.write_text("#!/bin/sh\nfor last; do :; done\n" + body + "\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def service():
    """The renderer as a client of the workbench's would reach it, in this process (not started, so the
    test's own environment is not checked: see the start-up tests)."""
    return TestClient(render.app, base_url=URL)


@pytest.fixture
def faked(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    return tmp_path


def ask(service, pdf: bytes, page: int = 1, **params) -> httpx.Response:
    return service.post("/pages", params={"page": page, **params}, content=pdf)


@needs_poppler
def test_the_workbench_gets_the_page_it_asked_for_at_the_resolution_it_asked_for(tmp_path, service):
    pdf = tmp_path / "issue.pdf"
    pdf.write_bytes(made_pdf(pages=3))
    out = render.fetch(URL, pdf, 2, tmp_path / "page-2.jpg", dpi=72, client=service)
    assert out.read_bytes().startswith(render.JPEG)
    assert jpeg_size(out.read_bytes()) == (144, 72)  # points at 72 dpi
    render.fetch(URL, pdf, 3, tmp_path / "page-3.jpg", client=service)
    assert jpeg_size((tmp_path / "page-3.jpg").read_bytes()) == (300, 150)  # the workbench's 150 dpi
    assert not list(tmp_path.glob("*.partial"))


@needs_poppler
def test_a_page_the_file_does_not_have_is_refused_with_pdftoppms_reason(tmp_path, service):
    answer = ask(service, made_pdf(pages=2), page=5)
    assert answer.status_code == 422 and "could not draw page 5" in answer.json()["detail"]
    pdf = tmp_path / "issue.pdf"
    pdf.write_bytes(made_pdf(pages=2))
    with pytest.raises(render.Failed, match="answered 422"):
        render.fetch(URL, pdf, 5, tmp_path / "page-5.jpg", client=service)
    assert not (tmp_path / "page-5.jpg").exists()


@needs_poppler
@pytest.mark.parametrize(
    "broken",
    [b"%PDF-1.7\n" + bytes(range(256)) * 40, b"not a PDF at all"],
    ids=["no-objects", "not-a-pdf"],
)
def test_a_file_pdftoppm_cannot_read_is_refused_and_the_service_goes_on(service, broken):
    answer = ask(service, broken)
    assert answer.status_code == 422 and answer.json()["detail"].startswith("pdftoppm could not draw page 1")
    assert ask(service, made_pdf()).status_code == 200


# A page tree that is its own child, which a reader following it would never leave, and a page whose
# drawing never closes what it opens.
LOOP = (
    b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [2 0 R] /Count 1 >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
)
UNCLOSED = (
    b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 144 72] /Contents 4 0 R >> endobj\n"
    b"4 0 obj << /Length 999999 >> stream\nq q q 0 g 0 0 m 10 10 l BT (\nendstream endobj\n"
    b"trailer << /Root 1 0 R >>\n%%EOF\n"
)


@needs_poppler
def test_a_damaged_pdf_is_drawn_or_refused_and_the_service_goes_on(service):
    whole = made_pdf(pages=4)
    for broken in [
        LOOP,
        UNCLOSED,
        *(whole[:cut] for cut in (len(whole) // 3, len(whole) // 2, len(whole) - 40)),
    ]:
        answer = ask(service, broken)
        assert answer.status_code in (200, 422)
        assert answer.status_code == 422 or answer.content.startswith(render.JPEG)
    assert ask(service, made_pdf()).status_code == 200


def test_the_request_is_checked_before_anything_is_drawn(service, faked):
    fake_pdftoppm(faked, 'echo drawn > "$last.jpg"; touch ' + str(faked / "ran"))
    assert ask(service, b"").status_code == 400
    assert ask(service, b"%PDF", page=0).status_code == 422
    assert ask(service, b"%PDF", dpi=301).status_code == 422
    assert ask(service, b"%PDF", dpi=35).status_code == 422
    assert service.post("/pages", content=b"%PDF").status_code == 422  # no page named
    assert not (faked / "ran").exists()


def test_a_pdf_larger_than_the_limit_is_refused_by_its_length_or_as_it_arrives(service, faked, monkeypatch):
    fake_pdftoppm(faked, "touch " + str(faked / "ran"))
    monkeypatch.setattr(render, "LARGEST_PDF", MIB)
    assert ask(service, b"%" * (MIB + 1)).status_code == 413

    def sent_without_a_length():
        for _ in range(3):
            yield b"%" * (MIB // 2)

    answer = service.post("/pages", params={"page": 1}, content=sent_without_a_length())
    assert answer.status_code == 413
    assert not (faked / "ran").exists()


def test_the_workbench_does_not_send_a_pdf_the_service_would_refuse(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "LARGEST_PDF", MIB)
    pdf = tmp_path / "issue.pdf"
    pdf.write_bytes(b"%" * (MIB + 1))

    def never(request):
        raise AssertionError("nothing is sent")

    with pytest.raises(render.Failed, match="at most 1 MiB"):
        render.fetch(
            URL, pdf, 1, tmp_path / "page-1.jpg", client=httpx.Client(transport=httpx.MockTransport(never))
        )


def test_a_page_that_takes_too_long_is_stopped_and_said_so(tmp_path, service, faked, monkeypatch):
    fake_pdftoppm(faked, "exec sleep 30")
    monkeypatch.setattr(render, "TIMEOUT", 1)
    answer = ask(service, made_pdf())
    assert answer.status_code == 504 and "longer than 1 seconds" in answer.json()["detail"]
    pdf = tmp_path / "issue.pdf"
    pdf.write_bytes(made_pdf())
    with pytest.raises(render.Failed, match="answered 504"):
        render.fetch(URL, pdf, 1, tmp_path / "page-1.jpg", client=service)


def test_an_image_larger_than_the_limit_is_cut_off_and_never_returned(service, faked, monkeypatch):
    # The write limit stops the tool part way; what it wrote is not passed on as a page.
    fake_pdftoppm(faked, f'printf "\\377\\330\\377"> "$last.jpg"; head -c {4 * MIB} /dev/zero >> "$last.jpg"')
    monkeypatch.setattr(render, "WRITTEN", MIB)
    answer = ask(service, made_pdf())
    assert answer.status_code == 422 and not answer.content.startswith(render.JPEG)


def test_pdftoppm_in_the_service_gets_only_what_a_tool_needs_of_the_environment(service, faked, monkeypatch):
    # Even were the service given a secret, the tool would not be (ingestion.tools).
    monkeypatch.setenv("ALVARY_REVIEW_DATABASE_URL", "postgresql+psycopg://review:a-secret@db/review")
    fake_pdftoppm(faked, 'printf "\\377\\330\\377" > "$last.jpg"; env >> "$last.jpg"')
    answer = ask(service, made_pdf())
    assert answer.status_code == 200 and b"PATH=" in answer.content and b"a-secret" not in answer.content


def test_what_the_workbench_takes_back_must_be_a_page_image(tmp_path):
    pdf = tmp_path / "issue.pdf"
    pdf.write_bytes(made_pdf())
    for answer in (
        httpx.Response(200, content=b"<script>not an image</script>"),
        httpx.Response(200, content=render.JPEG + b"x" * render.WRITTEN),
        httpx.Response(500, text="Internal Server Error"),
    ):
        client = httpx.Client(transport=httpx.MockTransport(lambda request, answer=answer: answer))
        with pytest.raises(render.Failed):
            render.fetch(URL, pdf, 1, tmp_path / "page-1.jpg", client=client)
    assert not (tmp_path / "page-1.jpg").exists()


def test_an_unreachable_renderer_fails_the_page(tmp_path):
    pdf = tmp_path / "issue.pdf"
    pdf.write_bytes(made_pdf())

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(render.Failed, match="could not be reached: ConnectError"):
        render.fetch(
            URL, pdf, 1, tmp_path / "page-1.jpg", client=httpx.Client(transport=httpx.MockTransport(refused))
        )


def test_on_google_cloud_the_request_carries_an_id_token_for_the_renderer(tmp_path, monkeypatch):
    pdf = tmp_path / "issue.pdf"
    pdf.write_bytes(made_pdf())
    asked, seen = [], []
    monkeypatch.setattr(render, "id_token", lambda audience: asked.append(audience) or "id-token")

    def renderer(request):
        seen.append((request.headers.get("authorization"), request.url.params["page"], request.read()))
        return httpx.Response(200, content=render.JPEG + b"page")

    client = httpx.Client(transport=httpx.MockTransport(renderer))
    render.fetch(
        "https://render.example",
        pdf,
        4,
        tmp_path / "page-4.jpg",
        audience="https://render.example",
        client=client,
    )
    assert asked == ["https://render.example"]
    assert seen == [("Bearer id-token", "4", pdf.read_bytes())]


def test_the_id_token_comes_from_the_metadata_server(monkeypatch):
    asked = []

    def metadata(url, params, headers, timeout):
        asked.append((url, params, headers))
        return httpx.Response(200, text="id-token", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", metadata)
    assert render.id_token("https://render.example") == "id-token"
    [(url, params, headers)] = asked
    assert url.startswith("http://metadata.google.internal/") and url.endswith(
        "/service-accounts/default/identity"
    )
    assert params == {"audience": "https://render.example"} and headers == {"Metadata-Flavor": "Google"}


# --- the service holds no secret ----------------------------------------------------------------------

# What the platforms and the image put in the environment themselves.
PLATFORM = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/render",
    "HOSTNAME": "render-1",
    "LANG": "C.UTF-8",
    "GPG_KEY": "7169605F62C751356D054A26A821E680E5FA6305",  # python:3.12-slim's release signing key's id
    "PYTHON_VERSION": "3.12.13",
    "PYTHON_SHA256": "0" * 64,
    "PORT": "8080",
    "K_SERVICE": "open-alvary-staging-render",
    "K_REVISION": "open-alvary-staging-render-00001-abc",
    "K_CONFIGURATION": "open-alvary-staging-render",
    "CONTAINER_APP_NAME": "open-alvary-staging-sc-render",
    "CONTAINER_APP_REVISION": "open-alvary-staging-sc-render--abc",
    "CONTAINER_APP_ENV_DNS_SUFFIX": "internal.example.azurecontainerapps.io",
    "IDENTITY_ENDPOINT": "http://localhost:42356/msi/token",
    "IDENTITY_HEADER": "platform",
}


def secret_settings() -> set[str]:
    """Every environment variable either cloud's Terraform fills from a secret, and every setting a
    service of ours is given: none belongs in the renderer."""
    names = set()
    for path in [
        *(ROOT / "deployment" / "gcp-terraform" / "env").glob("*.tf"),
        *(ROOT / "deployment" / "azure" / "env").glob("*.tf"),
    ]:
        source = path.read_text()
        for start in re.finditer(r"\w*secret_env\s*=\s*(merge\(|\{)", source):
            depth, at = 1, start.end()
            while depth:
                depth += {"(": 1, "{": 1, ")": -1, "}": -1}.get(source[at], 0)
                at += 1
            block = source[start.end() : at]
            names |= set(re.findall(r"(?:^|\{)\s*([A-Z][A-Z0-9_]+)\s*=", block, re.M))
            names |= set(re.findall(r'=\s*"([A-Z][A-Z0-9_]+)"', block))
        names |= set(re.findall(r'name\s*=\s*"([A-Z][A-Z0-9_]+)"\s*\n\s*value_source', source))
    names |= set(re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    return names


def test_terraform_names_the_secrets_this_check_covers():
    found = secret_settings()
    assert {
        "DATABASE_URL",
        "ALVARY_REVIEW_DATABASE_URL",
        "ALVARY_REVIEW_OIDC_CLIENT_SECRET",
        "RESEND_API_KEY",
    } <= found
    assert {"ALVARY_KEY_PEPPER", "ALVARY_FEED_KEYS", "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET"} <= found
    assert len(found) >= 20


@pytest.mark.parametrize("name", sorted(secret_settings()))
def test_the_service_will_not_start_beside_any_secret_or_setting_of_ours(name, monkeypatch):
    for present in render.secrets_in(os.environ):
        monkeypatch.delenv(present)
    monkeypatch.setenv(name, "a-secret")
    with pytest.raises(RuntimeError, match=f"remove {name}"):
        with TestClient(render.app):
            pass


def test_the_service_starts_with_what_its_platform_and_image_give_it(monkeypatch):
    for present in render.secrets_in(os.environ):
        monkeypatch.delenv(present)
    for name, value in PLATFORM.items():
        monkeypatch.setenv(name, value)
    assert render.secrets_in(PLATFORM) == []
    with TestClient(render.app, base_url=URL) as started:
        assert started.get("/healthz").json() == {"ok": True}


def test_the_image_holds_the_renderer_alone_and_is_given_nothing():
    lines = (ROOT / "deployment" / "render.Dockerfile").read_text().splitlines()
    copies = [line.split()[1:] for line in lines if line.startswith("COPY ")]
    assert copies == [
        ["deployment/render.requirements.lock", "/tmp/render.requirements.lock"],
        ["ingestion/__init__.py", "ingestion/tools.py", "ingestion/render.py", "ingestion/"],
    ]
    assert not [line for line in lines if line.startswith(("ENV ", "ARG ", "VOLUME "))]
    assert "USER render" in lines
    [command] = [json.loads(line.removeprefix("CMD")) for line in lines if line.startswith("CMD ")]
    assert command[:2] == ["hypercorn", "ingestion.render:app"]
    # The image installs the renderer's own short list, nothing more.
    assert "--no-deps -r /tmp/render.requirements.lock" in "\n".join(lines)


def test_the_renderers_modules_import_nothing_the_image_lacks():
    allowed = {"ingestion", "fastapi", "starlette", "httpx"}  # httpx only inside the workbench's side
    for module in ("ingestion/render.py", "ingestion/tools.py"):
        source = (ROOT / module).read_text()
        imported = set(re.findall(r"^\s*(?:from|import) ([a-z_]+)", source, re.M))
        third_party = {name for name in imported if name not in sys.stdlib_module_names}
        assert third_party <= allowed, module
    assert re.search(r"^import httpx", (ROOT / "ingestion/render.py").read_text(), re.M) is None


# --- the workbench draws pages at the service and nowhere else ---------------------------------------


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """A document whose page image is not prepared, so the workbench must draw it."""
    monkeypatch.delenv("ALVARY_REVIEW_COLLECTIONS", raising=False)
    (tmp_path / "doc.pdf").write_bytes(made_pdf())
    (tmp_path / "doc.reading.json").write_text(json.dumps({"pages": []}))
    library = content.Library(tmp_path)
    monkeypatch.setattr(library, "_where", lambda act: ({}, tmp_path))
    monkeypatch.setattr(library, "original", lambda act: tmp_path / "doc.pdf")
    return library


@pytest.fixture
def no_local_drawing(monkeypatch):
    """Any attempt to draw a page in this process fails the test."""

    def never(*args, **kwargs):
        raise AssertionError("a page was drawn in the workbench")

    monkeypatch.setattr("ingestion.tools.subprocess.run", never)
    monkeypatch.setattr(render, "draw", never)


def test_the_workbench_in_service_mode_draws_nothing_itself(staged, monkeypatch, no_local_drawing):
    monkeypatch.setenv("ALVARY_RENDER", "service")
    monkeypatch.setenv("ALVARY_RENDER_URL", URL)
    monkeypatch.setenv("ALVARY_RENDER_AUDIENCE", URL)
    monkeypatch.setattr(render, "id_token", lambda audience: "id-token")
    sent = []

    def renderer(request):
        sent.append(
            (request.url.path, dict(request.url.params), request.headers["authorization"], request.read())
        )
        return httpx.Response(200, content=render.JPEG + b"page two")

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(renderer)))
    path = staged.image("doc", 2)
    assert path.read_bytes() == render.JPEG + b"page two"
    assert sent == [("/pages", {"page": "2", "dpi": "150"}, "Bearer id-token", made_pdf())]
    assert staged.image("doc", 2) == path and len(sent) == 1  # drawn once, then kept


def test_a_failing_renderer_fails_the_page_and_nothing_is_drawn_here(staged, monkeypatch, no_local_drawing):
    monkeypatch.setenv("ALVARY_RENDER", "service")
    monkeypatch.setenv("ALVARY_RENDER_URL", URL)
    real = httpx.Client
    answer = httpx.Response(503, json={"detail": "no instance"})
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(lambda r: answer))
    )
    with pytest.raises(render.Failed, match="answered 503: no instance"):
        staged.image("doc", 1)
    monkeypatch.delenv("ALVARY_RENDER_URL")
    with pytest.raises(render.Failed, match="needs ALVARY_RENDER_URL"):
        staged.image("doc", 1)


def test_the_deployed_workbench_draws_through_the_service_end_to_end(staged, faked, monkeypatch):
    # The workbench's own client and the service, joined in this process, with a pdftoppm that marks its image.
    fake_pdftoppm(faked, 'printf "\\377\\330\\377 drawn by the service" > "$last.jpg"')
    monkeypatch.setenv("ALVARY_RENDER", "service")
    monkeypatch.setenv("ALVARY_RENDER_URL", URL)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: TestClient(render.app, base_url=URL))
    assert staged.image("doc", 1).read_bytes() == render.JPEG + b" drawn by the service"
