import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]/'app/src/main/python'))
sys.path.insert(0, str(Path(__file__).parents[1]))
import android_transport
import engine
from webapp.app import create_app
from webapp.controller import Controller
from webapp import desktop_transport
from webapp.plans import prepare
from webapp.storage import Storage


def condition():
    return dict(departure='서울', arrival='부산', date='20991101', start='0900', end='1800', passengers=1, option='GENERAL_ONLY')


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ktx-web-test-')
        self.assertEqual(Path(self.temp.name).resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.entered = threading.Event()
        self.release = threading.Event()
        def runner(payload, directory, listener, transport):
            listener.onStatus('searching', '시험 조회 중', 1)
            self.entered.set()
            while not self.release.wait(.01) and not listener.isStopped(): pass
            listener.onStatus('stopped', '시험 검색 종료', 1)
        self.controller = Controller(self.temp.name, runner)
        self.app = create_app(self.temp.name, self.controller)
        self.client = self.app.test_client()
        self.origin = 'http://localhost:8765'
        page = self.client.get('/', base_url=self.origin).get_data(as_text=True)
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page)[1]
        self.value = dict(username='test-user', password='unique-private-password-592', conditions=[condition()], remember_id=False, remember_password=False)

    def tearDown(self):
        self.release.set()
        self.controller.close()
        self.temp.cleanup()

    def post(self, path, value, **headers):
        return self.client.post(path, json=value, base_url=self.origin,
            headers={'Origin':self.origin, 'X-CSRF-Token':self.csrf, **headers})

    def reviewed(self):
        result = self.post('/api/preview', self.value)
        self.assertEqual(result.status_code, 200, result.get_json())
        return {**self.value, 'confirmed':True, 'confirmation':result.get_json()['confirmation']}

    def test_worker_continues_without_browser_and_duplicate_start_is_rejected(self):
        value = self.reviewed()
        self.assertEqual(self.post('/api/start', value).status_code, 202)
        self.assertTrue(self.entered.wait(1))
        # A new HTTP client observes the same worker; no tab owns its lifetime.
        second = self.app.test_client()
        self.assertTrue(second.get('/api/state', base_url=self.origin).get_json()['running'])
        self.assertEqual(self.post('/api/start', value).status_code, 409)
        self.post('/api/stop', {})
        self.controller.thread.join(2)
        self.assertFalse(self.controller.snapshot()['running'])

    def test_review_is_required_and_cannot_be_reused_for_changed_conditions(self):
        self.assertEqual(self.post('/api/start', self.value).status_code, 400)
        value = self.reviewed()
        value['conditions'] = [{**condition(), 'arrival':'대전'}]
        self.assertEqual(self.post('/api/start', value).status_code, 400)
        self.assertFalse(self.entered.is_set())

    def test_host_origin_and_csrf_are_checked(self):
        self.assertEqual(self.client.get('/api/settings', base_url='http://evil.example').status_code, 400)
        self.assertEqual(self.post('/api/preview', self.value, Origin='https://evil.example').status_code, 403)
        self.assertEqual(self.post('/api/preview', self.value, **{'X-CSRF-Token':'wrong'}).status_code, 403)
        self.assertEqual(self.client.post('/api/stop', json={}, base_url=self.origin).status_code, 403)
        response = self.client.get('/api/state', base_url=self.origin)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])

    def test_credentials_are_encrypted_and_never_returned_by_status_or_settings(self):
        self.value.update(remember_id=True, remember_password=True)
        self.assertEqual(self.post('/api/start', self.reviewed()).status_code, 202)
        settings = self.client.get('/api/settings', base_url=self.origin).get_json()
        self.assertTrue(settings['has_password'])
        self.assertEqual(settings['username'], self.value['username'])
        for name in ('settings', 'state'):
            self.assertNotIn(self.value['password'], self.client.get('/api/'+name, base_url=self.origin).get_data(as_text=True))
        for path in Path(self.temp.name).glob('*.json'):
            self.assertNotIn(self.value['password'], path.read_text(encoding='utf-8'))
        saved = self.controller.storage.credentials()
        self.assertEqual(saved['password'], self.value['password'])
        self.post('/api/credentials/clear', {})
        self.assertEqual(self.controller.storage.credentials()['password'], '')

    def test_saved_password_is_bound_to_the_saved_username(self):
        self.controller.storage.save_credentials('saved-user', 'private-password', True, True)
        value = {**self.value, 'password':'', 'username':'other-user', 'use_saved_password':True}
        self.assertEqual(self.post('/api/preview', value).status_code, 400)
        value['username'] = 'saved-user'
        self.assertEqual(self.post('/api/preview', value).status_code, 200)

    def test_payment_and_unknown_reservation_block_start_until_explicit_acknowledgement(self):
        value = self.reviewed()
        for key in ('payment_pending', 'attention_pending'):
            with self.subTest(key=key):
                self.controller.state[key] = True
                self.assertEqual(self.post('/api/start', value).status_code, 409)
                self.assertEqual(self.post('/api/acknowledge', {'confirmed':False}).status_code, 400)
                self.assertEqual(self.post('/api/acknowledge', {'confirmed':True}).status_code, 200)
                self.assertFalse(self.controller.state[key])

    def test_restart_does_not_resume_a_search_or_forget_an_uncertain_booking(self):
        self.controller.state.update(running=True, kind='reserving', attempts=5)
        self.controller._save()
        runner = Mock()
        restored = Controller(self.temp.name, runner)
        self.assertFalse(restored.snapshot()['running'])
        self.assertEqual(restored.snapshot()['kind'], 'uncertain')
        self.assertTrue(restored.snapshot()['attention_pending'])
        runner.assert_not_called()
        restored.close()
        self.controller.state['running'] = False

    def test_shared_engine_runs_once_on_desktop_and_persists_payment_result(self):
        self.controller.runner = engine.run
        client = Mock()
        client.trains.search.return_value = [SimpleNamespace(dep_date='20991101',dep_time='100000',has_general_seat=lambda:True)]
        client.reservations.create.return_value = 'TEST RESERVATION'
        with patch('webapp.desktop_transport.install'), patch('engine.device_profile',return_value=None), patch('pykorail.Korail',return_value=client):
            self.post('/api/start', self.reviewed())
            self.controller.thread.join(2)
        client.reservations.create.assert_called_once()
        self.assertEqual(self.controller.snapshot()['kind'], 'success')
        self.assertTrue(self.controller.snapshot()['payment_pending'])
        restored = Controller(self.temp.name)
        self.assertTrue(restored.snapshot()['payment_pending'])
        restored.close()

    def test_shared_engine_does_not_retry_an_unknown_desktop_booking(self):
        self.controller.runner = engine.run
        client = Mock()
        client.trains.search.return_value = [SimpleNamespace(dep_date='20991101',dep_time='100000',has_general_seat=lambda:True)]
        client.reservations.create.side_effect = OSError('private network failure')
        with patch('webapp.desktop_transport.install'), patch('engine.device_profile',return_value=None), patch('pykorail.Korail',return_value=client):
            self.post('/api/start', self.reviewed())
            self.controller.thread.join(2)
        client.reservations.create.assert_called_once()
        self.assertEqual(self.controller.snapshot()['kind'], 'uncertain')
        self.assertTrue(self.controller.snapshot()['attention_pending'])

    def test_invalid_types_and_oversized_requests_fail_without_starting(self):
        for changes in ({'password':42}, {'conditions':[]}, {'conditions':[None]}, {'remember_password':'true'},
                        {'conditions':[{**condition(),'passengers':True}]}, {'station_access':{'stations':[]}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.post('/api/preview',{**self.value,**changes}).status_code,400)
        result = self.post('/api/preview', {**self.value, 'unused':'x'*40000})
        self.assertEqual(result.status_code, 413)
        self.assertFalse(self.entered.is_set())

    def test_failed_initial_state_save_does_not_start_or_leave_a_phantom_worker(self):
        value = self.reviewed()
        with patch.object(self.controller, '_save', side_effect=OSError('disk full')):
            self.assertEqual(self.post('/api/start', value).status_code, 503)
        self.assertFalse(self.controller.snapshot()['running'])
        self.assertIsNone(self.controller.thread)
        self.assertFalse(self.entered.is_set())

    def test_deadline_save_failure_after_booking_still_blocks_another_search(self):
        self.controller.runner = engine.run
        client = Mock()
        client.trains.search.return_value = [SimpleNamespace(dep_date='20991101',dep_time='100000',has_general_seat=lambda:True)]
        client.reservations.create.return_value = 'TEST RESERVATION'
        save = self.controller._save
        def fail_deadline_save():
            if self.controller.state.get('payment_pending'): raise OSError('disk full')
            save()
        with patch('webapp.desktop_transport.install'), patch('engine.device_profile',return_value=None), patch('pykorail.Korail',return_value=client), \
             patch.object(self.controller, '_save', side_effect=fail_deadline_save):
            self.post('/api/start', self.reviewed())
            self.controller.thread.join(2)
        client.reservations.create.assert_called_once()
        self.assertTrue(self.controller.snapshot()['payment_pending'])
        self.assertEqual(self.post('/api/start', self.reviewed()).status_code, 409)
        restored = Controller(self.temp.name)
        self.assertTrue(restored.snapshot()['attention_pending'])
        restored.close()


class SelectionTests(unittest.TestCase):
    def test_manual_conditions_come_first_and_travel_candidates_share_the_three_condition_limit(self):
        access = dict(baseline='서울', stations=[dict(name='서울',minutes=20),dict(name='용산',minutes=10),dict(name='수서',minutes=50)],extra_minutes=30,boarding_minutes=15)
        body = dict(conditions=[condition(),{**condition(),'arrival':'대전'}], station_access=access)
        selected, preferences = prepare(body, dict(username='test',password='test'))
        self.assertEqual([(v['departure'],v['arrival']) for v in selected['conditions']], [('서울','부산'),('서울','대전'),('용산','부산')])
        self.assertEqual(len(preferences['conditions']), 2)
        self.assertNotIn('password', preferences)
        self.assertTrue(selected['confirmed'])

    def test_over_budget_manual_station_and_wrong_baseline_are_rejected(self):
        access = dict(baseline='서울',stations=[dict(name='서울',minutes=20),dict(name='수서',minutes=80)],extra_minutes=30,boarding_minutes=15)
        for body in [dict(conditions=[{**condition(),'departure':'수서'}],station_access=access),
                     dict(conditions=[condition()],station_access={**access,'baseline':'수서'})]:
            with self.subTest(body=body), self.assertRaises(ValueError): prepare(body,dict(username='test',password='test'))


class StorageTests(unittest.TestCase):
    def test_modified_ciphertext_or_missing_key_never_falls_back_to_plaintext(self):
        with tempfile.TemporaryDirectory(prefix='ktx-web-crypto-') as directory:
            self.assertEqual(Path(directory).resolve().parent, Path(tempfile.gettempdir()).resolve())
            storage = Storage(directory)
            storage.save_credentials('test', 'private', True, True)
            value = storage.read('credentials', {})
            value['tag'] = 'AAAAAAAAAAAAAAAAAAAAAA=='
            storage.write('credentials', value)
            with self.assertRaises(ValueError): storage.credentials()
            storage.save_credentials('test', 'private', True, True)
            Path(directory,'credentials.key').unlink()
            with self.assertRaises(ValueError): storage.credentials()
            self.assertFalse(Path(directory,'credentials.key').exists())


class DesktopTransportTests(unittest.TestCase):
    def session(self):
        native = Mock()
        native.headers = {}
        with patch.object(desktop_transport, '_original_factory', return_value=native):
            return desktop_transport.DesktopSession(), native

    def test_same_global_request_gate_is_used_across_desktop_sessions(self):
        first, native1 = self.session()
        second, native2 = self.session()
        clock = [100.0]
        calls = []
        def request(*args, **kwargs):
            calls.append(clock[0]); clock[0]+=7
            return SimpleNamespace(status_code=200,text='{}')
        native1.get.side_effect = native2.post.side_effect = request
        with patch.object(android_transport, '_last_finished',0), patch.object(android_transport.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(android_transport.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)), patch.object(android_transport.random,'uniform',return_value=3):
            first.get('https://example.invalid/login')
            second.post('https://example.invalid/search')
        self.assertEqual(calls,[100,110])
        self.assertEqual(native1.get.call_args.kwargs['timeout'],20)

    def test_reservation_is_checked_after_pacing_and_private_errors_are_not_exposed(self):
        from pykorail.constants import API_ENDPOINTS
        from pykorail.exceptions import TransportError
        session, native = self.session()
        wall=[100.0]
        with patch.object(android_transport,'_last_finished',100), patch.object(android_transport.time,'monotonic',return_value=100), \
             patch.object(android_transport.time,'time',side_effect=lambda:wall[0]), patch.object(android_transport.random,'uniform',return_value=3), \
             patch.object(android_transport.time,'sleep',side_effect=lambda seconds:wall.__setitem__(0,wall[0]+seconds)), \
             desktop_transport.reservation_window(102,lambda:False), self.assertRaises(desktop_transport.ReservationWindowClosed):
            session.post(API_ENDPOINTS['reserve'])
        native.post.assert_not_called()
        native.get.side_effect = OSError('private request contents')
        with patch.object(android_transport.time,'sleep'), self.assertRaises(TransportError) as raised: session.get('https://example.invalid')
        self.assertNotIn('private',str(raised.exception))


if __name__ == '__main__': unittest.main()
