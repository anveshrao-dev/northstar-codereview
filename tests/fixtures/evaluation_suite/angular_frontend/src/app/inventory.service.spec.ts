import { InventoryService } from './inventory.service';
describe('InventoryService fictional contract', () => {
  const service = new InventoryService();
  it('checks below and above stock', () => {
    expect(service.canFulfill(4, 5)).toBeFalse();
    expect(service.canFulfill(6, 5)).toBeTrue();
  });
  it('subtracts a fulfilled order including boundaries', () => {
    expect(service.stockAfterFulfillment(5, 5)).toBe(0);
    expect(service.stockAfterFulfillment(6, 5)).toBe(1);
  });
});
