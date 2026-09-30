# 1. Critical evaluation of the proposal

This document reviews the five requirements in the proposal (item-level coding, status monitoring and
documents, roles, translation and hosting). For each one it says what works, what fails, and what to
do instead. Every recommendation here is built and tested in the prototype in this repository
(see [02-solution-design.md](02-solution-design.md)).

## Verdict in one paragraph

The diagnosis is right. The three real pain points are the lack of item-level traceability, documents
scattered across chats and emails, and the language barrier. One shared portal is the right answer,
and the scale (one lane, three organisations, tens of consignments a year) is small enough to
build properly. **Five changes are needed** before the idea will work in practice:

| # | Proposal | Change needed | Why |
|---|----------|---------------|-----|
| 1 | Item code contains its carton (`XXDCJM-TJM3-1-CTN-001/057`) | **Separate identity from location.** The ID holds only permanent facts. The current carton is a record, updated by scanning. | The proposal's own reason for existing (items move between cartons) makes the label wrong at the first move. |
| 2 | Shipper and Receiver "maintain" status; reminders for 4 fixed documents | **Record status as a side effect of work** (scans, uploads, one-click milestones). Required documents depend on the transport mode, and reminders are tied to milestones, with escalation. | "Real-time" fails when it depends on people remembering to update. A B/L reminder before the vessel sails is noise. |
| 3 | Three roles; Shipper and Receiver can delete | **Organisation × access level, plus ownership and custody rules.** Nothing is ever hard-deleted, and there is a tamper-evident audit log. | Deletion destroys the evidence Oversight and any dispute depend on. A packer and a manager need different rights. |
| 4 | "Everything translated, independent of add-ons" | **Two separate mechanisms.** The interface is translated exactly (built-in catalogues). Free text gets optional machine translation, with the original always kept. | Machine-translating the interface is unnecessary and unreliable. Free text is the only part that needs it. |
| 5 | Host on "a local computer" | **Small cloud server in a neutral region (Singapore or Hong Kong), with encrypted backups to a NAS in Pakistan.** | Power cuts, no public IP, and the Great Firewall make a local PC the weakest link. |

