package app.contextrelay.mobile;

import java.util.regex.Pattern;

/** Display-only metadata filtering. Never apply to conversation text or command payloads. */
final class SensitiveText {
    static final String MASK = "••••";
    private static final String SECRET_KEY = "(?:(?:[a-z0-9]+[_-])*(?:token|(?:access|refresh|id|session)[_-]?token|secret(?:[_-]access[_-]key)?|password|passwd|api[_-]?key|authorization|access[_-]?key|private[_-]?key|pairing[_-]?(?:code|secret|token)))";
    private static final String VALUE = "(?:\"(?:\\\\.|[^\"\\\\])*\"|'[^']*'|[^\\s,;}]+)";
    private static final Pattern SECRET = Pattern.compile("(?i)(?<![\\w-])([\"']?" + SECRET_KEY + "[\"']?\\s*[:=]\\s*)");
    private static final Pattern COMMAND_SECRET = Pattern.compile("(?i)(--" + SECRET_KEY + "\\s+)" + VALUE);
    private static final Pattern ARGV_SECRET = Pattern.compile("(?i)([\"']--" + SECRET_KEY + "[\"']\\s*,\\s*)" + VALUE);
    private static final Pattern QUERY_SECRET = Pattern.compile("(?i)([?&](?:token|(?:access|refresh|id|session)(?:_|-|%5f|%2d)?token|secret(?:(?:_|%5f)access(?:_|%5f)key)?|api(?:_|-|%5f|%2d)?key|password|authorization)(?:=|%3d))[^&#\\s\"]*");
    private static final Pattern USERINFO = Pattern.compile("(?i)((?:https?|wss?)://)[^/@\\s]+@");
    private static final Pattern PAIR = Pattern.compile("(?i)contextrelay://pair[^\\s\"<>]*");
    private static final Pattern BEARER = Pattern.compile("(?i)\\b(?:Bearer|Basic)\\s+[^\\s,;\"']+");
    private static final Pattern PRIVATE_KEY = Pattern.compile("(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----");
    private static final Pattern FIELD = Pattern.compile("(?i)([\"']?(?:cwd|path|paths|file|file_path|directory|root|endpoint|host|hostname|ip|id|device[_-]?(?:id|name)|thread[_-]?id|turn[_-]?id|task[_-]?id|request[_-]?id|certificate_sha256|来源标识|请求编号|任务编号|设备标识|设备名称)[\"']?\\s*[:=：]\\s*)" + VALUE);
    private static final Pattern URL = Pattern.compile("(?i)\\b(?:https?|wss?|file)://[^\\s\"<>]+");
    private static final Pattern WINDOWS = Pattern.compile("(?i)(?<![\\p{L}\\d])(?:[a-z]:[\\\\/]|\\\\\\\\)[^\\r\\n\"<>|]*");
    private static final Pattern UNIX = Pattern.compile("(?<![\\w:])(?:/|\\.\\.?/|~/)(?:[\\p{L}\\d._~-]+/)*[\\p{L}\\d._~-]+");
    private static final Pattern IPV4 = Pattern.compile("(?<![\\d.])(?:\\d{1,3}\\.){3}\\d{1,3}(?::\\d+)?(?![\\d.])");
    private static final Pattern IPV6 = Pattern.compile("(?i)(?<![\\w:])(?:(?:[a-f0-9]{0,4}:){2,6}(?:\\d{1,3}\\.){3}\\d{1,3}|(?:[a-f0-9]{1,4}:){1,7}:[a-f0-9:]*|::[a-f0-9:]*|(?:[a-f0-9]{1,4}:){7}[a-f0-9]{1,4})(?:%[\\w]+)?");
    private static final Pattern IDENTIFIER = Pattern.compile("(?i)\\b(?:[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{64}|(?:[a-f0-9]{2}:){31}[a-f0-9]{2})\\b");

    static String withoutSecrets(String value) {
        if (value == null) return "";
        value = PRIVATE_KEY.matcher(value).replaceAll("[凭据已隐藏]");
        value = PAIR.matcher(value).replaceAll("[配对凭据已隐藏]");
        value = BEARER.matcher(value).replaceAll("[凭据已隐藏]");
        value = USERINFO.matcher(value).replaceAll("$1[凭据已隐藏]@");
        value = QUERY_SECRET.matcher(value).replaceAll("$1[凭据已隐藏]");
        value = COMMAND_SECRET.matcher(value).replaceAll("$1[凭据已隐藏]");
        value = ARGV_SECRET.matcher(value).replaceAll("$1[凭据已隐藏]");
        java.util.regex.Matcher match = SECRET.matcher(value);
        StringBuilder safe = new StringBuilder(); int end = 0;
        while (match.find(end)) {
            safe.append(value, end, match.end()).append("[凭据已隐藏]");
            end = valueEnd(value, match.end());
        }
        return safe.append(value, end, value.length()).toString();
    }

    private static int valueEnd(String value, int start) {
        int depth = 0; char quote = 0; boolean escape = false;
        for (int i = start; i < value.length(); i++) {
            char c = value.charAt(i);
            if (escape) { escape = false; continue; }
            if (quote != 0) {
                if (c == '\\') escape = true;
                else if (c == quote) { quote = 0; if (depth == 0) return i + 1; }
            } else if (c == '\"' || c == '\'') quote = c;
            else if (c == '[' || c == '{') depth++;
            else if (c == ']' || c == '}') { if (depth == 0) return i; if (--depth == 0) return i + 1; }
            else if (depth == 0 && (Character.isWhitespace(c) || c == ',' || c == ';' || c == '&' || c == '#')) return i;
        }
        return value.length();
    }

    static String masked(String value) {
        value = withoutSecrets(value);
        value = FIELD.matcher(value).replaceAll("$1" + MASK);
        for (Pattern pattern : new Pattern[] {URL, WINDOWS, UNIX, IPV6, IPV4, IDENTIFIER})
            value = pattern.matcher(value).replaceAll(MASK);
        return value;
    }

    /** One fresh system-credential result, bound to one page and never persisted. */
    static final class Reveal {
        final int code;
        final String page, detail;
        boolean expectedPause = true, paused, verified, consumed;
        Reveal(int code, String page, String detail) { this.code = code; this.page = page; this.detail = detail; }
        boolean pause() {
            if (!consumed && !verified && expectedPause) { expectedPause = false; paused = true; return true; }
            consumed = true; return false;
        }
        boolean verify(int resultCode, boolean approved) {
            if (consumed || verified || resultCode != code || !approved) { consumed = true; return false; }
            verified = true; return true;
        }
        boolean consume(String currentPage, boolean eligible) {
            boolean allowed = !consumed && verified && eligible && page != null && page.equals(currentPage);
            consumed = true; return allowed;
        }
    }
}
