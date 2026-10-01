package app.contextrelay.mobile;

import java.net.URI;
import java.util.Locale;

/** Pure rules shared by the UI/transport and host-side checks. */
public final class Protocol {
    private Protocol() {}

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

    public static boolean terminalReceipt(String state) {
        return "succeeded".equals(state) || "failed".equals(state);
    }

    public static boolean knownReceipt(String state) {
        return terminalReceipt(state) || "accepted".equals(state) || "running".equals(state) || "unknown".equals(state);
    }

    public static boolean clearSentDraft(String current, String sent) {
        return current != null && sent != null && current.equals(sent);
    }
}
