import hashlib
import hmac
import json
import secrets
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from .controller import Conflict, Controller
from .plans import prepare


def create_app(directory, controller=None):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=32768, TRUSTED_HOSTS=['localhost', '127.0.0.1', '[::1]'])
    controller = controller or Controller(directory)
    app.extensions['ktx'] = controller
    csrf = secrets.token_urlsafe(32)
    secret = secrets.token_bytes(32)

    def signature(config, body):
        value = dict(config=config, remember_id=body.get('remember_id', False), remember_password=body.get('remember_password', False))
        return hmac.new(secret, json.dumps(value, sort_keys=True, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict): raise ValueError('입력 형식을 확인하세요.')
        return value

    @app.before_request
    def protect():
        # Force trusted-host validation before serving even a static response.
        host = request.host
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin', '')
            if origin != request.host_url.rstrip('/') or not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf):
                return jsonify(error='페이지를 새로고침한 뒤 다시 시도하세요.'), 403

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"})
        return response

    @app.errorhandler(ValueError)
    def invalid(error): return jsonify(error=str(error)), 409 if isinstance(error, Conflict) else 400

    @app.errorhandler(413)
    def too_large(error): return jsonify(error='입력 데이터가 너무 큽니다.'), 413

    @app.errorhandler(OSError)
    def storage_failed(error): return jsonify(error='저장 공간에 접근하지 못했습니다. Docker 저장 공간을 확인하세요.'), 503

    @app.get('/')
    def index(): return render_template('index.html', csrf=csrf)

    @app.get('/api/state')
    def state(): return jsonify(controller.snapshot())

    @app.get('/api/settings')
    def settings(): return jsonify(controller.settings())

    @app.post('/api/preview')
    def preview():
        value = body()
        config, _ = prepare(value, controller.credentials(value))
        return jsonify(conditions=config['conditions'], station_access=config.get('station_access'), confirmation=signature(config, value))

    @app.post('/api/start')
    def start():
        value = body()
        if value.get('confirmed') is not True: raise ValueError('예약 조건을 먼저 확인하세요.')
        config, preferences = prepare(value, controller.credentials(value))
        confirmation = value.get('confirmation')
        if not isinstance(confirmation, str) or not hmac.compare_digest(confirmation, signature(config, value)):
            raise ValueError('조건이 변경되었습니다. 예약 조건을 다시 확인하세요.')
        controller.start(config, preferences, value)
        return jsonify(controller.snapshot()), 202

    @app.post('/api/stop')
    def stop():
        controller.stop()
        return jsonify(controller.snapshot())

    @app.post('/api/acknowledge')
    def acknowledge():
        if body().get('confirmed') is not True: raise ValueError('코레일톡 예약내역을 먼저 확인하세요.')
        controller.acknowledge()
        return jsonify(controller.snapshot())

    @app.post('/api/credentials/clear')
    def clear_credentials():
        with controller.lock: controller.storage.write('credentials', {})
        return jsonify(ok=True)

    @app.get('/notice')
    def notice():
        path = Path(__file__).resolve().parent/'vendor/LICENSE.pykorail.txt'
        text = 'KTX 자리찾기 — Docker 웹 개인용\n웹 화면과 검색 실행 흐름은 이 프로젝트에서 구현했습니다.\n통신 라이브러리: devgyurak/pykorail 0.5.1\n개인용·비상업적 이용에 한하며 요청 간격은 최소 3초입니다.\n\n'
        return app.response_class(text + path.read_text(encoding='utf-8'), mimetype='text/plain')

    @app.get('/health')
    def health(): return jsonify(ok=True)
    return app
