"""live-rec 웹 화면 — 데몬 프로세스 안에서 표준 라이브러리 HTTP 서버로 동작한다.

작업 관리는 rec 모듈(데몬 본체)의 함수를 그대로 쓴다. 상태는 /data 아래 파일이 원본.

  GET    /                       화면 (index.html)
  GET    /api/jobs[?all=1]       목록 + 데몬 상태
  GET    /api/jobs/<id>          상세 + 로그
  POST   /api/jobs               등록 {url, memo, hours, start_at:"YYYY-MM-DDTHH:MM"(컨테이너 TZ), quality}
  POST   /api/jobs/<id>/stop     중지
  DELETE /api/jobs/<id>          기록 삭제 (녹화 파일은 그대로)
  POST   /api/check              {url} 방송 여부 확인
  GET    /api/cookies            쿠키 상태 (값은 절대 내보내지 않음)
  POST   /api/cookies/<platform> {text} 쿠키 저장

변경 요청(POST/DELETE)은 X-Rec 헤더가 있어야 한다 — 다른 사이트에서 몰래 보내는 요청(CSRF) 차단.

로그인: OIDC(PKCE, Gitea·Authentik·Keycloak 등). REC_OIDC_ISSUER·REC_OIDC_CLIENT_ID·REC_PUBLIC_URL·
REC_ALLOWED_USERS와 시크릿(data/auth/oidc_client_secret)이 모두 있으면
모든 화면·API에 로그인이 필요하고(세션 30일, REC_ALLOWED_USERS만 허용), 없으면 리버스 프록시를 거치지 않은
사설망 직접 접속(X-Forwarded-For 없음)만 허용한다.
"""
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_BODY = 64 * 1024




# ---------- 로그인 (OIDC) ----------

class Auth:
    """OIDC 로그인 (preferred_username으로 허용 계정 확인). 설정(.env)과 시크릿 파일(rec auth secret)이 모두 있어야 켜진다.
    꺼져 있을 때는 사설망에서 포트로 직접 들어온 요청만 허용한다(리버스 프록시를 거친 외부 요청은 거부)."""

    def __init__(self, rec):
        self.dir = rec.DATA / 'auth'
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.client_id = os.environ.get('REC_OIDC_CLIENT_ID', '').strip()
        self.issuer = os.environ.get('REC_OIDC_ISSUER', '').strip().rstrip('/')
        self.public = os.environ.get('REC_PUBLIC_URL', '').strip().rstrip('/')
        self.allowed = {u.strip().lower() for u in os.environ.get('REC_ALLOWED_USERS', '').split(',')
                        if u.strip()}
        kf = self.dir / 'session_key'
        if not kf.exists():
            kf.write_bytes(secrets.token_bytes(32))
            os.chmod(kf, 0o600)
        self.key = kf.read_bytes()
        self._meta = None

    def secret(self):
        try:
            return (self.dir / 'oidc_client_secret').read_text().strip()
        except FileNotFoundError:
            return ''

    def enabled(self):
        return bool(self.client_id and self.secret() and self.issuer and self.public and self.allowed)

    def meta(self):
        if not self._meta:
            with urllib.request.urlopen(f'{self.issuer}/.well-known/openid-configuration', timeout=10) as r:
                self._meta = json.load(r)
        return self._meta

    def sign(self, obj):
        data = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')
        mac = hmac.new(self.key, data.encode(), hashlib.sha256).hexdigest()
        return f'{data}.{mac}'

    def verify(self, token):
        try:
            data, mac = (token or '').rsplit('.', 1)
            if not hmac.compare_digest(mac, hmac.new(self.key, data.encode(), hashlib.sha256).hexdigest()):
                return None
            obj = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
            return obj if obj.get('exp', 0) > time.time() else None
        except Exception:
            return None


def cookie_header(name, value, max_age, secure):
    c = f'{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}'
    return c + ('; Secure' if secure else '')


def page(title, body):
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
            '<style>body{font:15px/1.6 system-ui,sans-serif;max-width:420px;margin:15vh auto;padding:0 16px;'
            'color:#17181a;background:#f6f7f9}@media(prefers-color-scheme:dark){body{color:#e9eaec;background:#111214}'
            'a{color:#1fc777}}</style></head>'
            f'<body><h2>{html.escape(title)}</h2>{body}</body></html>').encode()


