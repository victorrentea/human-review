package fx;
import org.apache.commons.logging.Log;
import org.apache.commons.logging.LogFactory;

public class CommonsLogging {
    protected final Log logger = LogFactory.getLog(getClass());
    void go() { logger.info("commons"); logger.error("bad", new RuntimeException()); }
}
