package app.contextrelay.mobile;

import java.net.URI;
import java.net.URLEncoder;
import java.util.Locale;
import java.util.ArrayList;
import java.util.List;

/** Pure rules shared by the UI/transport and host-side checks. */
public final class Protocol {
    private Protocol() {}

    /** Only local appearance values belong in the separate preferences store. */
    public static String appearancePreference(String key, Object value) {
        if ("theme".equals(key)) return "mint".equals(value) ? "mint" : "blue";
        if ("font_size".equals(key)) return "large".equals(value) ? "large" : "standard";
        if ("density".equals(key)) return "compact".equals(value) ? "compact" : "comfortable";
        throw new IllegalArgumentException("不支持的外观设置。");
    }

    public static float appearanceFontScale(Object value) {
        return "large".equals(value) ? 1.15f : 1f;
    }

    public static String endpoint(String value) {
        try {
            URI uri = new URI(value);
            if (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null
                    || uri.getRawQuery() != null || uri.getRawFragment() != null
                    || !(uri.getPath().isEmpty() || "/".equals(uri.getPath()))
                    || uri.getPort() == 0 || uri.getPort() > 65535)
                throw new IllegalArgumentException();
            return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
        } catch (Exception ex) {
            throw new IllegalArgumentException("电脑地址必须是无账号、路径或参数的 HTTPS 地址。");
        }
    }

    public static String pin(String value) {
        if (value == null || !value.matches("[a-fA-F0-9]{64}"))
            throw new IllegalArgumentException("电脑证书指纹无效，请重新复制配对信息。");
        return value.toLowerCase(Locale.ROOT);
    }

    public static boolean sameEndpointHost(String expected, String actual) {
        if (expected == null || actual == null || expected.isEmpty() || actual.isEmpty()) return false;
        String first = expected.replace("[", "").replace("]", "").toLowerCase(Locale.ROOT);
        String second = actual.replace("[", "").replace("]", "").toLowerCase(Locale.ROOT);
        return first.equals(second);
    }

    public static boolean quiet(String state) {
        return "queued".equals(state) || "idle".equals(state) || "paused".equals(state);
    }

    public static boolean active(String state) {
        return "creating".equals(state) || "running".equals(state) || "pausing".equals(state)
                || "summarizing".equals(state) || "verifying".equals(state)
                || "briefing".equals(state) || "reviewing".equals(state);
    }

    public static boolean taskNoticeNeeded(String state, boolean offline, boolean attention) {
        return offline || attention || !quiet(state);
    }

    public static String originalNotice(int part, int parts, String state, String phase) {
        List<String> notices = new ArrayList<>();
        if (parts > 1) notices.add("第 " + part + " / " + parts + " 段");
        if (!"completed".equals(state)) notices.add("轮次状态：" + state);
        if (phase != null && !phase.isEmpty() && !"final".equals(phase) && !"final_answer".equals(phase)) notices.add("阶段：" + phase);
        return String.join(" · ", notices);
    }

    public static boolean terminalReceipt(String state) {
        return "succeeded".equals(state) || "failed".equals(state);
    }

    public static boolean knownReceipt(String state) {
        return terminalReceipt(state) || "accepted".equals(state) || "running".equals(state) || "unknown".equals(state);
    }

    public static boolean clearSentDraft(String current, String sent) {
        return current != null && sent != null && current.equals(sent);
    }

    public static String messageInput(String value) {
        if (value == null || value.codePoints().allMatch(c -> Character.isWhitespace(c) || Character.isSpaceChar(c)))
            throw new IllegalArgumentException("请先输入要发送的消息；空白内容不会发送。");
        return value;
    }

    public static String taskListPath() { return "/v1/tasks?summary=1"; }

    /** Persisted JSON null and malformed values mean no selected task. */
    public static String restoredTaskId(Object value) {
        if (!(value instanceof String)) return null;
        String taskId = (String) value;
        return !"null".equals(taskId) && taskId.matches("[A-Za-z0-9_-]{1,128}") ? taskId : null;
    }

    /** Summary rows are navigation only; commands require a versioned detail response. */
    public static boolean fullTask(boolean summaryOnly, String etag) {
        return !summaryOnly && etag != null && etag.matches("[a-fA-F0-9]{64}");
    }

    /** A sealed local snapshot may be shown, but never satisfies fullTask for actions. */
    public static boolean conversationSnapshot(boolean summaryOnly, boolean cachedConversation,
                                               boolean hasMessages, boolean hasLastMessage) {
        return !summaryOnly && (cachedConversation || hasMessages || hasLastMessage);
    }

    public static String conversationPath(String taskId, String cursor) {
        if (taskId == null || !taskId.matches("[A-Za-z0-9_-]{1,128}")) throw new IllegalArgumentException("任务编号无效。");
        String path = "/v1/tasks/" + taskId + "/conversation";
        if (cursor == null) return path;
        if (cursor.isEmpty() || cursor.length() > 180) throw new IllegalArgumentException("原文页码无效，请刷新到最新。");
        try { return path + "?cursor=" + URLEncoder.encode(cursor, "UTF-8"); }
        catch (java.io.UnsupportedEncodingException impossible) { throw new IllegalStateException(impossible); }
    }

