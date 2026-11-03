"""One personal KTX search on the device. No Telegram, Redis, or external app server."""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

KST = timezone(timedelta(hours=9))
MAX_SECONDS = 5 * 3600 + 45 * 60
OPTIONS = {"GENERAL_ONLY", "GENERAL_FIRST", "SPECIAL_ONLY", "SPECIAL_FIRST"}


def validate(config, now=None):
    for key in ("username", "password", "departure", "arrival", "date", "start", "end", "option"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError("로그인 정보와 예약 조건을 모두 입력하세요.")
    if config["departure"].strip() == config["arrival"].strip():
        raise ValueError("출발역과 도착역을 다르게 선택하세요.")
    if config["option"] not in OPTIONS:
        raise ValueError("좌석 선택이 올바르지 않습니다.")
    count = config.get("passengers")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 6:
        raise ValueError("어른 인원은 1~6명으로 선택하세요.")
    try:
        start = datetime.strptime(config["date"] + config["start"], "%Y%m%d%H%M").replace(tzinfo=KST)
        end = datetime.strptime(config["date"] + config["end"], "%Y%m%d%H%M").replace(tzinfo=KST)
    except ValueError as error:
        raise ValueError("날짜와 시간을 확인하세요.") from error
    now = now or datetime.now(KST)
    if end <= start or end <= now:
        raise ValueError("종료 시간은 시작 시간과 현재 시간보다 뒤여야 합니다.")
    if config.get("confirmed") is not True:
        raise ValueError("개인용 이용과 예약 조건을 확인하세요.")
    return start, end


def validated_conditions(config, now=None):
    access = validated_access(config.get("station_access"))
    entries = config.get("conditions")
    if entries is None:
        entries = [config]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 3:
        raise ValueError("검색 조건은 1~3개로 등록하세요.")
    plans = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("검색 조건을 다시 확인하세요.")
        condition = {key: entry.get(key) for key in ("departure", "arrival", "date", "start", "end", "passengers", "option")}
        condition.update(username=config.get("username"), password=config.get("password"), confirmed=config.get("confirmed"))
        if access:
            stations, limit, boarding = access
            minutes = stations.get(condition["departure"].strip()) if isinstance(condition["departure"], str) else None
            if minutes is None or minutes > limit:
                raise ValueError("등록한 역과 허용 이동 시간 안의 조건만 검색할 수 있습니다.")
            condition.update(access_minutes=minutes, boarding_minutes=boarding)
        start, end = validate(condition, now)
        plans.append((condition, start, end))
    return plans


def validated_access(value):
    if value is None: return None
    if not isinstance(value, dict): raise ValueError("역 이동 시간 설정을 확인하세요.")
    def minutes(number, maximum):
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= maximum:
            raise ValueError("이동 시간은 0~360분, 승차 여유는 0~120분으로 입력하세요.")
        return number
    entries = value.get("stations")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 3:
        raise ValueError("이동 시간을 등록할 역은 1~3개로 입력하세요.")
    stations = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise ValueError("이동 시간을 등록할 역 이름을 확인하세요.")
        name = entry["name"].strip()
        if name in stations: raise ValueError("같은 역은 한 번만 등록하세요.")
        stations[name] = minutes(entry.get("minutes"), 360)
    baseline = value.get("baseline")
    if not isinstance(baseline, str) or baseline not in stations: raise ValueError("기준 출발역의 이동 시간을 등록하세요.")
    return stations, stations[baseline] + minutes(value.get("extra_minutes"), 360), minutes(value.get("boarding_minutes"), 120)


def earliest_departure(condition, start, now=None):
    lead = condition.get("access_minutes", 0) + condition.get("boarding_minutes", 0)
    return max(start, (now or datetime.now(KST)) + timedelta(minutes=lead, seconds=5))


def payment_deadline(reservation):
    day = getattr(reservation, "buy_limit_date", "")
    clock = getattr(reservation, "buy_limit_time", "")
    if not isinstance(day, str) or not isinstance(clock, str): return 0
    if len(day) != 8 or len(clock) not in (4, 6) or not (day + clock).isdigit(): return 0
    try:
        deadline = datetime.strptime(day + clock.ljust(6, "0"), "%Y%m%d%H%M%S").replace(tzinfo=KST)
        return int(deadline.timestamp() * 1000)
    except (ValueError, OverflowError):
        return 0


def matches(train, start, end, option):
    departure = departure_of(train)
    if not start <= departure < end:
        return False
    if option == "GENERAL_ONLY":
        return train.has_general_seat()
    if option == "SPECIAL_ONLY":
        return train.has_special_seat()
    return train.has_seat()


def departure_of(train):
    return datetime.strptime(train.dep_date + train.dep_time.ljust(6, "0")[:6], "%Y%m%d%H%M%S").replace(tzinfo=KST)


def search_window(client, config, start, end, passengers, stopped):
    from pykorail import TrainType
    from pykorail.exceptions import NoResultsError
    cursor = start
    # Inspect later pages too: the first page may contain only sold-out trains.
    for _ in range(20):
        if stopped(): return []
        try:
            trains = client.trains.search(
                config["departure"].strip(), config["arrival"].strip(), depart_after=cursor,
                train_type=TrainType.KTX, passengers=passengers, include_no_seats=True,
                include_waiting_list=False, max_pages=1,
            )
        except NoResultsError:
            return []
        candidates = [train for train in trains if matches(train, start, end, config["option"])]
        if candidates: return candidates
        if not trains: return []
        last = max(departure_of(train) for train in trains)
        if last >= end or last < cursor: return []
        cursor = last + timedelta(seconds=1)
    return []


def device_profile(directory):
    from pykorail.device import profile_by_id, random_profile
    path = Path(directory) / "korail_device.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        profile = profile_by_id(saved["profile_id"], android_id=saved["android_id"])
        if profile is None:
            raise ValueError("저장된 기기 정보를 읽지 못해 중지했습니다. 앱 데이터 초기화로 기기 식별자가 변경될 수 있습니다.")
        return profile
    profile = random_profile()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"profile_id": profile.id, "android_id": profile.android_id}), encoding="utf-8")
    temporary.replace(path)
    return profile


