"""live-rec MCP 서버 — 에이전트(헤르메스 등)가 녹화를 등록·조회·중지한다.

Streamable HTTP의 상태 없는 최소 구현: POST /mcp 하나에 JSON 응답 하나 (스트리밍·세션 없음).
인증은 고정 Bearer 토큰(REC_MCP_TOKEN, 32자 이상) 하나. web.py가 Origin·프록시 경유 요청을 먼저 거른다.
쿠키 입력과 기록 삭제는 도구로 열지 않는다.
"""
import json

PROTOCOL_VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05']

job_id = {'type': 'string', 'description': '작업 ID (rec_list의 id). 앞부분만 써도 된다'}

TOOLS = [
    {
        'name': 'rec_check',
        'description': '등록 없이 주소가 지금 방송 중인지 확인합니다. 치지직 채널이면 채널명·방송 제목·성인 방송 여부도 돌려줍니다.',
        'inputSchema': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': '치지직·유튜브 채널 또는 영상 주소'}},
            'required': ['url'], 'additionalProperties': False},
        'annotations': {'title': '방송 확인', 'readOnlyHint': True, 'openWorldHint': True},
    },
    {
        'name': 'rec_add',
        'description': ('녹화를 등록합니다. 채널 주소면 방송이 켜지는 순간부터 녹화하고, 영상 주소면 바로 받습니다. '
                        'start_at을 주면 그 시각부터 hours시간 동안 방송을 기다립니다. 시각은 한국시간입니다. '
                        '녹화는 서버에서 백그라운드로 진행되니 등록 결과만 바로 알려주세요. '
                        '"N분만 녹화"처럼 길이를 정하면 minutes를 넣으세요. 서버가 그 시간 뒤 저장하고 자동으로 멈추므로 '
                        '기다리거나 rec_stop을 부를 필요가 없습니다.'),
        'inputSchema': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': '치지직·유튜브 채널 또는 영상 주소'},
            'hours': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 72,
                      'description': '방송을 기다릴 최대 시간 (기본 12)'},
            'start_at': {'type': 'string',
                         'description': '예약 시작 시각 (한국시간). "2026-10-04 21:00", "10-04 21:00", "21:00"(지났으면 내일). 생략하면 지금부터'},
            'quality': {'type': 'integer', 'description': '최대 해상도 (예: 1080, 720). 생략하면 최고 화질'},
            'minutes': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 4320,
                        'description': '녹화 길이(분). 녹화가 실제로 시작된 뒤 이만큼 지나면 저장하고 자동 종료. 생략하면 방송이 끝날 때까지'},
            'memo': {'type': 'string', 'description': '목록에 보일 이름'}},
            'required': ['url'], 'additionalProperties': False},
        'annotations': {'title': '녹화 등록', 'readOnlyHint': False, 'destructiveHint': False,
                        'idempotentHint': False, 'openWorldHint': True},
    },
    {
        'name': 'rec_list',
        'description': '녹화 작업 목록입니다. 진행 중(녹화중·대기·예약)이 먼저 오고, 끝난 작업은 최근 20개까지 붙습니다.',
        'inputSchema': {'type': 'object', 'properties': {
            'all': {'type': 'boolean', 'description': 'true면 끝난 작업을 모두 포함'}},
            'additionalProperties': False},
        'annotations': {'title': '녹화 목록', 'readOnlyHint': True, 'openWorldHint': False},
    },
    {
        'name': 'rec_status',
        'description': '작업 하나의 상세 상태와 최근 로그 30줄입니다.',
        'inputSchema': {'type': 'object', 'properties': {'id': job_id},
                        'required': ['id'], 'additionalProperties': False},
        'annotations': {'title': '녹화 상태', 'readOnlyHint': True, 'openWorldHint': False},
    },
    {
        'name': 'rec_stop',
        'description': ('대기를 취소하거나, 녹화 중이면 지금까지 분량을 저장하고 멈춥니다. '
                        '사용자가 이번 대화에서 명시적으로 중지를 요청했을 때만 호출하고 confirm에 true를 넣으세요.'),
        'inputSchema': {'type': 'object', 'properties': {
            'id': job_id,
            'confirm': {'type': 'boolean', 'const': True, 'description': '사용자가 중지를 명시적으로 요청했으면 true'}},
            'required': ['id', 'confirm'], 'additionalProperties': False},
        'annotations': {'title': '녹화 중지', 'readOnlyHint': False, 'destructiveHint': True,
                        'idempotentHint': True, 'openWorldHint': False},
    },
]


