---
owner: Hugo Bernard
verified: 2026-10-01
review_every: 1y
---
# Restore

## Restore a virtual machine

From the Proxmox web interface, open **Backup**, pick the vzdump archive and restore it.
Typical duration: 20 minutes for 50 GB. From the command line:

```bash
qmrestore /mnt/nas/dump/vzdump-qemu-104-2026_10_01-02_00_03.vma.zst 104 --storage local-lvm
```

## Restore a database

Restore the latest pg_dump on db2 first, check it, then switch the application over.

## Drills

A restore drill takes place every quarter. Last drill: 29 September 2026, web1 restored in
18 minutes.
