package fx;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class Slf4jExplicit {
    private static final Logger LOG = LoggerFactory.getLogger(Slf4jExplicit.class);
    void run(String owner, int petId) {
        LOG.info("Booking visit for owner {} pet {}", owner, petId);
        LOG.debug("cache miss");
        if (LOG.isTraceEnabled()) {
            LOG.trace("payload={}", owner);
        }
        LOG.error("boom", new IllegalStateException("x"));
    }
}
