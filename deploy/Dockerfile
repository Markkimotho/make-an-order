# make-an-order: one image for the web app, the backup loop and the jobs loop.
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t make-an-order .
# (deploy/compose.yml does this for you.)

# --- build stage: compile wheels (mysqlclient needs a compiler) --------------
FROM python:3.11-slim-bookworm AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config default-libmysqlclient-dev \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

# --- runtime stage ------------------------------------------------------------
FROM python:3.11-slim-bookworm

# MySQL client tools (mysqldump, mysql) for `flask backup`; rclone for the
# optional offsite copy. Set either to 0 to leave it out.
ARG WITH_MYSQL_CLIENT=1
ARG WITH_RCLONE=1
ARG APP_UID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends libmariadb3 ca-certificates tzdata \
      $( [ "$WITH_MYSQL_CLIENT" = "1" ] && echo default-mysql-client ) \
      $( [ "$WITH_RCLONE" = "1" ] && echo rclone ) \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# Non-root user. UID 1000 matches the first user on a fresh Ubuntu install,
# so a USB disk or folder owned by that user can hold the backups.
RUN useradd --create-home --uid "$APP_UID" --shell /usr/sbin/nologin app
WORKDIR /app
COPY --chown=app:app . /app
RUN mkdir -p /app/backups /app/logs \
 && chown app:app /app/backups /app/logs \
 && chmod 0755 /app/deploy/entrypoint.sh

ENV FLASK_APP=app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BACKUP_DIR=/app/backups

USER app
EXPOSE 8000
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["web"]
