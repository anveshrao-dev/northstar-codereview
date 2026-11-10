package northstar.fictional;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
class RefundServiceTest {
    @Test void fullRefundContract() {
        assertEquals(100, RefundService.refund(100));
    }
}
