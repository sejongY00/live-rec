#!/usr/bin/env python3
"""live-rec — 치지직·유튜브 녹화 대기열 (데몬 + CLI 한 파일)

  rec add <URL> [-a "21:00"] [-H 시간] [-m 메모] [-q 1080] [-f 포맷] [-x "yt-dlp 추가 인자"]
        채널 주소면 방송이 켜질 때까지 기다렸다 녹화, 영상 주소면 바로 받는다(유튜브 예약 방송은 시작까지 대기)
        -a: 예약 시작 시각(컨테이너 TZ, 기본 한국시간). 그 시각부터 -H 시간 동안 방송을 기다린다
  rec ls [-a]        목록 (기본: 진행 중 + 최근 끝난 10개)
  rec st <ID>        상세 상태 + 로그 끝부분
  rec stop <ID>      대기 취소 / 녹화 중지 (지금까지 분량은 저장)
  rec rm <ID>        끝난 작업 기록 삭제 (녹화 파일은 그대로)
  rec check <URL>    등록하지 않고 감지 결과·방송 여부만 확인
  rec cookies        쿠키 파일 상태 (값은 출력하지 않음)
  rec cookies set [chzzk|youtube]
                     쿠키 붙여넣기 입력 (docker exec -it 로 실행, 입력이 화면에 안 보임)
  rec auth [secret]  웹 화면 OIDC 로그인 상태 / 클라이언트 시크릿 입력 (docker exec -it)
  rec update         yt-dlp 업데이트
  rec daemon         컨테이너 메인 프로세스

웹 화면(web.py, 기본 :8790)은 데몬 프로세스 안에서 돌고, 상태는 /data 아래 파일로만 주고받는다.
  jobs/<id>/job.json    요청 (add가 만들고 이후 바꾸지 않음)
  jobs/<id>/state.json  상태 (데몬만 씀)
  jobs/<id>/stop        중지 요청 표시 (있으면 중지)
  jobs/<id>/files.txt   완성된 파일 경로
  jobs/<id>/log.txt     데몬 이벤트 + yt-dlp 출력
  cookies/chzzk.txt, cookies/youtube.txt
        Netscape 형식(브라우저 확장으로 내보낸 cookies.txt) 또는 'NID_AUT=...; NID_SES=...' 한 줄
"""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

DATA = Path(os.environ.get('REC_DATA', '/data'))
OUT = Path(os.environ.get('REC_OUT', '/recordings'))
JOBS = DATA / 'jobs'
COOKIES = DATA / 'cookies'
TMP = OUT / '.rec-tmp'  # 녹화 중 파일. 같은 마운트 안이라 완료 후 이동이 즉시 끝난다
MIN_FREE_GB = float(os.environ.get('REC_MIN_FREE_GB', '20'))
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')
COOKIE_DOMAIN = {'chzzk': '.naver.com', 'youtube': '.youtube.com'}
FINAL = {'done', 'failed', 'stopped', 'expired'}
MAX_RECONNECT = 30
STALL_WARN = 120
SHUTDOWN = threading.Event()
# 예약·표시 시각의 시간대 (컨테이너 TZ, 기본 한국시간)
TZ_NAME = os.environ.get("TZ") or "Asia/Seoul"
LOCAL_TZ = ZoneInfo(TZ_NAME)
TZ_LABEL = "한국시간" if TZ_NAME == "Asia/Seoul" else TZ_NAME
WEB_PORT = int(os.environ.get("REC_WEB_PORT", "8790"))

STATUS_KO = {'waiting': '대기', 'recording': '녹화중', 'done': '완료', 'failed': '실패',
             'stopped': '중지', 'expired': '만료', 'scheduled': '예약'}
PLATFORM_KO = {'chzzk': '치지직', 'youtube': '유튜브', 'other': '기타'}


# ---------- 공통 ----------

def now():
    return time.time()


def ts(t=None):
    return datetime.fromtimestamp(t or now(), LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p, obj):
    p = Path(p)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def human(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f}{unit}' if unit in ('B', 'KB') else f'{n:.1f}{unit}'
        n /= 1024


def dur(sec):
    sec = int(max(sec or 0, 0))
    return f'{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}'


def dir_size(p):
    total = 0
    for f in Path(p).rglob('*'):
        try:
            if f.is_file():
                total += f.stat().st_size
        except FileNotFoundError:
            pass
    return total


def free_gb():
    return shutil.disk_usage(OUT).free / 1024 ** 3


def tail(path, n=20):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(f.tell() - 16384, 0))
            text = f.read().decode(errors='replace')
    except FileNotFoundError:
        return []
    lines = [l for l in re.split(r'[\r\n]+', text) if l.strip()]
    return lines[-n:]


# ---------- 주소 판별 ----------

CHZZK_CH = re.compile(r'chzzk\.naver\.com/(?:live/)?([0-9a-f]{32})(?:[/?#]|$)')
YT_VIDEO = re.compile(r'(?:youtube\.com/(?:watch\?(?:.*&)?v=|live/|shorts/)|youtu\.be/)([\w-]{11})')
YT_CHANNEL = re.compile(r'(https?://(?:www\.|m\.)?youtube\.com/'
                        r'(?:@[^/?#]+|channel/UC[\w-]{22}|c/[^/?#]+|user/[^/?#]+))')