def serve(port, rec):
    auth = Auth(rec)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'live-rec'

        def log_message(self, fmt, *args):  # 접근 로그는 남기지 않음 (docker logs가 지저분해짐)
            pass

        def send(self, code, body, ctype='application/json; charset=utf-8', headers=()):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, url, cookies=()):
            self.send(302, b'', 'text/plain', [('Location', url)] + [('Set-Cookie', c) for c in cookies])

        def cookies(self):
            out = {}
            for part in (self.headers.get('Cookie') or '').split(';'):
                if '=' in part:
                    k, v = part.strip().split('=', 1)
                    out[k] = v
            return out

        def https(self):
            return self.headers.get('X-Forwarded-Proto') == 'https'

        def user(self):
            s = auth.verify(self.cookies().get('rec_session'))
            return s and s.get('u')

        def direct_lan(self):
            """리버스 프록시를 거치지 않고 사설망에서 포트로 직접 들어온 요청인가"""
            try:
                ip = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            return ip.is_private and 'X-Forwarded-For' not in self.headers

        def body(self):
            n = int(self.headers.get('Content-Length') or 0)
            if n > MAX_BODY:
                raise rec.RecError('요청이 너무 큼')
            try:
                return json.loads(self.rfile.read(n) or b'{}')
            except json.JSONDecodeError:
                raise rec.RecError('JSON 형식이 아님')

        # 로그인 흐름: /auth/login → OIDC 제공자 → /auth/callback → 세션 쿠키(30일)

        def auth_route(self, path, query):
            q = urllib.parse.parse_qs(query)
            if path == '/auth/login':
                if not auth.enabled():
                    return self.send(503, page('로그인 설정 전', '<p>로그인(OIDC)이 아직 설정되지 않았습니다.</p>'),
                                     'text/html; charset=utf-8')
                nxt = (q.get('next') or ['/'])[0]
                if not nxt.startswith('/') or nxt.startswith('//'):
                    nxt = '/'
                state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
                params = urllib.parse.urlencode({
                    'response_type': 'code', 'client_id': auth.client_id, 'scope': 'openid profile',
                    'redirect_uri': f'{auth.public}/auth/callback', 'state': state,
                    'code_challenge': challenge, 'code_challenge_method': 'S256'})
                st = auth.sign({'s': state, 'v': verifier, 'n': nxt, 'exp': time.time() + 600})
                return self.redirect(f"{auth.meta()['authorization_endpoint']}?{params}",
                                     [cookie_header('rec_state', st, 600, self.https())])
            if path == '/auth/callback':
                st = auth.verify(self.cookies().get('rec_state'))
                if not st or not hmac.compare_digest(st['s'], (q.get('state') or [''])[0]) or not q.get('code'):
                    return self.send(400, page('로그인 실패', '<p>로그인 요청이 만료됐거나 올바르지 않습니다. '
                                                          '<a href="/auth/login">다시 로그인</a></p>'),
                                     'text/html; charset=utf-8')
                form = urllib.parse.urlencode({
                    'grant_type': 'authorization_code', 'code': q['code'][0],
                    'redirect_uri': f'{auth.public}/auth/callback', 'client_id': auth.client_id,
                    'client_secret': auth.secret(), 'code_verifier': st['v']}).encode()
                req = urllib.request.Request(auth.meta()['token_endpoint'], data=form,
                                             headers={'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=15) as r:
                    token = json.load(r)['access_token']
                req = urllib.request.Request(auth.meta()['userinfo_endpoint'],
                                             headers={'Authorization': f'Bearer {token}'})
                with urllib.request.urlopen(req, timeout=15) as r:
                    info = json.load(r)
                name = (info.get('preferred_username') or '').lower()
                if name not in auth.allowed:
                    print(f'[{rec.ts()}] 로그인 거부: {name or "?"}', flush=True)
                    return self.send(403, page('허용되지 않은 계정', f'<p>{html.escape(name)} 계정은 사용할 수 없습니다.</p>'),
                                     'text/html; charset=utf-8',
                                     [('Set-Cookie', cookie_header('rec_state', '', 0, self.https()))])
                print(f'[{rec.ts()}] 로그인: {name}', flush=True)
                session = auth.sign({'u': name, 'exp': time.time() + 30 * 86400})
                return self.redirect(st.get('n') or '/', [
                    cookie_header('rec_session', session, 30 * 86400, self.https()),
                    cookie_header('rec_state', '', 0, self.https())])
            if path == '/auth/logout':
                return self.send(200, page('로그아웃됨', '<p><a href="/auth/login">다시 로그인</a></p>'),
                                 'text/html; charset=utf-8',
                                 [('Set-Cookie', cookie_header('rec_session', '', 0, self.https()))])
            if path == '/auth/me':
                return self.send(200, {'user': self.user(), 'auth': auth.enabled()})
            return self.send(404, {'error': '없는 주소'})

        def route(self, method):
            path = self.path.split('?', 1)[0].rstrip('/') or '/'
            query = self.path.split('?', 1)[1] if '?' in self.path else ''
            try:
                if path.startswith('/auth/') and method == 'GET':
                    return self.auth_route(path, query)
                if auth.enabled():
                    if not self.user():
                        if path.startswith('/api/'):
                            return self.send(401, {'error': '로그인 필요'})
                        # 로그인 쿠키는 공개 주소에만 붙으므로 IP 등 다른 주소로 들어오면 공개 주소로 보낸다
                        if self.headers.get('Host') != urllib.parse.urlparse(auth.public).netloc:
                            return self.redirect(auth.public + self.path)
                        return self.redirect('/auth/login?' + urllib.parse.urlencode({'next': self.path}))
                elif not self.direct_lan():
                    return self.send(403, page('접속 불가', '<p>로그인(OIDC) 설정 전에는 같은 네트워크에서 직접 접속할 때만 열 수 있습니다.</p>'),
                                     'text/html; charset=utf-8')
                if method != 'GET' and self.headers.get('X-Rec') != '1':
                    return self.send(403, {'error': '허용되지 않은 요청'})
                if method == 'GET' and path == '/':
                    return self.send(200, (HERE / 'index.html').read_bytes(), 'text/html; charset=utf-8')
                if method == 'GET' and path == '/api/jobs':
                    return self.send(200, {'daemon': rec.daemon_info(), 'now': rec.now(),
                                           'jobs': rec.list_jobs('all=1' in query)})
                if method == 'POST' and path == '/api/jobs':
                    b = self.body()
                    start_at = rec.parse_at(b['start_at']) if b.get('start_at') else None
                    job, msgs = rec.create_job(b.get('url'), b.get('hours') or 12, b.get('memo'),
                                               start_at=start_at, quality=b.get('quality') or None)
                    return self.send(200, {'id': job['id'], 'messages': msgs})
                if m := re.fullmatch(r'/api/jobs/([\w-]+)', path):
                    jid = rec.find_job(m[1])
                    if method == 'GET':
                        return self.send(200, rec.job_view(jid, 80))
                    if method == 'DELETE':
                        rec.remove_job(jid)
                        return self.send(200, {'ok': True})
                if (m := re.fullmatch(r'/api/jobs/([\w-]+)/stop', path)) and method == 'POST':
                    return self.send(200, {'message': rec.stop_job(rec.find_job(m[1]))})
                if method == 'POST' and path == '/api/check':
                    return self.send(200, rec.check_url((self.body().get('url') or '').strip()))
                if method == 'GET' and path == '/api/cookies':
                    return self.send(200, rec.cookie_status())
                if (m := re.fullmatch(r'/api/cookies/(chzzk|youtube)', path)) and method == 'POST':
                    rec.save_cookies(m[1], self.body().get('text'))
                    return self.send(200, rec.cookie_status())
                return self.send(404, {'error': '없는 주소'})
            except rec.RecError as e:
                return self.send(400, {'error': str(e)})
            except Exception as e:
                traceback.print_exc()
                return self.send(500, {'error': f'서버 오류: {e}'})

        def do_GET(self):
            self.route('GET')

        def do_POST(self):
            self.route('POST')

        def do_DELETE(self):
            self.route('DELETE')

    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