    public static boolean conversationPageFailure(int status, String code) {
        return status == 409 && "conversation_unavailable".equals(code)
                || status == 400 && "invalid_cursor".equals(code);
    }

    public static String failureNotice(String state, boolean rejected, String reason) {
        if (!rejected && !"failed".equals(state)) return "";
        String detail = reason == null || reason.trim().isEmpty() ? "电脑未提供具体原因，请查看完整回执。" : reason.trim();
        return (rejected ? "提交被拒绝：" : "操作失败：") + detail
                + "\n未自动重发；已保留确定结果，请核对后返回任务。";
    }

    public static boolean validApiPath(String method, String path) {
        if (path == null || !path.startsWith("/v1/") || path.contains("#")) return false;
        int query = path.indexOf('?');
        String route = query < 0 ? path : path.substring(0, query);
        if (route.contains("..")) return false;
        if (query < 0) return true;
        if ("GET".equals(method) && "/v1/tasks".equals(route)) return "/v1/tasks?summary=1".equals(path);
        if (!"GET".equals(method) || !route.matches("/v1/tasks/[A-Za-z0-9_-]{1,128}/conversation")) return false;
        String parameters = path.substring(query + 1);
        if (!parameters.startsWith("cursor=") || parameters.length() <= 7 || parameters.length() > 2167) return false;
        for (int i = 7; i < parameters.length(); i++) {
            char c = parameters.charAt(i);
            if (c == '%') {
                if (i + 2 >= parameters.length() || Character.digit(parameters.charAt(i + 1), 16) < 0
                        || Character.digit(parameters.charAt(i + 2), 16) < 0) return false;
                i += 2;
            } else if (!(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || "._*~+-".indexOf(c) >= 0)) return false;
        }
        return true;
    }

    public static boolean authorized(boolean unlocked, boolean foreground, boolean secure, int requestEpoch, int currentEpoch) {
        return unlocked && foreground && secure && requestEpoch == currentEpoch;
    }

    public static boolean autoArchiveReceipt(String state) { return "succeeded".equals(state); }

    public static boolean visibleMessage(String role, String status, String purpose) {
        return ("user".equals(role) || "assistant".equals(role)) && "completed".equals(status) && "work".equals(purpose);
    }

    public static String pairingUri(CharSequence value) {
        if (value == null || value.length() == 0) throw new IllegalArgumentException("请先复制电脑上的配对信息，再粘贴到这里。");
        if (value.length() > 10000) throw new IllegalArgumentException("配对信息过长，请重新复制电脑生成的完整配对信息。");
        String raw = value.toString().trim();
        try {
            URI uri = new URI(raw);
            if (!"contextrelay".equals(uri.getScheme()) || !"pair".equals(uri.getHost())
                    || uri.getPort() != -1 || uri.getRawQuery() != null || uri.getUserInfo() != null
                    || !(uri.getPath().isEmpty() || "/".equals(uri.getPath()))
                    || uri.getRawFragment() == null || !uri.getRawFragment().matches("[A-Za-z0-9_-]+={0,2}")) throw new IllegalArgumentException();
            return raw;
        } catch (Exception ex) { throw new IllegalArgumentException("这不是完整配对信息。请复制以 contextrelay://pair# 开头的一整段文字。"); }
    }

    public static String preview(String value) {
        if (value == null || value.trim().isEmpty()) return "尚无回复，打开后可发送消息。";
        String line = value.replaceAll("\\s+", " ").trim();
        return line.codePointCount(0, line.length()) > 100 ? line.substring(0, line.offsetByCodePoints(0, 100)) + "…" : line;
    }

    /** Only closed triple-backtick fences are styled; everything else stays selectable text. */
    public static List<String[]> messageBlocks(String value) {
        List<String[]> blocks = new ArrayList<>();
        String[] lines = value.split("\\n", -1);
        StringBuilder plain = new StringBuilder();
        boolean fences = true;
        for (int i = 0; i < lines.length; i++) {
            String opening = lines[i].trim();
            int end = i + 1;
            if (fences && opening.startsWith("```") && !opening.substring(3).contains("`") && blocks.size() < 20) {
                while (end < lines.length && !"```".equals(lines[end].trim())) end++;
                if (end < lines.length) {
                    if (plain.length() > 0) { blocks.add(new String[] {"text", "", plain.toString()}); plain.setLength(0); }
                    StringBuilder code = new StringBuilder();
                    for (int row = i + 1; row < end; row++) { if (row > i + 1) code.append('\n'); code.append(lines[row]); }
                    blocks.add(new String[] {"code", opening.substring(3).trim(), code.toString()});
                    i = end; continue;
                }
                fences = false;
            }
            if (plain.length() > 0) plain.append('\n');
            plain.append(lines[i]);
        }
        if (plain.length() > 0 || blocks.isEmpty()) blocks.add(new String[] {"text", "", plain.toString()});
        return blocks;
    }
}