def classify(url):
    if m := CHZZK_CH.search(url):
        return {'platform': 'chzzk', 'mode': 'channel', 'channel_id': m[1]}
    if 'chzzk.naver.com' in url:
        return {'platform': 'chzzk', 'mode': 'video'}
    if m := YT_VIDEO.search(url):
        return {'platform': 'youtube', 'mode': 'video', 'video_id': m[1]}
    if m := YT_CHANNEL.search(url):
        return {'platform': 'youtube', 'mode': 'channel', 'channel_url': m[1]}
    if 'youtube.com' in url or 'youtu.be' in url:
        return {'platform': 'youtube', 'mode': 'video'}
    return {'platform': 'other', 'mode': 'video'}


# ---------- 쿠키 ----------

def load_cookies(platform):
    """-> (쿠키 목록, 형식). 파일이 없으면 ([], None)"""
    p = COOKIES / f'{platform}.txt'
    if platform not in COOKIE_DOMAIN or not p.exists():
        return [], None
    text = p.read_text(errors='replace')
    out = []
    if re.search(r'^\S+\t(TRUE|FALSE)\t', text, re.M):
        for line in text.splitlines():
            if line.startswith('#HttpOnly_'):
                line = line[len('#HttpOnly_'):]
            if not line.strip() or line.startswith('#'):
                continue
            f = line.split('\t')
            if len(f) >= 7:
                out.append({'domain': f[0], 'path': f[2], 'secure': f[3], 'expires': f[4],
                            'name': f[5], 'value': f[6]})
        return out, 'netscape'
    for kv in re.split(r';\s*|\n', text.strip()):
        if '=' in kv:
            k, v = kv.split('=', 1)
            out.append({'domain': COOKIE_DOMAIN[platform], 'path': '/', 'secure': 'TRUE',
                        'expires': '0', 'name': k.strip(), 'value': v.strip()})
    return out, 'header'


def write_ytdlp_cookies(platform, dest):
    """yt-dlp용 Netscape 사본을 만든다(yt-dlp가 쿠키 파일을 덮어쓰므로 원본은 건드리지 않음)."""
    cookies, _ = load_cookies(platform)
    if not cookies:
        return None
    lines = ['# Netscape HTTP Cookie File']
    for c in cookies:
        sub = 'TRUE' if c['domain'].startswith('.') else 'FALSE'
        lines.append('\t'.join([c['domain'], sub, c['path'], c['secure'], c['expires'] or '0',
                                c['name'], c['value']]))
    dest = Path(dest)
    dest.write_text('\n'.join(lines) + '\n')
    os.chmod(dest, 0o600)
    return dest


def cookie_header(platform, host):
    cookies, _ = load_cookies(platform)
    return '; '.join(f"{c['name']}={c['value']}" for c in cookies
                     if host.endswith(c['domain'].lstrip('.')))


# ---------- 플랫폼 조회 ----------

def http_json(url, platform=None):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    if platform:
        ck = cookie_header(platform, urllib.request.urlparse(url).hostname)
        if ck:
            req.add_header('Cookie', ck)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def chzzk_channel_name(cid):
    c = http_json(f'https://api.chzzk.naver.com/service/v1/channels/{cid}', 'chzzk').get('content')
    return (c or {}).get('channelName')


def chzzk_live(cid):
    """-> (방송 중?, 방송 제목, 성인 방송?)"""
    c = http_json(f'https://api.chzzk.naver.com/service/v3/channels/{cid}/live-detail',
                  'chzzk').get('content') or {}
    return c.get('status') == 'OPEN', c.get('liveTitle'), bool(c.get('adult'))


def yt_probe(url, cookie=None):
    """yt-dlp로 방송 상태 조회 -> ({'live_status','url','title'} 또는 None, 오류 메시지)"""
    cmd = ['yt-dlp', '--ignore-config', '--skip-download', '--no-warnings', '--no-playlist',
           '--print', '%(live_status)s\t%(webpage_url)s\t%(title)s']
    if cookie:
        cmd += ['--cookies', str(cookie)]
    try:
        r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None, '조회 시간 초과'
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or ['알 수 없는 오류'])[-1]
        return None, err
    f = (r.stdout.strip().splitlines() or [''])[-1].split('\t') + ['', '', '']
    return {'live_status': f[0], 'url': f[1], 'title': f[2]}, None


# ---------- 작업 실행 (데몬) ----------

