package app.contextrelay.mobile;

public final class SensitiveTextCheck {
    private static int checks;
    private static void check(boolean value) { checks++; if (!value) throw new AssertionError(checks); }
    public static void main(String[] args) {
        for (String value : new String[] {"我的任务", "草稿计划 2026-10-04", "Token：150000 / 5 分钟", "10-04 21:30", "12:34:56", "第 1 / 2 页", "手机仅查看 · 等待继续"})
            check(value.equals(SensitiveText.masked(value)));
        for (String value : new String[] {"https://host.example:8765", "192.0.2.10:8765", "2001:db8::1", "::1", "::ffff:192.0.2.1", "C:\\Users\\example\\repo", "/home/example/repo", "\\\\server.example\\private", "{\"cwd\":\"/an example/path\"}", "{\"device_id\":\"phone-example-id\"}", "{\"thread_id\":\"thread-example\"}", "018d5385-ea81-7000-899a-42f8510b990f"})
            check(SensitiveText.masked(value).contains(SensitiveText.MASK) && !SensitiveText.masked(value).equals(value));
        for (String value : new String[] {"https://host.example:8765/path", "192.0.2.10:8765", "2001:db8::1", "::1", "::ffff:192.0.2.1", "fe80::1%example"})
            check(SensitiveText.MASK.equals(SensitiveText.masked(value)));
        check(("{\"device_id\":" + SensitiveText.MASK + "}").equals(SensitiveText.masked("{\"device_id\":\"phone-example\"}")));
        check(("cwd=" + SensitiveText.MASK).equals(SensitiveText.masked("cwd=/home/example/repo")));
        for (String value : new String[] {"token=top-secret-value", "{\"secret\": \"top-secret-value\"}", "Bearer top-secret-value", "contextrelay://pair#top-secret-value", "-----BEGIN PRIVATE KEY-----top-secret-value-----END PRIVATE KEY-----", "api_key: 'top-secret-value'", "https://user:top-secret-value@host.example/path", "https://host.example?access_token=top-secret-value&mode=read", "{\"refresh_token\":\"top-secret-value\"}", "secret_access_key=top-secret-value", "{\"secret\": [\"a\", {\"nested\":\"top-secret-value\"}]}", "--api-key top-secret-value"})
            check(!SensitiveText.withoutSecrets(value).contains("top-secret-value"));
        for (String value : new String[] {"https://host.example?access%5Ftoken=top-secret-value", "https://host.example?refresh%5Ftoken=top-secret-value", "https://host.example?access_token%3Dtop-secret-value", "[\"--api-key\",\"top-secret-value\"]"})
            check(!SensitiveText.withoutSecrets(value).contains("top-secret-value"));
        for (String value : new String[] {"OPENAI_API_KEY=top-secret-value", "AWS_SECRET_ACCESS_KEY=top-secret-value", "Authorization: Basic top-secret-value", "curl -H 'Authorization: Basic top-secret-value' https://host.example"})
            check(!SensitiveText.withoutSecrets(value).contains("top-secret-value"));
        check(("来源标识：" + SensitiveText.MASK).equals(SensitiveText.masked("来源标识：thread-example")));
        check(("请求编号：" + SensitiveText.MASK).equals(SensitiveText.masked("请求编号：request-example")));
        SensitiveText.Reveal noProof = new SensitiveText.Reveal(8192, "tasks", "safe detail");
        check(!noProof.consume("tasks", true));
        SensitiveText.Reveal once = new SensitiveText.Reveal(8193, "tasks", "safe detail");
        check(once.pause()); check(once.verify(8193, true)); check(once.consume("tasks", true)); check(!once.consume("tasks", true));
        SensitiveText.Reveal oldLogin = new SensitiveText.Reveal(8194, "tasks", "safe detail");
        check(!oldLogin.verify(701, true)); check(!oldLogin.consume("tasks", true));
        SensitiveText.Reveal navigation = new SensitiveText.Reveal(8195, "task-a", "safe detail");
        check(navigation.verify(8195, true)); check(!navigation.consume("task-b", true));
        SensitiveText.Reveal cancel = new SensitiveText.Reveal(8196, "tasks", "safe detail");
        check(!cancel.verify(8196, false)); check(!cancel.consume("tasks", true));
        SensitiveText.Reveal background = new SensitiveText.Reveal(8197, "tasks", "safe detail");
        check(background.pause()); check(!background.pause()); check(!background.verify(8197, true));
        SensitiveText.Reveal afterProof = new SensitiveText.Reveal(8198, "tasks", "safe detail");
        check(afterProof.verify(8198, true)); check(!afterProof.pause()); check(!afterProof.consume("tasks", true));
        SensitiveText.Reveal locked = new SensitiveText.Reveal(8199, "tasks", "safe detail");
        check(locked.verify(8199, true)); check(!locked.consume("tasks", false));
        System.out.println("SensitiveTextCheck: " + checks + " checks passed; metadata only, no Android/device execution");
    }
}
