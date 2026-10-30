# live-rec

치지직·유튜브 방송을 **미리 대기 걸어 두면 켜지는 순간 녹화**하는 Docker 컨테이너.
yt-dlp + ffmpeg 기반이고, 웹 화면과 CLI를 함께 제공한다.

- 치지직 채널: 30초마다 방송 확인 → 켜지면 녹화
- 유튜브 채널: 2분마다 확인, 예약 방송은 시작까지 대기
- 다시보기·클립·일반 영상 주소는 바로 받기
- **예약**: 정한 시각(기본 한국시간)부터 N시간 동안 방송을 기다린다
- 녹화가 끊겨도 방송이 계속되면 다시 연결해 이어 녹화 (파일이 나뉨)
- 웹 화면: 등록·예약·화질 선택, 녹화 용량·속도·경과, 정체 경고, 로그, 중지, 쿠키 입력
- 여유 공간이 부족하면 지금까지 분량을 저장하고 멈춤
- yt-dlp는 컨테이너 시작 시와 매일 05시(녹화가 없을 때) 자동 업데이트

> 개인 시청 목적으로만 사용하세요. 저작권과 각 플랫폼 이용약관을 지키는 것은 사용자 책임입니다.

## 설치

Docker Compose가 필요하다 (Synology는 Container Manager).

```bash
git clone <이 저장소 주소> live-rec
cd live-rec
cp .env.example .env      # 저장 위치(REC_DIR), PUID/PGID 등 수정
mkdir -p data recordings  # REC_DIR을 바꿨다면 그 폴더를 만든다
docker compose up -d --build
```

웹 화면: `http://<서버 IP>:8790`

- **PUID/PGID**: 녹화 파일 소유자. 호스트에서 `id`로 확인해 `.env`에 넣는다.
  이 값은 이미지 빌드에 쓰이므로 바꾸면 `docker compose up -d --build`.
- **폴더 권한**: `data/`와 저장 폴더는 PUID 사용자가 쓸 수 있어야 한다.
  Docker가 대신 만들면 root 소유가 되니 미리 만들어 둔다.
- **Synology**: 공유 폴더의 ACL이 상속되면 컨테이너 안에서 읽기 전용으로 보인다.
  그럴 때는 해당 폴더만 ACL을 지우고 일반 권한으로 바꾼다.
  ```bash
  sudo synoacltool -del /volume1/<공유폴더>/live && sudo chmod 755 /volume1/<공유폴더>/live
  ```

## 사용법

### 웹 화면

주소를 넣고 **확인**을 누르면 방송 중인지 미리 볼 수 있다. 시작을 **지금부터** 또는 **예약**으로 고르고, 기다릴 시간과 화질을 정해 등록한다.

### CLI

```bash
R="docker exec live-rec rec"
$R add https://chzzk.naver.com/<채널ID> -H 6 -m "메모"   # 지금부터 6시간 대기
$R add https://www.youtube.com/@핸들 -a "21:00" -H 3      # 21시부터 3시간 대기 (지난 시각이면 내일)
$R add <URL> -a "10-05 20:30" -q 1080                     # 날짜 지정, 1080p 이하
$R ls                # 목록
$R st <ID>           # 상세 + 로그
$R stop <ID>         # 대기 취소 / 녹화 중지 (분량 저장)
$R rm <ID>           # 끝난 작업 기록 삭제 (녹화 파일은 그대로)
$R check <URL>       # 등록 없이 방송 여부 확인
$R update            # yt-dlp 수동 업데이트
```

ID는 앞부분만 써도 된다. 저장 파일: `<REC_DIR>/<채널명>/<시작시각> <제목> [<id>].mp4`

### 쿠키 (성인 방송·연령 제한 영상)

치지직 성인 방송은 네이버 로그인 쿠키(`NID_AUT`, `NID_SES`)가 있어야 받을 수 있다.
웹 화면 아래 **쿠키**에 붙여넣거나, 터미널에서 입력한다 (입력 내용은 화면에 보이지 않음).

