"""The workbench's Traffic section (TRF-04): how the public website and the API are being used, for
operators with the admin role. It only reads totals (accounts.traffic), through the Access section's
limited database connection; without that connection it is off.
"""

from fastapi import FastAPI, Request

from accounts.access_admin import AccessAdmin
from accounts.traffic import report
from workbench import auth
from workbench.privileged import refuse
from workbench.store import Store

ROLE = "admin"


def install(app: FastAPI, store: Store, admin: AccessAdmin | None) -> None:
    @app.get("/api/traffic")
    def summary(request: Request, days: int = 30):
        if admin is None:
            refuse(404, "traffic_off", "The Traffic section is not set up on this workbench.")
        auth.current(request, store, role=ROLE)
        return report(admin.accounts.engine, admin.accounts.namespace, days)
