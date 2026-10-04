package app.contextrelay.mobile;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.KeyguardManager;
import android.content.Intent;
import android.content.ClipData;
import android.content.ClipDescription;
import android.content.ClipboardManager;
import android.graphics.Color;
import android.graphics.Rect;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PersistableBundle;
import android.text.Editable;
import android.text.InputType;
import android.text.TextUtils;
import android.text.TextWatcher;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.style.StyleSpan;
import android.text.style.RelativeSizeSpan;
import android.util.Base64;
import android.view.View;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.HorizontalScrollView;
import android.widget.ScrollView;
import android.widget.TextView;
import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONArray;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    // Operate: familiar native chat, real completed messages, a stable composer, and explicit approvals.
    // White reading surface, neutral user bubbles, system type; no invented streaming or decorations.
    private static final int UNLOCK_REQUEST = 701;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final List<Button> actions = new ArrayList<>();
    private final List<AlertDialog> dialogs = new ArrayList<>();
    private Vault vault;
    private JSONObject saved;
    private JSONObject drafts;
    private JSONObject task;
    private JSONArray tasks = new JSONArray();
    private TextView status;
    private TextView operation;
    private LinearLayout content;
    private LinearLayout root, header, composer, timeline;
    private TextView taskStatus;
    private Button send, pause, latest, requestsAction;
    private String originalInfo = "", originalError = "";
    private JSONObject originalPage;
    private String originalCursor;
    private boolean originalView, originalChoice;
    private ScrollView scroll;
    private EditText message;
    private boolean busy, refreshing, popup, storageFailed, pairingView, authPending, unlockApproved, offline, requestOnResume = true;
    private volatile boolean foreground, unlocked;
    private volatile int authEpoch;
    private boolean keyboardOpen, statusPinned;
    private int generation;
    private String selectedId;
    private String incomingPair;
    private String timelineSignature = "";
    private String listSignature = "";
    private String resumeTaskId;

    private interface Job { JSONObject run(int epoch) throws Exception; }
    private interface Result { void accept(JSONObject value) throws Exception; }

    private final Runnable poller = new Runnable() {
        public void run() {
            if (!authorized() || isFinishing()) return;
            if (!busy && !refreshing && !popup && !pairingView && connection() != null && !connection().optBoolean("needs_pairing")) {
                if (pending() != null && !pending().optBoolean("rejected")
                        && (pending().optJSONObject("receipt") == null || !Protocol.terminalReceipt(pending().optJSONObject("receipt").optString("state")))) queryOperation();
                else if (pending() == null) refresh(true);
            }
            handler.postDelayed(this, 5000);
        }
    };
    private final Runnable saveDrafts = () -> persist();

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        readIntent(getIntent());
        showLocked("请验证手机锁屏身份后继续。");
    }

    private boolean deviceSecure() {
        KeyguardManager manager = (KeyguardManager) getSystemService(KEYGUARD_SERVICE);
        return manager != null && manager.isDeviceSecure();
    }

    private boolean authorized() { return Protocol.authorized(unlocked, foreground, deviceSecure(), authEpoch, authEpoch); }

    private void showLocked(String reason) {
        LinearLayout box = column(); box.setPadding(dp(24), dp(32), dp(24), dp(24));
        text(box, "Context Relay 已锁定", 24);
        text(box, reason + "\n离开 App 后再次进入需要重新验证。未确认的操作仍保留，不会自动重发。", 17);
        Button unlock = new Button(this); unlock.setText("验证手机身份");
        unlock.setOnClickListener(v -> requestUnlock()); box.addView(unlock);
        setContentView(box);
    }

    private void requestUnlock() {
        if (!foreground || authPending || storageFailed) return;
        requestOnResume = false;
        KeyguardManager manager = (KeyguardManager) getSystemService(KEYGUARD_SERVICE);
        if (manager == null || !manager.isDeviceSecure()) {
            showLocked("请先在手机系统设置中启用锁屏密码、PIN 或图案。未设置安全锁时，不能读取连接或操作电脑。");
            return;
        }
        Intent intent = manager.createConfirmDeviceCredentialIntent("解锁 Context Relay", "验证手机锁屏身份后查看对话和控制电脑");
        if (intent == null) { showLocked("系统暂时无法验证身份，请检查手机锁屏设置后重试。"); return; }
        authPending = true;
        try { startActivityForResult(intent, UNLOCK_REQUEST); }
        catch (Exception ex) { authPending = false; showLocked("无法打开系统身份验证，请稍后重试。"); }
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == UNLOCK_REQUEST) {
            authPending = false;
            unlockApproved = result == RESULT_OK && deviceSecure();
            if (!unlockApproved) showLocked("身份验证未完成，连接和任务保持锁定。");
        }
    }

    private void openUnlocked() {
        try {
            vault = new Vault(this);
            saved = vault.read();
            drafts = saved.optJSONObject("drafts");
            if (drafts == null) { drafts = new JSONObject(); saved.put("drafts", drafts); }
            resumeTaskId = saved.optString("selected_task", "");
            if (resumeTaskId.isEmpty()) resumeTaskId = null;
            task = saved.optJSONObject("last_task");
            if (task != null && (!task.optString("id").equals(resumeTaskId) || connection() == null
                    || connection().optBoolean("needs_pairing")
                    || !saved.optString("last_task_pin").equals(connection().optString("certificate_sha256")))) task = null;
            offline = true; // A sealed snapshot is display-only until the computer answers again.
        } catch (Exception ex) {
            storageFailed = true;
            LinearLayout box = column();
            text(box, "无法安全读取本机记录", 24);
            text(box, "未自动清空数据或重新发送。请先在电脑核对未完成操作，再处理手机存储。\n" + safeError(ex), 17);
            setContentView(box);
            return;
        }
        root = column();
        root.setBackgroundColor(Color.WHITE);
        root.setPadding(dp(14), dp(12), dp(14), dp(8));
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(dp(14) + insets.getSystemWindowInsetLeft(), dp(8) + insets.getSystemWindowInsetTop(),
                    dp(14) + insets.getSystemWindowInsetRight(), dp(8) + insets.getSystemWindowInsetBottom());
            return insets;
        });
        header = column(); root.addView(header);
        status = text(root, "手机操作 · 电脑执行", 15);
        compactStatus(status);
        status.setVisibility(View.GONE);
        operation = text(root, "", 14);
        compactStatus(operation);
        operation.setOnClickListener(v -> showOperationDetails());
        operation.setTextColor(Color.rgb(138, 59, 0));
        scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setOnScrollChangeListener((view, x, y, oldX, oldY) -> updateLatestButton());
        content = column();
        scroll.addView(content);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        composer = column(); root.addView(composer);
        setContentView(root);
        root.getViewTreeObserver().addOnGlobalLayoutListener(() -> {
            if (!authorized()) return;
            Rect visible = new Rect(); root.getWindowVisibleDisplayFrame(visible);
            boolean open = root.getRootView().getHeight() - visible.bottom > dp(150);
            if (open != keyboardOpen) { keyboardOpen = open; compactChat(); }
        });
        if (connection() == null || incomingPair != null) showPairing();
        else {
            if (task != null) { selectedId = resumeTaskId; showTask(); }
            else { showTasks(); selectedId = resumeTaskId; }
            if (pending() != null) queryOperation(); else refresh();
        }
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        readIntent(intent);
        if (authorized() && !storageFailed && incomingPair != null) showPairing();
    }

    private void readIntent(Intent intent) {
        if (intent != null && intent.getData() != null) {
            incomingPair = intent.getData().toString();
            intent.setData(null); // Do not keep pairing secrets in a recreated Activity intent.
        }
    }

    @Override protected void onResume() {
        super.onResume();
        foreground = true;
        if (unlockApproved && deviceSecure()) {
            unlockApproved = false; unlocked = true; openUnlocked();
            handler.removeCallbacks(poller); handler.postDelayed(poller, 500);
        } else if (requestOnResume && !authPending && !storageFailed) requestUnlock();
    }
    @Override protected void onPause() {
        authEpoch++; generation++; // Invalidate in-flight permits before disk writes can delay the foreground transition.
        handler.removeCallbacks(poller);
        handler.removeCallbacks(saveDrafts);
        if (unlocked) {
            resumeTaskId = selectedId; requestOnResume = true;
            if (saved != null) {
                put(saved, "selected_task", selectedId == null ? JSONObject.NULL : selectedId);
                if (task != null && connection() != null && !connection().optBoolean("needs_pairing")) {
                    JSONObject snapshot = new JSONObject();
                    for (String name : new String[] {"id", "title", "state", "mode", "last_message", "messages", "messages_truncated", "error", "connection_mode",
                            "summary_only", "message_preview"})
                        if (task.has(name)) put(snapshot, name, task.opt(name));
                    put(snapshot, "cached_conversation", conversationAvailable(task));
                    if (snapshot.toString().getBytes(StandardCharsets.UTF_8).length <= 512 * 1024) {
                        put(saved, "last_task", snapshot); put(saved, "last_task_pin", connection().optString("certificate_sha256"));
                    } else { saved.remove("last_task"); saved.remove("last_task_pin"); }
                }
            }
            if (!storageFailed) persist();
        }
        foreground = false; unlocked = false;
        for (AlertDialog dialog : new ArrayList<>(dialogs)) dialog.dismiss();
        saved = null; drafts = null; vault = null; task = null; tasks = new JSONArray();
        if (timeline != null) clearTimeline();
        originalPage = null; originalCursor = null; originalInfo = ""; originalError = ""; timelineSignature = "";
        selectedId = null; message = null;
        showLocked("请验证手机锁屏身份后继续。");
        super.onPause();
    }
    @Override protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        worker.shutdownNow();
        super.onDestroy();
    }
    @Override public void onBackPressed() {
        if (authorized() && selectedId != null && !busy) { selectedId = null; task = null; showTasks(); refresh(); }
        else super.onBackPressed();
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private LinearLayout column() { LinearLayout box = new LinearLayout(this); box.setOrientation(LinearLayout.VERTICAL); return box; }
    private TextView text(LinearLayout parent, String value, int size) {
        TextView view = new TextView(this);
        view.setText(value); view.setTextSize(size); view.setTextIsSelectable(true);
        view.setTextColor(Color.rgb(28, 31, 36));
        view.setPadding(0, dp(5), 0, dp(5));
        parent.addView(view, new LinearLayout.LayoutParams(-1, -2));
        return view;
    }
    private EditText input(LinearLayout parent, String hint, String value, boolean multiline) {
        EditText view = new ScrollingEditText(this);
        view.setHint(hint); view.setText(value); view.setTextSize(18);
        view.setInputType(InputType.TYPE_CLASS_TEXT | (multiline ? InputType.TYPE_TEXT_FLAG_MULTI_LINE | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES : 0));
        view.setMinLines(1); view.setMaxLines(multiline ? 3 : 2);
        parent.addView(view, new LinearLayout.LayoutParams(-1, -2));
        return view;
    }
    private void compactStatus(TextView view) {
        view.setMaxLines(2);
        view.setEllipsize(TextUtils.TruncateAt.END);
        view.setOnClickListener(v -> {
            if (view.getText().length() == 0) return;
            popup = true;
            AlertDialog dialog = new AlertDialog.Builder(this).setTitle("完整状态说明")
                    .setMessage(view.getText()).setPositiveButton("关闭", null).create();
            dialog.setOnDismissListener(d -> popup = false);
            showDialog(dialog, false);
        });
    }
    private Button button(LinearLayout parent, String label, boolean enabled, Runnable callback) {
        Button view = new Button(this);
        view.setText(label); view.setAllCaps(false); view.setTextSize(16);
        view.setEnabled(enabled && !busy && !storageFailed && authorized());
        view.setMinHeight(dp(48));
        view.setTag(enabled);
        view.setOnClickListener(v -> { if (authorized()) try { callback.run(); } catch (Exception ex) { error(ex); } });
        parent.addView(view, new LinearLayout.LayoutParams(-1, -2));
        actions.add(view);
        return view;
    }
    private void controls() {
        for (Button item : actions) item.setEnabled(Boolean.TRUE.equals(item.getTag()) && !busy && !storageFailed && authorized());
    }
    private void clearPage() {
        generation++; content.removeAllViews(); header.removeAllViews(); composer.removeAllViews();
        actions.clear(); message = null; timeline = null; timelineSignature = ""; listSignature = "";
        taskStatus = null; send = null; pause = null; latest = null; requestsAction = null;
        originalInfo = ""; originalError = ""; statusPinned = false; status.setVisibility(View.GONE);
        originalPage = null; originalCursor = null; originalView = false; originalChoice = false;
        status.setMaxLines(2); operation.setMaxLines(2);
        text(header, "Context Relay", 22);
    }
    private JSONObject connection() { return saved == null ? null : saved.optJSONObject("connection"); }
    private JSONObject pending() { return saved == null ? null : saved.optJSONObject("pending"); }
    private String pendingFailure() {
        JSONObject current = pending();
        if (current == null) return "";
        JSONObject receipt = current.optJSONObject("receipt");
        JSONObject failure = receipt == null ? null : receipt.optJSONObject("error");
        boolean rejected = current.optBoolean("rejected");
        return Protocol.failureNotice(receipt == null ? "" : receipt.optString("state"), rejected,
                rejected ? current.optString("rejection") : failure == null ? "" : failure.optString("message"));
    }
    private void statusWithFailure(String value) {
        showStatus(value);
        if (pending() != null) status.setVisibility(View.GONE); // The actionable receipt notice already carries this state.
    }
    private void showStatus(String value) {
        statusPinned = true;
        status.setText(value);
        status.setVisibility(View.VISIBLE);
    }
    private void showProgress(String value) {
        showStatus(value);
        statusPinned = false;
    }
    private boolean controlAllowed(JSONObject value) {
        return connection() != null && !connection().optBoolean("needs_pairing")
                && fullTask(value)
                && "control".equals(value.optString("remote_access", connection().optString("scope", "read_only")));
    }
    private boolean fullTask(JSONObject value) {
        return value != null && Protocol.fullTask(value.optBoolean("summary_only"), value.optString("etag", null));
    }
    private boolean conversationAvailable(JSONObject value) {
        return value != null && (fullTask(value) || Protocol.conversationSnapshot(value.optBoolean("summary_only"),
                value.optBoolean("cached_conversation"), value.has("messages"), value.has("last_message")));
    }
    private void expireConnection() {
        if (connection() == null) return;
        connection().remove("token"); put(connection(), "needs_pairing", true); offline = true;
        saved.remove("last_task"); saved.remove("last_task_pin"); saved.remove("selected_task");
        task = null; tasks = new JSONArray(); resumeTaskId = null; selectedId = null;
        persist(); showTasks();
    }
    private String draftKey(String id) { return (connection() == null ? "" : connection().optString("certificate_sha256")) + ":" + id; }
    private String draft(String id) { return drafts.optString(draftKey(id), ""); }
    private void put(JSONObject object, String key, Object value) {
        try { object.put(key, value); } catch (Exception ex) { throw new IllegalStateException(ex); }
    }
    private boolean persist() {
        if (!authorized() || vault == null || saved == null) return false;
        try { vault.write(saved); return true; }
        catch (Exception ex) { storageFailed = true; if (status != null) showStatus("本机保存失败；已停止发送。请在电脑核对。" + safeError(ex)); controls(); return false; }
    }
    private void error(Exception ex) {
        if (!authorized()) return;
        if (ex instanceof IllegalArgumentException) { if (status != null) showStatus(safeError(ex)); return; }
        if (ex instanceof HttpApi.ApiError && "invalid_pairing".equals(((HttpApi.ApiError) ex).code)) {
            showStatus("配对信息无效、已使用或已过期。请在电脑重新生成配对信息，再完整复制到这里。"); return;
        }
        offline = true;
        if (ex instanceof HttpApi.ApiError && ((HttpApi.ApiError) ex).status == 401) expireConnection();
        if (status != null) showStatus(ex instanceof HttpApi.ApiError && ((HttpApi.ApiError) ex).status == 401
                ? "连接授权已到期或被撤销。请在电脑重新配对；未确认操作先在电脑核对。"
                : "连接未更新 · 保留最后内容\n" + safeError(ex));
        updateTaskControls();
    }
    private static String safeError(Exception ex) {
        String value = ex.getMessage();
        if (value == null || value.isEmpty()) value = ex.getClass().getSimpleName();
        return value.length() > 700 ? value.substring(0, 700) : value;
    }
    private static String pretty(Object value) {
        if (value == null || value == JSONObject.NULL) return "无";
        try {
            if (value instanceof JSONObject) return ((JSONObject) value).toString(2);
            if (value instanceof JSONArray) return ((JSONArray) value).toString(2);
        } catch (Exception ignored) { }
        return value.toString();
    }
    private static String pathId(String value) throws Exception { return URLEncoder.encode(value, "UTF-8"); }
    private static String shortTaskId(String value) {
        if (value == null || value.isEmpty()) return "未知任务";
        return value.substring(0, Math.min(8, value.length()));
    }
    private String pendingTaskLabel(JSONObject current) {
        JSONObject body = current == null ? null : current.optJSONObject("body");
        String id = body == null ? "" : body.optString("task_id");
        String title = current == null ? "" : current.optString("task_title").trim();
        return (title.isEmpty() ? "任务" : Protocol.preview(title)) + " · " + shortTaskId(id);
    }

    private HttpApi api(JSONObject target, int epoch) throws Exception {
        return new HttpApi(target, () -> Protocol.authorized(unlocked, foreground, deviceSecure(), epoch, authEpoch));
    }

    private void run(String label, Job job, Result result) {
        run(label, job, result, false);
    }

    private void run(String label, Job job, Result result, boolean silent) {
        if (!authorized() || busy || silent && refreshing || storageFailed || isFinishing()) return;
        if (silent) refreshing = true;
        else { busy = true; controls(); showProgress(label); }
        final int bound = generation;
        final int epoch = authEpoch;
        worker.execute(() -> {
            JSONObject value = null; Exception failure = null;
            try {
                if (!Protocol.authorized(unlocked, foreground, deviceSecure(), epoch, authEpoch)) throw new IllegalStateException("手机已锁定，未开始发送。");
                value = job.run(epoch);
            } catch (Exception ex) { failure = ex; }
            final JSONObject returned = value; final Exception problem = failure;
            handler.post(() -> {
                if (isDestroyed() || isFinishing()) return;
                if (silent) refreshing = false; else busy = false;
                if (!Protocol.authorized(unlocked, foreground, deviceSecure(), epoch, authEpoch)) return;
                if (silent && busy) return; // A confirmed command owns the next visible state; its queued dispatch is never dropped.
                if (!silent) controls();
                // Reads are bound to their original page; late results never switch tasks.
                if (bound != generation) return;
                if (problem != null) { error(problem); return; }
                try { result.accept(returned); } catch (Exception ex) { error(ex); }
            });
        });
    }

    private void confirm(String title, String detail, Runnable action) {
        if (!authorized()) return;
        popup = true;
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle(title).setMessage(detail)
                .setNegativeButton("取消", null).setPositiveButton("确认本次操作", (d, which) -> {
                    if (!authorized()) return;
                    for (AlertDialog open : new ArrayList<>(dialogs)) open.dismiss();
                    action.run();
                }).create();
        dialog.setOnDismissListener(d -> popup = false);
        showDialog(dialog, false);
    }

    private void showDialog(AlertDialog dialog, boolean inputForm) {
        if (!authorized()) return;
        dialogs.add(dialog); popup = true;
        dialog.setOnDismissListener(d -> {
            forgetActions(dialog.getWindow().getDecorView());
            dialogs.remove(dialog); popup = !dialogs.isEmpty();
        });
        dialog.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        if (inputForm) dialog.getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
                | WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN);
        dialog.show();
        if (inputForm) {
            // Let Android reserve space for the IME; the dialog's ScrollView shrinks above its action bar.
            dialog.getWindow().setLayout(WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT);
            dialog.getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
                    | WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN);
        }
    }

    private void dismissDialogs() {
        for (AlertDialog dialog : new ArrayList<>(dialogs)) dialog.dismiss();
    }

    private void showPanel(String title, LinearLayout box) {
        ScrollView viewport = new ScrollView(this); viewport.addView(box);
        showDialog(new AlertDialog.Builder(this).setTitle(title).setView(viewport).setPositiveButton("关闭", null).create(), true);
    }

    private void showPairing() {
        clearPage(); selectedId = null; task = null; pairingView = true;
        text(content, "连接自己的电脑", 22);
        text(content, "1  电脑：打开手机连接，选择允许查看的任务和控制权限。\n2  电脑：复制配对信息，通过可信方式传到手机。\n3  手机：粘贴并核对电脑地址与证书指纹。", 16);
        text(content, "手机与电脑须在同一网络或已配置的私有网络。配对信息含一次性密钥，请勿公开。", 14);
        if (pending() != null) {
            text(content, "仍有结果待核对的操作，不能替换连接或丢弃原请求。请返回查询。", 17);
            button(content, "返回原连接", true, () -> { incomingPair = null; showTasks(); });
            return;
        }
        EditText code = input(content, "粘贴 contextrelay://pair#…", incomingPair == null ? "" : incomingPair, true);
        code.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        incomingPair = null;
        button(content, "粘贴配对信息", true, () -> {
            try {
                ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
                ClipData clip = clipboard == null ? null : clipboard.getPrimaryClip(); // User click only; never inspect the clipboard on resume/poll.
                if (clip == null || clip.getItemCount() != 1 || !clip.getDescription().hasMimeType(ClipDescription.MIMETYPE_TEXT_PLAIN))
                    throw new IllegalArgumentException("剪贴板没有一段可用的配对文字，请先复制电脑生成的配对信息。");
                ClipData.Item item = clip.getItemAt(0);
                if (item.getUri() != null || item.getIntent() != null || item.getHtmlText() != null)
                    throw new IllegalArgumentException("这里只粘贴纯文字配对信息，不读取文件、网页或其他应用内容。");
                String raw = Protocol.pairingUri(item.getText());
                pairingInfo(raw);
                code.setText(raw);
                showProgress("已粘贴。点“核对并配对”检查电脑身份；尚未建立连接。");
            } catch (Exception ex) { error(ex); }
        });
        button(content, "核对并配对", true, () -> {
            try {
                JSONObject info = pairingInfo(Protocol.pairingUri(code.getText()));
                String endpoint = Protocol.endpoint(info.getString("endpoint"));
                String pin = Protocol.pin(info.getString("certificate_sha256"));
                String secret = info.getString("secret");
                if (secret.length() < 16 || secret.length() > 512) throw new IllegalArgumentException("配对密钥无效。");
                JSONObject target = new JSONObject().put("endpoint", endpoint).put("certificate_sha256", pin);
                JSONObject body = new JSONObject().put("secret", secret).put("device_name", Build.MANUFACTURER + " " + Build.MODEL);
                confirm("确认电脑身份", endpoint + "\n证书 SHA-256：\n" + pin + "\n请与电脑显示的内容核对。", () ->
                    run("正在配对…", epoch -> api(target, epoch).request("POST", "/v1/pair", body), result -> {
                        target.put("token", result.getString("token")).put("device_id", result.getString("device_id"));
                        if (result.has("expires_at")) target.put("expires_at", result.get("expires_at"));
                        target.put("scope", result.optString("scope", "read_only"));
                        if (result.has("task_ids")) target.put("task_ids", result.get("task_ids"));
                        saved.remove("last_task"); saved.remove("last_task_pin"); saved.remove("selected_task"); resumeTaskId = null;
                        tasks = new JSONArray(); task = null; selectedId = null; offline = true;
                        saved.put("connection", target);
                        if (persist()) { code.setText(""); showTasks(); refresh(); }
                    }));
            } catch (Exception ex) { error(ex); }
        });
        if (connection() != null) button(content, "返回已有连接", true, this::showTasks);
    }

    private JSONObject pairingInfo(String raw) {
        try {
            JSONObject info = new JSONObject(new String(Base64.decode(new URI(raw).getRawFragment(), Base64.URL_SAFE | Base64.NO_WRAP), StandardCharsets.UTF_8));
            if (info.getInt("version") != 1) throw new IllegalArgumentException("不支持此配对版本，请更新手机 App 或重新生成配对信息。");
            Protocol.endpoint(info.getString("endpoint")); Protocol.pin(info.getString("certificate_sha256"));
            if (info.getString("secret").length() < 16 || info.getString("secret").length() > 512) throw new IllegalArgumentException("配对密钥无效，请在电脑重新生成。");
            return info;
        } catch (IllegalArgumentException ex) { throw ex; }
        catch (Exception ex) { throw new IllegalArgumentException("配对资料不完整，请在电脑重新复制整段信息。"); }
    }

    private void showTasks() {
        int position = selectedId == null && !pairingView && !listSignature.isEmpty() ? scroll.getScrollY() : 0;
        clearPage(); selectedId = null; task = null; pairingView = false;
        header.removeAllViews();
        LinearLayout navigation = new LinearLayout(this); navigation.setGravity(Gravity.CENTER_VERTICAL); header.addView(navigation);
        TextView title = text(navigation, "我的任务", 22); title.setLayoutParams(new LinearLayout.LayoutParams(0, -2, 1));
        Button more = button(navigation, "更多", true, this::showDetails); more.setLayoutParams(new LinearLayout.LayoutParams(dp(72), -2));
        renderOperation();
        int visible = 0;
        for (int i = 0; i < tasks.length(); i++) {
            JSONObject item = tasks.optJSONObject(i);
            if (item == null || item.optBoolean("archived")) continue;
            visible++;
            String id = item.optString("id");
            String preview = item.has("message_preview") ? item.optString("message_preview") : item.optString("last_message");
            Button row = button(content, item.optString("title", "未命名任务") + "\n" + stateLabel(item.optString("state")) + "\n" + Protocol.preview(preview), true, () -> {
                selectedId = id; task = item; showTask(); refresh();
            });
            row.setGravity(Gravity.START | Gravity.CENTER_VERTICAL); row.setPadding(dp(12), dp(10), dp(12), dp(10));
            row.setMaxLines(5); row.setEllipsize(TextUtils.TruncateAt.END);
        }
        if (visible == 0) {
            text(content, "暂无获授权的任务。请在电脑选择要连接的任务后重新配对。", 17);
            button(content, "刷新列表", connection() != null, this::refresh);
        }
        listSignature = tasks.toString();
        int bound = generation;
        scroll.post(() -> { if (authorized() && bound == generation) scroll.scrollTo(0, position); });
    }

    private void refresh() { refresh(false); }

    private void refresh(boolean silent) {
        if (!authorized() || connection() == null || busy) return;
        if (connection().optBoolean("needs_pairing")) {
            offline = true; showStatus("授权已失效，请返回任务列表重新配对。未确认操作先在电脑核对。"); updateTaskControls(); return;
        }
        JSONObject target = connection();
        String id = selectedId;
        run("正在读取电脑状态…", epoch -> {
            try { return api(target, epoch).request("GET", id == null ? Protocol.taskListPath() : "/v1/tasks/" + pathId(id), null); }
            catch (HttpApi.ApiError ex) {
                if (id == null && ex.status == 404)
                    throw new IllegalArgumentException("请先更新并重新打开电脑端 Context Relay，再刷新任务。");
                throw ex;
            }
        }, result -> {
            if (id == null) {
                JSONArray listed = result.getJSONArray("tasks");
                for (int i = 0; i < listed.length(); i++) {
                    JSONObject item = listed.optJSONObject(i);
                    if (item == null || !item.optBoolean("summary_only"))
                        throw new IllegalArgumentException("请先更新并重新打开电脑端 Context Relay，再刷新任务。");
                }
                tasks = listed;
                if (!tasks.toString().equals(listSignature)) showTasks();
            }
            else if (id.equals(selectedId)) {
                if (!id.equals(result.optString("id")) || !fullTask(result))
                    throw new IllegalArgumentException("对话未能读取，请刷新或更新电脑端。");
                boolean loading = !fullTask(task);
                task = result;
                if (timeline == null || loading) showTask(); else updateTask();
            }
            offline = false;
            if (!statusPinned) status.setVisibility(View.GONE);
            updateTaskControls();
            if (id != null && originalView && originalCursor == null) readOriginal(null, true, false);
        }, silent);
    }

    private static String stateLabel(String state) {
        switch (state) {
            case "queued": return "待启动"; case "idle": return "空闲（项目未标记完成）";
            case "running": return "执行中"; case "creating": return "正在创建执行会话";
            case "paused": return "已暂停"; case "pausing": return "正在确认暂停";
            case "briefing": return "正在整理简报"; case "reviewing": return "正在阶段审核";
            case "summarizing": return "正在整理交接"; case "verifying": return "正在核验交接";
            case "needs_reconcile": return "结果未知，需要核对"; case "blocked": return "需要处理";
            case "completed": return "已标记完成"; default: return "状态：" + state;
        }
    }

    private void showTask() {
        if (!authorized() || task == null) return;
        pairingView = false;
        clearPage();
        header.removeAllViews();
        LinearLayout navigation = new LinearLayout(this); navigation.setGravity(Gravity.CENTER_VERTICAL);
        header.addView(navigation);
        Button back = button(navigation, "返回", true, () -> { selectedId = null; task = null; showTasks(); refresh(); });
        back.setLayoutParams(new LinearLayout.LayoutParams(dp(72), -2));
        TextView title = text(navigation, task.optString("title", "对话"), 19);
        title.setMaxLines(2); title.setEllipsize(TextUtils.TruncateAt.END);
        title.setLayoutParams(new LinearLayout.LayoutParams(0, -2, 1));
        Button more = button(navigation, "更多", fullTask(task), this::showDetails);
        more.setLayoutParams(new LinearLayout.LayoutParams(dp(72), -2));
        taskStatus = text(header, "", 14); compactStatus(taskStatus);
        requestsAction = button(header, "处理请求", true, this::showRequests);
        requestsAction.setVisibility(View.GONE);
        timeline = column(); content.addView(timeline);
        renderOperation();
        final String id = task.optString("id");
        if (!conversationAvailable(task)) {
            taskStatus.setText("正在读取对话…");
            taskStatus.setVisibility(View.VISIBLE);
            text(timeline, "可先写草稿，读到最新状态后再发送。", 17);
            composer.setPadding(0, dp(8), 0, 0);
            draftInput(id, "可先写草稿；完整任务读取成功后才能发送");
            text(composer, "草稿只保存在手机，不会在详情加载前发送。", 13);
            controls();
            return;
        }
        composer.setPadding(0, dp(8), 0, 0);
        latest = button(composer, "最新消息 ↓", true, () -> {
            if (originalView && originalCursor != null) readOriginal(null, false, true);
            else { scroll.scrollTo(0, Math.max(0, content.getHeight() - scroll.getHeight())); updateLatestButton(); }
        });
        latest.setVisibility(View.GONE);
        draftInput(id, "发送消息给电脑上的 Codex");
        LinearLayout commands = new LinearLayout(this); composer.addView(commands);
        pause = button(commands, "暂停", false, () -> command(task, "pause", new JSONObject(), "暂停任务", "请求电脑暂停；收到原生终态之前不能假定已停止。", null));
        pause.setLayoutParams(new LinearLayout.LayoutParams(0, -2, 1));
        send = button(commands, "发送", false, () -> {
            try {
                String value = Protocol.messageInput(draft(id));
                JSONObject payload = new JSONObject().put("message", value);
                command(task, "start", payload, "发送给电脑", "提交到任务：" + task.optString("title") + "\n" + value, draft(id));
            } catch (Exception ex) { error(ex); }
        });
        send.setLayoutParams(new LinearLayout.LayoutParams(0, -2, 1));
        updateTask();
    }

    private void draftInput(String id, String hint) {
        message = input(composer, hint, draft(id), true);
        message.setMaxLines(4); message.setContentDescription("消息草稿，不会自动发送");
        message.addTextChangedListener(new TextWatcher() {
            public void beforeTextChanged(CharSequence s, int start, int count, int after) { }
            public void onTextChanged(CharSequence s, int start, int before, int count) {
                if (authorized() && drafts != null) { put(drafts, draftKey(id), s.toString()); handler.removeCallbacks(saveDrafts); handler.postDelayed(saveDrafts, 600); }
            }
            public void afterTextChanged(Editable value) { }
        });
    }

    private void updateTaskControls() {
        if (task == null || taskStatus == null || !authorized()) return;
        if (!conversationAvailable(task)) {
            taskStatus.setText("正在读取对话…");
            taskStatus.setVisibility(View.VISIBLE);
            controls();
            return;
        }
        if (!fullTask(task)) {
            if (send != null) { send.setTag(false); send.setText("等待更新"); }
            if (pause != null) { pause.setTag(false); pause.setVisibility(View.GONE); }
            if (requestsAction != null) requestsAction.setVisibility(View.GONE);
            taskStatus.setText("已显示手机保存的对话，正在读取最新状态。读取成功前不会发送或操作。");
            taskStatus.setVisibility(View.VISIBLE);
            compactChat();
            controls();
            return;
        }
        boolean control = controlAllowed(task);
        boolean mutable = pending() == null && !task.optBoolean("archived") && !offline && control;
        boolean waitingBrief = !"direct".equals(task.optString("connection_mode"))
                && task.optBoolean("brief_required") && candidate(task.optJSONObject("brief"));
        send.setTag(mutable && Protocol.quiet(task.optString("state")) && !waitingBrief);
        send.setText(!control ? "仅查看" : "direct".equals(task.optString("connection_mode")) ? "原话发送" : task.optBoolean("brief_required") ? "整理简报" : "发送 / 继续");
        pause.setTag(mutable && (Protocol.active(task.optString("state")) || "blocked".equals(task.optString("state"))));
        pause.setText(!control ? "仅查看" : Protocol.quiet(task.optString("state")) ? "未运行" : "暂停");
        pause.setVisibility(control && Protocol.active(task.optString("state")) ? View.VISIBLE : View.GONE);
        JSONArray requests = task.optJSONArray("pending");
        int requestCount = requests == null ? 0 : requests.length();
        requestsAction.setVisibility(requestCount > 0 ? View.VISIBLE : View.GONE);
        requestsAction.setText("处理请求（" + requestCount + "）");
        String notice = requestCount > 0 ? " · 等待你处理请求" : "";
        if (waitingBrief) notice = " · 简报待核对，请点更多";
        taskStatus.setText((control ? "" : "手机仅查看：请在电脑选择任务，并以“查看与控制”重新配对。\n")
                + (task.optBoolean("archived") ? "任务已归档；请先在电脑取消归档，手机不能继续执行。\n" : "")
                + (originalView && !originalError.isEmpty() ? "原文未更新：" + originalError + "\n" : "")
                + (offline ? "离线快照 · " : "") + stateLabel(task.optString("state")) + " · " + ("workspace-write".equals(task.optString("mode")) ? "项目可写" : "项目只读") + notice
                + (task.optString("error").isEmpty() ? "" : "\n" + task.optString("error")));
        taskStatus.setVisibility(Protocol.taskNoticeNeeded(task.optString("state"), control, offline,
                task.optBoolean("archived") || waitingBrief || requestCount > 0 || !task.optString("error").isEmpty()
                        || originalView && !originalError.isEmpty()) ? View.VISIBLE : View.GONE);
        compactChat();
        controls();
    }

    private static String timeLabel(String value) {
        try { return DateTimeFormatter.ofPattern("MM-dd HH:mm").withZone(ZoneId.systemDefault()).format(OffsetDateTime.parse(value).toInstant()); }
        catch (Exception ignored) { return value == null ? "" : value; }
    }

    private void updateLatestButton() {
        if (latest != null && timeline != null) latest.setVisibility(!keyboardOpen && (scroll.canScrollVertically(1) || originalView && originalCursor != null) ? View.VISIBLE : View.GONE);
    }

    private void compactChat() {
        if (taskStatus == null || message == null) return;
        // Keep the composer and its actions reachable with a keyboard and large system text.
        // Status remains tappable for its full text; reading shortcuts return when typing ends.
        taskStatus.setMaxLines(keyboardOpen ? 1 : 2);
        status.setMaxLines(keyboardOpen ? 1 : 2);
        operation.setMaxLines(keyboardOpen ? 1 : 2);
        message.setMaxLines(keyboardOpen ? 2 : 4);
        JSONArray requests = task == null ? null : task.optJSONArray("pending");
        requestsAction.setVisibility(requests != null && requests.length() > 0 ? View.VISIBLE : View.GONE);
        updateLatestButton();
    }

    private void copyText(String value, String label) {
        if (!authorized()) return;
        ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        if (clipboard == null) { showStatus("系统剪贴板暂不可用，请稍后重试。"); return; }
        ClipData clip = ClipData.newPlainText(label, value);
        PersistableBundle flags = new PersistableBundle(); flags.putBoolean("android.content.extra.IS_SENSITIVE", true);
        clip.getDescription().setExtras(flags);
        clipboard.setPrimaryClip(clip);
        showProgress("已复制" + label + "。粘贴到其他应用前请核对接收方。");
    }

    private void copyMenu(TextView label, String value, String kind) {
        label.setTextIsSelectable(false);
        label.setMinHeight(dp(48));
        label.setGravity(Gravity.CENTER_VERTICAL);
        label.setContentDescription(label.getText() + "，长按复制" + kind);
        label.setOnCreateContextMenuListener((menu, view, info) -> {
            if (authorized()) menu.add("复制" + kind).setOnMenuItemClickListener(item -> {
                copyText(value, kind); return true;
            });
        });
    }

    private CharSequence styledText(String raw) {
        SpannableStringBuilder styled = new SpannableStringBuilder();
        Matcher bold = Pattern.compile("\\*\\*([^*\\n]+)\\*\\*").matcher(raw);
        int copied = 0;
        while (bold.find()) {
            styled.append(raw, copied, bold.start()); int start = styled.length(); styled.append(bold.group(1));
            styled.setSpan(new StyleSpan(Typeface.BOLD), start, styled.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            copied = bold.end();
        }
        styled.append(raw, copied, raw.length());
        Matcher heading = Pattern.compile("(?m)^#{1,3} .+$").matcher(styled.toString());
        while (heading.find()) {
            styled.setSpan(new StyleSpan(Typeface.BOLD), heading.start(), heading.end(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            styled.setSpan(new RelativeSizeSpan(1.12f), heading.start(), heading.end(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        return styled;
    }

    private void messageBody(LinearLayout parent, String value) {
        for (String[] block : Protocol.messageBlocks(value)) {
            if (!"code".equals(block[0])) {
                TextView body = text(parent, "", 17); body.setText(styledText(block[2])); body.setLineSpacing(dp(3), 1);
                continue;
            }
            LinearLayout bar = new LinearLayout(this); bar.setGravity(Gravity.CENTER_VERTICAL); parent.addView(bar);
            String language = block[1].length() > 24 ? block[1].substring(0, 24) + "…" : block[1];
            TextView label = text(bar, language.isEmpty() ? "代码" : "代码 · " + language, 13);
            label.setLayoutParams(new LinearLayout.LayoutParams(0, -2, 1));
            copyMenu(label, block[2], "代码");
            HorizontalScrollView viewport = new HorizontalScrollView(this); viewport.setFillViewport(true);
            TextView code = new TextView(this); code.setText(block[2]); code.setTextSize(15); code.setTypeface(Typeface.MONOSPACE);
            code.setTextColor(Color.rgb(28, 31, 36)); code.setBackgroundColor(Color.rgb(244, 246, 248));
            code.setPadding(dp(10), dp(10), dp(10), dp(10)); code.setTextIsSelectable(true); code.setHorizontallyScrolling(true);
            viewport.addView(code, new android.widget.FrameLayout.LayoutParams(-2, -2));
            parent.addView(viewport, new LinearLayout.LayoutParams(-1, -2));
        }
    }

    private void chatMessage(String role, String value, String at, String notice) {
        chatMessage(role, value, at, notice, false);
    }

    private void chatMessage(String role, String value, String at, String notice, boolean verbatim) {
        boolean mine = "user".equals(role);
        LinearLayout bubble = column();
        LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(-1, -2);
        layout.setMargins(mine ? dp(32) : 0, dp(8), mine ? 0 : dp(8), dp(12));
        bubble.setPadding(dp(12), dp(8), dp(12), dp(8));
        if (mine) { GradientDrawable background = new GradientDrawable(); background.setColor(Color.rgb(238, 242, 246)); background.setCornerRadius(dp(14)); bubble.setBackground(background); }
        timeline.addView(bubble, layout);
        LinearLayout caption = new LinearLayout(this); caption.setGravity(Gravity.CENTER_VERTICAL); bubble.addView(caption);
        TextView label = text(caption, (mine ? "你" : "Codex") + (at.isEmpty() ? "" : " · " + timeLabel(at)), 13);
        label.setLayoutParams(new LinearLayout.LayoutParams(0, -2, 1));
        label.setTextColor(Color.rgb(78, 87, 98));
        copyMenu(label, value, "完整消息");
        if (!notice.isEmpty()) text(bubble, notice, 13);
        if (verbatim) { TextView body = text(bubble, value, 17); body.setLineSpacing(dp(3), 1); }
        else messageBody(bubble, value);
    }

    private void updateTask() {
        if (!authorized() || timeline == null || task == null) return;
        if (!originalChoice && "direct".equals(task.optString("connection_mode"))) originalView = true;
        updateTaskControls();
        if (originalView) {
            if (originalPage == null && !"original-unread".equals(timelineSignature)) {
                clearTimeline(); timelineSignature = "original-unread";
                originalInfo = "原始对话尚未读取；不会用管理摘要替代。";
                text(timeline, "正在读取原始对话…可在“更多”中刷新。", 16);
            }
            return;
        }
        JSONArray messages = task.optJSONArray("messages");
        String signature = (messages == null ? task.optString("last_message") : messages.toString())
                + task.optBoolean("messages_truncated") + task.optString("state") + task.optString("connection_mode");
        if (signature.equals(timelineSignature)) return;
        boolean follow = timelineSignature.isEmpty() || !scroll.canScrollVertically(1);
        int position = scroll.getScrollY();
        timelineSignature = signature;
        clearTimeline();
        if (task.optBoolean("messages_truncated")) text(timeline, "这里只显示最近的对话，较早内容请在电脑查看。", 14);
        int rendered = 0;
        if (messages != null) for (int i = 0; i < messages.length(); i++) {
            JSONObject item = messages.optJSONObject(i);
            if (item == null || !Protocol.visibleMessage(item.optString("role"), item.optString("status"), item.optString("purpose")) || item.optString("text").isEmpty()) continue;
            String notice = item.optBoolean("historical") ? "导入的历史参考" : "";
            if (item.optBoolean("truncated")) notice += (notice.isEmpty() ? "" : "\n") + "内容已截短，完整资料请在电脑查看。";
            chatMessage(item.optString("role"), item.optString("text"), item.optBoolean("historical") ? "" : item.optString("created_at"), notice); rendered++;
        }
        if (rendered == 0 && !task.optString("last_message").isEmpty()) { chatMessage("assistant", task.optString("last_message"), "", ""); rendered++; }
        if (rendered == 0) text(timeline, "对话会显示在这里。输入消息并发送，电脑处理后会自动显示回复。", 17);
        if (Protocol.active(task.optString("state"))) text(timeline, "电脑正在处理…完成后的回复会自动显示。", 15);
        int bound = generation;
        scroll.post(() -> { if (authorized() && bound == generation) { scroll.scrollTo(0, follow ? Math.max(0, content.getHeight() - scroll.getHeight()) : position); updateLatestButton(); } });
    }

    private void clearTimeline() {
        forgetActions(timeline);
        timeline.removeAllViews();
    }

    private void forgetActions(View container) {
        for (int i = actions.size() - 1; i >= 0; i--) {
            android.view.ViewParent parent = actions.get(i).getParent();
            while (parent != null && parent != container) parent = parent.getParent();
            if (parent == container) actions.remove(i);
        }
    }

    private String pageCursor(String key) {
        return originalPage == null || originalPage.isNull(key) ? null : originalPage.optString(key, null);
    }

    private void readOriginal(String cursor, boolean silent, boolean changePage) {
        if (!fullTask(task)) {
            if (!silent) showStatus("完整任务尚未读取，暂不能查看原始对话。");
            return;
        }
        if (!authorized() || !originalView || selectedId == null || connection() == null
                || connection().optBoolean("needs_pairing") || busy || silent && refreshing) return;
        String id = selectedId;
        JSONObject target = connection();
        run("正在只读获取原始对话…", epoch -> {
            try { return api(target, epoch).request("GET", Protocol.conversationPath(id, cursor), null); }
            catch (HttpApi.ApiError ex) {
                if (!Protocol.conversationPageFailure(ex.status, ex.code)) throw ex;
                return new JSONObject().put("conversation_error", ex.getMessage());
            }
        }, page -> {
            if (!originalView || !id.equals(selectedId)) return;
            if (page.has("conversation_error")) {
                originalError = page.getString("conversation_error");
                originalInfo = "原文暂不可读，保留上次内容；可刷新到最新重读。\n" + originalError;
                updateTaskControls();
                return;
            }
            JSONArray entries = page.getJSONArray("entries");
            if (page.optString("thread_id").isEmpty() || entries.length() > 8)
                throw new IllegalStateException("原文页结构不完整，未用摘要替代。");
            int length = 0;
            for (int i = 0; i < entries.length(); i++) {
                JSONObject entry = entries.getJSONObject(i);
                if (!(entry.opt("text") instanceof String)) throw new IllegalStateException("原文文字结构无效。");
                String role = entry.getString("role"), value = entry.getString("text");
                if (!("user".equals(role) || "assistant".equals(role))
                        || entry.optInt("part") < 1 || entry.optInt("parts") < entry.optInt("part"))
                    throw new IllegalStateException("原文消息结构不完整，未显示不明内容。");
                length += value.codePointCount(0, value.length());
            }
            if (length > 6000) throw new IllegalStateException("原文页超过约定大小，未截断显示。");
            boolean follow = originalPage == null || !scroll.canScrollVertically(1);
            int position = scroll.getScrollY();
            originalPage = page; originalCursor = cursor; originalError = "";
            originalInfo = "原始对话 · 来源 " + page.getString("thread_id")
                    + "\n读取时间：" + timeLabel(page.optString("checked_at")) + " · 宿主状态：" + page.optString("status", "unknown")
                    + "\n第 " + page.optInt("page", 1) + " / " + page.optInt("pages", 1) + " 页 · 非文字记录（含附件、工具）" + page.optInt("non_text_items") + " 项未展开"
                    + "\n" + page.optString("notice")
                    + ("direct".equals(page.optString("connection_mode")) ? "" : "\n当前为管理任务；发送仍按管理流程处理，不代表回复来源聊天。");
            String signature = "original:" + page.getString("thread_id") + page.optString("connection_mode") + entries.toString();
            if (!signature.equals(timelineSignature)) {
                timelineSignature = signature; clearTimeline();
                for (int i = 0; i < entries.length(); i++) {
                    JSONObject entry = entries.getJSONObject(i);
                    String detail = Protocol.originalNotice(entry.getInt("part"), entry.getInt("parts"),
                            entry.optString("turn_status", "unknown"), entry.isNull("phase") ? "" : entry.optString("phase"));
                    chatMessage(entry.getString("role"), entry.getString("text"), "", detail, true);
                }
                if (entries.length() == 0) text(timeline, "宿主未返回用户或助手文字。非文字记录不在此页展开。", 16);
            }
            updateTaskControls();
            int bound = generation;
            scroll.post(() -> {
                if (authorized() && bound == generation && originalView && id.equals(selectedId)) {
                    scroll.scrollTo(0, changePage && cursor != null ? 0 : follow || changePage
                            ? Math.max(0, content.getHeight() - scroll.getHeight()) : position);
                    updateLatestButton();
                }
            });
        }, silent);
    }

    private void showRequests() {
        if (!fullTask(task)) { showStatus("完整任务尚未读取，暂不能处理请求。"); return; }
        LinearLayout box = column(); box.setPadding(dp(16), dp(8), dp(16), dp(8));
        renderRequests(box, task, pending() == null && !task.optBoolean("archived") && !offline && controlAllowed(task));
        showPanel("处理本次请求", box);
    }

    private void showDetails() {
        if (task != null && !fullTask(task)) { showStatus("完整任务尚未读取，暂不能打开任务操作。"); return; }
        LinearLayout box = column(); box.setPadding(dp(16), dp(8), dp(16), dp(8));
        if (task == null) {
            text(box, "这里只列出本次配对获授权的任务。请在电脑创建或导入、选择任务后连接手机。", 16);
            if (connection() != null && connection().has("expires_at"))
                text(box, "连接授权到期：" + timeLabel(connection().optString("expires_at")), 14);
            button(box, "刷新任务列表", connection() != null, () -> { dismissDialogs(); refresh(); });
            button(box, "更换电脑 / 重新配对", pending() == null, () -> { dismissDialogs(); showPairing(); });
            if (pending() != null) button(box, "查看待核对操作", true, () -> { dismissDialogs(); showOperationDetails(); });
            showPanel("更多", box);
            return;
        }
        final JSONObject displayed = task;
        button(box, "刷新当前对话", true, () -> { dismissDialogs(); refresh(); });
        button(box, originalView ? "查看管理记录" : "查看原始对话", true, () -> {
            dismissDialogs(); generation++; originalChoice = true; originalView = !originalView;
            timelineSignature = ""; originalPage = null; originalCursor = null; originalError = "";
            updateTask();
            if (originalView) readOriginal(null, false, true);
        });
        if (originalView) {
            text(box, originalInfo.isEmpty() ? "原始对话尚未读取。" : originalInfo, 14);
            JSONArray entries = originalPage == null ? null : originalPage.optJSONArray("entries");
            if (entries != null) for (int i = 0; i < entries.length(); i++) {
                JSONObject entry = entries.optJSONObject(i);
                if (entry == null) continue;
                text(box, "消息 " + (i + 1) + " · " + entry.optString("role") + " · 第 " + entry.optInt("part") + " / " + entry.optInt("parts")
                        + " 段 · " + entry.optString("turn_status", "unknown") + (entry.isNull("phase") ? "" : " · " + entry.optString("phase")), 13);
            }
            LinearLayout pages = new LinearLayout(this); box.addView(pages);
            String older = pageCursor("older_cursor"), newer = pageCursor("newer_cursor");
            Button previous = button(pages, "较早", older != null, () -> { dismissDialogs(); readOriginal(older, false, true); });
            Button next = button(pages, "较新", newer != null, () -> { dismissDialogs(); readOriginal(newer, false, true); });
            Button newest = button(pages, "最新", true, () -> { dismissDialogs(); readOriginal(null, false, true); });
            for (Button item : new Button[] {previous, next, newest}) item.setLayoutParams(new LinearLayout.LayoutParams(0, -2, 1));
        }
        if (pending() != null) button(box, "查看待核对操作", true, () -> { dismissDialogs(); showOperationDetails(); });
        text(box, stateLabel(displayed.optString("state")) + " · " + ("workspace-write".equals(displayed.optString("mode")) ? "项目可写" : "项目只读")
                + " · " + (controlAllowed(displayed) ? "手机可控制" : "手机仅查看"), 14);
        text(box, "长按消息标题可复制完整原文；正文可选中文字复制。代码长行可左右滑动，长按代码标题可复制代码。", 14);
        text(box, "目标\n" + displayed.optString("goal", "待整理简报"), 17);
        text(box, "累计记录 Token：" + pretty(displayed.opt("usage")) + "\n软预算：" + displayed.optLong("max_tokens")
                + " Token / " + displayed.optDouble("max_minutes", 0) + " 分钟（0 为不限；不是当前上下文占用）", 14);
        text(box, "执行时可先写草稿。要追加指令，请先暂停并核对结果。手机不会扩大电脑任务权限。", 15);
        boolean mutable = pending() == null && !displayed.optBoolean("archived") && !offline && controlAllowed(displayed);
        boolean quiet = mutable && Protocol.quiet(displayed.optString("state"));
        if ("blocked".equals(displayed.optString("state")))
            button(box, "暂停任务", mutable, () -> command(displayed, "pause", new JSONObject(), "暂停任务", "请求电脑暂停；以原生终态确认结果。", null));
        button(box, "只读核对恢复", mutable && !Protocol.active(displayed.optString("state")),
                () -> command(displayed, "reconcile", new JSONObject(), "核对恢复", "只读查询已知执行结果。不会自动继续或重试未知操作。", null));
        JSONArray requests = displayed.optJSONArray("pending");
        if (requests != null && requests.length() > 0)
            button(box, "处理请求（" + requests.length() + "）", true, () -> { dismissDialogs(); showRequests(); });
        renderAssessments(box, displayed, quiet);
        showPanel("更多", box);
    }

    private boolean candidate(JSONObject value) { return value != null && "current".equals(value.optString("status")) && "pending".equals(value.optString("decision")); }

    private void renderRequests(LinearLayout parent, JSONObject displayed, boolean mutable) {
        JSONArray requests = displayed.optJSONArray("pending");
        if (requests == null || requests.length() == 0) return;
        text(parent, "待处理请求", 22);
        for (int i = 0; i < requests.length(); i++) {
            JSONObject request = requests.optJSONObject(i);
            if (request == null) continue;
            JSONObject params = request.optJSONObject("params");
            if (params == null) params = new JSONObject();
            String method = request.optString("method");
            text(parent, "请求 " + pretty(request.opt("id")) + "\n" + pretty(params), 16);
            if ("item/commandExecution/requestApproval".equals(method) || "item/fileChange/requestApproval".equals(method)) {
                if (!request.optBoolean("can_approve")) text(parent, "动作证据不完整或不支持，只能拒绝。请在电脑核对。", 17);
                button(parent, "仅允许本次动作", mutable && request.optBoolean("can_approve"), () -> answerApproval(displayed, request, true));
                button(parent, "拒绝本次动作", mutable, () -> answerApproval(displayed, request, false));
            } else if ("item/tool/requestUserInput".equals(method)) {
                final JSONObject questions = params;
                button(parent, "填写本次问题回答", mutable && request.optBoolean("can_approve"), () -> questions(displayed, request, questions));
                if (!request.optBoolean("can_approve")) text(parent, "问题资料不完整或包含秘密字段，不在手机收集。请在电脑处理或暂停。", 16);
            } else text(parent, "手机不支持此类授权，请在电脑处理。", 16);
        }
    }

    private void answerApproval(JSONObject displayed, JSONObject request, boolean allow) {
        try {
            JSONObject payload = new JSONObject().put("pending_request_id", request.get("id"))
                    .put("answer", new JSONObject().put("decision", allow ? "accept" : "decline"));
            command(displayed, "answer", payload, allow ? "允许本次动作" : "拒绝本次动作", pretty(request.opt("params")) + "\n仅对当前请求作出一次决定。", null);
        } catch (Exception ex) { error(ex); }
    }

    private void questions(JSONObject displayed, JSONObject request, JSONObject params) {
        JSONArray list = params.optJSONArray("questions");
        if (list == null || list.length() == 0) { showStatus("问题资料不完整，请在电脑处理。"); return; }
        LinearLayout box = column(); box.setPadding(dp(16), dp(8), dp(16), dp(8));
        JSONObject fields = new JSONObject();
        for (int i = 0; i < list.length(); i++) {
            JSONObject question = list.optJSONObject(i);
            if (question == null || question.optBoolean("isSecret") || question.optString("id").isEmpty() || fields.has(question.optString("id"))) {
                showStatus("不收集密码、密钥或不完整的问题；请在电脑处理。"); return;
            }
            text(box, question.optString("question") + "\n" + pretty(question.opt("options")), 17);
            put(fields, question.optString("id"), input(box, "填写或输入选项文字", "", true));
        }
        ScrollView viewport = new ScrollView(this); viewport.addView(box);
        popup = true;
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle("本次问题回答").setView(viewport)
                .setNegativeButton("取消", null).setPositiveButton("核对并提交", null).create();
        dialog.setOnDismissListener(d -> popup = false);
        showDialog(dialog, true);
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            try {
                JSONObject answers = new JSONObject();
                Iterator<String> ids = fields.keys();
                while (ids.hasNext()) {
                    String id = ids.next(); String value = ((EditText) fields.get(id)).getText().toString();
                    if (value.trim().isEmpty()) throw new IllegalArgumentException("请回答每个问题。");
                    answers.put(id, new JSONObject().put("answers", new JSONArray().put(value)));
                }
                JSONObject payload = new JSONObject().put("pending_request_id", request.get("id")).put("answer", new JSONObject().put("answers", answers));
                dialog.dismiss();
                command(displayed, "answer", payload, "提交本次回答", pretty(answers), null);
            } catch (Exception ex) { error(ex); }
        });
    }

    private void renderAssessments(LinearLayout parent, JSONObject displayed, boolean quiet) {
        if ("direct".equals(displayed.optString("connection_mode"))) {
            text(parent, "原话连接：直接发送你的要求。需要整理、审核或修改时，请在对话输入框说明；这里不生成管理指令。", 16);
            return;
        }
        text(parent, "简报 / 阶段审核", 22);
        text(parent, "生成简报和审核均消耗模型用量。AI 意见不等于测试通过、人工认可或发布验收。采用前由电脑重新核验结果有效性。", 16);
        button(parent, "生成 / 重新整理只读简报", quiet, () -> analyze(displayed, "brief"));
        JSONObject brief = displayed.optJSONObject("brief");
        if (brief != null) {
            text(parent, "简报 · " + brief.optString("decision") + " · 采用前须重验\n" + pretty(brief.opt("report")), 16);
            button(parent, "编辑并采用简报", quiet && candidate(brief), () -> adoptBrief(displayed, brief));
        }
        button(parent, "按需审核阶段成果", quiet && displayed.optInt("work_turns") > 0, () -> analyze(displayed, "review"));
        JSONObject review = displayed.optJSONObject("review");
        if (review != null) {
            text(parent, "AI 审查意见\n" + pretty(review.opt("report")), 16);
            text(parent, "自动化验收：未独立验证；已有测试报告仅作参考。\n人工认可："
                    + ("accepted".equals(review.optString("human_acceptance")) ? "已认可本阶段" : "尚未认可")
                    + "\n发布验收另行完成。结果状态：" + review.optString("status") + "（操作前由电脑重验）", 16);
            JSONObject report = review.optJSONObject("report");
            boolean accept = candidate(review) && report != null && "ready_for_user".equals(report.optString("verdict"));
            button(parent, "人工认可本阶段结果", quiet && accept, () -> command(displayed, "accept_review", new JSONObject(), "人工认可", "记录你对本阶段结果的认可；这不代表正式发布验收。不会自动继续任务。", null));
            button(parent, "按审核意见启动返工", quiet && candidate(review), () -> command(displayed, "revise_from_review", new JSONObject(), "确认启动一轮返工", "将在原授权范围内启动一轮核对和返工，消耗模型用量。未知项应先核对，审核意见不扩大权限。", null));
        }
    }

    private void analyze(JSONObject displayed, String kind) {
        try { command(displayed, "analyze", new JSONObject().put("kind", kind), "启动只读分析", "将使用模型整理" + ("brief".equals(kind) ? "简报" : "阶段审核") + "，消耗时间与用量，不提升任务权限。", null); }
        catch (Exception ex) { error(ex); }
    }

    private void adoptBrief(JSONObject displayed, JSONObject brief) {
        JSONObject report = brief.optJSONObject("report");
        if (report == null) return;
        LinearLayout box = column(); box.setPadding(dp(16), dp(8), dp(16), dp(8));
        text(box, "核对并编辑目标和验收标准。原始授权范围保持不变。", 17);
        EditText goal = input(box, "本轮目标", report.optString("goal"), true);
        JSONArray criteria = report.optJSONArray("acceptance");
        StringBuilder lines = new StringBuilder();
        if (criteria != null) for (int i = 0; i < criteria.length(); i++) lines.append(criteria.optString(i)).append('\n');
        EditText acceptance = input(box, "验收标准，每行一条", lines.toString().trim(), true);
        ScrollView viewport = new ScrollView(this); viewport.addView(box);
        popup = true;
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle("编辑并采用简报").setView(viewport)
                .setNegativeButton("取消", null).setPositiveButton("核对并采用", null).create();
        dialog.setOnDismissListener(d -> popup = false);
        showDialog(dialog, true);
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            try {
                if (goal.getText().toString().trim().isEmpty()) throw new IllegalArgumentException("请填写目标。");
                JSONArray values = new JSONArray();
                for (String line : acceptance.getText().toString().split("\\r?\\n")) if (!line.trim().isEmpty()) values.put(line.trim());
                if (values.length() == 0) throw new IllegalArgumentException("请至少填写一项验收标准。");
                JSONObject payload = new JSONObject().put("goal", goal.getText().toString().trim()).put("acceptance", values);
                dialog.dismiss();
                command(displayed, "adopt_brief", payload, "采用简报", pretty(payload) + "\n只保存你的选择，不自动启动。", null);
            } catch (Exception ex) { error(ex); }
        });
    }

    private void command(JSONObject displayed, String name, JSONObject payload, String title, String description, String sentDraft) {
        if (!authorized()) return;
        if (!fullTask(displayed)) { showStatus("完整任务尚未读取，暂不能发送或操作。请等待详情加载完成。"); return; }
        if (!controlAllowed(displayed)) { showStatus("此手机没有当前任务的控制权限，请在电脑重新授权配对。"); return; }
        if (pending() != null || busy || storageFailed) { showStatus("先查询原操作结果，不能另发一次。"); return; }
        confirm(title, description, () -> {
            try {
                JSONObject body = new JSONObject().put("request_id", UUID.randomUUID().toString()).put("task_id", displayed.getString("id"))
                        .put("command", name).put("expected_etag", displayed.getString("etag")).put("payload", payload);
                JSONObject record = new JSONObject().put("body", body).put("task_title", displayed.optString("title"))
                        .put("sent_draft", sentDraft == null ? JSONObject.NULL : sentDraft);
                saved.put("pending", record);
                if (!persist()) return; // Durable identity must exist before the first byte can be sent.
                JSONObject target = connection();
                renderOperation();
                run("已保存请求编号；正在明确提交…", epoch -> {
                    try { return api(target, epoch).request("POST", "/v1/commands", body); }
                    catch (HttpApi.ApiError ex) {
                        // A definitive HTTP rejection is visible; transport errors remain unresolved and query-only.
                        if (ex.status >= 400 && ex.status < 500) return new JSONObject().put("rejected", true).put("message", ex.getMessage()).put("http_status", ex.status);
                        throw ex;
                    }
                }, result -> {
                    if (result.optBoolean("rejected")) {
                        if (result.optInt("http_status") == 401) expireConnection();
                        record.put("rejected", true).put("rejection", result.optString("message"));
                        persist(); renderCurrent(); statusWithFailure("电脑明确拒绝了提交；草稿已保留。");
                    } else receiveReceipt(result);
                });
            } catch (Exception ex) { error(ex); }
        });
    }

    private void queryOperation() {
        if (!authorized()) return;
        if (connection() == null || connection().optBoolean("needs_pairing")) {
            showStatus("授权已失效，无法核实原请求。请先在电脑核对，再解除等待并重新配对。"); return;
        }
        JSONObject current = pending();
        if (current == null || busy) return;
        JSONObject target = connection();
        JSONObject body = current.optJSONObject("body");
        if (body == null) { showStatus("本地请求记录损坏，不能自动重试。请在电脑核对。"); return; }
        run("只查询原请求结果，不重发…", epoch -> {
            try { return api(target, epoch).request("GET", "/v1/commands/" + pathId(body.getString("request_id")), null); }
            catch (HttpApi.ApiError ex) {
                if (ex.status == 401 || ex.status == 403 || ex.status == 404)
                    return new JSONObject().put("lookup_unknown", true).put("lookup_status", ex.status);
                throw ex;
            }
        }, value -> {
            if (value.optBoolean("lookup_unknown")) {
                if (value.optInt("lookup_status") == 401) expireConnection();
                current.put("lookup_unknown", true);
                current.put("lookup_status", value.optInt("lookup_status"));
                if (persist()) { renderCurrent(); showStatus(value.optInt("lookup_status") == 404
                        ? "电脑查不到原请求。404 不证明未执行，请在电脑核对；不会自动重发。"
                        : "电脑拒绝当前凭据，不能核实原操作结果；请先在电脑核对，再明确解除手机等待或重新配对。"); }
            } else receiveReceipt(value);
        });
    }

    private void receiveReceipt(JSONObject receipt) throws Exception {
        if (!authorized()) return;
        JSONObject current = pending();
        if (current == null) return;
        JSONObject body = current.getJSONObject("body");
        if (!body.getString("request_id").equals(receipt.optString("request_id"))
                || !body.getString("task_id").equals(receipt.optString("task_id"))
                || !body.getString("command").equals(receipt.optString("command"))
                || !Protocol.knownReceipt(receipt.optString("state")))
            throw new IllegalStateException("回执身份或状态不符，保留原请求并停止后续发送。");
        current.put("receipt", receipt);
        current.remove("lookup_unknown");
        current.remove("lookup_status");
        String state = receipt.getString("state");
        if (("accepted".equals(state) || "running".equals(state) || "succeeded".equals(state)) && !current.optBoolean("draft_cleared")) {
            String id = body.getString("task_id");
            if (!current.isNull("sent_draft") && Protocol.clearSentDraft(draft(id), current.getString("sent_draft"))) {
                drafts.put(draftKey(id), "");
                if (id.equals(selectedId) && message != null) message.setText("");
            }
            current.put("draft_cleared", true);
        }
        if (Protocol.autoArchiveReceipt(state)) {
            rememberOperation(current);
            saved.remove("pending");
            if (!persist()) return;
            renderOperation(); updateTaskControls();
            showProgress("电脑已接收操作，正在更新对话…");
            refresh();
            return;
        }
        if (!persist()) return;
        renderOperation();
        statusWithFailure("succeeded".equals(state) ? "电脑已处理本次命令；模型工作是否完成请看任务最新状态。" : "已核对操作回执：" + state);
        updateTaskControls();
    }

    private void renderCurrent() { if (selectedId == null) showTasks(); else showTask(); }
    private void renderOperation() {
        JSONObject current = pending();
        if (current == null) { operation.setText(""); operation.setVisibility(View.GONE); return; }
        operation.setVisibility(View.VISIBLE);
        JSONObject receipt = current.optJSONObject("receipt");
        String state = receipt == null ? "结果尚未确认" : receipt.optString("state");
        String failure = pendingFailure();
        String target = pendingTaskLabel(current) + "\n";
        operation.setText(target + (failure.isEmpty() ? (receipt == null || "unknown".equals(state) || current.optBoolean("lookup_unknown")
                ? "结果尚未确定，点此核对；不会自动重发。" : "请求已提交，正在核对结果；点此查看。") : failure + "\n点此处理。"));
    }

    private void showOperationDetails() {
        if (!authorized() || pending() == null) return;
        JSONObject current = pending();
        JSONObject body = current.optJSONObject("body"), receipt = current.optJSONObject("receipt");
        String state = receipt == null ? "结果尚未确认" : receipt.optString("state");
        LinearLayout box = column(); box.setPadding(dp(16), dp(8), dp(16), dp(8));
        text(box, "任务：" + pendingTaskLabel(current) + "\n本次操作：" + (body == null ? "记录不完整" : body.optString("command")) + " · " + state
                + "\n请求编号：" + (body == null ? "未知" : body.optString("request_id")), 16);
        text(box, "操作回执\n" + (current.optBoolean("rejected") ? current.optString("rejection") : receipt == null
                ? "请求已在本机密封保存。失去连接或退出后只查询原请求，不自动重发。" : pretty(receipt.opt("error"))), 15);
        button(box, "只查询这个请求的结果", !current.optBoolean("rejected") && connection() != null && !connection().optBoolean("needs_pairing"),
                () -> { dismissDialogs(); queryOperation(); });
        boolean terminal = current.optBoolean("rejected") || receipt != null && Protocol.terminalReceipt(receipt.optString("state"));
        button(box, "已读此确定结果，返回任务", terminal, () -> {
            dismissDialogs();
            rememberOperation(current);
            saved.remove("pending");
            if (persist()) { renderCurrent(); refresh(); }
        });
        if (!terminal) {
            text(box, "结果未确定时，请先在电脑核对。找不到回执也不代表未执行；不会自动再次执行。", 17);
            boolean uncertain = receipt == null || "unknown".equals(state) || current.optBoolean("lookup_unknown");
            button(box, "已在电脑核对，解除手机等待", uncertain, () -> confirm("确认已在电脑核对", "只有你已在电脑查看原操作及当前任务后才继续。\n将保留原请求编号和已有回执，再解除手机等待。这不证明操作成功，也不会重发原操作。后续操作仍由电脑状态和版本检查决定。", () -> {
                put(current, "released_after_user_check", true);
                rememberOperation(current);
                saved.remove("pending");
                if (persist()) { renderCurrent(); refresh(); }
            }));
        }
        showPanel("操作回执", box);
    }

    private void rememberOperation(JSONObject record) {
        JSONArray history = saved.optJSONArray("history");
        if (history == null) { history = new JSONArray(); put(saved, "history", history); }
        history.put(record);
    }
}
