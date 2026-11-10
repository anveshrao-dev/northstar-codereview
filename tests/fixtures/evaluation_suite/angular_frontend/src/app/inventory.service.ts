import { Injectable } from '@angular/core';
// Fictional static source; inputs are nonnegative integer units, requested > 0.
@Injectable({ providedIn: 'root' })
export class InventoryService {
  // Contract: exact stock is sufficient to fulfill an order.
  canFulfill(available: number, requested: number): boolean {
    return available > requested;
  }
  // Contract: requested <= available; no concurrent changes.
  stockAfterFulfillment(available: number, requested: number): number {
    return available - requested;
  }
}
