# Monitoring

Zabbix watches the servers. Alerts go to the `#alerts` channel and, at night, to the person
[on call](../team/on-call.md).

Disk usage alert threshold: **85 %**.

Every Monday at 8 a.m., the dashboard is exported as a standalone HTML page for the team
meeting: latest one, [weekly status report, week 40](documents/status-report-2026-w40.html).

We are evaluating Prometheus to complement Zabbix: see the
[observability project](../projects/observability.md).
