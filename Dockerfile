# LMS Request — guest song requests for Lyrion Media Server.
#
# Only speaks HTTP to LMS (port 9000), so ordinary bridge networking is enough:
# unlike LMS itself, LMS Request needs no host networking, multicast or slimproto.
#
#   docker build -t lms-request .
#   docker run -d --name lms-request -p 8080:8080 -v lms-request-data:/data \
#     -e LMSREQUEST_LMS_HOST=lms -e LMSREQUEST_HOST_TOKEN=pick-something lms-request

FROM python:3.12-slim

# Supplied by CI so a running container can say what it is (see /healthz).
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILT=unknown

LABEL org.opencontainers.image.title="LMS Request" \
      org.opencontainers.image.description="Guest song requests for Lyrion Media Server with TIDAL" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILT}" \
      org.opencontainers.image.licenses="MIT"

ENV LMSREQUEST_VERSION=${VERSION} \
    LMSREQUEST_COMMIT=${COMMIT} \
    LMSREQUEST_BUILT=${BUILT} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LMSREQUEST_DATA_DIR=/data

WORKDIR /app

# Dependencies first: this layer is cached unless requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY lmsrequest/ ./lmsrequest/
COPY scripts/probe.py ./scripts/probe.py
COPY VERSION ./VERSION

# /data holds lmsrequest.db: the cookie-signing secret, the ban list, the host's
# player and playlist choices, and the request log. Mount it as a volume or
# every restart logs the host out and forgets the bans.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin lmsrequest \
 && mkdir -p /data \
 && chown -R lmsrequest:lmsrequest /data /app
USER lmsrequest

VOLUME ["/data"]
EXPOSE 8080

# Liveness only; deliberately does not depend on LMS being reachable, so a
# restart loop can't be triggered by the music server going away.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]

# --no-proxy-headers is load-bearing. uvicorn otherwise trusts X-Forwarded-For
# from 127.0.0.1 and rewrites the client address before LMS Request sees it, which
# silently overrides trust_forwarded_for and lets a guest forge the address the
# ban list works on.
CMD ["sh", "-c", "exec uvicorn lmsrequest.app:app --host \"${LMSREQUEST_BIND:-0.0.0.0}\" --port \"${LMSREQUEST_PORT:-8080}\" --no-proxy-headers"]
