package northstar.fictional;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
class RefundServiceTest {
    @Test void fullRefundContract() {
        assertEquals(100, RefundService.refund(100));
    }
    @Test void remainingBalanceBoundaries() {
        assertEquals(100, RefundService.remainingBalance(100, 0));
        assertEquals(0, RefundService.remainingBalance(100, 100));
        assertEquals(60, RefundService.remainingBalance(100, 40));
    }
}
