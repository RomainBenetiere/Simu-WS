# syntax=docker/dockerfile:1
# =============================================================================
# Waveshare 8-ch Ethernet Relay simulator
#   - multi-stage: dependencies are built in a throw-away stage
#   - runs as an unprivileged user
#   - PID 1 is Python itself (exec form) => SIGTERM from `docker stop`
#     triggers the simulator's graceful shutdown.
# =============================================================================

ARG PYTHON_VERSION=3.12

# ---- Stage 1: build wheels / venv -------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only requirements first => this layer is cached until they change.
COPY requirements.txt .
RUN pip install -r requirements.txt


# ---- Stage 2: runtime --------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="waveshare-relay-sim" \
      org.opencontainers.image.description="High-fidelity simulator of the Waveshare 8-ch Modbus TCP Ethernet relay"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WAVESHARE_MODBUS_PORT=5020 \
    WAVESHARE_WEB_PORT=8080

# Unprivileged runtime user.
RUN groupadd --system sim && useradd --system --gid sim --no-create-home sim

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=sim:sim main.py ./
COPY --chown=sim:sim waveshare_sim/ ./waveshare_sim/

USER sim

EXPOSE 5020/tcp 8080/tcp

# Healthy when the web server answers (stdlib only, no curl in slim image).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WAVESHARE_WEB_PORT\",\"8080\")}/api/state', timeout=2)" || exit 1

# Exec form: Python is PID 1 and receives SIGTERM/SIGINT directly.
ENTRYPOINT ["python", "main.py"]
CMD []