class Worker(threading.Thread):
    def __init__(self, jid):
        super().__init__(daemon=True, name=jid)
        self.jid = jid
        self.dir = JOBS / jid
        self.job = read_json(self.dir / 'job.json')
        self.state = read_json(self.dir / 'state.json', {})
        self.tmp = TMP / jid
        self.proc = None

    # 상태·로그

    def log(self, msg):
        with open(self.dir / 'log.txt', 'a') as f:
            f.write(f'[{ts()}] {msg}\n')

    def set(self, **kw):
        self.state.update(kw)
        self.state['updated_at'] = now()
        write_json(self.dir / 'state.json', self.state)

    def stop_requested(self):
        return (self.dir / 'stop').exists()

    def deadline(self):
        return start_of(self.job) + self.job['wait_hours'] * 3600

    def sleep(self, sec):
        end = now() + sec
        while now() < end and not self.stop_requested() and not SHUTDOWN.is_set():
            time.sleep(1)

    def cookie(self):
        if self.job['platform'] in COOKIE_DOMAIN:
            return write_ytdlp_cookies(self.job['platform'], self.dir / 'cookies.txt')
        return None

    def files(self):
        try:
            return [l for l in (self.dir / 'files.txt').read_text().splitlines() if l.strip()]
        except FileNotFoundError:
            return []

    # 흐름

    def run(self):
        try:
            self.main()
        except Exception as e:
            self.log(f'오류: {e!r}')
            self.finish('failed', error=str(e))

    def main(self):
        self.collect()  # 데몬이 녹화 중 재시작됐다면 남은 임시 파일부터 정리
        # 예약: 시작 시각까지 기다린다
        start = self.job.get('start_at')
        if start and now() < start:
            self.set(status='scheduled', note=f'{ts(start)} 시작 예약')
            self.log(f'예약 대기: {ts(start)} 시작')
            while now() < start:
                if SHUTDOWN.is_set():
                    return
                if self.stop_requested():
                    return self.finish('stopped')
                time.sleep(min(5, max(start - now(), 0.1)))
        if self.state.get('status') != 'waiting':
            self.set(status='waiting', note='방송 확인 시작')
        self.log(f"감시 시작 ({'채널 대기' if self.job['mode'] == 'channel' else '바로 받기'})")
        while True:
            if SHUTDOWN.is_set():
                return
            if self.stop_requested():
                return self.finish('stopped')
            target = self.find_target()
            if target is None:
                if now() > self.deadline():
                    return self.finish('done' if self.files() else 'expired')
                self.sleep(self.job['interval'])
                continue
            before = len(self.files())
            result = self.record(target)
            self.collect()
            if result == 'shutdown':
                self.set(status='waiting', note='데몬 재시작 — 다시 이어서 확인')
                return
            if self.stop_requested():
                return self.finish('stopped')
            if result == 'expired':
                return self.finish('expired')
            if result == 'nospace':
                return self.finish('failed', error=f'저장 공간 부족 (여유 {MIN_FREE_GB:.0f}GB 미만)')
            # 한 조각도 못 받고 실패하는 일이 반복되면 재연결해도 소용없다
            empty = 0 if len(self.files()) > before else self.state.get('empty_fails', 0) + 1
            self.set(empty_fails=empty)
            if result != 0 and empty >= 3:
                return self.finish('failed', error=self.last_error())
            if self.job['mode'] == 'channel' and self.still_live():
                n = self.state.get('reconnects', 0) + 1
                if n <= MAX_RECONNECT:
                    self.set(reconnects=n)
                    self.log(f'녹화가 끊겼지만 방송은 계속 중 — 다시 연결 ({n}회째)')
                    time.sleep(10 * empty + 5)
                    continue
            if result == 0 or self.files():
                return self.finish('done')
            return self.finish('failed', error=self.last_error())

    def finish(self, status, **kw):
        self.collect()
        (self.dir / 'cookies.txt').unlink(missing_ok=True)
        self.set(status=status, finished_at=now(), bytes=0, rate=0, pid=None, **kw)
        self.log(f'종료: {STATUS_KO[status]}' + (f" — {kw['error']}" if kw.get('error') else ''))

    def find_target(self):
        j = self.job
        if j['mode'] == 'video':
            return j['url']
        try:
            if j['platform'] == 'chzzk':
                live, title, adult = chzzk_live(j['channel_id'])
                if live:
                    self.set(live_title=title, adult=adult)
                    self.log(f'방송 감지: {title}' + (' (성인 방송)' if adult else ''))
                    return f"https://chzzk.naver.com/live/{j['channel_id']}"
            else:
                info, err = yt_probe(j['channel_url'] + '/live', self.cookie())
                if info and info['live_status'] in ('is_live', 'is_upcoming'):
                    self.set(live_title=info['title'])
                    self.log(f"방송 감지({info['live_status']}): {info['title']}")
                    return info['url']
            self.set(status='waiting', checked_at=now(), note='방송 꺼짐 — 대기 중')
        except Exception as e:
            self.set(status='waiting', checked_at=now(), note=f'확인 실패: {e}')
        return None

    def still_live(self):
        for _ in range(3):
            try:
                if self.job['platform'] == 'chzzk':
                    return chzzk_live(self.job['channel_id'])[0]
                info, _ = yt_probe(self.job['channel_url'] + '/live', self.cookie())
                return bool(info) and info['live_status'] == 'is_live'
            except Exception:
                time.sleep(10)
        return False

    def record(self, url):
        j = self.job
        start = datetime.now(LOCAL_TZ).strftime('%Y%m%d_%H%M%S')
        self.tmp.mkdir(parents=True, exist_ok=True)
        cmd = ['yt-dlp', '--ignore-config', '--newline', '--no-colors', '--progress-delta', '10',
               '--windows-filenames', '--no-playlist',
               '-P', f'home:{OUT}', '-P', f'temp:{self.tmp}',
               '-o', f'%(uploader,channel|기타)s/{start} %(fulltitle).80B [%(id)s].%(ext)s',
               '--print-to-file', 'after_move:filepath', str(self.dir / 'files.txt'),
               '--merge-output-format', 'mp4',
               # 치지직 fMP4 조각(.m4s)을 ffmpeg 7.1+가 확장자 검사로 거부하는 문제
               '--downloader-args', 'ffmpeg_i:-extension_picky 0']
        cookie = self.cookie()
        if cookie:
            cmd += ['--cookies', str(cookie)]
        if j['platform'] == 'youtube':
            cmd += ['--wait-for-video', '30-60']
        if j.get('quality'):
            cmd += ['-S', f"res:{j['quality']}"]  # 이 해상도 이하 중 최고
        if j.get('format'):
            cmd += ['-f', j['format']]
        cmd += j.get('extra_args', [])
        cmd.append(url)

        self.log(f'yt-dlp 시작: {url}')
        with open(self.dir / 'log.txt', 'a') as logf:
            self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
        self.set(pid=self.proc.pid, note='시작 중', part_started_at=now(), stalled_since=None)
        last_size, last_t, stopping, result = 0, now(), None, None
        while (rc := self.proc.poll()) is None:
            time.sleep(5)
            size, t = dir_size(self.tmp), now()
            rate = max(size - last_size, 0) / (t - last_t)
            st = {'bytes': size, 'rate': rate}
            if size > 0:
                st['status'] = 'recording'
                if not self.state.get('recording_since') or self.state.get('status') != 'recording':
                    st['recording_since'] = t
                if size == last_size:
                    stalled = self.state.get('stalled_since') or t
                    st['stalled_since'] = stalled
                    st['note'] = f'⚠ {dur(t - stalled)}째 용량 증가 없음' if t - stalled >= STALL_WARN else '녹화 중'
                else:
                    st['stalled_since'], st['note'] = None, '녹화 중'
            else:
                st['note'] = '방송 시작 대기 중' if j['platform'] == 'youtube' else '연결 중'
            self.set(**st)
            last_size, last_t = size, t

            if stopping is None:
                why = None
                if SHUTDOWN.is_set():
                    why, result = '데몬 종료', 'shutdown'
                elif self.stop_requested():
                    why = '중지 요청'
                elif size == 0 and t > self.deadline():
                    why, result = '대기 시간 초과', 'expired'
                elif free_gb() < MIN_FREE_GB:
                    why, result = '저장 공간 부족', 'nospace'
                if why:
                    self.log(f'{why} — 지금까지 분량을 저장하고 종료')
                    stopping = t
                    self.proc.send_signal(signal.SIGINT)  # yt-dlp가 ffmpeg에 q를 보내 파일을 정상 마무리
            elif t - stopping > 1800:  # 긴 녹화는 마무리(MP4 변환)에 몇 분 걸린다
                self.log('정상 종료가 늦어 강제 종료')
                self.proc.kill()
        self.proc = None
        self.set(pid=None, bytes=0, rate=0, stalled_since=None)
        self.log(f'yt-dlp 종료 (코드 {rc})')
        return result or rc

    def collect(self):
        """임시 폴더에 남은 파일(강제 종료·오류로 이동 못 한 분량)을 저장 위치로 옮긴다."""
        if self.tmp.exists():
            for f in sorted(self.tmp.rglob('*')):
                if not f.is_file():
                    continue
                name = f.name
                if (f.stat().st_size == 0 or re.search(r'\.(ytdl|json|temp)$|\.part-Frag', name)):
                    f.unlink()
                    continue
                if name.endswith('.part'):
                    name = name[:-5]
                dest = OUT / f.relative_to(self.tmp).parent / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                stem, n = dest, 1
                while dest.exists():
                    dest = stem.with_name(f'{stem.stem} ({n}){stem.suffix}')
                    n += 1
                shutil.move(f, dest)
                with open(self.dir / 'files.txt', 'a') as fl:
                    fl.write(f'{dest}\n')
                self.log(f'남은 임시 파일 보관: {dest}')
            shutil.rmtree(self.tmp, ignore_errors=True)
        files = self.files()
        total = sum(Path(p).stat().st_size for p in files if Path(p).exists())
        if files != self.state.get('files') or total != self.state.get('total_bytes'):
            self.set(files=files, total_bytes=total)

    def last_error(self):
        errs = [l for l in tail(self.dir / 'log.txt', 60) if 'ERROR' in l]
        return errs[-1] if errs else '알 수 없는 오류 (rec st로 로그 확인)'


