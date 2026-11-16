# The identity-provider check server (trials/idp_check/server.py) on Cloud Run, test data only.
# Built from the commit itself (git archive): nothing untracked or private enters the build.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY trials ./trials
RUN useradd --create-home check && mkdir -p private-staging/idp-check && chown -R check:check /app
USER check
CMD ["python", "-m", "trials.idp_check.server"]
