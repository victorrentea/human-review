import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import java.util.List;

class ArgTypesBase {
    protected Long id;
    public Long getId() { return id; }
}

class ArgTypesPet extends ArgTypesBase {
    private String name;
    private List<String> tags;
}

record ArgTypesOwner(String email, int age) {}

class ArgTypes {
    private static final Logger log = LoggerFactory.getLogger(ArgTypes.class);
    private ArgTypesOwner owner;
    private String o;

    static <T> T pick(T t) { return t; }
    static ArgTypesPet load(int id) { return null; }

    void run(ArgTypesPet pet, List<ArgTypesPet> pets, int count) {
        var fresh = new ArgTypesPet();
        log.info("{} {} {} {}", pet.getId(), pet.getName(), fresh, count);
        for (ArgTypesPet p : pets) {
            log.debug("{} {}", p.getTags().size(), owner.email());
        }
        pets.forEach(o -> log.info("{}", o));
        log.info("{} {} {}", "a" + count, pick(pet), load(3).getId());
        log.info("{} {}", java.time.Instant.now(), unknown.thing());
        try {
            run(pet, pets, 1);
        } catch (IllegalStateException e) {
            log.error("boom {}", e.getMessage(), e);
        }
    }
}
