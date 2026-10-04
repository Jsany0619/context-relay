package app.contextrelay.mobile;

/** Host checks for one-shot authorization; no Android UI, device, task or credentials. */
public final class VerifiedScreenshotCheck {
    private static int checks;
    private static void check(boolean value) { checks++; if (!value) throw new AssertionError(checks); }
    private static VerifiedScreenshot.Attempt request(String page) {
        return new VerifiedScreenshot.Attempt(page, VerifiedScreenshot.Attempt.VERIFYING, 16384);
    }
    public static void main(String[] args) {
        VerifiedScreenshot.Attempt unverified = request("task-a");
        check(!unverified.consume("task-a", true));
        check(!unverified.verify());

        VerifiedScreenshot.Attempt current = request("task-a");
        check(current.verify());
        check(!current.verify());
        check(current.consume("task-a", true));
        check(!current.consume("task-a", true));

        VerifiedScreenshot.Attempt changed = request("task-a");
        check(changed.verify());
        check(!changed.consume("task-b", true));
        check(!changed.consume("task-a", true));

        VerifiedScreenshot.Attempt sensitive = request("task-a");
        check(sensitive.verify());
        check(!sensitive.consume("task-a", false));

        VerifiedScreenshot.Attempt unknown = request(null);
        check(unknown.verify());
        check(!unknown.consume(null, true));

        VerifiedScreenshot.Attempt system = request("task-a");
        system.expectedPause = true;
        check(system.pause());
        check(system.paused);
        check(system.verify());
        check(system.consume("task-a", true));

        VerifiedScreenshot.Attempt background = request("task-a");
        check(!background.pause());
        check(!background.verify());

        VerifiedScreenshot.Attempt twice = request("task-a");
        twice.expectedPause = true;
        check(twice.pause());
        check(!twice.pause());
        check(!twice.verify());

        VerifiedScreenshot.Attempt proofThenLeave = request("task-a");
        check(proofThenLeave.verify());
        check(!proofThenLeave.pause());
        check(!proofThenLeave.consume("task-a", true));

        VerifiedScreenshot.Attempt chooser = new VerifiedScreenshot.Attempt("task-a", VerifiedScreenshot.Attempt.CHOOSING, 16386);
        check(!chooser.verify());
        check(chooser.saveRequest != system.saveRequest && chooser.authRequest != system.authRequest);
        System.out.println("VerifiedScreenshotCheck: " + checks + " checks passed; no device/UI execution");
    }
}
