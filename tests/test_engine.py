import json
from pathlib import Path
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory, gettempdir

sys.path.insert(0, str(Path(__file__).parents[1] / "app/src/main/python"))
import engine


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(username="test-user", password="not-a-real-password", departure="서울", arrival="부산",
            date="20991101", start="1630", end="1750", passengers=1, option="GENERAL_ONLY", confirmed=True)
        self.now = datetime(2099, 10, 31, 12, tzinfo=engine.KST)

    def test_confirmation_count_and_range_are_required(self):
        engine.validate(self.config, self.now)
        for change in ({"confirmed": False}, {"passengers": 0}, {"passengers": True}, {"end": "1600"}, {"arrival": "서울"}, {"password": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                engine.validate({**self.config, **change}, self.now)

    def test_end_is_exclusive_and_seat_preference_is_enforced(self):
        start, end = engine.validate(self.config, self.now)
        train = SimpleNamespace(dep_date="20991101", dep_time="175000", has_general_seat=lambda: True)
        self.assertFalse(engine.matches(train, start, end, "GENERAL_ONLY"))
        train.dep_time = "163000"
        self.assertTrue(engine.matches(train, start, end, "GENERAL_ONLY"))
        train.has_general_seat = lambda: False
        self.assertFalse(engine.matches(train, start, end, "GENERAL_ONLY"))

    def test_sold_out_first_page_does_not_hide_later_available_trains(self):
        start, end = engine.validate(self.config, self.now)
        sold = SimpleNamespace(dep_date="20991101", dep_time="164000", has_general_seat=lambda: False)
        available = SimpleNamespace(dep_date="20991101", dep_time="170000", has_general_seat=lambda: True)
        client = Mock()
        client.trains.search.side_effect = [[sold], [available]]
        self.assertEqual(engine.search_window(client, self.config, start, end, [], lambda: False), [available])
        self.assertEqual(client.trains.search.call_count, 2)
        self.assertEqual(client.trains.search.call_args.kwargs["depart_after"].strftime("%H%M%S"), "164001")

    def test_stop_prevents_a_new_query(self):
        start, end = engine.validate(self.config, self.now)
        client = Mock()
        self.assertEqual(engine.search_window(client, self.config, start, end, [], lambda: True), [])
        client.trains.search.assert_not_called()

    def run_fake(self, reserve_error=None):
        client = Mock()
        train = SimpleNamespace(dep_date="20991101", dep_time="170000", has_general_seat=lambda: True)
        client.trains.search.return_value = [train]
        client.reservations.create.return_value = "TEST RESERVATION"
        if reserve_error: client.reservations.create.side_effect = reserve_error
        listener = Mock()
        listener.isStopped.return_value = False
        with patch("android_transport.install"), patch("pykorail.Korail", return_value=client), patch("engine.device_profile", return_value=None):
            engine.run(json.dumps(self.config), "unused-test-directory", listener)
        return client, listener

    def test_success_reserves_once_and_stops(self):
        client, listener = self.run_fake()
        client.reservations.create.assert_called_once()
        self.assertEqual(listener.onStatus.call_args.args[0], "success")
        client.close.assert_called_once()

    def test_uncertain_reservation_is_not_retried(self):
        client, listener = self.run_fake(OSError("simulated dropped connection"))
        client.reservations.create.assert_called_once()
        self.assertEqual(listener.onStatus.call_args.args[0], "uncertain")

    def test_alternative_conditions_are_checked_in_turn_and_stop_after_one_reservation(self):
        first = {key: self.config[key] for key in ("departure", "arrival", "date", "start", "end", "passengers", "option")}
        second = {**first, "arrival": "동대구", "passengers": 2}
        self.config["conditions"] = [first, second]
        train = SimpleNamespace(dep_date="20991101", dep_time="170000", has_general_seat=lambda: True)
        client, listener, _, _ = self.run_search_outcomes([[], [train]])
        self.assertEqual([call.args[:2] for call in client.trains.search.call_args_list], [("서울", "부산"), ("서울", "동대구")])
        client.reservations.create.assert_called_once()
        self.assertEqual(client.reservations.create.call_args.kwargs["passengers"][0].count, 2)
        self.assertEqual(listener.onStatus.call_args.args[0], "success")

    def test_uncertain_reservation_does_not_try_another_alternative(self):
        self.config["conditions"] = [dict(self.config), {**self.config, "arrival": "동대구"}]
        client, listener = self.run_fake(OSError("simulated dropped connection"))
        client.reservations.create.assert_called_once()
        client.trains.search.assert_called_once()
        self.assertEqual(listener.onStatus.call_args.args[0], "uncertain")

    def test_alternative_validation_rejects_invalid_count_or_any_invalid_condition(self):
        for conditions in ([], [dict(self.config)] * 4, [dict(self.config), {**self.config, "passengers": 7}], [None]):
            with self.subTest(conditions=conditions), self.assertRaises(ValueError):
                engine.validated_conditions({**self.config, "conditions": conditions}, self.now)

    def test_expired_alternative_is_skipped_while_another_is_still_valid(self):
        first = {**self.config, "end": "1700"}
        second = {**self.config, "arrival": "동대구", "start": "1800", "end": "1900"}
        self.config["conditions"] = [first, second]
        train = SimpleNamespace(dep_date="20991101", dep_time="180500", has_general_seat=lambda: True)
        initial = datetime(2099, 11, 1, 16, tzinfo=engine.KST)
        later = datetime(2099, 11, 1, 17, 30, tzinfo=engine.KST)
        with patch("engine.datetime", wraps=datetime) as dates:
            dates.now.side_effect = [initial, initial, later, later]
            client, listener, _, _ = self.run_search_outcomes([[train]])
        client.trains.search.assert_called_once()
        self.assertEqual(client.trains.search.call_args.args[:2], ("서울", "동대구"))
        self.assertEqual(listener.onStatus.call_args.args[0], "success")

    def test_payment_deadline_is_korean_time_and_unknown_deadline_is_not_invented(self):
        expected = int(datetime(2099, 11, 1, 17, 30, tzinfo=engine.KST).timestamp() * 1000)
        self.assertEqual(engine.payment_deadline(SimpleNamespace(buy_limit_date="20991101", buy_limit_time="173000")), expected)
        self.assertEqual(engine.payment_deadline(SimpleNamespace(buy_limit_date="20991101", buy_limit_time="1730")), expected)
        for day, clock in (("00000000", "235959"), ("20991131", "173000"), ("", ""), ("20991101", "999999")):
            with self.subTest(day=day, clock=clock):
                self.assertEqual(engine.payment_deadline(SimpleNamespace(buy_limit_date=day, buy_limit_time=clock)), 0)

    def test_access_restriction_stops_without_retry(self):
        from pykorail.exceptions import AccessRestrictedError, HttpStatusError
        for error in (AccessRestrictedError(403, "test restriction", "-8202"), HttpStatusError(403), HttpStatusError(429)):
            with self.subTest(error=error):
                client, listener, moments, clock = self.run_search_outcomes([error] * 5)
                client.trains.search.assert_called_once()
                client.reservations.create.assert_not_called()
                self.assertEqual(listener.onStatus.call_args.args[0], "error")

    def run_search_outcomes(self, outcomes, stop_at=None):
        clock = [100.0]
        moments = []
        client = Mock()

        def search(*args, **kwargs):
            moments.append(clock[0])
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception): raise outcome
            return outcome

        client.trains.search.side_effect = search
        listener = Mock()
        listener.isStopped.side_effect = lambda: stop_at is not None and clock[0] >= stop_at
        with patch("android_transport.install"), patch("pykorail.Korail", return_value=client), patch("engine.device_profile", return_value=None), \
             patch("engine.time.monotonic", side_effect=lambda: clock[0]), \
             patch("engine.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            engine.run(json.dumps(self.config), "unused-test-directory", listener)
        return client, listener, moments, clock[0]

    def test_error_backoff_grows_caps_and_stops_after_five_failures(self):
        from pykorail.exceptions import TransportError, HttpStatusError, KorailError
        for error in (TransportError("test"), HttpStatusError(503), KorailError("test")):
            with self.subTest(error=error):
                client, listener, moments, clock = self.run_search_outcomes([error] * 5)
                self.assertEqual(client.trains.search.call_count, 5)
                for actual, expected in zip(moments, [100, 130, 190, 310, 430]):
                    self.assertAlmostEqual(actual, expected)
                self.assertEqual(listener.onStatus.call_args.args[0], "error")

    def test_successful_query_resets_backoff(self):
        from pykorail.exceptions import TransportError, AccessRestrictedError
        error = TransportError("test")
        _, _, moments, _ = self.run_search_outcomes([error, error, [], error, AccessRestrictedError(403)])
        self.assertEqual(len(moments), 5)
        for actual, expected in zip(moments, [100, 130, 190, 193, 223]):
            self.assertAlmostEqual(actual, expected)

    def test_stop_during_backoff_prevents_retry(self):
        from pykorail.exceptions import TransportError
        client, listener, _, clock = self.run_search_outcomes([TransportError("test")], stop_at=101)
        client.trains.search.assert_called_once()
        self.assertLess(clock, 102)
        self.assertEqual(listener.onStatus.call_args.args[0], "stopped")

    def test_device_identity_is_preserved(self):
        with TemporaryDirectory(prefix="ktx-engine-test-") as directory:
            self.assertEqual(Path(directory).resolve().parent, Path(gettempdir()).resolve())
            first = engine.device_profile(directory)
            second = engine.device_profile(directory)
            self.assertEqual(first.id, second.id)
            self.assertEqual(first.android_id, second.android_id)

    def test_transport_randomizes_gap_without_going_below_three_seconds(self):
        import android_transport as transport
        session = object.__new__(transport.AndroidSession)
        session.headers = {"User-Agent": "test"}
        session.native = Mock()
        session.native.exchange.return_value = json.dumps({"status_code": 200, "text": "{}"})
        for gap in (3.0, 4.2, 5.0):
            with self.subTest(gap=gap), patch.object(transport, "_last_finished", 100), \
                 patch.object(transport.time, "monotonic", side_effect=[101, 103]), \
                 patch("android_transport.random.uniform", return_value=gap) as uniform, \
                 patch.object(transport.time, "sleep") as sleep:
                response = session.get("https://example.invalid/search", params={"station": "서울"})
                uniform.assert_called_once_with(3.0, 5.0)
                sleep.assert_called_once_with(gap - 1.0)
                self.assertEqual(response.status_code, 200)
                self.assertIn("station=%EC%84%9C%EC%9A%B8", session.native.exchange.call_args.args[0])

    def test_transport_gap_is_global_and_starts_after_success_or_failure_finishes(self):
        import android_transport as transport
        from pykorail.exceptions import TransportError
        clock = [100.0]
        moments = []

        def exchange(*args):
            moments.append(clock[0])
            clock[0] += 7.0
            if len(moments) == 2: raise OSError("test network failure")
            return json.dumps({"status_code": 200, "text": "{}"})

        first, second = (object.__new__(transport.AndroidSession) for _ in range(2))
        for session in (first, second):
            session.headers = {}
            session.native = SimpleNamespace(exchange=exchange)
        with patch.object(transport, "_last_finished", 0), \
             patch.object(transport.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(transport.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch("android_transport.random.uniform", return_value=3.0):
            first.get("https://example.invalid/login")
            with self.assertRaises(TransportError): second.post("https://example.invalid/search")
            first.get("https://example.invalid/search")
        self.assertEqual(moments, [100, 110, 120])

    def test_native_network_failure_is_translated_without_request_details(self):
        import android_transport as transport
        from pykorail.exceptions import TransportError
        session = object.__new__(transport.AndroidSession)
        session.headers = {}
        session.native = Mock()
        session.native.exchange.side_effect = OSError("example private request data")
        with patch.object(transport.time, "sleep"), self.assertRaises(TransportError) as result:
            session.get("https://example.invalid")
        self.assertNotIn("private", str(result.exception))


if __name__ == "__main__": unittest.main()
