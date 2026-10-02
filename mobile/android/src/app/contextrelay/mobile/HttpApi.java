package app.contextrelay.mobile;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.util.function.BooleanSupplier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import org.json.JSONObject;

public final class HttpApi {
    private final String endpoint;
    private final String token;
    private final SSLContext ssl;
    private final PinnedTrust trust;
    private final String host;
    private final BooleanSupplier permitted;

    public HttpApi(JSONObject connection, BooleanSupplier permitted) throws Exception {
        this.permitted = permitted;
        requireUnlocked();
        endpoint = Protocol.endpoint(connection.getString("endpoint"));
        token = connection.optString("token", "");
        host = new URL(endpoint).getHost();
        trust = new PinnedTrust(connection.getString("certificate_sha256"));
        ssl = SSLContext.getInstance("TLS");
        ssl.init(null, new TrustManager[] { trust }, null);
    }

    private void requireUnlocked() {
        if (!permitted.getAsBoolean()) throw new IllegalStateException("手机已锁定，未继续发送。已发送的请求需重新解锁后核对结果。");
    }

    public JSONObject request(String method, String path, JSONObject body) throws Exception {
        requireUnlocked();
        if (!path.startsWith("/v1/") || path.contains("..") || path.contains("?") || path.contains("#"))
            throw new IllegalArgumentException("Invalid API path");
        HttpsURLConnection connection = (HttpsURLConnection) new URL(endpoint + path).openConnection();
        connection.setSSLSocketFactory(ssl.getSocketFactory());
        // This local certificate is identified by the user's explicit leaf pin, not a public CA/SAN.
        // Permit only the configured endpoint host, and independently check its exact peer certificate.
        connection.setHostnameVerifier((actualHost, session) -> {
            if (!Protocol.sameEndpointHost(host, actualHost)) return false;
            try {
                Certificate[] peers = session.getPeerCertificates();
                if (peers.length == 0 || !(peers[0] instanceof X509Certificate)) return false;
                trust.checkServerTrusted(new X509Certificate[] { (X509Certificate) peers[0] }, "PINNED");
                return true;
            } catch (Exception rejected) { return false; }
        });
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(10000);
        connection.setReadTimeout(15000);
        connection.setUseCaches(false);
        connection.setRequestMethod(method);
        connection.setRequestProperty("Accept", "application/json");
        if (!token.isEmpty()) connection.setRequestProperty("Authorization", "Bearer " + token);
        try {
            if (body != null) {
                byte[] encoded = body.toString().getBytes(StandardCharsets.UTF_8);
                if (encoded.length > 65536) throw new IllegalArgumentException("请求过长，请缩短内容。");
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setFixedLengthStreamingMode(encoded.length);
                // TLS + certificate + hostname verification precede all secret/body transmission.
                requireUnlocked();
                try (OutputStream out = connection.getOutputStream()) { requireUnlocked(); out.write(encoded); }
            }
            requireUnlocked();
            int status = connection.getResponseCode();
            if (status >= 300 && status < 400) throw new IllegalStateException("已拒绝地址重定向；请在电脑重新生成配对信息。");
            InputStream input = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            if (input == null) throw new IllegalStateException("电脑没有返回可读取的结果（HTTP " + status + "）。");
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            try (InputStream source = input) {
                byte[] chunk = new byte[8192];
                int count;
                while ((count = source.read(chunk)) != -1) {
                    if (bytes.size() + count > 2 * 1024 * 1024) throw new IllegalStateException("电脑响应超过大小限制。");
                    bytes.write(chunk, 0, count);
                }
            }
            JSONObject result = new JSONObject(new String(bytes.toByteArray(), StandardCharsets.UTF_8));
            if (status < 200 || status >= 300) {
                JSONObject error = result.optJSONObject("error");
                throw new ApiError(status, error == null ? "http_error" : error.optString("code"),
                        error == null ? "电脑拒绝请求" : error.optString("message", "电脑拒绝请求"));
            }
            return result;
        } finally { connection.disconnect(); }
    }

    public static final class ApiError extends Exception {
        public final int status;
        public final String code;
        public ApiError(int status, String code, String message) {
            super(message + "（" + code + " / HTTP " + status + "）");
            this.status = status;
            this.code = code;
        }
    }
}
