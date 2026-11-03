ARG FLUTTER_IMAGE=ghcr.io/stone-soup-tech/flutter:latest
FROM ${FLUTTER_IMAGE}

USER root
LABEL org.opencontainers.image.source=https://github.com/Stone-Soup-Tech/docker-images-flutter-bot
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/flutter-bot
COPY sdk/flutter_bot.py ./flutter_bot.py
RUN printf '#!/bin/sh\nexec python3 -B /opt/flutter-bot/flutter_bot.py "$@"\n' > /usr/local/bin/flutter-bot \
    && chmod 755 /usr/local/bin/flutter-bot
ENV PYTHONDONTWRITEBYTECODE=1 \
    PUB_CACHE=/opt/pub-cache \
    CI=true
WORKDIR /build
# No entrypoint: allow Bitbucket to execute step shell scripts.
CMD ["/bin/bash"]
