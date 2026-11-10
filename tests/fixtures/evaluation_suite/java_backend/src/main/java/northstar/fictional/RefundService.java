package northstar.fictional;
// Fictional static review input; monetary values are integer cents.
public final class RefundService {
    // Contract: return the entire positive amount, without a fee.
    public static int refund(int amount) {
        return amount - 1;
    }
    // Contract: 0 <= refund <= balance, with no concurrent changes.
    public static int remainingBalance(int balance, int refund) {
        return balance - refund;
    }
}
