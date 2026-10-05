"""Build public website assets; source data and releases stay behind the API."""

import os
import shutil
import subprocess

from api.store import ROOT
from scripts.check_launch import public_identity_errors


def main():
    if os.getenv("VERCEL_ENV") == "production":
        errors = public_identity_errors(os.environ, (ROOT / "DATA-LICENSE.md").read_text())
        if errors:
            raise SystemExit("Public launch blocked:\n" + "\n".join(errors))
    subprocess.run(["npm", "--prefix", "web", "ci"], cwd=ROOT, check=True)
    subprocess.run(["npm", "--prefix", "web", "run", "build"], cwd=ROOT, check=True)
    output = ROOT / "public"
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(ROOT / "web/dist", output)
    if (output / "releases").exists() or (output / "ingestion").exists():
        raise SystemExit("Source data must not be exposed as static assets")


if __name__ == "__main__":
    main()
