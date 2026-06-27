import subprocess
import threading
import os
import signal
import time
import requests
import json
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='.')

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"

mpv_proc = None
mpv_lock = threading.Lock()

last_play_payload = {}
TOKENS_FILE = 'anilist_tokens.json'
SESSIONS_FILE = 'watched_sessions.json'
OAUTH_STATE_STORE = {}


def anilist_save_progress(token, media_id, progress=1):
    """Save or update a media list entry on Anilist using GraphQL.
    Expects a valid OAuth access token with the required scopes.
    Returns (True, data) on success or (False, error) on failure.
    """
    if not token or not media_id:
        return False, 'missing token or media_id'
    url = 'https://graphql.anilist.co'
    query = '''
    mutation ($mediaId: Int, $progress: Int) {
      SaveMediaListEntry(mediaId: $mediaId, progress: $progress) {
        id
        progress
        status
      }
    }
    '''
    variables = { 'mediaId': int(media_id), 'progress': int(progress) }
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    try:
        r = requests.post(url, json={'query': query, 'variables': variables}, headers=headers, timeout=10)
        r.raise_for_status()
        j = r.json()
        if 'errors' in j:
            return False, j['errors']
        return True, j.get('data')
    except Exception as e:
        return False, str(e)


def anilist_search(title):
    """Search Anilist for a media by title. Returns (True, {id, title}) or (False, error)"""
    if not title:
        return False, 'missing title'
    url = 'https://graphql.anilist.co'
    query = '''
    query ($search: String) {
      Media(search: $search, type: ANIME) {
        id
        title { romaji english native }
      }
    }
    '''
    variables = { 'search': title }
    try:
        r = requests.post(url, json={'query': query, 'variables': variables}, timeout=10)
        r.raise_for_status()
        j = r.json()
        if 'errors' in j:
            return False, j['errors']
        data = j.get('data', {}).get('Media')
        if not data:
            return False, 'no match'
        return True, data
    except Exception as e:
        return False, str(e)