def ytdlp_version():
    try:
        return subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except Exception:
        return '?'


def update_ytdlp():
    before = ytdlp_version()
    r = subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-U', '--no-cache-dir',
                        'yt-dlp[default]'], capture_output=True, text=True, timeout=600)
    after = ytdlp_version()
    if r.returncode != 0:
        return f'yt-dlp 업데이트 실패: {(r.stderr.strip().splitlines() or ["?"])[-1]}'
    return f'yt-dlp {before} → {after}' if before != after else f'yt-dlp {after} (최신)'


def daemon():
    for d in (JOBS, COOKIES, TMP):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(COOKIES, 0o700)
    signal.signal(signal.SIGTERM, lambda *_: SHUTDOWN.set())
    signal.signal(signal.SIGINT, lambda *_: SHUTDOWN.set())
    print(f'[{ts()}] {update_ytdlp()}', flush=True)
    ver = ytdlp_version()
    workers = {}
    started, updated_day = now(), datetime.now(LOCAL_TZ).date()
    import web  # 같은 폴더의 web.py — 데몬과 같은 프로세스에서 웹 화면 제공
    threading.Thread(target=web.serve, args=(WEB_PORT, sys.modules[__name__]), daemon=True,
                     name='web').start()
    print(f'[{ts()}] 웹 화면 :{WEB_PORT}', flush=True)
    while not SHUTDOWN.is_set():
        present = set()
        for d in sorted(JOBS.iterdir()):
            if not d.is_dir() or not (d / 'job.json').exists():
                continue
            present.add(d.name)
            if d.name in workers:
                continue
            if read_json(d / 'state.json', {}).get('status') in FINAL:
                workers[d.name] = None
                continue
            w = workers[d.name] = Worker(d.name)
            w.start()
            print(f'[{ts()}] 작업 시작 {d.name}', flush=True)
        for k in set(workers) - present:
            del workers[k]
        busy = [w for w in workers.values() if w and w.is_alive() and w.proc]
        # 매일 05시, 녹화가 없을 때 yt-dlp 업데이트 (사이트 변경 대응)
        if datetime.now(LOCAL_TZ).hour == 5 and datetime.now(LOCAL_TZ).date() != updated_day and not busy:
            updated_day = datetime.now(LOCAL_TZ).date()
            print(f'[{ts()}] {update_ytdlp()}', flush=True)
            ver = ytdlp_version()
        write_json(DATA / 'daemon.json', {'pid': os.getpid(), 'started_at': started,
                                          'updated_at': now(), 'ytdlp': ver,
                                          'active': len([w for w in workers.values() if w and w.is_alive()]),
                                          'recording': len(busy), 'free_gb': round(free_gb(), 1)})
        SHUTDOWN.wait(3)
    print(f'[{ts()}] 종료 신호 — 녹화 중인 작업을 저장하는 중', flush=True)
    for w in workers.values():
        if w and w.is_alive():
            w.join(timeout=170)


