# DNS

- Public zone `acme.example`, hosted by our domain registrar.
- Internal zone `acme.internal`, served by two Unbound resolvers (ns1 and ns2).

Default time to live (TTL): 3600 seconds. Lower it to 300 seconds a day before any planned
migration.
