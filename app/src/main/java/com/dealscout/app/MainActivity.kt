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

        // Configure WebView
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.loadsImagesAutomatically = true

        webView.webViewClient = WebViewClient()
        webView.webChromeClient = WebChromeClient()

        // Start Python
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        // Start Deal Scout Python server
        thread(
            name = "DealScoutPython",
            isDaemon = true
        ) {

            try {

                val py = Python.getInstance()

                // Set environment variable so Python doesn't try
                // to launch a desktop browser on Android.
                val os = py.getModule("os")
                val environ = os["environ"]

                if (environ != null) {
                    environ.callAttr(
                        "__setitem__",
                        "DEAL_SCOUT_NO_BROWSER",
                        "1"
                    )
                }

                // Load deal_scout.py
                val dealScout = py.getModule("deal_scout")

                // Start Python main()
                dealScout.callAttr("main")

            } catch (e: Exception) {

                e.printStackTrace()

                runOnUiThread {

                    statusText.visibility = View.VISIBLE

                    statusText.text =
                        "Deal Scout couldn't start.\n\n" +
                        (e.message ?: e.toString())
                }
            }
        }

        // Wait until the Python HTTP server is ready
        waitForServer()
    }

    private fun waitForServer(attempt: Int = 0) {

        thread {

            val ready = try {

                val connection =
                    URL("$localUrl/api/status")
                        .openConnection() as HttpURLConnection

                connection.connectTimeout = 500
                connection.readTimeout = 500
                connection.requestMethod = "GET"

                val responseCode = connection.responseCode

                connection.disconnect()

                responseCode == HttpURLConnection.HTTP_OK

            } catch (_: Exception) {

                false
            }

            runOnUiThread {

                if (ready) {

                    statusText.visibility = View.GONE

                    webView.visibility = View.VISIBLE

                    webView.loadUrl(localUrl)

                } else if (attempt < 40) {

                    Handler(Looper.getMainLooper()).postDelayed(
                        {
                            waitForServer(attempt + 1)
                        },
                        250
                    )

                } else {

                    statusText.visibility = View.VISIBLE

                    statusText.text =
                        "Deal Scout took too long to start.\n\n" +
                        "Close the app and try again."
                }
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {

        if (webView.canGoBack()) {

            webView.goBack()

        } else {

            super.onBackPressed()
        }
    }
}