# ---------- 작업 관리 (CLI·웹 공용) ----------

class RecError(Exception):
    """사용자에게 그대로 보여줄 오류"""


def parse_at(s):
    """'2026-10-04 21:00' / '2026-10-04T21:00' / '10-04 21:00' / '21:00' → epoch (TZ 기준, 기본 한국시간).
    시각만 주면 오늘 그 시각, 이미 지났으면 내일."""
    s = s.strip().replace('T', ' ')
    s = re.sub(r'(\+09:?00|Z)$', '', s).strip()
    n = datetime.now(LOCAL_TZ)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%m-%d %H:%M', '%H:%M'):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == '%H:%M':
            d = n.replace(hour=d.hour, minute=d.minute, second=0, microsecond=0)
            if d.timestamp() < n.timestamp():
                d = d + timedelta(days=1)
            return d.timestamp()
        if fmt == '%m-%d %H:%M':
            d = d.replace(year=n.year)
        return d.replace(tzinfo=LOCAL_TZ).timestamp()
    raise RecError(f'시각 형식을 모름: {s} (예: 2026-10-04 21:00, 21:00)')


def find_job(prefix):
    ids = sorted(d.name for d in JOBS.iterdir() if d.is_dir() and d.name.startswith(prefix))
    if not ids:
        raise RecError(f'작업 없음: {prefix}')
    if len(ids) > 1 and prefix not in ids:
        raise RecError(f'여러 작업과 일치: {" ".join(ids)}')
    return prefix if prefix in ids else ids[0]


def load(jid):
    d = JOBS / jid
    return read_json(d / 'job.json', {}), read_json(d / 'state.json', {})


def label(job, state):
    return job.get('memo') or job.get('title') or state.get('live_title') or job['url']


def start_of(job):
    return job.get('start_at') or job['created_at']


def create_job(url, hours=12, memo=None, fmt=None, interval=None, extra=None, start_at=None,
               quality=None):
    """대기 등록 -> (작업, 안내 메시지 목록)"""
    url = (url or '').strip()
    if not re.match(r'https?://', url):
        raise RecError('주소는 http(s)://로 시작해야 함')
    if not 0 < float(hours) <= 72:
        raise RecError('대기 시간은 0~72시간')
    info = classify(url)
    jid = datetime.now(LOCAL_TZ).strftime('%m%d-%H%M%S')
    while (JOBS / jid).exists():
        jid += 'x'
    t = now()
    if start_at and start_at <= t + 30:
        start_at = None  # 지금이나 과거면 바로 시작
    job = {'id': jid, 'url': url, 'created_at': t, 'start_at': start_at, 'wait_hours': float(hours),
           'interval': int(interval or (30 if info['platform'] == 'chzzk' else 120)),
           'memo': (memo or '').strip() or None, 'format': fmt or None,
           'quality': int(quality) if quality else None,
           'extra_args': shlex.split(extra or ''), **info}
    msgs = []
    if info['platform'] == 'chzzk' and info['mode'] == 'channel':
        try:
            job['title'] = chzzk_channel_name(info['channel_id'])
            live, title, adult = chzzk_live(info['channel_id'])
            msgs.append(f'지금 방송 중: {title}' if live else '지금은 방송 꺼짐')
            if adult and not load_cookies('chzzk')[0]:
                msgs.append('⚠ 성인 방송인데 치지직 쿠키가 없음 — 녹화가 실패할 수 있음')
        except Exception as e:
            msgs.append(f'채널 조회 실패({e}) — 대기는 등록함')
    d = JOBS / jid
    d.mkdir(parents=True)
    write_json(d / 'job.json', job)
    status = 'scheduled' if start_at else 'waiting'
    write_json(d / 'state.json', {'status': status, 'note': '등록됨', 'updated_at': now()})
    return job, msgs


