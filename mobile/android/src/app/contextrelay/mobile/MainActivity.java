package app.contextrelay.mobile;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.InputType;
import android.text.TextUtils;
import android.text.TextWatcher;
import android.util.Base64;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONArray;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final List<Button> actions = new ArrayList<>();
    private Vault vault;
    private JSONObject saved;
    private JSONObject drafts;
    private JSONObject task;
    private JSONArray tasks = new JSONArray();
    private TextView status;
    private TextView operation;
    private LinearLayout content;
    private LinearLayout operationPanel;
    private ScrollView scroll;
    private EditText message;
    private boolean busy, foreground, popup, storageFailed, pairingView;
    private int generation;
    private String selectedId;
    private String incomingPair;

    private interface Job { JSONObject run() throws Exception; }
    private interface Result { void accept(JSONObject value) throws Exception; }

    private final Runnable poller = new Runnable() {
        public void run() {
            if (!foreground || isFinishing()) return;
            if (!busy && !popup && !pairingView && connection() != null) {
                if (pending() != null && !pending().optBoolean("rejected")
                        && (pending().optJSONObject("receipt") == null || !Protocol.terminalReceipt(pending().optJSONObject("receipt").optString("state")))) queryOperation();
                else if (pending() == null && !editing()) refresh();
            }
            handler.postDelayed(this, 5000);
        }
    };
    private final Runnable saveDrafts = () -> persist();

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE);
        try {
            vault = new Vault(this);
            saved = vault.read();
            drafts = saved.optJSONObject("drafts");
            if (drafts == null) { drafts = new JSONObject(); saved.put("drafts", drafts); }
        } catch (Exception ex) {
            storageFailed = true;
            LinearLayout box = column();
            text(box, "无法安全读取本机记录", 24);
            text(box, "未自动清空数据或重新发送。请先在电脑核对未完成操作，再处理手机存储。\n" + safeError(ex), 17);
            setContentView(box);
            return;
        }
        LinearLayout root = column();
        root.setPadding(dp(14), dp(12), dp(14), dp(8));
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(dp(14) + insets.getSystemWindowInsetLeft(), dp(8) + insets.getSystemWindowInsetTop(),
                    dp(14) + insets.getSystemWindowInsetRight(), dp(8) + insets.getSystemWindowInsetBottom());
            return insets;
        });
        text(root, "Context Relay", 24);
        status = text(root, "手机操作 · 电脑执行", 15);
        compactStatus(status);
        operation = text(root, "", 14);
        compactStatus(operation);
        operation.setTextColor(Color.rgb(138, 59, 0));
        scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        content = column();
        scroll.addView(content);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(root);
        readIntent(getIntent());
        if (connection() == null || incomingPair != null) showPairing();
        else { showTasks(); refresh(); }
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        readIntent(intent);
        if (!storageFailed && incomingPair != null) showPairing();
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
        if (!storageFailed) { handler.removeCallbacks(poller); handler.postDelayed(poller, 500); }
    }
    @Override protected void onPause() {
        foreground = false;
        handler.removeCallbacks(poller);
        handler.removeCallbacks(saveDrafts);
        if (!storageFailed) persist();
        super.onPause();
    }
    @Override protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        worker.shutdownNow();
        super.onDestroy();
    }
    @Override public void onBackPressed() {
        if (selectedId != null && !busy) { selectedId = null; task = null; showTasks(); refresh(); }
        else super.onBackPressed();
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private LinearLayout column() { LinearLayout box = new LinearLayout(this); box.setOrientation(LinearLayout.VERTICAL); return box; }
    private TextView text(LinearLayout parent, String value, int size) {
        TextView view = new TextView(this);
        view.setText(value); view.setTextSize(size); view.setTextIsSelectable(true);
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
        view.setEnabled(enabled && !busy && !storageFailed);
        view.setTag(enabled);
        view.setOnClickListener(v -> { try { callback.run(); } catch (Exception ex) { error(ex); } });
        parent.addView(view, new LinearLayout.LayoutParams(-1, -2));
        actions.add(view);
        return view;
    }
    private void controls() {
        for (Button item : actions) item.setEnabled(Boolean.TRUE.equals(item.getTag()) && !busy && !storageFailed);
    }
    private void clearPage() { generation++; content.removeAllViews(); actions.clear(); message = null; }
    private JSONObject connection() { return saved == null ? null : saved.optJSONObject("connection"); }
    private JSONObject pending() { return saved == null ? null : saved.optJSONObject("pending"); }
    private boolean editing() { View focus = getCurrentFocus(); return focus instanceof EditText; }
    private String draftKey(String id) { return (connection() == null ? "" : connection().optString("certificate_sha256")) + ":" + id; }
    private String draft(String id) { return drafts.optString(draftKey(id), ""); }
    private void put(JSONObject object, String key, Object value) {
        try { object.put(key, value); } catch (Exception ex) { throw new IllegalStateException(ex); }
    }
    private boolean persist() {
        try { vault.write(saved); return true; }
        catch (Exception ex) { storageFailed = true; if (status != null) status.setText("本机保存失败；已停止发送。请在电脑核对。" + safeError(ex)); controls(); return false; }
    }
    private void error(Exception ex) { if (status != null) status.setText(safeError(ex)); }
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

    private void run(String label, Job job, Result result) {
        if (busy || storageFailed || isFinishing()) return;
        busy = true; controls(); status.setText(label);
        final int bound = generation;
        worker.execute(() -> {
            JSONObject value = null; Exception failure = null;
            try { value = job.run(); } catch (Exception ex) { failure = ex; }
            final JSONObject returned = value; final Exception problem = failure;
            handler.post(() -> {
                if (isDestroyed() || isFinishing()) return;
                busy = false; controls();
                if (problem != null) { error(problem); return; }
                // Navigation cannot happen while a request owns the view; late results never switch tasks.
                if (bound != generation) return;
                try { result.accept(returned); } catch (Exception ex) { error(ex); }
            });
        });
    }

    private void confirm(String title, String detail, Runnable action) {
        popup = true;
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle(title).setMessage(detail)
                .setNegativeButton("取消", null).setPositiveButton("确认本次操作", (d, which) -> action.run()).create();
        dialog.setOnDismissListener(d -> popup = false);
        showDialog(dialog, false);
    }

    private void showDialog(AlertDialog dialog, boolean inputForm) {
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

    private void showPairing() {
        clearPage(); selectedId = null; task = null; pairingView = true;
        text(content, "连接自己的电脑", 22);
        text(content, "在电脑管理器打开手机连接，复制完整配对信息。请通过可信方式传递；配对信息含一次性密钥。仅支持 HTTPS，同一网络或已配置的私有网络。", 16);
        if (pending() != null) {
            text(content, "仍有结果待核对的操作，不能替换连接或丢弃原请求。请返回查询。", 17);
            button(content, "返回原连接", true, () -> { incomingPair = null; showTasks(); });
            return;
        }
        EditText code = input(content, "粘贴 contextrelay://pair#…", incomingPair == null ? "" : incomingPair, true);
        code.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        incomingPair = null;
        button(content, "核对并配对", true, () -> {
            try {
                String raw = code.getText().toString().trim();
                if (raw.length() > 10000) throw new IllegalArgumentException("配对信息过长。");
                URI uri = new URI(raw);
                if (!"contextrelay".equals(uri.getScheme()) || !"pair".equals(uri.getHost())
                        || uri.getRawFragment() == null || uri.getRawQuery() != null || uri.getUserInfo() != null
                        || !(uri.getPath().isEmpty() || "/".equals(uri.getPath())))
                    throw new IllegalArgumentException("不是有效的 Context Relay 配对信息。");
                JSONObject info = new JSONObject(new String(Base64.decode(uri.getRawFragment(), Base64.URL_SAFE | Base64.NO_WRAP), StandardCharsets.UTF_8));
                if (info.getInt("version") != 1) throw new IllegalArgumentException("不支持此配对版本。");
                String endpoint = Protocol.endpoint(info.getString("endpoint"));
                String pin = Protocol.pin(info.getString("certificate_sha256"));
                String secret = info.getString("secret");
                if (secret.length() < 16 || secret.length() > 512) throw new IllegalArgumentException("配对密钥无效。");
                JSONObject target = new JSONObject().put("endpoint", endpoint).put("certificate_sha256", pin);
                JSONObject body = new JSONObject().put("secret", secret).put("device_name", Build.MANUFACTURER + " " + Build.MODEL);
                confirm("确认电脑身份", endpoint + "\n证书 SHA-256：\n" + pin + "\n请与电脑显示的内容核对。", () ->
                    run("正在配对…", () -> new HttpApi(target).request("POST", "/v1/pair", body), result -> {
                        target.put("token", result.getString("token")).put("device_id", result.getString("device_id"));
                        saved.put("connection", target);
                        if (persist()) { code.setText(""); showTasks(); refresh(); }
                    }));
            } catch (Exception ex) { error(ex); }
        });
        if (connection() != null) button(content, "返回已有连接", true, this::showTasks);
    }

    private void showTasks() {
        clearPage(); selectedId = null; task = null; pairingView = false;
        text(content, "电脑已有任务", 22);
        text(content, "在电脑创建或导入任务后，在这里选择。手机不会接管同时运行的官方 Codex 聊天。", 16);
        renderOperation();
        button(content, "刷新任务列表", connection() != null, this::refresh);
        for (int i = 0; i < tasks.length(); i++) {
            JSONObject item = tasks.optJSONObject(i);
            if (item == null || item.optBoolean("archived")) continue;
            String id = item.optString("id");
            button(content, item.optString("title", "未命名任务") + "\n" + stateLabel(item.optString("state")), true, () -> {
                selectedId = id; task = item; showTask(); refresh();
            });
        }
        if (tasks.length() == 0) text(content, "尚无可显示的任务。", 17);
        button(content, "更换电脑 / 重新配对", pending() == null, this::showPairing);
    }

    private void refresh() {
        if (connection() == null || busy) return;
        JSONObject target = connection();
        String id = selectedId;
        run("正在读取电脑状态…", () -> new HttpApi(target).request("GET", id == null ? "/v1/tasks" : "/v1/tasks/" + pathId(id), null), result -> {
            if (id == null) { tasks = result.getJSONArray("tasks"); showTasks(); }
            else if (id.equals(selectedId)) { task = result; showTask(); }
            status.setText("已从电脑更新 · 草稿只在明确发送后提交");
        });
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
        if (task == null) return;
        pairingView = false;
        int position = scroll.getScrollY();
        clearPage();
        final JSONObject displayed = task;
        final String id = displayed.optString("id");
        text(content, displayed.optString("title"), 22);
        text(content, stateLabel(displayed.optString("state")) + " · " + ("workspace-write".equals(displayed.optString("mode")) ? "项目可写" : "只读"), 17);
        text(content, "目标\n" + displayed.optString("goal", "待整理简报"), 17);
        text(content, "累计记录 Token：" + pretty(displayed.opt("usage")) + "\n软预算：" + displayed.optLong("max_tokens")
                + " Token / " + displayed.optDouble("max_minutes", 0) + " 分钟（0 为不限；不是当前上下文占用）", 14);
        if (!displayed.optString("error").isEmpty()) text(content, "需要注意\n" + displayed.optString("error"), 17);
        text(content, "最新回复\n" + displayed.optString("last_message", "暂无"), 18);
        renderOperation();
        button(content, "刷新 / 核对最新显示", true, this::refresh);
        message = input(content, "补充指令或回复草稿（不会自动发送）", draft(id), true);
        message.addTextChangedListener(new TextWatcher() {
            public void beforeTextChanged(CharSequence s, int start, int count, int after) { }
            public void onTextChanged(CharSequence s, int start, int before, int count) {
                put(drafts, draftKey(id), s.toString()); handler.removeCallbacks(saveDrafts); handler.postDelayed(saveDrafts, 600);
            }
            public void afterTextChanged(Editable value) { }
        });
        boolean mutable = pending() == null && !displayed.optBoolean("archived");
        boolean quiet = mutable && Protocol.quiet(displayed.optString("state"));
        boolean waitingBrief = displayed.optBoolean("brief_required") && candidate(displayed.optJSONObject("brief"));
        text(content, "执行中不能追加启动：可以保留草稿，先暂停并确认结果，再明确继续。手机端不修改任务的权限范围。", 15);
        button(content, displayed.optBoolean("brief_required") ? "开始只读整理简报（耗模型用量）" : "明确发送 / 启动或继续", quiet && !waitingBrief, () -> {
            try {
                String value = draft(id).trim();
                JSONObject payload = new JSONObject().put("message", value.isEmpty() ? JSONObject.NULL : value);
                command(displayed, "start", payload, "启动 / 继续", "提交到电脑任务：" + displayed.optString("title") + "\n" + (value.isEmpty() ? "继续已记录目标" : value), draft(id));
            } catch (Exception ex) { error(ex); }
        });
        button(content, "暂停并核对在途结果", mutable && (Protocol.active(displayed.optString("state")) || "blocked".equals(displayed.optString("state"))),
                () -> command(displayed, "pause", new JSONObject(), "暂停任务", "请求电脑暂停；收到原生终态之前不能假定已停止。", null));
        button(content, "只读核对恢复", mutable && !Protocol.active(displayed.optString("state")),
                () -> command(displayed, "reconcile", new JSONObject(), "核对恢复", "只读查询已知执行结果。不会自动继续或重试未知操作。", null));
        renderRequests(displayed, mutable);
        renderAssessments(displayed, quiet);
        button(content, "返回任务列表", true, () -> { selectedId = null; task = null; showTasks(); refresh(); });
        scroll.post(() -> scroll.scrollTo(0, position));
    }

    private boolean candidate(JSONObject value) { return value != null && "current".equals(value.optString("status")) && "pending".equals(value.optString("decision")); }

    private void renderRequests(JSONObject displayed, boolean mutable) {
        JSONArray requests = displayed.optJSONArray("pending");
        if (requests == null || requests.length() == 0) return;
        text(content, "待处理请求", 22);
        for (int i = 0; i < requests.length(); i++) {
            JSONObject request = requests.optJSONObject(i);
            if (request == null) continue;
            JSONObject params = request.optJSONObject("params");
            if (params == null) params = new JSONObject();
            String method = request.optString("method");
            text(content, "请求 " + pretty(request.opt("id")) + "\n" + pretty(params), 16);
            if ("item/commandExecution/requestApproval".equals(method) || "item/fileChange/requestApproval".equals(method)) {
                if (!request.optBoolean("can_approve")) text(content, "动作证据不完整或不支持，只能拒绝。请在电脑核对。", 17);
                button(content, "仅允许本次动作", mutable && request.optBoolean("can_approve"), () -> answerApproval(displayed, request, true));
                button(content, "拒绝本次动作", mutable, () -> answerApproval(displayed, request, false));
            } else if ("item/tool/requestUserInput".equals(method)) {
                final JSONObject questions = params;
                button(content, "填写本次问题回答", mutable && request.optBoolean("can_approve"), () -> questions(displayed, request, questions));
                if (!request.optBoolean("can_approve")) text(content, "问题资料不完整或包含秘密字段，不在手机收集。请在电脑处理或暂停。", 16);
            } else text(content, "手机不支持此类授权，请在电脑处理。", 16);
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
        if (list == null || list.length() == 0) { status.setText("问题资料不完整，请在电脑处理。"); return; }
        LinearLayout box = column(); box.setPadding(dp(16), dp(8), dp(16), dp(8));
        JSONObject fields = new JSONObject();
        for (int i = 0; i < list.length(); i++) {
            JSONObject question = list.optJSONObject(i);
            if (question == null || question.optBoolean("isSecret") || question.optString("id").isEmpty() || fields.has(question.optString("id"))) {
                status.setText("不收集密码、密钥或不完整的问题；请在电脑处理。"); return;
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
                    String id = ids.next(); String value = ((EditText) fields.get(id)).getText().toString().trim();
                    if (value.isEmpty()) throw new IllegalArgumentException("请回答每个问题。");
                    answers.put(id, new JSONObject().put("answers", new JSONArray().put(value)));
                }
                JSONObject payload = new JSONObject().put("pending_request_id", request.get("id")).put("answer", new JSONObject().put("answers", answers));
                dialog.dismiss();
                command(displayed, "answer", payload, "提交本次回答", pretty(answers), null);
            } catch (Exception ex) { error(ex); }
        });
    }

    private void renderAssessments(JSONObject displayed, boolean quiet) {
        text(content, "简报 / 阶段审核", 22);
        text(content, "生成简报和审核均消耗模型用量。AI 意见不等于测试通过、人工认可或发布验收。采用前由电脑重新核验结果有效性。", 16);
        button(content, "生成 / 重新整理只读简报", quiet, () -> analyze(displayed, "brief"));
        JSONObject brief = displayed.optJSONObject("brief");
        if (brief != null) {
            text(content, "简报 · " + brief.optString("decision") + " · 采用前须重验\n" + pretty(brief.opt("report")), 16);
            button(content, "编辑并采用简报", quiet && candidate(brief), () -> adoptBrief(displayed, brief));
        }
        button(content, "按需审核阶段成果", quiet && displayed.optInt("work_turns") > 0, () -> analyze(displayed, "review"));
        JSONObject review = displayed.optJSONObject("review");
        if (review != null) {
            text(content, "AI 审查意见\n" + pretty(review.opt("report")), 16);
            text(content, "自动化验收：未独立验证；已有测试报告仅作参考。\n人工认可："
                    + ("accepted".equals(review.optString("human_acceptance")) ? "已认可本阶段" : "尚未认可")
                    + "\n发布验收另行完成。结果状态：" + review.optString("status") + "（操作前由电脑重验）", 16);
            JSONObject report = review.optJSONObject("report");
            boolean accept = candidate(review) && report != null && "ready_for_user".equals(report.optString("verdict"));
            button(content, "人工认可本阶段结果", quiet && accept, () -> command(displayed, "accept_review", new JSONObject(), "人工认可", "记录你对本阶段结果的认可；这不代表正式发布验收。不会自动继续任务。", null));
            button(content, "按审核意见启动返工", quiet && candidate(review), () -> command(displayed, "revise_from_review", new JSONObject(), "确认启动一轮返工", "将在原授权范围内启动一轮核对和返工，消耗模型用量。未知项应先核对，审核意见不扩大权限。", null));
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
        if (pending() != null || busy || storageFailed) { status.setText("先查询原操作结果，不能另发一次。"); return; }
        confirm(title, description, () -> {
            try {
                JSONObject body = new JSONObject().put("request_id", UUID.randomUUID().toString()).put("task_id", displayed.getString("id"))
                        .put("command", name).put("expected_etag", displayed.getString("etag")).put("payload", payload);
                JSONObject record = new JSONObject().put("body", body).put("sent_draft", sentDraft == null ? JSONObject.NULL : sentDraft);
                saved.put("pending", record);
                if (!persist()) return; // Durable identity must exist before the first byte can be sent.
                JSONObject target = connection();
                renderOperation();
                run("已保存请求编号；正在明确提交…", () -> {
                    try { return new HttpApi(target).request("POST", "/v1/commands", body); }
                    catch (HttpApi.ApiError ex) {
                        // A definitive HTTP rejection is visible; transport errors remain unresolved and query-only.
                        if (ex.status >= 400 && ex.status < 500) return new JSONObject().put("rejected", true).put("message", ex.getMessage());
                        throw ex;
                    }
                }, result -> {
                    if (result.optBoolean("rejected")) {
                        record.put("rejected", true).put("rejection", result.optString("message"));
                        persist(); renderCurrent(); status.setText("电脑明确拒绝了提交；草稿已保留。");
                    } else receiveReceipt(result);
                });
            } catch (Exception ex) { error(ex); }
        });
    }

    private void queryOperation() {
        JSONObject current = pending();
        if (current == null || busy) return;
        JSONObject target = connection();
        JSONObject body = current.optJSONObject("body");
        if (body == null) { status.setText("本地请求记录损坏，不能自动重试。请在电脑核对。"); return; }
        run("只查询原请求结果，不重发…", () -> {
            try { return new HttpApi(target).request("GET", "/v1/commands/" + pathId(body.getString("request_id")), null); }
            catch (HttpApi.ApiError ex) {
                if (ex.status == 401 || ex.status == 403 || ex.status == 404)
                    return new JSONObject().put("lookup_unknown", true).put("lookup_status", ex.status);
                throw ex;
            }
        }, value -> {
            if (value.optBoolean("lookup_unknown")) {
                current.put("lookup_unknown", true);
                current.put("lookup_status", value.optInt("lookup_status"));
                if (persist()) { renderCurrent(); status.setText(value.optInt("lookup_status") == 404
                        ? "电脑查不到原请求。404 不证明未执行，请在电脑核对；不会自动重发。"
                        : "电脑拒绝当前凭据，不能核实原操作结果；请先在电脑核对，再明确解除手机等待或重新配对。"); }
            } else receiveReceipt(value);
        });
    }

    private void receiveReceipt(JSONObject receipt) throws Exception {
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
        if (!persist()) return;
        renderOperation();
        status.setText("succeeded".equals(state) ? "电脑已处理本次命令；模型工作是否完成请看任务最新状态。" : "已核对操作回执：" + state);
        if (Protocol.terminalReceipt(state)) renderCurrent();
    }

    private void renderCurrent() { if (selectedId == null) showTasks(); else showTask(); }
    private void renderOperation() {
        if (operationPanel != null) {
            for (int i = actions.size() - 1; i >= 0; i--) if (actions.get(i).getParent() == operationPanel) actions.remove(i);
            content.removeView(operationPanel);
        }
        operationPanel = column();
        content.addView(operationPanel, 0);
        JSONObject current = pending();
        if (current == null) { operation.setText(""); return; }
        JSONObject body = current.optJSONObject("body");
        JSONObject receipt = current.optJSONObject("receipt");
        String state = receipt == null ? "结果尚未确认" : receipt.optString("state");
        operation.setText("本次操作：" + (body == null ? "记录不完整" : body.optString("command")) + " · " + state
                + "\n请求编号：" + (body == null ? "未知" : body.optString("request_id")));
        text(operationPanel, "操作回执\n" + (current.optBoolean("rejected") ? current.optString("rejection") : receipt == null
                ? "请求已在本机密封保存。失去连接或退出后只查询原请求，不自动重发。" : pretty(receipt.opt("error"))), 15);
        button(operationPanel, "只查询这个请求的结果", !current.optBoolean("rejected"), this::queryOperation);
        boolean terminal = current.optBoolean("rejected") || receipt != null && Protocol.terminalReceipt(receipt.optString("state"));
        button(operationPanel, "已读此确定结果，返回任务", terminal, () -> {
            rememberOperation(current);
            saved.remove("pending");
            if (persist()) { renderCurrent(); refresh(); }
        });
        if (!terminal) {
            text(operationPanel, "结果未确定时，请先在电脑核对。找不到回执也不代表未执行；不会自动再次执行。", 17);
            boolean uncertain = receipt == null || "unknown".equals(state) || current.optBoolean("lookup_unknown");
            button(operationPanel, "已在电脑核对，解除手机等待", uncertain, () -> confirm("确认已在电脑核对", "只有你已在电脑查看原操作及当前任务后才继续。\n将保留原请求编号和已有回执，再解除手机等待。这不证明操作成功，也不会重发原操作。后续操作仍由电脑状态和版本检查决定。", () -> {
                put(current, "released_after_user_check", true);
                rememberOperation(current);
                saved.remove("pending");
                if (persist()) { renderCurrent(); refresh(); }
            }));
        }
    }

    private void rememberOperation(JSONObject record) {
        JSONArray history = saved.optJSONArray("history");
        if (history == null) { history = new JSONArray(); put(saved, "history", history); }
        history.put(record);
    }
}
