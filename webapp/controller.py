"""A single reservation worker, independent of browser connections."""
import copy
import json
import threading
import time
import engine
from . import desktop_transport
from .storage import Storage

TERMINAL = {'success', 'stopped', 'error', 'uncertain'}


class Conflict(ValueError):
    pass


class Controller:
    def __init__(self, directory, runner=engine.run):
        self.storage = Storage(directory)
        self.runner = runner
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.closing = False
        self.state = self.storage.read('state', dict(running=False, kind='idle', message='예약 조건을 확인한 후 검색을 시작합니다.',
            attempts=0, updated=0, payment_deadline=0, payment_pending=False, attention_pending=False, history=[]))
        if self.state.get('running'):
            uncertain = self.state.get('kind') in ('reserving', 'stopping')
            self.state.update(running=False, kind='uncertain' if uncertain else 'stopped', attention_pending=uncertain,
                message='이전 검색이 중단되었습니다. 처리 중이던 예약이 있다면 코레일톡 예약내역을 먼저 확인하세요.')
            self._save()

    def _save(self):
        self.state['updated'] = int(time.time() * 1000)
        self.storage.write('state', self.state)

    def snapshot(self):
        with self.lock: return copy.deepcopy(self.state)

    def credentials(self, body):
        username = body.get('username', '')
        password = body.get('password', '')
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 120 or len(password) > 256:
            raise ValueError('로그인 정보를 확인하세요.')
        username = username.strip()
        if not password and body.get('use_saved_password') is True:
            saved = self.storage.credentials()
            if username != saved['username']: raise ValueError('저장된 비밀번호는 같은 아이디에서만 사용할 수 있습니다.')
            password = saved['password']
        if not username or not password: raise ValueError('코레일 아이디와 비밀번호를 입력하세요.')
        for key in ('remember_id', 'remember_password'):
            if not isinstance(body.get(key, False), bool): raise ValueError('로그인 정보 저장 옵션을 확인하세요.')
        return dict(username=username, password=password)

    def settings(self):
        with self.lock:
            preferences = self.storage.read('settings', {})
            try:
                saved = self.storage.credentials()
                preferences.update(username=saved['username'], has_password=bool(saved['password']), credential_error='')
            except ValueError as error:
                preferences.update(username='', has_password=False, credential_error=str(error))
            return preferences

    def start(self, config, preferences, body):
        with self.lock:
            if self.closing: raise Conflict('서버 종료 중입니다.')
            if self.state.get('running'): raise Conflict('이미 검색 중입니다.')
            if self.state.get('payment_pending') or self.state.get('attention_pending'):
                raise Conflict('이전 예약내역을 확인한 뒤 확인 완료를 누르세요.')
            self.storage.save_credentials(config['username'], config['password'], body.get('remember_id', False), body.get('remember_password', False))
            self.storage.write('settings', preferences)
            self.stop_event.clear()
            self.state.update(running=True, kind='login', message='코레일 계정 인증 대기', attempts=0,
                payment_deadline=0, payment_pending=False, attention_pending=False, history=[], conditions=config['conditions'])
            try:
                self._save()
            except OSError:
                self.state.update(running=False, kind='error', message='실행 상태를 저장하지 못해 검색을 시작하지 않았습니다.')
                raise
            self.thread = threading.Thread(target=self._run, args=(config,), daemon=True, name='ktx-search')
            self.thread.start()

    def _run(self, config):
        controller = self
        class Listener:
            def isStopped(self): return controller.stop_event.is_set()
            def onPaymentDeadline(self, value):
                with controller.lock:
                    # A reservation already exists, even if saving its deadline fails.
                    controller.state['payment_pending'] = True
                    controller.state['payment_deadline'] = int(value)
                    controller._save()
            def onStatus(self, kind, message, attempts):
                with controller.lock:
                    if kind == 'error' and controller.state.get('kind') == 'reserving':
                        controller.state['attention_pending'] = True
                    controller.state.update(kind=str(kind), message=str(message), attempts=int(attempts))
                    if kind == 'success': controller.state['payment_pending'] = True
                    if kind == 'uncertain': controller.state['attention_pending'] = True
                    history = controller.state.setdefault('history', [])
                    if not history or history[-1]['message'] != str(message):
                        history.append(dict(kind=str(kind), message=str(message), at=int(time.time()*1000)))
                        del history[:-12]
                    controller._save()
        try:
            self.runner(json.dumps(config), str(self.storage.directory), Listener(), desktop_transport)
        except Exception:
            with self.lock:
                uncertain = self.state.get('kind') == 'reserving'
                self.state.update(kind='uncertain' if uncertain else 'error', attention_pending=uncertain,
                    message='실행 오류로 중지했습니다. 처리 중이던 예약이 있다면 코레일톡 예약내역을 먼저 확인하세요.')
        finally:
            config['username'] = config['password'] = ''
            with self.lock:
                self.state['running'] = False
                if self.state['kind'] not in TERMINAL:
                    uncertain = self.state['kind'] == 'reserving'
                    self.state.update(kind='uncertain' if uncertain else 'stopped', attention_pending=uncertain,
                        message='검색을 종료했습니다. 예약 요청 중이었다면 코레일톡 예약내역을 확인하세요.')
                try: self._save()
                except OSError:
                    self.state.update(kind='error', message='실행 결과를 저장하지 못했습니다. Docker 저장 공간과 코레일톡 예약내역을 확인하세요.')

    def stop(self):
        with self.lock:
            if self.state.get('running'):
                self.stop_event.set()
                # Keep an in-flight booking distinguishable if the process is interrupted.
                if self.state['kind'] != 'reserving': self.state['kind'] = 'stopping'
                self.state['message'] = '중지 요청 처리 중. 전송한 예약 요청은 결과 확인까지 기다립니다.'
                self._save()

    def acknowledge(self):
        with self.lock:
            if self.state.get('running'): raise Conflict('검색 종료 후 예약내역을 확인하세요.')
            self.state.update(payment_pending=False, attention_pending=False)
            self._save()

    def close(self):
        self.closing = True
        self.stop()
        thread = self.thread
        if thread and thread.is_alive(): thread.join(timeout=35)
        if thread and thread.is_alive():
            with self.lock:
                self.state.update(running=False, kind='uncertain', attention_pending=True,
                    message='종료 전에 예약 결과를 확인하지 못했습니다. 코레일톡 예약내역을 먼저 확인하세요.')
                self._save()
