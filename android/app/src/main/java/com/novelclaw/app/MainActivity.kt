package com.novelclaw.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.view.Gravity
import android.webkit.CookieManager
import android.webkit.URLUtil
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/** Full-screen WebView pointed at the embedded server on 127.0.0.1. */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var status: TextView
    private val handler = Handler(Looper.getMainLooper())
    private val baseUrl = "http://127.0.0.1:${ServerService.PORT}"
    private var waited = 0

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        ServerService.start(this)

        webView = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true
            settings.builtInZoomControls = true
            settings.displayZoomControls = false
            settings.allowFileAccess = false
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val url = request.url
                    if (url.host == "127.0.0.1" || url.host == "localhost") return false
                    // External links open in the system browser.
                    startActivity(Intent(Intent.ACTION_VIEW, url))
                    return true
                }
            }
            setDownloadListener { url, _, contentDisposition, mimeType, _ ->
                download(url, URLUtil.guessFileName(url, contentDisposition, mimeType), mimeType)
            }
        }
        CookieManager.getInstance().setAcceptCookie(true)
        status = TextView(this).apply {
            text = getString(R.string.starting)
            textSize = 18f
            setTextColor(0xFFFFFFFF.toInt())
            gravity = Gravity.CENTER
        }
        val root = FrameLayout(this)
        root.addView(webView, FrameLayout.LayoutParams(-1, -1))
        root.addView(status, FrameLayout.LayoutParams(-1, -1))
        setContentView(root)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else moveTaskToBack(true)
            }
        })

        if (savedInstanceState != null) {
            webView.restoreState(savedInstanceState)
            status.visibility = android.view.View.GONE
        } else {
            waitForServer()
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    private fun waitForServer() {
        Thread {
            val ok = try {
                val c = URL("$baseUrl/healthz").openConnection() as HttpURLConnection
                c.connectTimeout = 500
                c.readTimeout = 1000
                val code = c.responseCode
                c.disconnect()
                code == 200
            } catch (e: Exception) {
                false
            }
            handler.post {
                when {
                    ok -> {
                        status.visibility = android.view.View.GONE
                        webView.loadUrl("$baseUrl/")
                    }
                    ServerService.startError != null ->
                        status.text = "Failed to start the embedded server:\n${ServerService.startError}"
                    else -> {
                        waited++
                        if (waited % 10 == 0) status.text = getString(R.string.starting) + " (${waited / 2}s)"
                        handler.postDelayed({ waitForServer() }, 500)
                    }
                }
            }
        }.start()
    }

    /** Saves exports (manuscripts, chapters) from the local server into Downloads. */
    private fun download(url: String, fileName: String, mimeType: String?) {
        val cookie = CookieManager.getInstance().getCookie(url)
        Thread {
            try {
                val conn = URL(url).openConnection() as HttpURLConnection
                if (cookie != null) conn.setRequestProperty("Cookie", cookie)
                val bytes = conn.inputStream.use { it.readBytes() }
                val where: String
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    val values = ContentValues().apply {
                        put(MediaStore.Downloads.DISPLAY_NAME, fileName)
                        put(MediaStore.Downloads.MIME_TYPE, mimeType ?: "application/octet-stream")
                    }
                    val uri: Uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)!!
                    contentResolver.openOutputStream(uri)!!.use { it.write(bytes) }
                    where = "Downloads/$fileName"
                } else {
                    val dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)!!
                    File(dir, fileName).writeBytes(bytes)
                    where = File(dir, fileName).absolutePath
                }
                handler.post { Toast.makeText(this, "Saved $where", Toast.LENGTH_LONG).show() }
            } catch (e: Exception) {
                handler.post { Toast.makeText(this, "Download failed: $e", Toast.LENGTH_LONG).show() }
            }
        }.start()
    }
}
