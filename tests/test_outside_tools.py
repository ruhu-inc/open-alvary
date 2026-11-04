"""Tools reading outside PDFs get none of this process's secrets, and limits on what they may use (ingestion.tools)."""

import json
import resource
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ingestion import gazette_layout
from ingestion.tools import BECOME, ENVIRONMENT, MIB, minimal_environment, run_tool
from workbench import content

SECRETS = {
    "DATABASE_URL": "postgresql+psycopg://owner:a-secret@db/open_alvary",
    "ALVARY_REVIEW_OIDC_CLIENT_SECRET": "a-secret",
    "RESEND_API_KEY": "a-secret",
    "ALVARY_REVIEW_DATABASE_URL": "postgresql+psycopg://review:a-secret@db/review",
}


def fake_tool(folder: Path, name: str, body: str) -> Path:
    tool = folder / name
    tool.write_text("#!/bin/sh\n" + body + "\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


@pytest.fixture
def secrets_in_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LANG", "C.UTF-8")


def test_a_tool_sees_only_what_it_needs_of_the_environment(tmp_path, secrets_in_environment):
    fake_tool(tmp_path, "pdftool", "env")
    done = run_tool(["pdftool", "issue.pdf"], timeout=20, capture_output=True, text=True, check=True)
    seen = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    assert "a-secret" not in done.stdout
    assert not set(SECRETS) & set(seen)
    assert seen["HOME"] == str(tmp_path) and seen["LANG"] == "C.UTF-8"
    # Beside those, only what the shell and macOS add themselves.
    assert set(seen) <= {*ENVIRONMENT, "PWD", "OLDPWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}


def test_a_tools_own_setting_is_all_that_is_added():
    assert minimal_environment({"TOOL_SETTING": "1"})["TOOL_SETTING"] == "1"


def test_rendering_a_page_in_the_workbench_keeps_the_secrets_from_pdftoppm(
    tmp_path, secrets_in_environment, monkeypatch
):
    # The deployed workbench's own call, with a pdftoppm that writes what it was given as the page image.
    fake_tool(tmp_path, "pdftoppm", 'for last; do :; done\nenv > "$last.jpg"')
    monkeypatch.setenv("ALVARY_RENDER", "direct")
    monkeypatch.delenv("ALVARY_REVIEW_COLLECTIONS", raising=False)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "doc.reading.json").write_text(json.dumps({"pages": []}))
    library = content.Library(tmp_path)
    monkeypatch.setattr(library, "_where", lambda act: ({}, tmp_path))
    monkeypatch.setattr(library, "original", lambda act: tmp_path / "doc.pdf")
    written = library.image("doc", 1).read_text()
    assert "PATH=" in written and "a-secret" not in written and "DATABASE_URL" not in written


def test_reading_a_text_layer_in_a_job_keeps_the_secrets_from_poppler(
    tmp_path, secrets_in_environment, monkeypatch
):
    fake_tool(tmp_path, "pdftotext", f'env > "{tmp_path}/pdftotext.env"\necho "<doc></doc>"')
    fake_tool(tmp_path, "pdfinfo", f'env > "{tmp_path}/pdfinfo.env"\necho "Pages: 0"')
    monkeypatch.setenv("ALVARY_TEXT_LAYER", "direct")
    assert gazette_layout.text_layer(tmp_path / "issue.pdf") == []
    for tool in ("pdftotext", "pdfinfo"):
        seen = (tmp_path / f"{tool}.env").read_text()
        assert "PATH=" in seen and "a-secret" not in seen


def test_a_tool_that_is_not_there_says_so_as_it_would_without_the_limits(tmp_path, secrets_in_environment):
    with pytest.raises(FileNotFoundError):
        run_tool(["no-such-tool"], timeout=5)


def test_a_failure_and_a_timeout_name_the_tool_not_what_set_its_limits(tmp_path, secrets_in_environment):
    fake_tool(tmp_path, "fails", "exit 3")
    fake_tool(tmp_path, "waits", "exec sleep 30")
    with pytest.raises(subprocess.CalledProcessError) as failed:
        run_tool(["fails", "x"], timeout=20, check=True, capture_output=True)
    assert failed.value.returncode == 3 and failed.value.cmd == ["fails", "x"]
    assert run_tool(["fails"], timeout=20, capture_output=True).returncode == 3
    with pytest.raises(subprocess.TimeoutExpired) as late:
        run_tool(["waits"], timeout=1)
    assert late.value.cmd == ["waits"]


def test_a_tool_spinning_on_a_bad_file_is_stopped_by_its_processor_limit(tmp_path, secrets_in_environment):
    # A processor limit of one second, against a wall clock of thirty: the limit is what stops it.
    fake_tool(tmp_path, "spins", "while :; do :; done")
    limits = [str(8 * 1024**3), "1", str(MIB)]
    stopped = subprocess.run(
        [sys.executable, "-I", "-S", "-c", BECOME, *limits, "spins"], timeout=30, capture_output=True
    )
    assert stopped.returncode != 0


@pytest.mark.skipif(sys.platform != "linux", reason="Only Linux enforces an address-space limit")
def test_a_tool_asking_for_too_much_memory_fails_on_linux(tmp_path, secrets_in_environment):
    script = "b = bytearray(512 * 1024 * 1024); print(len(b))"
    done = run_tool(
        [sys.executable, "-I", "-S", "-c", script],
        timeout=30,
        memory=256 * MIB,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0 and "MemoryError" in done.stderr


def test_a_tool_can_write_no_more_than_its_limit(tmp_path, secrets_in_environment):
    limit = 1024 * 1024
    out = tmp_path / "written"
    script = f"open({str(out)!r}, 'wb').write(b'x' * {4 * limit})"
    done = run_tool(
        [sys.executable, "-I", "-S", "-c", script], timeout=30, written=limit, capture_output=True, text=True
    )
    assert done.returncode != 0 and out.stat().st_size <= limit


def test_limits_are_those_of_the_tool_not_of_this_process(tmp_path, secrets_in_environment):
    before = resource.getrlimit(resource.RLIMIT_CPU)
    fake_tool(tmp_path, "ok", "exit 0")
    run_tool(["ok"], timeout=5, capture_output=True, check=True)
    assert resource.getrlimit(resource.RLIMIT_CPU) == before
