package fx;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

public class Log4j2Explicit {
    private static final Logger logger = LogManager.getLogger();
    void go(String id) {
        logger.fatal("fatal {}", id);
        logger.atDebug().log("fluent {}", id);
    }
}
