package app.contextrelay.mobile;

import android.app.Activity;
import android.app.KeyguardManager;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.net.Uri;
import android.os.Build;
import android.os.CancellationSignal;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.DocumentsContract;
import android.provider.MediaStore;
import android.view.View;
import android.view.ViewTreeObserver;
import android.view.WindowManager;

import java.io.IOException;
import java.io.OutputStream;

/** One owner-verified export of this Activity's current, explicitly eligible page. */
final class VerifiedScreenshot {
    interface Host {
        boolean eligible();
        String pageKey();
        View captureView();
        // Re-enter the existing app-unlock completion path; never grant task permissions here.
        void onCredentialVerified();
        void report(String message);
    }

    private static final int FIRST_REQUEST = 16384;
    // UI-thread only; do not reuse a result code after same-process Activity recreation.
    private static int nextRequest = FIRST_REQUEST;
    private final Activity activity;
    private final Host host;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private Attempt pending;
    private Uri document;
    private CancellationSignal cancellation;
    private boolean resumed;
    private View layoutView;
    private ViewTreeObserver.OnPreDrawListener layoutListener;

    // This one-shot state has no persisted authorization, image, credentials or task content.
    static final class Attempt {
        static final int CHOOSING = 1, VERIFYING = 2, VERIFIED = 3, CONSUMED = 4, CANCELED = 5;
        final String page;
        final int saveRequest, authRequest;
        int stage;
        boolean expectedPause, paused;

        Attempt(String page, int stage, int code) {
            this.page = page; this.stage = stage; saveRequest = code; authRequest = code + 1;
        }
        boolean pause() {
            if ((stage == CHOOSING || stage == VERIFYING) && expectedPause) {
                expectedPause = false;
                paused = true;
                return true;
            }
            stage = CANCELED;
            return false;
        }
        boolean verify() {
            if (stage != VERIFYING) return false;
            stage = VERIFIED;
            return true;
        }
        boolean consume(String currentPage, boolean eligible) {
            if (stage != VERIFIED || !eligible || page == null || !page.equals(currentPage)) {
                stage = CANCELED;
                return false;
            }
            stage = CONSUMED;
            return true;
        }
    }

    VerifiedScreenshot(Activity activity, Host host) { this.activity = activity; this.host = host; }

    boolean isPending() { return pending != null; }

    // Only call from the current, manually checked risk-consent dialog's Continue action.
    void request() {
        if (pending != null) { host.report("已有截图验证，请先完成或取消。"); return; }
        String key = host.pageKey();
        if (!resumed || !host.eligible() || key == null || !secureDevice()) {
            host.report("当前页面不能截图，请返回已解锁的任务或列表后重试。");
            return;
        }
        if (nextRequest >= 65534) { host.report("请重新打开应用后再截图。"); return; }
        Attempt attempt = new Attempt(key, Build.VERSION.SDK_INT < 29 ? Attempt.CHOOSING : Attempt.VERIFYING, nextRequest);
        nextRequest += 2;
        pending = attempt;
        handler.postDelayed(() -> {
            if (pending == attempt) fail("截图请求已过期，请重新阅读提示并验证。");
        }, 180000);
        if (Build.VERSION.SDK_INT < 29) {
            // Choose first: neither a bitmap nor a verified permit crosses the document picker.
            Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .setType("image/png").putExtra(Intent.EXTRA_TITLE, filename());
            attempt.expectedPause = true;
            try { activity.startActivityForResult(intent, attempt.saveRequest); }
            catch (RuntimeException error) { fail("无法打开系统保存位置，未保存截图。"); }
        } else authenticate(attempt);
    }

    private boolean secureDevice() {
        KeyguardManager keyguard = (KeyguardManager) activity.getSystemService(Activity.KEYGUARD_SERVICE);
        return keyguard != null && keyguard.isDeviceSecure();
    }

