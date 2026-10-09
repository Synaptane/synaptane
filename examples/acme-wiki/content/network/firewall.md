---
owner: Inès Petit
verified: 2025-02-10
review_every: 6m
---
# Firewall

Two OPNsense firewalls in high availability (CARP), `fw1` and `fw2`.

Any port opening goes through a [ticket](../tools/tickets.md) and is reviewed by the network
team. Rules are exported to GitLab every night.

| Zone | Default policy |
|---|---|
| Internet → DMZ | deny, except HTTP/HTTPS to web1 and web2 |
| Staff → servers | deny, except through the bastion |
| Servers → Internet | allow, logged |
