package com.dealscout.app

import android.annotation.SuppressLint
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private lateinit var statusText: TextView
    private val localUrl = "http://127.0.0.1:8765"

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        statusText = findViewById(R.id.statusText)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.loadsImagesAutomatically = true
        webView.webViewClient = WebViewClient()
        webView.webChromeClient = WebChromeClient()

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        // The Python file already contains its own HTTP server. Start main() on a
        // background thread and suppress its desktop browser launcher.
        thread(name = "DealScoutPython", isDaemon = true) {
            try {
                val py = Python.getInstance()
                val os = py.getModule("os")
                os["environ"].callAttr("__setitem__", "DEAL_SCOUT_NO_BROWSER", "1")
                py.getModule("deal_scout").callAttr("main")
            } catch (e: Exception) {
                runOnUiThread {
                    statusText.text = "Deal Scout couldn't start.\n\n${e.message}"
                }
            }
        }

        waitForServer()
    }

    private fun waitForServer(attempt: Int = 0) {
        thread {
            val ready = try {
                val c = URL("$localUrl/api/status").openConnection() as HttpURLConnection
                c.connectTimeout = 500
                c.readTimeout = 500
                c.responseCode == 200
            } catch (_: Exception) { false }

            runOnUiThread {
                if (ready) {
                    statusText.visibility = View.GONE
                    webView.visibility = View.VISIBLE
                    webView.loadUrl(localUrl)
                } else if (attempt < 30) {
                    Handler(Looper.getMainLooper()).postDelayed(
                        { waitForServer(attempt + 1) }, 250
                    )
                } else {
                    statusText.text = "Deal Scout took too long to start. Close the app and try again."
                }
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }
}