```bash
docker exec -it live-rec rec cookies set chzzk     # NID_AUT=...; NID_SES=... 한 줄
docker exec -it live-rec rec cookies set youtube   # 유튜브는 Netscape cookies.txt 내용
docker exec live-rec rec cookies                   # 상태 확인 (값은 출력하지 않음)
```

쿠키는 계정 로그인 정보다. `data/cookies/`는 다른 사람과 공유하지 말 것.
브라우저에서 로그아웃하면 쿠키도 무효가 되니 다시 넣어야 한다.

## MCP (에이전트에게 녹화 요청)

`.env`에 `REC_MCP_TOKEN`(32자 이상, `openssl rand -hex 32`)을 넣고 `docker compose up -d`하면
`http://<서버 IP>:8790/mcp`가 MCP 서버(Streamable HTTP)가 된다. `Authorization: Bearer <REC_MCP_TOKEN>` 헤더로 붙는다.

- 도구: `rec_check`(방송 확인), `rec_add`(등록·예약), `rec_list`(목록), `rec_status`(상세+로그), `rec_stop`(중지, `confirm: true` 필수)
- 쿠키 입력과 기록 삭제는 도구로 열지 않는다
- 웹 로그인(OIDC)과 별개로 토큰만 확인한다. 대신 브라우저 요청(Origin 헤더)과 리버스 프록시를 거친 요청(`X-Forwarded-For`)은 거부하므로, 공개 주소가 아니라 포트로 직접 붙어야 한다

헤르메스 예시 (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  live-rec:
    url: http://<서버 IP>:8790/mcp
    headers:
      Authorization: "Bearer <REC_MCP_TOKEN>"
```

## 외부 공개 (선택)

웹 화면을 인터넷에 열 때는 리버스 프록시(Caddy, Nginx 등)로 HTTPS를 붙이고 **OIDC 로그인**을 켠다.
로그인 설정 전에는 프록시를 거친 요청(`X-Forwarded-For` 있음)을 모두 거부하고, 같은 네트워크에서 포트로 직접 들어온 접속만 허용한다.

1. OIDC 제공자(Gitea, Authentik, Keycloak 등)에 OAuth2 앱을 만든다.
   - 리디렉션 URI: `https://<공개 주소>/auth/callback`
   - 기밀(confidential) 클라이언트
2. `.env`에 `REC_OIDC_ISSUER`, `REC_OIDC_CLIENT_ID`, `REC_PUBLIC_URL`, `REC_ALLOWED_USERS`를 채우고 `docker compose up -d`
3. 클라이언트 시크릿을 넣는다: `docker exec -it live-rec rec auth secret`
4. 상태 확인: `docker exec live-rec rec auth`

허용 계정은 OIDC `preferred_username`으로 확인한다. 세션은 30일 유지된다.

Caddy 예시:

```
rec.example.com {
	reverse_proxy 127.0.0.1:8790
}
```

## 구조

| 파일 | 내용 |
|---|---|
| `rec.py` | 데몬(작업마다 스레드, yt-dlp 실행·감시)과 CLI |
| `web.py` | 웹 서버 (표준 라이브러리), OIDC 로그인 |
| `index.html` | 웹 화면 |

상태는 `data/` 아래 파일로만 주고받는다.

| 경로 | 내용 |
|---|---|
| `jobs/<ID>/job.json` | 요청 |
| `jobs/<ID>/state.json` | 상태 (데몬만 씀) |
| `jobs/<ID>/log.txt` | 데몬 이벤트와 yt-dlp 출력 |
| `cookies/` | 쿠키 |
| `auth/` | 세션 키, OIDC 시크릿 |

녹화 중 파일은 `<REC_DIR>/.rec-tmp/<ID>/`에 있다가 끝나면 이동한다.

참고:
- 유튜브용 JS 런타임 deno를 포함한다.
- 치지직 라이브의 fMP4 조각(`.m4s`) 때문에 ffmpeg에 `-extension_picky 0`을 넘긴다.
