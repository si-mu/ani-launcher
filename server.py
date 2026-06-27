import subprocess
import threading
import os
import signal
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='.')

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"

mpv_proc = None
mpv_lock = threading.Lock()


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
        ani_cmd = ['ani-cli']
        # Auto-select first result and exit after play
        ani_cmd += ['--select-nth', '1', '--exit-after-play']
        if episode:
            ani_cmd += ['-e', episode]
        if quality:
            ani_cmd += ['-q', quality + 'p']
        if url:
            ani_cmd.append(url)
        # Run ani-cli in WSL
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
            
            # For ani-cli, don't track the process since it spawns mpv as a child
            # Let ani-cli and mpv run independently in the background
            if env == 'ani-cli':
                mpv_proc = None
            else:
                mpv_proc = proc
        
        return jsonify(success=True, pid=proc.pid, cmd=' '.join(cmd))
    except FileNotFoundError as e:
        return jsonify(success=False, error=f'executable not found: {e}'), 500
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500


@app.route('/stop', methods=['POST'])
def stop():
    kill_mpv()
    return jsonify(success=True)


@app.route('/status', methods=['GET'])
def status():
    global mpv_proc
    with mpv_lock:
        running = mpv_proc is not None and mpv_proc.poll() is None
    return jsonify(running=running)


if __name__ == '__main__':
    print('\n  ani-launcher running at http://localhost:5000\n')
    app.run(host='0.0.0.0', port=5000, debug=False)