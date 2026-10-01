package app.contextrelay.mobile;

import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import javax.net.ssl.X509TrustManager;

/** The explicitly paired leaf is the sole trust anchor; there is no trust-all fallback. */
public final class PinnedTrust implements X509TrustManager {
    private final String expected;
    public PinnedTrust(String fingerprint) { expected = Protocol.pin(fingerprint); }

    public void checkClientTrusted(X509Certificate[] chain, String authType) throws CertificateException {
        throw new CertificateException("Client certificates unsupported");
    }

    public void checkServerTrusted(X509Certificate[] chain, String authType) throws CertificateException {
        if (chain == null || chain.length == 0 || authType == null || authType.isEmpty())
            throw new CertificateException("Missing server certificate");
        chain[0].checkValidity();
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded());
            StringBuilder actual = new StringBuilder();
            for (byte b : digest) actual.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
            if (!MessageDigest.isEqual(expected.getBytes(java.nio.charset.StandardCharsets.US_ASCII),
                    actual.toString().getBytes(java.nio.charset.StandardCharsets.US_ASCII)))
                throw new CertificateException("Computer certificate changed; pair again on the computer");
        } catch (CertificateException ex) { throw ex; }
        catch (Exception ex) { throw new CertificateException("Cannot verify pinned certificate", ex); }
    }

    public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
}