def _brief(rec, v):
    """목록용 요약. 토큰을 아끼려고 로그·원본 작업 정보는 뺀다"""
    out = {'id': v['id'], 'label': v['label'], 'status': v['status_ko'], 'platform': v['platform_ko'],
           'start_at': rec.ts(v['start_at']), 'deadline': rec.ts(v['deadline'])}
    if v.get('live_title'):
        out['live_title'] = v['live_title']
    if v['files']:
        out['files'] = [f['path'] for f in v['files']]
        out['size'] = rec.human(sum(f['size'] or 0 for f in v['files']))
    if v['job'].get('max_minutes'):
        out['max_minutes'] = v['job']['max_minutes']
    if v.get('note'):
        out['note'] = v['note']
    return out


def _call(rec, name, args):
    if name == 'rec_check':
        return rec.check_url((args.get('url') or '').strip())
    if name == 'rec_add':
        start_at = rec.parse_at(args['start_at']) if args.get('start_at') else None
        job, msgs = rec.create_job(args.get('url'), args.get('hours') or 12, args.get('memo'),
                                   start_at=start_at, quality=args.get('quality') or None,
                                   max_minutes=args.get('minutes') or None)
        return {'id': job['id'], 'label': rec.label(job, {}), 'platform': rec.PLATFORM_KO.get(job['platform']),
                'start_at': rec.ts(rec.start_of(job)), 'wait_hours': job['wait_hours'],
                'max_minutes': job['max_minutes'], 'messages': msgs}
    if name == 'rec_list':
        info = rec.daemon_info()
        return {'daemon_alive': info['alive'], 'now': rec.ts(), 'tz': info['tz_label'],
                'jobs': [_brief(rec, v) for v in rec.list_jobs(bool(args.get('all')))]}
    if name == 'rec_status':
        v = rec.job_view(rec.find_job(str(args.get('id') or '')), 30)
        return {**_brief(rec, v), 'url': v['job'].get('url'), 'log': v.get('log', [])}
    if name == 'rec_stop':
        if args.get('confirm') is not True:
            raise rec.RecError('confirm: true가 필요합니다 (사용자가 중지를 요청했을 때만)')
        jid = rec.find_job(str(args.get('id') or ''))
        return {'id': jid, 'message': rec.stop_job(jid)}
    raise KeyError(name)


def handle(rec, msg):
    """JSON-RPC 요청 하나 → (HTTP 코드, 응답 본문). 알림(id 없음)은 (202, None)"""
    if not isinstance(msg, dict) or 'id' not in msg or 'method' not in msg:
        return 202, None
    mid = msg['id']

    def error(code, message):
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}

    if msg.get('jsonrpc') != '2.0' or not isinstance(msg['method'], str):
        return 400, error(-32600, 'Invalid Request')
    params = msg.get('params') if isinstance(msg.get('params'), dict) else {}
    method = msg['method']
    if method == 'initialize':
        v = params.get('protocolVersion')
        result = {'protocolVersion': v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'live-rec', 'version': '1.0.0'},
                  'instructions': ('치지직·유튜브 방송을 예약 녹화합니다. 등록 전에 rec_check로 주소를 확인할 수 있고, '
                                   'rec_list로 진행 상황을 봅니다. 중지(rec_stop)는 사용자가 명시적으로 요청했을 때만 하세요.')}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': TOOLS}
    elif method == 'tools/call':
        name = params.get('name')
        if name not in {t['name'] for t in TOOLS}:
            return 200, error(-32602, f'알 수 없는 도구입니다: {name}')
        args = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
        try:
            out = _call(rec, name, args)
            result = {'content': [{'type': 'text', 'text': json.dumps(out, ensure_ascii=False)}]}
        except rec.RecError as e:
            result = {'content': [{'type': 'text', 'text': str(e)}], 'isError': True}
        except Exception as e:
            print(f'[{rec.ts()}] MCP {name} 오류: {e}', flush=True)
            result = {'content': [{'type': 'text', 'text': f'서버 오류: {e}'}], 'isError': True}
    else:
        return 200, error(-32601, f'지원하지 않는 메서드입니다: {method}')
    return 200, {'jsonrpc': '2.0', 'id': mid, 'result': result}