def job_view(jid, log_lines=0):
    """웹·CLI 표시용으로 작업과 상태를 합친다"""
    job, st = load(jid)
    s = st.get('status', '?')
    v = {**st, 'id': jid, 'job': job, 'label': label(job, st), 'status': s,
         'status_ko': STATUS_KO.get(s, s), 'platform_ko': PLATFORM_KO.get(job.get('platform'), '?'),
         'start_at': start_of(job), 'deadline': start_of(job) + job.get('wait_hours', 0) * 3600,
         'final': s in FINAL}
    v['files'] = [{'path': str(Path(f).relative_to(OUT)) if Path(f).is_relative_to(OUT) else f,
                   'size': Path(f).stat().st_size if Path(f).exists() else None}
                  for f in st.get('files', [])]
    if log_lines:
        v['log'] = [l[:400] for l in tail(JOBS / jid / 'log.txt', log_lines)]
    return v


def list_jobs(all_=False):
    ids = [d.name for d in sorted(JOBS.iterdir(), reverse=True)
           if d.is_dir() and (d / 'job.json').exists()]
    views = [job_view(i) for i in ids]
    active = [v for v in views if not v['final']]
    active.sort(key=lambda v: (v['status'] != 'recording', v['status'] == 'scheduled', v['start_at']))
    done = [v for v in views if v['final']]
    return active + (done if all_ else done[:20])


def stop_job(jid):
    _, st = load(jid)
    if st.get('status') in FINAL:
        raise RecError(f"이미 끝난 작업 ({STATUS_KO[st['status']]})")
    (JOBS / jid / 'stop').touch()
    return '녹화분 저장 후 종료' if st.get('status') == 'recording' else '대기 취소'


def remove_job(jid):
    _, st = load(jid)
    if st.get('status') not in FINAL:
        raise RecError('진행 중인 작업 — 먼저 중지')
    shutil.rmtree(JOBS / jid)


def check_url(url):
    info = classify(url)
    r = {'platform': info['platform'], 'platform_ko': PLATFORM_KO[info['platform']],
         'mode': info['mode'], 'mode_ko': '채널 대기' if info['mode'] == 'channel' else '영상'}
    if info['platform'] == 'chzzk' and info['mode'] == 'channel':
        live, title, adult = chzzk_live(info['channel_id'])
        r.update(name=chzzk_channel_name(info['channel_id']), live=live, title=title, adult=adult)
        return r
    cookie = None
    if info['platform'] in COOKIE_DOMAIN:
        cookie = write_ytdlp_cookies(info['platform'], DATA / f'.check-{os.getpid()}-{threading.get_ident()}.txt')
    try:
        res, err = yt_probe(info['channel_url'] + '/live' if info['mode'] == 'channel' else url, cookie)
    finally:
        if cookie:
            cookie.unlink(missing_ok=True)
    if res:
        r.update(live=res['live_status'] in ('is_live', 'is_upcoming'), live_status=res['live_status'],
                 title=res['title'])
    else:
        r.update(live=False, error=err)
    return r


COOKIE_KEYS = {'chzzk': ['NID_AUT', 'NID_SES'], 'youtube': ['SAPISID', '__Secure-3PSID', 'LOGIN_INFO']}


def save_cookies(platform, text):
    """쿠키 한 줄(또는 Netscape 파일 내용) 저장. 필수 쿠키가 없으면 바꾸지 않는다."""
    if platform not in COOKIE_KEYS:
        raise RecError(f'모르는 플랫폼: {platform}')
    text = (text or '').strip().strip('"\'')
    if not text:
        raise RecError('입력 없음 — 바꾸지 않음')
    names = (set(re.findall(r'(?:^|[;\s])([A-Za-z0-9_.-]+)=', text))
             | set(re.findall(r'\t([^\t]+)\t[^\t]*$', text, re.M)))
    missing = [k for k in COOKIE_KEYS[platform][:2] if k not in names]
    if missing:
        raise RecError(f"필요한 쿠키가 없음: {', '.join(missing)} — 바꾸지 않음")
    COOKIES.mkdir(parents=True, exist_ok=True)
    path = COOKIES / f'{platform}.txt'
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text + '\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cookie_status():
    """쿠키 상태 (값은 절대 포함하지 않음)"""
    out = []
    for platform, keys in COOKIE_KEYS.items():
        cookies, fmt = load_cookies(platform)
        path = COOKIES / f'{platform}.txt'
        r = {'platform': platform, 'platform_ko': PLATFORM_KO[platform], 'present': bool(cookies)}
        if cookies:
            names = {c['name'] for c in cookies}
            exp = [int(c['expires']) for c in cookies if c['name'] in keys and c['expires'].isdigit()
                   and int(c['expires']) > 0]
            r.update(format=fmt, count=len(cookies), modified=path.stat().st_mtime,
                     keys={k: k in names for k in keys}, expires=min(exp) if exp else None)
            if platform == 'chzzk':
                try:
                    c = http_json('https://comm-api.game.naver.com/nng_main/v1/user/getUserStatus',
                                  'chzzk').get('content') or {}
                    r['logged_in'] = bool(c.get('loggedIn'))
                except Exception as e:
                    r['login_error'] = str(e)
        out.append(r)
    return out


def daemon_info():
    d = read_json(DATA / 'daemon.json') or {}
    d['alive'] = bool(d) and now() - d.get('updated_at', 0) <= 30
    d['out_dir'] = os.environ.get('REC_DIR_HOST', str(OUT))
    d['tz'], d['tz_label'] = TZ_NAME, TZ_LABEL
    return d


# ---------- CLI ----------

