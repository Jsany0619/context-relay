package app.contextrelay.mobile;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import org.json.JSONObject;

/** Credentials, drafts and unresolved requests are encrypted together, never logged/backed up. */
public final class Vault {
    private static final String ALIAS = "context-relay-v1";
    private final AtomicFile file;
    private final SecretKey key;

    public Vault(Context context) throws Exception {
        file = new AtomicFile(new File(context.getNoBackupFilesDir(), "relay.enc"));
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (!store.containsAlias(ALIAS)) {
            if (file.getBaseFile().exists()) throw new IllegalStateException("设备密钥已丢失，不能读取旧连接或请求；请先在电脑核对。");
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setRandomizedEncryptionRequired(true).setKeySize(256).build());
            generator.generateKey();
        }
        key = (SecretKey) store.getKey(ALIAS, null);
    }

    public synchronized JSONObject read() throws Exception {
        if (!file.getBaseFile().exists()) return new JSONObject();
        byte[] packed = file.readFully();
        if (packed.length < 29 || packed[0] != 1) throw new IllegalStateException("本地密封数据损坏，不能自动重置未知请求。");
        byte[] nonce = new byte[12];
        System.arraycopy(packed, 1, nonce, 0, nonce.length);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(128, nonce));
        return new JSONObject(new String(cipher.doFinal(packed, 13, packed.length - 13), StandardCharsets.UTF_8));
    }

    public synchronized void write(JSONObject data) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key);
        byte[] encrypted = cipher.doFinal(data.toString().getBytes(StandardCharsets.UTF_8));
        ByteBuffer packed = ByteBuffer.allocate(13 + encrypted.length);
        packed.put((byte) 1).put(cipher.getIV()).put(encrypted);
        FileOutputStream stream = null;
        try {
            stream = file.startWrite();
            stream.write(packed.array());
            file.finishWrite(stream);
        } catch (Exception ex) { if (stream != null) file.failWrite(stream); throw ex; }
    }
}
