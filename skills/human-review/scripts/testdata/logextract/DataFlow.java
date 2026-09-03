package fixtures;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class DataFlow {
    private static final Logger LOG = LoggerFactory.getLogger(DataFlow.class);
    private static final String APP = "petclinic";
    private String tenant;

    void chained(Owner owner, int petId) {
        var customer = owner.getCustomer();
        String name = customer.name;
        LOG.info("owner {} in {} for {}", name, APP, tenant);
    }

    void reassigned(Vet vet) {
        String who = vet.name;
        who = vet.nickname;
        LOG.warn("vet {}", who);
    }

    void loopVar(Owner owner) {
        for (Pet p : owner.pets) {
            LOG.debug("pet {}", p);
        }
    }

    void deep(Owner owner) {
        var a = owner.root;
        var b = a;
        var c = b;
        var d = c;
        LOG.info("deep {}", d);
    }

    void plenty(String p1,
                String p2,
                String p3,
                String p4,
                String p5,
                String p6,
                String p7,
                String p8) {
        LOG.info("{} {} {} {} {} {} {} {}", p1, p2, p3, p4, p5, p6, p7, p8);
    }
}
