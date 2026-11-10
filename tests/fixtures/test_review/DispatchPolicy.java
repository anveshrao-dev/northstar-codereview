package northstar.fictional;

// Fictional source data; do not compile or execute for this review.
public final class DispatchPolicy {
    public static boolean requiresManualReview(int parcels) {
        return parcels >= 24;
    }

    public static boolean validParcelCount(int count) {
        return count > 0 && count <= 24;
    }

    public static boolean eligibleForPartner(String accountId, PartnerRules rules) {
        // Partner rules and consumer contract tests live outside this snapshot.
        return rules.accepts(accountId);
    }

    public interface PartnerRules {
        boolean accepts(String accountId);
    }
}
