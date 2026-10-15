# The identity-provider check server (trials/idp_check/server.py) on Cloud Run, test data only.
# Built from the commit itself (git archive): nothing untracked or private enters the build.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock cryptography==50.0.2
COPY trials ./trials
RUN useradd --create-home check && mkdir -p private-staging/idp-check && chown -R check:check /app
USER check
CMD ["python", "-m", "trials.idp_check.server"]
