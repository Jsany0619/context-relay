package app.contextrelay.mobile;

import java.io.FileInputStream;
import java.security.MessageDigest;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;

/** Host-side logic/identity checks; they do not claim Android UI/device acceptance. */
public final class ProtocolCheck {
    private static int count;
    private interface Attempt { void run() throws Exception; }
    private static void check(boolean truth) { count++; if (!truth) throw new AssertionError("check " + count); }
    private static void rejects(Attempt action) {
        count++;
        try { action.run(); } catch (Exception expected) { return; }
        throw new AssertionError("Expected rejection " + count);
    }
    public static void main(String[] args) throws Exception {
        check("https://192.168.1.2:9443".equals(Protocol.endpoint("https://192.168.1.2:9443/")));
        check("https://relay.example".equals(Protocol.endpoint("https://relay.example")));
        for (String bad : new String[] {"http://192.168.1.2", "https://u:p@host", "https://host/path", "https://host/?x=1",
                "https://host/#x", "file:///host", "https://host:0", "https://host:70000", "https:///path"})
            rejects(() -> Protocol.endpoint(bad));
        rejects(() -> Protocol.pin("")); rejects(() -> Protocol.pin("0")); rejects(() -> Protocol.pin(null));
        check(Protocol.sameEndpointHost("Relay.Example", "relay.example"));
        check(Protocol.sameEndpointHost("[::1]", "::1"));
        check(!Protocol.sameEndpointHost("192.168.1.2", "attacker.example"));
        check(!Protocol.sameEndpointHost("relay.example", "relay.example.attacker"));
        check(!Protocol.sameEndpointHost(null, "relay.example"));
        check(!Protocol.sameEndpointHost("", ""));
        check(Protocol.quiet("queued")); check(Protocol.quiet("paused")); check(Protocol.quiet("idle"));
        check(!Protocol.quiet("running")); check(!Protocol.quiet("needs_reconcile")); check(!Protocol.quiet("completed"));
        check(Protocol.active("briefing")); check(Protocol.active("reviewing")); check(Protocol.active("pausing"));
        check(Protocol.terminalReceipt("succeeded")); check(Protocol.terminalReceipt("failed"));
        check(!Protocol.terminalReceipt("accepted")); check(!Protocol.terminalReceipt("running")); check(!Protocol.terminalReceipt("unknown"));
        check(Protocol.knownReceipt("unknown")); check(!Protocol.knownReceipt("made_up"));
        check(Protocol.clearSentDraft("old", "old")); check(!Protocol.clearSentDraft("new edit", "old"));
        check(!Protocol.clearSentDraft(null, "old")); check(!Protocol.clearSentDraft("old", null));
        String exact = "  用户原话\n**保留标记**\n```\n  x = 1\n```\n\t🙂 ";
        check(exact.equals(Protocol.messageInput(exact)));
        for (String blank : new String[] {"", " \t\r\n", "\u3000\u00a0"}) rejects(() -> Protocol.messageInput(blank));
        rejects(() -> Protocol.messageInput(null));
        check("/v1/tasks/task_1/conversation".equals(Protocol.conversationPath("task_1", null)));
        check("/v1/tasks/task_1/conversation?cursor=a%2Bb%2F%3D%26%3F%23+%E4%B8%AD".equals(Protocol.conversationPath("task_1", "a+b/=&?# 中")));
        rejects(() -> Protocol.conversationPath("../other", null));
        rejects(() -> Protocol.conversationPath("task_1", ""));
        rejects(() -> Protocol.conversationPath("task_1", new String(new char[181])));
        check(Protocol.validApiPath("GET", Protocol.conversationPath("task_1", "opaque+&?#/..")));
        check(Protocol.validApiPath("GET", "/v1/tasks/task_1"));
        check(Protocol.validApiPath("POST", "/v1/commands"));
        for (String bad : new String[] {"/v1/tasks/task_1?cursor=x", "/v1/tasks/task_1/conversation?x=1",
                "/v1/tasks/task_1/conversation?cursor=", "/v1/tasks/task_1/conversation?cursor=x&extra=y",
                "/v1/tasks/task_1/conversation?cursor=%ZZ", "/v1/tasks/task_1/conversation?cursor=%1",
                "/v1/tasks/task_1/conversation?cursor=x#fragment", "/v1/tasks/../conversation?cursor=x"}) check(!Protocol.validApiPath("GET", bad));
        check(!Protocol.validApiPath("POST", "/v1/tasks/task_1/conversation?cursor=x"));
        check(!Protocol.validApiPath("GET", null));
        check(Protocol.conversationPageFailure(409, "conversation_unavailable"));
        check(Protocol.conversationPageFailure(400, "invalid_cursor"));
        for (int status : new int[] {0, 401, 403, 404, 429, 500, 503})
            check(!Protocol.conversationPageFailure(status, "conversation_unavailable"));
        check(!Protocol.conversationPageFailure(409, "other_conflict"));
        check(!Protocol.conversationPageFailure(400, "invalid_command"));
        check(!Protocol.conversationPageFailure(409, null));
        check(Protocol.failureNotice("failed", false, "任务已达到预算。").startsWith("操作失败：任务已达到预算。"));
        check(Protocol.failureNotice("failed", false, "任务已达到预算。").contains("未自动重发"));
        check(Protocol.failureNotice("failed", false, "任务已达到预算。").contains("保留确定结果"));
        check(Protocol.failureNotice("", true, "此设备仅有只读权限。").startsWith("提交被拒绝：此设备仅有只读权限。"));
        check(!Protocol.failureNotice("failed", false, null).isEmpty());
        check(Protocol.failureNotice("failed", false, " \n ").contains("未提供具体原因"));
        for (String state : new String[] {"unknown", "accepted", "running", "succeeded", ""})
            check(Protocol.failureNotice(state, false, "不得把未知或未完成结果改为失败").isEmpty());
        check(Protocol.authorized(true, true, true, 2, 2));
        check(!Protocol.authorized(false, true, true, 2, 2));
        check(!Protocol.authorized(true, false, true, 2, 2));
        check(!Protocol.authorized(true, true, false, 2, 2));
        check(!Protocol.authorized(true, true, true, 1, 2));
        check(Protocol.autoArchiveReceipt("succeeded"));
        for (String state : new String[] {"accepted", "running", "unknown", "failed", ""}) check(!Protocol.autoArchiveReceipt(state));
        check(Protocol.visibleMessage("user", "completed", "work"));
        check(Protocol.visibleMessage("assistant", "completed", "work"));
        check(!Protocol.visibleMessage("assistant", "in_progress", "work"));
        check(!Protocol.visibleMessage("assistant", "completed", "review"));
        check(!Protocol.visibleMessage("system", "completed", "work"));
        check("contextrelay://pair#YWJj".equals(Protocol.pairingUri("  contextrelay://pair#YWJj  ")));
        for (String value : new String[] {"", "https://computer", "contextrelay://pair#", "contextrelay://pair?secret=x#YWJj",
                "contextrelay://user@pair#YWJj", "contextrelay://pair:12#YWJj", "contextrelay://pair#YWJj%20", "contextrelay://pair#YWJj\nabc"}) rejects(() -> Protocol.pairingUri(value));
        rejects(() -> Protocol.pairingUri(null)); rejects(() -> Protocol.pairingUri(new String(new char[10001]).replace('\0', 'a')));
        check("a b c".equals(Protocol.preview("a\nb  c")));
        check(Protocol.preview(new String(new char[120]).replace('\0', 'a')).length() == 101);
        check(!Protocol.preview(null).isEmpty());
        String raw = "# Result\n**done**\n```python\nprint('hello')\n  exact indent\n```\nplain <script>keep text</script>";
        java.util.List<String[]> blocks = Protocol.messageBlocks(raw);
        check(blocks.size() == 3 && "code".equals(blocks.get(1)[0]));
        check("python".equals(blocks.get(1)[1]));
        check("print('hello')\n  exact indent".equals(blocks.get(1)[2]));
        check(blocks.get(2)[2].contains("<script>keep text</script>"));
        check(Protocol.messageBlocks("```unterminated\nkeep **literal**").get(0)[2].equals("```unterminated\nkeep **literal**"));
        String repeated = new String(new char[100]).replace("\0", "```\nx\n```\n");
        check(Protocol.messageBlocks(repeated).size() <= 22);
        X509Certificate cert;
        try (FileInputStream input = new FileInputStream(args[0])) {
            cert = (X509Certificate) CertificateFactory.getInstance("X.509").generateCertificate(input);
        }
        StringBuilder hex = new StringBuilder();
        for (byte b : MessageDigest.getInstance("SHA-256").digest(cert.getEncoded())) hex.append(String.format("%02x", b & 255));
        PinnedTrust valid = new PinnedTrust(hex.toString());
        valid.checkServerTrusted(new X509Certificate[] {cert}, "RSA"); check(true);
        String wrong = (hex.charAt(0) == '0' ? "1" : "0") + hex.substring(1);
        rejects(() -> new PinnedTrust(wrong).checkServerTrusted(new X509Certificate[] {cert}, "RSA"));
        rejects(() -> valid.checkServerTrusted(new X509Certificate[0], "RSA"));
        rejects(() -> valid.checkServerTrusted(null, "RSA"));
        rejects(() -> valid.checkServerTrusted(new X509Certificate[] {cert}, ""));
        rejects(() -> valid.checkClientTrusted(new X509Certificate[] {cert}, "RSA"));
        check(valid.getAcceptedIssuers().length == 0);
        System.out.println("ProtocolCheck: " + count + " assertions passed (host JVM; no Android runtime/device claim)");
    }
}
