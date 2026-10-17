FROM docker.io/library/postgres:18.6-bookworm
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-psycopg \
    && rm -rf /var/lib/apt/lists/*
COPY scripts /opt/lab/scripts
COPY sql /opt/lab/sql
RUN mkdir -p /private && chown postgres:postgres /private
WORKDIR /opt/lab
USER postgres
ENTRYPOINT ["python3", "/opt/lab/scripts/restore.py"]
