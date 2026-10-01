# SOP demo dataset

The dummy data of the SOP demo: one sea consignment of 12 cartons on 2 pallets. All names and numbers are invented.

| File | What it holds |
|---|---|
| `item-master.json` | Five products with names in English, Chinese and Urdu: DCJM (serialised, controlled), PSU, CTRL, ANT (serialised) and CBL (batch) |
| `packing-list-12-cartons.csv` | The packing list: 21 rows, one per carton and item type, with maker serial numbers, a batch number, and each carton's weight and size |

Rehearse on a practice copy with its own data folder, never on the live portal. Load the products, then import the CSV on the consignment's **Packing & traceability** tab:

```
CNPK-Portal --data-dir <practice folder> --port 8002
CNPK-Portal --data-dir <practice folder> manage loaddata sop-demo/item-master.json
```

From the source code, run `python launcher/portal_launcher.py` with the same options and `docs/sop-demo/item-master.json`. Loading the file again updates the same five products.

The import gives pallets PLT-001 and PLT-002, cartons CTN-001 to CTN-012 and 40 item IDs for 98 pieces and 173.20 kg. `src/logistics/tests/test_sop_demo.py` checks that it still does.
