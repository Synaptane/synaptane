# Servers

| Name | Role | Location |
|---|---|---|
| web1 | public website | rack A |
| web2 | public website (standby) | rack A |
| db1 | primary PostgreSQL | rack B |
| db2 | PostgreSQL read replica | rack B |
| pve1 | [[proxmox]] hypervisor | rack B |

Physical servers are covered by a next-business-day hardware support contract until 2027.

Access goes through the bastion, `bastion.acme.internal`, with your personal SSH key.