def daemon_warning():
    return None if daemon_info()['alive'] else '⚠ 데몬이 응답하지 않음 — 컨테이너 상태 확인 필요'


def cmd_add(a):
    start_at = parse_at(a.at) if a.at else None
    job, msgs = create_job(a.url, a.hours, a.memo, a.format, a.interval, a.extra, start_at, a.quality)
    mode = '채널 방송 대기' if job['mode'] == 'channel' else '바로 받기'
    print(f"등록: {job['id']}  [{PLATFORM_KO[job['platform']]} · {mode}]  {label(job, {})}")
    if job['start_at']:
        print(f"예약: {ts(job['start_at'])} ({TZ_LABEL})부터 시작")
    if job['mode'] == 'channel' or job['platform'] == 'youtube':
        print(f"대기 한도: {ts(start_of(job) + job['wait_hours'] * 3600)} 까지 ({job['wait_hours']:g}시간)")
    for m in msgs:
        print(m)


def cmd_ls(a):
    if w := daemon_warning():
        print(w)
    rows = list_jobs(a.all)
    if not rows:
        print('작업 없음')
    for v in rows:
        s = v['status']
        if s == 'recording':
            extra = (f"{human(v.get('total_bytes', 0) + v.get('bytes', 0))}  "
                     f"{human(v.get('rate'))}/s  {dur(now() - v.get('recording_since', now()))}")
        elif s == 'scheduled':
            extra = f"{ts(v['start_at'])} 시작"
        elif s == 'waiting':
            extra = v.get('note', '')
            if v.get('checked_at'):
                extra += f" (확인 {int(now() - v['checked_at'])}초 전)"
        else:
            extra = f"{human(v.get('total_bytes'))}  파일 {len(v['files'])}개"
            if v.get('error'):
                extra += f"  {v['error'][:60]}"
        print(f"{v['id']}  {v['status_ko']:<3}  {v['platform_ko']:<3}  {v['label'][:30]}  |  {extra}")


def cmd_st(a):
    v = job_view(find_job(a.id), a.lines)
    job, s = v['job'], v['status']
    print(f"ID       {v['id']}")
    print(f"상태     {v['status_ko']}  {v.get('note', '') if not v['final'] else ''}")
    print(f"대상     {v['platform_ko']} · {'채널 대기' if job['mode'] == 'channel' else '영상'}  {v['label']}")
    print(f"주소     {job['url']}")
    print(f"등록     {ts(job['created_at'])}" + (f"   예약 {ts(job['start_at'])}" if job.get('start_at') else '')
          + f"   대기 한도 {ts(v['deadline'])}")
    if v.get('live_title'):
        print(f"방송     {v['live_title']}" + ('  (성인)' if v.get('adult') else ''))
    if v.get('checked_at'):
        print(f"마지막 확인 {ts(v['checked_at'])}")
    if s == 'recording':
        print(f"녹화     {dur(now() - v.get('recording_since', now()))} 경과  "
              f"현재 파일 {human(v.get('bytes'))}  속도 {human(v.get('rate'))}/s")
    if v.get('reconnects'):
        print(f"재연결   {v['reconnects']}회")
    if v.get('finished_at'):
        print(f"종료     {ts(v['finished_at'])}")
    if v.get('error'):
        print(f"오류     {v['error']}")
    for f in v['files']:
        print(f"파일     {f['path']}  {human(f['size']) if f['size'] is not None else '(없음)'}")
    print(f'여유공간 {free_gb():.0f}GB')
    if w := daemon_warning():
        print(w)
    print('--- 로그 ---')
    for line in v.get('log', []):
        print(line[:300])


def cmd_stop(a):
    jid = find_job(a.id)
    print(f'{jid} 중지 요청 — {stop_job(jid)}')


def cmd_rm(a):
    jid = find_job(a.id)
    remove_job(jid)
    print(f'{jid} 기록 삭제 (녹화 파일은 그대로)')


def cmd_check(a):
    r = check_url(a.url)
    print(f"판별: {r['platform_ko']} · {r['mode_ko']}")
    if r.get('name'):
        print(f"채널: {r['name']}")
    if r.get('error'):
        print(f"결과: {r['error']}")
    else:
        print(f"방송: {'켜짐 — ' + str(r.get('title')) if r['live'] else '꺼짐'}"
              + (f"  ({r['live_status']})" if r.get('live_status') else '')
              + ('  (성인)' if r.get('adult') else ''))


def cmd_cookies(a):
    if a.action == 'set':
        if sys.stdin.isatty():
            import getpass
            print(f"{PLATFORM_KO[a.platform]} 쿠키를 붙여넣고 Enter (입력 내용은 화면에 표시되지 않음)")
            print("  형식: NID_AUT=...; NID_SES=...   (크롬 확장 '치지직 도우미' → 한 줄로 복사)"
                  if a.platform == 'chzzk' else "  형식: 이름=값; 이름=값 ...")
            text = getpass.getpass('> ')
        else:
            text = sys.stdin.read()  # 파이프: Netscape 파일도 그대로 넣을 수 있다
        save_cookies(a.platform, text)
        print(f'저장함: {COOKIES / (a.platform + ".txt")}')
    for r in cookie_status():
        if not r['present']:
            print(f"{r['platform_ko']}: 없음 ({COOKIES / (r['platform'] + '.txt')})")
            continue
        line = (f"{r['platform_ko']}: {r['format']} 형식, {r['count']}개, 수정 {ts(r['modified'])}"
                f"  핵심 쿠키 " + ' '.join(f"{k}{'✓' if ok else '✗'}" for k, ok in r['keys'].items()))
        if r.get('expires'):
            line += f"  가장 빠른 만료 {ts(r['expires'])}"
        if 'logged_in' in r:
            line += f"  로그인 {'확인됨' if r['logged_in'] else '안 됨(만료?)'}"
        elif r.get('login_error'):
            line += f"  로그인 확인 실패({r['login_error']})"
        print(line)