def run(config_json, directory, listener):
    config = json.loads(config_json)
    attempts = 0
    client = None
    plans = []
    deadline = time.monotonic() + MAX_SECONDS

    def status(kind, message):
        listener.onStatus(kind, message, attempts)

    def stopped():
        return listener.isStopped() or time.monotonic() >= deadline

    def pause(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until and not stopped():
            time.sleep(min(0.2, max(0, until - time.monotonic())))

    try:
        plans = validated_conditions(config)
        from android_transport import install, reservation_window, ReservationWindowClosed
        install()
        from pykorail import Korail, TrainType, AdultPassenger
        from pykorail.exceptions import (
            AccessRestrictedError, HttpStatusError, LoginFailedError, NeedToLoginError,
            NoResultsError, PastDepartureError, PasswordChangeRequiredError, SoldOutError,
            StationNotFoundError, TransportError, KorailError,
        )
        logging.getLogger("pykorail").setLevel(logging.CRITICAL)
        status("login", "코레일 계정 인증 중")
        client = Korail(device_profile=device_profile(directory))
        client.login(config["username"].strip(), config["password"])
        # A server notice may leave an apparently successful login unusable.
        client.reservations.all()
        failures = 0
        turn = 0
        while not stopped():
            now = datetime.now(KST)
            available = [plan for plan in plans if earliest_departure(plan[0], plan[1], now) < plan[2]]
            if not available:
                status("stopped", "이동 시간과 승차 여유를 확보할 수 있는 출발 범위가 없어 검색을 종료했습니다.")
                return
            plan = available[turn % len(available)]
            condition, start, end = plan
            turn += 1
            passengers = [AdultPassenger(condition["passengers"])]
            attempts += 1
            status("searching", f"조건 {plans.index(plan) + 1}/{len(plans)} · {condition['departure']} → {condition['arrival']} 조회 중")
            try:
                trains = search_window(client, condition, earliest_departure(condition, start, now), end, passengers, stopped)
                failures = 0
            except NoResultsError:
                pause(3)
                continue
            except NeedToLoginError:
                status("searching", "로그인이 만료되어 60초 후 다시 연결합니다.")
                pause(60)
                if stopped(): break
                client.login(config["username"].strip(), config["password"])
                client.reservations.all()
                continue
            except (TransportError, HttpStatusError) as error:
                if isinstance(error, AccessRestrictedError) or getattr(error, "status_code", None) in (403, 429):
                    raise
                failures += 1
                if failures >= 5:
                    status("error", "통신 오류가 5회 이어져 검색을 중지했습니다. 네트워크와 코레일 상태를 확인하세요.")
                    return
                delay = min(120, 30 * 2 ** (failures - 1))
                status("searching", f"네트워크 응답을 받지 못했습니다. {delay}초 후 다시 조회합니다.")
                pause(delay)
                continue
            except StationNotFoundError:
                raise
            except KorailError:
                failures += 1
                if failures >= 5:
                    status("error", "코레일이 조회를 계속 거절해 중지했습니다. 공식 앱에서 상태를 확인하세요.")
                    return
                delay = min(120, 30 * 2 ** (failures - 1))
                status("searching", f"코레일 조회가 잠시 지연됩니다. {delay}초 후 다시 확인합니다.")
                pause(delay)
                continue
            for train in trains:
                if stopped(): break
                if not matches(train, earliest_departure(condition, start), end, condition["option"]): continue
                status("reserving", "예약 요청 전송 중. 결과 확인 전에는 다시 시작하지 마세요.")
                try:
                    latest_start = departure_of(train) - timedelta(minutes=condition.get("access_minutes", 0) + condition.get("boarding_minutes", 0))
                    with reservation_window(latest_start.timestamp(), stopped):
                        reservation = client.reservations.create(train, passengers=passengers, option=condition["option"])
                except ReservationWindowClosed:
                    continue
                except SoldOutError:
                    continue
                except Exception:
                    # A timed-out reservation may already exist on the server: never blindly retry it.
                    status("uncertain", "예약 결과를 확정하지 못해 자동 시도를 멈췄습니다. 코레일 앱의 ‘예약내역’을 먼저 확인하세요.")
                    return
                listener.onPaymentDeadline(payment_deadline(reservation))
                travel_note = ""
                if "access_minutes" in condition:
                    travel_note = f"\n\n출발역 {condition['departure']} · 입력한 이동 {condition['access_minutes']}분 + 승차 여유 {condition['boarding_minutes']}분 기준, {latest_start:%m월 %d일 %H:%M}까지 이동 시작이 필요합니다. 실제 교통 상황을 확인하세요."
                status("success", str(reservation) + travel_note + "\n\n예약 1건 확보 · 검색 종료. 코레일톡 예약내역에서 결제 기한을 확인하고 결제하세요.")
                return
            pause(3)
        status("stopped", "검색을 중지했습니다." if listener.isStopped() else "5시간 45분 실행 한도에 도달했습니다. 앱에서 다시 시작할 수 있습니다.")
    except Exception as error:
        from pykorail.exceptions import AccessRestrictedError, NeedToLoginError, LoginFailedError, PasswordChangeRequiredError, StationNotFoundError, HttpStatusError
        if isinstance(error, AccessRestrictedError) or (isinstance(error, HttpStatusError) and error.status_code in (403, 429)):
            message = "코레일에서 접속을 제한해 검색을 중지했습니다. 같은 연결로 자동 재시도하지 않습니다."
        elif isinstance(error, (LoginFailedError, PasswordChangeRequiredError, NeedToLoginError)):
            message = "로그인을 완료하지 못했습니다. 코레일 앱에서 비밀번호와 계정 안내를 먼저 확인하세요."
        elif isinstance(error, StationNotFoundError):
            message = "역 이름을 찾지 못했습니다. 코레일에 등록된 정확한 역 이름을 입력하세요."
        elif isinstance(error, ValueError):
            message = str(error)
        else:
            message = "실행 오류로 검색을 중지했습니다 (" + type(error).__name__ + ")."
        status("error", message)
    finally:
        config["password"] = ""
        config["username"] = ""
        for condition, _, _ in plans:
            condition["password"] = ""
            condition["username"] = ""
        if client is not None:
            try: client.close()
            except Exception: pass
