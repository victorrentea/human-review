package fx;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
@Slf4j
public class AnnotationOrder {
    void go(String criteria) {
        log.debug("REST request to get Listings by criteria: {}", criteria);
    }
}
