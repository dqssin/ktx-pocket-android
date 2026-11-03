package kr.personal.ktxpocket;

import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.SystemClock;

/** Local reminders contain reservation results, never login credentials. */
public final class SearchAlerts extends BroadcastReceiver {
    private static final String WATCH = "kr.personal.ktxpocket.WATCH";
    private static final String PAYMENT = "kr.personal.ktxpocket.PAYMENT";
    private static final String DISMISS = "kr.personal.ktxpocket.DISMISS_PAYMENT";
    private static final int WATCH_ID = 200, PAYMENT_ID = 201;

    private static SharedPreferences prefs(Context context) { return context.getSharedPreferences("search_alerts", 0); }

    static void createChannel(Context context) {
        NotificationChannel channel = new NotificationChannel("result", "예약 결과", NotificationManager.IMPORTANCE_HIGH);
        channel.setDescription("예약 성공, 검색 중단, 결제 재알림");
        channel.enableVibration(true);
        channel.setVibrationPattern(new long[]{0, 500, 200, 500, 200, 800});
        context.getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }

    static boolean paymentPending(Context context) { return prefs(context).getBoolean("payment_pending", false); }

    static void heartbeat(Context context) {
        prefs(context).edit().putLong("heartbeat", SystemClock.elapsedRealtime()).apply();
    }

    static void startWatching(Context context) {
        prefs(context).edit().putBoolean("watching", true).putLong("heartbeat", SystemClock.elapsedRealtime()).apply();
        schedule(context, WATCH, WATCH_ID, System.currentTimeMillis() + 3 * 60_000, false);
    }

    static void finishWatching(Context context) {
        prefs(context).edit().putBoolean("watching", false).apply();
        cancel(context, WATCH, WATCH_ID);
    }

    static void reserved(Context context, String message, long deadline) {
        dismissPayment(context);
        prefs(context).edit().putBoolean("payment_pending", true).putString("reservation", message).putLong("deadline", deadline)
            .putLong("reserved_at", System.currentTimeMillis()).apply();
        show(context, "KTX 예약 성공 · 결제가 필요합니다", message, true);
        schedulePayment(context);
    }

    private static void schedulePayment(Context context) {
        long now = System.currentTimeMillis();
        long[] times = ReminderSchedule.paymentTimes(prefs(context).getLong("reserved_at", now), prefs(context).getLong("deadline", 0));
        try {
            for (int i = 0; i < 2; i++) cancel(context, PAYMENT, PAYMENT_ID + i);
            for (int i = 0; i < times.length; i++) if (times[i] > now) schedule(context, PAYMENT, PAYMENT_ID + i, times[i], true);
        } catch (RuntimeException error) {
            show(context, "KTX 예약 성공 · 결제를 확인하세요", prefs(context).getString("reservation", "") + "\n\n결제 재알림을 설정하지 못했습니다. 코레일톡에서 결제 기한을 확인하세요.", true);
        }
    }

    static void dismissPayment(Context context) {
        prefs(context).edit().putBoolean("payment_pending", false).remove("reservation").remove("deadline").remove("reserved_at").apply();
        for (int i = 0; i < 2; i++) cancel(context, PAYMENT, PAYMENT_ID + i);
        context.getSystemService(NotificationManager.class).cancel(101);
    }

    private static PendingIntent alarmIntent(Context context, String action, int id) {
        return PendingIntent.getBroadcast(context, id, new Intent(context, SearchAlerts.class).setAction(action), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private static void schedule(Context context, String action, int id, long when, boolean precise) {
        AlarmManager alarms = context.getSystemService(AlarmManager.class);
        PendingIntent operation = alarmIntent(context, action, id);
        if (precise && (Build.VERSION.SDK_INT < 31 || alarms.canScheduleExactAlarms())) {
            try { alarms.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, when, operation); return; }
            catch (SecurityException ignored) { }
        }
        alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, when, operation);
    }

    private static void cancel(Context context, String action, int id) {
        context.getSystemService(AlarmManager.class).cancel(alarmIntent(context, action, id));
    }

    static void show(Context context, String title, String message, boolean payment) {
        createChannel(context);
        if (Build.VERSION.SDK_INT >= 33 && context.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return;
        PendingIntent open = PendingIntent.getActivity(context, 0, new Intent(context, MainActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = new Notification.Builder(context, "result").setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title).setContentText(message).setStyle(new Notification.BigTextStyle().bigText(message))
            .setContentIntent(open).setAutoCancel(true).setOnlyAlertOnce(false).setVisibility(Notification.VISIBILITY_PRIVATE);
        if (payment) builder.addAction(new Notification.Action.Builder(null, "결제·취소 확인 · 재알림 끄기", alarmIntent(context, DISMISS, 203)).build());
        try { context.getSystemService(NotificationManager.class).notify(101, builder.build()); }
        catch (SecurityException ignored) { }
    }

    private static void interrupted(Context context) {
        String message = "검색 프로세스가 종료되어 더 이상 조회하지 않습니다. 예약 처리 중이었다면 코레일톡 예약내역을 먼저 확인하세요.";
        finishWatching(context);
        context.getSharedPreferences(SearchService.PREFS, 0).edit().putString("kind", "error").putString("message", message).putLong("updated", System.currentTimeMillis()).apply();
        show(context, "KTX 검색이 중단되었습니다", message, false);
    }

    @Override public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (DISMISS.equals(action)) { dismissPayment(context); return; }
        if (Intent.ACTION_BOOT_COMPLETED.equals(action)) {
            if (prefs(context).getBoolean("watching", false)) interrupted(context);
            if (paymentPending(context)) {
                show(context, "이전 KTX 예약의 결제를 확인하세요", prefs(context).getString("reservation", "코레일톡 예약내역을 확인하세요."), true);
                schedulePayment(context);
            }
            return;
        }
        if (AlarmManager.ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED.equals(action)) {
            if (paymentPending(context)) schedulePayment(context);
            return;
        }
        if (WATCH.equals(action)) {
            if (!prefs(context).getBoolean("watching", false)) return;
            if (ReminderSchedule.interrupted(true, SearchService.running, SystemClock.elapsedRealtime(), prefs(context).getLong("heartbeat", 0))) interrupted(context);
            else schedule(context, WATCH, WATCH_ID, System.currentTimeMillis() + 3 * 60_000, false);
        } else if (PAYMENT.equals(action) && paymentPending(context)) {
            long deadline = prefs(context).getLong("deadline", 0);
            String prefix = deadline > 0 && System.currentTimeMillis() >= deadline
                ? "알림이 늦게 도착해 결제 기한이 지났을 수 있습니다. 예약내역을 확인하세요.\n\n"
                : "예약 후 결제를 확인하세요. 이미 결제·취소했다면 재알림을 끄세요.\n\n";
            show(context, "KTX 결제 기한을 확인하세요", prefix + prefs(context).getString("reservation", "코레일톡 예약내역을 확인하세요."), true);
        }
    }
}
