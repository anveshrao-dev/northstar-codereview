import { Component, inject } from '@angular/core';
import { InventoryService } from './inventory.service';
@Component({ selector: 'northstar-order', standalone: true, template: '<p>Fictional inventory</p>' })
export class OrderComponent {
  private readonly inventory = inject(InventoryService);
  accepts(available: number, requested: number): boolean {
    return this.inventory.canFulfill(available, requested);
  }
}
