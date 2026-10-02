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
