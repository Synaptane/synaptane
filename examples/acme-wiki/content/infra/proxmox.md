# Proxmox

A cluster of three hypervisors (pve1, pve2, pve3) running Proxmox VE 8.

Virtual machines are backed up every night at 2 a.m. with vzdump, to the NAS
(see [Backups](backups.md)).

Why Proxmox: see the [2025 decision](../decisions/2025-01-proxmox.md).

## Useful commands

```bash
# List the virtual machines of a node
qm list

# Cluster health
pvecm status
```
