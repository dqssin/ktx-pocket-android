package kr.personal.ktxpocket;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.os.*;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

public final class SearchService extends Service {
    public static volatile boolean running = false;
    public static final String STOP = "kr.personal.ktxpocket.STOP";
    static final String PREFS = "search_state";
    private static final int ONGOING_ID = 100;
    private volatile boolean stopRequested;
    private volatile boolean timedOut;
    private Thread worker;
    private PowerManager.WakeLock wakeLock;
    private NotificationManager notifications;
    private volatile long paymentDeadline;
    private final Handler heartbeatHandler = new Handler(Looper.getMainLooper());
    private final Runnable heartbeat = new Runnable() {
        @Override public void run() {
            if (running) { SearchAlerts.heartbeat(SearchService.this); heartbeatHandler.postDelayed(this, 30_000); }
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        notifications = getSystemService(NotificationManager.class);
        NotificationChannel ongoing = new NotificationChannel("search", "검색 진행", NotificationManager.IMPORTANCE_LOW);
        ongoing.setDescription("휴대폰에서 진행 중인 KTX 검색과 중지 버튼");
        notifications.createNotificationChannel(ongoing);
        SearchAlerts.createChannel(this);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopSelf(); return START_NOT_STICKY; }
        if (STOP.equals(intent.getAction())) {
            stopRequested = true;
            if (worker == null) stopSelf();
            else publish("stopping", "중지 요청을 받았습니다. 진행 중인 요청 결과를 확인한 뒤 멈춥니다.", getSharedPreferences(PREFS, 0).getInt("attempts", 0));
            return START_NOT_STICKY;
        }
        if (worker != null) return START_NOT_STICKY;
        String config = intent.getStringExtra("config");
        intent.removeExtra("config");
        if (config == null) { stopSelf(); return START_NOT_STICKY; }
        running = true;
        Notification first = notification("search", "KTX 자리찾기", "검색을 준비하고 있습니다.", true);
        if (Build.VERSION.SDK_INT >= 29) startForeground(ONGOING_ID, first, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        else startForeground(ONGOING_ID, first);
        publish("login", "검색을 준비하고 있습니다.", 0);
        SearchAlerts.startWatching(this);
        heartbeatHandler.post(heartbeat);
        wakeLock = ((PowerManager)getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "KtxPocket:Search");
        wakeLock.acquire((5 * 3600L + 47 * 60L) * 1000L);
        worker = new Thread(() -> {
            try {
                if (!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
                Python.getInstance().getModule("engine").callAttr("run", config, getFilesDir().getAbsolutePath(), new Listener());
            } catch (Exception error) {
                // Python/Java exception text can include request data; never persist it.
                publish("error", "앱 실행 중 오류가 발생했습니다. 통신·암호화 모듈이 이 휴대폰에서 동작하는지 확인이 필요합니다.", 0);
            } finally {
                SearchAlerts.finishWatching(this);
                heartbeatHandler.removeCallbacks(heartbeat);
                running = false;
                if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
                stopForeground(STOP_FOREGROUND_REMOVE);
                stopSelf();
            }
        }, "KTX-search");
        worker.start();
        return START_NOT_STICKY;
    }

    public final class Listener {
        public boolean isStopped() { return stopRequested; }
        public void onPaymentDeadline(long deadline) { paymentDeadline = deadline; }
        public void onStatus(String kind, String message, int attempts) {
            if (!timedOut) publish(kind, message, attempts);
        }
    }

    private synchronized void publish(String kind, String message, int attempts) {
        getSharedPreferences(PREFS, 0).edit().putString("kind", kind).putString("message", message)
            .putInt("attempts", attempts).putLong("updated", System.currentTimeMillis()).apply();
        boolean active = kind.equals("login") || kind.equals("searching") || kind.equals("reserving") || kind.equals("stopping");
        if (active) notifications.notify(ONGOING_ID, notification("search", "KTX 자리찾기 · " + attempts + "회 조회", message, true));
        else {
            SearchAlerts.finishWatching(this);
            if (kind.equals("success")) SearchAlerts.reserved(this, message, paymentDeadline);
            else if (!kind.equals("stopped") || !stopRequested)
                SearchAlerts.show(this, kind.equals("stopped") ? "KTX 검색을 마쳤습니다" : "KTX 검색 확인이 필요합니다", message, false);
        }
    }

    private Notification notification(String channel, String title, String message, boolean ongoing) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = new Notification.Builder(this, channel).setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title).setContentText(message).setStyle(new Notification.BigTextStyle().bigText(message))
            .setContentIntent(open).setOngoing(ongoing).setAutoCancel(!ongoing).setOnlyAlertOnce(ongoing)
            .setVisibility(Notification.VISIBILITY_PRIVATE);
        if (ongoing) {
            PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, SearchService.class).setAction(STOP), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            builder.addAction(new Notification.Action.Builder(null, "검색 중지", stop).build());
        }
        return builder.build();
    }

    @Override public void onTimeout(int startId, int type) {
        timedOut = true;
        stopRequested = true;
        publish("error", "안드로이드 실행 시간 제한으로 검색을 중지했습니다. 예약 처리 중이었다면 코레일 예약내역을 먼저 확인하세요.", getSharedPreferences(PREFS, 0).getInt("attempts", 0));
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    @Override public void onDestroy() {
        stopRequested = true;
        heartbeatHandler.removeCallbacks(heartbeat);
        // Keep the UI from starting a second reservation while a previous request finishes.
        if (worker == null || !worker.isAlive()) running = false;
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}