def cmd_auth(a):
    """웹 화면 OIDC 로그인 설정 상태 / 클라이언트 시크릿 입력 (값은 출력하지 않음)"""
    d = DATA / 'auth'
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    f = d / 'oidc_client_secret'
    if a.action == 'secret':
        if sys.stdin.isatty():
            import getpass
            print("OIDC(OAuth2) 앱의 클라이언트 시크릿을 붙여넣고 Enter (화면에 표시되지 않음)")
            text = getpass.getpass('> ')
        else:
            text = sys.stdin.read()
        text = text.strip()
        if len(text) < 16:
            raise RecError('시크릿이 너무 짧음 — 바꾸지 않음')
        tmp = f.with_name(f.name + '.tmp')
        tmp.write_text(text + '\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, f)
        print('저장함 — 바로 적용됨')
    cid = os.environ.get('REC_OIDC_CLIENT_ID', '').strip()
    secret = f.read_text().strip() if f.exists() else ''
    issuer = os.environ.get('REC_OIDC_ISSUER', '').strip()
    public = os.environ.get('REC_PUBLIC_URL', '').strip().rstrip('/')
    allowed = os.environ.get('REC_ALLOWED_USERS', '').strip()
    print(f"발급자(issuer) {issuer or '없음 (.env REC_OIDC_ISSUER)'}")
    print(f"클라이언트 ID  {cid or '없음 (.env REC_OIDC_CLIENT_ID)'}")
    print(f"시크릿         {f'있음 ({len(secret)}자)' if secret else '없음 (rec auth secret)'}")
    print(f"공개 주소      {public or '없음 (.env REC_PUBLIC_URL)'}"
          + (f" → 리디렉션 URI {public}/auth/callback" if public else ''))
    print(f"허용 계정      {allowed or '없음 (.env REC_ALLOWED_USERS)'}")
    on = all([cid, secret, issuer, public, allowed])
    print('상태           ' + ('로그인 필요 (켜짐)' if on else '꺼짐 — 리버스 프록시를 거치지 않은 사설망 직접 접속만 허용'))


def cmd_update(a):
    if daemon_info().get('recording'):
        print('⚠ 녹화 중인 작업이 있음 — 진행 중인 yt-dlp에는 영향 없음')
    print(update_ytdlp())


def main():
    p = argparse.ArgumentParser(prog='rec', description='치지직·유튜브 녹화 대기열')
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('add', help='대기 등록')
    s.add_argument('url')
    s.add_argument('-H', '--hours', type=float, default=12, help='방송 대기 최대 시간 (기본 12)')
    s.add_argument('-a', '--at', help='예약 시작 (TZ 기준, 기본 한국시간): "2026-10-04 21:00", "10-04 21:00", "21:00"')
    s.add_argument('-m', '--memo', help='목록에 보일 이름')
    s.add_argument('-q', '--quality', type=int, help='최대 해상도 (예: 1080, 720)')
    s.add_argument('-f', '--format', help='yt-dlp 포맷 (예: "bv*[height<=720]+ba/b")')
    s.add_argument('-i', '--interval', type=int, help='방송 확인 간격 초 (치지직 30, 유튜브 120)')
    s.add_argument('-x', '--extra', help='yt-dlp 추가 인자 (따옴표로 묶어서)')
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser('ls', help='목록')
    s.add_argument('-a', '--all', action='store_true')
    s.set_defaults(fn=cmd_ls)
    s = sub.add_parser('st', help='상세 상태')
    s.add_argument('id')
    s.add_argument('-n', '--lines', type=int, default=15)
    s.set_defaults(fn=cmd_st)
    for name, fn, h in (('stop', cmd_stop, '중지/취소'), ('rm', cmd_rm, '끝난 작업 기록 삭제')):
        s = sub.add_parser(name, help=h)
        s.add_argument('id')
        s.set_defaults(fn=fn)
    s = sub.add_parser('check', help='등록 없이 확인')
    s.add_argument('url')
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser('cookies', help='쿠키 상태 / rec cookies set chzzk 로 입력')
    s.add_argument('action', nargs='?', choices=['set'])
    s.add_argument('platform', nargs='?', choices=['chzzk', 'youtube'], default='chzzk')
    s.set_defaults(fn=cmd_cookies)
    s = sub.add_parser('auth', help='웹 OIDC 로그인 상태 / rec auth secret 으로 시크릿 입력')
    s.add_argument('action', nargs='?', choices=['secret'])
    s.set_defaults(fn=cmd_auth)
    sub.add_parser('update', help='yt-dlp 업데이트').set_defaults(fn=cmd_update)
    sub.add_parser('daemon', help='데몬 실행').set_defaults(fn=lambda a: daemon())
    a = p.parse_args()
    try:
        a.fn(a)
    except RecError as e:
        sys.exit(str(e))


if __name__ == '__main__':
    main()
