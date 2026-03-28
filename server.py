#!/usr/bin/env python3
import os
import requests
from pathlib import Path
from functools import wraps
from flask import (Flask, request, jsonify, send_from_directory,
                   Response, session, redirect, url_for)

app = Flask(__name__, static_folder='public')

ALLOWED_EXTENSIONS = {'.wav', '.mp3'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def load_env():
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()

app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')


# ── Auth helpers ─────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def get_api_key():
    return os.environ.get('ELEVENLABS_API_KEY', '').strip() or None


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        valid_user = os.environ.get('LOGIN_USER', '')
        valid_pass = os.environ.get('LOGIN_PASSWORD', '')
        if username == valid_user and password == valid_pass:
            session['logged_in'] = True
            return redirect(url_for('index'))
        return redirect(url_for('login') + '?error=1')
    return send_from_directory(app.static_folder, 'login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── App routes ────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/clone-voice', methods=['POST'])
@login_required
def clone_voice():
    api_key = get_api_key()
    if not api_key:
        return jsonify({'error': 'ElevenLabs API key not configured'}), 500

    if 'audio' not in request.files:
        return jsonify({'error': 'No audio file uploaded'}), 400

    audio_file = request.files['audio']
    voice_name = request.form.get('voiceName', '').strip()
    description = request.form.get('description', '').strip()
    noise_reduction = request.form.get('noiseReduction', 'false').lower() == 'true'

    if not voice_name:
        return jsonify({'error': 'Voice name is required'}), 400

    ext = Path(audio_file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({'error': 'Only .wav and .mp3 files are supported'}), 400

    audio_bytes = audio_file.read()
    if len(audio_bytes) > MAX_FILE_SIZE:
        return jsonify({'error': 'File size must be under 50 MB'}), 400

    content_type = 'audio/wav' if ext == '.wav' else 'audio/mpeg'
    files = [('files', (audio_file.filename, audio_bytes, content_type))]
    data = {'name': voice_name, 'remove_background_noise': 'true' if noise_reduction else 'false'}
    if description:
        data['description'] = description

    try:
        resp = requests.post(
            'https://api.elevenlabs.io/v1/voices/add',
            headers={'xi-api-key': api_key},
            files=files, data=data, timeout=120
        )
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Network error: {str(e)}'}), 502

    try:
        resp_data = resp.json()
    except Exception:
        resp_data = {}

    if not resp.ok:
        detail = resp_data.get('detail', {})
        msg = detail.get('message', str(resp_data)) if isinstance(detail, dict) else str(detail)
        return jsonify({'error': msg}), resp.status_code

    return jsonify({'success': True, 'voiceId': resp_data.get('voice_id'), 'voiceName': voice_name})


@app.route('/api/voices')
@login_required
def list_voices():
    api_key = get_api_key()
    if not api_key:
        return jsonify({'error': 'API key not configured'}), 500
    try:
        resp = requests.get('https://api.elevenlabs.io/v1/voices',
                            headers={'xi-api-key': api_key}, timeout=30)
        data = resp.json()
        cloned = [v for v in data.get('voices', []) if v.get('category') == 'cloned']
        return jsonify({'voices': cloned})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tts/<voice_id>', methods=['POST'])
@login_required
def text_to_speech(voice_id):
    api_key = get_api_key()
    if not api_key:
        return jsonify({'error': 'ElevenLabs API key not configured'}), 500

    body = request.get_json()
    if not body or not body.get('text', '').strip():
        return jsonify({'error': 'Text is required'}), 400

    text = body['text'].strip()
    if len(text) > 5000:
        return jsonify({'error': 'Text must be under 5000 characters'}), 400

    payload = {
        'text': text,
        'model_id': body.get('model_id', 'eleven_multilingual_v2'),
        'voice_settings': {
            'stability': float(body.get('voice_settings', {}).get('stability', 0.5)),
            'similarity_boost': float(body.get('voice_settings', {}).get('similarity_boost', 0.75)),
            'style': float(body.get('voice_settings', {}).get('style', 0.0)),
            'use_speaker_boost': bool(body.get('voice_settings', {}).get('use_speaker_boost', True))
        }
    }

    try:
        resp = requests.post(
            f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',
            headers={'xi-api-key': api_key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'},
            json=payload, timeout=60
        )
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Network error: {str(e)}'}), 502

    if not resp.ok:
        try:
            err = resp.json()
            detail = err.get('detail', {})
            msg = detail.get('message', str(err)) if isinstance(detail, dict) else str(detail)
        except Exception:
            msg = f'ElevenLabs error {resp.status_code}'
        return jsonify({'error': msg}), resp.status_code

    return Response(resp.content, status=200, mimetype='audio/mpeg',
                    headers={'Content-Disposition': 'inline; filename="speech.mp3"'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    print(f'Voice Clone server running at http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
