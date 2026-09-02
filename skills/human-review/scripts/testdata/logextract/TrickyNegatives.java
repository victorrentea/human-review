package fx;
import java.time.Duration;

public class TrickyNegatives {
    static class Ledger { void log() {} void info(String s) {} }

    void go() {
        double d = Math.log(2.0);                 // NOT a logger
        Duration duration = Duration.ofDays(1);
        duration.toString();
        Ledger log = new Ledger();                // a variable literally named `log`
        log.log();                                // NOT a logger
        log.info("this is a ledger entry");       // NOT a logger
        StringBuilder logger = new StringBuilder();
        logger.append("x");
        java.util.List<String> warn = null;
        warn.size();
    }

    void fluentLookalike(Duration duration) {
        duration.log();                           // NOT a logger
    }
}
