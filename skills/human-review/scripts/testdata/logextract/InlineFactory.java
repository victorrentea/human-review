package fx;
import org.slf4j.LoggerFactory;
import org.apache.commons.logging.LogFactory;

class InlineFactory {
    void go() {
        LoggerFactory.getLogger(InlineFactory.class).warn("no field, straight off the factory");
        LogFactory.getLog(InlineFactory.class).error("commons inline", new RuntimeException());
    }
}