    private void authenticate(Attempt attempt) {
        if (pending != attempt || !secureDevice()) { cancel(); return; }
        attempt.stage = Attempt.VERIFYING;
        attempt.expectedPause = true;
        if (Build.VERSION.SDK_INT >= 30) {
            cancellation = new CancellationSignal();
            try {
                new BiometricPrompt.Builder(activity).setTitle("验证后保存当前页截图")
                        .setSubtitle("本次验证仅允许保存一张截图")
                        .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG
                                | BiometricManager.Authenticators.DEVICE_CREDENTIAL)
                        .build().authenticate(cancellation, activity.getMainExecutor(),
                                new BiometricPrompt.AuthenticationCallback() {
                                    @Override public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                                        verified(attempt);
                                    }
                                    @Override public void onAuthenticationError(int code, CharSequence message) {
                                        if (pending == attempt) fail("身份验证已取消或未完成，未保存截图。");
                                    }
                                });
            } catch (RuntimeException error) { fail("系统身份验证暂不可用，未保存截图。"); }
        } else {
            KeyguardManager keyguard = (KeyguardManager) activity.getSystemService(Activity.KEYGUARD_SERVICE);
            Intent intent = keyguard.createConfirmDeviceCredentialIntent("验证后保存当前页截图", "本次验证仅允许保存一张截图");
            if (intent == null) { fail("系统身份验证暂不可用，未保存截图。"); return; }
            try { activity.startActivityForResult(intent, attempt.authRequest); }
            catch (RuntimeException error) { fail("无法打开系统身份验证，未保存截图。"); }
        }
    }

    boolean onActivityResult(int request, int result, Intent data) {
        if (request < FIRST_REQUEST || request >= nextRequest) return false;
        Attempt attempt = pending;
        if (attempt == null) return true; // Late results never start another request.
        if (request == attempt.saveRequest) {
            if (attempt.stage != Attempt.CHOOSING) return true;
            Uri uri = data == null ? null : data.getData();
            if (result != Activity.RESULT_OK || uri == null || !"content".equals(uri.getScheme())) {
                fail("已取消保存，未生成截图。");
            } else {
                document = uri;
                authenticate(attempt);
            }
        } else if (request == attempt.authRequest && attempt.stage == Attempt.VERIFYING) {
            if (result == Activity.RESULT_OK) verified(attempt);
            else fail("身份验证未完成，未保存截图。");
        }
        return true;
    }

    private void verified(Attempt attempt) {
        if (pending != attempt) return;
        if (!secureDevice() || activity.isFinishing()) { cancel(); return; }
        if (!attempt.verify()) return;
        cancellation = null;
        if (attempt.paused) host.onCredentialVerified();
        scheduleCapture();
    }

    void onPause() {
        resumed = false;
        if (pending != null && !pending.pause()) cancel();
    }

    void onResume() { resumed = true; scheduleCapture(); }

    void onWindowFocusChanged(boolean focused) { if (focused) scheduleCapture(); }

    private void scheduleCapture() {
        Attempt attempt = pending;
        if (attempt == null || attempt.stage != Attempt.VERIFIED || !resumed) return;
        // Let system authentication and the app's normal restored-page layout finish first.
        handler.post(() -> {
            if (pending != attempt || !resumed || !activity.hasWindowFocus()) return;
            View view = host.captureView();
            if (view != null && (!view.isLaidOut() || view.isLayoutRequested())) {
                if (layoutView == view && layoutListener != null) return;
                clearLayoutWait();
                layoutView = view;
                layoutListener = () -> {
                    clearLayoutWait();
                    if (pending == attempt && resumed && activity.hasWindowFocus()) capture(attempt);
                    return true;
                };
                view.getViewTreeObserver().addOnPreDrawListener(layoutListener);
                handler.postDelayed(() -> {
                    if (pending == attempt && layoutListener != null) fail("页面尚未显示完整，未保存截图，请稍后重试。");
                }, 2000);
            } else {
                clearLayoutWait();
                capture(attempt);
            }
        });
    }

    private void clearLayoutWait() {
        if (layoutView != null && layoutListener != null && layoutView.getViewTreeObserver().isAlive())
            layoutView.getViewTreeObserver().removeOnPreDrawListener(layoutListener);
        layoutView = null;
        layoutListener = null;
    }

    private void capture(Attempt attempt) {
        if (pending != attempt) return;
        View view = host.captureView();
        boolean allowed = host.eligible() && secureDevice() && !activity.isFinishing()
                && view != null && view.isAttachedToWindow() && view.isShown()
                && view.getRootView() == activity.getWindow().getDecorView()
                && (activity.getWindow().getAttributes().flags & WindowManager.LayoutParams.FLAG_SECURE) != 0;
        if (!attempt.consume(host.pageKey(), allowed)) {
            fail("页面或身份状态已变化，未保存截图；请在当前页重新验证。");
            return;
        }
        Bitmap bitmap = null;
        Uri output = null;
        boolean saved = false;
        ContentResolver resolver = activity.getContentResolver();
        try {
            int width = view.getWidth(), height = view.getHeight();
            if (width <= 0 || height <= 0 || (long) width * height > 16000000L) throw new IOException();
            bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
            view.draw(new Canvas(bitmap)); // Own current View only; never a display, system UI or another app.
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues values = new ContentValues();
                values.put(MediaStore.Images.Media.DISPLAY_NAME, filename());
                values.put(MediaStore.Images.Media.MIME_TYPE, "image/png");
                values.put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/Screenshots");
                values.put(MediaStore.Images.Media.IS_PENDING, 1);
                output = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
            } else output = document;
            if (output == null) throw new IOException();
            try (OutputStream stream = resolver.openOutputStream(output, "w")) {
                if (stream == null || !bitmap.compress(Bitmap.CompressFormat.PNG, 100, stream)) throw new IOException();
            }
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues values = new ContentValues();
                values.put(MediaStore.Images.Media.IS_PENDING, 0);
                if (resolver.update(output, values, null, null) != 1) throw new IOException();
            }
            saved = true;
        } catch (IOException | RuntimeException | OutOfMemoryError error) {
            if (output != null && Build.VERSION.SDK_INT >= 29) {
                try { resolver.delete(output, null, null); } catch (RuntimeException ignored) { }
            }
        } finally {
            if (bitmap != null) bitmap.recycle();
            if (saved) document = null;
            cancel();
        }
        host.report(saved ? "已保存当前页截图；它不包含系统键盘或其他应用。" : "截图保存失败，未完成保存。");
    }

    private static String filename() { return "ContextRelay-" + System.currentTimeMillis() + ".png"; }

    private void fail(String message) { cancel(); host.report(message); }

    void cancel() {
        if (pending != null) pending.stage = Attempt.CANCELED;
        pending = null;
        clearLayoutWait();
        handler.removeCallbacksAndMessages(null);
        CancellationSignal signal = cancellation;
        cancellation = null;
        if (signal != null) signal.cancel();
        if (document != null) {
            try { DocumentsContract.deleteDocument(activity.getContentResolver(), document); }
            catch (Exception ignored) { }
            document = null;
        }
    }
}
