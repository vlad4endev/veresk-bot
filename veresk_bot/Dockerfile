FROM python:3.11-slim

# Московское время для datetime.now(), автопоздравлений, логов
ENV TZ=Europe/Moscow
ARG SING_BOX_VERSION=1.11.15
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) SB_ARCH=amd64 ;; \
         arm64) SB_ARCH=arm64 ;; \
         *) SB_ARCH=amd64 ;; \
       esac \
    && curl -fsSL \
         "https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-${SB_ARCH}.tar.gz" \
       | tar -xz -C /tmp \
    && mv /tmp/sing-box-*/sing-box /usr/local/bin/sing-box \
    && chmod +x /usr/local/bin/sing-box \
    && rm -rf /tmp/sing-box-* \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