def save_tokens_local(data):
    try:
        with open(TOKENS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return True
    except Exception:
        return False


def load_tokens_local():
    try:
        with open(TOKENS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def load_sessions_local():
    try:
        with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def save_sessions_local(data):
    try:
        with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


@app.route('/anilist/search', methods=['POST'])
def anilist_search_route():
    data = request.json or {}
    title = data.get('title')
    ok, res = anilist_search(title)
    if ok:
        return jsonify(success=True, media=res)
    return jsonify(success=False, error=res), 400


SESSION_START_CACHE = {}

@app.route('/anilist/push', methods=['POST'])
def anilist_push_route():
    data = request.json or {}
    token = data.get('token') or os.environ.get('ANILIST_TOKEN')
    if not token:
        token = load_tokens_local().get('access_token')
    media_id = data.get('media_id') or data.get('mediaId')
    progress = int(data.get('progress', 1))
    increment = data.get('increment', False)
    session_start = data.get('start') or data.get('startEpisode') or data.get('start_episode')
    if session_start is not None and session_start != '':
        try:
            session_start = int(session_start)
        except ValueError:
            session_start = None

    if not media_id and data.get('title'):
        ok, found = anilist_search(data.get('title'))
        if not ok:
            return jsonify(success=False, error=found), 400
        media_id = found.get('id')

    cache_key = media_id or data.get('title')
    if session_start is not None and cache_key:
        SESSION_START_CACHE[cache_key] = session_start
    elif cache_key and cache_key in SESSION_START_CACHE:
        session_start = SESSION_START_CACHE[cache_key]

    if not token or not media_id:
        return jsonify(success=False, error='missing token or media_id'), 400

    if increment:
        progress = progress + 1

    ok, res = anilist_save_progress(token, media_id, progress)
    if ok:
        return jsonify(success=True, result=res, session_start=session_start)
    return jsonify(success=False, error=res), 500


@app.route('/auth/start')
def auth_start():
    # Start OAuth authorization by redirecting to AniList authorize URL.
    client_id = request.args.get('client_id') or os.environ.get('ANILIST_CLIENT_ID')
    client_secret = request.args.get('client_secret') or os.environ.get('ANILIST_CLIENT_SECRET')
    redirect = request.args.get('redirect_uri') or 'http://localhost:5000/callback'
    if not client_id:
        return jsonify(success=False, error='client_id missing; set ANILIST_CLIENT_ID or pass client_id'), 400
    state = None
    if client_secret:
        import secrets
        state = secrets.token_urlsafe(16)
        OAUTH_STATE_STORE[state] = {
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect,
        }
    auth_url = f'https://anilist.co/api/v2/oauth/authorize?client_id={client_id}&redirect_uri={redirect}&response_type=code'
    if state:
        auth_url += f'&state={state}'
    return '', 302, {'Location': auth_url}


@app.route('/callback')
def oauth_callback():
    # AniList redirects here with ?code=...
    code = request.args.get('code')
    if not code:
        return '<h3>Authorization failed: no code provided</h3>', 400

    state = request.args.get('state')
    client_id = None
    client_secret = None
    redirect = request.args.get('redirect_uri') or 'http://localhost:5000/callback'
    if state and state in OAUTH_STATE_STORE:
        stored = OAUTH_STATE_STORE.pop(state)
        client_id = stored.get('client_id')
        client_secret = stored.get('client_secret')
        redirect = stored.get('redirect_uri', redirect)
    else:
        client_id = request.args.get('client_id') or os.environ.get('ANILIST_CLIENT_ID')
        client_secret = request.args.get('client_secret') or os.environ.get('ANILIST_CLIENT_SECRET')

    if not client_id or not client_secret:
        return '<h3>Server missing client credentials. Provide client_id/secret in settings or set ANILIST_CLIENT_ID/ANILIST_CLIENT_SECRET.</h3>', 500
    token_url = 'https://anilist.co/api/v2/oauth/token'
    data = {
        'grant_type': 'authorization_code',
        'client_id': client_id,
        'client_secret': client_secret,
        'code': code,
        'redirect_uri': redirect,
    }
    try:
        r = requests.post(token_url, data=data, timeout=10)
        r.raise_for_status()
        tokens = r.json()
        # persist tokens locally
        save_tokens_local(tokens)
        # Return a small HTML page that posts token to opener and closes
        html = f"""
        <html><body>
        <script>
        try {{
          window.opener && window.opener.postMessage({json.dumps(tokens)}, '*');
        }} catch(e){{}}
        document.write('<h3>Authorized — you can close this window.</h3>');
        setTimeout(()=>window.close(), 1200);
        </script>
        </body></html>
        """
        return html
    except requests.HTTPError as he:
        resp = he.response
        body = ''
        try:
            body = resp.text
        except Exception:
            body = str(he)
        return f'<h3>Token exchange failed: {resp.status_code} {body}</h3>', 500
    except Exception as e:
        return f'<h3>Token exchange failed: {e}</h3>', 500


@app.route('/auth/exchange', methods=['POST'])
def auth_exchange():
    data = request.json or {}
    code = data.get('code')
    redirect = data.get('redirect_uri') or 'http://localhost:5000/callback'
    client_id = data.get('client_id') or os.environ.get('ANILIST_CLIENT_ID')
    client_secret = data.get('client_secret') or os.environ.get('ANILIST_CLIENT_SECRET')
    if not code:
        return jsonify(success=False, error='missing code'), 400
    if not client_id or not client_secret:
        return jsonify(success=False, error='server missing client credentials; provide client_id/secret'), 500
    token_url = 'https://anilist.co/api/v2/oauth/token'
    payload = {
        'grant_type': 'authorization_code',
        'client_id': client_id,
        'client_secret': client_secret,
        'code': code,
        'redirect_uri': redirect,
    }
    try:
        r = requests.post(token_url, data=payload, timeout=10)
        r.raise_for_status()
        tokens = r.json()
        save_tokens_local(tokens)
        return jsonify(success=True, tokens=tokens)
    except requests.HTTPError as he:
        resp = he.response
        body = ''
        try:
            body = resp.text
        except Exception:
            body = str(he)
        return jsonify(success=False, error=f'{resp.status_code} {body}'), 500
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500

@app.route('/auth/token', methods=['GET'])
def auth_token_route():
    tokens = load_tokens_local()
    if not tokens or not tokens.get('access_token'):
        return jsonify(success=False, error='no saved token'), 404
    return jsonify(success=True, access_token=tokens.get('access_token'), refresh_token=tokens.get('refresh_token'))


@app.route('/session/save', methods=['POST'])
def session_save_route():
    data = request.json or {}
    title = (data.get('title') or '').strip()
    media_id = data.get('media_id') or data.get('mediaId') or ''
    progress = data.get('progress') or data.get('end') or data.get('episode') or ''
    url = (data.get('url') or '').strip()
    env = (data.get('env') or '').strip()

    if not title and not media_id:
        return jsonify(success=False, error='missing title or media_id'), 400
    if not progress and not data.get('start') and not data.get('end') and not data.get('episode'):
        return jsonify(success=False, error='missing progress or range'), 400

    session = {
        'id': str(int(time.time() * 1000)),
        'created_at': datetime.utcnow().isoformat() + 'Z',
        'env': env,
        'url': url,
        'payload': data,
        'status': 'pending',
    }
    sessions = load_sessions_local()
    sessions.append(session)
    save_sessions_local(sessions)
    return jsonify(success=True, session=session)


@app.route('/session/list', methods=['GET'])
def session_list_route():
    return jsonify(success=True, sessions=load_sessions_local())


@app.route('/session/delete', methods=['POST'])
def session_delete_route():
    data = request.json or {}
    session_id = data.get('id')
    if not session_id:
        return jsonify(success=False, error='missing id'), 400
    sessions = load_sessions_local()
    new_sessions = [s for s in sessions if s.get('id') != session_id]
    if len(new_sessions) == len(sessions):
        return jsonify(success=False, error='not found'), 404
    save_sessions_local(new_sessions)
    return jsonify(success=True)


@app.route('/session/push', methods=['POST'])
def session_push_route():
    data = request.json or {}
    token = data.get('token') or data.get('anilist_token') or os.environ.get('ANILIST_TOKEN') or load_tokens_local().get('access_token')
    if not token:
        return jsonify(success=False, error='missing token'), 400

    sessions = load_sessions_local()
    results = []
    remaining = []

    for session in sessions:
        payload = session.get('payload', {}) if isinstance(session.get('payload'), dict) else {}
        title = (payload.get('title') or '').strip()
        media_id = payload.get('media_id') or payload.get('mediaId') or ''
        progress_value = payload.get('progress') or payload.get('end') or payload.get('episode') or ''
        start = payload.get('start') or payload.get('startEpisode') or payload.get('start_episode') or ''

        item = {
            'id': session.get('id'),
            'title': title,
            'media_id': media_id,
            'payload': payload,
        }

        if not progress_value and not start:
            item['success'] = False
            item['error'] = 'missing progress or range'
            results.append(item)
            remaining.append(session)
            continue

        if not media_id and title:
            ok, found = anilist_search(title)
            if not ok:
                item['success'] = False
                item['error'] = found
                results.append(item)
                remaining.append(session)
                continue
            media_id = found.get('id')

        if not media_id:
            item['success'] = False
            item['error'] = 'missing media id or title'
            results.append(item)
            remaining.append(session)
            continue

        try:
            progress = int(progress_value)
        except Exception:
            progress = 1

        ok, res = anilist_save_progress(token, media_id, progress)
        if ok:
            item['success'] = True
            item['result'] = res
        else:
            item['success'] = False
            item['error'] = res
            remaining.append(session)
        results.append(item)

    save_sessions_local(remaining)
    return jsonify(success=True, results=results, remaining=len(remaining))


def kill_mpv():
    global mpv_proc
    with mpv_lock:
        if mpv_proc and mpv_proc.poll() is None:
            try:
                if os.name == 'posix':
                    os.killpg(os.getpgid(mpv_proc.pid), signal.SIGTERM)
                else:
                    mpv_proc.terminate()
            except Exception:
                mpv_proc.terminate()
            mpv_proc = None


@app.route('/')
def index():
    return send_from_directory('.', 'page.html')


@app.route('/play', methods=['POST'])
def play():
    global mpv_proc
    data = request.json or {}
    env = data.get('env', 'mpv-arch')
    url = data.get('url', '').strip()
    referer = data.get('referer', 'https://kwik.cx/').strip()
    winuser = data.get('winuser', 'vaibh').strip()
    extra = data.get('extra', '').strip()

    if not url:
        return jsonify(success=False, error='no URL provided'), 400

    kill_mpv()

    origin = referer.rstrip('/')

    base_flags = [
        f'--http-header-fields=Referer: {referer},Origin: {origin}',
        f'--user-agent={UA}',
        '--demuxer=lavf',
        '--demuxer-lavf-format=hls',
        '--cache=yes',
        '--demuxer-max-bytes=100MiB',
    ]

    if extra:
        base_flags += extra.split()

    if env == 'mpv-arch':
        # Run mpv in WSL (Arch Linux)
        cmd = ['wsl', 'mpv'] + base_flags + [url]
    elif env == 'mpv-win':
        mpv_exe = 'C:\\Program Files\\MPV Player\\mpv.exe'
        cmd = [mpv_exe] + base_flags + [url]
    elif env == 'ani-cli':
        episode = data.get('episode', '')
        quality = data.get('quality', '')
        flags = data.get('flags') or data.get('ani_cli_flags') or ''
        ani_cmd = ['ani-cli']
        has_e = False
        has_q = False

        if flags:
            try:
                import shlex
                flag_parts = shlex.split(flags)
            except Exception:
                flag_parts = flags.split()
            ani_cmd += flag_parts
            has_e = any(part == '-e' or part == '--episode' or part.startswith('-e') for part in flag_parts)
            has_q = any(part.startswith('-q') or part == '--quality' for part in flag_parts)
        else:
            ani_cmd += ['--select-nth', '1', '--exit-after-play']

        if episode and not has_e:
            ani_cmd += ['-e', episode]
        if quality and not has_q:
            ani_cmd += ['-q', quality + 'p']
        if url:
            ani_cmd.append(url)
        cmd = ['wsl'] + ani_cmd
    else:
        return jsonify(success=False, error='unknown env'), 400

    try:
        with mpv_lock:
            popen_kwargs = {
                'stdout': subprocess.DEVNULL,
                'stderr': subprocess.DEVNULL,
            }
            # os.setsid is Unix-only; on Windows, use creationflags instead
            if os.name == 'posix':
                popen_kwargs['preexec_fn'] = os.setsid
            else:
                popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
            
            proc = subprocess.Popen(cmd, **popen_kwargs)
            mpv_proc = proc

        # Prepare response
        response = {'success': True, 'pid': proc.pid, 'cmd': ' '.join(cmd)}

        # Optional: update Anilist media list entry if provided
        anilist_token = data.get('anilist_token') or os.environ.get('ANILIST_TOKEN')
        if not anilist_token:
            anilist_token = load_tokens_local().get('access_token')
        anilist_media_id = data.get('anilist_media_id') or data.get('anilistMediaId')
        anilist_progress = data.get('anilist_progress', 1)
        if anilist_media_id and anilist_token:
            ok, res = anilist_save_progress(anilist_token, anilist_media_id, anilist_progress)
            response['anilist'] = {'ok': ok, 'result': res}

        return jsonify(response)
    except FileNotFoundError as e:
        return jsonify(success=False, error=f'executable not found: {e}'), 500
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500


@app.route('/stop', methods=['POST'])
def stop():
    kill_mpv()
    return jsonify(success=True)


@app.route('/shutdown', methods=['POST'])
def shutdown():
    func = request.environ.get('werkzeug.server.shutdown')
    if not func:
        return jsonify(success=False, error='shutdown unavailable'), 500
    func()
    return jsonify(success=True, message='server shutting down')


@app.route('/status', methods=['GET'])
def status():
    global mpv_proc
    with mpv_lock:
        running = mpv_proc is not None and mpv_proc.poll() is None
    return jsonify(running=running)


if __name__ == '__main__':
    print('\n  ani-launcher running at http://localhost:5000\n')
    app.run(host='0.0.0.0', port=5000, debug=False)