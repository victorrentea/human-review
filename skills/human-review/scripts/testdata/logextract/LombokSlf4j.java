package fx;
import lombok.extern.slf4j.Slf4j;

@Slf4j
public class LombokSlf4j {
    void handle(Exception e) {
        log.warn("Validation failed: {}", e.getMessage());
        log.error("An unexpected error occurred: {}", e.getMessage(), e);
        this.log.info("done");
    }
}
