FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# 유튜브는 JS 챌린지 풀이에 외부 JS 런타임(deno)이 필요하다
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# 녹화 파일 소유자. 호스트 사용자와 맞춘다 (.env PUID/PGID, 예: Synology 첫 사용자 1026:100)
ARG PUID=1000
ARG PGID=1000

# yt-dlp는 venv에 두고 컨테이너 사용자 소유로 → 재빌드 없이 rec update / 데몬이 매일 업데이트
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -U 'yt-dlp[default]' \
 && chown -R ${PUID}:${PGID} /opt/venv

# rec.py(데몬·CLI) + web.py·index.html(웹 화면) — 같은 폴더에 있어야 데몬이 web을 불러온다
COPY --chmod=755 rec.py /opt/rec/
COPY web.py index.html /opt/rec/
RUN ln -s /opt/rec/rec.py /usr/local/bin/rec

ENV PATH=/opt/venv/bin:$PATH TZ=Asia/Seoul PYTHONUNBUFFERED=1
USER ${PUID}:${PGID}
WORKDIR /data
CMD ["rec", "daemon"]
