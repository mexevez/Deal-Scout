# Deal Scout Android

This project packages your existing `deal_scout.py` into a normal Android application.
It uses Chaquopy to bundle Python inside the APK and an Android WebView to display the
existing Deal Scout interface. Termux is not required after the APK is installed.

## What you need on your computer

- Android Studio
- Internet access for the first Gradle sync/build
- Your Android phone and a USB cable, or another way to transfer the APK

## Build the APK

1. Unzip `DealScoutAndroid.zip`.
2. Open Android Studio.
3. Choose **Open** and select the unzipped `DealScoutAndroid` folder.
4. If Android Studio asks whether you trust the project, choose **Trust Project**.
5. Allow Android Studio to download/sync the Android SDK and Gradle dependencies.
   The first sync can take several minutes.
6. If Android Studio offers to install Android SDK Platform 35, accept it.
7. Wait until Gradle sync finishes without errors.
8. From the top menu choose **Build > Build Bundle(s) / APK(s) > Build APK(s)**.
9. When the build finishes, click **Locate** in the notification.
10. The debug APK is normally:
    `app/build/outputs/apk/debug/app-debug.apk`

## Install it on your Samsung

1. Copy `app-debug.apk` to your phone.
2. Open the APK from Samsung **My Files**.
3. Android may say installs from that source aren't allowed. Tap **Settings** and
   temporarily enable **Allow from this source** for My Files (or the app you used
   to open the APK).
4. Go back and tap **Install**.
5. Open **Deal Scout** from your app drawer.
6. You no longer need Termux to run Deal Scout.

## Updating the Python app later

The bundled Python source is:
`app/src/main/python/deal_scout.py`

Replace that file with your newer version, then build the APK again.

## Notes

- Deal Scout still obtains public shopping pages over the internet, so the phone
  must be online.
- Stores can change their HTML or block automated requests. If a store starts
  returning no results, the parser in `deal_scout.py` may need updating.
- This project uses a localhost server internally. That's normal: the Python server
  and Android WebView both run inside the Deal Scout app.
- The package ID is `com.dealscout.app`.
- Version 1.0 is configured as a debug build for easy personal installation.
