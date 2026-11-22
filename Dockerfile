FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home --home-dir /data motionwatch

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

USER motionwatch
ENV MOTIONWATCH_DATA=/data MOTIONWATCH_WEB_LISTEN=0.0.0.0:8080 PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["motionwatch"]
CMD ["run"]