The proposal also leaves out several things that will decide whether the system pays off. The most
important are receiving reconciliation (expected vs. received), allocation of items to end users,
compliance documents for controlled goods, and the physical labelling and scanning routine. See
[§6](#6-what-the-proposal-is-missing).

---

## 1. Item-level traceability and the coding scheme

### What is right
- A unique ID for every carton (`CTN-001 … CTN-057`) is standard practice and matches the
  shipping mark "C/No. 1–57" already printed on outer cartons.
- Labelling each serialised item is the right call for equipment such as jamming modules, where every
  unit must be accounted for up to the end user.
- Generating codes automatically, and modelling a hierarchy (container › pallet › carton › item), is
  the only way this will scale. Hand-typed codes produce duplicates and typos.

### What breaks

1. **The location is built into the identity. This is the critical flaw.**
   `01DCJM-TJM3-1-CTN-001/057` contains `CTN-001`. The requirement explicitly says items are moved
   between cartons (repacking at inspection, consolidation, damage). From the first move, the label on
   the item is wrong. The alternatives are reprinting labels in the field (in practice this does not
   happen) or living with labels that point to the wrong carton, which is worse than having no carton
   on the label. International practice (GS1: SGTIN for items, SSCC for logistic units, EPCIS
   "aggregation events" for packing) keeps **identity** (never changes) separate from **location**
   (a relationship updated by scans).
2. **`/057` makes each carton ID depend on the total count.** Adding a 58th carton, or merging two
   cartons, makes all 57 IDs inconsistent. The `/` character also breaks file names and web addresses,
   and Excel turns `1/57` into a date.
3. **The IDs are not unique across consignments.** `CTN-001` and `01DCJM` recur in every
   consignment. When spreadsheets are merged or a stray item turns up in the next shipment, the scan
   is ambiguous. Every ID must include the consignment number.
4. **`XX` has only two digits**, so it stops at 99. It also has no separator (`01DCJM`), which
   invites misreading.
5. **`TJM3-1` is redundant** (the proposal already says so). Every redundant segment is another
   chance for the parts of an ID to contradict each other.
6. **Item codes need an owner.** Without a controlled item master, people will write `DCJM`, `DC-JM`
   and `DJM` for the same product.
7. **The manufacturer's serial number is missing.** For warranty, returns and end-user
   accountability, the OEM serial is the long-term identity. Capture it at packing and link it to
   the portal ID.
8. **Not everything should carry a serial number.** Cables, kits and consumables are tracked as
   quantities per carton (batch lines). 400 cable labels that nobody scans cost money and add no
   traceability.
9. **A fixed four-level hierarchy is too rigid.** Air freight has no container. Road freight via
   Khunjerab uses a truck. Some cartons ship loose, some contain inner boxes, and heavy items go in
   crates. A recursive model ("any unit can contain units or items, but only smaller kinds") covers
   every case and makes loops impossible.
10. **The consignment is not a physical layer.** It is the commercial and document grouping
    (invoice, packing list, B/L). A container can carry several consignments and a consignment can
    fill several containers, so the consignment is kept as a separate dimension.
11. **Traceability depends on scanning discipline, not on the code.** Without thermal label printers,
    QR codes and a "no scan, no move" rule at every repack, the data drifts from reality within
    weeks.
12. **Security of the marking.** Outer labels that spell out "Dual Channel Jamming Module" advertise
    theft-attractive, legally sensitive goods in transit. Contents belong in the system and on the
    inner packing slip, not on the outside.

### Recommended scheme (implemented)

| Object | ID | Parts |
|--------|----|-------|
| Consignment | `CSG-2026-0042` | prefix, year, running number (automatic) |
| Handling unit | `CSG-2026-0042-CTN-017` | consignment + unit type (`CNT` container/truck, `PLT` pallet, `CRT` crate, `CTN` carton, `BOX` inner box) + number |
| Item | `CSG-2026-0042-DCJM-017` | consignment + item-master code + number within the consignment; OEM serial stored alongside |

**Rule:** an ID contains only facts that never change: the consignment it was packed for, what the
thing is, and a number. **Where it is now** is a database record. Every move is an event recording
who, when, where and why.

Worked example (this is the seeded demo consignment):

- 40 × DCJM are registered and packed into CTN-001…003. The system numbers them DCJM-001…040 and
  spreads them 14 / 13 / 13 (DCJM-001–014, 015–027, 028–040).
- At export inspection DCJM-017 is moved from CTN-002 to CTN-003. **Its label stays valid.**
  Scanning it shows `CNT-001 (MSKU1234565) › PLT-001 › CTN-003 › DCJM-017`, and its history shows
  the move with the reason and the inspection location.
- The printed packing list regenerates from the data: CTN-002 now reads `DCJM-015–016, 018–027` and
  CTN-003 reads `DCJM-017, 028–040`.
- "C/No. 17 of 57" is still printed on the carton label as a shipping mark, but it is not part of
  the ID.

| | Proposal | Recommended |
|---|---|---|
| Unique across consignments | ✗ | ✓ |
| Survives moving the item | ✗ (label becomes wrong) | ✓ (location is a record) |
| Survives adding or removing cartons | ✗ (`/057` changes) | ✓ |
| More than 99 units | ✗ | ✓ |
| Safe in URLs, file names, Excel | ✗ (`/`) | ✓ |
| Machine-readable | ✗ (text only) | ✓ (QR code) |
| Links to the manufacturer's serial | ✗ | ✓ |
| Works for cables and consumables | ✗ (forces unit labels) | ✓ (batch lines) |

---

## 2. Real-time status monitoring and document upload

### What is right
One place for status, documents and remarks per consignment. A checklist of required documents with
automatic reminders. Remarks from the Receiver with answers from the Shipper.

### What breaks

1. **"Real-time" is a behaviour problem, not a technology problem.** Showing a change within seconds
   is easy (the prototype does it). Data is only as current as the last update, though. Updates must
   happen as part of the work itself: scan when packing, loading and receiving; record a milestone in
   one click; upload a document and its status updates. The dashboard must show what is late, not
   only what was entered.
2. **There is no status model.** "Status" needs defined milestones with an owner:
   draft › packing › packed › dispatched › export cleared › in transit (Shipper) › arrived ›
   import cleared › delivered › received & verified › closed (Receiver). Without an owner, each
   party waits for the other.
3. **The four required documents depend on the transport mode.** A Bill of Lading exists only for
   sea freight. Air freight uses an Air Waybill. Road freight via the Khunjerab Pass (now officially
   open year-round, though winter conditions still disrupt it) uses a road consignment note / TIR
   carnet. A fixed list will send wrong reminders.
4. **Reminders must follow milestones, not the calendar.** A B/L cannot exist before the vessel
   sails. A reminder before then is noise, and noise teaches people to ignore reminders. Due date =
   milestone + offset (e.g. B/L 3 days after departure). Escalation: responsible party, then a second
   reminder, then Oversight.
5. **Who owns "payment documents"?** Proof of payment (TT/SWIFT copy, LC documents) comes from the
   paying party's bank. If the Receiver pays, the Receiver holds it. The proposal makes the Shipper
   responsible, so the Shipper will chase the Receiver for it. The demo data reproduces exactly this
   conversation. Ownership must be configurable per document type.
6. **Important documents on this lane are missing.**
   - *Certificate of Origin, CPFTA Form P*: gives the China–Pakistan FTA preferential duty in
     Pakistan. Missing it can cost real money.
   - *Pakistan side*: Goods Declaration filed through WeBOC / Pakistan Single Window, and the bank's
     **Financial Instrument** registered in PSW (it replaced the old EIF).
   - *Controlled goods*: since 1 Dec 2024 China applies a unified dual-use export control list, and
     licence applications need end-user and end-use documents. In Pakistan, jamming equipment
     requires a No-Objection Certificate (the MoITT Inter-Ministerial Committee grants it, and the
     PTA enforces the rules), and sellers may only supply NOC holders. The prototype makes the export
     licence and the end-user NOC required automatically as soon as a "controlled" item type is packed.
7. **One-way remarks are too narrow.** Either party must be able to raise an issue, and each issue
   needs a state (open › answered › resolved) and a "waiting for" party. Otherwise it is a chat log,
   not an issue register.
8. **Documents need versions, not overwrites.** Keep every version, fingerprint it (SHA-256), log
   every download, and scan uploads for viruses (files cross company boundaries).
9. **Reminder channels.** Gmail, Google services and WhatsApp are blocked in mainland China. Email to
   Chinese mailboxes also needs a sender with good deliverability (SPF/DKIM/DMARC). Reach the Chinese
   team through **WeCom (企业微信) group bots** and the Pakistani team through email (and WhatsApp
   Business later).
10. **Upload fewer documents, generate more.** The packing list should be *generated* from the
    packing data, not uploaded as a separate spreadsheet. That removes the most common discrepancy:
    a packing list that disagrees with what is in the cartons.

---

## 3. Roles and permissions

1. **The three "roles" are really three organisations.** Inside each organisation, the warehouse
   packer, the documentation clerk and the manager need different rights. Model **organisation ×
   access level**: Manager (everything, including archiving and waiving), Operator (create, edit,
   upload, scan) and Viewer (read only).
2. **"Delete" is dangerous.** Hard deletion destroys the audit trail top management relies on, and
   lets one party remove evidence in a dispute (a disputed invoice or a damage report). Replace it
   with **archive with a reason**: the record stays in the history, only the uploading party's
   manager can archive, and a system administrator purges only under a written retention policy.
3. **Ownership and custody.** The Shipper must not alter the Receiver's receipts and vice versa.
   Packing may only be changed by the party that physically holds the goods: the Shipper until export
   clearance, the Receiver from arrival, nobody in transit.
4. **Oversight read-only is right**, but someone must administer accounts. Add a system
   administrator (IT or the consultant) and a joiner/leaver process. Also consider letting Oversight
   comment and escalate later.
5. **Plan for a second supplier.** Each party sees only consignments it is part of, so a second
   shipper can be added without seeing the first one's data.
6. **Security basics:** mandatory two-factor authentication (any TOTP authenticator app; SMS to
   Chinese numbers is unreliable), lock-out after repeated failures, and an **append-only audit log
   whose entries are hash-chained**, so edited or deleted history is detectable.

---

## 4. Translation

1. **There are two different problems.**
   - *The interface* (menus, statuses, document names, error messages): translate it once,
     professionally, and ship it inside the system (gettext catalogues). This is exact, instant, free,
     and works offline. It needs no translation engine at all. The prototype ships complete English,
     Simplified Chinese and Urdu catalogues (687 strings).
   - *Free text* (remarks, replies): this needs machine translation (MT). It is the only place MT
     is needed.
2. **Minimise free text.** Statuses, issue categories, document types and item names (entered once
   in three languages in the item master) are structured data and need no translation.
3. **The MT engine must be under your control, which is what "independent of add-ons" means in
   practice.**
   - *Self-hosted LibreTranslate*: runs next to the portal, so text never leaves your server, and it
     supports Chinese and Urdu. Chinese ↔ Urdu goes through English, so quality is modest. Test it
     with real remarks before relying on it.
   - *Claude API (Anthropic)*: much better for business wording and keeps codes and numbers intact,
     but remark text is sent to an external service. Decide this against the sensitivity of the goods.
   - DeepL's Urdu support is new and limited. Google Translate is unavailable inside China.
4. **Keep the original, always.** Show "Machine translation from 中文 · Show original". The original
   is authoritative, because MT can drop a negation or garble a quantity.
5. **Urdu is right-to-left** and reads best in a Nastaliq font. IDs and dates must stay
   left-to-right inside Urdu text. In testing, `2026-09-28` displayed as `28-09-2026` next to Urdu
   words until it was isolated; this is fixed in the prototype.
6. **Roman Urdu** (Urdu written in Latin letters, common in chat) translates badly. Ask users to write
   in English or in Urdu script.
7. **English stays the document language.** Trade documents are in English. Printed labels are
   bilingual English / Chinese: the warehouse is in China and customs in Pakistan reads English.

---

## 5. Hosting ("a local computer")

**Why a local computer is the weakest option:**

- *Availability.* Power cuts (load-shedding) and consumer internet outages in Pakistan. Many ISPs use
  carrier-grade NAT, so there is no public IP address and remote access needs tunnels.
- *Security.* Exposing an office PC to the internet puts the whole office network at risk, and
  patching and physical security are informal.
- *Data loss.* One disk, no off-site copy.
- *Cross-border access.* Chinese users reaching a server on a Pakistani consumer line will get slow,
  unstable connections. Tunnel services perform poorly inside mainland China. VPNs are restricted in
  China and regulated in Pakistan, so a VPN-only design would lock the Shipper out.

| Option | Reachable from China | Reachable from Pakistan | Reliability | Data jurisdiction | Verdict |
|---|---|---|---|---|---|
| Local PC / office server in Pakistan | poor | good (office only) | low | Pakistan | only as backup target |
| Cloud in mainland China | excellent | fair | high | China; needs ICP filing by a Chinese entity | not recommended |
| Cloud in Hong Kong | very good | good | high | Hong Kong | good |
| **Cloud in Singapore** | good | good | high | Singapore (neutral, strong data law) | **recommended** |
| Cloud in UAE (Dubai) | fair | very good | high | UAE | alternative |

**Recommendation:** a small virtual machine (2 vCPU, 4 GB RAM) in **Singapore** or **Hong Kong** from
a provider with good mainland-China peering. Before committing, run a one-week latency test from both
offices at peak hours. On it run the Docker stack in this repository: HTTPS certificates are automatic,
only ports 80/443 are open, SSH is key-only, security updates are automatic, and there are
**nightly encrypted backups** to cloud storage *and* to a NAS at the Oversight office in Pakistan.
That NAS gives you the "local copy" of the data without making a local PC the thing everyone depends
on. Details in [04-deployment-guide.md](04-deployment-guide.md).

---

## 6. What the proposal is missing

1. **Receiving reconciliation.** This is the Receiver's core use case: expected vs. received vs.
   damaged, per unit and per item, with discrepancy reports. It is included in the prototype.
2. **Allocation to end users.** The stated purpose of traceability is to "send the right item to the
   right end user". That needs allocation (item IDs → end user / order), a check that the end user's
   NOC is on file before dispatch, a delivery note, and a dispatch scan. This is phase 4 of the plan.
3. **Compliance data.** Controlled-item flags, HS codes, licences, end-user certificates and NOCs,
   with the documents required automatically.
4. **Master data governance.** Who creates item codes, and names in three languages.
5. **Hardware and routines.** Thermal label printers (100×150 mm and 50×30 mm), 2D scanners or
   phones, label stock, and a written "no scan, no move" rule.
6. **An audit trail** usable in disputes (included, hash-chained).
7. **Offline scanning** for warehouses or ports with poor connectivity (phase 4).
8. **KPIs for Oversight:** documents on file, overdue documents, delayed shipments, open issues and
   their age, receiving progress, and dwell time at port or customs.
9. **Data protection.** The portal stores business contact data of staff in both countries. Give
   staff a short privacy notice, and check China's rules on cross-border transfer of personal
   information with the Shipper (small volumes like this generally fall under the 2024 exemptions,
   but confirm).
10. **Change management.** Bilingual standard operating procedures, training, and a pilot run in
    parallel with the current process before switching over.

## 7. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| People do not scan every move, so the data drifts | high | high | scanning made fast (phone or handheld); custody rules; receiving reconciliation exposes gaps; KPIs per party |
| Controlled goods handled without valid licence or NOC | medium | very high | controlled flag auto-requires documents; allocation checks the end-user NOC (phase 4) |
| Connectivity across the Great Firewall | medium | medium | neutral-region hosting tested from both sides; no Google/CDN dependencies; WeCom notifications |
| Poor machine translation causes a misunderstanding | medium | medium | structured data first; original always visible; LLM engine for important lanes |
| Single developer or maintainer dependency | medium | high | standard stack (Django, PostgreSQL, Docker), tests, documented runbooks, support agreement |
| Scope creep into a full ERP | medium | medium | phased plan; integrate by CSV/API instead of rebuilding accounting |

## 8. Build vs. buy

Off-the-shelf ERPs (e.g. Odoo, ERPNext) have serial/lot tracking and nested packages. Freight
visibility SaaS tracks containers. For **one lane shared by three organisations** they still need
heavy customisation to provide:

- external-party portal access with custody rules,
- the coding scheme,
- an Urdu right-to-left interface,
- milestone-driven document reminders,
- comment translation.

A focused custom portal is justified at this scale **provided** there is a maintenance agreement. It
exports everything to CSV/Excel, so it can feed an ERP later.

---

### Sources for the lane-specific facts
- China–Pakistan FTA Certificate of Origin (Form P): [MOFCOM FTA portal](https://fta.mofcom.gov.cn/topic/enpakistan.shtml), [Pakistan rules of origin (commerce.gov.pk)](https://www.commerce.gov.pk/wp-content/uploads/pdf/Annex-II_PK-CN_FTA.pdf)
- Pakistan Single Window, EIF replaced by bank Financial Instruments: [PSW – elimination of EIF/EFE](https://www.psw.gov.pk/elimination-of-eif-efe), [SBP FE Manual ch. 13](https://www.sbp.org.pk/fe_manual/pdf/2020/Chp-13.pdf)
- Jammers in Pakistan require an NOC; sellers may only supply NOC holders: [Dawn](https://www.dawn.com/news/1037246), [PTA notice](https://www.pta.gov.pk/category/illegal-mobile-jammers-to-be-removed-806219675-2023-06-01)
- China's unified dual-use export control list (effective 1 Dec 2024): [Eversheds Sutherland](https://www.eversheds-sutherland.com/en/global/insights/effective-1-december-2024-pr-cs-new-regulation-on-the-export-control-of-dual-use-items), [Morrison Foerster](https://www.mofo.com/resources/insights/241216-china-s-new-export-control-framework-key-changes)
- Khunjerab Pass open year-round since Dec 2024: [Dawn](https://www.dawn.com/news/1906063/khunjerab-pass-opens-for-year-round-trade-and-travel), [Radio Pakistan](https://www.radio.gov.pk/05-12-2024/khunjerab-pass-to-remain-open-round-the-year)
- LibreTranslate language support (Chinese, Urdu): [LibreTranslate docs](https://docs.libretranslate.com/guides/supported_languages/)
- DeepL language expansion incl. Urdu (beta): [DeepL documentation](https://developers.deepl.com/docs/getting-started/supported-languages)
