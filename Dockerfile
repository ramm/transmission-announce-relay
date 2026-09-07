FROM python:3.12-alpine
# Only the package is copied (see .dockerignore); never build from a directory holding a real relay.json.
COPY pyproject.toml README.md LICENSE /src/
COPY transmission_announce_relay /src/transmission_announce_relay
RUN pip install --no-cache-dir /src && rm -rf /src
# Run as the uid that owns the mounted relay.json (mode 600); see examples/docker-compose.yml.
ENTRYPOINT ["transmission-announce-relay"]
