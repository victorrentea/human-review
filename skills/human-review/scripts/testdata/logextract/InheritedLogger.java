package fx;
import org.apache.commons.logging.Log;

public class InheritedLogger extends SomeSpringBase {
    void go(String bean) {
        logger.debug("Creating instance of bean '" + bean + "'");
    }
}
