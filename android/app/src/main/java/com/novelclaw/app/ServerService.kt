package com.novelclaw.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

/**
 * Foreground service that hosts the embedded Python FastAPI server
 * (apps/android_server.py) on 127.0.0.1:[PORT]. Keeping it in a foreground
 * service lets long story-generation jobs survive the activity being backgrounded.
 */
class ServerService : Service() {

    companion object {
        const val PORT = 8765
        private const val CHANNEL_ID = "novelclaw_server"
        private const val NOTIFICATION_ID = 1
        private const val TAG = "NovelClawServer"

        @Volatile
        var startError: String? = null
            private set

        fun start(context: Context) {
            val intent = Intent(context, ServerService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun dataDir(context: Context): File = File(context.filesDir, "novelclaw").apply { mkdirs() }
    }

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "NovelClaw::server").apply {
            setReferenceCounted(false)
            acquire()
        }
        Thread({ startPython() }, "python-boot").start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    private fun startPython() {
        try {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(this))
            }
            val py = Python.getInstance()
            val environ = py.getModule("os").get("environ")!!
            environ.callAttr("__setitem__", "NOVELCLAW_DATA_DIR", dataDir(this).absolutePath)
            environ.callAttr("__setitem__", "APP_INPROCESS_WORKERS", "1")
            environ.callAttr("__setitem__", "HOME", filesDir.absolutePath)
            environ.callAttr("__setitem__", "TMPDIR", cacheDir.absolutePath)
            py.getModule("android_server").callAttr("start", PORT, "127.0.0.1")
            Log.i(TAG, "server starting on 127.0.0.1:$PORT")
        } catch (t: Throwable) {
            startError = t.toString()
            Log.e(TAG, "failed to start Python server", t)
        }
    }

    override fun onDestroy() {
        try {
            if (Python.isStarted()) {
                Python.getInstance().getModule("android_server").callAttr("stop")
            }
        } catch (t: Throwable) {
            Log.w(TAG, "stop failed", t)
        }
        wakeLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, getString(R.string.notif_channel), NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION") Notification.Builder(this)
        }
        return builder
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notif_text))
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }
}
